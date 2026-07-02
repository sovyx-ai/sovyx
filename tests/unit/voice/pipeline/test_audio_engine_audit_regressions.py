"""Regression tests for the 2026-07-02 audio-engine cross-platform audit.

One test class per confirmed pipeline finding (register:
MISSION-AUDIO-ENGINE-CROSS-PLATFORM-AUDIT-2026-07-02-FINDINGS, IDs
PIPELINE-1/4/5/6/7/10 — PIPELINE-2/3 counter semantics live in
``tests/unit/voice/test_pipeline.py`` next to the pre-existing
barge-in coverage, PIPELINE-8 in ``test_audio.py``, PIPELINE-9 in
``test_capture_task.py``).

Debugging Rule #13 discipline: these tests exercise REAL asyncio
timing (event-gated fake TTS, real task cancellation, real chain
runs) — the defects they pin were invisible to fully-mocked seams.
Debugging Rule #12 discipline: every started pipeline is stopped and
every spawned task is drained before the test returns.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

from sovyx.voice.pipeline import (
    VoicePipeline,
    VoicePipelineConfig,
    VoicePipelineState,
)
from sovyx.voice.tts_piper import AudioChunk
from sovyx.voice.vad import VADEvent, VADState

_FRAME_LEN = 512


def _speech_frame() -> np.ndarray:
    return np.full(_FRAME_LEN, 1000, dtype=np.int16)


def _audio_chunk(duration_ms: float = 20.0) -> AudioChunk:
    samples = int(22050 * duration_ms / 1000)
    return AudioChunk(
        audio=np.zeros(samples, dtype=np.int16),
        sample_rate=22050,
        duration_ms=duration_ms,
    )


def _make_vad(speech: bool = False) -> MagicMock:
    vad = MagicMock()
    vad.process_frame.return_value = VADEvent(
        is_speech=speech,
        probability=0.9 if speech else 0.1,
        state=VADState.SPEECH if speech else VADState.SILENCE,
    )
    return vad


class _GatedTTS:
    """Real-timing TTS double: synthesis parks on an event.

    Lets a test cancel the outer task (or the inner tracked synth
    task) while ``stream_text`` is genuinely parked at its
    longest-dwell await — the exact window PIPELINE-1 targets.

    ``armed`` starts False so ``pipeline.start()``'s Jarvis
    ``pre_cache()`` synthesis completes instantly; tests arm the gate
    AFTER start so only the turn-path synthesis parks.
    """

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.armed = False
        self.synth_calls = 0

    async def synthesize(self, text: str) -> AudioChunk:  # noqa: ARG002
        if not self.armed:
            return _audio_chunk()
        self.synth_calls += 1
        self.started.set()
        await self.release.wait()
        return _audio_chunk()


def _make_pipeline(
    *,
    vad_speech: bool = False,
    barge_in_threshold: int = 1,
    tts: Any | None = None,
) -> tuple[VoicePipeline, dict[str, Any]]:
    config = VoicePipelineConfig(
        mind_id="test-mind",
        wake_word_enabled=True,
        barge_in_enabled=True,
        fillers_enabled=False,
        filler_delay_ms=100,
        silence_frames_end=3,
        max_recording_frames=10,
        barge_in_threshold=barge_in_threshold,
    )
    if tts is None:
        tts = AsyncMock()
        tts.synthesize.return_value = _audio_chunk()
    vad = _make_vad(speech=vad_speech)
    ww = MagicMock()
    ww_event = MagicMock()
    ww_event.detected = False
    ww.process_frame.return_value = ww_event
    stt = AsyncMock()
    bus = AsyncMock()
    pipeline = VoicePipeline(
        config=config,
        vad=vad,
        wake_word=ww,
        stt=stt,
        tts=tts,
        event_bus=bus,
        on_perception=None,
    )
    return pipeline, {"vad": vad, "tts": tts, "bus": bus}


async def _wait_for(predicate: Any, timeout_s: float = 2.0) -> None:
    """Poll ``predicate()`` until truthy (real-timing helper)."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            msg = "condition not reached within timeout"
            raise AssertionError(msg)
        await asyncio.sleep(0.001)


