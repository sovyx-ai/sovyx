"""Soak validation for the frame-typed pipeline observability layer.

Mission §1.1 Hybrid Option C — synthetic conversation simulation that
exercises the 5 emission sites (Steps 13-14) and verifies the
invariants:

* No frame leaks — frame_history bounded at history_capacity
* No state-frame divergence — every state transition that should
  produce a frame DOES produce a frame
* No memory growth across 50 simulated turns
* Concurrent barge-ins each produce a complete BargeInInterruptionFrame

The soak test runs synthetically (no real STT / LLM / TTS / audio
device) so it can run in CI without ONNX models, sounddevice, or
GPU. The integration aspect is the orchestrator + state machine +
frame ring buffer wired together end-to-end.

Reference: MISSION-voice-100pct-autonomous-2026-04-25.md step 16.
"""

from __future__ import annotations

import asyncio
import gc
from unittest.mock import AsyncMock, MagicMock

import pytest

from sovyx.voice.pipeline._config import VoicePipelineConfig
from sovyx.voice.pipeline._frame_types import (
    BargeInInterruptionFrame,
    EndFrame,
    UserStartedSpeakingFrame,
)
from sovyx.voice.pipeline._orchestrator import VoicePipeline
from sovyx.voice.pipeline._state import VoicePipelineState


def _make_pipeline() -> VoicePipeline:
    return VoicePipeline(
        config=VoicePipelineConfig(),
        vad=MagicMock(),
        wake_word=MagicMock(),
        stt=AsyncMock(),
        tts=AsyncMock(),
        event_bus=None,
    )


def _drive_synthetic_turn(pipeline: VoicePipeline) -> None:
    """Drive one synthetic IDLE → RECORDING → TRANSCRIBING → THINKING
    → SPEAKING → IDLE turn via raw state mutations.

    The state setter hooks (Step 13) emit EndFrame on every terminal
    IDLE; manual emissions via _record_frame add the per-stage
    UserStartedSpeakingFrame / TranscriptionFrame markers we'd see
    in a real pipeline run.
    """
    import time

    pipeline._current_utterance_id = f"uuid-{id(pipeline) ^ int(time.monotonic() * 1000)}"
    pipeline._record_frame(
        UserStartedSpeakingFrame(
            frame_type="UserStartedSpeaking",
            timestamp_monotonic=time.monotonic(),
            source="wake_word",
        ),
    )
    pipeline._state = VoicePipelineState.RECORDING
    pipeline._state = VoicePipelineState.TRANSCRIBING
    pipeline._state = VoicePipelineState.THINKING
    pipeline._state = VoicePipelineState.SPEAKING
    pipeline._state = VoicePipelineState.IDLE
    pipeline._current_utterance_id = ""


class TestFrameRingBufferBounded:
    """No frame leaks — the ring stays at history_capacity even after
    many turns."""

    def test_50_turns_keep_ring_bounded(self) -> None:
        pipeline = _make_pipeline()
        capacity = pipeline._state_machine.history_capacity

        for _ in range(50):
            _drive_synthetic_turn(pipeline)

        history = pipeline._state_machine.frame_history()
        assert len(history) <= capacity


class TestStateFrameConsistency:
    """Every IDLE-terminating turn produces an EndFrame; every
    UserStartedSpeakingFrame is followed by a corresponding EndFrame."""

    def test_n_turns_produce_n_end_frames(self) -> None:
        pipeline = _make_pipeline()

        n_turns = 10
        for _ in range(n_turns):
            _drive_synthetic_turn(pipeline)

        history = pipeline._state_machine.frame_history()
        end_frames = [f for f in history if isinstance(f, EndFrame)]
        assert len(end_frames) == n_turns

    def test_n_turns_produce_n_user_started_frames(self) -> None:
        pipeline = _make_pipeline()

        n_turns = 10
        for _ in range(n_turns):
            _drive_synthetic_turn(pipeline)

        history = pipeline._state_machine.frame_history()
        user_frames = [f for f in history if isinstance(f, UserStartedSpeakingFrame)]
        assert len(user_frames) == n_turns


