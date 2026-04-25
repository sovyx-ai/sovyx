"""Windows capture-chain APO detector (stdlib-only, via winreg).

Audio Processing Objects (APOs) are DLL-backed DSP stages the Windows
audio engine runs on every capture endpoint in shared mode. They are
registered per-endpoint under ::

    HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\MMDevices\\
    Audio\\Capture\\{endpoint_id}\\FxProperties

The values there reference either raw CLSIDs or Software-Device
(``SWD\\DRIVERENUM\\...``) paths whose trailing segment carries the
human-readable package name (e.g. ``VocaEffectPack``).

Why this module exists
======================

Microsoft began shipping *Windows Voice Clarity* (internal package:
``voiceclarityep`` / ``VocaEffectPack``) via Windows Update in early
2026. It registers itself as a capture APO on every active microphone
and applies ML-based noise suppression **before** PortAudio or any
user-space app can see a frame. On a significant fraction of hardware
(observed on Razer BlackShark V2 Pro, but the class of bug is not
headset-specific) the post-APO waveform retains plausible RMS but is
unusable for downstream VAD — the Silero v5 model's speech probability
stays stuck at ``< 0.01`` regardless of what the user says.

Identifying this failure mode from logs alone is very hard: the audio
capture heartbeat looks healthy, the pipeline looks healthy, yet the
orchestrator never transitions out of IDLE. This detector surfaces the
root cause as structured data so:

1. Startup can log ``voice_apo_detected`` with the full chain.
2. The orchestrator's auto-bypass heuristic can decide whether to
   retry the stream in WASAPI exclusive mode.
3. ``sovyx doctor`` and the dashboard can render an actionable message
   instead of a generic "silence detected".

Design notes
============

- **Windows-only.** On Linux/macOS the module imports cleanly but every
  public function returns an empty list. ``winreg`` is stdlib and
  Windows-only; the import is guarded.
- **Read-only.** We never write to the registry. The durable bypass
  lives in ``capture_wasapi_exclusive`` — we do not fight Microsoft's
  defaults at the OS level.
- **Best-effort.** Any I/O / decoding failure is logged at DEBUG and
  collapses to "no APOs observed" for that endpoint. A missing
  FxProperties subkey is the common case on default Windows installs,
  not an error.
- **Low-cardinality output.** The returned report lists friendly APO
  names (from a small curated catalog) plus raw CLSIDs for forensics.
  The ``voice_clarity_active`` shortcut exists because that single bit
  drives 100% of the auto-bypass decision tree.
"""

from __future__ import annotations

import re
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from sovyx.observability.logging import get_logger

logger = get_logger(__name__)


_CAPTURE_ROOT = r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Capture"


# W10 — bounded walk over the MMDevices capture tree. The MMDevices
# subkey legitimately holds tens to ~100 endpoints on a heavily-used
# system (every USB mic ever plugged in keeps a stale entry until
# Windows expires it). 256 leaves headroom for outliers (workstations
# with audio interfaces, virtualised stacks) while capping the worst
# case if the registry gets corrupt and EnumKey returns garbage forever.
_MAX_CAPTURE_ENDPOINTS = 256
"""Hard ceiling on endpoints walked per :func:`detect_capture_apos`
call. Hitting this emits a one-shot WARN — it should be rare enough
to indicate either an exotic deployment OR a corrupted registry.
Active endpoints (the ones that matter) are a tiny subset; the
remainder is mostly stale unplugged-device residue."""

_GIL_YIELD_INTERVAL = 32
"""How many endpoints to walk between cooperative GIL releases. The
function is called via ``asyncio.to_thread`` from the dashboard
endpoint and factory startup — the worker thread holds the GIL
during the loop. Yielding every N iterations lets other Python-side
threads (capture / WS) make progress while the registry walk runs."""


