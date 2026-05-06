"""Unit tests for sovyx.voice.calibration._fingerprint.capture_fingerprint.

The fingerprint extractor is best-effort: each helper has its own
try/except + sentinel fallback, so a missing tool / file / permission
denial on one field never blocks the rest of the capture.

Tests use ``patch.object`` to inject controlled responses into the
helper functions instead of running real subprocess calls -- this
keeps the suite fast + deterministic across Linux / macOS / Windows
test environments.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from sovyx.voice.calibration import HardwareFingerprint, capture_fingerprint
from sovyx.voice.calibration import _fingerprint as fp

# ====================================================================
# capture_fingerprint top-level
# ====================================================================


class TestCaptureFingerprint:
    """capture_fingerprint composes the per-field helpers into a frozen profile."""

    def test_returns_hardware_fingerprint(self) -> None:
        # Mock every helper so the test runs deterministically.
        with (
            patch.object(fp, "_detect_audio_stack", return_value="pipewire"),
            patch.object(fp, "_detect_pipewire_version", return_value="1.0.5"),
            patch.object(fp, "_read_kernel_release", return_value="6.8.0-50-generic"),
            patch.object(fp, "_read_distro_id", return_value="linuxmint"),
            patch.object(fp, "_read_distro_id_like", return_value="debian"),
            patch.object(fp, "_read_cpu_model", return_value="Intel Core i5-1240P"),
            patch.object(fp, "_read_cpu_cores", return_value=12),
            patch.object(fp, "_read_ram_mb", return_value=16384),
            patch.object(fp, "_detect_has_gpu", return_value=False),
            patch.object(fp, "_detect_gpu_vram_mb", return_value=0),
            patch.object(fp, "_read_alsa_lib_version", return_value="ALSA k6.8"),
            patch.object(fp, "_read_codec_id", return_value="10ec:0257"),
            patch.object(fp, "_read_driver_family", return_value="hda"),
            patch.object(fp, "_read_system_vendor", return_value="Sony"),
            patch.object(fp, "_read_system_product", return_value="VAIO"),
            patch.object(fp, "_count_capture_cards", return_value=1),
            patch.object(fp, "_enumerate_capture_devices", return_value=("Internal Mic",)),
            patch.object(fp, "_detect_destructive_pulse_modules", return_value=()),
        ):
            result = capture_fingerprint(captured_at_utc="2026-05-05T18:00:00Z")

        assert isinstance(result, HardwareFingerprint)
        assert result.audio_stack == "pipewire"
        assert result.pipewire_version == "1.0.5"
        assert result.pulseaudio_version is None  # not pulseaudio stack
        assert result.kernel_release == "6.8.0-50-generic"
        assert result.kernel_major_minor == "6.8"  # computed from release
        assert result.system_vendor == "Sony"
        assert result.system_product == "VAIO"
        assert result.capture_devices == ("Internal Mic",)
        # Win/macOS-specific fields ship empty in v0.30.15.
        assert result.apo_active is False
        assert result.apo_name is None
        assert result.hal_interceptors == ()

    def test_pulseaudio_stack_only_populates_pa_version(self) -> None:
        # _all_safe_helpers_patched() enters FIRST so its baseline
        # patches are applied; the explicit overrides come AFTER and
        # win (later patch.object on the same attribute supersedes).
        with (
            _all_safe_helpers_patched(),
            patch.object(fp, "_detect_audio_stack", return_value="pulseaudio"),
            patch.object(fp, "_detect_pulseaudio_version", return_value="16.1"),
        ):
            result = capture_fingerprint(captured_at_utc="2026-05-05T18:00:00Z")
        assert result.pipewire_version is None
        assert result.pulseaudio_version == "16.1"

    def test_alsa_only_stack_populates_neither_version(self) -> None:
        with (
            _all_safe_helpers_patched(),
            patch.object(fp, "_detect_audio_stack", return_value="alsa-only"),
        ):
            result = capture_fingerprint(captured_at_utc="2026-05-05T18:00:00Z")
        assert result.pipewire_version is None
        assert result.pulseaudio_version is None
        assert result.audio_stack == "alsa-only"

    def test_default_captured_at_utc_is_iso_8601(self) -> None:
        with (
            _all_safe_helpers_patched(),
            patch.object(fp, "_detect_audio_stack", return_value=""),
        ):
            result = capture_fingerprint()
        # Format: YYYY-MM-DDTHH:MM:SS or with microseconds. Sanity check.
        assert "T" in result.captured_at_utc
        assert len(result.captured_at_utc) >= 19


def _all_safe_helpers_patched():  # type: ignore[no-untyped-def]
    """Context manager patching every fingerprint helper with safe defaults."""
    from contextlib import ExitStack

    stack = ExitStack()
    stack.enter_context(patch.object(fp, "_read_kernel_release", return_value="0.0.0"))
    stack.enter_context(patch.object(fp, "_read_distro_id", return_value=""))
    stack.enter_context(patch.object(fp, "_read_distro_id_like", return_value=""))
    stack.enter_context(patch.object(fp, "_read_cpu_model", return_value=""))
    stack.enter_context(patch.object(fp, "_read_cpu_cores", return_value=0))
    stack.enter_context(patch.object(fp, "_read_ram_mb", return_value=0))
    stack.enter_context(patch.object(fp, "_detect_has_gpu", return_value=False))
    stack.enter_context(patch.object(fp, "_detect_gpu_vram_mb", return_value=0))
    stack.enter_context(patch.object(fp, "_detect_pipewire_version", return_value=None))
    stack.enter_context(patch.object(fp, "_detect_pulseaudio_version", return_value=None))
    stack.enter_context(patch.object(fp, "_read_alsa_lib_version", return_value=""))
    stack.enter_context(patch.object(fp, "_read_codec_id", return_value=""))
    stack.enter_context(patch.object(fp, "_read_driver_family", return_value=""))
    stack.enter_context(patch.object(fp, "_read_system_vendor", return_value=""))
    stack.enter_context(patch.object(fp, "_read_system_product", return_value=""))
    stack.enter_context(patch.object(fp, "_count_capture_cards", return_value=0))
    stack.enter_context(patch.object(fp, "_enumerate_capture_devices", return_value=()))
    stack.enter_context(patch.object(fp, "_detect_destructive_pulse_modules", return_value=()))
    return stack


# ====================================================================
# Per-helper unit tests
# ====================================================================


class TestKernelMajorMinor:
    """Parse MAJOR.MINOR from a kernel release string."""

    def test_standard_linux_release(self) -> None:
        assert fp._compute_kernel_major_minor("6.8.0-50-generic") == "6.8"

    def test_release_with_build_metadata(self) -> None:
        assert fp._compute_kernel_major_minor("5.15.0-microsoft-standard-WSL2") == "5.15"

    def test_unparseable_returns_empty(self) -> None:
        assert fp._compute_kernel_major_minor("not-a-version") == ""

    def test_empty_returns_empty(self) -> None:
        assert fp._compute_kernel_major_minor("") == ""


class TestParseOsRelease:
    """Parse the freedesktop os-release format."""

    def test_basic(self) -> None:
        content = """NAME="Linux Mint"
