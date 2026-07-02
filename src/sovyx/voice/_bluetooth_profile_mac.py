"""macOS Bluetooth profile detection (MA6).

Bluetooth headsets expose TWO mutually-exclusive audio profiles:

* **A2DP** (Advanced Audio Distribution Profile) — playback only,
  high-quality stereo audio. Codecs: SBC, AAC, aptX. NO microphone
  signal whatsoever — the headset's mic is OFF.
* **HFP/HSP** (Hands-Free / Headset Profile) — bidirectional,
  voice-grade mono audio. Codecs: SBC, mSBC. Mic is ON. Sample
  rate drops to 8 kHz (CVSD) or 16 kHz (mSBC) wideband.

When a user enables Sovyx voice with their AirPods / similar in
A2DP mode, the cascade sees a working playback path but ZERO mic
signal — failure mode is identical to a privacy block (capture is
healthy, all-zero frames). The fix is a profile flip to HFP, but
this can ONLY happen if the OS is asked (typically via the
``BluetoothAudioAgent`` framework, which Sovyx can't directly
control without pyobjc + entitlements).

Pre-MA6: cascade had no signal to attribute "AirPods are in A2DP
mode" specifically. Operators saw "mic captures silence" with no
hint to swap profile. MA6 ships the detection; its output is
surfaced via the boot diagnostic log line
``voice.macos.bluetooth_profile_detected`` AND the dashboard's
platform-diagnostics Bluetooth card, which renders an operator
remediation hint for A2DP-only devices (MACOS-1/MACOS-5).

Discovery method (MACOS-8):

1. **Primary:** ``system_profiler SPBluetoothDataType -json`` —
   locale/format-stable machine output with ``device_connected`` /
   ``device_not_connected`` arrays. The JSON schema does NOT expose
   the active A2DP/HFP profile, so connected devices parse to
   profile UNKNOWN with capability heuristics from
   ``device_minorType``; not-connected devices parse to
   NOT_CONNECTED. UNVERIFIED on real Mac hardware — the fixture
   shape follows the publicly documented schema (V-AEA backlog).
2. **Fallback:** the legacy indented-text output (``-detailLevel
   mini``), parsed in BOTH known shapes: the modern grouped tree
   (devices nested under ``Connected:`` / ``Not Connected:`` group
   headers) and the legacy flat tree (devices directly under
   ``Bluetooth:`` with ``Audio Source`` / ``Audio Sink`` attributes,
   from which the A2DP/HFP profile is inferred when present).

Each subprocess attempt has its own :data:`_SP_TIMEOUT_S` budget, so
the worst case (JSON unsupported → text fallback) is two attempts.

Reference: F1 inventory mission task MA6; Apple Bluetooth
Programming Guide (BluetoothAudioAgent); MACOS-8 of
MISSION-AUDIO-ENGINE-CROSS-PLATFORM-AUDIT-2026-07-02.
"""

from __future__ import annotations

import json
import shutil
import subprocess  # noqa: S404 — fixed-argv subprocess to trusted Apple binary
import sys
from dataclasses import dataclass, field
from enum import StrEnum

from sovyx.observability.logging import get_logger

logger = get_logger(__name__)


# ── Bounds ─────────────────────────────────────────────────────────


_SP_TIMEOUT_S = 8.0
"""Wall-clock budget for ONE ``system_profiler SPBluetoothDataType``
invocation. Cold-start of system_profiler is genuinely 2-5 s on first
call; 8 s is generous enough to absorb that without false-failing
while short enough that a wedged Bluetooth daemon doesn't stall the
voice pipeline preflight (worst case: JSON attempt + text fallback =
two budgets)."""


# ── Public types ──────────────────────────────────────────────────


class BluetoothAudioProfile(StrEnum):
    """Closed-set vocabulary of audio profiles for a connected
    Bluetooth device."""

    A2DP_ONLY = "a2dp_only"
    """Playback-only stereo (no mic). When this device is the OS
    default input, capture WILL produce zero frames."""

    HFP_ACTIVE = "hfp_active"
    """Hands-free / headset profile active (bidirectional, mic
    available). Voice should work."""

    UNKNOWN = "unknown"
    """Profile state could not be determined from system_profiler
    output (the ``-json`` schema doesn't expose it; text output only
    exposes it on legacy macOS). Cascade should treat as inconclusive
    — proceed but surface the note for forensic attribution."""

    NOT_CONNECTED = "not_connected"
    """Device is paired but not currently connected. Capture from
    this device is impossible until reconnected."""


