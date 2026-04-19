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
from dataclasses import dataclass, field

from sovyx.observability.logging import get_logger

logger = get_logger(__name__)


_CAPTURE_ROOT = r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Capture"


# PKEY_* registry keys (property-store GUID + PID).
_PKEY_DEVICE_FRIENDLY_NAME = "{a45c254e-df1c-4efd-8020-67d146a850e0},2"
_PKEY_ENUMERATOR_NAME = "{b3f8fa53-0004-438e-9003-51a46e139bfc},6"
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
            BlackShark V2 Pro)"``).
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
    known_apos: list[str] = field(default_factory=list)
    raw_clsids: list[str] = field(default_factory=list)
    voice_clarity_active: bool = False


def detect_capture_apos() -> list[CaptureApoReport]:
    """Enumerate every active capture endpoint and its APO chain.

    Returns an empty list on non-Windows platforms or when the MMDevices
    registry tree is unreadable (rare — the tree is present on every
    supported Windows build). Each active endpoint yields exactly one
    :class:`CaptureApoReport`; inactive endpoints (unplugged mics, etc.)
    are skipped so the report tracks what the user is actually using.

    The function is pure (no observability side effects) so callers
    control when to log the detection event — startup code logs once;
    diagnostics endpoints may call it on every request.
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
        logger.debug("voice_apo_registry_open_failed", detail=str(exc))
        return []

    reports: list[CaptureApoReport] = []
    try:
        idx = 0
        while True:
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
    finally:
        winreg.CloseKey(root)
    return reports


def find_endpoint_report(
    reports: list[CaptureApoReport],
    *,
    device_name: str | None,
) -> CaptureApoReport | None:
    """Pick the report whose friendly name matches the active device.

    PortAudio device names and MMDevices friendly names usually align
    (both ultimately come from ``PKEY_Device_FriendlyName``) but MME
    truncates to 31 chars and WASAPI adds a suffix. We match by
    substring in *both directions* so the lookup is robust to either
    side being a prefix.
    """
    if not reports or not device_name:
        return None
    needle = device_name.strip().lower()
    if not needle:
        return None
    for rep in reports:
        hay = rep.endpoint_name.strip().lower()
        if not hay:
            continue
        if needle in hay or hay in needle:
            return rep
    return None


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
        enumerator = _read_property(wr, ep, "Properties", _PKEY_ENUMERATOR_NAME)
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
            lowered = text.lower()
            for needle, label in _PACKAGE_PATTERNS:
                if needle in lowered and label not in seen:
                    seen.add(label)
                    known.append(label)
            if _VOICE_CLARITY_LABEL in known:
                voice_clarity = True

        return CaptureApoReport(
            endpoint_id=endpoint_id,
            endpoint_name=str(friendly or ""),
            enumerator=str(enumerator or ""),
            fx_binding_count=len(fx_values),
            known_apos=known,
            raw_clsids=raw_clsids,
            voice_clarity_active=voice_clarity,
        )
    finally:
        wr.CloseKey(ep)  # type: ignore[attr-defined]


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
