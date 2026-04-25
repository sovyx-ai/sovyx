"""Background audio capture task that feeds the VoicePipeline.

The :class:`~sovyx.voice.pipeline.VoicePipeline` is push-based — frames
must be delivered via ``pipeline.feed_frame()``. This module owns the
microphone side: opens a ``sounddevice.InputStream`` on the selected
input device (through the unified :mod:`sovyx.voice._stream_opener`
pyramid), pulls int16 frames from its callback into an asyncio queue,
and dispatches each frame into the pipeline from a consumer task. On
device disconnection the stream is closed, the task waits for
``capture_reconnect_delay_seconds``, and retries from scratch — again
through the opener, so reconnect inherits host-API × rate × channels
fallback for free.

Lifecycle (owned by the hot-enable endpoint)::

    capture = AudioCaptureTask(pipeline, input_device=device_index)
    await capture.start()
    ...
    await capture.stop()

Post-open validation
--------------------

``sd.InputStream`` on Windows happily opens a broken configuration
(MME + 16 kHz on a 48 kHz Razer driver, privacy-blocked mic, etc.) and
then delivers **all-zero frames** without raising. The pipeline looks
"running" but is deaf. :meth:`AudioCaptureTask.start` hands a
``validate_fn`` to :func:`sovyx.voice._stream_opener.open_input_stream`,
which samples ~600 ms of audio after opening each variant and rejects
it when the peak RMS never crosses ``capture_validation_min_rms_db``.
The opener walks the full pyramid automatically, so silent variants
are replaced by their host-API siblings without any caller-side
bookkeeping.

Without this task the pipeline is silent: frames never arrive and VAD
never fires. See CLAUDE.md §anti-pattern #14 — ONNX inference is run on
``asyncio.to_thread`` already inside :meth:`VoicePipeline.feed_frame`,
so this consumer loop does not need to offload work itself.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import re
import sys
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from sovyx.engine._backoff import BackoffPolicy, BackoffSchedule, JitterStrategy
from sovyx.engine.config import VoiceTuningConfig as _VoiceTuning
from sovyx.observability.logging import get_logger
from sovyx.observability.tasks import spawn
from sovyx.voice._agc2 import build_agc2_if_enabled
from sovyx.voice._frame_normalizer import FrameNormalizer
from sovyx.voice._stream_opener import _import_sounddevice

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    import numpy as np
    import numpy.typing as npt

    from sovyx.engine.config import VoiceTuningConfig
    from sovyx.voice._stream_opener import OpenAttempt
    from sovyx.voice.device_enum import DeviceEntry
    from sovyx.voice.pipeline._orchestrator import VoicePipeline

logger = get_logger(__name__)

_SAMPLE_RATE = 16_000
_FRAME_SAMPLES = 512  # must match VoicePipeline._FRAME_SAMPLES
_RECONNECT_DELAY_S = _VoiceTuning().capture_reconnect_delay_seconds
_QUEUE_MAXSIZE = _VoiceTuning().capture_queue_maxsize
_VALIDATION_S = _VoiceTuning().capture_validation_seconds
_VALIDATION_MIN_RMS_DB = _VoiceTuning().capture_validation_min_rms_db
_HEARTBEAT_INTERVAL_S = _VoiceTuning().capture_heartbeat_interval_seconds

# ── v1.3 §4.2 L4-B — ring buffer state packing ──────────────────────
#
# The capture task packs ``(epoch, samples_written)`` into a single
# ``int`` attribute so a single ``LOAD_ATTR`` by an external consumer
# (:meth:`samples_written_mark`) observes both components atomically
# — without the packing, a reader could race the writer between the
# "bump samples" and "bump epoch" assignments and see an epoch that
# does not match the samples count.
#
# Layout: ``state = (epoch << _RING_EPOCH_SHIFT) | (samples & _RING_SAMPLES_MASK)``
#
# * Samples occupy the low 40 bits. At 16 kHz that is 2**40 / 16000 / 86400 / 365
#   ≈ 2179 years of continuous capture before wrapping — practically unreachable
#   within a single process lifetime.
# * Epoch occupies the remaining (high) bits of a Python ``int`` (arbitrary
#   precision). In realistic use the epoch increments once per stream reopen,
#   so ~10^4 is the practical ceiling across a multi-year daemon lifetime.
#
# External consumers (:class:`CaptureIntegrityCoordinator`) receive the
# pair as ``tuple[int, int]`` — neither component individually can
# exceed ``2**53`` in realistic deployments, so the tuple survives JSON
# / Prometheus / structlog serialization boundaries without truncation.
_RING_EPOCH_SHIFT = 40
_RING_SAMPLES_MASK = (1 << _RING_EPOCH_SHIFT) - 1

# Floor for log10 — 32-bit PCM noise ≈ -96 dBFS, so -120 is safely below.
_RMS_FLOOR_DB = -120.0


# ── T7 session-manager contention helpers ────────────────────────────

_SESSION_MANAGER_CONTENTION_ERROR_CODES: frozenset[str] = frozenset(
    {
        "device_busy",
        "device_disappeared",
        "device_not_found",
    }
)
"""ErrorCode values interpreted as "another client holds the device".

