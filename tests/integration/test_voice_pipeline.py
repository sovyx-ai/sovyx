"""Integration test — voice pipeline full mock cycle (no hardware).

Validates the full pipeline state machine: onset → transcription → response → idle.
All hardware components are mocked but the pipeline logic runs for real.
"""

from __future__ import annotations

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
_SAMPLE_RATE = 16000


def _frame(val: int = 0) -> np.ndarray:
    return np.full(_FRAME_LEN, val, dtype=np.int16)


def _speech_frame() -> np.ndarray:
    return np.full(_FRAME_LEN, 1000, dtype=np.int16)


def _silence_frame() -> np.ndarray:
    return np.zeros(_FRAME_LEN, dtype=np.int16)


def _vad_event(speech: bool) -> VADEvent:
    return VADEvent(
        is_speech=speech,
        probability=0.9 if speech else 0.1,
        state=VADState.SPEECH if speech else VADState.SILENCE,
    )


def _audio_chunk(duration_ms: float = 50.0) -> AudioChunk:
    samples = int(22050 * duration_ms / 1000)
    return AudioChunk(
        audio=np.zeros(samples, dtype=np.int16),
        sample_rate=22050,
        duration_ms=duration_ms,
    )


class TestVoicePipelineIntegration:
    """Full pipeline cycle: IDLE → wake → record → transcribe → respond → idle."""

    @pytest.mark.asyncio
    async def test_full_cycle_with_wake_word(self) -> None:
        """Complete voice interaction with wake word detection."""
        # -- Setup mocks --
        vad = MagicMock()
        ww = MagicMock()
        stt = AsyncMock()
        tts = AsyncMock()
        bus = AsyncMock()
        bus.emit = AsyncMock()

        stt_result = MagicMock()
        stt_result.text = "what time is it"
        stt_result.confidence = 0.95
        stt_result.language = "en"
        stt.transcribe.return_value = stt_result

        tts.synthesize.return_value = _audio_chunk(50)

        perception_cb = AsyncMock(return_value="It is three o'clock")

        config = VoicePipelineConfig(
            mind_id="integration-test",
            wake_word_enabled=True,
            barge_in_enabled=False,
            fillers_enabled=False,
            silence_frames_end=2,
            max_recording_frames=20,
        )

        pipeline = VoicePipeline(
            config=config,
            vad=vad,
            wake_word=ww,
            stt=stt,
            tts=tts,
            event_bus=bus,
            on_perception=perception_cb,
        )
        await pipeline.start()

        # -- Phase 1: IDLE, no speech → stays IDLE --
        vad.process_frame.return_value = _vad_event(speech=False)
        ww.process_frame.return_value = MagicMock(detected=False)
        result = await pipeline.feed_frame(_silence_frame())
        assert result["state"] == "IDLE"

        # -- Phase 2: Speech detected, wake word detected → WAKE_DETECTED --
        vad.process_frame.return_value = _vad_event(speech=True)
        ww.process_frame.return_value = MagicMock(detected=True)
        result = await pipeline.feed_frame(_speech_frame())
        assert result["state"] == "WAKE_DETECTED"

        # -- Phase 3: Continue speech → RECORDING --
        vad.process_frame.return_value = _vad_event(speech=True)
        result = await pipeline.feed_frame(_speech_frame())
        assert result["state"] == "RECORDING"

        # -- Phase 4: More speech frames --
        for _ in range(3):
            result = await pipeline.feed_frame(_speech_frame())
            assert result["state"] == "RECORDING"

        # -- Phase 5: Silence → end recording → transcribe --
        vad.process_frame.return_value = _vad_event(speech=False)
        for _ in range(2):  # silence_frames_end=2
            result = await pipeline.feed_frame(_silence_frame())

        # Should have transcribed
        assert stt.transcribe.called
        assert perception_cb.called

        await pipeline.stop()

    @pytest.mark.asyncio
    async def test_full_cycle_without_wake_word(self) -> None:
        """Complete voice interaction without wake word (always-on mode)."""
        vad = MagicMock()
        stt = AsyncMock()
        tts = AsyncMock()
        bus = AsyncMock()
        bus.emit = AsyncMock()

        stt_result = MagicMock()
        stt_result.text = "hello"
        stt_result.confidence = 0.9
        stt_result.language = "en"
        stt.transcribe.return_value = stt_result

        tts.synthesize.return_value = _audio_chunk(50)

        perception_cb = AsyncMock(return_value="Hi there")

        config = VoicePipelineConfig(
            mind_id="integration-no-ww",
            wake_word_enabled=False,
            barge_in_enabled=False,
            fillers_enabled=False,
            silence_frames_end=2,
            max_recording_frames=20,
        )

        pipeline = VoicePipeline(
            config=config,
            vad=vad,
            wake_word=None,
            stt=stt,
            tts=tts,
            event_bus=bus,
            on_perception=perception_cb,
        )
        await pipeline.start()

        # Speech → should go straight to recording
        vad.process_frame.return_value = _vad_event(speech=True)
        result = await pipeline.feed_frame(_speech_frame())
        # Without wake word, speech → RECORDING directly
        assert result["state"] in ("RECORDING", "WAKE_DETECTED")

        # More speech
        for _ in range(3):
            result = await pipeline.feed_frame(_speech_frame())

        # Silence → end
        vad.process_frame.return_value = _vad_event(speech=False)
        for _ in range(2):
            result = await pipeline.feed_frame(_silence_frame())

        assert stt.transcribe.called
        await pipeline.stop()

    @pytest.mark.asyncio
    async def test_pipeline_start_stop_lifecycle(self) -> None:
        """Pipeline start/stop/reset lifecycle."""
        vad = MagicMock()
        stt = AsyncMock()
        tts = AsyncMock()

        config = VoicePipelineConfig(mind_id="lifecycle-test")
        pipeline = VoicePipeline(
            config=config,
            vad=vad,
            wake_word=None,
            stt=stt,
            tts=tts,
        )

        assert not pipeline.is_running
        await pipeline.start()
        assert pipeline.is_running
        assert pipeline.state == VoicePipelineState.IDLE

        pipeline.reset()
        assert pipeline.state == VoicePipelineState.IDLE

        await pipeline.stop()
        assert not pipeline.is_running

    @pytest.mark.asyncio
    async def test_streaming_tts_response(self) -> None:
        """Test streaming text → TTS synthesis flow."""
        vad = MagicMock()
        stt = AsyncMock()
        tts = AsyncMock()
        tts.synthesize.return_value = _audio_chunk(50)

        config = VoicePipelineConfig(
            mind_id="stream-test",
            fillers_enabled=False,
        )
        pipeline = VoicePipeline(
            config=config,
            vad=vad,
            wake_word=None,
            stt=stt,
            tts=tts,
        )
        await pipeline.start()
        pipeline._state = VoicePipelineState.SPEAKING

        # Stream text chunks
        await pipeline.stream_text("Hello world. ")
        await pipeline.stream_text("How are you today? ")
        await pipeline.stream_text("I am fine.")
        await pipeline.flush_stream()

        assert pipeline.state == VoicePipelineState.IDLE
        await pipeline.stop()
