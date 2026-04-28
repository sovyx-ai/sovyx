"""Structured capture-pipeline exception hierarchy.

Extracted from ``voice/_capture_task.py`` (lines 293-424 pre-split)
per master mission Phase 1 / T1.4 step 2. Pure exception classes —
no behaviour coupling, no I/O, no class-state mutation.

Hierarchy::

    RuntimeError
        ↳ CaptureError                          (base check-point)
              ↳ CaptureSilenceError             (opened, all-zero frames)
              ↳ CaptureInoperativeError         (cascade exhausted, no viable combo)
              ↳ CaptureDeviceContendedError     (Linux session-manager contention)

Why a base class: the dashboard ``/api/voice/enable`` handler uses
``isinstance(exc, CaptureError)`` to discriminate "known structured
capture failure" from a generic ``RuntimeError`` (which includes
programmer bugs). All three concrete subclasses inherit from
``CaptureError`` so a single ``except CaptureError`` clause catches
the structured-failure surface without swallowing real bugs.

Legacy import surface preserved: ``voice/_capture_task.py``
re-exports every name in ``__all__`` so existing imports like
``from sovyx.voice._capture_task import CaptureInoperativeError``
keep working without an import-path migration.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sovyx.voice._stream_opener import OpenAttempt


__all__ = [
    "CaptureDeviceContendedError",
    "CaptureError",
    "CaptureInoperativeError",
    "CaptureSilenceError",
]


class CaptureError(RuntimeError):
    """Base class for structured capture-pipeline errors.

    Establishes an ``isinstance(..., CaptureError)`` check-point the
    dashboard ``/api/voice/enable`` handler can use to discriminate
    "known structured capture failure" from a generic ``RuntimeError``
    (which includes programmer bugs). Continues to inherit from
    :class:`RuntimeError` so existing ``except RuntimeError`` handlers
    in legacy code keep working during the migration.

    Introduced by ``voice-linux-cascade-root-fix`` T7. Pre-existing
    subclasses (``CaptureSilenceError``, ``CaptureInoperativeError``)
    are re-parented atomically in the same commit; Python MRO preserves
    ``isinstance(..., RuntimeError)`` semantics.
    """


class CaptureSilenceError(CaptureError):
    """The capture stream opened but delivered only silence.

    Typical causes on Windows: MME host API with non-native sample rate
    on a USB headset, exclusive-mode conflict with another app, OS
    microphone privacy block. The ``host_api`` + ``device`` attributes
    let the caller decide whether to retry on a different host API.
    """

    def __init__(
        self,
        message: str,
        *,
        device: int | str | None,
        host_api: str | None,
        observed_peak_rms_db: float,
    ) -> None:
        super().__init__(message)
        self.device = device
        self.host_api = host_api
        self.observed_peak_rms_db = observed_peak_rms_db


class CaptureInoperativeError(CaptureError):
    """The boot cascade declared the capture endpoint inoperative.

    Raised from :func:`sovyx.voice.factory.create_voice_pipeline` BEFORE
    :class:`AudioCaptureTask` is constructed when the cascade exhausted
    every viable combo (or the kernel-invalidated fail-over found no
    alternative endpoint). Bubbling this distinct error type — instead
    of letting the legacy opener fall through to MME shared and
    silently boot a deaf pipeline — is the v0.20.2 §4.4.7 / Bug D fix.

    The dashboard ``/api/voice/enable`` route catches this and returns
    HTTP 503 with the structured diagnosis so the UI can surface a
    real "no working microphone" prompt rather than a fake "capture
    started" success.

    Attributes:
        device: PortAudio device index/name the cascade tried to validate.
        host_api: Host API of the would-be capture endpoint
            (``"WASAPI"`` / ``"ALSA"`` / ``"CoreAudio"`` / ...). May be
            ``None`` when the cascade never resolved a host API.
        reason: Stable string tag for the verdict — ``"no_winner"``
            (cascade exhausted), ``"no_alternative_endpoint"``
            (kernel-invalidated fail-over found nothing). Used by the
            dashboard to localise + show a fix suggestion.
        attempts: Total cascade attempts made before giving up. ``0``
            when the cascade itself crashed before any probe ran.
    """

    def __init__(
        self,
        message: str,
        *,
        device: int | str | None,
        host_api: str | None,
        reason: str,
        attempts: int = 0,
    ) -> None:
        super().__init__(message)
        self.device = device
        self.host_api = host_api
        self.reason = reason
        self.attempts = attempts


class CaptureDeviceContendedError(CaptureError):
    """Every candidate failed with the session-manager-contention pattern.

    Raised when :meth:`AudioCaptureTask._raise_classified_open_error`
    observes a Linux-specific failure pattern where every attempted
    combo on the target device came back with a contention-class
    :class:`~sovyx.voice.device_test._protocol.ErrorCode` (``DEVICE_BUSY``,
    ``DEVICE_DISAPPEARED``, ``DEVICE_NOT_FOUND``) — the strong signal
    that another audio client is holding the hardware. See
    ``docs-internal/ADR-voice-linux-cascade-candidate-set.md`` §5.

    Carries enough structure for the dashboard to render an actionable
    banner: the ``suggested_actions`` tokens map to i18n keys
    (``"select_device:pipewire"`` → "Try the PipeWire virtual device"),
    and ``contending_process_hint`` is populated when the
    :mod:`sovyx.voice._session_manager_detector` managed to pin down
    which process holds the mic (usually ``pipewire`` /
    ``wireplumber``).

    Attributes:
        device: Index / name of the device whose open attempts failed.
        host_api: Host API label. Always ``"ALSA"`` in the known VAIO
            pattern but kept generic for forward compatibility.
        suggested_actions: Ordered list of i18n tokens the UI renders
            as clickable chips. First entry is the most preferred.
        contending_process_hint: Best-effort process name holding the
            device (``"pipewire"`` / ``"wireplumber"`` / ``"pulseaudio"``).
            ``None`` when the detector wasn't consulted or failed.
        attempts: The opener's raw attempts list for debug logs. Not
            rendered in the UI but handy for support tickets.
    """

    def __init__(
        self,
        message: str,
        *,
        device: int | str | None,
        host_api: str | None,
        suggested_actions: list[str],
        contending_process_hint: str | None = None,
        attempts: list[OpenAttempt] | None = None,
    ) -> None:
        super().__init__(message)
        self.device = device
        self.host_api = host_api
        self.suggested_actions = list(suggested_actions)
        self.contending_process_hint = contending_process_hint
        self.attempts = list(attempts) if attempts else []