PortAudio on Linux returns ``-9985 Device unavailable`` for the common
"PipeWire grabbed hw:X,Y" pathology. The opener classifies that as
``ErrorCode.DEVICE_BUSY``. ``DEVICE_DISAPPEARED`` covers the related
``-9988 Device disappeared`` and ``DEVICE_NOT_FOUND`` is included
because some kernel-invalidated states surface as ``-9996 Invalid
device`` when a session manager yanks the exclusive lock mid-open.
"""


def _is_session_manager_contention_pattern(
    *,
    platform: str,
    open_attempts: Sequence[OpenAttempt],
) -> bool:
    """Return ``True`` iff the attempt list matches "session manager holds hw".

    The rule is intentionally narrow — false positives would only
    swap a generic ``RuntimeError`` message for a slightly more useful
    one (no regression risk), but we still constrain the heuristic to
    (a) Linux only, (b) at least one attempt made, (c) every attempt
    falls in the contention-class :data:`_SESSION_MANAGER_CONTENTION_ERROR_CODES`.

    The ``attempts_tried_hw_and_virtual`` half of the ADR rule is
    handled upstream by the candidate-set: when this function fires,
    the opener already iterated the opener-side pyramid on the *current*
    candidate, and the cascade-level loop in
    :func:`~sovyx.voice.health.cascade.run_cascade_for_candidates` has
    exhausted every candidate (hardware + virtual). Re-checking here
    would require access to the cascade history, which the capture
    task legitimately does not have. Keeping the check at open-level
    is sound because the cascade only reaches ``start()`` on a device
    it already considered "best bet remaining" — a device-busy cluster
    at this stage implies every earlier candidate also failed.
    """
    if platform != "linux":
        return False
    if not open_attempts:
        return False
    return all(
        attempt.error_code is not None
        and attempt.error_code.value in _SESSION_MANAGER_CONTENTION_ERROR_CODES
        for attempt in open_attempts
    )


def _suggest_session_manager_alternatives() -> list[str]:
    """Return the UI-facing action tokens for a session-manager grab.

    Order: preferred alternative first. The dashboard maps each token
    to an i18n key + an action (chip click dispatches the corresponding
    fallback request). Currently static — future revisions may query
    enumeration to elide tokens for devices that don't exist on the
    host, but doing so here would introduce a sync ``sounddevice`` call
    on the error path.
    """
    return [
        "select_device:pipewire",
        "select_device:default",
        "select_device:pulse",
        "stop_process:pipewire",
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


class ExclusiveRestartVerdict(StrEnum):
    """Verdict of :meth:`AudioCaptureTask.request_exclusive_restart`.

    Pre-v0.20.2 the method returned ``None`` and always logged
    ``audio_capture_exclusive_restart_ok`` when the reopen succeeded —
    even when WASAPI silently handed back a shared-mode stream (e.g.
    the device was held by another exclusive-mode app, or Windows
    policy denied exclusive access). Callers could not distinguish
    "APO bypassed" from "APO still active, we just reopened the same
    deaf pipe". This enum makes the outcome inspectable:

    Members:
        EXCLUSIVE_ENGAGED: Stream reopened and WASAPI confirmed
            exclusive engagement (``info.exclusive_used=True``). The
            APO chain is bypassed — the user's mic is now reaching
            PortAudio untouched.
        DOWNGRADED_TO_SHARED: Stream reopened successfully, but
            ``info.exclusive_used=False``. WASAPI returned shared
            mode (the only combos that survived fallback were shared
            variants) — the APO chain is still in the signal path,
            so the deaf condition that triggered the bypass remains.
        OPEN_FAILED_SHARED_FALLBACK: The exclusive reopen raised a
            :class:`StreamOpenError` and the secondary shared-mode
            :meth:`_reopen_stream_after_device_error` recovered. The
            pipeline is alive but deaf (same as before the request).
        OPEN_FAILED_NO_STREAM: Both the exclusive reopen and the
            shared-mode fallback raised. The stream is closed *and*
            the consumer task has been signalled to exit
            (``_running=False`` + consumer cancelled) — ``_consume_loop``
            cannot self-recover from this state (it would be parked on
            ``queue.get()`` with no callback feeding it), so upstream
            supervisors MUST detect the dead state via the returned
            verdict and rebuild the task.
        NOT_RUNNING: Called while the task is stopped — no-op.
    """

    EXCLUSIVE_ENGAGED = "exclusive_engaged"
    DOWNGRADED_TO_SHARED = "downgraded_to_shared"
    OPEN_FAILED_SHARED_FALLBACK = "open_failed_shared_fallback"
    OPEN_FAILED_NO_STREAM = "open_failed_no_stream"
    NOT_RUNNING = "not_running"


class SharedRestartVerdict(StrEnum):
    """Verdict of :meth:`AudioCaptureTask.request_shared_restart`.

    Symmetric revert of :class:`ExclusiveRestartVerdict` — re-opens the
    stream in shared mode when the APO-bypass experiment needs to be
    rolled back (e.g. a strategy proved ineffective and the coordinator
    wants the pipeline returned to its pre-bypass configuration before
    trying the next strategy, or the user explicitly unpins exclusive
    mode in the wizard).

    Members:
        SHARED_ENGAGED: Stream reopened successfully in shared mode; the
            platform APO chain is back in the signal path. Equivalent to
            the pre-bypass state.
        OPEN_FAILED_NO_STREAM: The shared-mode reopen raised and no
            stream is live. The consumer task has been signalled to
            exit (``_running=False`` + consumer cancelled) because
            ``_consume_loop`` cannot self-recover from this state —
            it would be parked on ``queue.get()`` with no callback
            feeding it. Upstream supervisors MUST detect the dead
            state via the returned verdict and rebuild the capture
            task (or issue an explicit
            :meth:`request_exclusive_restart`).
        NOT_RUNNING: Called while the task is stopped — no-op.
    """

    SHARED_ENGAGED = "shared_engaged"
    OPEN_FAILED_NO_STREAM = "open_failed_no_stream"
    NOT_RUNNING = "not_running"


@dataclass(frozen=True, slots=True)
class ExclusiveRestartResult:
    """Structured outcome of :meth:`AudioCaptureTask.request_exclusive_restart`.

    Attributes:
        verdict: The :class:`ExclusiveRestartVerdict` describing what
            happened. Callers should treat anything other than
            :attr:`ExclusiveRestartVerdict.EXCLUSIVE_ENGAGED` as an
            unsuccessful bypass — the APO chain is still in place.
        engaged: Convenience flag — ``True`` iff
            ``verdict == EXCLUSIVE_ENGAGED``.
        host_api: Host API of the resulting stream (or ``None`` when
            :attr:`OPEN_FAILED_NO_STREAM` / :attr:`NOT_RUNNING`).
        device: Resolved PortAudio device index of the resulting
            stream.
        sample_rate: Effective sample rate of the resulting stream.
        detail: Human-readable error / downgrade reason for logs and
            the dashboard UI. ``None`` on a successful engagement.
    """

    verdict: ExclusiveRestartVerdict
    engaged: bool
    host_api: str | None = None
    device: int | str | None = None
    sample_rate: int | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class SharedRestartResult:
    """Structured outcome of :meth:`AudioCaptureTask.request_shared_restart`.

    Attributes:
        verdict: The :class:`SharedRestartVerdict` describing what
            happened. ``SHARED_ENGAGED`` means the revert worked and the
            pipeline is now running on the default shared-mode pipe;
            anything else means the stream is down.
        engaged: Convenience flag — ``True`` iff
            ``verdict == SHARED_ENGAGED``.
        host_api: Host API of the resulting stream (or ``None`` when
            :attr:`OPEN_FAILED_NO_STREAM` / :attr:`NOT_RUNNING`).
        device: Resolved PortAudio device index of the resulting stream.
        sample_rate: Effective sample rate of the resulting stream.
        detail: Human-readable error / downgrade reason for logs and
            the dashboard UI. ``None`` on a successful engagement.
    """

    verdict: SharedRestartVerdict
    engaged: bool
    host_api: str | None = None
    device: int | str | None = None
    sample_rate: int | None = None
    detail: str | None = None


class AlsaHwDirectRestartVerdict(StrEnum):
    """Verdict of :meth:`AudioCaptureTask.request_alsa_hw_direct_restart`.

    Linux-specific twin of :class:`ExclusiveRestartVerdict`. The
    ``LinuxPipeWireDirectBypass`` strategy requests this restart when it
    wants to bypass a misbehaving PipeWire/PulseAudio filter chain and
    talk to the kernel ALSA device directly. PortAudio's ``ALSA`` host
    API opens the device without traversing the session manager.

    Members:
        ALSA_HW_ENGAGED: Stream reopened and PortAudio confirmed the
            winning attempt targets the ``ALSA`` host API. The session
            manager is no longer in the signal path.
        DOWNGRADED_TO_SESSION_MANAGER: Stream reopened but the opener
            fell back to a sibling device that routes through
            PipeWire/PulseAudio (no ALSA-direct sibling survived). The
            session-manager chain is still in the signal path.
        NO_ALSA_SIBLING: Enumeration yielded no ``ALSA``-host-API
            sibling for the currently active device — some distros ship
            PortAudio builds with ALSA compiled out, or the ALSA device
            is held by another exclusive client. Existing stream
            preserved; no mutation occurred.
        OPEN_FAILED_NO_STREAM: Both the ALSA-direct open and the
            session-manager fallback raised. The stream is closed and
            the consumer task has been signalled to exit — upstream
            supervisors MUST rebuild the capture task.
        NOT_LINUX: Called on a non-Linux host — no-op, preserves the
            existing stream. Strategies must gate on
            ``platform_key == "linux"`` but the method is defensive.
        NOT_RUNNING: Called while the task is stopped — no-op.
    """

    ALSA_HW_ENGAGED = "alsa_hw_engaged"
    DOWNGRADED_TO_SESSION_MANAGER = "downgraded_to_session_manager"
    NO_ALSA_SIBLING = "no_alsa_sibling"
    OPEN_FAILED_NO_STREAM = "open_failed_no_stream"
    NOT_LINUX = "not_linux"
    NOT_RUNNING = "not_running"


class SessionManagerRestartVerdict(StrEnum):
    """Verdict of :meth:`AudioCaptureTask.request_session_manager_restart`.

    Linux-specific twin of :class:`SharedRestartVerdict`. Called by the
    ``LinuxPipeWireDirectBypass`` strategy during ``revert`` to return
    the stream to PipeWire/PulseAudio after an ALSA-direct experiment.

    Members:
        SESSION_MANAGER_ENGAGED: Stream reopened against a sibling
            device served by PulseAudio or PipeWire; the session
            manager is back in the signal path.
        DOWNGRADED_TO_ALSA_HW: Enumeration yielded no
            PulseAudio/PipeWire sibling — the device is only reachable
            via ALSA direct. The stream is alive but still bypasses the
            session manager (same state as before the request).
        OPEN_FAILED_NO_STREAM: The session-manager reopen raised and no
            stream is live. Consumer task signalled to exit; supervisor
            must rebuild.
        NOT_LINUX: Called on a non-Linux host — no-op.
        NOT_RUNNING: Called while the task is stopped — no-op.
    """

    SESSION_MANAGER_ENGAGED = "session_manager_engaged"
    DOWNGRADED_TO_ALSA_HW = "downgraded_to_alsa_hw"
    OPEN_FAILED_NO_STREAM = "open_failed_no_stream"
    NOT_LINUX = "not_linux"
    NOT_RUNNING = "not_running"


@dataclass(frozen=True, slots=True)
class AlsaHwDirectRestartResult:
    """Structured outcome of :meth:`AudioCaptureTask.request_alsa_hw_direct_restart`.

    Attributes:
        verdict: The :class:`AlsaHwDirectRestartVerdict` describing what
            happened. Callers should treat anything other than
            :attr:`AlsaHwDirectRestartVerdict.ALSA_HW_ENGAGED` as an
            unsuccessful bypass — the session-manager chain is still in
            place or the stream is gone.
        engaged: Convenience flag — ``True`` iff
            ``verdict == ALSA_HW_ENGAGED``.
        host_api: Host API of the resulting stream. ``"ALSA"`` on
            successful engagement; ``None`` when the stream is down.
        device: Resolved PortAudio device index of the resulting stream.
        sample_rate: Effective sample rate of the resulting stream.
        detail: Human-readable error / downgrade reason for logs and
            the dashboard UI. ``None`` on successful engagement.
    """

    verdict: AlsaHwDirectRestartVerdict
    engaged: bool
    host_api: str | None = None
    device: int | str | None = None
    sample_rate: int | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class SessionManagerRestartResult:
    """Structured outcome of :meth:`AudioCaptureTask.request_session_manager_restart`.

    Attributes:
        verdict: The :class:`SessionManagerRestartVerdict` describing
            what happened. ``SESSION_MANAGER_ENGAGED`` means the revert
            worked and the pipeline is now running through PipeWire or
            PulseAudio again.
        engaged: Convenience flag — ``True`` iff
            ``verdict == SESSION_MANAGER_ENGAGED``.
        host_api: Host API of the resulting stream.
        device: Resolved PortAudio device index of the resulting stream.
        sample_rate: Effective sample rate of the resulting stream.
        detail: Human-readable error / downgrade reason for logs and
            the dashboard UI. ``None`` on successful engagement.
    """

    verdict: SessionManagerRestartVerdict
    engaged: bool
    host_api: str | None = None
    device: int | str | None = None
    sample_rate: int | None = None
    detail: str | None = None


_LINUX_ALSA_HOST_API = "ALSA"
"""PortAudio's label for the direct-to-kernel ALSA host API on Linux."""


_LINUX_SESSION_MANAGER_HOST_APIS: frozenset[str] = frozenset({"PulseAudio", "PipeWire", "JACK"})
"""Host APIs that route through a Linux session manager.

A device served by any of these is considered non-direct for the
purpose of :meth:`request_alsa_hw_direct_restart` — the strategy wants
to route *around* these layers, not through them.
"""


def _emit_exclusive_restart_metric(result: ExclusiveRestartResult) -> None:
    """Record a ``voice.capture.exclusive_restart.verdicts`` counter event.

    Lazy-imports :mod:`sovyx.observability.metrics` so module-load in
    unit suites that swap the metrics provider still works. Failures
    in the metrics pipeline never bubble up to the caller — instead we
    log at DEBUG and continue, so an OTel exporter hiccup cannot break
    the capture task's reopen path.
    """
    try:
        import sys

        from sovyx.observability.metrics import get_metrics

        registry = get_metrics()
        counter = getattr(registry, "voice_capture_exclusive_restart_verdicts", None)
        if counter is None:
            return
        counter.add(
            1,
            attributes={
                "verdict": result.verdict.value,
                "host_api": result.host_api or "none",
                "platform": sys.platform,
            },
        )
    except Exception:  # noqa: BLE001 — metrics must never break capture
        logger.debug("voice_capture_exclusive_restart_metric_failed", exc_info=True)


def _emit_shared_restart_metric(result: SharedRestartResult) -> None:
    """Record a ``voice.capture.shared_restart.verdicts`` counter event.

    Symmetric twin of :func:`_emit_exclusive_restart_metric`; separate
    counter name so dashboards can distinguish engagements from reverts
    without parsing labels.
    """
    try:
        import sys

        from sovyx.observability.metrics import get_metrics

        registry = get_metrics()
        counter = getattr(registry, "voice_capture_shared_restart_verdicts", None)
        if counter is None:
            return
        counter.add(
            1,
            attributes={
                "verdict": result.verdict.value,
                "host_api": result.host_api or "none",
                "platform": sys.platform,
            },
        )
    except Exception:  # noqa: BLE001 — metrics must never break capture
        logger.debug("voice_capture_shared_restart_metric_failed", exc_info=True)


def _emit_alsa_hw_direct_restart_metric(result: AlsaHwDirectRestartResult) -> None:
    """Record a ``voice.capture.alsa_hw_direct_restart.verdicts`` counter event.

    Linux-specific twin of :func:`_emit_exclusive_restart_metric`. The
    counter is emitted regardless of whether the strategy engaged so
    dashboards can tell "Linux direct bypass never got a chance"
    (``NO_ALSA_SIBLING`` / ``NOT_LINUX``) apart from "direct bypass was
    tried and the session manager was bypassed" (``ALSA_HW_ENGAGED``)
    without scraping logs.
    """
    try:
        import sys

        from sovyx.observability.metrics import get_metrics

        registry = get_metrics()
        counter = getattr(registry, "voice_capture_alsa_hw_direct_restart_verdicts", None)
        if counter is None:
            return
        counter.add(
            1,
            attributes={
                "verdict": result.verdict.value,
                "host_api": result.host_api or "none",
                "platform": sys.platform,
            },
        )
    except Exception:  # noqa: BLE001 — metrics must never break capture
        logger.debug("voice_capture_alsa_hw_direct_restart_metric_failed", exc_info=True)


def _emit_session_manager_restart_metric(
    result: SessionManagerRestartResult,
) -> None:
    """Record a ``voice.capture.session_manager_restart.verdicts`` counter event.

    Symmetric twin of :func:`_emit_alsa_hw_direct_restart_metric` for
    the revert side of the Linux PipeWire-direct strategy.
    """
    try:
        import sys

        from sovyx.observability.metrics import get_metrics

        registry = get_metrics()
        counter = getattr(registry, "voice_capture_session_manager_restart_verdicts", None)
        if counter is None:
            return
        counter.add(
            1,
            attributes={
                "verdict": result.verdict.value,
                "host_api": result.host_api or "none",
                "platform": sys.platform,
            },
        )
    except Exception:  # noqa: BLE001 — metrics must never break capture
        logger.debug("voice_capture_session_manager_restart_metric_failed", exc_info=True)


def _rms_db_int16(frame: Any) -> float:  # noqa: ANN401 — numpy int16 array; Any keeps numpy lazy-imported
    """Compute dBFS RMS of an int16 buffer — safe for silent / empty buffers.

    Returns ``_RMS_FLOOR_DB`` for empty or all-zero frames to keep the
    output finite.
    """
    import numpy as np

    if frame is None or len(frame) == 0:
        return _RMS_FLOOR_DB
    # int16 max magnitude = 32767 — normalise to [-1, 1] to get dBFS.
    sample_sq = np.mean(np.square(frame.astype(np.float32) / 32768.0))
    if sample_sq <= 0 or not math.isfinite(float(sample_sq)):
        return _RMS_FLOOR_DB
    return float(10.0 * math.log10(float(sample_sq)))


