"""Tests for ``voice._error_messages.translate_audio_error`` — Phase 7 / T7.27 + T7.28.

Covers every translation table:

* Windows AUDCLNT_E_* (WASAPI HRESULT family).
* Windows MMSYSERR_* (legacy waveIn/Out family).
* POSIX errno (Linux + macOS shared).
* PortAudio paErrorCode mnemonics.
* macOS Core Audio kAudioHardware* codes.

Plus matcher contract:
* Case-insensitive substring matching.
* First-match-wins ordering.
* Unknown errors fall through to UNKNOWN with raw text echo.
* Empty / whitespace-only input handled gracefully.
* Exception inputs str()-ified before matching.
"""

from __future__ import annotations

import pytest

from sovyx.voice._error_messages import (
    AudioErrorClass,
    translate_audio_error,
    translation_count,
)

# ── Windows AUDCLNT_E_* ─────────────────────────────────────────────


class TestWindowsAudClnt:
    @pytest.mark.parametrize(
        "raw",
        [
            "Error: AUDCLNT_E_DEVICE_INVALIDATED",
            "audclnt_e_device_invalidated",
            "HRESULT 0x88890004",
            "Audio error -2004287484 reported",
        ],
    )
    def test_device_invalidated_maps_to_disconnected(self, raw: str) -> None:
        result = translate_audio_error(raw)
        assert result.error_class is AudioErrorClass.DEVICE_DISCONNECTED
        assert "disconnected" in result.user_message.lower()
        assert "reconnect" in result.actionable_hint.lower()

    def test_device_in_use(self) -> None:
        result = translate_audio_error(
            "PortAudioError: AUDCLNT_E_DEVICE_IN_USE (0x8889000a)",
        )
        assert result.error_class is AudioErrorClass.DEVICE_IN_USE
        assert "exclusive mode" in result.user_message.lower()
        # Hint mentions specific apps to close.
        assert any(app in result.actionable_hint.lower() for app in ("zoom", "teams", "discord"))

    def test_exclusive_mode_not_allowed(self) -> None:
        result = translate_audio_error("AUDCLNT_E_EXCLUSIVE_MODE_NOT_ALLOWED")
        assert result.error_class is AudioErrorClass.EXCLUSIVE_MODE_DENIED
        assert result.severity == "warning"

    @pytest.mark.parametrize(
        "raw",
        [
            "AUDCLNT_E_BUFFER_SIZE_NOT_ALIGNED",
            "audclnt_e_buffer_too_large",
            "0x88890011",
        ],
    )
    def test_buffer_size_family(self, raw: str) -> None:
        result = translate_audio_error(raw)
        assert result.error_class is AudioErrorClass.BUFFER_SIZE_ERROR

    def test_unsupported_format(self) -> None:
        result = translate_audio_error(
            "AUDCLNT_E_UNSUPPORTED_FORMAT (0x88890008)",
        )
        assert result.error_class is AudioErrorClass.UNSUPPORTED_FORMAT
        assert "format" in result.user_message.lower()

    def test_service_not_running(self) -> None:
        result = translate_audio_error("AUDCLNT_E_SERVICE_NOT_RUNNING")
        assert result.error_class is AudioErrorClass.SERVICE_NOT_RUNNING
        assert result.severity == "fatal"
        assert "audiosrv" in result.actionable_hint.lower()


# ── Windows MMSYSERR ────────────────────────────────────────────────


class TestWindowsMMSys:
    def test_mmsyserr_allocated(self) -> None:
        result = translate_audio_error("MMSYSERR_ALLOCATED")
        assert result.error_class is AudioErrorClass.DEVICE_IN_USE

    def test_mmsyserr_nodriver(self) -> None:
        result = translate_audio_error("MMSYSERR_NODRIVER")
        assert result.error_class is AudioErrorClass.DRIVER_FAILURE
        assert result.severity == "fatal"


# ── POSIX errno ─────────────────────────────────────────────────────