# ===========================================================================
# PIPELINE-1 — stream_text must not swallow TASK-level CancelledError
# ===========================================================================


class TestStreamTextCancellationDiscrimination:
    """PIPELINE-1(a) — the CancelledError handler distinguishes an
    inner-synth-task cancel (swallow, survive) from a cancel of the
    task RUNNING stream_text (re-raise, end cancelled)."""

    @pytest.mark.asyncio
    async def test_task_level_cancel_propagates(self) -> None:
        """Barge-in chain step 2.5/3 cancels the cogloop task while it
        is parked at the synth await → the cancellation MUST propagate
        (pre-fix it was eaten and the LLM kept streaming)."""
        tts = _GatedTTS()
        pipeline, _refs = _make_pipeline(tts=tts)
        await pipeline.start()
        tts.armed = True
        try:
            # One COMPLETE segment + a >=3-word remainder: split_at_boundaries
            # only emits segments[:-1] to TTS (the tail stays buffered), so
            # the text must contain a finished sentence for synthesis to run.
            task = asyncio.create_task(pipeline.stream_text("Hello there world. And more words"))
            await asyncio.wait_for(tts.started.wait(), timeout=2.0)

            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert task.cancelled(), (
                "stream_text swallowed a TASK-level cancellation — the "
                "cogloop task would keep streaming and re-assert SPEAKING "
                "over the user's RECORDING (PIPELINE-1 / #69)"
            )
            assert pipeline._text_buffer == ""
        finally:
            tts.release.set()
            await pipeline.stop()

    @pytest.mark.asyncio
    async def test_inner_synth_cancel_still_swallowed(self) -> None:
        """Chain step 2 cancels only the tracked TTS task — stream_text
        must survive (swallow + clean buffer) so the caller continues."""
        tts = _GatedTTS()
        pipeline, _refs = _make_pipeline(tts=tts)
        await pipeline.start()
        tts.armed = True
        try:
            # One COMPLETE segment + a >=3-word remainder: split_at_boundaries
            # only emits segments[:-1] to TTS (the tail stays buffered), so
            # the text must contain a finished sentence for synthesis to run.
            task = asyncio.create_task(pipeline.stream_text("Hello there world. And more words"))
            await asyncio.wait_for(tts.started.wait(), timeout=2.0)
            await _wait_for(lambda: len(pipeline._in_flight_tts_tasks) > 0)

            inner = next(iter(pipeline._in_flight_tts_tasks))
            inner.cancel()

            result = await asyncio.wait_for(task, timeout=2.0)
            assert result is None
            assert not task.cancelled(), (
                "an inner-synth-only cancel must NOT cancel the stream task"
            )
            assert pipeline._text_buffer == ""
            with contextlib.suppress(asyncio.CancelledError):
                await inner
        finally:
            tts.release.set()
            await pipeline.stop()

    @pytest.mark.asyncio
    async def test_recording_state_drops_chunk(self) -> None:
        """PIPELINE-1(b) — once barge-in handed the turn to RECORDING,
        a late LLM chunk is dropped: no SPEAKING re-assert, no session
        re-open, no re-duck, no synthesis."""
        tts = _GatedTTS()
        pipeline, _refs = _make_pipeline(tts=tts)
        gate = MagicMock()
        pipeline._self_feedback_gate = gate
        await pipeline.start()
        tts.armed = True
        try:
            pipeline._state = VoicePipelineState.RECORDING
            pipeline._speech_session_active = False

            await pipeline.stream_text("rogue late chunk. ")

            assert pipeline._state is VoicePipelineState.RECORDING
            assert pipeline._speech_session_active is False
            assert tts.synth_calls == 0
            gate.on_tts_start.assert_not_called()
        finally:
            await pipeline.stop()


# ===========================================================================
# PIPELINE-4 — no barge-in path may hold a stale VAD after swap_vad
# ===========================================================================


