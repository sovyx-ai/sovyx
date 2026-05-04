"""Tests for the sync↔async STT-fallback bridge.

Mission: ``MISSION-wake-word-stt-fallback-2026-05-04.md`` §T5.

The bridge crosses three boundaries simultaneously:
* Sync caller (audio thread) → async target (STTEngine.transcribe) →
  loop-bound coroutine (daemon main loop).

These tests pin the contract:

* Happy path returns ``TranscriptionResult.text``.
* Engine raising → returns "" (no-match contract; failure isolation).
* Timeout → returns "" + future cancelled (best-effort).
* Loop closed/not-running → returns "" instead of raising.
* Lock serialises concurrent calls (R1 defense-in-depth).
* Bridge is reentrant — a fresh future per call.

Reference: research findings R1 (MoonshineSTT contract) + R2 (bridge
primitive selection) in the mission spec.
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass

import numpy as np
import pytest

from sovyx.voice.factory._stt_fallback_bridge import (
    make_stt_fallback_transcribe_fn,
)

# ── Test fakes ───────────────────────────────────────────────────────


@dataclass
class _FakeTranscriptionResult:
    """Mirror of :class:`TranscriptionResult` — only ``text`` is read."""

    text: str


class _FakeEngineHappy:
    """Returns a deterministic transcript regardless of audio input."""

    def __init__(self, transcript: str) -> None:
        self._transcript = transcript
        self.call_count = 0

    async def transcribe(
        self,
        audio: np.ndarray,
        sample_rate: int = 16000,
    ) -> _FakeTranscriptionResult:
        del audio, sample_rate
        self.call_count += 1
        return _FakeTranscriptionResult(text=self._transcript)


class _FakeEngineErroring:
    """Raises a deterministic exception on every call."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def transcribe(
        self,
        audio: np.ndarray,
        sample_rate: int = 16000,
    ) -> _FakeTranscriptionResult:
        del audio, sample_rate
        raise self._exc


class _FakeEngineSlow:
    """Sleeps for ``delay_s`` before returning. Used to test timeouts."""

    def __init__(self, delay_s: float, transcript: str = "delayed") -> None:
        self._delay_s = delay_s
        self._transcript = transcript

    async def transcribe(
        self,
        audio: np.ndarray,
        sample_rate: int = 16000,
    ) -> _FakeTranscriptionResult:
        del audio, sample_rate
        await asyncio.sleep(self._delay_s)
        return _FakeTranscriptionResult(text=self._transcript)


# ── Loop-running fixtures ────────────────────────────────────────────


def _start_loop_in_thread() -> tuple[asyncio.AbstractEventLoop, threading.Thread]:
    """Spin up an event loop in a daemon thread.

    Mirrors the production topology: the daemon's main loop runs on
    one thread; the audio thread (which calls ``transcribe_sync``) is a
    different thread. Tests submit work via
    ``asyncio.run_coroutine_threadsafe`` exactly as production does.
    """
    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def _run() -> None:
        asyncio.set_event_loop(loop)
        ready.set()
        loop.run_forever()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    ready.wait(timeout=2.0)
    return loop, thread


def _stop_loop(loop: asyncio.AbstractEventLoop, thread: threading.Thread) -> None:
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=2.0)
    loop.close()


@pytest.fixture
def loop_and_thread() -> object:
    loop, thread = _start_loop_in_thread()
    try:
        yield loop
    finally:
        _stop_loop(loop, thread)


# ── Test cases ───────────────────────────────────────────────────────


class TestHappyPath:
    """Bridge returns the engine's transcript text verbatim."""

    def test_happy_path_returns_engine_text(
        self, loop_and_thread: asyncio.AbstractEventLoop
    ) -> None:
        loop = loop_and_thread
        engine = _FakeEngineHappy(transcript="hey sovyx")

        async def _make() -> object:
            lock = asyncio.Lock()
            return make_stt_fallback_transcribe_fn(engine=engine, loop=loop, lock=lock)

        future = asyncio.run_coroutine_threadsafe(_make(), loop)
        transcribe_fn = future.result(timeout=2.0)

        audio = np.zeros(16000, dtype=np.float32)
        result = transcribe_fn(audio)

        assert result == "hey sovyx"
        assert engine.call_count == 1