class TestPosixErrno:
    @pytest.mark.parametrize(
        "raw",
        [
            "[Errno 16] Device or resource busy",
            "EBUSY: device or resource busy",
            "OSError: [Errno 16]",
        ],
    )
    def test_ebusy(self, raw: str) -> None:
        result = translate_audio_error(raw)
        assert result.error_class is AudioErrorClass.DEVICE_IN_USE

    @pytest.mark.parametrize(
        "raw",
        [
            "[Errno 13] Permission denied",
            "EACCES",
            "Permission denied opening /dev/snd/pcmC0D0c",
            "Operation not permitted",
        ],
    )
    def test_permission_denied(self, raw: str) -> None:
        result = translate_audio_error(raw)
        assert result.error_class is AudioErrorClass.PERMISSION_DENIED
        # Hint covers all three platforms.
        hint_lower = result.actionable_hint.lower()
        assert "macos" in hint_lower
        assert "linux" in hint_lower
        assert "windows" in hint_lower

    def test_enodev(self) -> None:
        result = translate_audio_error("[Errno 19] No such device")
        assert result.error_class is AudioErrorClass.DEVICE_NOT_FOUND

    def test_enoent(self) -> None:
        result = translate_audio_error(
            "[Errno 2] No such file or directory: '/dev/snd/pcmC0D0c'",
        )
        assert result.error_class is AudioErrorClass.DEVICE_NOT_FOUND

    def test_einval(self) -> None:
        result = translate_audio_error("[Errno 22] Invalid argument")
        assert result.error_class is AudioErrorClass.INVALID_ARGUMENT
        assert result.severity == "warning"


# ── PortAudio mnemonics ─────────────────────────────────────────────


class TestPortAudio:
    def test_painvaliddevice(self) -> None:
        result = translate_audio_error("paInvalidDevice (-9996)")
        assert result.error_class is AudioErrorClass.DEVICE_NOT_FOUND

    def test_padeviceunavailable(self) -> None:
        result = translate_audio_error("paDeviceUnavailable (-9985)")
        assert result.error_class is AudioErrorClass.DEVICE_DISCONNECTED

    def test_painvalidsamplerate(self) -> None:
        result = translate_audio_error("paInvalidSampleRate (-9986)")
        assert result.error_class is AudioErrorClass.UNSUPPORTED_FORMAT

    def test_paunanticipatedhosterror(self) -> None:
        result = translate_audio_error("paUnanticipatedHostError (-9999)")
        assert result.error_class is AudioErrorClass.DRIVER_FAILURE

    def test_pastreamisstopped(self) -> None:
        result = translate_audio_error("paStreamIsStopped")
        assert result.error_class is AudioErrorClass.DRIVER_FAILURE


# ── macOS Core Audio ────────────────────────────────────────────────


class TestCoreAudio:
    def test_baddeviceerror(self) -> None:
        result = translate_audio_error("kAudioHardwareBadDeviceError")
        assert result.error_class is AudioErrorClass.DEVICE_NOT_FOUND
        # Hint mentions Audio MIDI Setup (the Mac-specific fix path).
        assert "audio midi setup" in result.actionable_hint.lower()

    def test_illegaloperationerror(self) -> None:
        result = translate_audio_error("kAudioHardwareIllegalOperationError")
        assert result.error_class is AudioErrorClass.DRIVER_FAILURE
        assert "coreaudiod" in result.actionable_hint.lower()

    def test_notrunningerror(self) -> None:
        result = translate_audio_error("kAudioHardwareNotRunningError")
        assert result.error_class is AudioErrorClass.SERVICE_NOT_RUNNING
        assert result.severity == "fatal"

    def test_unsupportedoperationerror(self) -> None:
        result = translate_audio_error("kAudioHardwareUnsupportedOperationError")
        assert result.error_class is AudioErrorClass.UNSUPPORTED_FORMAT


# ── Matcher contract ────────────────────────────────────────────────