def _norm(value: str) -> str:
    """Unicode-aware key for substring / equality matching (W12).

    ``str.lower()`` is locale-incomplete for non-ASCII case folding —
    German ``ß`` stays as ``ß`` (it should fold to ``ss``); Turkish
    dotted/dotless ``İ``/``ı`` are mishandled; full-width ASCII (e.g.
    pasted from Asian-locale device names) won't match its half-width
    equivalent. ``unicodedata.normalize("NFKC", ...).casefold()`` is
    the W3C-recommended canonical normalisation that fixes all three:

    * NFKC collapses compatibility codepoints (full-width → half-width,
      ligatures → letters, superscripts → bare digits).
    * ``casefold()`` is Unicode-aware case folding (``ß`` → ``ss``,
      ``İ`` → ``i̇``, etc.) — strictly stronger than ``lower()``.

    Empty / whitespace-only inputs collapse to ``""`` so substring
    short-circuits don't match accidentally.
    """
    if not value:
        return ""
    return unicodedata.normalize("NFKC", value).strip().casefold()


# PKEY_* registry keys (property-store GUID + PID).
#
# History: prior to the 2026-04 fix this module read PID 2 (DeviceDesc,
# "Microfone") as the friendly name and PID 6 of the device-interface
# GUID (DeviceInterface_FriendlyName) as the "enumerator". Both were
# wrong: DeviceDesc collides across every USB mic on the system (so the
# substring matcher in ``find_endpoint_report`` would happily return the
# C922's report when asked about the Razer headset), and the PID-6
# value isn't an enumerator name at all. The constants below now point
# at the documented PKEY_* slots used by the Windows audio stack.
_PROPS_FMTID = "{a45c254e-df1c-4efd-8020-67d146a850e0}"
_DEV_IFACE_FMTID = "{b3f8fa53-0004-438e-9003-51a46e139bfc}"
_PKEY_DEVICE_FRIENDLY_NAME = f"{_PROPS_FMTID},14"
_PKEY_DEVICE_DESC = f"{_PROPS_FMTID},2"
_PKEY_ENUMERATOR_NAME = f"{_PROPS_FMTID},24"
_PKEY_DEVICE_INTERFACE_NAME = f"{_DEV_IFACE_FMTID},6"
_DEVICE_STATE_ACTIVE = 1


# Curated catalog of known capture APO CLSIDs (uppercase, braces included).
# Kept intentionally small — unknown CLSIDs are still returned as raw values
# so operators can grep them; only the ones we want the dashboard to *name*
# live here.
_KNOWN_CLSIDS: dict[str, str] = {
    "{62DC1A93-AE24-464C-A43E-452F824C4250}": "MS Default Stream FX",
    "{47620F45-DBE4-47F3-B308-09F9120CFB05}": "MS Communications Mode APO",
    "{112F45E0-A531-42A8-9DE5-57F0EB73C6DE}": "MS Voice Capture DMO",
    "{CF1DDA2C-3B93-4EFE-8AA9-DEB6F8D4FDF1}": "MS Acoustic Echo Cancellation",
    "{9CF81848-DE9F-4BDF-B177-A9D8B16A7AAB}": "MS Automatic Gain Control",
    "{1B20CB5B-6E1B-4BFE-A2F6-7E3E81C7E0F4}": "MS Voice Isolation",
    "{7A8B0F43-6C2E-4C85-A1A6-C9F1F7D50E9D}": "MS Voice Focus",
    # KSCATEGORY_AUDIO_PROCESSING_OBJECT — seen as the "kind" GUID wrapping
    # the actual package. Not an APO itself, but its presence in an FxProps
    # value signals a real APO chain is installed.
    "{96BEDF2C-18CB-4A15-B821-5E95ED0FEA61}": "Windows APO container",
}


# Substring patterns (case-insensitive) matched against the SWD path /
# package-name portion of an FxProperties value. These identify APO
# packages that do not always expose a stable CLSID — Windows Voice
# Clarity in particular reuses the generic ``voiceisolation`` CLSID but
# the package name ``VocaEffectPack`` / ``voiceclarityep`` is stable.
_PACKAGE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("vocaeffectpack", "Windows Voice Clarity"),
    ("voiceclarityep", "Windows Voice Clarity"),
    ("voiceclarity", "Windows Voice Clarity"),
    ("voiceisolation", "MS Voice Isolation"),
    ("voicefocus", "MS Voice Focus"),
)


_VOICE_CLARITY_LABEL = "Windows Voice Clarity"

_CLSID_RE = re.compile(
    r"\{[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\}"
)


