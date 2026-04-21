"""Auto-extracted from voice/pipeline.py - see __init__.py for the public re-exports."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from sovyx.voice.tts_piper import AudioChunk

logger = get_logger(__name__)


class AudioOutputQueue:
    """Queue-based audio output with interruption support.

    Manages a FIFO of :class:`AudioChunk` objects and plays them
    sequentially.  :meth:`interrupt` clears the queue and stops
    current playback (used for barge-in).
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[AudioChunk] = asyncio.Queue()
        self._playing = False
        self._interrupted = False
        # Tracks total audio_ms enqueued but not yet drained — gives the
        # dashboard a real "how much audio is queued" gauge instead of
        # just a chunk count (chunks may be tiny single-sentence TTS or
        # large multi-paragraph pre-renders).
        self._pending_audio_ms = 0.0

    @property
    def is_playing(self) -> bool:
        """Whether audio is currently being played."""
        return self._playing

    async def enqueue(self, chunk: AudioChunk) -> None:
        """Add an audio chunk to the playback queue.

        Args:
            chunk: Audio data to play.
        """
        await self._queue.put(chunk)
        self._pending_audio_ms += float(chunk.duration_ms)
        depth = self._queue.qsize()
        logger.info(
            "voice.output_queue.enqueued",
            **{
                "voice.depth": depth,
                "voice.chunk_audio_ms": round(float(chunk.duration_ms), 1),
                "voice.pending_audio_ms": round(self._pending_audio_ms, 1),
            },
        )
        logger.info(
            "voice.output_queue.depth",
            **{
                "voice.depth": depth,
                "voice.pending_audio_ms": round(self._pending_audio_ms, 1),
                "voice.is_playing": self._playing,
            },
        )

    async def play_immediate(self, chunk: AudioChunk) -> None:
        """Play a single chunk immediately (blocking until done).

        Args:
            chunk: Audio data to play.
        """
        self._playing = True
        try:
            await _play_audio(chunk)
        finally:
            self._playing = False

    async def drain(self) -> None:
        """Play all queued chunks sequentially until queue is empty."""
        self._playing = True
        self._interrupted = False
        chunks_drained = 0
        audio_ms_drained = 0.0
        try:
            while not self._queue.empty() and not self._interrupted:
                chunk = self._queue.get_nowait()
                self._pending_audio_ms = max(
                    0.0, self._pending_audio_ms - float(chunk.duration_ms)
                )
                await _play_audio(chunk)
                chunks_drained += 1
                audio_ms_drained += float(chunk.duration_ms)
        finally:
            self._playing = False
            interrupted = self._interrupted
            self._interrupted = False
            logger.info(
                "voice.output_queue.drained",
                **{
                    "voice.chunks_drained": chunks_drained,
                    "voice.audio_ms_drained": round(audio_ms_drained, 1),
                    "voice.depth_remaining": self._queue.qsize(),
                    "voice.pending_audio_ms": round(self._pending_audio_ms, 1),
                    "voice.interrupted": interrupted,
                },
            )

    def interrupt(self) -> None:
        """Stop current playback and clear the queue (barge-in)."""
        self._interrupted = True
        # Drain queue without awaiting
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._pending_audio_ms = 0.0

    def clear(self) -> None:
        """Clear pending chunks without interrupting current playback."""
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._pending_audio_ms = 0.0


async def _play_audio(chunk: AudioChunk) -> None:
    """Play an audio chunk via sounddevice (or simulate in test).

    This is the low-level playback function.  In production it uses
    :func:`sovyx.voice._stream_opener.blocking_write_play` (the
    threadpool-safe ``sd.OutputStream.write`` blocking path); unit
    tests can patch this function.

    Playback is blocking — a typical TTS chunk takes hundreds of
    milliseconds to several seconds. Running that inside ``async def``
    would stall the dashboard WebSocket, voice pipeline frame loop, and
    every other coroutine for the whole playback. Offload to a worker
    thread so the event loop stays responsive (anti-pattern #14).

    ``sd.play`` is deliberately avoided here: on Windows + WASAPI its
    callback engine requires COM on the calling thread, which
    :func:`asyncio.to_thread` workers do not have —
    :func:`blocking_write_play` uses the blocking WASAPI path that
    handles COM transitions internally.

    Args:
        chunk: The audio chunk to play.
    """
    try:
        import sounddevice as sd
    except ImportError:
        # Headless / test environment — simulate playback duration
        if chunk.duration_ms > 0:
            await asyncio.sleep(chunk.duration_ms / 1000)
        return

    from sovyx.voice._stream_opener import blocking_write_play

    await asyncio.to_thread(blocking_write_play, sd, chunk.audio, chunk.sample_rate)


# ---------------------------------------------------------------------------
# BargeInDetector
# ---------------------------------------------------------------------------