@dataclass(frozen=True, slots=True)
class BluetoothDeviceReport:
    """One connected Bluetooth device with audio capability."""

    name: str
    """Human-readable device name (e.g. ``"AirPods Pro"``)."""

    address: str = ""
    """MAC address. Stable identifier across reconnects."""

    profile: BluetoothAudioProfile = BluetoothAudioProfile.UNKNOWN
    """Current audio profile state."""

    is_input_capable: bool = False
    """Heuristic: device declares itself as having a mic. False for
    speaker-only devices (HomePod, Bluetooth speakers)."""

    is_output_capable: bool = False
    """Device declares itself as having a speaker. False for
    button-only remotes / sensors."""


@dataclass(frozen=True, slots=True)
class BluetoothReport:
    """Aggregate Bluetooth audio report."""

    devices: tuple[BluetoothDeviceReport, ...] = field(default_factory=tuple)
    """All audio-capable Bluetooth devices observed (connected OR
    paired-but-disconnected)."""

    notes: tuple[str, ...] = field(default_factory=tuple)
    """Per-step diagnostic notes (subprocess timeout, parse errors,
    binary missing)."""

    @property
    def a2dp_only_devices(self) -> tuple[BluetoothDeviceReport, ...]:
        """Devices currently in A2DP-only mode AND input-capable.
        These are the "user's headset is in playback mode and they
        wonder why Sovyx can't hear them" candidates. Consumers: the
        boot diagnostic log line
        ``voice.macos.bluetooth_profile_detected`` in
        ``factory/_diagnostics.py`` (count + names); the dashboard
        renders its A2DP remediation hint client-side off the raw
        ``profile`` token (MACOS-1/MACOS-5)."""
        return tuple(
            d
            for d in self.devices
            if d.profile is BluetoothAudioProfile.A2DP_ONLY and d.is_input_capable
        )


# ── Probe ─────────────────────────────────────────────────────────


def detect_bluetooth_audio_profile() -> BluetoothReport:
    """Synchronous Bluetooth audio profile detection.

    Tries ``system_profiler SPBluetoothDataType -json`` first
    (format-stable); falls back to the legacy text output when the
    JSON attempt fails or is unparseable (MACOS-8).

    Returns:
        :class:`BluetoothReport` with per-device profile + notes.
        Never raises — non-darwin / subprocess / parse failures
        collapse into empty/notes.
    """
    if sys.platform != "darwin":
        return BluetoothReport(notes=(f"non-darwin platform: {sys.platform}",))

    sp_path = shutil.which("system_profiler")
    if sp_path is None:
        return BluetoothReport(notes=("system_profiler binary not found on PATH",))

    notes: list[str] = []

    json_stdout, json_notes = _run_system_profiler(sp_path, "-json")
    notes.extend(json_notes)
    if json_stdout is not None:
        parsed = _parse_bluetooth_json(json_stdout)
        if parsed is not None:
            devices, parse_notes = parsed
            return BluetoothReport(devices=tuple(devices), notes=(*notes, *parse_notes))
        notes.append("system_profiler -json output unparseable; falling back to text")

    text_stdout, text_notes = _run_system_profiler(sp_path, "-detailLevel", "mini")
    notes.extend(text_notes)
    if text_stdout is None:
        return BluetoothReport(notes=tuple(notes))

    devices, parse_notes = _parse_bluetooth_output(text_stdout)
    return BluetoothReport(devices=tuple(devices), notes=(*notes, *parse_notes))


def _run_system_profiler(
    sp_path: str,
    *extra_args: str,
) -> tuple[str | None, list[str]]:
    """Run one ``system_profiler SPBluetoothDataType`` invocation.

    Returns ``(stdout, notes)`` — stdout is ``None`` on any failure
    (timeout / spawn / nonzero exit), with the reason in notes."""
    argv = (sp_path, "SPBluetoothDataType", *extra_args)
    label = " ".join(extra_args)
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=_SP_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None, [f"system_profiler SPBluetoothDataType {label} timed out"]
    except OSError as exc:
        return None, [f"system_profiler spawn failed ({label}): {exc!r}"]

    if result.returncode != 0:
        return None, [
            f"system_profiler ({label}) exited {result.returncode}: {result.stderr.strip()[:120]}",
        ]
    return result.stdout, []


# ── JSON parsing (primary path) ───────────────────────────────────


_MINOR_TYPE_CAPABILITIES: dict[str, tuple[bool, bool]] = {
    # HEURISTIC ``device_minorType`` → (is_input_capable,
    # is_output_capable). The -json schema exposes no explicit audio
    # source/sink flags, so capability is inferred from the device
    # class. Conservative on input (AirPods report "Headphones" and DO
    # have mics, but we cannot verify — same conservative bias as the
    # legacy text path in A2DP mode). UNVERIFIED on real Mac HW.
    "headset": (True, True),
    "headphones": (False, True),
    "speaker": (False, True),
}