@dataclass(frozen=True, slots=True)
class CaptureApoReport:
    """Summary of the APO chain bound to one active capture endpoint.

    Attributes:
        endpoint_id: The MMDevices endpoint GUID (registry subkey name).
        endpoint_name: The PKEY_Device_FriendlyName property — what the
            user sees in Sound settings (e.g. ``"Microfone (Razer
            BlackShark V2 Pro)"``). Falls back to PKEY_DeviceDesc when
            the friendly name is absent (rare, but observed on some OEM
            installs).
        device_interface_name: The PKEY_DeviceInterface_FriendlyName
            (e.g. ``"Razer BlackShark V2 Pro"``) — typically the most
            distinctive, vendor-supplied string for a USB endpoint and
            our preferred matching key against PortAudio device names.
        enumerator: The PKEY_Device_EnumeratorName (``USB``, ``BTHENUM``,
            ``MMDevAPI``, ``SWD``, ...). Useful for correlating with the
            PortAudio device host API.
        fx_binding_count: Number of non-empty values under FxProperties
            for this endpoint. Zero means "no APO chain observed" — the
            raw Windows default has at least the SysFx value.
        known_apos: Deduplicated friendly names recognised via
            :data:`_KNOWN_CLSIDS` or :data:`_PACKAGE_PATTERNS`, in
            insertion order.
        raw_clsids: All unique ``{GUID}`` strings observed across the
            FxProperties values, preserved for forensics (grep-friendly).
        voice_clarity_active: ``True`` iff ``Windows Voice Clarity`` is
            in :attr:`known_apos`. Hoisted out for the auto-bypass
            decision tree — this one bit is load-bearing.
    """

    endpoint_id: str
    endpoint_name: str
    enumerator: str
    fx_binding_count: int
    device_interface_name: str = ""
    known_apos: list[str] = field(default_factory=list)
    raw_clsids: list[str] = field(default_factory=list)
    voice_clarity_active: bool = False
    unknown_clsid_dll_info: dict[str, Any] = field(default_factory=dict)
    """WI3 wire-up: per-CLSID DLL introspection for any CLSID not in
    the static catalog. Empty unless
    ``VoiceTuningConfig.voice_apo_dll_introspection_enabled`` is
    True. Each value is an
    :class:`~sovyx.voice._apo_dll_introspect.ApoDllInfo` carrying
    the registered DLL path, version-info fields, and a
    ``is_microsoft_signed`` heuristic. Typed as ``dict[str, Any]``
    instead of ``dict[str, ApoDllInfo]`` to avoid a circular import
    between this module and ``_apo_dll_introspect``; consumers can
    cast safely or use the ``Any`` directly for dashboard rendering."""


