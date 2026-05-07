"""Hardware + audio-stack fingerprint extraction for calibration.

Captures a :class:`HardwareFingerprint` describing the running host's
identity at a level the calibration rules consume (distro + kernel +
audio stack + codec_id + driver_family + system vendor/product +
capture topology + interceptor presence).

The existing :mod:`sovyx.voice.health._fingerprint_linux` covers
**per-endpoint** identity (which mic is which); this module covers
**whole-system** identity (which laptop is this, which audio stack is
running). The two are complementary -- the per-endpoint fingerprint
identifies devices, the calibration fingerprint identifies hosts.

Each ``_read_*`` helper is a pure function with bounded subprocess
timeouts. On non-Linux hosts, Linux-specific fields fall back to
sentinel values (``""``, ``0``, ``None``, ``()``); ``apo_active``/
``apo_name`` and ``hal_interceptors`` ship empty by design.
Non-Linux platforms are gated out of the calibration wizard at the
dashboard layer (``platform_supported=False`` returned by
:func:`sovyx.dashboard.routes.voice_calibration.get_calibration_feature_flag`),
and the CLI ``--full-diag``/``--calibrate`` paths raise
``DiagPrerequisiteError`` on non-Linux hosts. The unfilled
non-Linux fields therefore never reach the rule engine; they exist
only to keep the schema stable for forensic profiles imported
across platforms.

History: introduced in v0.30.15 as T2.2 of mission
``MISSION-voice-self-calibrating-system-2026-05-05.md`` Layer 2.
"""

from __future__ import annotations

import contextlib
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from sovyx.observability.logging import get_logger
from sovyx.voice.calibration.schema import (
    HARDWARE_FINGERPRINT_SCHEMA_VERSION,
    HardwareFingerprint,
)

logger = get_logger(__name__)

_SUBPROCESS_TIMEOUT_S = 5.0
_OS_RELEASE = Path("/etc/os-release")
_PROC_CPUINFO = Path("/proc/cpuinfo")
_PROC_MEMINFO = Path("/proc/meminfo")
_PROC_ASOUND_VERSION = Path("/proc/asound/version")
_PROC_ASOUND_CARDS = Path("/proc/asound/cards")
_DMI_SYS_VENDOR = Path("/sys/class/dmi/id/sys_vendor")
_DMI_PRODUCT_NAME = Path("/sys/class/dmi/id/product_name")

_DESTRUCTIVE_PULSE_MODULE_PATTERNS = (
    "module-echo-cancel",
    "module-rnnoise",
    "module-webrtc-audio-processing",
    "module-noise-suppression",
)


def capture_fingerprint(*, captured_at_utc: str | None = None) -> HardwareFingerprint:
    """Capture a HardwareFingerprint for the running host.

    Best-effort: each field has its own try/except + fallback so a
    missing tool (e.g. no ``pactl`` on a PipeWire-only system) never
    fails the whole capture. Operators on Linux get the rich field
    set; macOS + Windows get the system-wide subset (distro_id stays
    empty, kernel_release maps to ``platform.release()``, etc.).

    Args:
        captured_at_utc: Override the capture timestamp (testability).
            Defaults to ``datetime.now(tz=UTC).isoformat()``.

    Returns:
        A frozen :class:`HardwareFingerprint`.
    """
    audio_stack = _detect_audio_stack()
    pipewire_version = _detect_pipewire_version() if audio_stack == "pipewire" else None
    pulseaudio_version = _detect_pulseaudio_version() if audio_stack == "pulseaudio" else None
    kernel_release = _read_kernel_release()
    return HardwareFingerprint(
        schema_version=HARDWARE_FINGERPRINT_SCHEMA_VERSION,
        captured_at_utc=(
            captured_at_utc
            if captured_at_utc is not None
            else datetime.now(tz=UTC).isoformat(timespec="seconds")
        ),
        distro_id=_read_distro_id(),
        distro_id_like=_read_distro_id_like(),
        kernel_release=kernel_release,
        kernel_major_minor=_compute_kernel_major_minor(kernel_release),
        cpu_model=_read_cpu_model(),
        cpu_cores=_read_cpu_cores(),
        ram_mb=_read_ram_mb(),
        has_gpu=_detect_has_gpu(),
        gpu_vram_mb=_detect_gpu_vram_mb(),
        audio_stack=audio_stack,
        pipewire_version=pipewire_version,
        pulseaudio_version=pulseaudio_version,
        alsa_lib_version=_read_alsa_lib_version(),
        codec_id=_read_codec_id(),
        driver_family=_read_driver_family(),
        system_vendor=_read_system_vendor(),
        system_product=_read_system_product(),
        capture_card_count=_count_capture_cards(),
        capture_devices=_enumerate_capture_devices(),
        # Win/macOS-specific interceptor fields ship empty by design.
        # Non-Linux platforms are gated out of the calibration wizard
        # at the dashboard layer (platform_supported=False) and at the
        # CLI layer (DiagPrerequisiteError); these fields exist only
        # to keep the schema stable across cross-platform forensic
        # imports.
        apo_active=False,
        apo_name=None,
        hal_interceptors=(),
        pulse_modules_destructive=_detect_destructive_pulse_modules(),
    )