def _parse_bluetooth_json(
    stdout: str,
) -> tuple[list[BluetoothDeviceReport], list[str]] | None:
    """Parse ``system_profiler SPBluetoothDataType -json`` output.

    Expected shape (publicly documented; fixture faithful but
    UNVERIFIED on real Mac hardware — V-AEA backlog)::

        {"SPBluetoothDataType": [{
            "controller_properties": {...},
            "device_connected": [{"AirPods Pro": {
                "device_address": "AA:BB:CC:DD:EE:FF",
                "device_minorType": "Headphones", ...}}],
            "device_not_connected": [{"Keyboard": {...}}]
        }]}

    Profile semantics: the JSON schema does NOT expose A2DP/HFP
    state, so connected devices report UNKNOWN (with a note when any
    are present) and not-connected devices report NOT_CONNECTED
    without further parsing.

    Returns ``None`` when the payload isn't the expected shape (the
    caller then falls back to the text parser); ``(devices, notes)``
    otherwise."""
    try:
        payload = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    sections = payload.get("SPBluetoothDataType")
    if not isinstance(sections, list):
        return None

    devices: list[BluetoothDeviceReport] = []
    notes: list[str] = []
    connected_seen = False
    for section in sections:
        if not isinstance(section, dict):
            continue
        for entry in _iter_json_device_entries(section.get("device_connected")):
            connected_seen = True
            devices.append(_json_device_report(*entry, connected=True))
        for entry in _iter_json_device_entries(section.get("device_not_connected")):
            devices.append(_json_device_report(*entry, connected=False))

    if connected_seen:
        notes.append(
            "system_profiler -json does not expose A2DP/HFP profile state; "
            "connected devices report profile=unknown",
        )
    if not devices:
        notes.append("no audio-capable Bluetooth devices parsed from output")
    return devices, notes


def _iter_json_device_entries(
    raw: object,
) -> list[tuple[str, dict[str, object]]]:
    """Flatten a ``device_connected`` / ``device_not_connected`` array
    of single-key ``{name: attrs}`` dicts into ``(name, attrs)``
    pairs, skipping malformed entries."""
    entries: list[tuple[str, dict[str, object]]] = []
    if not isinstance(raw, list):
        return entries
    for item in raw:
        if not isinstance(item, dict):
            continue
        for name, attrs in item.items():
            if isinstance(name, str) and isinstance(attrs, dict):
                entries.append((name, attrs))
    return entries


def _json_device_report(
    name: str,
    attrs: dict[str, object],
    *,
    connected: bool,
) -> BluetoothDeviceReport:
    """Translate one -json device entry into a structured report."""
    address = str(attrs.get("device_address", "") or "")
    minor_type = str(attrs.get("device_minorType", "") or "").strip().lower()
    is_input, is_output = _MINOR_TYPE_CAPABILITIES.get(minor_type, (False, False))
    profile = BluetoothAudioProfile.UNKNOWN if connected else BluetoothAudioProfile.NOT_CONNECTED
    return BluetoothDeviceReport(
        name=name,
        address=address,
        profile=profile,
        is_input_capable=is_input,
        is_output_capable=is_output,
    )


# ── Text parsing (fallback path) ──────────────────────────────────


_GROUP_HEADER_CONNECTED = "connected"
_GROUP_HEADER_NOT_CONNECTED = "not connected"

_NON_DEVICE_SECTION_NAMES = frozenset({"bluetooth controller", "controller"})
"""Section headers that describe the host's Bluetooth controller, not
a paired device — must never be parsed as devices (MACOS-8)."""


