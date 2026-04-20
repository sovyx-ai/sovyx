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
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from sovyx.engine.config import VoiceTuningConfig as _VoiceTuning
from sovyx.observability.logging import get_logger
from sovyx.voice._frame_normalizer import FrameNormalizer
from sovyx.voice._stream_opener import _import_sounddevice

if TYPE_CHECKING:
    from collections.abc import Callable

    import numpy as np
    import numpy.typing as npt

    from sovyx.engine.config import VoiceTuningConfig
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

# Floor for log10 — 32-bit PCM noise ≈ -96 dBFS, so -120 is safely below.
_RMS_FLOOR_DB = -120.0


class CaptureSilenceError(RuntimeError):
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


class CaptureInoperativeError(RuntimeError):
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
            shared-mode fallback raised. The stream is closed; the
            pipeline has no capture source until the next reconnect
            cycle in :meth:`_consume_loop`.
        NOT_RUNNING: Called while the task is stopped — no-op.
    """

    EXCLUSIVE_ENGAGED = "exclusive_engaged"
    DOWNGRADED_TO_SHARED = "downgraded_to_shared"
    OPEN_FAILED_SHARED_FALLBACK = "open_failed_shared_fallback"
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

        # Telemetry — populated by the consumer loop.
        self._last_rms_db: float = _RMS_FLOOR_DB
        self._frames_delivered: int = 0
        self._last_heartbeat_monotonic: float = 0.0
        self._frames_since_heartbeat: int = 0
        self._silent_frames_since_heartbeat: int = 0

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
    def host_api_name(self) -> str | None:
        """Host API label for the opened stream (``None`` if unknown)."""
        return self._host_api_name

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

        self._normalizer = FrameNormalizer(
            source_rate=info.sample_rate,
            source_channels=info.channels,
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
        self._consumer = asyncio.create_task(self._consume_loop(), name="audio-capture-consumer")
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

        When every pyramid attempt produced a silent stream, raise
        :class:`CaptureSilenceError` so the existing dashboard route
        catches it and renders the wizard silence UX. Otherwise re-raise
        a :class:`RuntimeError` carrying ``.code`` + ``.attempts`` so
        operators see precisely which combinations were tried.
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
        await asyncio.to_thread(self._close_stream)
        # Drop any in-flight frames — they are stale once stopped.
        while not self._queue.empty():
            self._queue.get_nowait()
        logger.info("audio_capture_task_stopped")

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
        await asyncio.to_thread(self._close_stream)
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
        self._normalizer = FrameNormalizer(
            source_rate=info.sample_rate,
            source_channels=info.channels,
        )
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
        self._normalizer = FrameNormalizer(
            source_rate=info.sample_rate,
            source_channels=info.channels,
        )

    def _close_stream(self) -> None:
        """Stop and close the stream — tolerant of already-closed streams."""
        stream = self._stream
        self._stream = None
        if stream is None:
            return
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
            # CallbackFlags: input overflow/underflow. Log but keep going.
            logger.debug("audio_callback_status", status=str(status))
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
                await asyncio.to_thread(self._close_stream)
                await asyncio.sleep(_RECONNECT_DELAY_S)
                if not self._running:
                    return
                try:
                    await self._reopen_stream_after_device_error()
                    logger.info("audio_capture_device_reconnected")
                except Exception as reopen_exc:  # noqa: BLE001
                    logger.error(
                        "audio_capture_reconnect_failed",
                        error=str(reopen_exc),
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