# ====================================================================
# /etc/os-release (distro identity)
# ====================================================================


def _parse_os_release(content: str) -> dict[str, str]:
    """Parse the freedesktop os-release format into a dict.

    Format reference: https://www.freedesktop.org/software/systemd/man/os-release.html
    """
    parsed: dict[str, str] = {}
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        # Strip surrounding double-quotes (the spec permits them).
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        parsed[key.strip()] = value.strip()
    return parsed


def _read_distro_id() -> str:
    if not _OS_RELEASE.is_file():
        return ""
    try:
        content = _OS_RELEASE.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return _parse_os_release(content).get("ID", "")


def _read_distro_id_like() -> str:
    if not _OS_RELEASE.is_file():
        return ""
    try:
        content = _OS_RELEASE.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return _parse_os_release(content).get("ID_LIKE", "")


# ====================================================================
# Kernel
# ====================================================================


def _read_kernel_release() -> str:
    """Run ``uname -r``; fall back to :func:`platform.release` on failure."""
    if sys.platform == "linux":
        result = _safe_subprocess(["uname", "-r"])
        if result:
            return result
    # Cross-platform fallback.
    import platform

    return platform.release()


def _compute_kernel_major_minor(release: str) -> str:
    """Extract ``MAJOR.MINOR`` from a kernel release string.

    Example: ``"6.8.0-50-generic"`` -> ``"6.8"``.
    Returns ``""`` on parse failure.
    """
    match = re.match(r"^(\d+)\.(\d+)", release)
    if match is None:
        return ""
    return f"{match.group(1)}.{match.group(2)}"


# ====================================================================
# CPU
# ====================================================================


def _read_cpu_model() -> str:
    if _PROC_CPUINFO.is_file():
        try:
            content = _PROC_CPUINFO.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        for line in content.splitlines():
            if line.startswith("model name"):
                _, _, value = line.partition(":")
                return value.strip()
    # Cross-platform fallback.
    import platform

    return platform.processor() or ""


def _read_cpu_cores() -> int:
    """Logical CPU core count via :func:`os.cpu_count`."""
    import os

    count = os.cpu_count()
    return count if count is not None else 0


# ====================================================================
# RAM
# ====================================================================


def _read_ram_mb() -> int:
    if not _PROC_MEMINFO.is_file():
        return 0
    try:
        content = _PROC_MEMINFO.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    for line in content.splitlines():
        if line.startswith("MemTotal:"):
            # "MemTotal:       16345628 kB"
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                return int(parts[1]) // 1024  # kB -> MB
    return 0


# ====================================================================
# GPU
# ====================================================================