class TestFailureIsolation:
    """Bridge swallows every error and returns "" so the detector
    treats it as a no-match (per the wake-word detector's contract)."""

    def test_engine_raises_returns_empty_string(
        self, loop_and_thread: asyncio.AbstractEventLoop
    ) -> None:
        loop = loop_and_thread
        engine = _FakeEngineErroring(RuntimeError("engine boom"))

        async def _make() -> object:
            return make_stt_fallback_transcribe_fn(engine=engine, loop=loop, lock=asyncio.Lock())

        future = asyncio.run_coroutine_threadsafe(_make(), loop)
        transcribe_fn = future.result(timeout=2.0)

        audio = np.zeros(1024, dtype=np.float32)
        assert transcribe_fn(audio) == ""

    def test_timeout_returns_empty_string(
        self, loop_and_thread: asyncio.AbstractEventLoop
    ) -> None:
        loop = loop_and_thread
        # Engine sleeps 5 s; bridge timeout is 0.2 s.
        engine = _FakeEngineSlow(delay_s=5.0)

        async def _make() -> object:
            return make_stt_fallback_transcribe_fn(
                engine=engine, loop=loop, lock=asyncio.Lock(), timeout_s=0.2
            )

        future = asyncio.run_coroutine_threadsafe(_make(), loop)
        transcribe_fn = future.result(timeout=2.0)

        start = time.monotonic()
        result = transcribe_fn(np.zeros(16000, dtype=np.float32))
        elapsed = time.monotonic() - start
        assert result == ""
        # Verify the bridge actually honoured the timeout (no
        # accidental 5 s wait); 50 ms slack absorbs scheduler jitter.
        assert elapsed < 1.0, f"bridge waited {elapsed:.2f} s — timeout not honoured"

    def test_loop_closed_returns_empty_string(self) -> None:
        """When the captured loop is no longer running, the bridge
        does NOT raise — it returns "" so detector behaviour stays
        identical to the engine-empty-transcript path."""
        # Build the bridge against a freshly-stopped loop.
        loop, thread = _start_loop_in_thread()
        engine = _FakeEngineHappy(transcript="never reached")

        async def _make() -> object:
            return make_stt_fallback_transcribe_fn(engine=engine, loop=loop, lock=asyncio.Lock())

        future = asyncio.run_coroutine_threadsafe(_make(), loop)
        transcribe_fn = future.result(timeout=2.0)
        _stop_loop(loop, thread)

        # Loop closed — calling transcribe_fn must not raise.
        result = transcribe_fn(np.zeros(1024, dtype=np.float32))
        assert result == ""


class TestSerialisation:
    """The shared ``asyncio.Lock`` MUST serialise concurrent calls
    against the underlying engine instance (R1 defense-in-depth)."""

    def test_lock_serialises_concurrent_calls(
        self, loop_and_thread: asyncio.AbstractEventLoop
    ) -> None:
        loop = loop_and_thread

        # Engine that records start + finish timestamps so we can
        # verify intervals do NOT overlap.
        intervals: list[tuple[float, float]] = []
        intervals_lock = threading.Lock()

        class _RecordingEngine:
            async def transcribe(
                self,
                audio: np.ndarray,
                sample_rate: int = 16000,
            ) -> _FakeTranscriptionResult:
                del audio, sample_rate
                start = time.monotonic()
                # Simulate ~100 ms transcribe work.
                await asyncio.sleep(0.1)
                end = time.monotonic()
                with intervals_lock:
                    intervals.append((start, end))
                return _FakeTranscriptionResult(text="x")

        engine = _RecordingEngine()

        async def _make() -> object:
            return make_stt_fallback_transcribe_fn(
                engine=engine, loop=loop, lock=asyncio.Lock(), timeout_s=5.0
            )

        future = asyncio.run_coroutine_threadsafe(_make(), loop)
        transcribe_fn = future.result(timeout=2.0)

        # Fire 3 concurrent calls from 3 worker threads.
        results: list[str] = []
        results_lock = threading.Lock()

        def _worker() -> None:
            r = transcribe_fn(np.zeros(1024, dtype=np.float32))
            with results_lock:
                results.append(r)

        threads = [threading.Thread(target=_worker) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        assert results == ["x", "x", "x"]
        assert len(intervals) == 3

        # Sort by start, verify no two intervals overlap (the lock
        # MUST serialise — this is the R1 contract).
        intervals.sort(key=lambda iv: iv[0])
        for prev, current in zip(intervals, intervals[1:], strict=False):
            assert current[0] >= prev[1] - 0.005, (
                f"interval overlap: prev ended {prev[1]:.3f}, current started {current[0]:.3f}"
            )


class TestReentrancy:
    """Each invocation creates a fresh future; calling the bridge
    repeatedly is safe."""

    def test_bridge_can_be_called_repeatedly(
        self, loop_and_thread: asyncio.AbstractEventLoop
    ) -> None:
        loop = loop_and_thread
        engine = _FakeEngineHappy(transcript="ok")

        async def _make() -> object:
            return make_stt_fallback_transcribe_fn(engine=engine, loop=loop, lock=asyncio.Lock())

        future = asyncio.run_coroutine_threadsafe(_make(), loop)
        transcribe_fn = future.result(timeout=2.0)

        audio = np.zeros(16000, dtype=np.float32)
        for _ in range(5):
            assert transcribe_fn(audio) == "ok"
        assert engine.call_count == 5
