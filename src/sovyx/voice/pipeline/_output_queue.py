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
        try:
            while not self._queue.empty() and not self._interrupted:
                chunk = self._queue.get_nowait()
                await _play_audio(chunk)
        finally:
            self._playing = False
            self._interrupted = False

    def interrupt(self) -> None:
        """Stop current playback and clear the queue (barge-in)."""
        self._interrupted = True
        # Drain queue without awaiting
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    def clear(self) -> None:
        """Clear pending chunks without interrupting current playback."""
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break


async def _play_audio(chunk: AudioChunk) -> None:
    """Play an audio chunk via sounddevice (or simulate in test).

    This is the low-level playback function.  In production it uses
    ``sounddevice``; unit tests can patch this function.

    Args:
        chunk: The audio chunk to play.
    """
    try:
        import sounddevice as sd

        sd.play(chunk.audio, chunk.sample_rate)
        sd.wait()
    except ImportError:
        # Headless / test environment — simulate playback duration
        if chunk.duration_ms > 0:
            await asyncio.sleep(chunk.duration_ms / 1000)


# ---------------------------------------------------------------------------
# BargeInDetector
# ---------------------------------------------------------------------------
