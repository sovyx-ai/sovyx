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
hint to swap profile. MA6 ships the detection so the dashboard can
surface a precise remediation: "Your headset is in A2DP playback
mode; switch to a hands-free / call mode in System Settings →
Bluetooth → [device] → Mode".

Discovery method: ``system_profiler SPBluetoothDataType`` — slow
(~2-5 s) but stable across macOS releases since Big Sur. Output
parses to identify connected devices + their audio sink/source
state.

Reference: F1 inventory mission task MA6; Apple Bluetooth
Programming Guide (BluetoothAudioAgent).
"""

from __future__ import annotations

import shutil
import subprocess  # noqa: S404 — fixed-argv subprocess to trusted Apple binary
import sys
from dataclasses import dataclass, field
from enum import StrEnum

from sovyx.observability.logging import get_logger

logger = get_logger(__name__)


# ── Bounds ─────────────────────────────────────────────────────────


_SP_TIMEOUT_S = 8.0
"""Wall-clock budget for ``system_profiler SPBluetoothDataType``.
Cold-start of system_profiler is genuinely 2-5 s on first call;
8 s is generous enough to absorb that without false-failing while
short enough that a wedged Bluetooth daemon doesn't stall the
voice pipeline preflight."""


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
    """Profile state could not be parsed from system_profiler output.
    Cascade should treat as inconclusive — proceed but surface the
    note for forensic attribution."""

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
        wonder why Sovyx can't hear them" candidates — load-bearing
        for the dashboard's specific remediation hint."""
        return tuple(
            d
            for d in self.devices
            if d.profile is BluetoothAudioProfile.A2DP_ONLY and d.is_input_capable
        )


# ── Probe ─────────────────────────────────────────────────────────


def detect_bluetooth_audio_profile() -> BluetoothReport:
    """Synchronous Bluetooth audio profile detection.

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

    try:
        result = subprocess.run(
            (sp_path, "SPBluetoothDataType", "-detailLevel", "mini"),
            capture_output=True,
            text=True,
            timeout=_SP_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return BluetoothReport(notes=("system_profiler SPBluetoothDataType timed out",))
    except OSError as exc:
        return BluetoothReport(notes=(f"system_profiler spawn failed: {exc!r}",))

    if result.returncode != 0:
        return BluetoothReport(
            notes=(f"system_profiler exited {result.returncode}: {result.stderr.strip()[:120]}",),
        )

    devices, parse_notes = _parse_bluetooth_output(result.stdout)
    return BluetoothReport(devices=tuple(devices), notes=tuple(parse_notes))


def _parse_bluetooth_output(stdout: str) -> tuple[list[BluetoothDeviceReport], list[str]]:
    """Parse the indented key-value tree that ``system_profiler -
    detailLevel mini`` emits.

    Format example::

        Bluetooth:
          AirPods Pro:
              Address: AA:BB:CC:DD:EE:FF
              Connected: Yes
              Services: 0x0000001
              Audio Source: Yes
              Audio Sink: Yes
              Vendor ID: 0x004C
        ...

    Devices appear as 4-space-indented sections; their attributes
    are 6-space-indented ``Key: Value`` pairs. We track the current
    device by indent depth + first-line-of-section detection.

    Returns ``(devices, notes)``."""
    devices: list[BluetoothDeviceReport] = []
    notes: list[str] = []
    current: dict[str, str] | None = None
    current_name = ""

    def _flush() -> None:
        nonlocal current, current_name
        if current is not None and current_name:
            devices.append(_dict_to_device_report(current_name, current))
        current = None
        current_name = ""

    for raw_line in stdout.splitlines():
        # Skip blank lines.
        if not raw_line.strip():
            continue
        # Detect device-section header: 4-space indent + ends with ":"
        # AND the next character isn't another space (vs. attribute
        # which is 6+ space indent).
        stripped_left = raw_line.rstrip()
        # Count leading spaces.
        indent = len(stripped_left) - len(stripped_left.lstrip(" "))
        content = stripped_left.lstrip(" ")
        if not content:
            continue
        # Top-level "Bluetooth:" header — skip.
        if indent == 0:
            continue
        # Device section header: indent == 4 AND ends with ":" AND no
        # value after the colon (purely a section start).
        if indent == 4 and content.endswith(":") and ": " not in content:  # noqa: PLR2004
            _flush()
            current_name = content.rstrip(":").strip()
            current = {}
            continue
        # Attribute line: indent > 4 AND contains "Key: Value".
        if indent > 4 and ": " in content and current is not None:  # noqa: PLR2004
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
) -> BluetoothDeviceReport:
    """Translate a parsed attribute dict into a structured report."""
    address = attrs.get("Address", "")
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