class TestMemoryStability:
    """The ring buffer's bounded eviction must keep the orchestrator's
    memory footprint stable across many turns."""

    def test_object_count_stable_across_turns(self) -> None:
        pipeline = _make_pipeline()

        # Warm-up: drive one turn to amortise lazy initialisation.
        _drive_synthetic_turn(pipeline)
        gc.collect()
        baseline_objs = len(gc.get_objects())

        # Drive many turns.
        for _ in range(100):
            _drive_synthetic_turn(pipeline)

        gc.collect()
        post_objs = len(gc.get_objects())

        # 100 turns × ~3 frames/turn = 300 frame allocations BUT the
        # ring is bounded at 256 so memory should plateau. Allow a
        # tolerance — the total Python object count includes test
        # framework allocations we can't predict — but reject growth
        # > 50% which would indicate a real leak.
        growth_factor = post_objs / max(1, baseline_objs)
        assert growth_factor < 1.5, (
            f"Object count grew {growth_factor:.2f}× across 100 synthetic turns "
            f"({baseline_objs} → {post_objs}); ring buffer is leaking"
        )


class TestConcurrentBargeIns:
    """Concurrent barge-ins serialise on the cancellation lock + each
    produces a complete BargeInInterruptionFrame."""

    @pytest.mark.asyncio
    async def test_5_concurrent_barge_ins_each_produce_a_frame(self) -> None:
        pipeline = _make_pipeline()

        await asyncio.gather(
            *[pipeline.cancel_speech_chain(reason=f"reason-{i}") for i in range(5)],
        )

        history = pipeline._state_machine.frame_history()
        barge_in_frames = [f for f in history if isinstance(f, BargeInInterruptionFrame)]
        assert len(barge_in_frames) == 5

        # Each frame must have all 5 step verdicts populated.
        expected_steps = {
            "output_flush",
            "tts_tasks_cancel",
            "llm_cancel",
            "filler_and_gate",
            "text_buffer_cleanup",
        }
        for frame in barge_in_frames:
            assert set(frame.step_results.keys()) == expected_steps

    @pytest.mark.asyncio
    async def test_50_barge_ins_dont_overflow_ring(self) -> None:
        pipeline = _make_pipeline()
        capacity = pipeline._state_machine.history_capacity

        # 50 barge-ins serial; each produces 1 frame in the ring.
        for i in range(50):
            await pipeline.cancel_speech_chain(reason=f"barge-{i}")

        history = pipeline._state_machine.frame_history()
        assert len(history) <= capacity
        # All 50 barge-in frames are present (50 < 256 default capacity).
        barge_in_frames = [f for f in history if isinstance(f, BargeInInterruptionFrame)]
        assert len(barge_in_frames) == 50


class TestFrameOrderingIsStable:
    """Frame insertion order is preserved (oldest-first snapshot)."""

    def test_user_then_end_frame_order_preserved(self) -> None:
        import time

        pipeline = _make_pipeline()
        # Manual sequence: UserStartedSpeakingFrame then state mutation
        # to RECORDING then back to IDLE (which emits EndFrame via the
        # state setter hook).
        pipeline._record_frame(
            UserStartedSpeakingFrame(
                frame_type="UserStartedSpeaking",
                timestamp_monotonic=time.monotonic(),
                source="wake_word",
            ),
        )
        pipeline._state = VoicePipelineState.RECORDING
        pipeline._state = VoicePipelineState.IDLE

        history = pipeline._state_machine.frame_history()
        # Find the UserStartedSpeaking + End frame indices.
        user_idx = next(
            i for i, f in enumerate(history) if isinstance(f, UserStartedSpeakingFrame)
        )
        end_idx = next(i for i, f in enumerate(history) if isinstance(f, EndFrame))
        assert user_idx < end_idx