class TestSwapVadBargeInCoherence:
    """PIPELINE-4 — after the C1 L2 swap, every inference (including
    the barge-in gate, which now reuses feed_frame's verdict) runs on
    the swapped instance."""

    @pytest.mark.asyncio
    async def test_barge_in_uses_swapped_vad(self) -> None:
        pipeline, refs = _make_pipeline(vad_speech=True, barge_in_threshold=1)
        await pipeline.start()
        try:
            new_vad = _make_vad(speech=True)
            new_vad.model_path = "fake"
            new_vad.config = None
            await pipeline.swap_vad(new_vad)
            old_calls = refs["vad"].process_frame.call_count

            pipeline._state = VoicePipelineState.SPEAKING
            pipeline._output._playing = True
            result = await pipeline.feed_frame(_speech_frame())

            assert result["state"] == "RECORDING"
            assert new_vad.process_frame.call_count == 1
            assert refs["vad"].process_frame.call_count == old_calls, (
                "the discarded pre-swap VAD instance was consulted after an L2 swap (PIPELINE-4)"
            )
            # Structural pin: the detector holds no VAD reference at all.
            assert not hasattr(pipeline._barge_in, "_vad")
        finally:
            await pipeline.stop()


# ===========================================================================
# PIPELINE-5 — stop() quiesces cogloop producers; speak/stream_text
# refuse a stopped pipeline
# ===========================================================================


class TestStopQuiescesCogloopProducers:
    @pytest.mark.asyncio
    async def test_stop_cancels_registered_cogloop_tasks(self) -> None:
        pipeline, _refs = _make_pipeline()
        await pipeline.start()
        started = asyncio.Event()

        async def _fake_bridge() -> None:
            started.set()
            await asyncio.sleep(30)

        bridge_task = asyncio.create_task(_fake_bridge())
        pipeline.register_cogloop_task(bridge_task)
        await asyncio.wait_for(started.wait(), timeout=2.0)

        await pipeline.stop()

        assert bridge_task.done()
        assert bridge_task.cancelled(), (
            "stop() left the cogloop bridge task streaming into a stopped pipeline (PIPELINE-5)"
        )
        with contextlib.suppress(asyncio.CancelledError):
            await bridge_task

    @pytest.mark.asyncio
    async def test_speak_after_stop_is_noop(self) -> None:
        pipeline, refs = _make_pipeline()
        gate = MagicMock()
        pipeline._self_feedback_gate = gate
        await pipeline.start()
        await pipeline.stop()
        gate.reset_mock()
        refs["tts"].synthesize.reset_mock()  # start()'s pre_cache synthesised

        await pipeline.speak("hello after stop")

        assert pipeline.state is VoicePipelineState.IDLE
        assert pipeline._speech_session_active is False
        refs["tts"].synthesize.assert_not_awaited()
        gate.on_tts_start.assert_not_called()

    @pytest.mark.asyncio
    async def test_stream_text_after_stop_is_noop(self) -> None:
        pipeline, refs = _make_pipeline()
        await pipeline.start()
        await pipeline.stop()
        refs["tts"].synthesize.reset_mock()  # start()'s pre_cache synthesised

        await pipeline.stream_text("hello after stop. ")

        assert pipeline.state is VoicePipelineState.IDLE
        assert pipeline._speech_session_active is False
        assert pipeline._text_buffer == ""
        refs["tts"].synthesize.assert_not_awaited()


# ===========================================================================
# PIPELINE-6 — a pending filler cannot resurrect playback inside the
# cancellation chain's awaited window
# ===========================================================================