def _detect_has_gpu() -> bool:
    """Quick GPU presence check: ``nvidia-smi`` exists + can list devices."""
    if shutil.which("nvidia-smi") is None:
        return False
    result = _safe_subprocess(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"])
    return bool(result)


def _detect_gpu_vram_mb() -> int:
    """First GPU's VRAM in MB via ``nvidia-smi --query-gpu=memory.total``."""
    if shutil.which("nvidia-smi") is None:
        return 0
    result = _safe_subprocess(
        ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"]
    )
    if not result:
        return 0
    first_line = result.splitlines()[0].strip()
    return int(first_line) if first_line.isdigit() else 0


# ====================================================================
# Audio stack
# ====================================================================


def _detect_audio_stack() -> str:
    """Return one of ``pipewire`` | ``pulseaudio`` | ``alsa-only`` | ``"" `` (non-Linux)."""
    if sys.platform != "linux":
        return ""
    # PipeWire detection: pw-cli ``info 0`` returns server info; pactl
    # also reports PipeWire as the server name when present. We use
    # pactl for unified detection because PipeWire ships a pulse
    # compatibility shim that reports itself.
    if shutil.which("pactl") is not None:
        info = _safe_subprocess(["pactl", "info"])
        if info:
            for line in info.splitlines():
                if line.startswith("Server Name:"):
                    if "PipeWire" in line:
                        return "pipewire"
                    if "pulseaudio" in line.lower():
                        return "pulseaudio"
    # Fall back to ALSA-only if /proc/asound exists.
    if Path("/proc/asound").is_dir():
        return "alsa-only"
    return ""


def _detect_pipewire_version() -> str | None:
    if shutil.which("pw-cli") is None:
        return None
    info = _safe_subprocess(["pw-cli", "info", "0"])
    if not info:
        return None
    for line in info.splitlines():
        line = line.strip()
        if line.startswith("core.version"):
            # ``core.version = "1.0.5"``
            match = re.search(r'"([^"]+)"', line)
            if match is not None:
                return match.group(1)
    return None


def _detect_pulseaudio_version() -> str | None:
    if shutil.which("pactl") is None:
        return None
    info = _safe_subprocess(["pactl", "info"])
    if not info:
        return None
    for line in info.splitlines():
        if line.startswith("Server Version:"):
            _, _, value = line.partition(":")
            return value.strip()
    return None


def _read_alsa_lib_version() -> str:
    if not _PROC_ASOUND_VERSION.is_file():
        return ""
    try:
        content = _PROC_ASOUND_VERSION.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    # Format: "Advanced Linux Sound Architecture Driver Version k6.8.0-50-generic."
    # The "v1.x.x" library version isn't in /proc/asound/version directly;
    # we surface what's there for now (mostly just kernel-bundled marker).
    return content.strip()


# ====================================================================
# Audio hardware (codec, driver family, capture topology)
# ====================================================================


def _read_proc_asound_cards_lines() -> list[str]:
    if not _PROC_ASOUND_CARDS.is_file():
        return []
    try:
        content = _PROC_ASOUND_CARDS.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return content.splitlines()


def _read_codec_id() -> str:
    """Extract first PCI codec id (e.g. ``"10ec:0257"``) from /proc/asound/cards.

    /proc/asound/cards format::
        0 [HDA Intel PCH]: HDA-Intel - HDA Intel PCH
                          HDA Intel PCH at 0xfd340000 irq 154
    The codec id isn't directly there. We use lspci -nn to map the
    HDA controller to its PCI vendor:device id.
    """
    if shutil.which("lspci") is None:
        return ""
    output = _safe_subprocess(["lspci", "-nn"])
    if not output:
        return ""
    for line in output.splitlines():
        # Match "Audio device", "Multimedia audio controller", etc.
        if "udio" in line:  # matches "Audio" and "audio"
            match = re.search(r"\[([0-9a-fA-F]{4}):([0-9a-fA-F]{4})\]", line)
            if match is not None:
                return f"{match.group(1).lower()}:{match.group(2).lower()}"
    return ""


def _read_driver_family() -> str:
    """Return one of ``hda`` | ``sof`` | ``usb-audio`` | ``bt`` | ``""``."""
    lines = _read_proc_asound_cards_lines()
    for raw_line in lines:
        # Card lines look like: " 0 [HDA Intel PCH]: HDA-Intel - HDA Intel PCH"
        # Type is the second [...] section.
        match = re.search(r"\[(.*?)\]:\s*(\S+)", raw_line)
        if match is not None:
            type_token = match.group(2).lower()
            if "hda" in type_token:
                return "hda"
            if "sof" in type_token:
                return "sof"
            if "usb" in type_token:
                return "usb-audio"
            if "bt" in type_token or "bluetooth" in type_token:
                return "bt"
    return ""


def _count_capture_cards() -> int:
    """Count distinct cards listed in /proc/asound/cards."""
    lines = _read_proc_asound_cards_lines()
    # Card list lines start with " <N> [" -- count those.
    count = 0
    for raw_line in lines:
        if re.match(r"^\s*\d+\s+\[", raw_line) is not None:
            count += 1
    return count


def _enumerate_capture_devices() -> tuple[str, ...]:
    """Return sorted tuple of capture device names from /proc/asound/cards."""
    lines = _read_proc_asound_cards_lines()
    devices: list[str] = []
    for raw_line in lines:
        match = re.match(r"^\s*\d+\s+\[(.*?)\]:", raw_line)
        if match is not None:
            devices.append(match.group(1).strip())
    return tuple(sorted(devices))


# ====================================================================
# DMI (system vendor / product)
# ====================================================================


def _read_dmi_field(path: Path) -> str:
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _read_system_vendor() -> str:
    return _read_dmi_field(_DMI_SYS_VENDOR)


def _read_system_product() -> str:
    return _read_dmi_field(_DMI_PRODUCT_NAME)


# ====================================================================
# Destructive PulseAudio modules
# ====================================================================


def _detect_destructive_pulse_modules() -> tuple[str, ...]:
    """Return sorted tuple of loaded PA modules matching destructive patterns."""
    if shutil.which("pactl") is None:
        return ()
    output = _safe_subprocess(["pactl", "list", "modules", "short"])
    if not output:
        return ()
    matched: set[str] = set()
    for line in output.splitlines():
        for pattern in _DESTRUCTIVE_PULSE_MODULE_PATTERNS:
            if pattern in line:
                matched.add(pattern)
    return tuple(sorted(matched))


# ====================================================================
# Subprocess helper
# ====================================================================


def _safe_subprocess(cmd: list[str]) -> str:
    """Run ``cmd`` with a bounded timeout; return stripped stdout or ``""``.

    Never raises -- subprocess errors / timeouts return empty string and
    log at DEBUG so the calling fingerprint helper falls through to its
    own sentinel. This is the contract that makes the whole capture
    best-effort: a missing tool, a hung command, or a permission denial
    on one field never blocks the rest of the fingerprint.
    """
    try:
        completed = subprocess.run(  # noqa: S603 -- command is hardcoded by callers
            cmd,
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        with contextlib.suppress(Exception):
            logger.debug(
                "voice.calibration.fingerprint.subprocess_failed",
                cmd=cmd[0] if cmd else "",
                reason=type(exc).__name__,
            )
        return ""
    if completed.returncode != 0:
        return ""
    return completed.stdout.strip()