class TestMatcherContract:
    def test_case_insensitive(self) -> None:
        upper = translate_audio_error("AUDCLNT_E_DEVICE_INVALIDATED")
        lower = translate_audio_error("audclnt_e_device_invalidated")
        mixed = translate_audio_error("AudClnt_E_Device_Invalidated")
        assert upper.error_class is AudioErrorClass.DEVICE_DISCONNECTED
        assert lower.error_class is AudioErrorClass.DEVICE_DISCONNECTED
        assert mixed.error_class is AudioErrorClass.DEVICE_DISCONNECTED

    def test_substring_matching(self) -> None:
        """Real PortAudio messages embed the token in a longer
        sentence — matcher must catch the substring."""
        result = translate_audio_error(
            "Error opening InputStream: PaErrorCode -9996, "
            "Error message: 'Invalid device' (paInvalidDevice)",
        )
        assert result.error_class is AudioErrorClass.DEVICE_NOT_FOUND

    def test_unknown_error_returns_raw_text(self) -> None:
        result = translate_audio_error("Some completely unrelated error text")
        assert result.error_class is AudioErrorClass.UNKNOWN
        assert "Some completely unrelated error text" in result.user_message

    def test_empty_input(self) -> None:
        result = translate_audio_error("")
        assert result.error_class is AudioErrorClass.UNKNOWN
        assert "empty" in result.user_message.lower()

    def test_whitespace_input(self) -> None:
        result = translate_audio_error("   \t\n   ")
        assert result.error_class is AudioErrorClass.UNKNOWN
        assert "empty" in result.user_message.lower()

    def test_exception_input_stringified(self) -> None:
        """Pass an Exception directly — translator str()-ifies it."""
        result = translate_audio_error(OSError(13, "Permission denied"))
        assert result.error_class is AudioErrorClass.PERMISSION_DENIED

    def test_long_unknown_text_truncated(self) -> None:
        """200+ char unknown errors get truncated for UI rendering."""
        long_text = "ZZZ" * 200  # 600 chars, no recognisable token
        result = translate_audio_error(long_text)
        assert result.error_class is AudioErrorClass.UNKNOWN
        assert len(result.user_message) < 250  # truncated  # noqa: PLR2004
        assert result.user_message.endswith("...")


# ── Wire-format stability ───────────────────────────────────────────


class TestErrorClassEnum:
    def test_canonical_class_values(self) -> None:
        """Mission-mandated taxonomy. Renaming = breaking schema
        change for downstream telemetry / dashboard correlations."""
        names = {c.value for c in AudioErrorClass}
        assert names == {
            "device_not_found",
            "device_in_use",
            "device_disconnected",
            "permission_denied",
            "unsupported_format",
            "buffer_size_error",
            "exclusive_mode_denied",
            "driver_failure",
            "invalid_argument",
            "service_not_running",
            "unknown",
        }


class TestImmutability:
    def test_translation_dataclass_frozen(self) -> None:
        result = translate_audio_error("EBUSY")
        with pytest.raises((AttributeError, TypeError)):
            result.user_message = "tampered"  # type: ignore[misc]


# ── Coverage breadth ────────────────────────────────────────────────


class TestCoverage:
    def test_translation_count_matches_expected(self) -> None:
        """Pin the number of distinct patterns. Bumps require a
        deliberate test update — preserves audit trail of additions."""
        # 7 AUDCLNT + 2 MMSYSERR + 4 CoreAudio + 5 PortAudio + 5 POSIX = 23.
        assert translation_count() == 23  # noqa: PLR2004

    def test_every_class_has_at_least_one_pattern(self) -> None:
        """Each non-UNKNOWN error class must have at least one
        pattern — UNKNOWN is the explicit fallback."""
        seen_classes: set[AudioErrorClass] = set()
        # Simple corpus exercising each pattern table.
        corpus = [
            "AUDCLNT_E_DEVICE_INVALIDATED",
            "AUDCLNT_E_DEVICE_IN_USE",
            "AUDCLNT_E_EXCLUSIVE_MODE_NOT_ALLOWED",
            "AUDCLNT_E_BUFFER_SIZE_NOT_ALIGNED",
            "AUDCLNT_E_UNSUPPORTED_FORMAT",
            "AUDCLNT_E_NOT_INITIALIZED",
            "AUDCLNT_E_SERVICE_NOT_RUNNING",
            "MMSYSERR_NODRIVER",
            "[Errno 13] Permission denied",
            "[Errno 22] Invalid argument",
            "kAudioHardwareBadDeviceError",
        ]
        for raw in corpus:
            seen_classes.add(translate_audio_error(raw).error_class)
        # Should hit the major classes.
        for required in (
            AudioErrorClass.DEVICE_DISCONNECTED,
            AudioErrorClass.DEVICE_IN_USE,
            AudioErrorClass.EXCLUSIVE_MODE_DENIED,
            AudioErrorClass.BUFFER_SIZE_ERROR,
            AudioErrorClass.UNSUPPORTED_FORMAT,
            AudioErrorClass.DRIVER_FAILURE,
            AudioErrorClass.SERVICE_NOT_RUNNING,
            AudioErrorClass.PERMISSION_DENIED,
            AudioErrorClass.INVALID_ARGUMENT,
            AudioErrorClass.DEVICE_NOT_FOUND,
        ):
            assert required in seen_classes, f"class {required} not exercised"