def _parse_bluetooth_output(stdout: str) -> tuple[list[BluetoothDeviceReport], list[str]]:
    """Parse the indented key-value tree that the text form of
    ``system_profiler SPBluetoothDataType`` emits, in BOTH known
    shapes (MACOS-8):

    Legacy flat shape (devices directly under ``Bluetooth:``)::

        Bluetooth:
            AirPods Pro:
                Address: AA:BB:CC:DD:EE:FF
                Connected: Yes
                Audio Source: Yes
                Audio Sink: Yes

    Modern grouped shape (Monterey+; devices nested one indent level
    under ``Connected:`` / ``Not Connected:`` group headers, with a
    ``Bluetooth Controller:`` sibling section)::

        Bluetooth:
            Bluetooth Controller:
                Address: ...
                State: On
            Connected:
                AirPods Pro:
                    Address: AA:BB:CC:DD:EE:FF
                    Minor Type: Headphones
            Not Connected:
                Keyboard:
                    Address: ...

    Section headers are lines ending with ``:`` and carrying no
    inline value; attributes are deeper-indented ``Key: Value``
    pairs. Group headers set the connection state for the devices
    nested beneath them (``Not Connected`` devices short-circuit to
    NOT_CONNECTED; ``Connected`` devices are connected even without a
    ``Connected: Yes`` attribute). Controller sections are skipped.
    Both shapes UNVERIFIED against real Mac hardware output — fixture
    -shaped only (V-AEA backlog).

    Returns ``(devices, notes)``."""
    devices: list[BluetoothDeviceReport] = []
    notes: list[str] = []
    current: dict[str, str] | None = None
    current_name = ""
    current_group: str | None = None
    group_indent = -1
    device_indent = -1
    controller_indent = -1

    def _flush() -> None:
        nonlocal current, current_name
        if current is not None and current_name:
            devices.append(_dict_to_device_report(current_name, current, current_group))
        current = None
        current_name = ""

    for raw_line in stdout.splitlines():
        stripped_left = raw_line.rstrip()
        content = stripped_left.lstrip(" ")
        if not content:
            continue
        indent = len(stripped_left) - len(content)
        # Top-level "Bluetooth:" header — skip.
        if indent == 0:
            continue

        is_section_header = content.endswith(":") and ": " not in content
        if is_section_header:
            header_name = content.rstrip(":").strip()
            header_lower = header_name.lower()
            # Group headers (modern shape).
            if header_lower in (_GROUP_HEADER_CONNECTED, _GROUP_HEADER_NOT_CONNECTED):
                _flush()
                current_group = header_lower
                group_indent = indent
                device_indent = -1
                controller_indent = -1
                continue
            # Nested sub-section inside the controller block — skip.
            if controller_indent >= 0 and indent > controller_indent:
                continue
            # Controller section — never a device.
            if header_lower in _NON_DEVICE_SECTION_NAMES:
                _flush()
                controller_indent = indent
                if indent <= group_indent:
                    current_group = None
                continue
            # Nested sub-section inside the current device (e.g. a
            # "Services:" list) — keep the device open, don't parse
            # the sub-section header as a new device.
            if current is not None and device_indent >= 0 and indent > device_indent:
                continue
            # A header at or above the active group's indent ends it.
            if current_group is not None and indent <= group_indent:
                current_group = None
            # Device section header.
            _flush()
            controller_indent = -1
            current_name = header_name
            current = {}
            device_indent = indent
            continue

        # Attribute line: deeper-indented "Key: Value".
        if ": " in content:
            if controller_indent >= 0 or current is None:
                continue
            if indent <= device_indent:
                continue
            key, _, value = content.partition(": ")
            current[key.strip()] = value.strip()
            continue
        # Anything else is structural noise; ignore.
    _flush()

    if not devices:
        notes.append("no audio-capable Bluetooth devices parsed from output")
    return devices, notes


def _dict_to_device_report(
    name: str,
    attrs: dict[str, str],
    group: str | None = None,
) -> BluetoothDeviceReport:
    """Translate a parsed attribute dict into a structured report.

    ``group`` carries the modern-shape group header context: devices
    under ``Not Connected:`` short-circuit to NOT_CONNECTED; devices
    under ``Connected:`` are connected even when no ``Connected:``
    attribute is present (the modern shape encodes connection state
    in the grouping, not per-device attributes)."""
    address = attrs.get("Address", "")
    if group == _GROUP_HEADER_NOT_CONNECTED:
        connected = False
    elif group == _GROUP_HEADER_CONNECTED:
        connected = True
    else:
        connected = attrs.get("Connected", "").lower() in ("yes", "true")
    audio_source = attrs.get("Audio Source", "").lower() in ("yes", "true")
    audio_sink = attrs.get("Audio Sink", "").lower() in ("yes", "true")

    if not connected:
        profile = BluetoothAudioProfile.NOT_CONNECTED
    elif audio_source and audio_sink:
        # Both source and sink — full duplex audio = HFP mode active.
        # AirPods + Beats etc. report this when on a call / Sovyx-
        # like app has opened the input device.
        profile = BluetoothAudioProfile.HFP_ACTIVE
    elif audio_sink and not audio_source:
        # Sink only — playback only mode = A2DP. Most common state
        # when the headset is in "music playback" mode and Sovyx
        # tries to capture.
        profile = BluetoothAudioProfile.A2DP_ONLY
    else:
        profile = BluetoothAudioProfile.UNKNOWN

    return BluetoothDeviceReport(
        name=name,
        address=address,
        profile=profile,
        is_input_capable=audio_source,
        is_output_capable=audio_sink,
    )


__all__ = [
    "BluetoothAudioProfile",
    "BluetoothDeviceReport",
    "BluetoothReport",
    "detect_bluetooth_audio_profile",
]