ID=linuxmint
ID_LIKE=ubuntu
VERSION_ID="22"
"""
        parsed = fp._parse_os_release(content)
        assert parsed["NAME"] == "Linux Mint"
        assert parsed["ID"] == "linuxmint"
        assert parsed["ID_LIKE"] == "ubuntu"
        assert parsed["VERSION_ID"] == "22"

    def test_skips_comments_and_blank_lines(self) -> None:
        content = """# This is a comment
ID=arch

# another comment
ID_LIKE=
"""
        parsed = fp._parse_os_release(content)
        assert parsed["ID"] == "arch"
        assert parsed["ID_LIKE"] == ""

    def test_handles_unquoted_values(self) -> None:
        parsed = fp._parse_os_release("ID=fedora\n")
        assert parsed["ID"] == "fedora"


class TestSafeSubprocess:
    """The subprocess wrapper never raises."""

    def test_missing_command_returns_empty(self) -> None:
        # A command that surely doesn't exist; should return "" not raise.
        result = fp._safe_subprocess(["this-command-definitely-does-not-exist-xyz123"])
        assert result == ""

    def test_successful_command_returns_stripped_stdout(self) -> None:
        # Use python -c so it runs cross-platform (the test host has python).
        import sys

        result = fp._safe_subprocess([sys.executable, "-c", "print('hello')"])
        assert result == "hello"

    def test_non_zero_exit_returns_empty(self) -> None:
        import sys

        result = fp._safe_subprocess([sys.executable, "-c", "import sys; sys.exit(1)"])
        assert result == ""


class TestDriverFamilyParsing:
    """Map /proc/asound/cards card type tokens to driver families."""

    def test_hda_intel(self, monkeypatch: Any) -> None:
        sample = " 0 [HDA Intel PCH ]: HDA-Intel - HDA Intel PCH\n"
        with patch.object(fp, "_read_proc_asound_cards_lines", return_value=sample.splitlines()):
            assert fp._read_driver_family() == "hda"

    def test_sof(self, monkeypatch: Any) -> None:
        sample = " 0 [sofsoundwire ]: sof-soundwire - sof-soundwire\n"
        with patch.object(fp, "_read_proc_asound_cards_lines", return_value=sample.splitlines()):
            assert fp._read_driver_family() == "sof"

    def test_usb(self, monkeypatch: Any) -> None:
        sample = " 1 [Headset ]: USB-Audio - HyperX Cloud Headset\n"
        with patch.object(fp, "_read_proc_asound_cards_lines", return_value=sample.splitlines()):
            assert fp._read_driver_family() == "usb-audio"

    def test_empty_returns_empty(self, monkeypatch: Any) -> None:
        with patch.object(fp, "_read_proc_asound_cards_lines", return_value=[]):
            assert fp._read_driver_family() == ""


class TestEnumerateCaptureDevices:
    """Extract device names from /proc/asound/cards."""

    def test_single_card(self, monkeypatch: Any) -> None:
        sample = " 0 [HDA Intel PCH ]: HDA-Intel - HDA Intel PCH\n"
        with patch.object(fp, "_read_proc_asound_cards_lines", return_value=sample.splitlines()):
            assert fp._enumerate_capture_devices() == ("HDA Intel PCH",)

    def test_multiple_cards_sorted(self, monkeypatch: Any) -> None:
        sample = (
            " 1 [Webcam     ]: USB-Audio - C920\n 0 [HDA Intel PCH ]: HDA-Intel - HDA Intel PCH\n"
        )
        with patch.object(fp, "_read_proc_asound_cards_lines", return_value=sample.splitlines()):
            # Sort by name, not card index.
            assert fp._enumerate_capture_devices() == ("HDA Intel PCH", "Webcam")


class TestDetectAudioStack:
    """audio_stack detection: pipewire / pulseaudio / alsa-only / non-Linux."""

    def test_non_linux_returns_empty(self) -> None:
        with patch.object(fp.sys, "platform", "darwin"):
            assert fp._detect_audio_stack() == ""

    def test_pipewire_via_pactl_info(self) -> None:
        info = "Server Name: PulseAudio (on PipeWire 1.0.5)\nServer Version: 15.0.0\n"
        with (
            patch.object(fp.sys, "platform", "linux"),
            patch.object(fp.shutil, "which", return_value="/usr/bin/pactl"),
            patch.object(fp, "_safe_subprocess", return_value=info),
        ):
            assert fp._detect_audio_stack() == "pipewire"

    def test_pulseaudio_via_pactl_info(self) -> None:
        info = "Server Name: pulseaudio\nServer Version: 16.1\n"
        with (
            patch.object(fp.sys, "platform", "linux"),
            patch.object(fp.shutil, "which", return_value="/usr/bin/pactl"),
            patch.object(fp, "_safe_subprocess", return_value=info),
        ):
            assert fp._detect_audio_stack() == "pulseaudio"