class TestFillerCannotResurrectInterruptedPlayback:
    @pytest.mark.asyncio
    async def test_filler_cancelled_before_first_chain_await(self) -> None:
        """A filler parked one loop-tick away MUST be cancelled by the
        chain's synchronous step 1.1 — pre-fix it fired during step 2's
        await, and its play_immediate cleared the interrupt flag."""
        pipeline, _refs = _make_pipeline()
        await pipeline.start()
        try:
            # Wedge step 2 open: a tracked TTS task that is slow to
            # honour cancellation, giving a pending filler ample loop
            # cycles to wake inside the chain window.
            async def _wedged() -> None:
                try:
                    await asyncio.sleep(30)
                except asyncio.CancelledError:
                    for _ in range(20):
                        await asyncio.sleep(0)
                    raise

            wedged_task = asyncio.create_task(_wedged())
            pipeline._in_flight_tts_tasks.add(wedged_task)
            await asyncio.sleep(0)  # let the wedge park

            fired: list[bool] = []

            async def _filler_body() -> bool:
                # One tick from running — exactly the pre-fix window.
                await asyncio.sleep(0)
                fired.append(True)
                await pipeline._output.play_immediate(_audio_chunk(5))
                return True

            filler_task = asyncio.create_task(_filler_body())
            pipeline._filler_task = filler_task
            # Do NOT yield here: the filler is scheduled but has not
            # started — mirroring a delay that expires mid-chain.

            await pipeline.cancel_speech_chain(reason="barge_in")

            assert fired == [], (
                "the pending filler ran inside the cancellation chain "
                "window and resurrected playback (PIPELINE-6)"
            )
            assert pipeline._output._interrupted is True, (
                "the barge-in interrupt flag was cleared mid-chain"
            )
            assert pipeline._filler_task is None
            with contextlib.suppress(asyncio.CancelledError):
                await filler_task
            with contextlib.suppress(asyncio.CancelledError):
                await wedged_task
            pipeline._in_flight_tts_tasks.discard(wedged_task)
        finally:
            await pipeline.stop()


# ===========================================================================
# PIPELINE-7 — barge-in during a streaming turn's playback gaps
# ===========================================================================


class TestBargeInDuringPlaybackGaps:
    @pytest.mark.asyncio
    async def test_speech_in_gap_triggers_barge_in(self) -> None:
        """Session open + is_playing False (between segments / LLM
        stall) + sustained speech → chain fires and the turn hands to
        RECORDING. Pre-fix those frames were silently discarded."""
        pipeline, _refs = _make_pipeline(vad_speech=True, barge_in_threshold=2)
        await pipeline.start()
        try:
            pipeline._state = VoicePipelineState.SPEAKING
            pipeline._speech_session_active = True
            assert pipeline._output.is_playing is False

            first = await pipeline.feed_frame(_speech_frame())
            assert first["state"] == "SPEAKING"  # below sustain threshold
            result = await pipeline.feed_frame(_speech_frame())

            assert result["state"] == "RECORDING"
            assert result["event"] == "barge_in_recording"
            assert pipeline._speech_session_active is False
        finally:
            await pipeline.stop()

    @pytest.mark.asyncio
    async def test_closed_session_not_playing_still_falls_back_to_idle(self) -> None:
        """Guard the pre-existing fallback: session closed + playback
        finished → IDLE hand-off unchanged."""
        pipeline, _refs = _make_pipeline(vad_speech=True, barge_in_threshold=2)
        await pipeline.start()
        try:
            pipeline._state = VoicePipelineState.SPEAKING
            pipeline._speech_session_active = False

            result = await pipeline.feed_frame(_speech_frame())

            assert result["state"] == "IDLE"
            assert result["event"] == "tts_completed"
        finally:
            await pipeline.stop()


# ===========================================================================
# PIPELINE-10 — reset() completes the turn-state recovery
# ===========================================================================


class TestResetCompletesTurnState:
    @pytest.mark.asyncio
    async def test_reset_clears_session_state_and_releases_duck(self) -> None:
        pipeline, _refs = _make_pipeline()
        gate = MagicMock()
        pipeline._self_feedback_gate = gate
        await pipeline.start()
        try:
            pipeline._state = VoicePipelineState.SPEAKING
            pipeline._speech_session_active = True
            pipeline._mint_new_utterance_id()
            drainer = asyncio.create_task(asyncio.sleep(30))
            pipeline._stream_drain_task = drainer
            pipeline._barge_in.observe(is_speech=True)

            pipeline.reset()

            assert pipeline.state is VoicePipelineState.IDLE
            assert pipeline._speech_session_active is False
            assert pipeline._current_utterance_id == ""
            assert pipeline._stream_drain_task is None
            assert pipeline._barge_in.frames_sustained == 0
            gate.on_tts_end.assert_called_once()
            with contextlib.suppress(asyncio.CancelledError):
                await drainer
            assert drainer.cancelled()
        finally:
            await pipeline.stop()