class AudioCaptureTask:
    """Microphone → VoicePipeline bridge.

    Owns a ``sounddevice.InputStream`` running at 16 kHz / int16 /
    512-sample blocks — the exact frame shape the pipeline expects.
    Frames land on an asyncio queue via ``call_soon_threadsafe`` from
    the PortAudio thread and are drained by an async consumer task
    that calls ``pipeline.feed_frame`` for each one.

    Args:
        pipeline: The orchestrator to feed frames into.
        input_device: PortAudio device index/name. ``None`` uses the OS
            default input device.
        sample_rate: Capture rate in Hz. Only 16 kHz is supported by
            the downstream VAD / STT.
        blocksize: Samples per callback block. Must equal
            ``_FRAME_SAMPLES`` so each block is a whole pipeline frame.
        host_api_name: Host API label (``"Windows WASAPI"``, ``"MME"``, …)
            recorded for :meth:`status_snapshot` so ``/api/voice/status``
            can expose which variant is live.
        validate_on_start: When True (default), :meth:`start` samples the
            first ~600 ms of audio and raises :class:`CaptureSilenceError`
            if the peak RMS never crosses the noise floor. Tests can
            disable this to avoid racing PortAudio stubs.
    """

    def __init__(
        self,
        pipeline: VoicePipeline,
        *,
        input_device: int | str | None = None,
        sample_rate: int = _SAMPLE_RATE,
        blocksize: int = _FRAME_SAMPLES,
        host_api_name: str | None = None,
        validate_on_start: bool = True,
        tuning: VoiceTuningConfig | None = None,
        sd_module: Any | None = None,  # noqa: ANN401 — DI for tests
        enumerate_fn: Callable[[], list[DeviceEntry]] | None = None,
        endpoint_guid: str | None = None,
    ) -> None:
        self._pipeline = pipeline
        self._input_device = input_device
        self._sample_rate = sample_rate
        self._blocksize = blocksize
        self._host_api_name = host_api_name
        self._validate_on_start = validate_on_start
        self._tuning = tuning
        self._sd_module = sd_module
        self._enumerate_fn = enumerate_fn
        self._queue: asyncio.Queue[npt.NDArray[np.int16]] = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stream: Any = None
        self._consumer: asyncio.Task[None] | None = None
        self._running = False
        self._normalizer: FrameNormalizer | None = None
        self._resolved_device_name: str | None = None
        self._endpoint_guid: str = endpoint_guid or ""
        # Band-aid #10 replacement: per-task exponential backoff
        # schedule for the reconnect loop. Lazy-initialised on first
        # PortAudio error so the zero-error case has zero overhead.
        # Reset to attempt 0 on each successful reconnect so a
        # transient outage doesn't penalise future ones.
        self._reconnect_backoff: BackoffSchedule | None = None

        # Telemetry — populated by the consumer loop.
        self._last_rms_db: float = _RMS_FLOOR_DB
        self._frames_delivered: int = 0
        self._last_heartbeat_monotonic: float = 0.0
        self._frames_since_heartbeat: int = 0
        self._silent_frames_since_heartbeat: int = 0

        # Per-stream lifecycle counters — reset on every successful open
        # so ``audio.stream.closed`` reflects the activity of *that*
        # stream, not the cumulative life of the task.
        self._stream_id: str = ""
        self._stream_underruns: int = 0
        self._stream_overflows: int = 0
        self._stream_callback_frames: int = 0

        # Ring buffer — allocated lazily in :meth:`start` from the
        # per-instance tuning so tests that build the task without
        # calling start() don't pay the ~1 MB allocation cost.
        #
        # Thread-safety: writes happen inside ``_consume_loop`` between
        # ``await`` points and reads from :meth:`tap_recent_frames`
        # happen on the same event loop; the asyncio scheduler serialises
        # them without a lock as long as neither path awaits while
        # mutating the index fields. That invariant is asserted by the
        # unit tests — do not add awaits inside the critical sections.
        #
        # v1.3 §4.2 L4-B — ``_ring_state`` packs ``(epoch, samples_written)``
        # into a single int (layout in ``_RING_EPOCH_SHIFT`` / ``_RING_SAMPLES_MASK``)
        # so external readers via :meth:`samples_written_mark` observe a
        # consistent pair in one atomic ``LOAD_ATTR``. The bare
        # ``_ring_write_index`` remains separate because it is read only
        # by :meth:`tap_recent_frames` on the same event loop as the
        # writer — no cross-loop atomicity required.
        self._ring_buffer: npt.NDArray[np.int16] | None = None
        self._ring_capacity: int = 0
        self._ring_write_index: int = 0
        self._ring_state: int = 0

    # -- Properties -----------------------------------------------------------

    @property
    def is_running(self) -> bool:
        """Whether the capture task is active (stream open + consumer live)."""
        return self._running

    @property
    def input_device(self) -> int | str | None:
        """Selected PortAudio input device (``None`` = OS default)."""
        return self._input_device

    @property
    def input_device_name(self) -> str | None:
        """Resolved PortAudio device name for the active stream.

        Populated during :meth:`start` from the enumerated
        :class:`DeviceEntry`. Remains ``None`` until the stream opens
        successfully, so callers (dashboard diagnostics) can distinguish
        "not yet started" from "OS default" safely.
        """
        return self._resolved_device_name

    @property
    def active_device_name(self) -> str:
        """Non-nullable alias of :attr:`input_device_name`.

        Satisfies :class:`~sovyx.voice.health.contract.CaptureTaskProto`:
        the bypass coordinator + strategies work with a plain ``str``
        instead of ``str | None`` since they only run after the stream
        is open and always need a human-readable label for logs.
        Returns ``""`` during the pre-start window.
        """
        return self._resolved_device_name or ""

    @property
    def active_device_guid(self) -> str:
        """Stable endpoint GUID for the live capture stream — see ADR §1.

        Populated either from the explicit constructor ``endpoint_guid``
        argument or derived at :meth:`start` from the resolved
        :class:`DeviceEntry` via
        :func:`sovyx.voice.health._factory_integration.derive_endpoint_guid`.
        Returns ``""`` before the stream opens so callers can distinguish
        "not yet started" from "OS default" — the bypass coordinator
        will not call this pre-start anyway, but the Protocol requires a
        non-nullable string return.
        """
        return self._endpoint_guid

    @property
    def host_api_name(self) -> str | None:
        """Host API label for the opened stream (``None`` if unknown)."""
        return self._host_api_name

    @property
    def active_device_index(self) -> int:
        """PortAudio index of the open capture device; ``-1`` pre-start.

        Introduced by :mod:`voice-linux-cascade-root-fix` so runtime
        bypass strategies can address the exact numeric index the
        stream is currently bound to. ``-1`` is the structural
        sentinel for "not yet started" — no real PortAudio device
        ever takes that index.
        """
        if isinstance(self._input_device, int):
            return self._input_device
        return -1

    @property
    def active_device_kind(self) -> str:
        """Best-effort semantic kind of the active device.

        Returns the :class:`~sovyx.voice.device_enum.DeviceKind` value
        for the current ``active_device_name`` when enumeration
        succeeds; ``"unknown"`` otherwise. Used by the
        :class:`LinuxSessionManagerEscapeBypass` eligibility probe to
        tell a hardware node from a session-manager virtual.
        Never raises.
        """
        if not self._running:
            return "unknown"
        try:
            from sovyx.voice.device_enum import classify_device_kind

            return classify_device_kind(
                name=self._resolved_device_name or "",
                host_api_name=self._host_api_name or "",
                platform_key=sys.platform,
            ).value
        except Exception:  # noqa: BLE001 — classifier must never fail an apply path
            return "unknown"

    @property
    def last_rms_db(self) -> float:
        """Most recent per-frame RMS in dBFS (updated by consumer loop)."""
        return self._last_rms_db

    @property
    def frames_delivered(self) -> int:
        """Total frames fed to the pipeline since :meth:`start`."""
        return self._frames_delivered

    def status_snapshot(self) -> dict[str, Any]:
        """Compact dict for ``/api/voice/status`` — no async, no locks."""
        return {
            "running": self._running,
            "input_device": self._input_device,
            "host_api": self._host_api_name,
            "sample_rate": self._sample_rate,
            "frames_delivered": self._frames_delivered,
            "last_rms_db": round(self._last_rms_db, 1),
        }

    def apply_mic_ducking_db(self, gain_db: float) -> None:
        """Forward a self-feedback duck gain target to the normalizer.

        Thin adapter invoked by
        :class:`~sovyx.voice.health.SelfFeedbackGate` when TTS starts /
        ends (ADR §4.4.6.b). Before the capture stream opens, the
        normalizer is ``None`` — in that window the call is silently
        dropped: the gate will re-engage on the next utterance once
        the normalizer exists, which matches the ducking contract
        (attenuation is per-TTS-session, not persistent state).

        Args:
            gain_db: Target attenuation in dB. Must be ``<= 0``. The
                underlying :class:`FrameNormalizer` raises ``ValueError``
                on positive gains; we propagate that up so a programming
                error surfaces during testing, not silently in prod.
        """
        normalizer = self._normalizer
        if normalizer is None:
            return
        normalizer.set_ducking_gain_db(gain_db)

    # -- Lifecycle ------------------------------------------------------------

    async def start(self) -> None:
        """Open the input stream, validate it, and spawn the consumer task.

        Delegates stream construction to
        :func:`sovyx.voice._stream_opener.open_input_stream`, which walks
        the full host-API × auto_convert × channels × rate pyramid and
        optionally validates each opened stream for silence via
        ``validate_fn``. When every viable variant delivers only zeros,
        :class:`CaptureSilenceError` is raised so callers (notably
        :func:`sovyx.voice.factory.create_voice_pipeline`) can surface a
        precise error payload to the UI.

        Idempotent — a second call while running is a no-op.

        Raises:
            CaptureSilenceError: Every pyramid variant opened cleanly
                but delivered only silence.
            RuntimeError: Every pyramid variant failed with a
                non-silence PortAudio error (device busy, permission,
                AUDCLNT_E_*). ``.code`` carries the classified
                :class:`ErrorCode`.
        """
        if self._running:
            return

        from sovyx.voice._stream_opener import StreamOpenError, open_input_stream

        self._loop = asyncio.get_running_loop()
        tuning = self._tuning if self._tuning is not None else _VoiceTuning()
        entry = _resolve_input_entry(
            input_device=self._input_device,
            enumerate_fn=self._enumerate_fn,
            host_api_name=self._host_api_name,
        )
        validate_fn = self._validate_stream_from_queue if self._validate_on_start else None

        try:
            stream, info = await open_input_stream(
                device=entry,
                target_rate=self._sample_rate,
                blocksize=self._blocksize,
                callback=self._audio_callback,
                tuning=tuning,
                sd_module=self._sd_module,
                enumerate_fn=self._enumerate_fn,
                validate_fn=validate_fn,
            )
        except StreamOpenError as exc:
            self._raise_classified_open_error(exc, entry)

        self._stream = stream
        self._sample_rate = info.sample_rate
        self._input_device = info.device_index
        self._host_api_name = info.host_api
        self._resolved_device_name = entry.name if entry is not None else None
        self._ensure_endpoint_guid(entry)
        self._allocate_ring_buffer(tuning)

        # F5/F6: AGC2 default-on per VoiceTuningConfig.agc2_enabled
        # (commit 2e36893). Operators can revert via
        # SOVYX_TUNING__VOICE__AGC2_ENABLED=false. The factory
        # returns None when disabled — FrameNormalizer accepts None
        # as the no-op default so the call site needs no ``if`` branch.
        _agc2_tuning = self._tuning if self._tuning is not None else _VoiceTuning()
        self._normalizer = FrameNormalizer(
            source_rate=info.sample_rate,
            source_channels=info.channels,
            agc2=build_agc2_if_enabled(
                enabled=_agc2_tuning.agc2_enabled,
                sample_rate=info.sample_rate,
            ),
        )
        if not self._normalizer.is_passthrough:
            logger.info(
                "audio_capture_resample_active",
                source_rate=info.sample_rate,
                source_channels=info.channels,
                target_rate=self._normalizer.target_rate,
                target_window=self._normalizer.target_window,
            )

        self._running = True
        self._last_heartbeat_monotonic = time.monotonic()
        self._consumer = spawn(self._consume_loop(), name="audio-capture-consumer")
        self._emit_stream_opened(info, apo_bypass_attempted=False)
        logger.info(
            "audio_capture_task_started",
            device=self._input_device if self._input_device is not None else "default",
            host_api=self._host_api_name,
            sample_rate=self._sample_rate,
            channels=info.channels,
            auto_convert=info.auto_convert_used,
            fallback_depth=info.fallback_depth,
            blocksize=self._blocksize,
            normalizer_active=not self._normalizer.is_passthrough,
        )

    async def _validate_stream_from_queue(
        self,
        _stream: Any,  # noqa: ANN401 — provided by opener, not used here
        *,
        device_index: int,  # noqa: ARG002
    ) -> float:
        """Drain ``_VALIDATION_S`` seconds of callback output and return peak dBFS.

        Two validation modes (controlled by
        :attr:`VoiceTuningConfig.capture_validation_require_signal`):

        * **Presence-only (default)**: accepts as soon as
          :attr:`~VoiceTuningConfig.capture_validation_min_frames` frames
          have arrived. Returns ``0.0`` dBFS (well above any threshold)
          so the opener treats the variant as valid regardless of
          ambient signal level. This is the right default for production
          capture: a silent user shouldn't invalidate a perfectly good
          audio path.
        * **Signal-gated (opt-in)**: measures peak RMS and requires it
          to cross ``capture_validation_min_rms_db``. Reserved for the
          setup-wizard and explicit diagnostic flows where the user is
          actively making noise.

        The queue is drained first so stale frames from a previously
        rejected pyramid variant do not leak into the current measurement.
        """
        return await self._validate_stream()

    def _raise_classified_open_error(
        self,
        exc: Any,  # noqa: ANN401 — StreamOpenError, typed lazily
        entry: DeviceEntry,
    ) -> None:
        """Map a :class:`StreamOpenError` to the public exception API.

        Classification order (most specific first):

        1. **All silent** → :class:`CaptureSilenceError`. Every variant
           opened but delivered ≤ validation-RMS audio; the wizard UX
           handles this distinctly (the mic is open but nobody's home).
        2. **Session-manager contention (Linux)** →
           :class:`CaptureDeviceContendedError`. Every variant failed
           with a contention-class error code on Linux AND at least
           one candidate was tried — the strong signal that another
           audio client is holding ``hw:X,Y``. Carries
           ``suggested_actions`` so the dashboard renders actionable
           chips. Introduced by ``voice-linux-cascade-root-fix`` T7.
        3. **Default** → generic :class:`RuntimeError` with ``.code``
           + ``.attempts`` attached for operator debugging. Preserves
           the pre-T7 behaviour for patterns we don't recognise.
        """
        attempts = list(getattr(exc, "attempts", []))
        all_silent = bool(attempts) and all(
            "silent stream" in (a.error_detail or "") for a in attempts
        )
        if all_silent:
            worst = min(
                (
                    _extract_peak_db(a.error_detail)
                    for a in attempts
                    if "silent stream" in (a.error_detail or "")
                ),
                default=_RMS_FLOOR_DB,
            )
            msg = (
                f"Input stream opened on device={entry.index!r} "
                f"(host_api={entry.host_api_name!r}) but every variant delivered only silence "
                f"(peak RMS {worst:.1f} dBFS < threshold {_VALIDATION_MIN_RMS_DB:.1f} dBFS)."
            )
            raise CaptureSilenceError(
                msg,
                device=entry.index,
                host_api=entry.host_api_name,
                observed_peak_rms_db=worst,
            ) from exc

        # T7 — session-manager contention (Linux). See
        # :func:`_is_session_manager_contention_pattern` for the rule.
        if _is_session_manager_contention_pattern(
            platform=sys.platform,
            open_attempts=attempts,
        ):
            suggested = _suggest_session_manager_alternatives()
            msg = (
                f"Every attempt on device={entry.index!r} "
                f"(host_api={entry.host_api_name!r}) failed with a device-busy error "
                "— another audio client (likely PipeWire or PulseAudio) is holding this "
                "device. Try selecting the 'pipewire' or 'default' PCM instead."
            )
            logger.error(
                "audio_capture_device_contended",
                device=entry.index,
                host_api=entry.host_api_name,
                suggested_actions=suggested,
                attempt_count=len(attempts),
            )
            raise CaptureDeviceContendedError(
                msg,
                device=entry.index,
                host_api=entry.host_api_name,
                suggested_actions=suggested,
                attempts=attempts,
            ) from exc

        runtime = RuntimeError(str(exc))
        runtime.code = getattr(exc, "code", None)  # type: ignore[attr-defined]
        runtime.attempts = attempts  # type: ignore[attr-defined]
        raise runtime from exc

    async def stop(self) -> None:
        """Cancel the consumer task and close the stream."""
        if not self._running:
            return
        self._running = False
        if self._consumer is not None:
            self._consumer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._consumer
            self._consumer = None
        await asyncio.to_thread(self._close_stream, "shutdown")
        # Drop any in-flight frames — they are stale once stopped.
        while not self._queue.empty():
            self._queue.get_nowait()
        logger.info("audio_capture_task_stopped")

    def _signal_consumer_shutdown(self) -> None:
        """Mark the task dead and wake the consumer so it can exit.

        Used by the terminal ``OPEN_FAILED_NO_STREAM`` branches of
        :meth:`request_exclusive_restart` and
        :meth:`request_shared_restart` — after both open paths have
        failed, the stream is ``None`` and the consume loop would
        otherwise stay parked on ``queue.get()`` forever (nothing can
        enqueue, and the ``sd.PortAudioError`` reconnect branch cannot
        fire without a live stream). Flipping ``_running`` + cancelling
        the consumer task unblocks it and lets upstream supervisors
        detect the dead state by observing the task's completion and
        the returned verdict.

        Safe to call from outside the consumer task (e.g. the
        coordinator's bypass ``apply``/``revert`` path). Idempotent —
        a second invocation after the consumer is already done is a
        no-op.
        """
        self._running = False
        consumer = self._consumer
        if consumer is not None and not consumer.done():
            consumer.cancel()

    async def _validate_stream(self) -> float:
        """Observe the freshly-opened stream for up to ``_VALIDATION_S`` seconds.

        Drains any residual frames left over from a previous pyramid
        variant, then observes the fresh callback for up to
        ``capture_validation_seconds``. Behaviour branches on
        :attr:`VoiceTuningConfig.capture_validation_require_signal`:

        * When ``False`` (default): returns ``0.0`` dBFS as soon as
          :attr:`~VoiceTuningConfig.capture_validation_min_frames` frames
          have arrived — proving the PortAudio callback is live without
          demanding the user speak. If the stream is truly dead (callback
          never fires), the deadline expires and the floor value is
          returned, which trips the opener's silence fallback.
        * When ``True``: measures the peak per-frame RMS and short-circuits
          the moment it crosses ``capture_validation_min_rms_db``. Retains
          the legacy diagnostic semantics used by the setup-wizard.
        """
        # Drain stale frames from any previously rejected variant — the
        # queue is shared across pyramid iterations.
        while not self._queue.empty():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()

        tuning = self._tuning if self._tuning is not None else _VoiceTuning()
        require_signal = tuning.capture_validation_require_signal
        min_frames = max(1, tuning.capture_validation_min_frames)
        min_rms_db = tuning.capture_validation_min_rms_db
        deadline = time.monotonic() + tuning.capture_validation_seconds

        peak_db = _RMS_FLOOR_DB
        frames_seen = 0
        while time.monotonic() < deadline:
            timeout = max(deadline - time.monotonic(), 0.05)
            try:
                frame = await asyncio.wait_for(self._queue.get(), timeout=timeout)
            except TimeoutError:
                break
            frames_seen += 1
            db = _rms_db_int16(frame)
            peak_db = max(peak_db, db)
            if require_signal:
                if peak_db >= min_rms_db:
                    return peak_db
            elif frames_seen >= min_frames:
                # Callback is alive — return a value far above any threshold
                # so the opener accepts this variant irrespective of the
                # ambient signal level.
                return 0.0
        return peak_db

    # -- Internals ------------------------------------------------------------

    async def request_exclusive_restart(self) -> ExclusiveRestartResult:
        """Re-open the capture stream in WASAPI exclusive mode.

        Called by the orchestrator when it decides that a capture-side
        APO (Windows Voice Clarity / VocaEffectPack) is destroying the
        microphone signal — exclusive mode bypasses the entire APO chain
        by talking to the IAudioClient directly. The current stream is
        torn down first; on failure the method logs and returns without
        raising so a single heartbeat loop iteration does not crash the
        pipeline.

        Idempotent — safe to call while stopped; in that case it is a
        no-op. The orchestrator already latches the request so the
        callback fires at most once per session.

        Returns:
            An :class:`ExclusiveRestartResult` describing whether
            exclusive mode was actually engaged. v0.20.2 / Bug C —
            pre-v0.20.2 this method returned ``None`` and logged
            success whenever the reopen succeeded, even when WASAPI
            fell back to shared mode (APO still in the signal path).
            Callers now inspect ``result.engaged`` to distinguish a
            real APO bypass from a cosmetic restart.
        """
        if not self._running:
            logger.debug("audio_capture_exclusive_restart_skipped_not_running")
            result = ExclusiveRestartResult(
                verdict=ExclusiveRestartVerdict.NOT_RUNNING,
                engaged=False,
                detail="capture task is not running",
            )
            _emit_exclusive_restart_metric(result)
            return result

        from sovyx.voice._stream_opener import StreamOpenError, open_input_stream

        base_tuning = self._tuning if self._tuning is not None else _VoiceTuning()
        exclusive_tuning = base_tuning.model_copy(update={"capture_wasapi_exclusive": True})
        entry = _resolve_input_entry(
            input_device=self._input_device,
            enumerate_fn=self._enumerate_fn,
            host_api_name=self._host_api_name,
        )
        logger.warning(
            "audio_capture_exclusive_restart_begin",
            device=self._input_device,
            host_api=self._host_api_name,
        )

        # Tear down the existing stream on the PortAudio thread before
        # we try to grab the device exclusively — otherwise WASAPI
        # returns AUDCLNT_E_EXCLUSIVE_MODE_NOT_ALLOWED on our own stream.
        await asyncio.to_thread(self._close_stream, "exclusive_restart")
        # Clear any residual frames from the shared-mode callback.
        while not self._queue.empty():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()

        try:
            stream, info = await open_input_stream(
                device=entry,
                target_rate=self._sample_rate,
                blocksize=self._blocksize,
                callback=self._audio_callback,
                tuning=exclusive_tuning,
                sd_module=self._sd_module,
                enumerate_fn=self._enumerate_fn,
                validate_fn=None,
            )
        except StreamOpenError as exc:
            logger.error(
                "audio_capture_exclusive_restart_failed",
                error=str(exc),
                device=self._input_device,
                host_api=self._host_api_name,
            )
            # Fall back to shared mode so the pipeline keeps running
            # (deaf, but alive — the dashboard banner will still guide
            # the user through the manual APO-disable path).
            try:
                await self._reopen_stream_after_device_error()
            except Exception as fallback_exc:  # noqa: BLE001
                logger.error(
                    "audio_capture_exclusive_fallback_failed",
                    error=str(fallback_exc),
                )
                # Stream is gone and no recovery path inside the task
                # can resurrect it — unblock the consume loop so the
                # supervisor sees a completed consumer and rebuilds.
                self._signal_consumer_shutdown()
                result = ExclusiveRestartResult(
                    verdict=ExclusiveRestartVerdict.OPEN_FAILED_NO_STREAM,
                    engaged=False,
                    host_api=self._host_api_name,
                    device=self._input_device,
                    detail=(
                        f"exclusive open failed ({exc}); shared fallback "
                        f"also failed ({fallback_exc})"
                    ),
                )
                _emit_exclusive_restart_metric(result)
                return result
            result = ExclusiveRestartResult(
                verdict=ExclusiveRestartVerdict.OPEN_FAILED_SHARED_FALLBACK,
                engaged=False,
                host_api=self._host_api_name,
                device=self._input_device,
                sample_rate=self._sample_rate,
                detail=(f"exclusive open failed ({exc}); recovered into shared mode"),
            )
            _emit_exclusive_restart_metric(result)
            return result

        self._stream = stream
        self._sample_rate = info.sample_rate
        self._input_device = info.device_index
        self._host_api_name = info.host_api
        if entry is not None:
            self._resolved_device_name = entry.name
        # F5/F6: AGC2 default-on per VoiceTuningConfig.agc2_enabled
        # (commit 2e36893). Operators can revert via
        # SOVYX_TUNING__VOICE__AGC2_ENABLED=false. The factory
        # returns None when disabled — FrameNormalizer accepts None
        # as the no-op default so the call site needs no ``if`` branch.
        _agc2_tuning = self._tuning if self._tuning is not None else _VoiceTuning()
        self._normalizer = FrameNormalizer(
            source_rate=info.sample_rate,
            source_channels=info.channels,
            agc2=build_agc2_if_enabled(
                enabled=_agc2_tuning.agc2_enabled,
                sample_rate=info.sample_rate,
            ),
        )
        # Reset the ring buffer so the bypass coordinator's post-apply
        # integrity probe only sees frames from the reopened stream.
        self._allocate_ring_buffer(exclusive_tuning)
        self._emit_stream_opened(info, apo_bypass_attempted=True)
        # v0.20.2 / Bug C — an opener that couldn't honour exclusive
        # (device busy, policy denied, old PortAudio) falls through to
        # shared variants of the same combo and returns a stream with
        # ``exclusive_used=False``. The pipeline is alive but the APO
        # chain is still in the signal path — the deaf condition that
        # triggered the request is unchanged. Distinguish this from a
        # real engagement so the dashboard / orchestrator / user know
        # the bypass did not take.
        if not info.exclusive_used:
            logger.error(
                "audio_capture_exclusive_restart_downgraded_to_shared",
                device=self._input_device,
                host_api=self._host_api_name,
                sample_rate=self._sample_rate,
                channels=info.channels,
            )
            result = ExclusiveRestartResult(
                verdict=ExclusiveRestartVerdict.DOWNGRADED_TO_SHARED,
                engaged=False,
                host_api=self._host_api_name,
                device=self._input_device,
                sample_rate=self._sample_rate,
                detail=(
                    "WASAPI granted shared mode instead of exclusive — APO "
                    "chain still in signal path. Another app may hold the "
                    "device exclusively or Windows policy denied exclusive "
                    "access."
                ),
            )
            _emit_exclusive_restart_metric(result)
            return result
        logger.warning(
            "audio_capture_exclusive_restart_ok",
            device=self._input_device,
            host_api=self._host_api_name,
            sample_rate=self._sample_rate,
            channels=info.channels,
            exclusive_used=info.exclusive_used,
        )
        result = ExclusiveRestartResult(
            verdict=ExclusiveRestartVerdict.EXCLUSIVE_ENGAGED,
            engaged=True,
            host_api=self._host_api_name,
            device=self._input_device,
            sample_rate=self._sample_rate,
        )
        _emit_exclusive_restart_metric(result)
        return result

    async def _reopen_stream_after_device_error(self) -> None:
        """Reopen the stream after a ``sd.PortAudioError`` in the consume loop.

        Uses the same unified opener as :meth:`start` so reconnect after
        a USB-headset yank inherits host-API × auto_convert × channels
        fallback automatically.
        """
        from sovyx.voice._stream_opener import StreamOpenError, open_input_stream

        tuning = self._tuning if self._tuning is not None else _VoiceTuning()
        entry = _resolve_input_entry(
            input_device=self._input_device,
            enumerate_fn=self._enumerate_fn,
            host_api_name=self._host_api_name,
        )
        try:
            stream, info = await open_input_stream(
                device=entry,
                target_rate=self._sample_rate,
                blocksize=self._blocksize,
                callback=self._audio_callback,
                tuning=tuning,
                sd_module=self._sd_module,
                enumerate_fn=self._enumerate_fn,
                validate_fn=None,  # reconnect path skips validation
            )
        except StreamOpenError as exc:
            raise RuntimeError(str(exc)) from exc
        self._stream = stream
        self._sample_rate = info.sample_rate
        self._input_device = info.device_index
        self._host_api_name = info.host_api
        if entry is not None:
            self._resolved_device_name = entry.name
        # F5/F6: AGC2 default-on per VoiceTuningConfig.agc2_enabled
        # (commit 2e36893). Operators can revert via
        # SOVYX_TUNING__VOICE__AGC2_ENABLED=false. The factory
        # returns None when disabled — FrameNormalizer accepts None
        # as the no-op default so the call site needs no ``if`` branch.
        _agc2_tuning = self._tuning if self._tuning is not None else _VoiceTuning()
        self._normalizer = FrameNormalizer(
            source_rate=info.sample_rate,
            source_channels=info.channels,
            agc2=build_agc2_if_enabled(
                enabled=_agc2_tuning.agc2_enabled,
                sample_rate=info.sample_rate,
            ),
        )
        # Reset the ring buffer — stale frames from the pre-error stream
        # would mislead any integrity probe issued immediately after the
        # reconnect.
        self._allocate_ring_buffer(tuning)
        self._emit_stream_opened(info, apo_bypass_attempted=False)

    async def request_shared_restart(self) -> SharedRestartResult:
        """Revert the capture stream to shared mode.

        Symmetric twin of :meth:`request_exclusive_restart` — re-opens
        the device with ``capture_wasapi_exclusive=False`` so a failed
        APO-bypass experiment (or an explicit user unpin) restores the
        pre-bypass state. Used by
        :class:`sovyx.voice.health.capture_integrity.CaptureIntegrityCoordinator`
        when a strategy evaluated STILL_DEAD or when a later strategy
        superseded an earlier one.

        Idempotent — safe to call while stopped; in that case it is a
        no-op. All metric + log semantics mirror the exclusive path so
        dashboards can correlate engagements and reverts one-to-one.

        Returns:
            A :class:`SharedRestartResult` describing the outcome. A
            non-``SHARED_ENGAGED`` verdict means the pipeline has no
            active capture until the next reconnect cycle or explicit
            restart.
        """
        if not self._running:
            logger.debug("audio_capture_shared_restart_skipped_not_running")
            result = SharedRestartResult(
                verdict=SharedRestartVerdict.NOT_RUNNING,
                engaged=False,
                detail="capture task is not running",
            )
            _emit_shared_restart_metric(result)
            return result

        from sovyx.voice._stream_opener import StreamOpenError, open_input_stream

        base_tuning = self._tuning if self._tuning is not None else _VoiceTuning()
        shared_tuning = base_tuning.model_copy(update={"capture_wasapi_exclusive": False})
        entry = _resolve_input_entry(
            input_device=self._input_device,
            enumerate_fn=self._enumerate_fn,
            host_api_name=self._host_api_name,
        )
        logger.warning(
            "audio_capture_shared_restart_begin",
            device=self._input_device,
            host_api=self._host_api_name,
        )

        # Mirror request_exclusive_restart — tear down the existing
        # stream on the PortAudio thread so the shared reopen does not
        # race against our own exclusive handle.
        await asyncio.to_thread(self._close_stream, "shared_restart")
        while not self._queue.empty():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()

        try:
            stream, info = await open_input_stream(
                device=entry,
                target_rate=self._sample_rate,
                blocksize=self._blocksize,
                callback=self._audio_callback,
                tuning=shared_tuning,
                sd_module=self._sd_module,
                enumerate_fn=self._enumerate_fn,
                validate_fn=None,
            )
        except StreamOpenError as exc:
            logger.error(
                "audio_capture_shared_restart_failed",
                error=str(exc),
                device=self._input_device,
                host_api=self._host_api_name,
            )
            # Stream is gone and no recovery path inside the task can
            # resurrect it (no callback → no frames → no PortAudioError
            # → consume loop parked on queue.get). Unblock the loop so
            # the supervisor sees a completed consumer and rebuilds.
            self._signal_consumer_shutdown()
            result = SharedRestartResult(
                verdict=SharedRestartVerdict.OPEN_FAILED_NO_STREAM,
                engaged=False,
                host_api=self._host_api_name,
                device=self._input_device,
                detail=f"shared reopen failed: {exc}",
            )
            _emit_shared_restart_metric(result)
            return result

        self._stream = stream
        self._sample_rate = info.sample_rate
        self._input_device = info.device_index
        self._host_api_name = info.host_api
        if entry is not None:
            self._resolved_device_name = entry.name
        # F5/F6: AGC2 default-on per VoiceTuningConfig.agc2_enabled
        # (commit 2e36893). Operators can revert via
        # SOVYX_TUNING__VOICE__AGC2_ENABLED=false. The factory
        # returns None when disabled — FrameNormalizer accepts None
        # as the no-op default so the call site needs no ``if`` branch.
        _agc2_tuning = self._tuning if self._tuning is not None else _VoiceTuning()
        self._normalizer = FrameNormalizer(
            source_rate=info.sample_rate,
            source_channels=info.channels,
            agc2=build_agc2_if_enabled(
                enabled=_agc2_tuning.agc2_enabled,
                sample_rate=info.sample_rate,
            ),
        )
        self._allocate_ring_buffer(shared_tuning)
        self._emit_stream_opened(info, apo_bypass_attempted=False)
        logger.warning(
            "audio_capture_shared_restart_ok",
            device=self._input_device,
            host_api=self._host_api_name,
            sample_rate=self._sample_rate,
            channels=info.channels,
        )
        result = SharedRestartResult(
            verdict=SharedRestartVerdict.SHARED_ENGAGED,
            engaged=True,
            host_api=self._host_api_name,
            device=self._input_device,
            sample_rate=self._sample_rate,
        )
        _emit_shared_restart_metric(result)
        return result

    async def request_alsa_hw_direct_restart(self) -> AlsaHwDirectRestartResult:
        """Reopen the capture stream against the ALSA-direct sibling device.

        Linux-specific twin of :meth:`request_exclusive_restart`. The
        ``LinuxPipeWireDirectBypass`` strategy invokes this when it
        wants to bypass a misbehaving PipeWire/PulseAudio filter chain
        (e.g. ``module-echo-cancel``, ``rnnoise`` filter, user-added
        EQ) and talk to the kernel ALSA device directly.

        Resolution: re-enumerate input devices, locate the sibling whose
        :attr:`DeviceEntry.canonical_name` matches the current endpoint
        AND whose :attr:`DeviceEntry.host_api_name` equals ``"ALSA"``.
        When found, that entry is handed to the unified opener as the
        starting point — the opener's sibling-chain fallback then
        automatically covers the "ALSA open refused, fall back to
        PulseAudio" path.

        Idempotent — safe to call while stopped or on a non-Linux host;
        in either case it is a no-op and the existing stream (if any)
        is preserved.

        Returns:
            An :class:`AlsaHwDirectRestartResult`. Callers inspect
            ``result.engaged`` (``True`` iff the ALSA host API actually
            won the fallback pyramid) to know whether the PipeWire
            bypass is in effect.
        """
        if not self._running:
            logger.debug("audio_capture_alsa_hw_direct_restart_skipped_not_running")
            result = AlsaHwDirectRestartResult(
                verdict=AlsaHwDirectRestartVerdict.NOT_RUNNING,
                engaged=False,
                detail="capture task is not running",
            )
            _emit_alsa_hw_direct_restart_metric(result)
            return result
        if sys.platform != "linux":
            logger.debug(
                "audio_capture_alsa_hw_direct_restart_skipped_not_linux",
                platform=sys.platform,
            )
            result = AlsaHwDirectRestartResult(
                verdict=AlsaHwDirectRestartVerdict.NOT_LINUX,
                engaged=False,
                host_api=self._host_api_name,
                device=self._input_device,
                sample_rate=self._sample_rate,
                detail=f"request_alsa_hw_direct_restart is Linux-only; running on {sys.platform}",
            )
            _emit_alsa_hw_direct_restart_metric(result)
            return result

        alsa_entry = self._find_sibling_with_host_api(_LINUX_ALSA_HOST_API)
        if alsa_entry is None:
            logger.warning(
                "audio_capture_alsa_hw_direct_restart_no_sibling",
                device=self._input_device,
                host_api=self._host_api_name,
            )
            result = AlsaHwDirectRestartResult(
                verdict=AlsaHwDirectRestartVerdict.NO_ALSA_SIBLING,
                engaged=False,
                host_api=self._host_api_name,
                device=self._input_device,
                sample_rate=self._sample_rate,
                detail=(
                    "no ALSA-host-API sibling found for current endpoint "
                    "(PortAudio build without ALSA, or device held exclusive)"
                ),
            )
            _emit_alsa_hw_direct_restart_metric(result)
            return result

        from sovyx.voice._stream_opener import StreamOpenError, open_input_stream

        tuning = self._tuning if self._tuning is not None else _VoiceTuning()
        logger.warning(
            "audio_capture_alsa_hw_direct_restart_begin",
            device=self._input_device,
            host_api=self._host_api_name,
            target_host_api=_LINUX_ALSA_HOST_API,
            target_device_index=alsa_entry.index,
        )

        # Tear down the existing (session-manager-backed) stream before
        # we grab the kernel device — some ALSA drivers reject a second
        # client even for read-only capture.
        await asyncio.to_thread(self._close_stream, "alsa_hw_direct_restart")
        while not self._queue.empty():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()

        try:
            stream, info = await open_input_stream(
                device=alsa_entry,
                target_rate=self._sample_rate,
                blocksize=self._blocksize,
                callback=self._audio_callback,
                tuning=tuning,
                sd_module=self._sd_module,
                enumerate_fn=self._enumerate_fn,
                validate_fn=None,
            )
        except StreamOpenError as exc:
            logger.error(
                "audio_capture_alsa_hw_direct_restart_failed",
                error=str(exc),
                device=self._input_device,
                host_api=self._host_api_name,
            )
            # Mirror the exclusive-fallback behaviour: try to recover
            # the pipeline through shared mode so the user is not left
            # with a dead stream.
            try:
                await self._reopen_stream_after_device_error()
            except Exception as fallback_exc:  # noqa: BLE001
                logger.error(
                    "audio_capture_alsa_hw_direct_fallback_failed",
                    error=str(fallback_exc),
                )
                self._signal_consumer_shutdown()
                result = AlsaHwDirectRestartResult(
                    verdict=AlsaHwDirectRestartVerdict.OPEN_FAILED_NO_STREAM,
                    engaged=False,
                    host_api=self._host_api_name,
                    device=self._input_device,
                    detail=(
                        f"ALSA-direct open failed ({exc}); session-manager "
                        f"fallback also failed ({fallback_exc})"
                    ),
                )
                _emit_alsa_hw_direct_restart_metric(result)
                return result
            result = AlsaHwDirectRestartResult(
                verdict=AlsaHwDirectRestartVerdict.DOWNGRADED_TO_SESSION_MANAGER,
                engaged=False,
                host_api=self._host_api_name,
                device=self._input_device,
                sample_rate=self._sample_rate,
                detail=f"ALSA-direct open failed ({exc}); recovered via session manager",
            )
            _emit_alsa_hw_direct_restart_metric(result)
            return result

        self._stream = stream
        self._sample_rate = info.sample_rate
        self._input_device = info.device_index
        self._host_api_name = info.host_api
        self._resolved_device_name = alsa_entry.name
        # F5/F6: AGC2 default-on per VoiceTuningConfig.agc2_enabled
        # (commit 2e36893). Operators can revert via
        # SOVYX_TUNING__VOICE__AGC2_ENABLED=false. The factory
        # returns None when disabled — FrameNormalizer accepts None
        # as the no-op default so the call site needs no ``if`` branch.
        _agc2_tuning = self._tuning if self._tuning is not None else _VoiceTuning()
        self._normalizer = FrameNormalizer(
            source_rate=info.sample_rate,
            source_channels=info.channels,
            agc2=build_agc2_if_enabled(
                enabled=_agc2_tuning.agc2_enabled,
                sample_rate=info.sample_rate,
            ),
        )
        self._allocate_ring_buffer(tuning)
        self._emit_stream_opened(info, apo_bypass_attempted=True)

        if info.host_api != _LINUX_ALSA_HOST_API:
            logger.error(
                "audio_capture_alsa_hw_direct_restart_downgraded_to_session_manager",
                device=self._input_device,
                host_api=info.host_api,
                sample_rate=self._sample_rate,
                channels=info.channels,
            )
            result = AlsaHwDirectRestartResult(
                verdict=AlsaHwDirectRestartVerdict.DOWNGRADED_TO_SESSION_MANAGER,
                engaged=False,
                host_api=self._host_api_name,
                device=self._input_device,
                sample_rate=self._sample_rate,
                detail=(
                    f"opener fell back to {info.host_api!r} — session manager still in signal path"
                ),
            )
            _emit_alsa_hw_direct_restart_metric(result)
            return result
        logger.warning(
            "audio_capture_alsa_hw_direct_restart_ok",
            device=self._input_device,
            host_api=self._host_api_name,
            sample_rate=self._sample_rate,
            channels=info.channels,
        )
        result = AlsaHwDirectRestartResult(
            verdict=AlsaHwDirectRestartVerdict.ALSA_HW_ENGAGED,
            engaged=True,
            host_api=self._host_api_name,
            device=self._input_device,
            sample_rate=self._sample_rate,
        )
        _emit_alsa_hw_direct_restart_metric(result)
        return result

    async def request_session_manager_restart(
        self,
        target_device: DeviceEntry | None = None,
    ) -> SessionManagerRestartResult:
        """Revert the capture stream to the PipeWire/PulseAudio session manager.

        Linux-specific twin of :meth:`request_shared_restart`. Two
        legitimate callers:

        * :class:`LinuxPipeWireDirectBypass` (revert path) — no
          ``target_device`` supplied, the method searches for the
          first sibling whose :attr:`DeviceEntry.host_api_name` lies
          in :data:`_LINUX_SESSION_MANAGER_HOST_APIS`.
        * :class:`LinuxSessionManagerEscapeBypass` (apply path, T6
          of voice-linux-cascade-root-fix) — supplies a concrete
          ``target_device`` resolved to a session-manager virtual
          (``pipewire``, ``pulse``) or the OS default ``default`` PCM.
          The method skips sibling discovery and opens directly.

        When neither path yields a target the method returns
        :attr:`SessionManagerRestartVerdict.DOWNGRADED_TO_ALSA_HW` with
        the existing stream preserved.

        Args:
            target_device: Optional explicit target. When ``None``,
                the canonical-name-sibling discovery runs. When
                provided, the method opens against that device
                verbatim — callers are responsible for pre-filtering.

        Returns:
            A :class:`SessionManagerRestartResult`. A non-engaged
            verdict means either the session-manager reopen was not
            feasible (``DOWNGRADED_TO_ALSA_HW``, ``NO_TARGET``) or the
            pipeline is now without a live capture
            (``OPEN_FAILED_NO_STREAM``).
        """
        if not self._running:
            logger.debug("audio_capture_session_manager_restart_skipped_not_running")
            result = SessionManagerRestartResult(
                verdict=SessionManagerRestartVerdict.NOT_RUNNING,
                engaged=False,
                detail="capture task is not running",
            )
            _emit_session_manager_restart_metric(result)
            return result
        if sys.platform != "linux":
            logger.debug(
                "audio_capture_session_manager_restart_skipped_not_linux",
                platform=sys.platform,
            )
            result = SessionManagerRestartResult(
                verdict=SessionManagerRestartVerdict.NOT_LINUX,
                engaged=False,
                host_api=self._host_api_name,
                device=self._input_device,
                sample_rate=self._sample_rate,
                detail=(
                    f"request_session_manager_restart is Linux-only; running on {sys.platform}"
                ),
            )
            _emit_session_manager_restart_metric(result)
            return result

        if target_device is not None:
            session_entry: DeviceEntry | None = target_device
        else:
            session_entry = self._find_sibling_with_host_api_in(
                _LINUX_SESSION_MANAGER_HOST_APIS,
            )
        if session_entry is None:
            logger.warning(
                "audio_capture_session_manager_restart_no_sibling",
                device=self._input_device,
                host_api=self._host_api_name,
            )
            result = SessionManagerRestartResult(
                verdict=SessionManagerRestartVerdict.DOWNGRADED_TO_ALSA_HW,
                engaged=False,
                host_api=self._host_api_name,
                device=self._input_device,
                sample_rate=self._sample_rate,
                detail=(
                    "no PulseAudio/PipeWire sibling available — device is "
                    "ALSA-direct only; existing stream preserved"
                ),
            )
            _emit_session_manager_restart_metric(result)
            return result

        from sovyx.voice._stream_opener import StreamOpenError, open_input_stream

        tuning = self._tuning if self._tuning is not None else _VoiceTuning()
        logger.warning(
            "audio_capture_session_manager_restart_begin",
            device=self._input_device,
            host_api=self._host_api_name,
            target_host_api=session_entry.host_api_name,
            target_device_index=session_entry.index,
        )

        await asyncio.to_thread(self._close_stream, "session_manager_restart")
        while not self._queue.empty():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()

        try:
            stream, info = await open_input_stream(
                device=session_entry,
                target_rate=self._sample_rate,
                blocksize=self._blocksize,
                callback=self._audio_callback,
                tuning=tuning,
                sd_module=self._sd_module,
                enumerate_fn=self._enumerate_fn,
                validate_fn=None,
            )
        except StreamOpenError as exc:
            logger.error(
                "audio_capture_session_manager_restart_failed",
                error=str(exc),
                device=self._input_device,
                host_api=self._host_api_name,
            )
            self._signal_consumer_shutdown()
            result = SessionManagerRestartResult(
                verdict=SessionManagerRestartVerdict.OPEN_FAILED_NO_STREAM,
                engaged=False,
                host_api=self._host_api_name,
                device=self._input_device,
                detail=f"session-manager reopen failed: {exc}",
            )
            _emit_session_manager_restart_metric(result)
            return result

        self._stream = stream
        self._sample_rate = info.sample_rate
        self._input_device = info.device_index
        self._host_api_name = info.host_api
        self._resolved_device_name = session_entry.name
        # F5/F6: AGC2 default-on per VoiceTuningConfig.agc2_enabled
        # (commit 2e36893). Operators can revert via
        # SOVYX_TUNING__VOICE__AGC2_ENABLED=false. The factory
        # returns None when disabled — FrameNormalizer accepts None
        # as the no-op default so the call site needs no ``if`` branch.
        _agc2_tuning = self._tuning if self._tuning is not None else _VoiceTuning()
        self._normalizer = FrameNormalizer(
            source_rate=info.sample_rate,
            source_channels=info.channels,
            agc2=build_agc2_if_enabled(
                enabled=_agc2_tuning.agc2_enabled,
                sample_rate=info.sample_rate,
            ),
        )
        self._allocate_ring_buffer(tuning)
        self._emit_stream_opened(info, apo_bypass_attempted=False)

        if info.host_api not in _LINUX_SESSION_MANAGER_HOST_APIS:
            logger.warning(
                "audio_capture_session_manager_restart_downgraded_to_alsa_hw",
                device=self._input_device,
                host_api=info.host_api,
            )
            result = SessionManagerRestartResult(
                verdict=SessionManagerRestartVerdict.DOWNGRADED_TO_ALSA_HW,
                engaged=False,
                host_api=self._host_api_name,
                device=self._input_device,
                sample_rate=self._sample_rate,
                detail=(
                    f"opener fell back to {info.host_api!r} — session manager not in signal path"
                ),
            )
            _emit_session_manager_restart_metric(result)
            return result
        logger.warning(
            "audio_capture_session_manager_restart_ok",
            device=self._input_device,
            host_api=self._host_api_name,
            sample_rate=self._sample_rate,
            channels=info.channels,
        )
        result = SessionManagerRestartResult(
            verdict=SessionManagerRestartVerdict.SESSION_MANAGER_ENGAGED,
            engaged=True,
            host_api=self._host_api_name,
            device=self._input_device,
            sample_rate=self._sample_rate,
        )
        _emit_session_manager_restart_metric(result)
        return result

    def _find_sibling_with_host_api(self, host_api: str) -> DeviceEntry | None:
        """Return the enumeration sibling of the current endpoint on ``host_api``.

        Siblings share the same :attr:`DeviceEntry.canonical_name` but
        are served by different host APIs — on Linux a single USB
        microphone typically appears once via ``ALSA``, once via
        ``PulseAudio``, once via ``PipeWire``. Returns ``None`` when the
        current entry has no sibling on the requested host API.
        """
        return self._find_sibling_with_host_api_in(frozenset({host_api}))

    def _find_sibling_with_host_api_in(
        self,
        host_apis: frozenset[str],
    ) -> DeviceEntry | None:
        """Return the first enumeration sibling whose host API is in ``host_apis``.

        Uses the same :func:`_resolve_input_entry` entry point as the
        start + restart paths so any DI-provided ``enumerate_fn`` is
        honoured. Returns ``None`` on enumeration failure rather than
        raising — the caller translates the absence into a structured
        verdict.
        """
        try:
            current = _resolve_input_entry(
                input_device=self._input_device,
                enumerate_fn=self._enumerate_fn,
                host_api_name=self._host_api_name,
            )
        except RuntimeError:
            return None
        canonical = current.canonical_name
        if self._enumerate_fn is not None:
            entries = self._enumerate_fn()
        else:
            from sovyx.voice.device_enum import enumerate_devices

            entries = enumerate_devices()
        for entry in entries:
            if entry.max_input_channels <= 0:
                continue
            if entry.canonical_name != canonical:
                continue
            if entry.host_api_name in host_apis:
                return entry
        return None

    def _ensure_endpoint_guid(self, entry: DeviceEntry | None) -> None:
        """Populate :attr:`_endpoint_guid` from ``entry`` if still unset.

        Uses the same
        :func:`~sovyx.voice.health._factory_integration.derive_endpoint_guid`
        the cascade + ComboStore use, so the GUID the coordinator keys
        on matches the GUID already persisted on disk. Idempotent: an
        explicit value passed through the constructor is preserved.
        """
        if self._endpoint_guid:
            return
        if entry is None:
            return
        from sovyx.voice.health._factory_integration import derive_endpoint_guid

        self._endpoint_guid = derive_endpoint_guid(entry)

    def _allocate_ring_buffer(self, tuning: VoiceTuningConfig) -> None:
        """Allocate (or resize) the 16 kHz-mono int16 ring buffer.

        Called from :meth:`start` and from the two reopen paths
        (:meth:`request_exclusive_restart`, :meth:`request_shared_restart`,
        :meth:`_reopen_stream_after_device_error`) so a reopen never
        leaks stale frames from the pre-reopen stream. The write pointer
        is reset to zero so ``tap_recent_frames`` after the reopen only
        returns audio from the fresh stream.

        v1.3 §4.2 L4-B — the epoch component of ``_ring_state`` bumps on
        every allocation so an in-flight :meth:`tap_frames_since_mark`
        can detect "the ring was reset while I was waiting" via epoch
        inequality and avoid waiting forever for a sample count the new
        ring will never reach.
        """
        import numpy as np

        seconds = max(0.0, float(tuning.capture_ring_buffer_seconds))
        capacity = max(1, int(seconds * _SAMPLE_RATE))
        self._ring_buffer = np.zeros(capacity, dtype=np.int16)
        self._ring_capacity = capacity
        self._ring_write_index = 0
        # Bump epoch, reset samples. Single atomic assignment so any
        # concurrent reader observing ``_ring_state`` sees the new
        # (epoch, 0) pair consistently — never an old-epoch/new-samples
        # or new-epoch/old-samples interleaving.
        current_epoch = self._ring_state >> _RING_EPOCH_SHIFT
        self._ring_state = (current_epoch + 1) << _RING_EPOCH_SHIFT

    def _ring_write(self, window: npt.NDArray[np.int16]) -> None:
        """Append a pipeline-shaped frame (16 kHz mono int16) to the ring.

        Synchronous by design: runs between ``await`` points inside
        :meth:`_consume_loop` so no lock is required against
        :meth:`tap_recent_frames` (which is also synchronous between
        its own awaits). Silent no-op when :meth:`_allocate_ring_buffer`
        hasn't run yet — keeps test harnesses that drive ``feed_frame``
        without starting the task alive.
        """
        buf = self._ring_buffer
        if buf is None:
            return
        cap = self._ring_capacity
        n = int(window.shape[0])
        if n <= 0 or cap <= 0:
            return
        # v1.3 §4.2 — compute the post-write state once and commit via
        # a single ``_ring_state = ...`` assignment so cross-loop readers
        # never observe a half-updated pair. The samples component wraps
        # at ``_RING_SAMPLES_MASK`` (effectively never, at 16 kHz); the
        # epoch is preserved by masking the low bits.
        state = self._ring_state
        epoch_bits = state & ~_RING_SAMPLES_MASK
        new_samples = ((state & _RING_SAMPLES_MASK) + n) & _RING_SAMPLES_MASK
        # If a single window is larger than the buffer (pathological —
        # 33 s default holds ~1_032 blocks of 16 ms), keep only the tail.
        if n >= cap:
            buf[:] = window[-cap:]
            self._ring_write_index = 0
            self._ring_state = epoch_bits | new_samples
            return
        start = self._ring_write_index
        end = start + n
        if end <= cap:
            buf[start:end] = window
        else:
            head = cap - start
            buf[start:cap] = window[:head]
            buf[0 : n - head] = window[head:]
        self._ring_write_index = (start + n) % cap
        self._ring_state = epoch_bits | new_samples

    async def tap_recent_frames(
        self,
        duration_s: float,
    ) -> npt.NDArray[np.int16]:
        """Return the most recent ``duration_s`` seconds of 16 kHz mono int16.

        The returned array is always a fresh copy — callers can hold on
        to it after subsequent writes invalidate the ring slot. When
        fewer frames than requested have been written (cold start, early
        bypass attempt) the slice is truncated to what's actually
        available; callers inspect ``.shape[0]`` to decide whether the
        sample is large enough for their analysis.

        Thread-safety: see ``__init__`` docstring — reads happen
        synchronously against writes that also run between awaits on the
        same event loop, so no lock is required. The async signature is
        kept for future-proofing (Protocol contract + possible move to
        an off-loop ring implementation).

        Args:
            duration_s: Requested snapshot duration in seconds. Clamped
                to ``[0, capture_ring_buffer_seconds]``.

        Returns:
            An ``(N,)`` int16 array, ``N == int(duration_s * 16_000)``
            at most, possibly shorter when the ring is not yet full.
        """
        import numpy as np

        buf = self._ring_buffer
        cap = self._ring_capacity
        if buf is None or cap <= 0 or duration_s <= 0:
            return np.zeros(0, dtype=np.int16)
        wanted = min(cap, int(duration_s * _SAMPLE_RATE))
        # v1.3 §4.2 — derive samples_written from the packed ``_ring_state``
        # so reads and writes share a single source of truth.
        available = min(self._ring_state & _RING_SAMPLES_MASK, cap)
        n = min(wanted, available)
        if n <= 0:
            return np.zeros(0, dtype=np.int16)
        end = self._ring_write_index
        begin = (end - n) % cap
        if begin + n <= cap:
            return buf[begin : begin + n].copy()
        head = cap - begin
        out = np.empty(n, dtype=np.int16)
        out[:head] = buf[begin:cap]
        out[head:] = buf[0 : n - head]
        return out

    # -- v1.3 §4.2 L4-B — mark-based tap -------------------------------

    def samples_written_mark(self) -> tuple[int, int]:
        """Return an opaque ``(epoch, samples_written)`` pair.

        Atomic decomposition of the packed :attr:`_ring_state` into the
        two logical components the coordinator needs:

        1. Single ``LOAD_ATTR`` of ``_ring_state`` copies both components
           into a local name in one bytecode step — no cross-loop race
           can split the epoch from the samples.
        2. The returned tuple is therefore guaranteed to reflect one
           consistent state generation, satisfying the
           :class:`~sovyx.voice.health.contract.CaptureTaskProto`
           contract.

        Callers treat the tuple as opaque. See the Protocol docstring
        for the contract's rationale.
        """
        state = self._ring_state  # single atomic LOAD_ATTR
        return (state >> _RING_EPOCH_SHIFT, state & _RING_SAMPLES_MASK)

    async def tap_frames_since_mark(
        self,
        mark: tuple[int, int],
        min_samples: int,
        max_wait_s: float,
    ) -> npt.NDArray[np.int16]:
        """Return frames written AFTER ``mark`` was captured.

        See :class:`~sovyx.voice.health.contract.CaptureTaskProto` for
        the full contract. Implementation notes:

        * ``_ring_state`` is read in one ``LOAD_ATTR`` per loop iteration
          so epoch and samples count always correspond to the same state
          generation.
        * If the epoch bundled in ``mark`` no longer matches the current
          epoch, the ring was reallocated (a stream reopen / exclusive
          restart): every sample currently in the buffer is by
          definition post-mark, and we short-circuit with the available
          tail rather than spinning for a delta that will never
          materialise.
        * The poll interval comes from
          :attr:`VoiceTuningConfig.mark_tap_poll_interval_s` (§14.E4)
          so operators can tune responsiveness without editing code.
        """
        import numpy as np

        mark_epoch, mark_samples = mark
        tuning = self._tuning if self._tuning is not None else _VoiceTuning()
        poll_interval_s = max(0.001, float(tuning.mark_tap_poll_interval_s))
        deadline = time.monotonic() + max(0.0, float(max_wait_s))

        while True:
            state = self._ring_state  # atomic LOAD_ATTR per iteration
            current_epoch = state >> _RING_EPOCH_SHIFT
            current_samples = state & _RING_SAMPLES_MASK

            if current_epoch != mark_epoch:
                # Ring was reallocated after the mark was taken — every
                # sample now in the buffer is post-reset, so treat the
                # entire capacity as post-mark.
                available = min(current_samples, self._ring_capacity)
                if available >= min_samples or time.monotonic() >= deadline:
                    if available <= 0:
                        return np.zeros(0, dtype=np.int16)
                    return await self.tap_recent_frames(
                        min(available, min_samples) / _SAMPLE_RATE,
                    )
            else:
                new_samples = current_samples - mark_samples
                if new_samples >= min_samples:
                    return await self.tap_recent_frames(min_samples / _SAMPLE_RATE)
                if time.monotonic() >= deadline:
                    if new_samples <= 0:
                        return np.zeros(0, dtype=np.int16)
                    return await self.tap_recent_frames(new_samples / _SAMPLE_RATE)

            await asyncio.sleep(poll_interval_s)

    def _emit_stream_opened(
        self,
        info: Any,  # noqa: ANN401 — StreamInfo dataclass, typed lazily
        *,
        apo_bypass_attempted: bool,
    ) -> None:
        """Generate a fresh stream_id and emit ``audio.stream.opened``.

        Resets the per-stream lifecycle counters
        (``_stream_underruns`` / ``_stream_overflows`` /
        ``_stream_callback_frames``) so the matching
        ``audio.stream.closed`` event reflects *this* stream only — not
        cumulative activity from prior reopens.

        ``apo_bypass_attempted`` is ``True`` only when the open was
        triggered by :meth:`request_exclusive_restart` (the explicit
        APO-bypass path); reverts and reconnects pass ``False``.
        """
        self._stream_id = uuid4().hex[:16]
        self._stream_underruns = 0
        self._stream_overflows = 0
        self._stream_callback_frames = 0
        sample_rate = int(getattr(info, "sample_rate", 0) or 0)
        mode = "exclusive" if getattr(info, "exclusive_used", False) else "shared"
        buffer_size_ms = int(self._blocksize * 1000 / sample_rate) if sample_rate else 0
        logger.info(
            "audio.stream.opened",
            **{
                "voice.stream_id": self._stream_id,
                "voice.device_id": self._resolved_device_name or "default",
                "voice.host_api": getattr(info, "host_api", None),
                "voice.mode": mode,
                "voice.sample_rate": sample_rate,
                "voice.channels": int(getattr(info, "channels", 0) or 0),
                "voice.buffer_size_ms": buffer_size_ms,
                "voice.apo_bypass_attempted": apo_bypass_attempted,
                "voice.fallback_depth": int(getattr(info, "fallback_depth", 0) or 0),
                "voice.auto_convert_used": bool(getattr(info, "auto_convert_used", False)),
            },
        )

    def _close_stream(self, reason: str = "unknown") -> None:
        """Stop and close the stream — tolerant of already-closed streams.

        Emits ``audio.stream.closed`` with the cumulative xrun counts
        and frame total observed by the PortAudio callback for this
        stream BEFORE tearing it down. ``reason`` is a stable tag
        (``"shutdown"`` / ``"exclusive_restart"`` / ``"shared_restart"``
        / ``"device_error"`` / ``"unknown"``) the dashboard uses to
        distinguish operator-initiated tear-downs from device errors.
        """
        stream = self._stream
        self._stream = None
        if stream is None:
            return
        if self._stream_id:
            logger.info(
                "audio.stream.closed",
                **{
                    "voice.stream_id": self._stream_id,
                    "voice.device_id": self._resolved_device_name or "default",
                    "voice.reason": reason,
                    "voice.underruns": self._stream_underruns,
                    "voice.overflows": self._stream_overflows,
                    "voice.frames_processed": self._stream_callback_frames,
                },
            )
            self._stream_id = ""
        try:
            stream.stop()
            stream.close()
        except Exception:  # noqa: BLE001 — stream may already be dead
            logger.debug("audio_capture_close_failed", exc_info=True)

    def _audio_callback(
        self,
        indata: npt.NDArray[np.int16],
        frames: int,  # noqa: ARG002
        time_info: object,  # noqa: ARG002
        status: object,
    ) -> None:
        """PortAudio callback — runs in the audio thread.

        Hands the raw block (any shape, any sample rate that the opener
        negotiated) to the asyncio loop. Downmix + resample + rewindow
        happen on the consumer side via :class:`FrameNormalizer`, which
        is not thread-safe and therefore cannot be touched here. Drops
        frames when the queue is saturated rather than blocking the
        audio thread, which would cause device underruns.
        """
        if status:
            # CallbackFlags: input overflow/underflow. Track for the
            # per-stream ``audio.stream.closed`` event so operators can
            # correlate xruns with kernel-mixer / USB-bus pressure.
            if getattr(status, "input_overflow", False):
                self._stream_overflows += 1
            if getattr(status, "input_underflow", False):
                self._stream_underruns += 1
            logger.debug("audio_callback_status", status=str(status))
        self._stream_callback_frames += 1
        block = indata.copy()
        loop = self._loop
        if loop is None:
            return
        # Loop may be closed mid-shutdown — swallow that and move on.
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(self._enqueue, block)

    def _enqueue(self, frame: npt.NDArray[np.int16]) -> None:
        """Enqueue a frame; drop the oldest on overflow."""
        if self._queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
        self._queue.put_nowait(frame)

    async def _consume_loop(self) -> None:
        """Pull frames off the queue and feed them to the pipeline.

        On ``sd.PortAudioError`` (device unplugged, driver reset) we
        close the stream, sleep briefly, and reopen through the unified
        opener — so a user yanking a USB headset does not wedge the
        pipeline.

        Emits an ``audio_capture_heartbeat`` log every
        ``capture_heartbeat_interval_seconds`` so operators can confirm
        (a) frames are arriving, (b) the mic is not stuck at silence.
        """
        sd = self._sd_module if self._sd_module is not None else _import_sounddevice()

        while self._running:
            try:
                block = await self._queue.get()
                windows = self._normalizer.push(block) if self._normalizer is not None else [block]
                for window in windows:
                    rms_db = _rms_db_int16(window)
                    self._last_rms_db = rms_db
                    self._frames_delivered += 1
                    self._frames_since_heartbeat += 1
                    if rms_db < _VALIDATION_MIN_RMS_DB:
                        self._silent_frames_since_heartbeat += 1
                    # Record the post-normalization frame into the ring
                    # buffer BEFORE feeding the pipeline so the bypass
                    # coordinator's integrity probe sees the exact
                    # samples that VAD sees — not an upstream raw block
                    # that the normalizer would later resample / downmix.
                    self._ring_write(window)
                    await self._pipeline.feed_frame(window)
                self._maybe_emit_heartbeat()
            except asyncio.CancelledError:
                raise
            except sd.PortAudioError as exc:
                logger.warning(
                    "audio_capture_device_error",
                    error=str(exc),
                    device=self._input_device,
                    host_api=self._host_api_name,
                )
                await asyncio.to_thread(self._close_stream, "device_error")
                # Band-aid #10 replacement: exponential backoff with
                # FULL jitter. Constant ``_RECONNECT_DELAY_S`` was the
                # legacy band-aid that hammered a degraded driver
                # every 5 s regardless of how long the outage was.
                # The schedule is lazy-initialised so the (overwhelmingly
                # common) zero-error case has zero backoff overhead.
                # Reset on successful reconnect; advance on each failure.
                if self._reconnect_backoff is None:
                    # Clamp base delay to the BackoffPolicy minimum
                    # (1 ms) so a test-time _RECONNECT_DELAY_S=0
                    # override + future config that lets operators
                    # set 0 doesn't violate the loud-fail bound.
                    # The clamp preserves the operator intent of
                    # "fast retries" while keeping the policy's
                    # busy-loop guard rail.
                    base = max(_RECONNECT_DELAY_S, 0.001)
                    self._reconnect_backoff = BackoffSchedule(
                        BackoffPolicy(
                            base_delay_s=base,
                            max_delay_s=max(base * 12.0, 60.0),
                            multiplier=2.0,
                            max_attempts=1_000_000,  # effectively unbounded
                            jitter=JitterStrategy.FULL,
                        )
                    )
                try:
                    delay_s = self._reconnect_backoff.next()
                except StopIteration:
                    # Should not occur with max_attempts=1M, but the
                    # schedule contract requires handling.
                    delay_s = _RECONNECT_DELAY_S
                logger.info(
                    "audio_capture_reconnect_backoff",
                    delay_s=round(delay_s, 3),
                    attempt=self._reconnect_backoff.attempt_count,
                    base_s=_RECONNECT_DELAY_S,
                )
                await asyncio.sleep(delay_s)
                if not self._running:
                    return
                try:
                    await self._reopen_stream_after_device_error()
                    logger.info("audio_capture_device_reconnected")
                    # Successful reconnect — reset the backoff so the
                    # next outage starts from base_delay_s, not
                    # wherever the previous outage's escalation left
                    # us. Without reset, a transient outage 30 min
                    # ago would still penalise today's reconnect.
                    self._reconnect_backoff.reset()
                except Exception as reopen_exc:  # noqa: BLE001
                    logger.error(
                        "audio_capture_reconnect_failed",
                        error=str(reopen_exc),
                        next_delay_attempt=self._reconnect_backoff.attempt_count,
                    )
            except Exception:  # noqa: BLE001
                # A single bad frame must not kill the loop. Log with
                # traceback so persistent upstream errors surface.
                logger.exception("audio_capture_feed_failed")

    def _maybe_emit_heartbeat(self) -> None:
        """Log a periodic RMS/frame-count heartbeat.

        Only fires when ``_HEARTBEAT_INTERVAL_S`` has elapsed since the
        last one, so log volume stays constant regardless of sample
        rate. Resets per-interval counters after each emit.
        """
        now = time.monotonic()
        if now - self._last_heartbeat_monotonic < _HEARTBEAT_INTERVAL_S:
            return
        normalizer = self._normalizer
        logger.info(
            "audio_capture_heartbeat",
            device=self._input_device,
            host_api=self._host_api_name,
            frames_delivered=self._frames_delivered,
            frames_since_last=self._frames_since_heartbeat,
            silent_frames=self._silent_frames_since_heartbeat,
            last_rms_db=round(self._last_rms_db, 1),
            source_rate=normalizer.source_rate if normalizer is not None else None,
            source_channels=normalizer.source_channels if normalizer is not None else None,
            normalizer_active=(not normalizer.is_passthrough if normalizer is not None else False),
        )
        self._last_heartbeat_monotonic = now
        self._frames_since_heartbeat = 0
        self._silent_frames_since_heartbeat = 0


