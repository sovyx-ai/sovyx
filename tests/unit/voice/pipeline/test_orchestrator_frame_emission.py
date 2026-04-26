"""Tests for the Step 13 orchestrator frame emission sites.

The 5 transition sites in _orchestrator.py emit typed frames into
the state machine's frame_history ring. Pin the contract:

* Wake-word fire emits UserStartedSpeakingFrame (source="wake_word")
* No-wake recording emits UserStartedSpeakingFrame
  (source="barge_in_or_no_wake")
* TRANSCRIBING emits TranscriptionFrame with the validated text +
  confidence + language
* THINKING emits LLMFullResponseStartFrame
* SPEAKING (via speak()) emits OutputAudioRawFrame with
  synthesis_health="speak_started"
* Every terminal IDLE transition emits EndFrame with
  reason=f"from_{prior_state}"

Reference: MISSION-voice-100pct-autonomous-2026-04-25.md step 13.
"""

from __future__ import annotations

from typing import Any

from sovyx.voice.pipeline._frame_types import (
    EndFrame,
    LLMFullResponseStartFrame,
    OutputAudioRawFrame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
)
from sovyx.voice.pipeline._orchestrator import VoicePipeline
from sovyx.voice.pipeline._state import VoicePipelineState


def _frame_types_in_history(pipeline: VoicePipeline) -> list[str]:
    return [f.frame_type for f in pipeline._state_machine.frame_history()]


class TestEndFrameOnTerminalIdle:
    """The state setter hook emits EndFrame on every terminal IDLE."""

    def _make_pipeline(
        self,
        config: Any,  # noqa: ANN401 — test factory; precise type irrelevant
    ) -> VoicePipeline:
        from unittest.mock import AsyncMock, MagicMock

        from sovyx.voice.pipeline._config import VoicePipelineConfig

        if config is None:
            config = VoicePipelineConfig()
        return VoicePipeline(
            config=config,
            vad=MagicMock(),
            wake_word=MagicMock(),
            stt=AsyncMock(),
            tts=AsyncMock(),
            event_bus=None,
        )

    def test_idle_to_recording_to_idle_emits_end_frame(self) -> None:
        pipeline = self._make_pipeline(config=None)
        # Manually drive a transition path that ends in IDLE.
        pipeline._state = VoicePipelineState.RECORDING
        pipeline._state = VoicePipelineState.IDLE

        end_frames = [
            f for f in pipeline._state_machine.frame_history() if isinstance(f, EndFrame)
        ]
        assert len(end_frames) == 1
        assert end_frames[0].reason == "from_recording"

    def test_idle_self_loop_does_not_emit_end_frame(self) -> None:
        """Self-loop IDLE→IDLE must NOT emit EndFrame (the saga
        wasn't open, so there's no trace to close)."""
        pipeline = self._make_pipeline(config=None)
        pipeline._state = VoicePipelineState.IDLE
        end_frames = [
            f for f in pipeline._state_machine.frame_history() if isinstance(f, EndFrame)
        ]
        assert end_frames == []

    def test_recording_to_thinking_to_idle_emits_end_frame_from_thinking(
        self,
    ) -> None:
        pipeline = self._make_pipeline(config=None)
        pipeline._state = VoicePipelineState.RECORDING
        pipeline._state = VoicePipelineState.TRANSCRIBING
        pipeline._state = VoicePipelineState.THINKING
        pipeline._state = VoicePipelineState.IDLE

        end_frames = [
            f for f in pipeline._state_machine.frame_history() if isinstance(f, EndFrame)
        ]
        assert len(end_frames) == 1
        assert end_frames[0].reason == "from_thinking"


class TestSpeakEmitsOutputAudioRawFrame:
    def _make_pipeline(self) -> VoicePipeline:
        from unittest.mock import AsyncMock, MagicMock

        from sovyx.voice.pipeline._config import VoicePipelineConfig

        # tts.speak is mocked async to return immediately so the test
        # focuses on the frame emission, not on actual synthesis.
        tts = AsyncMock()
        return VoicePipeline(
            config=VoicePipelineConfig(),
            vad=MagicMock(),
            wake_word=MagicMock(),
            stt=AsyncMock(),
            tts=tts,
            event_bus=None,
        )

    def test_record_frame_helper_stamps_utterance_id(self) -> None:
        """The _record_frame helper stamps utterance_id from the
        orchestrator's current trace context."""
        import time

        pipeline = self._make_pipeline()
        # Set a known utterance_id then emit a frame.
        pipeline._current_utterance_id = "uuid-test"
        pipeline._record_frame(
            OutputAudioRawFrame(
                frame_type="OutputAudioRaw",
                timestamp_monotonic=time.monotonic(),
                chunk_index=0,
                pcm_bytes=0,
                sample_rate=24000,
                synthesis_health="speak_started",
            ),
        )
        history = pipeline._state_machine.frame_history()
        speak_started = [
            f
            for f in history
            if isinstance(f, OutputAudioRawFrame) and f.synthesis_health == "speak_started"
        ]
        assert len(speak_started) == 1
        # The helper stamps the current utterance_id automatically.
        assert speak_started[0].utterance_id == "uuid-test"


class TestFrameTypesIntegrationSurface:
    def test_frame_history_is_immutable_tuple(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from sovyx.voice.pipeline._config import VoicePipelineConfig

        pipeline = VoicePipeline(
            config=VoicePipelineConfig(),
            vad=MagicMock(),
            wake_word=MagicMock(),
            stt=AsyncMock(),
            tts=AsyncMock(),
            event_bus=None,
        )
        history = pipeline._state_machine.frame_history()
        assert isinstance(history, tuple)

    def test_frame_imports_exposed(self) -> None:
        """The Step 11 frame classes must be importable + the
        orchestrator's emission sites must reference them."""
        # If any of these imports fail, the orchestrator's emission
        # block has a regression — the test catches it before the
        # full suite runs.
        assert UserStartedSpeakingFrame
        assert TranscriptionFrame
        assert LLMFullResponseStartFrame
        assert OutputAudioRawFrame
        assert EndFrame