def detect_capture_apos() -> list[CaptureApoReport]:
    """Enumerate every active capture endpoint and its APO chain.

    Returns an empty list on non-Windows platforms or when the MMDevices
    registry tree is unreadable (rare — the tree is present on every
    supported Windows build). Each active endpoint yields exactly one
    :class:`CaptureApoReport`; inactive endpoints (unplugged mics, etc.)
    are skipped so the report tracks what the user is actually using.

    The function is pure (no observability side effects beyond W4 + W10
    structured warnings) so callers control when to log the detection
    event — startup code logs once; diagnostics endpoints may call it
    on every request.

    **Threading model (W10).** This function performs synchronous
    Windows registry I/O and blocks the calling thread. Async callers
    MUST wrap it in ``asyncio.to_thread()`` (the dashboard endpoint
    and the doctor CLI already do). The walk is internally bounded
    by :data:`_MAX_CAPTURE_ENDPOINTS` and yields the GIL every
    :data:`_GIL_YIELD_INTERVAL` iterations so other Python-side
    threads (capture / websocket / metrics) make progress while
    the registry walk runs.
    """
    if sys.platform != "win32":
        return []

    try:
        import winreg
    except ImportError:  # pragma: no cover — impossible on win32
        return []

    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _CAPTURE_ROOT)
    except OSError as exc:
        # W4 — registry permission silent fail: previously this swallowed
        # at DEBUG (invisible in operator logs). Promoted to WARN with an
        # action_required hint because reaching this branch on a normal
        # Windows install means either (a) the MMDevices tree is corrupt
        # (rare; reinstall driver) or (b) Sovyx is running under a token
        # that lost SeBackupPrivilege (unusual; service-account hardening).
        # Either way the operator deserves to see it.
        logger.warning(
            "voice.windows.apo_registry_access_denied",
            error=str(exc),
            error_type=type(exc).__name__,
            registry_root=_CAPTURE_ROOT,
            **{
                "voice.action_required": (
                    "Cannot read MMDevices capture registry. APO detection "
                    "is disabled — Voice Clarity / Communications Mode "
                    "auto-bypass cannot fire. Confirm Sovyx is running "
                    "under a user token with read access to "
                    "HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\"
                    "MMDevices\\Audio\\Capture, or reinstall the audio "
                    "driver if the registry tree is corrupt."
                ),
            },
        )
        return []

    reports: list[CaptureApoReport] = []
    try:
        idx = 0
        cap_hit = False
        while True:
            # W10 — bounded walk: cap at _MAX_CAPTURE_ENDPOINTS to
            # avoid pathological loops on a corrupt registry that
            # never raises StopIteration. Hitting the cap is rare
            # enough to deserve a single WARN with the count.
            if idx >= _MAX_CAPTURE_ENDPOINTS:
                cap_hit = True
                break
            try:
                endpoint_id = winreg.EnumKey(root, idx)
            except OSError:
                break
            idx += 1
            try:
                report = _read_endpoint(winreg, root, endpoint_id)
            except OSError as exc:
                logger.debug(
                    "voice_apo_endpoint_read_failed",
                    endpoint_id=endpoint_id,
                    detail=str(exc),
                )
                continue
            if report is not None:
                reports.append(report)
            # W10 — periodic GIL release so other Python threads
            # (capture / WS) make progress during the walk. zero-arg
            # sleep is a Python convention for "yield to scheduler"
            # without actually blocking.
            if idx % _GIL_YIELD_INTERVAL == 0:
                time.sleep(0)
        if cap_hit:
            logger.warning(
                "voice.windows.apo_walk_cap_hit",
                walked=idx,
                cap=_MAX_CAPTURE_ENDPOINTS,
                **{
                    "voice.action_required": (
                        f"MMDevices capture registry has more than "
                        f"{_MAX_CAPTURE_ENDPOINTS} endpoint subkeys — "
                        "stopping the walk to avoid unbounded scan time. "
                        "This usually means the audio driver registry is "
                        "polluted with stale endpoint entries (every USB "
                        "mic ever plugged in keeps a residue subkey). "
                        "Consider running `pnputil /enum-devices /removed` "
                        "and pruning unused devices via Device Manager."
                    ),
                },
            )
    finally:
        winreg.CloseKey(root)
    return reports


def find_endpoint_report(
    reports: list[CaptureApoReport],
    *,
    device_name: str | None,
    endpoint_id: str | None = None,
) -> CaptureApoReport | None:
    """Pick the report that corresponds to the active capture device.

    Resolution order, strongest signal first:

    1. **Endpoint GUID exact match.** When the caller can supply the
       MMDevices ``{guid}`` directly (e.g. from PortAudio's WASAPI
       extra info), this is unambiguous.
    2. **Device-interface name match.** ``PKEY_DeviceInterface_FriendlyName``
       carries the vendor-supplied string (``"Razer BlackShark V2 Pro"``)
       which is reliably distinctive across endpoints. We require both
       directions of substring containment AND a minimum needle length
       so that a generic prefix like ``"Microfone"`` cannot promiscuously
       match every USB mic on the system.
    3. **Endpoint friendly-name match** with the same strict containment
       rule, as a final fallback.

    The previous implementation matched any substring overlap on the
    friendly name; on systems where every USB mic is exposed as
    ``"Microfone (...)"`` (Windows pt-BR, common configuration) the
    first endpoint always won, regardless of which device was actually
    in use. That bug masked Voice Clarity APO detection on the active
    headset and is the reason the strict matcher exists.
    """
    if not reports:
        return None

    if endpoint_id:
        ep_needle = _norm(endpoint_id)
        if ep_needle:
            for rep in reports:
                if _norm(rep.endpoint_id) == ep_needle:
                    return rep

    if not device_name:
        return None
    needle = _norm(device_name)
    if len(needle) < 4:
        return None

    for rep in reports:
        hay = _norm(rep.device_interface_name)
        if hay and _strict_name_match(needle, hay):
            return rep

    for rep in reports:
        hay = _norm(rep.endpoint_name)
        if hay and _strict_name_match(needle, hay):
            return rep

    return None


