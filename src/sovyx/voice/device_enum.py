"""PortAudio device enumeration with host-API preference.

Three things that look obvious but aren't:

1. **MME truncates names to 31 chars.** On Windows, the same mic is
   exposed by up to 4 host APIs (MME, DirectSound, WASAPI, WDM-KS); each
   yields a distinct :func:`sounddevice.query_devices` entry. MME
   truncates the device name at the 31-char legacy ``MAXPNAMELEN``
   boundary, so the naive ``name not in seen`` dedup treats the MME
   entry (``"Microfone (Razer BlackShark V2 "``) as different from the
   WASAPI entry (``"Microfone (Razer BlackShark V2 Pro)"``) even though
   they point at the same hardware.

2. **MME is broken for non-native rates.** Modern USB gaming headsets
   expose 44.1 / 48 kHz natively. When Sovyx opens a 16 kHz capture
   stream on the MME variant, PortAudio's MME backend does not
   resample — it either errors or (worse) silently delivers zeros.
   VAD then never fires and the pipeline looks alive but is deaf. This
   is the root-cause bug behind the "mic does nothing" reports.

3. **PortAudio's default input index points at MME.** ``sd.default.device``
   on Windows returns the MME variant of the OS-default device, so any
   ``is_default=True`` flag honestly reflects "what PortAudio thinks is
   default", not "what actually works".

Fix: prefer WASAPI > DirectSound > WDM-KS > MME on Windows, CoreAudio
on macOS, ALSA > PulseAudio > JACK on Linux. Dedup by a normalised key
that collapses the 31-char MME truncation onto the full name.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sovyx.observability.logging import get_logger

logger = get_logger(__name__)

# Ordered host-API preference per platform. First match wins.
_HOST_API_PREFERENCE: tuple[str, ...] = (
    "Windows WASAPI",  # Win: native rate conversion, works w/ USB headsets
    "Core Audio",  # macOS
    "ALSA",  # Linux native
    "PulseAudio",  # Linux user-session
    "JACK Audio Connection Kit",  # Linux pro audio
    "Windows DirectSound",  # Win legacy fallback 1
    "Windows WDM-KS",  # Win kernel streaming (exclusive, skip by default)
    "MME",  # Win legacy last resort — breaks rate conversion
    "OSS",  # BSD
)


@dataclass(frozen=True)
class DeviceEntry:
    """A single audio device variant as exposed by PortAudio.

    Attributes:
        index: PortAudio device index (mutable across boots).
        name: Device name as returned by PortAudio (may be truncated
            on MME — always use :attr:`canonical_name` for matching).
        canonical_name: Normalised lower-cased prefix used to dedup
            across host APIs.
        host_api_index: PortAudio host-API index.
        host_api_name: Human-readable host-API name (``"Windows WASAPI"``,
            ``"MME"``, …).
        max_input_channels: Channel count for capture; 0 if not an input.
        max_output_channels: Channel count for playback; 0 if not an output.
        default_samplerate: Native rate the driver reports.
        is_os_default: Whether PortAudio marks this index as the OS default.
    """

    index: int
    name: str
    canonical_name: str
    host_api_index: int
    host_api_name: str
    max_input_channels: int
    max_output_channels: int
    default_samplerate: int
    is_os_default: bool


def _canonicalise(name: str) -> str:
    """Collapse MME's 31-char truncation onto the full name.

    MME pads names to ``MAXPNAMELEN=31`` and trailing whitespace is
    emitted as-is. Strip + lower + keep only the first 30 chars (one
    below the boundary to be safe) so the WASAPI ``"Microfone (Razer
    BlackShark V2 Pro)"`` and the MME ``"Microfone (Razer BlackShark V2
    "`` hash to the same key.
    """
    return name.strip().lower()[:30]


def _host_api_rank(name: str) -> int:
    """Sort key: lower number = more preferred."""
    try:
        return _HOST_API_PREFERENCE.index(name)
    except ValueError:
        return len(_HOST_API_PREFERENCE)


def enumerate_devices() -> list[DeviceEntry]:
    """Return every PortAudio device entry enriched with host-API info.

    Returns an empty list if ``sounddevice`` is not importable or the
    host has no audio stack available (CI containers, SSH-only hosts).
    """
    try:
        import sounddevice as sd  # noqa: PLC0415
    except (ImportError, OSError):
        logger.debug("sounddevice_unavailable")
        return []

    try:
        raw_devices: Any = sd.query_devices()
        raw_host_apis: Any = sd.query_hostapis()
        default_in, default_out = sd.default.device
    except Exception:  # noqa: BLE001
        logger.warning("device_enumeration_failed", exc_info=True)
        return []

    host_api_names: dict[int, str] = {
        i: str(a.get("name", "unknown") if isinstance(a, dict) else "unknown")
        for i, a in enumerate(raw_host_apis)
    }

    out: list[DeviceEntry] = []
    for i, d in enumerate(raw_devices):
        if not isinstance(d, dict):
            continue
        name = str(d.get("name", "unknown"))
        host_idx = int(d.get("hostapi", -1) or 0)
        host_name = host_api_names.get(host_idx, "unknown")
        in_ch = int(d.get("max_input_channels", 0) or 0)
        out_ch = int(d.get("max_output_channels", 0) or 0)
        sr = int(d.get("default_samplerate", 0) or 0)
        is_os_default = i in (default_in, default_out)
        out.append(
            DeviceEntry(
                index=i,
                name=name,
                canonical_name=_canonicalise(name),
                host_api_index=host_idx,
                host_api_name=host_name,
                max_input_channels=in_ch,
                max_output_channels=out_ch,
                default_samplerate=sr,
                is_os_default=is_os_default,
            )
        )
    _emit_enumeration(out, default_in=default_in, default_out=default_out)
    return out


def _emit_enumeration(
    entries: list[DeviceEntry],
    *,
    default_in: int,
    default_out: int,
) -> None:
    """Emit ``audio.device.enumerated`` only when the device topology changes.

    The full device list is included so the dashboard can render the
    enumeration without a follow-up RPC, but emitting on every call
    would spam during the boot cascade (each stream-open re-enumerates).
    A SHA-256 fingerprint of the (name, host_api, channels, default_rate)
    tuples is cached at module scope; the event fires only when the
    fingerprint changes — which is precisely when an operator wants to
    see it (cold boot, hot-plug, USB renumeration). The fingerprint
    survives across :func:`enumerate_devices` invocations within the
    same process.
    """
    import hashlib

    signature_items = sorted(
        (
            e.name,
            e.host_api_name,
            e.max_input_channels,
            e.max_output_channels,
            e.default_samplerate,
        )
        for e in entries
    )
    signature = hashlib.sha256(repr(signature_items).encode("utf-8")).hexdigest()[:16]
    global _last_enumeration_signature  # noqa: PLW0603
    if signature == _last_enumeration_signature:
        return
    _last_enumeration_signature = signature

    devices = [
        {
            "index": e.index,
            "name": e.name,
            "host_api": e.host_api_name,
            "max_input_channels": e.max_input_channels,
            "max_output_channels": e.max_output_channels,
            "default_samplerate": e.default_samplerate,
            "is_default_input": e.index == default_in and e.max_input_channels > 0,
            "is_default_output": e.index == default_out and e.max_output_channels > 0,
        }
        for e in entries
    ]
    logger.info(
        "audio.device.enumerated",
        **{
            "voice.device_count": len(entries),
            "voice.input_count": sum(1 for e in entries if e.max_input_channels > 0),
            "voice.output_count": sum(1 for e in entries if e.max_output_channels > 0),
            "voice.default_input_index": default_in,
            "voice.default_output_index": default_out,
            "voice.signature": signature,
            "voice.devices": devices,
        },
    )


_last_enumeration_signature: str | None = None


def pick_preferred(entries: list[DeviceEntry], *, kind: str) -> list[DeviceEntry]:
    """Dedup by :attr:`DeviceEntry.canonical_name` keeping the best host API.

    Args:
        entries: Output of :func:`enumerate_devices`.
        kind: Either ``"input"`` or ``"output"`` — filters channels and
            picks the correct OS default to mark.

    Returns:
        One entry per logical device, host-API-preferred. On Windows a
        Razer mic exposed through MME (index 1), DirectSound (index 8)
        and WASAPI (index 18) collapses to the WASAPI entry. The
        ``is_os_default`` flag is forwarded from **any** variant — so a
        device is marked default as long as one of its host-API variants
        was the OS default, which is what users expect.
    """
    if kind not in {"input", "output"}:
        msg = f"kind must be 'input' or 'output', not {kind!r}"
        raise ValueError(msg)

    groups: dict[str, list[DeviceEntry]] = {}
    for e in entries:
        if kind == "input" and e.max_input_channels <= 0:
            continue
        if kind == "output" and e.max_output_channels <= 0:
            continue
        groups.setdefault(e.canonical_name, []).append(e)

    preferred: list[DeviceEntry] = []
    for variants in groups.values():
        variants.sort(key=lambda v: (_host_api_rank(v.host_api_name), v.index))
        chosen = variants[0]
        any_default = any(v.is_os_default for v in variants)
        if any_default and not chosen.is_os_default:
            chosen = DeviceEntry(
                index=chosen.index,
                name=chosen.name,
                canonical_name=chosen.canonical_name,
                host_api_index=chosen.host_api_index,
                host_api_name=chosen.host_api_name,
                max_input_channels=chosen.max_input_channels,
                max_output_channels=chosen.max_output_channels,
                default_samplerate=chosen.default_samplerate,
                is_os_default=True,
            )
        preferred.append(chosen)

    # Stable ordering: OS-default first, then by preferred host API, then by name.
    preferred.sort(
        key=lambda e: (
            not e.is_os_default,
            _host_api_rank(e.host_api_name),
            e.canonical_name,
        )
    )
    return preferred


def resolve_device(
    *,
    requested_index: int | str | None,
    requested_name: str | None,
    requested_host_api: str | None,
    kind: str,
) -> DeviceEntry | None:
    """Resolve a persisted device spec to a current PortAudio entry.

    Resolution order (most specific → least):

    1. ``(requested_name, requested_host_api)`` exact match.
    2. ``requested_name`` matched, host API via global preference order.
    3. Legacy ``requested_index`` pointing at a live entry.
    4. OS default (as picked by :func:`pick_preferred`).

    Returns ``None`` only if the host has no audio devices of ``kind``.

    Args:
        requested_index: Index persisted by an older mind.yaml.
        requested_name: Stable device name (preferred persistence key).
        requested_host_api: Host-API name (``"Windows WASAPI"`` …).
        kind: ``"input"`` or ``"output"``.
    """
    entries = enumerate_devices()
    if not entries:
        return None

    candidates = [
        e
        for e in entries
        if (kind == "input" and e.max_input_channels > 0)
        or (kind == "output" and e.max_output_channels > 0)
    ]
    if not candidates:
        return None

    if requested_name:
        canonical = _canonicalise(requested_name)
        name_matches = [e for e in candidates if e.canonical_name == canonical]
        if name_matches and requested_host_api:
            exact = [e for e in name_matches if e.host_api_name == requested_host_api]
            if exact:
                return exact[0]
        if name_matches:
            name_matches.sort(key=lambda v: (_host_api_rank(v.host_api_name), v.index))
            return name_matches[0]

    if isinstance(requested_index, int) and 0 <= requested_index < len(entries):
        entry = entries[requested_index]
        if (kind == "input" and entry.max_input_channels > 0) or (
            kind == "output" and entry.max_output_channels > 0
        ):
            # Legacy path: the saved index is still valid. Upgrade it to
            # the preferred host-API variant of the same device before
            # returning so the user doesn't stay on MME forever.
            canonical = entry.canonical_name
            same_device = [e for e in candidates if e.canonical_name == canonical]
            same_device.sort(key=lambda v: (_host_api_rank(v.host_api_name), v.index))
            if same_device:
                return same_device[0]
            return entry

    preferred = pick_preferred(entries, kind=kind)
    defaults = [e for e in preferred if e.is_os_default]
    if defaults:
        return defaults[0]
    return preferred[0] if preferred else None
