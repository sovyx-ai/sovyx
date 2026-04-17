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

Without this task the pipeline is silent: frames never arrive and VAD
never fires. See CLAUDE.md §anti-pattern #14 — ONNX inference is run on
``asyncio.to_thread`` already inside :meth:`VoicePipeline.feed_frame`,
so this consumer loop does not need to offload work itself.
"""

from __future__ import annotations

import asyncio
import contextlib
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
    """

    def __init__(
        self,
        pipeline: VoicePipeline,
        *,
        input_device: int | str | None = None,
        sample_rate: int = _SAMPLE_RATE,
        blocksize: int = _FRAME_SAMPLES,
    ) -> None:
        self._pipeline = pipeline
        self._input_device = input_device
        self._sample_rate = sample_rate
        self._blocksize = blocksize
        self._queue: asyncio.Queue[npt.NDArray[np.int16]] = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stream: Any = None
        self._consumer: asyncio.Task[None] | None = None
        self._running = False

    # -- Properties -----------------------------------------------------------

    @property
    def is_running(self) -> bool:
        """Whether the capture task is active (stream open + consumer live)."""
        return self._running

    @property
    def input_device(self) -> int | str | None:
        """Selected PortAudio input device (``None`` = OS default)."""
        return self._input_device

    # -- Lifecycle ------------------------------------------------------------

    async def start(self) -> None:
        """Open the input stream and spawn the consumer task.

        Idempotent — a second call while running is a no-op.
        """
        if self._running:
            return
        self._loop = asyncio.get_running_loop()
        await asyncio.to_thread(self._open_stream)
        self._running = True
        self._consumer = asyncio.create_task(self._consume_loop(), name="audio-capture-consumer")
        logger.info(
            "audio_capture_task_started",
            device=self._input_device if self._input_device is not None else "default",
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
        if loop is None or not self._running:
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
        """
        import sounddevice as sd

        while self._running:
            try:
                frame = await self._queue.get()
                await self._pipeline.feed_frame(frame)
            except asyncio.CancelledError:
                raise
            except sd.PortAudioError as exc:
                logger.warning(
                    "audio_capture_device_error",
                    error=str(exc),
                    device=self._input_device,
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
