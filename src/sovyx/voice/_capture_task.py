"""Background audio capture task that feeds the VoicePipeline.

The :class:`~sovyx.voice.pipeline.VoicePipeline` is push-based — frames
must be delivered via ``pipeline.feed_frame()``. This module owns the
microphone side: opens a ``sounddevice.InputStream`` on the selected
input device, pulls int16 frames from its callback into an asyncio
queue, and dispatches each frame into the pipeline from a consumer
task. On device disconnection the stream is closed, the task waits for
``capture_reconnect_delay_seconds``, and retries from scratch.

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
"running" but is deaf. :meth:`AudioCaptureTask.start` now samples ~600
ms of audio after opening the stream and raises
:class:`CaptureSilenceError` if the peak RMS never crosses
``capture_validation_min_rms_db``. The :func:`sovyx.voice.factory.create_voice_pipeline`
caller catches that and auto-retries the same device on the next
preferred host API (WASAPI → DirectSound → …) so the user does not need
to re-run the wizard.

Without this task the pipeline is silent: frames never arrive and VAD
never fires. See CLAUDE.md §anti-pattern #14 — ONNX inference is run on
``asyncio.to_thread`` already inside :meth:`VoicePipeline.feed_frame`,
so this consumer loop does not need to offload work itself.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import time
from typing import TYPE_CHECKING, Any

from sovyx.engine.config import VoiceTuningConfig as _VoiceTuning
from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    import numpy as np
    import numpy.typing as npt

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
    ) -> None:
        self._pipeline = pipeline
        self._input_device = input_device
        self._sample_rate = sample_rate
        self._blocksize = blocksize
        self._host_api_name = host_api_name
        self._validate_on_start = validate_on_start
        self._queue: asyncio.Queue[npt.NDArray[np.int16]] = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stream: Any = None
        self._consumer: asyncio.Task[None] | None = None
        self._running = False

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

    # -- Lifecycle ------------------------------------------------------------

    async def start(self) -> None:
        """Open the input stream, validate it, and spawn the consumer task.

        Idempotent — a second call while running is a no-op.

        Raises:
            CaptureSilenceError: If ``validate_on_start`` is True and the
                first ~600 ms of audio contains only silence. Callers
                (notably :func:`sovyx.voice.factory.create_voice_pipeline`)
                can catch this to retry on a different host API.
        """
        if self._running:
            return
        self._loop = asyncio.get_running_loop()
        await asyncio.to_thread(self._open_stream)

        if self._validate_on_start:
            try:
                observed_db = await self._validate_stream()
            except BaseException:
                await asyncio.to_thread(self._close_stream)
                raise
            if observed_db < _VALIDATION_MIN_RMS_DB:
                await asyncio.to_thread(self._close_stream)
                msg = (
                    f"Input stream opened on device={self._input_device!r} "
                    f"(host_api={self._host_api_name!r}) but delivered only silence "
                    f"(peak RMS {observed_db:.1f} dBFS < threshold "
                    f"{_VALIDATION_MIN_RMS_DB:.1f} dBFS)."
                )
                raise CaptureSilenceError(
                    msg,
                    device=self._input_device,
                    host_api=self._host_api_name,
                    observed_peak_rms_db=observed_db,
                )
            logger.info(
                "audio_capture_validated",
                device=self._input_device,
                host_api=self._host_api_name,
                peak_rms_db=round(observed_db, 1),
            )

        self._running = True
        self._last_heartbeat_monotonic = time.monotonic()
        self._consumer = asyncio.create_task(self._consume_loop(), name="audio-capture-consumer")
        logger.info(
            "audio_capture_task_started",
            device=self._input_device if self._input_device is not None else "default",
            host_api=self._host_api_name,
            sample_rate=self._sample_rate,
            blocksize=self._blocksize,
        )

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

        Reads frames off the queue (populated by the PortAudio callback
        from a worker thread) and returns the peak per-frame RMS in
        dBFS. Short-circuits as soon as the peak crosses the silence
        threshold — a live mic typically registers >-60 dBFS within a
        few frames of background noise, so the full ~600 ms budget only
        applies when the stream is truly dead.
        """
        deadline = time.monotonic() + _VALIDATION_S
        peak_db = _RMS_FLOOR_DB
        while time.monotonic() < deadline:
            timeout = max(deadline - time.monotonic(), 0.05)
            try:
                frame = await asyncio.wait_for(self._queue.get(), timeout=timeout)
            except TimeoutError:
                break
            db = _rms_db_int16(frame)
            peak_db = max(peak_db, db)
            if peak_db >= _VALIDATION_MIN_RMS_DB:
                return peak_db
        return peak_db

    # -- Internals ------------------------------------------------------------

    def _open_stream(self) -> None:
        """Open a fresh ``sd.InputStream`` (runs in a worker thread)."""
        import sounddevice as sd

        self._stream = sd.InputStream(
            samplerate=self._sample_rate,
            channels=1,
            dtype="int16",
            blocksize=self._blocksize,
            device=self._input_device,
            callback=self._audio_callback,
        )
        self._stream.start()

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

        Extracts the mono channel and hands the frame to the asyncio
        loop. Drops frames when the queue is saturated rather than
        blocking the audio thread, which would cause device underruns.
        """
        if status:
            # CallbackFlags: input overflow/underflow. Log but keep going.
            logger.debug("audio_callback_status", status=str(status))
        mono = indata[:, 0].copy() if indata.ndim > 1 else indata.copy()
        loop = self._loop
        if loop is None:
            return
        # Loop may be closed mid-shutdown — swallow that and move on.
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(self._enqueue, mono)

    def _enqueue(self, frame: npt.NDArray[np.int16]) -> None:
        """Enqueue a frame; drop the oldest on overflow."""
        if self._queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
        self._queue.put_nowait(frame)

    async def _consume_loop(self) -> None:
        """Pull frames off the queue and feed them to the pipeline.

        On ``sd.PortAudioError`` (device unplugged, driver reset) we
        close the stream, sleep briefly, and reopen — so a user
        yanking a USB headset does not wedge the pipeline.

        Emits an ``audio_capture_heartbeat`` log every
        ``capture_heartbeat_interval_seconds`` so operators can confirm
        (a) frames are arriving, (b) the mic is not stuck at silence.
        """
        import sounddevice as sd

        while self._running:
            try:
                frame = await self._queue.get()
                rms_db = _rms_db_int16(frame)
                self._last_rms_db = rms_db
                self._frames_delivered += 1
                self._frames_since_heartbeat += 1
                if rms_db < _VALIDATION_MIN_RMS_DB:
                    self._silent_frames_since_heartbeat += 1
                await self._pipeline.feed_frame(frame)
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
                    await asyncio.to_thread(self._open_stream)
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

    # -- Swap-in for fallback -------------------------------------------------

    def _swap_device(self, *, device: int | str | None, host_api: str | None) -> None:
        """Re-target the task at a different device *before* start.

        Used by :func:`start_capture_with_fallback` after the first
        :class:`CaptureSilenceError` — the task is still unstarted at
        that point, so mutating the device fields is safe.
        """
        if self._running:
            msg = "cannot swap device on a running capture task"
            raise RuntimeError(msg)
        self._input_device = device
        self._host_api_name = host_api

    def _maybe_emit_heartbeat(self) -> None:
        """Log a periodic RMS/frame-count heartbeat.

        Only fires when ``_HEARTBEAT_INTERVAL_S`` has elapsed since the
        last one, so log volume stays constant regardless of sample
        rate. Resets per-interval counters after each emit.
        """
        now = time.monotonic()
        if now - self._last_heartbeat_monotonic < _HEARTBEAT_INTERVAL_S:
            return
        logger.info(
            "audio_capture_heartbeat",
            device=self._input_device,
            host_api=self._host_api_name,
            frames_delivered=self._frames_delivered,
            frames_since_last=self._frames_since_heartbeat,
            silent_frames=self._silent_frames_since_heartbeat,
            last_rms_db=round(self._last_rms_db, 1),
        )
        self._last_heartbeat_monotonic = now
        self._frames_since_heartbeat = 0
        self._silent_frames_since_heartbeat = 0


async def start_capture_with_fallback(
    task: AudioCaptureTask,
    *,
    device_name: str | None = None,
) -> None:
    """Start ``task``, falling back to adjacent host APIs on silence.

    The real-world failure mode on Windows is MME + non-native sample
    rate + USB gaming headset = silent zeros. Rather than surface an
    opaque ``CaptureSilenceError`` to the dashboard, this helper:

    1. Starts the task as configured.
    2. If it raises :class:`CaptureSilenceError`, looks up the same
       canonical device name in :mod:`sovyx.voice.device_enum`, walks
       through variants on other host APIs in preference order, and
       retries. Already-tried ``(name, host_api)`` pairs are skipped.
    3. Gives up only after every viable variant has delivered silence.

    Args:
        task: Freshly-constructed :class:`AudioCaptureTask` (not yet
            started).
        device_name: Stable device name to use for fallback resolution.
            When ``None``, the current ``task.input_device`` is resolved
            against the live PortAudio list to derive the canonical
            name — so a caller that only has an index still gets
            fallback behaviour.

    Raises:
        CaptureSilenceError: When every variant fails. Carries the
            original silence details so the dashboard can surface a
            precise error payload.
    """
    from sovyx.voice.device_enum import (
        _canonicalise,
        _host_api_rank,
        enumerate_devices,
    )

    first_error: CaptureSilenceError | None = None
    first_host_api: str | None = task.host_api_name
    first_index: int | str | None = task.input_device

    try:
        await task.start()
        return
    except CaptureSilenceError as exc:
        first_error = exc
        logger.warning(
            "audio_capture_silence_detected",
            device=exc.device,
            host_api=exc.host_api,
            observed_peak_rms_db=exc.observed_peak_rms_db,
        )

    entries = await asyncio.to_thread(enumerate_devices)
    if not entries:
        raise first_error

    canonical = _canonicalise(device_name) if device_name else None
    if canonical is None:
        # Derive the canonical name from whatever device was just tried so
        # we can look up its host-API siblings.
        if isinstance(first_index, int):
            for e in entries:
                if e.index == first_index:
                    canonical = e.canonical_name
                    break
        elif isinstance(first_index, str):
            canonical = _canonicalise(first_index)

    if canonical is None:
        raise first_error

    tried: set[tuple[str, str]] = {(canonical, first_host_api or "")}

    siblings = [e for e in entries if e.canonical_name == canonical and e.max_input_channels > 0]
    siblings.sort(key=lambda e: (_host_api_rank(e.host_api_name), e.index))

    for variant in siblings:
        key = (variant.canonical_name, variant.host_api_name)
        if key in tried:
            continue
        logger.info(
            "audio_capture_fallback_retry",
            name=variant.name,
            host_api=variant.host_api_name,
            index=variant.index,
        )
        task._swap_device(device=variant.index, host_api=variant.host_api_name)  # noqa: SLF001
        tried.add(key)
        try:
            await task.start()
            logger.info(
                "audio_capture_fallback_succeeded",
                host_api=variant.host_api_name,
            )
            return
        except CaptureSilenceError as exc:
            first_error = exc
            logger.warning(
                "audio_capture_fallback_silent",
                host_api=variant.host_api_name,
                observed_peak_rms_db=exc.observed_peak_rms_db,
            )
            continue

    raise first_error