_PEAK_DB_RE = re.compile(r"peak\s+(-?\d+(?:\.\d+)?)\s*dBFS", re.IGNORECASE)


def _extract_peak_db(detail: str | None) -> float:
    """Parse ``peak -XX.X dBFS`` out of an opener silence-attempt detail.

    The opener formats silence attempts as
    ``"silent stream (peak -96.0 dBFS < threshold -80.0 dBFS)"``.
    Returns :data:`_RMS_FLOOR_DB` when the pattern is absent so callers
    can still aggregate a worst-case peak across attempts.
    """
    if not detail:
        return _RMS_FLOOR_DB
    match = _PEAK_DB_RE.search(detail)
    if match is None:
        return _RMS_FLOOR_DB
    try:
        return float(match.group(1))
    except ValueError:
        return _RMS_FLOOR_DB


def _resolve_input_entry(
    *,
    input_device: int | str | None,
    enumerate_fn: Callable[[], list[DeviceEntry]] | None,
    host_api_name: str | None,
) -> DeviceEntry:
    """Resolve a capture-task input selector to a live :class:`DeviceEntry`.

    Matching order:

    1. Exact PortAudio index (``int``) when provided.
    2. Canonical device name (``str``) optionally refined by
       ``host_api_name`` — lets the wizard persist a stable identifier
       across reboots where indices shuffle.
    3. First OS-default input, or the first available input entry.

    Raises :class:`RuntimeError` when the host exposes no input devices
    at all so :meth:`AudioCaptureTask.start` can fail loudly instead of
    silently opening the OS default.
    """
    if enumerate_fn is not None:
        entries = enumerate_fn()
    else:
        from sovyx.voice.device_enum import enumerate_devices

        entries = enumerate_devices()

    candidates = [e for e in entries if e.max_input_channels > 0]
    if not candidates:
        msg = "No audio input devices available"
        raise RuntimeError(msg)

    if isinstance(input_device, int):
        for entry in candidates:
            if entry.index == input_device:
                return entry

    if isinstance(input_device, str) and input_device:
        from sovyx.voice.device_enum import _canonicalise

        canonical = _canonicalise(input_device)
        matches = [e for e in candidates if e.canonical_name == canonical]
        if host_api_name:
            for entry in matches:
                if entry.host_api_name == host_api_name:
                    return entry
        if matches:
            return matches[0]

    defaults = [e for e in candidates if e.is_os_default]
    return defaults[0] if defaults else candidates[0]