def _strict_name_match(needle: str, hay: str) -> bool:
    """Return ``True`` when ``needle`` and ``hay`` plausibly name the same device.

    Both strings must be at least 4 chars and the shorter of the two
    must be at least 6 chars (or the entire opposite string) to count.
    This rejects degenerate matches like ``"Microfone"`` (9 chars but
    not distinctive) being absorbed by every ``"Microfone (X)"`` value.
    """
    if not needle or not hay:
        return False
    if needle == hay:
        return True
    short, long_ = (needle, hay) if len(needle) <= len(hay) else (hay, needle)
    if len(short) < 6:
        return False
    if short not in long_:
        return False
    # Reject prefix-only collisions like bare "microfone" ⊂ "microfone (...)".
    # Require the short string to carry a distinctive token *past* a leading
    # generic device-class word. If the short string is itself a single
    # device-class word ("microfone", "microphone", "line in", ...) we
    # bail out and let the caller fall back to endpoint_id matching.
    if " " not in short and "(" not in short:
        return False
    distinctive = short.split(" ", 1)[-1] if " " in short else short.split("(", 1)[-1]
    distinctive = distinctive.strip(" ()")
    return len(distinctive) >= 4 and distinctive in long_


def _read_endpoint(winreg_mod: object, root: object, endpoint_id: str) -> CaptureApoReport | None:
    """Read one MMDevices capture endpoint's DeviceState + Properties + FxProperties."""
    # ``winreg`` functions are untyped stubs in mypy; cast at boundary.
    wr: object = winreg_mod

    try:
        ep = wr.OpenKey(root, endpoint_id)  # type: ignore[attr-defined]
    except OSError:
        return None
    try:
        try:
            state, _ = wr.QueryValueEx(ep, "DeviceState")  # type: ignore[attr-defined]
        except OSError:
            state = 0
        if int(state) != _DEVICE_STATE_ACTIVE:
            return None

        friendly = _read_property(wr, ep, "Properties", _PKEY_DEVICE_FRIENDLY_NAME)
        if not friendly:
            # OEM installs occasionally omit PKEY_Device_FriendlyName and only
            # populate PKEY_DeviceDesc ("Microfone"). It's a worse string for
            # disambiguation but better than empty.
            friendly = _read_property(wr, ep, "Properties", _PKEY_DEVICE_DESC)
        enumerator = _read_property(wr, ep, "Properties", _PKEY_ENUMERATOR_NAME)
        device_iface = _read_property(wr, ep, "Properties", _PKEY_DEVICE_INTERFACE_NAME)
        fx_values = _read_fx_properties(wr, ep)

        known: list[str] = []
        seen: set[str] = set()
        raw_clsids: list[str] = []
        voice_clarity = False
        for value in fx_values:
            text = _stringify(value)
            for clsid in _CLSID_RE.findall(text):
                up = clsid.upper()
                if up not in raw_clsids:
                    raw_clsids.append(up)
                label = _KNOWN_CLSIDS.get(up)
                if label and label not in seen:
                    seen.add(label)
                    known.append(label)
            lowered = _norm(text)
            for needle, label in _PACKAGE_PATTERNS:
                if needle in lowered and label not in seen:
                    seen.add(label)
                    known.append(label)
            if _VOICE_CLARITY_LABEL in known:
                voice_clarity = True

        # WI3 wire-up: opt-in DLL introspection for unknown CLSIDs.
        # Static catalog hits stay zero-cost; only CLSIDs not in the
        # catalog trigger the registry + DLL header read. Errors are
        # caught + logged, never propagated — the report still ships.
        unknown_dll_info = _maybe_introspect_unknown_clsids(raw_clsids)

        return CaptureApoReport(
            endpoint_id=endpoint_id,
            endpoint_name=str(friendly or ""),
            enumerator=str(enumerator or ""),
            fx_binding_count=len(fx_values),
            device_interface_name=str(device_iface or ""),
            known_apos=known,
            raw_clsids=raw_clsids,
            voice_clarity_active=voice_clarity,
            unknown_clsid_dll_info=unknown_dll_info,
        )
    finally:
        wr.CloseKey(ep)  # type: ignore[attr-defined]


