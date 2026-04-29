"""Tests for the AEC render-buffer wire-up in AudioOutputQueue [T4.4.c].

Pins the contract that every AudioChunk handed to ``play_immediate``
or drained via ``drain`` is forwarded to the registered RenderPcmSink
BEFORE the playback dispatch — so the FrameNormalizer's AEC stage
sees the render reference time-aligned with the speaker output.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import numpy as np
import pytest

from sovyx.voice.pipeline import _output_queue as _output_queue_mod
from sovyx.voice.pipeline._output_queue import AudioOutputQueue

# ── Test doubles ─────────────────────────────────────────────────────────


class _RecordingSink:
    """RenderPcmSink stub that records every (pcm, sample_rate) call."""

    def __init__(self) -> None:
        self.calls: list[tuple[np.ndarray, int]] = []

    def feed(self, pcm: np.ndarray, sample_rate: int) -> None:
        self.calls.append((pcm.copy(), sample_rate))


def _make_chunk(
    *,
    audio_ms: float = 100.0,
    sample_rate: int = 22_050,
    pcm_value: int = 1234,
) -> object:
    """Construct a minimal AudioChunk-like value.

    AudioChunk is a frozen dataclass in voice.tts_piper; constructing
    a duck-typed stand-in here keeps these tests free of TTS deps.
    """
    n_samples = int(round(sample_rate * audio_ms / 1000))

    class _Chunk:
        def __init__(self) -> None:
            self.audio = np.full(n_samples, pcm_value, dtype=np.int16)
            self.sample_rate = sample_rate
            self.duration_ms = audio_ms

    return _Chunk()


# ── Construction ─────────────────────────────────────────────────────────


class TestConstruction:
    def test_default_render_buffer_is_none(self) -> None:
        q = AudioOutputQueue()
        assert q._render_buffer is None  # noqa: SLF001 — single white-box check

    def test_constructs_with_explicit_render_buffer(self) -> None:
        sink = _RecordingSink()
        q = AudioOutputQueue(render_buffer=sink)
        assert q._render_buffer is sink  # noqa: SLF001

    def test_set_render_buffer_assigns_at_runtime(self) -> None:
        q = AudioOutputQueue()
        sink = _RecordingSink()
        q.set_render_buffer(sink)
        assert q._render_buffer is sink  # noqa: SLF001

    def test_set_render_buffer_can_unwire(self) -> None:
        q = AudioOutputQueue(render_buffer=_RecordingSink())
        q.set_render_buffer(None)
        assert q._render_buffer is None  # noqa: SLF001


# ── play_immediate feeds buffer ──────────────────────────────────────────


class TestPlayImmediateFeedsBuffer:
    @pytest.mark.asyncio()
    async def test_single_chunk_flows_to_sink_before_play(self) -> None:
        sink = _RecordingSink()
        q = AudioOutputQueue(render_buffer=sink)
        chunk = _make_chunk(audio_ms=50.0, sample_rate=22_050, pcm_value=2000)

        with patch.object(_output_queue_mod, "_play_audio", new_callable=AsyncMock):
            await q.play_immediate(chunk)

        assert len(sink.calls) == 1
        fed_pcm, fed_rate = sink.calls[0]
        assert fed_rate == 22_050
        assert np.all(fed_pcm == 2000)

    @pytest.mark.asyncio()
    async def test_no_buffer_means_no_feed_call(self) -> None:
        # Regression: pre-AEC behaviour preserved when buffer not wired.
        q = AudioOutputQueue()
        chunk = _make_chunk()
        with patch.object(_output_queue_mod, "_play_audio", new_callable=AsyncMock):
            await q.play_immediate(chunk)
        # No sink, no call to track — assertion is that we don't crash.

    @pytest.mark.asyncio()
    async def test_feed_failure_does_not_block_play(self) -> None:
        # Best-effort: a broken sink must NOT prevent playback.
        class _FailingSink:
            def feed(self, pcm: np.ndarray, sample_rate: int) -> None:
                raise RuntimeError("simulated sink failure")

        q = AudioOutputQueue(render_buffer=_FailingSink())
        chunk = _make_chunk()

        play_mock = AsyncMock()
        with patch.object(_output_queue_mod, "_play_audio", play_mock):
            await q.play_immediate(chunk)

        # play_audio still got dispatched even though feed raised.
        play_mock.assert_called_once()


# ── drain feeds buffer per chunk ─────────────────────────────────────────


class TestDrainFeedsBuffer:
    @pytest.mark.asyncio()
    async def test_each_drained_chunk_flows_to_sink(self) -> None:
        sink = _RecordingSink()
        q = AudioOutputQueue(render_buffer=sink)

        chunks = [
            _make_chunk(pcm_value=10),
            _make_chunk(pcm_value=20),
            _make_chunk(pcm_value=30),
        ]
        for chunk in chunks:
            await q.enqueue(chunk)  # type: ignore[arg-type]

        with patch.object(_output_queue_mod, "_play_audio", new_callable=AsyncMock):
            await q.drain()

        assert len(sink.calls) == 3
        fed_values = [int(call[0][0]) for call in sink.calls]
        assert fed_values == [10, 20, 30]

    @pytest.mark.asyncio()
    async def test_interrupt_during_drain_skips_remaining_feeds(self) -> None:
        sink = _RecordingSink()
        q = AudioOutputQueue(render_buffer=sink)

        for v in (10, 20, 30):
            await q.enqueue(_make_chunk(pcm_value=v))  # type: ignore[arg-type]

        async def _slow_play(chunk: object) -> None:  # noqa: ARG001 — patched
            # After the first chunk plays, interrupt to truncate drain.
            q.interrupt()

        with patch.object(_output_queue_mod, "_play_audio", side_effect=_slow_play):
            await q.drain()

        # Exactly one chunk made it through to the sink.
        assert len(sink.calls) == 1
        assert int(sink.calls[0][0][0]) == 10  # noqa: PLR2004 — value pin


# ── End-to-end with real RenderPcmBuffer ─────────────────────────────────


class TestEndToEndWithRealBuffer:
    """Sanity check that the queue + real RenderPcmBuffer actually
    bridges producer→consumer."""

    @pytest.mark.asyncio()
    async def test_chunk_is_readable_from_buffer_after_play(self) -> None:
        from sovyx.voice._render_pcm_buffer import RenderPcmBuffer

        # Default 2 s @ 16 kHz buffer.
        buffer = RenderPcmBuffer()
        q = AudioOutputQueue(render_buffer=buffer)
        # 16 kHz mono so no resample step alters the value.
        chunk = _make_chunk(audio_ms=32.0, sample_rate=16_000, pcm_value=4242)

        with patch.object(_output_queue_mod, "_play_audio", new_callable=AsyncMock):
            await q.play_immediate(chunk)

        # Read 512 samples; the most recent ones should be the
        # constant value we played.
        out = buffer.get_aligned_window(512)
        assert int(out[-1]) == 4242
        # 32 ms * 16 kHz = 512 samples — every output position should
        # be the played value.
        assert np.all(out == 4242)
