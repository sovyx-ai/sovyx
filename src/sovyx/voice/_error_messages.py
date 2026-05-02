"""Plain-language audio error translation — Phase 7 / T7.27 + T7.28.

When the voice subsystem surfaces an OS-level audio error to the
operator (wizard ``device_error`` diagnosis, voice doctor output,
dashboard "voice not hearing me" panel), the raw error message is
typically a Windows ``HRESULT 0x88890004`` or a POSIX ``[Errno 13]``
or a PortAudio ``paInvalidDevice (-9996)``. Useful for engineers,
opaque to non-technical operators.

This module maps the raw token to operator-facing structured
guidance:

* ``error_class`` — canonical category for telemetry / UI styling.
* ``user_message`` — one-line plain English explanation.
* ``actionable_hint`` — specific next step the operator should take.
* ``severity`` — ``info`` | ``warning`` | ``error`` | ``fatal``.

The translation is **append-only**: adding a new HRESULT or errno
entry is a one-line PR; existing entries are stable wire surface
(error_class values must not be renamed because dashboards
correlate against them).

Coverage:
* Windows AUDCLNT_E_* codes (the WASAPI HRESULT family)
* Windows MMSYSERR_* codes (the legacy waveIn/Out family)
* POSIX errno names (Linux + macOS shared)
* PortAudio paErrorCode mnemonics
* macOS Core Audio kAudioHardware* codes

Reference: master mission §Phase 7 / T7.27 + T7.28.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from sovyx.observability.logging import get_logger

logger = get_logger(__name__)


class AudioErrorClass(StrEnum):
    """Canonical error categories for telemetry + UI styling.

    StrEnum so the wire format is stable (operators consume the
    string value via JSON / log greps). Adding a new member is
    safe; renaming breaks downstream correlations.
    """

    DEVICE_NOT_FOUND = "device_not_found"
    """No such device / device disappeared."""

    DEVICE_IN_USE = "device_in_use"
    """Another process holds the device in exclusive mode."""

    DEVICE_DISCONNECTED = "device_disconnected"
    """Device was unplugged or removed mid-session."""

    PERMISSION_DENIED = "permission_denied"
    """OS-level access denial (TCC on macOS, Group Policy on Windows,
    udev on Linux)."""

    UNSUPPORTED_FORMAT = "unsupported_format"
    """Sample rate / channel count / bit depth not supported by the
    driver."""

    BUFFER_SIZE_ERROR = "buffer_size_error"
    """Frame buffer size rejected (too large, not aligned, etc.)."""

    EXCLUSIVE_MODE_DENIED = "exclusive_mode_denied"
    """Driver refuses exclusive-mode opening."""

    DRIVER_FAILURE = "driver_failure"
    """Audio driver itself reported an internal error."""

    INVALID_ARGUMENT = "invalid_argument"
    """Caller passed an out-of-range parameter (sample rate, channels)."""

    SERVICE_NOT_RUNNING = "service_not_running"
    """Audio service (Audiosrv on Win, PulseAudio/PipeWire on Linux,
    coreaudiod on macOS) is not responsive."""

    UNKNOWN = "unknown"
    """No translation matched. ``user_message`` echoes the raw
    error verbatim."""


@dataclass(frozen=True, slots=True)
class AudioErrorTranslation:
    """Structured translation of a raw OS audio error.

    Attributes:
        error_class: Canonical category (see :class:`AudioErrorClass`).
        user_message: One-line plain English. UI renders this as the
            primary error text.
        actionable_hint: Specific next step. UI renders below the
            user message in a slightly less prominent style.
        severity: ``info`` / ``warning`` / ``error`` / ``fatal``.
            Drives UI colour code (yellow / red / etc.).
        raw_token: The exact substring from the input that matched a
            translation table entry. Empty for ``UNKNOWN``.
    """

    error_class: AudioErrorClass
    user_message: str
    actionable_hint: str
    severity: str
    raw_token: str


# ── Translation tables ──────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _Pattern:
    """One pattern → translation entry.

    Patterns are case-insensitive substrings checked against the
    lowercased input. Order matters: more specific patterns must
    come BEFORE less specific ones in the master list (the matcher
    returns the first hit).
    """

    tokens: tuple[str, ...]
    translation: AudioErrorTranslation


# Windows WASAPI / Audio Client (AUDCLNT_E_*) — HRESULT 0x88890xxx
# (signed -2004287xxx). Sources: Microsoft Audio Client API docs +
# `voice/health/probe/_cold.py` token tables.
_WINDOWS_AUDCLNT_PATTERNS: tuple[_Pattern, ...] = (
    _Pattern(
        tokens=(
            "audclnt_e_device_invalidated",
            "0x88890004",
            "-2004287484",
        ),
        translation=AudioErrorTranslation(
            error_class=AudioErrorClass.DEVICE_DISCONNECTED,
            user_message="Your microphone was disconnected.",
            actionable_hint=(
                "Reconnect the microphone and reselect it in the "
                "device picker. If it's a USB device, try a different "
                "port."
            ),
            severity="error",
            raw_token="AUDCLNT_E_DEVICE_INVALIDATED",
        ),
    ),
    _Pattern(
        tokens=(
            "audclnt_e_device_in_use",
            "0x8889000a",
            "-2004287478",
        ),
        translation=AudioErrorTranslation(
            error_class=AudioErrorClass.DEVICE_IN_USE,
            user_message=("Another application is holding your microphone in exclusive mode."),
            actionable_hint=(
                "Close apps that may be using the mic (Zoom, Teams, Discord, Skype) and try again."
            ),
            severity="error",
            raw_token="AUDCLNT_E_DEVICE_IN_USE",
        ),
    ),
    _Pattern(
        tokens=(
            "audclnt_e_exclusive_mode_not_allowed",
            "0x88890017",
            "-2004287465",
        ),
        translation=AudioErrorTranslation(
            error_class=AudioErrorClass.EXCLUSIVE_MODE_DENIED,
            user_message=("The microphone driver doesn't allow exclusive-mode access."),
            actionable_hint=(
                "Sovyx will fall back to shared mode automatically. "
                "If wake-word detection still fails, run "
                "``sovyx doctor voice --fix --yes``."
            ),
            severity="warning",
            raw_token="AUDCLNT_E_EXCLUSIVE_MODE_NOT_ALLOWED",
        ),
    ),
    _Pattern(
        tokens=(
            "audclnt_e_buffer_size_not_aligned",
            "audclnt_e_buffer_too_large",
            "audclnt_e_buffer_size_error",
            "0x88890019",
            "-2004287463",
            "0x88890011",
            "-2004287471",
            "0x88890018",
            "-2004287464",
        ),
        translation=AudioErrorTranslation(
            error_class=AudioErrorClass.BUFFER_SIZE_ERROR,
            user_message=("The audio driver rejected Sovyx's buffer size."),
            actionable_hint=(
                "Sovyx will retry with a different buffer size "
                "automatically. If the error persists, update your "
                "audio driver to the latest version."
            ),
            severity="warning",
            raw_token="AUDCLNT_E_BUFFER_SIZE_*",
        ),
    ),
    _Pattern(
        tokens=(
            "audclnt_e_unsupported_format",
            "0x88890008",
            "-2004287480",
        ),
        translation=AudioErrorTranslation(
            error_class=AudioErrorClass.UNSUPPORTED_FORMAT,
            user_message=("Your microphone doesn't support the requested audio format."),
            actionable_hint=(
                "Sovyx tries multiple sample rates automatically. If "
                "this error keeps surfacing, check that the mic isn't "
                "stuck on a non-standard rate (Windows: Sound Control "
                "Panel → Properties → Advanced)."
            ),
            severity="warning",
            raw_token="AUDCLNT_E_UNSUPPORTED_FORMAT",
        ),
    ),
    _Pattern(
        tokens=(
            "audclnt_e_not_initialized",
            "0x88890001",
            "-2004287487",
        ),
        translation=AudioErrorTranslation(
            error_class=AudioErrorClass.DRIVER_FAILURE,
            user_message=("The Windows audio session wasn't initialised correctly."),
            actionable_hint=(
                "This is usually a transient error. Restart Sovyx "
                "and retry. If it persists, restart the Windows "
                "Audio service (services.msc → Audiosrv → Restart)."
            ),
            severity="error",
            raw_token="AUDCLNT_E_NOT_INITIALIZED",
        ),
    ),
    _Pattern(
        tokens=(
            "audclnt_e_service_not_running",
            "0x80004005",
        ),
        translation=AudioErrorTranslation(
            error_class=AudioErrorClass.SERVICE_NOT_RUNNING,
            user_message="The Windows Audio service is not running.",
            actionable_hint=(
                "Open services.msc, find 'Windows Audio' (Audiosrv), "
                "and click Start. If it won't start, check Event "
                "Viewer for driver errors."
            ),
            severity="fatal",
            raw_token="AUDCLNT_E_SERVICE_NOT_RUNNING",
        ),
    ),
)


# Windows legacy MMSYSERR_*. Surfaces less commonly than AUDCLNT_E_*
# but still appears via PortAudio's WMME host API.
_WINDOWS_MMSYS_PATTERNS: tuple[_Pattern, ...] = (
    _Pattern(
        tokens=("mmsyserr_allocated", "device is already in use"),
        translation=AudioErrorTranslation(
            error_class=AudioErrorClass.DEVICE_IN_USE,
            user_message="Another application is using your microphone.",
            actionable_hint=("Close other audio apps (Zoom, Teams, Discord) and try again."),
            severity="error",
            raw_token="MMSYSERR_ALLOCATED",
        ),
    ),
    _Pattern(
        tokens=("mmsyserr_nodriver",),
        translation=AudioErrorTranslation(
            error_class=AudioErrorClass.DRIVER_FAILURE,
            user_message="The microphone has no audio driver loaded.",
            actionable_hint=(
                "Open Device Manager, find your microphone, and "
                "reinstall its driver. Then restart Sovyx."
            ),
            severity="fatal",
            raw_token="MMSYSERR_NODRIVER",
        ),
    ),
)


# POSIX errno (Linux + macOS shared). Surfaces from PortAudio's
# native host APIs (ALSA, Core Audio) when they propagate the
# underlying syscall error.
_POSIX_ERRNO_PATTERNS: tuple[_Pattern, ...] = (
    _Pattern(
        tokens=("[errno 16]", "ebusy", "device or resource busy"),
        translation=AudioErrorTranslation(
            error_class=AudioErrorClass.DEVICE_IN_USE,
            user_message="The microphone is busy.",
            actionable_hint=(
                "Another process is holding the device. On Linux, "
                "check ``fuser /dev/snd/*`` to identify the owner. "
                "Closing PulseAudio / PipeWire clients (Firefox, "
                "Chrome's media tabs) typically frees it."
            ),
            severity="error",
            raw_token="EBUSY",
        ),
    ),
    _Pattern(
        tokens=(
            "[errno 13]",
            "eacces",
            "permission denied",
            "operation not permitted",
        ),
        translation=AudioErrorTranslation(
            error_class=AudioErrorClass.PERMISSION_DENIED,
            user_message=("Sovyx doesn't have permission to access your microphone."),
            actionable_hint=(
                "On macOS: System Settings → Privacy & Security → "
                "Microphone → enable Sovyx (or your terminal). "
                "On Linux: confirm your user is in the ``audio`` "
                "group (``groups | grep audio``). On Windows: "
                "Settings → Privacy & security → Microphone → "
                "enable for Sovyx."
            ),
            severity="error",
            raw_token="EACCES",
        ),
    ),
    _Pattern(
        tokens=("[errno 19]", "enodev", "no such device"),
        translation=AudioErrorTranslation(
            error_class=AudioErrorClass.DEVICE_NOT_FOUND,
            user_message="The selected microphone is not available.",
            actionable_hint=(
                "The device may have been disconnected or disabled. "
                "Reselect a device from the picker."
            ),
            severity="error",
            raw_token="ENODEV",
        ),
    ),
    _Pattern(
        tokens=("[errno 2]", "enoent", "no such file or directory"),
        translation=AudioErrorTranslation(
            error_class=AudioErrorClass.DEVICE_NOT_FOUND,
            user_message="The audio device file is missing.",
            actionable_hint=(
                "On Linux, check that ``/dev/snd/`` is populated. "
                "If empty, your audio driver may have crashed; reboot "
                "or run ``sudo modprobe snd``."
            ),
            severity="error",
            raw_token="ENOENT",
        ),
    ),
    _Pattern(
        tokens=("[errno 22]", "einval", "invalid argument"),
        translation=AudioErrorTranslation(
            error_class=AudioErrorClass.INVALID_ARGUMENT,
            user_message=("Sovyx passed a parameter the audio driver doesn't accept."),
            actionable_hint=(
                "Sovyx will retry with safer defaults. If the error "
                "persists, your driver may need an update."
            ),
            severity="warning",
            raw_token="EINVAL",
        ),
    ),
)


# PortAudio paErrorCode mnemonics. PortAudio is the cross-platform
# shim that sounddevice uses; surfaces these when a host API doesn't
# raise its own native error code.
_PORTAUDIO_PATTERNS: tuple[_Pattern, ...] = (
    _Pattern(
        tokens=("painvaliddevice", "-9996", "invalid device"),
        translation=AudioErrorTranslation(
            error_class=AudioErrorClass.DEVICE_NOT_FOUND,
            user_message="The selected device is no longer valid.",
            actionable_hint=(
                "The device list may have changed. Refresh the device picker and select again."
            ),
            severity="error",
            raw_token="paInvalidDevice",
        ),
    ),
    _Pattern(
        tokens=("padeviceunavailable", "-9985", "device unavailable"),
        translation=AudioErrorTranslation(
            error_class=AudioErrorClass.DEVICE_DISCONNECTED,
            user_message="The microphone is unavailable.",
            actionable_hint=(
                "Check that the device is plugged in and not disabled in your OS sound settings."
            ),
            severity="error",
            raw_token="paDeviceUnavailable",
        ),
    ),
    _Pattern(
        tokens=("painvalidsamplerate", "-9986", "invalid sample rate"),
        translation=AudioErrorTranslation(
            error_class=AudioErrorClass.UNSUPPORTED_FORMAT,
            user_message="The microphone doesn't support 16 kHz capture.",
            actionable_hint=(
                "Sovyx tries multiple sample rates. If this surfaces "
                "repeatedly, set your default mic format to 16 kHz "
                "or 48 kHz in OS sound settings."
            ),
            severity="warning",
            raw_token="paInvalidSampleRate",
        ),
    ),
    _Pattern(
        tokens=(
            "paunanticipatedhosterror",
            "-9999",
            "unanticipated host error",
        ),
        translation=AudioErrorTranslation(
            error_class=AudioErrorClass.DRIVER_FAILURE,
            user_message=("The audio driver returned an unexpected error."),
            actionable_hint=(
                "This is usually transient — retry. If it persists, update your audio driver."
            ),
            severity="warning",
            raw_token="paUnanticipatedHostError",
        ),
    ),
    _Pattern(
        tokens=("pastreamisstopped", "-9988", "stream is stopped"),
        translation=AudioErrorTranslation(
            error_class=AudioErrorClass.DRIVER_FAILURE,
            user_message="The audio stream stopped unexpectedly.",
            actionable_hint=(
                "Sovyx will reopen the stream automatically. If the "
                "issue continues, check Event Viewer (Windows) or "
                "``journalctl -u pipewire`` (Linux) for driver errors."
            ),
            severity="warning",
            raw_token="paStreamIsStopped",
        ),
    ),
)


# macOS Core Audio kAudioHardware* codes. Surfaces from PortAudio's
# Core Audio host API.
_COREAUDIO_PATTERNS: tuple[_Pattern, ...] = (
    _Pattern(
        tokens=("kaudiohardwarebaddeviceerror",),
        translation=AudioErrorTranslation(
            error_class=AudioErrorClass.DEVICE_NOT_FOUND,
            user_message="The selected microphone isn't available.",
            actionable_hint=(
                "Open Audio MIDI Setup and confirm the device is "
                "present. If you use Aggregate or virtual devices "
                "(BlackHole, Loopback), recreate the aggregate."
            ),
            severity="error",
            raw_token="kAudioHardwareBadDeviceError",
        ),
    ),
    _Pattern(
        tokens=("kaudiohardwareillegaloperationerror",),
        translation=AudioErrorTranslation(
            error_class=AudioErrorClass.DRIVER_FAILURE,
            user_message=("The Core Audio driver rejected the operation."),
            actionable_hint=(
                "Restart coreaudiod: ``sudo killall coreaudiod`` "
                "(macOS auto-respawns the daemon). Then retry."
            ),
            severity="error",
            raw_token="kAudioHardwareIllegalOperationError",
        ),
    ),
    _Pattern(
        tokens=("kaudiohardwarenotrunningerror",),
        translation=AudioErrorTranslation(
            error_class=AudioErrorClass.SERVICE_NOT_RUNNING,
            user_message="Core Audio (coreaudiod) is not running.",
            actionable_hint=(
                "macOS should auto-restart it. If it doesn't, reboot "
                "or run ``sudo launchctl kickstart -k "
                "system/com.apple.audio.coreaudiod``."
            ),
            severity="fatal",
            raw_token="kAudioHardwareNotRunningError",
        ),
    ),
    _Pattern(
        tokens=("kaudiohardwareunsupportedoperationerror",),
        translation=AudioErrorTranslation(
            error_class=AudioErrorClass.UNSUPPORTED_FORMAT,
            user_message=("The microphone driver doesn't support this operation."),
            actionable_hint=(
                "Try a different device, or check Audio MIDI Setup "
                "for the device's supported formats."
            ),
            severity="warning",
            raw_token="kAudioHardwareUnsupportedOperationError",
        ),
    ),
)


# Master pattern list — order is significant. More specific Windows
# tokens come BEFORE the POSIX errno catchall (some PortAudio messages
# include both).
_ALL_PATTERNS: tuple[_Pattern, ...] = (
    *_WINDOWS_AUDCLNT_PATTERNS,
    *_WINDOWS_MMSYS_PATTERNS,
    *_COREAUDIO_PATTERNS,
    *_PORTAUDIO_PATTERNS,
    *_POSIX_ERRNO_PATTERNS,
)


# ── Public translator ───────────────────────────────────────────────


def translate_audio_error(
    error: Exception | str,
) -> AudioErrorTranslation:
    """Translate a raw OS audio error to operator-facing guidance.

    Args:
        error: An exception or its string representation. Common
            inputs:
            * ``sounddevice.PortAudioError(...)``
            * ``OSError(13, "Permission denied")``
            * raw string from an external tool
            * a Windows ``HRESULT`` formatted as
              ``"... AUDCLNT_E_DEVICE_IN_USE 0x8889000a -2004287478"``

    Returns:
        :class:`AudioErrorTranslation` with a matched class +
        plain-language message + actionable hint. When no pattern
        matches, returns ``error_class=UNKNOWN`` with the raw text
        as ``user_message`` so the operator at least sees the
        original error.
    """
    # str() works on both Exception and str — the explicit branch
    # remains for self-documentation of the accepted input shapes.
    text = str(error)

    if not text.strip():
        result = AudioErrorTranslation(
            error_class=AudioErrorClass.UNKNOWN,
            user_message="Unknown audio error (empty error message).",
            actionable_hint=(
                "Re-run with ``SOVYX_LOG__LEVEL=DEBUG`` and check the "
                "logs for the underlying cause."
            ),
            severity="warning",
            raw_token="",
        )
        _emit_translation_metric(result.error_class.value)
        return result

    lowered = text.lower()
    for pattern in _ALL_PATTERNS:
        for token in pattern.tokens:
            if token in lowered:
                _emit_translation_metric(pattern.translation.error_class.value)
                return pattern.translation

    # No match — fall back to the raw text. Truncate to 200 chars so
    # the wizard UI doesn't blow out the layout on multi-line stack
    # traces.
    truncated = text if len(text) <= 200 else f"{text[:197]}..."  # noqa: PLR2004
    result = AudioErrorTranslation(
        error_class=AudioErrorClass.UNKNOWN,
        user_message=f"Audio error: {truncated}",
        actionable_hint=(
            "This error wasn't recognised by Sovyx's translation table. "
            "Re-run with ``SOVYX_LOG__LEVEL=DEBUG`` and check logs, "
            "or report at security@sovyx.ai with the full error text."
        ),
        severity="error",
        raw_token="",
    )
    _emit_translation_metric(result.error_class.value)
    return result


def _emit_translation_metric(error_class: str) -> None:
    """Best-effort OTel counter emission. Suppresses any failure.

    The translation function MUST stay correct even when the metrics
    subsystem isn't initialised (early boot, tests with mocked
    registry, headless one-off uses). Telemetry emission is a
    side-effect that observers care about; translation correctness
    is the contract callers depend on. So we lazy-import + suppress
    every failure mode.
    """
    try:
        from sovyx.voice.health._metrics import (  # noqa: PLC0415
            record_audio_error_translated,
        )

        record_audio_error_translated(error_class=error_class)
    except Exception:  # noqa: BLE001 — best-effort by design
        pass


def translation_count() -> int:
    """Number of distinct error patterns in the translation table.

    Useful for telemetry / docs ("Sovyx maps N audio error codes
    to plain-language guidance"). Each pattern entry counts once
    regardless of how many tokens it carries.
    """
    return len(_ALL_PATTERNS)


__all__ = [
    "AudioErrorClass",
    "AudioErrorTranslation",
    "translate_audio_error",
    "translation_count",
]