def _maybe_introspect_unknown_clsids(
    raw_clsids: list[str],
) -> dict[str, Any]:
    """WI3 wire-up: opt-in DLL introspection for CLSIDs not in
    :data:`_KNOWN_CLSIDS`.

    Returns a dict mapping each unknown CLSID to its
    :class:`~sovyx.voice._apo_dll_introspect.ApoDllInfo`. Empty dict
    when:
      * The opt-in flag is off (default),
      * No CLSIDs are unknown (all matched the static catalog),
      * The introspection module crashes (logged + swallowed).

    Never raises. Per-CLSID introspection failures collapse into
    that CLSID's ApoDllInfo carrying notes — they don't abort the
    full enrichment for siblings."""
    try:
        from sovyx.engine.config import VoiceTuningConfig

        if not VoiceTuningConfig().voice_apo_dll_introspection_enabled:
            return {}
    except Exception as exc:  # noqa: BLE001 — config read isolation
        logger.warning(
            "voice.apo.config_read_failed",
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return {}

    unknown_clsids = [c for c in raw_clsids if c not in _KNOWN_CLSIDS]
    if not unknown_clsids:
        return {}

    try:
        from sovyx.voice._apo_dll_introspect import introspect_apo_clsid
    except Exception as exc:  # noqa: BLE001 — import isolation
        logger.warning(
            "voice.apo.introspect_import_failed",
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return {}

    result: dict[str, Any] = {}
    for clsid in unknown_clsids:
        try:
            result[clsid] = introspect_apo_clsid(clsid)
        except Exception as exc:  # noqa: BLE001 — per-CLSID isolation
            logger.warning(
                "voice.apo.introspect_clsid_failed",
                clsid=clsid,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            # Skip this CLSID — siblings still get enriched.
    return result


def _read_property(winreg_mod: object, ep: object, subkey: str, value_name: str) -> str | None:
    """Read a single PKEY_* value from an endpoint's sub-key."""
    wr: object = winreg_mod
    try:
        sub = wr.OpenKey(ep, subkey)  # type: ignore[attr-defined]
    except OSError:
        return None
    try:
        try:
            value, _ = wr.QueryValueEx(sub, value_name)  # type: ignore[attr-defined]
        except OSError:
            return None
        return value if isinstance(value, str) else str(value)
    finally:
        wr.CloseKey(sub)  # type: ignore[attr-defined]


def _read_fx_properties(winreg_mod: object, ep: object) -> list[object]:
    """Return every non-empty value stored under ``FxProperties``."""
    wr: object = winreg_mod
    try:
        sub = wr.OpenKey(ep, "FxProperties")  # type: ignore[attr-defined]
    except OSError:
        return []
    values: list[object] = []
    try:
        idx = 0
        while True:
            try:
                _, data, _ = wr.EnumValue(sub, idx)  # type: ignore[attr-defined]
            except OSError:
                break
            idx += 1
            if data is None:
                continue
            values.append(data)
    finally:
        wr.CloseKey(sub)  # type: ignore[attr-defined]
    return values


def _stringify(value: object) -> str:
    """Coerce a registry value (``REG_SZ``, ``REG_BINARY``, ...) into a string.

    The FxProperties values we care about are typically ``REG_SZ`` on
    modern Windows, but older builds stored the same data as a
    zero-terminated wide-string inside a ``REG_BINARY`` blob. The
    CLSID regex is tolerant of either — we just need the text.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        # UTF-16 LE with trailing nulls is the common shape for legacy REG_BINARY.
        try:
            decoded = value.decode("utf-16-le", errors="ignore")
        except (UnicodeDecodeError, LookupError):  # pragma: no cover — errors=ignore
            decoded = ""
        return decoded.replace("\x00", "")
    return str(value)


__all__ = [
    "CaptureApoReport",
    "detect_capture_apos",
    "find_endpoint_report",
]
