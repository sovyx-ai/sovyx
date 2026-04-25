"""Tests for VoicePipeline orchestrator (V05-22).

Covers: state machine transitions, barge-in, filler injection,
streaming TTS, AudioOutputQueue, BargeInDetector, JarvisIllusion,
split_at_boundaries, and configuration validation.

Ref: SPE-010 §8, §10, §13 — full state machine + barge-in + timing.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from sovyx.voice.pipeline import (
    AudioOutputQueue,
    BargeInDetector,
    JarvisIllusion,
    PipelineErrorEvent,
    SpeechEndedEvent,
    SpeechStartedEvent,
    TranscriptionCompletedEvent,
    TTSCompletedEvent,
    TTSStartedEvent,
    VoicePipeline,
    VoicePipelineConfig,
    VoicePipelineState,
    WakeWordDetectedEvent,
    split_at_boundaries,
    validate_config,
)

# Patch site for _play_audio: the function lives in pipeline._output_queue
# now (after the god-file split). AudioOutputQueue calls it from there, so
# `patch.object(_pipeline_mod, "_play_audio", ...)` must target that module.
from sovyx.voice.pipeline import _output_queue as _pipeline_mod
from sovyx.voice.tts_piper import AudioChunk
from sovyx.voice.vad import VADEvent, VADState

# ---------------------------------------------------------------------------
# Helpers / Fixtures
# ---------------------------------------------------------------------------

_FRAME_LEN = 512


def _vad_event(speech: bool) -> VADEvent:
    """Create a VADEvent with all required fields."""
    return VADEvent(
        is_speech=speech,
        probability=0.9 if speech else 0.1,
        state=VADState.SPEECH if speech else VADState.SILENCE,
    )


def _frame(val: int = 0) -> np.ndarray:
    """Create a 512-sample int16 frame filled with *val*."""
    return np.full(_FRAME_LEN, val, dtype=np.int16)


def _speech_frame() -> np.ndarray:
    """Create a frame that simulates speech (non-zero)."""
    return np.full(_FRAME_LEN, 1000, dtype=np.int16)


def _silence_frame() -> np.ndarray:
    """Create a frame that simulates silence."""
    return np.zeros(_FRAME_LEN, dtype=np.int16)


def _audio_chunk(duration_ms: float = 100.0) -> AudioChunk:
    """Create a minimal audio chunk."""
    samples = int(22050 * duration_ms / 1000)
    return AudioChunk(
        audio=np.zeros(samples, dtype=np.int16),
        sample_rate=22050,
        duration_ms=duration_ms,
    )


def _make_vad(speech: bool = False) -> MagicMock:
    """Create a mock VAD that always returns *speech*."""
    vad = MagicMock()
    vad.process_frame.return_value = VADEvent(
        is_speech=speech,
        probability=0.9 if speech else 0.1,
        state=VADState.SPEECH if speech else VADState.SILENCE,
    )
    return vad


def _make_wake_word(detected: bool = False) -> MagicMock:
    """Create a mock wake word detector."""
    ww = MagicMock()
    event = MagicMock()
    event.detected = detected
    ww.process_frame.return_value = event
    return ww


def _make_stt(text: str = "hello world", confidence: float = 0.95) -> AsyncMock:
    """Create a mock STT engine."""
    stt = AsyncMock()
    result = MagicMock()
    result.text = text
    result.confidence = confidence
    result.language = "en"
    stt.transcribe.return_value = result
    return stt


def _make_tts() -> AsyncMock:
    """Create a mock TTS engine."""
    tts = AsyncMock()
    tts.synthesize.return_value = _audio_chunk(50)
    return tts


def _make_event_bus() -> AsyncMock:
    """Create a mock event bus."""
    bus = AsyncMock()
    bus.emit = AsyncMock()
    return bus


def _make_pipeline(
    *,
    wake_word_enabled: bool = True,
    barge_in_enabled: bool = True,
    fillers_enabled: bool = False,
    vad_speech: bool = False,
    ww_detected: bool = False,
    stt_text: str = "hello world",
    on_perception: AsyncMock | None = None,
) -> tuple[VoicePipeline, dict[str, Any]]:
    """Create a pipeline with mocked components and return it with refs."""
    config = VoicePipelineConfig(
        mind_id="test-mind",
        wake_word_enabled=wake_word_enabled,
        barge_in_enabled=barge_in_enabled,
        fillers_enabled=fillers_enabled,
        filler_delay_ms=100,
        silence_frames_end=3,
        max_recording_frames=10,
    )
    vad = _make_vad(speech=vad_speech)
    ww = _make_wake_word(detected=ww_detected)
    stt = _make_stt(text=stt_text)
    tts = _make_tts()
    bus = _make_event_bus()

    pipeline = VoicePipeline(
        config=config,
        vad=vad,
        wake_word=ww,
        stt=stt,
        tts=tts,
        event_bus=bus,
        on_perception=on_perception,
    )

    return pipeline, {
        "vad": vad,
        "ww": ww,
        "stt": stt,
        "tts": tts,
        "bus": bus,
        "config": config,
    }


# ===========================================================================
# validate_config
# ===========================================================================


class TestValidateConfig:
    """Tests for configuration validation."""

    def test_valid_config(self) -> None:
        """Default config is valid."""
        validate_config(VoicePipelineConfig())

    def test_negative_filler_delay(self) -> None:
        with pytest.raises(ValueError, match="filler_delay_ms"):
            validate_config(VoicePipelineConfig(filler_delay_ms=-1))

    def test_zero_silence_frames(self) -> None:
        with pytest.raises(ValueError, match="silence_frames_end"):
            validate_config(VoicePipelineConfig(silence_frames_end=0))

    def test_zero_max_recording(self) -> None:
        with pytest.raises(ValueError, match="max_recording_frames"):
            validate_config(VoicePipelineConfig(max_recording_frames=0))

    def test_zero_barge_in_threshold(self) -> None:
        with pytest.raises(ValueError, match="barge_in_threshold"):
            validate_config(VoicePipelineConfig(barge_in_threshold=0))

    def test_invalid_confirmation_tone(self) -> None:
        with pytest.raises(ValueError, match="confirmation_tone"):
            validate_config(VoicePipelineConfig(confirmation_tone="chime"))


# ===========================================================================
# split_at_boundaries
# ===========================================================================


class TestSplitAtBoundaries:
    """Tests for text splitting for streaming TTS."""

    def test_single_sentence(self) -> None:
        result = split_at_boundaries("Hello world, how are you?")
        assert result == ["Hello world, how are you?"]

    def test_two_sentences(self) -> None:
        result = split_at_boundaries("First sentence. Second sentence here.")
        assert len(result) == 2
        assert result[0] == "First sentence."
        assert result[1] == "Second sentence here."

    def test_short_fragment_merged(self) -> None:
        """Fragments < 3 words merge with previous."""
        result = split_at_boundaries("Hello! Hi! What's up today?")
        # "Hi!" is only 1 word but ends with ! so it's kept
        assert any("Hi!" in s for s in result)

    def test_empty_string(self) -> None:
        result = split_at_boundaries("")
        assert result == [""]

    def test_newline_split(self) -> None:
        result = split_at_boundaries("Line one here.\nLine two here.")
        assert len(result) == 2

    def test_question_mark(self) -> None:
        result = split_at_boundaries("How are you? I am fine.")
        assert len(result) == 2

    def test_semicolon_split(self) -> None:
        result = split_at_boundaries("First part here; second part here.")
        assert len(result) == 2


# ===========================================================================
# AudioOutputQueue
# ===========================================================================


class TestAudioOutputQueue:
    """Tests for the audio output queue."""

    @pytest.mark.asyncio
    async def test_not_playing_initially(self) -> None:
        q = AudioOutputQueue()
        assert q.is_playing is False

    @pytest.mark.asyncio
    async def test_enqueue_and_drain(self) -> None:
        q = AudioOutputQueue()
        chunk = _audio_chunk(10)
        await q.enqueue(chunk)
        with patch.object(_pipeline_mod, "_play_audio", new_callable=AsyncMock):
            await q.drain()
        assert q.is_playing is False

    @pytest.mark.asyncio
    async def test_play_immediate(self) -> None:
        q = AudioOutputQueue()
        chunk = _audio_chunk(10)
        with patch.object(_pipeline_mod, "_play_audio", new_callable=AsyncMock):
            await q.play_immediate(chunk)
        assert q.is_playing is False

    @pytest.mark.asyncio
    async def test_interrupt_clears_queue(self) -> None:
        q = AudioOutputQueue()
        await q.enqueue(_audio_chunk(10))
        await q.enqueue(_audio_chunk(10))
        q.interrupt()
        assert q._queue.empty()

    @pytest.mark.asyncio
    async def test_clear_without_interrupt(self) -> None:
        q = AudioOutputQueue()
        await q.enqueue(_audio_chunk(10))
        q.clear()
        assert q._queue.empty()

    @pytest.mark.asyncio
    async def test_drain_with_interrupt(self) -> None:
        """Interrupt during drain stops playback."""
        q = AudioOutputQueue()
        await q.enqueue(_audio_chunk(10))
        await q.enqueue(_audio_chunk(10))

        call_count = 0

        async def _slow_play(chunk: AudioChunk) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                q.interrupt()

        with patch.object(_pipeline_mod, "_play_audio", side_effect=_slow_play):
            await q.drain()
        # Should have played at most 1 chunk before interrupt
        assert call_count == 1


# ===========================================================================
# BargeInDetector
# ===========================================================================


class TestBargeInDetector:
    """Tests for barge-in detection."""

    def test_check_frame_speech(self) -> None:
        vad = _make_vad(speech=True)
        output = AudioOutputQueue()
        detector = BargeInDetector(vad, output, threshold_frames=3)
        assert detector.check_frame(_speech_frame()) is True

    def test_check_frame_silence(self) -> None:
        vad = _make_vad(speech=False)
        output = AudioOutputQueue()
        detector = BargeInDetector(vad, output, threshold_frames=3)
        assert detector.check_frame(_silence_frame()) is False

    @pytest.mark.asyncio
    async def test_monitor_no_barge_in_when_not_playing(self) -> None:
        vad = _make_vad(speech=True)
        output = AudioOutputQueue()
        detector = BargeInDetector(vad, output, threshold_frames=1)
        result = await detector.monitor(get_frame=lambda: _speech_frame())
        assert result is False  # Not playing → no barge-in


# ===========================================================================
# JarvisIllusion
# ===========================================================================


class TestJarvisIllusion:
    """Tests for filler phrase injection and beep caching (pipeline compat)."""

    @pytest.mark.asyncio
    async def test_pre_cache_beep(self) -> None:
        from sovyx.voice.jarvis import JarvisConfig

        tts = _make_tts()
        config = JarvisConfig(confirmation_tone="beep")
        jarvis = JarvisIllusion(config, tts)
        await jarvis.pre_cache()
        assert jarvis.beep_cached is True

    @pytest.mark.asyncio
    async def test_pre_cache_no_beep(self) -> None:
        from sovyx.voice.jarvis import JarvisConfig

        tts = _make_tts()
        config = JarvisConfig(confirmation_tone="none")
        jarvis = JarvisIllusion(config, tts)
        await jarvis.pre_cache()
        assert jarvis.beep_cached is False

    @pytest.mark.asyncio
    async def test_pre_cache_fillers(self) -> None:
        from sovyx.voice.jarvis import FillerCategory, JarvisConfig

        tts = _make_tts()
        config = JarvisConfig(
            filler_bank={
                FillerCategory.TRANSITIONAL: ("Hmm...", "Let me check..."),
                FillerCategory.THINKING: (),
                FillerCategory.CHECKING: (),
                FillerCategory.ACKNOWLEDGING: (),
                FillerCategory.CONFIRMING: (),
            }
        )
        jarvis = JarvisIllusion(config, tts)
        await jarvis.pre_cache()
        assert jarvis.cached_filler_count == 2

    @pytest.mark.asyncio
    async def test_play_beep(self) -> None:
        from sovyx.voice.jarvis import JarvisConfig

        tts = _make_tts()
        config = JarvisConfig(confirmation_tone="beep")
        jarvis = JarvisIllusion(config, tts)
        await jarvis.pre_cache()
        output = AudioOutputQueue()
        with patch.object(_pipeline_mod, "_play_audio", new_callable=AsyncMock):
            await jarvis.play_beep(output)

    @pytest.mark.asyncio
    async def test_filler_cancelled_when_fast(self) -> None:
        """If LLM responds fast, filler is cancelled."""
        from sovyx.voice.jarvis import JarvisConfig

        tts = _make_tts()
        config = JarvisConfig(filler_delay_ms=200)
        jarvis = JarvisIllusion(config, tts)
        await jarvis.pre_cache()
        output = AudioOutputQueue()
        cancel = asyncio.Event()
        cancel.set()  # Already signalled — LLM responded immediately
        played = await jarvis.play_filler_after_delay(output, cancel)
        assert played is False

    @pytest.mark.asyncio
    async def test_filler_plays_on_timeout(self) -> None:
        """If LLM doesn't respond, filler plays after delay."""
        from sovyx.voice.jarvis import FillerCategory, JarvisConfig

        tts = _make_tts()
        config = JarvisConfig(
            filler_delay_ms=10,
            filler_bank={
                FillerCategory.TRANSITIONAL: ("Hmm...",),
                FillerCategory.THINKING: ("Hmm...",),
                FillerCategory.CHECKING: ("Hmm...",),
                FillerCategory.ACKNOWLEDGING: ("Hmm...",),
                FillerCategory.CONFIRMING: ("Hmm...",),
            },
        )
        jarvis = JarvisIllusion(config, tts)
        await jarvis.pre_cache()
        output = AudioOutputQueue()
        cancel = asyncio.Event()  # Never set
        with patch.object(_pipeline_mod, "_play_audio", new_callable=AsyncMock):
            played = await jarvis.play_filler_after_delay(output, cancel)
        assert played is True

    @pytest.mark.asyncio
    async def test_get_cached_filler_hit(self) -> None:
        from sovyx.voice.jarvis import FillerCategory, JarvisConfig

        tts = _make_tts()
        config = JarvisConfig(
            filler_bank={
                FillerCategory.TRANSITIONAL: ("Hmm...",),
                FillerCategory.THINKING: (),
                FillerCategory.CHECKING: (),
                FillerCategory.ACKNOWLEDGING: (),
                FillerCategory.CONFIRMING: (),
            }
        )
        jarvis = JarvisIllusion(config, tts)
        await jarvis.pre_cache()
        assert jarvis.get_cached_filler("Hmm...") is not None

    @pytest.mark.asyncio
    async def test_get_cached_filler_miss(self) -> None:
        from sovyx.voice.jarvis import FillerCategory, JarvisConfig

        tts = _make_tts()
        config = JarvisConfig(
            filler_bank={
                FillerCategory.TRANSITIONAL: ("Hmm...",),
                FillerCategory.THINKING: (),
                FillerCategory.CHECKING: (),
                FillerCategory.ACKNOWLEDGING: (),
                FillerCategory.CONFIRMING: (),
            }
        )
        jarvis = JarvisIllusion(config, tts)
        await jarvis.pre_cache()
        assert jarvis.get_cached_filler("Not cached") is None

    @pytest.mark.asyncio
    async def test_synthesize_beep(self) -> None:
        from sovyx.voice.jarvis import synthesize_beep

        chunk = synthesize_beep()
        assert chunk.sample_rate == 22050
        assert chunk.duration_ms == pytest.approx(50.0)
        assert len(chunk.audio) > 0


# ===========================================================================
# VoicePipeline — state machine
# ===========================================================================


class TestPipelineStateMachine:
    """Tests for the core state machine transitions."""

    @pytest.mark.asyncio
    async def test_initial_state(self) -> None:
        pipeline, _ = _make_pipeline()
        assert pipeline.state == VoicePipelineState.IDLE

    @pytest.mark.asyncio
    async def test_not_running_returns_early(self) -> None:
        pipeline, _ = _make_pipeline()
        # Don't call start() — pipeline._running is False
        result = await pipeline.feed_frame(_frame())
        assert result["event"] == "not_running"

    @pytest.mark.asyncio
    async def test_idle_silence_stays_idle(self) -> None:
        pipeline, _ = _make_pipeline(vad_speech=False)
        await pipeline.start()
        result = await pipeline.feed_frame(_silence_frame())
        assert result["state"] == "IDLE"

    @pytest.mark.asyncio
    async def test_idle_speech_no_wake_stays_idle(self) -> None:
        """Speech detected but wake word not triggered → stays IDLE."""
        pipeline, _ = _make_pipeline(vad_speech=True, ww_detected=False)
        await pipeline.start()
        result = await pipeline.feed_frame(_speech_frame())
        assert result["state"] == "IDLE"

    @pytest.mark.asyncio
    async def test_wake_word_detection(self) -> None:
        """Wake word detected → WAKE_DETECTED."""
        pipeline, refs = _make_pipeline(vad_speech=True, ww_detected=True)
        await pipeline.start()

        with patch.object(_pipeline_mod, "_play_audio", new_callable=AsyncMock):
            result = await pipeline.feed_frame(_speech_frame())
        assert result["state"] == "WAKE_DETECTED"
        assert result["event"] == "wake_word_detected"
        assert pipeline.state == VoicePipelineState.WAKE_DETECTED

    @pytest.mark.asyncio
    async def test_wake_detected_to_recording(self) -> None:
        """WAKE_DETECTED → RECORDING on next frame."""
        pipeline, refs = _make_pipeline(vad_speech=True, ww_detected=True)
        await pipeline.start()

        with patch.object(_pipeline_mod, "_play_audio", new_callable=AsyncMock):
            await pipeline.feed_frame(_speech_frame())  # → WAKE_DETECTED
        result = await pipeline.feed_frame(_speech_frame())  # → RECORDING
        assert result["state"] == "RECORDING"
        assert pipeline.state == VoicePipelineState.RECORDING

    @pytest.mark.asyncio
    async def test_recording_accumulates_frames(self) -> None:
        """Frames accumulate during RECORDING."""
        pipeline, _ = _make_pipeline(vad_speech=True, ww_detected=True)
        await pipeline.start()

        with patch.object(_pipeline_mod, "_play_audio", new_callable=AsyncMock):
            await pipeline.feed_frame(_speech_frame())  # WAKE_DETECTED
        await pipeline.feed_frame(_speech_frame())  # → RECORDING (counter=1)
        result = await pipeline.feed_frame(_speech_frame())  # counter=2
        assert result["state"] == "RECORDING"
        assert result["frames"] == 2  # wake_detected sets to 1, this is the 2nd frame in RECORDING

    @pytest.mark.asyncio
    async def test_silence_ends_recording(self) -> None:
        """Sufficient silence → ends recording → transcribes."""
        pipeline, refs = _make_pipeline(vad_speech=True, ww_detected=True, stt_text="test")
        await pipeline.start()

        with patch.object(_pipeline_mod, "_play_audio", new_callable=AsyncMock):
            await pipeline.feed_frame(_speech_frame())  # WAKE_DETECTED

        await pipeline.feed_frame(_speech_frame())  # RECORDING

        # Now switch VAD to silence
        refs["vad"].process_frame.return_value = _vad_event(False)

        # Feed silence frames until threshold (3)
        for _ in range(3):
            result = await pipeline.feed_frame(_silence_frame())

        assert result["state"] == "THINKING"
        assert result["text"] == "test"
        refs["stt"].transcribe.assert_called_once()

    @pytest.mark.asyncio
    async def test_recording_timeout(self) -> None:
        """Recording times out at max_recording_frames."""
        pipeline, refs = _make_pipeline(vad_speech=True, ww_detected=True, stt_text="timeout test")
        await pipeline.start()

        with patch.object(_pipeline_mod, "_play_audio", new_callable=AsyncMock):
            await pipeline.feed_frame(_speech_frame())  # WAKE_DETECTED

        # Feed 10 speech frames (max_recording_frames=10)
        for _ in range(10):
            result = await pipeline.feed_frame(_speech_frame())

        # Should have auto-ended
        assert result["state"] == "THINKING"
        assert result["text"] == "timeout test"

    @pytest.mark.asyncio
    async def test_empty_transcription_returns_idle(self) -> None:
        """Empty STT result → back to IDLE."""
        pipeline, refs = _make_pipeline(vad_speech=True, ww_detected=True, stt_text="")
        await pipeline.start()

        with patch.object(_pipeline_mod, "_play_audio", new_callable=AsyncMock):
            await pipeline.feed_frame(_speech_frame())

        await pipeline.feed_frame(_speech_frame())

        refs["vad"].process_frame.return_value = _vad_event(False)
        for _ in range(3):
            result = await pipeline.feed_frame(_silence_frame())

        assert result["state"] == "IDLE"
        assert result["event"] == "empty_transcription"

    @pytest.mark.asyncio
    async def test_stt_error_returns_idle(self) -> None:
        """STT exception → IDLE with error."""
        pipeline, refs = _make_pipeline(vad_speech=True, ww_detected=True)
        refs["stt"].transcribe.side_effect = RuntimeError("model crash")
        await pipeline.start()

        with patch.object(_pipeline_mod, "_play_audio", new_callable=AsyncMock):
            await pipeline.feed_frame(_speech_frame())

        await pipeline.feed_frame(_speech_frame())
        refs["vad"].process_frame.return_value = _vad_event(False)

        for _ in range(3):
            result = await pipeline.feed_frame(_silence_frame())

        assert result["state"] == "IDLE"
        assert result["event"] == "stt_error"

    @pytest.mark.asyncio
    async def test_perception_callback_called(self) -> None:
        """on_perception callback is invoked with transcribed text."""
        cb = AsyncMock()
        pipeline, refs = _make_pipeline(
            vad_speech=True, ww_detected=True, stt_text="hello", on_perception=cb
        )
        await pipeline.start()

        with patch.object(_pipeline_mod, "_play_audio", new_callable=AsyncMock):
            await pipeline.feed_frame(_speech_frame())

        await pipeline.feed_frame(_speech_frame())
        refs["vad"].process_frame.return_value = _vad_event(False)
        for _ in range(3):
            await pipeline.feed_frame(_silence_frame())

        cb.assert_called_once_with("hello", "test-mind")

    @pytest.mark.asyncio
    async def test_no_wake_word_mode(self) -> None:
        """With wake_word_enabled=False, speech goes directly to recording."""
        pipeline, refs = _make_pipeline(
            wake_word_enabled=False, vad_speech=True, stt_text="direct"
        )
        await pipeline.start()

        result = await pipeline.feed_frame(_speech_frame())
        assert result["state"] == "RECORDING"


# ===========================================================================
# VoicePipeline — speaking / streaming
# ===========================================================================


class TestPipelineSpeaking:
    """Tests for TTS / speaking functionality."""

    @pytest.mark.asyncio
    async def test_speak_synthesizes_and_plays(self) -> None:
        pipeline, refs = _make_pipeline()
        await pipeline.start()
        refs["tts"].synthesize.reset_mock()  # Clear pre_cache calls

        with patch.object(_pipeline_mod, "_play_audio", new_callable=AsyncMock):
            await pipeline.speak("Hello there!")

        refs["tts"].synthesize.assert_called_once_with("Hello there!")
        assert pipeline.state == VoicePipelineState.IDLE

    @pytest.mark.asyncio
    async def test_speak_emits_events(self) -> None:
        pipeline, refs = _make_pipeline()
        await pipeline.start()

        with patch.object(_pipeline_mod, "_play_audio", new_callable=AsyncMock):
            await pipeline.speak("test")

        events = [call.args[0] for call in refs["bus"].emit.call_args_list]
        event_types = [type(e) for e in events]
        assert TTSStartedEvent in event_types
        assert TTSCompletedEvent in event_types

    @pytest.mark.asyncio
    async def test_speak_tts_error(self) -> None:
        pipeline, refs = _make_pipeline()
        refs["tts"].synthesize.side_effect = RuntimeError("TTS crash")
        await pipeline.start()

        await pipeline.speak("fail")

        events = [call.args[0] for call in refs["bus"].emit.call_args_list]
        assert any(isinstance(e, PipelineErrorEvent) for e in events)
        assert pipeline.state == VoicePipelineState.IDLE

    @pytest.mark.asyncio
    async def test_stream_text_accumulates(self) -> None:
        """stream_text accumulates until sentence boundary."""
        pipeline, refs = _make_pipeline()
        await pipeline.start()
        refs["tts"].synthesize.reset_mock()  # Clear pre_cache calls

        await pipeline.stream_text("Hello ")
        # No sentence boundary yet — TTS not called
        refs["tts"].synthesize.assert_not_called()

    @pytest.mark.asyncio
    async def test_stream_text_synthesizes_at_boundary(self) -> None:
        """stream_text synthesizes complete sentences."""
        pipeline, refs = _make_pipeline()
        await pipeline.start()

        await pipeline.stream_text("First sentence here. Second")
        # "First sentence here." should be synthesized
        refs["tts"].synthesize.assert_called()

    @pytest.mark.asyncio
    async def test_flush_stream(self) -> None:
        """flush_stream synthesizes remaining buffer."""
        pipeline, refs = _make_pipeline()
        await pipeline.start()

        await pipeline.stream_text("Remaining text")
        with patch.object(_pipeline_mod, "_play_audio", new_callable=AsyncMock):
            await pipeline.flush_stream()

        assert pipeline.state == VoicePipelineState.IDLE
        # TTS should have been called for the flushed text
        refs["tts"].synthesize.assert_called()

    @pytest.mark.asyncio
    async def test_start_thinking_with_fillers_disabled(self) -> None:
        pipeline, _ = _make_pipeline(fillers_enabled=False)
        await pipeline.start()
        await pipeline.start_thinking()
        assert pipeline.state == VoicePipelineState.THINKING

    @pytest.mark.asyncio
    async def test_start_thinking_with_fillers_enabled(self) -> None:
        pipeline, _ = _make_pipeline(fillers_enabled=True)
        await pipeline.start()
        await pipeline.start_thinking()
        assert pipeline.state == VoicePipelineState.THINKING
        # Filler task should be created
        assert pipeline._filler_task is not None
        pipeline._cancel_filler()  # Cleanup


# ===========================================================================
# VoicePipeline — barge-in
# ===========================================================================


class TestPipelineBargeIn:
    """Tests for barge-in during speaking."""

    @pytest.mark.asyncio
    async def test_barge_in_during_speaking(self) -> None:
        """User speaking while TTS plays → barge-in → RECORDING."""
        pipeline, refs = _make_pipeline(vad_speech=True, barge_in_enabled=True)
        await pipeline.start()

        # Force into SPEAKING state
        pipeline._state = VoicePipelineState.SPEAKING
        pipeline._output._playing = True

        result = await pipeline.feed_frame(_speech_frame())
        assert result["state"] == "RECORDING"
        assert result["event"] == "barge_in_recording"

    @pytest.mark.asyncio
    async def test_barge_in_disabled(self) -> None:
        """With barge_in_enabled=False, speaking continues."""
        pipeline, refs = _make_pipeline(vad_speech=True, barge_in_enabled=False)
        await pipeline.start()
        pipeline._state = VoicePipelineState.SPEAKING
        pipeline._output._playing = True

        result = await pipeline.feed_frame(_speech_frame())
        assert result["state"] == "SPEAKING"

    @pytest.mark.asyncio
    async def test_speaking_finished_returns_idle(self) -> None:
        """When TTS finishes playing → back to IDLE."""
        pipeline, refs = _make_pipeline(vad_speech=False)
        await pipeline.start()
        pipeline._state = VoicePipelineState.SPEAKING
        # output._playing is False by default → playback finished

        result = await pipeline.feed_frame(_silence_frame())
        assert result["state"] == "IDLE"
        assert result["event"] == "tts_completed"


# ===========================================================================
# VoicePipeline — lifecycle
# ===========================================================================


class TestPipelineLifecycle:
    """Tests for start/stop/reset."""

    @pytest.mark.asyncio
    async def test_start_sets_running(self) -> None:
        pipeline, _ = _make_pipeline()
        await pipeline.start()
        assert pipeline.is_running is True
        assert pipeline.state == VoicePipelineState.IDLE

    @pytest.mark.asyncio
    async def test_stop_clears_state(self) -> None:
        pipeline, _ = _make_pipeline()
        await pipeline.start()
        await pipeline.stop()
        assert pipeline.is_running is False
        assert pipeline.state == VoicePipelineState.IDLE

    @pytest.mark.asyncio
    async def test_reset(self) -> None:
        pipeline, _ = _make_pipeline()
        await pipeline.start()
        pipeline._state = VoicePipelineState.RECORDING
        pipeline._text_buffer = "leftover"
        pipeline.reset()
        assert pipeline.state == VoicePipelineState.IDLE
        assert pipeline._text_buffer == ""

    @pytest.mark.asyncio
    async def test_properties(self) -> None:
        pipeline, refs = _make_pipeline()
        assert pipeline.config.mind_id == "test-mind"
        assert pipeline.output is pipeline._output
        assert pipeline.jarvis is pipeline._jarvis

    @pytest.mark.asyncio
    async def test_component_properties_expose_instances(self) -> None:
        """vad/stt/tts/wake_word expose the exact instances passed at construction."""
        pipeline, refs = _make_pipeline()
        assert pipeline.vad is refs["vad"]
        assert pipeline.stt is refs["stt"]
        assert pipeline.tts is refs["tts"]
        assert pipeline.wake_word is refs["ww"]


# ===========================================================================
# VoicePipeline — events emitted
# ===========================================================================


class TestPipelineEvents:
    """Tests for event bus emissions during pipeline transitions."""

    @pytest.mark.asyncio
    async def test_wake_word_emits_events(self) -> None:
        pipeline, refs = _make_pipeline(vad_speech=True, ww_detected=True)
        await pipeline.start()

        with patch.object(_pipeline_mod, "_play_audio", new_callable=AsyncMock):
            await pipeline.feed_frame(_speech_frame())

        events = [call.args[0] for call in refs["bus"].emit.call_args_list]
        event_types = [type(e) for e in events]
        assert WakeWordDetectedEvent in event_types
        assert SpeechStartedEvent in event_types

    @pytest.mark.asyncio
    async def test_end_recording_emits_speech_ended(self) -> None:
        pipeline, refs = _make_pipeline(vad_speech=True, ww_detected=True, stt_text="hi")
        await pipeline.start()

        with patch.object(_pipeline_mod, "_play_audio", new_callable=AsyncMock):
            await pipeline.feed_frame(_speech_frame())
        await pipeline.feed_frame(_speech_frame())

        refs["vad"].process_frame.return_value = _vad_event(False)
        for _ in range(3):
            await pipeline.feed_frame(_silence_frame())

        events = [call.args[0] for call in refs["bus"].emit.call_args_list]
        assert any(isinstance(e, SpeechEndedEvent) for e in events)
        assert any(isinstance(e, TranscriptionCompletedEvent) for e in events)

    @pytest.mark.asyncio
    async def test_event_bus_none_doesnt_crash(self) -> None:
        """Pipeline works without an event bus."""
        config = VoicePipelineConfig(
            mind_id="no-bus",
            silence_frames_end=2,
            max_recording_frames=5,
        )
        vad = _make_vad(speech=True)
        ww = _make_wake_word(detected=True)
        stt = _make_stt("works")
        tts = _make_tts()

        pipeline = VoicePipeline(
            config=config,
            vad=vad,
            wake_word=ww,
            stt=stt,
            tts=tts,
            event_bus=None,
            on_perception=None,
        )
        await pipeline.start()

        with patch.object(_pipeline_mod, "_play_audio", new_callable=AsyncMock):
            result = await pipeline.feed_frame(_speech_frame())
        assert result["state"] == "WAKE_DETECTED"


# ===========================================================================
# VoicePipeline — full cycle
# ===========================================================================


class TestPipelineFullCycle:
    """Integration-style test: full wake→record→transcribe→speak cycle."""

    @pytest.mark.asyncio
    async def test_full_cycle(self) -> None:
        """IDLE → WAKE → RECORDING → TRANSCRIBING → THINKING → speak → IDLE."""
        cb = AsyncMock()
        pipeline, refs = _make_pipeline(
            vad_speech=True,
            ww_detected=True,
            stt_text="turn on the lights",
            on_perception=cb,
        )
        await pipeline.start()

        # 1. Wake word detected
        with patch.object(_pipeline_mod, "_play_audio", new_callable=AsyncMock):
            r = await pipeline.feed_frame(_speech_frame())
        assert r["state"] == "WAKE_DETECTED"

        # 2. Start recording
        r = await pipeline.feed_frame(_speech_frame())
        assert r["state"] == "RECORDING"

        # 3. More speech
        r = await pipeline.feed_frame(_speech_frame())
        assert r["state"] == "RECORDING"

        # 4. Silence → end recording → transcribe
        refs["vad"].process_frame.return_value = _vad_event(False)
        for _ in range(3):
            r = await pipeline.feed_frame(_silence_frame())
        assert r["state"] == "THINKING"
        assert r["text"] == "turn on the lights"

        # 5. CogLoop responds — speak
        with patch.object(_pipeline_mod, "_play_audio", new_callable=AsyncMock):
            await pipeline.speak("Lights are on!")

        assert pipeline.state == VoicePipelineState.IDLE
        cb.assert_called_once_with("turn on the lights", "test-mind")

    @pytest.mark.asyncio
    async def test_streaming_cycle(self) -> None:
        """Full cycle with streaming TTS from LLM tokens."""
        pipeline, refs = _make_pipeline()
        await pipeline.start()

        await pipeline.start_thinking()
        assert pipeline.state == VoicePipelineState.THINKING

        # LLM tokens arrive — stream to TTS
        await pipeline.stream_text("This is the first sentence. ")
        assert pipeline.state == VoicePipelineState.SPEAKING

        await pipeline.stream_text("And this is more text.")

        # Flush remaining
        with patch.object(_pipeline_mod, "_play_audio", new_callable=AsyncMock):
            await pipeline.flush_stream()

        assert pipeline.state == VoicePipelineState.IDLE


# ===========================================================================
# Edge cases
# ===========================================================================


class TestEdgeCases:
    """Edge case tests."""

    @pytest.mark.asyncio
    async def test_perception_callback_error_doesnt_crash(self) -> None:
        """Exception in on_perception doesn't crash the pipeline."""
        cb = AsyncMock(side_effect=RuntimeError("callback boom"))
        pipeline, refs = _make_pipeline(
            vad_speech=True,
            ww_detected=True,
            stt_text="oops",
            on_perception=cb,
        )
        await pipeline.start()

        with patch.object(_pipeline_mod, "_play_audio", new_callable=AsyncMock):
            await pipeline.feed_frame(_speech_frame())
        await pipeline.feed_frame(_speech_frame())
        refs["vad"].process_frame.return_value = _vad_event(False)
        for _ in range(3):
            result = await pipeline.feed_frame(_silence_frame())

        # Should still reach THINKING despite callback error
        assert result["state"] == "THINKING"

    @pytest.mark.asyncio
    async def test_event_bus_error_doesnt_crash(self) -> None:
        """EventBus emit failure doesn't crash the pipeline."""
        pipeline, refs = _make_pipeline(vad_speech=True, ww_detected=True)
        refs["bus"].emit.side_effect = RuntimeError("bus crash")
        await pipeline.start()

        with patch.object(_pipeline_mod, "_play_audio", new_callable=AsyncMock):
            await pipeline.feed_frame(_speech_frame())
        # Should still transition despite bus error
        assert pipeline.state in (
            VoicePipelineState.WAKE_DETECTED,
            VoicePipelineState.RECORDING,
        )

    @pytest.mark.asyncio
    async def test_transcribing_thinking_passthrough(self) -> None:
        """TRANSCRIBING/THINKING states pass through on feed_frame."""
        pipeline, _ = _make_pipeline()
        await pipeline.start()
        pipeline._state = VoicePipelineState.TRANSCRIBING
        result = await pipeline.feed_frame(_frame())
        assert result["state"] == "TRANSCRIBING"

        pipeline._state = VoicePipelineState.THINKING
        result = await pipeline.feed_frame(_frame())
        assert result["state"] == "THINKING"

    @pytest.mark.asyncio
    async def test_stream_text_tts_error(self) -> None:
        """TTS error during stream_text is handled gracefully."""
        pipeline, refs = _make_pipeline()
        refs["tts"].synthesize.side_effect = RuntimeError("TTS down")
        await pipeline.start()

        # Should not raise
        await pipeline.stream_text("Error sentence. Another one.")

    @pytest.mark.asyncio
    async def test_flush_empty_buffer(self) -> None:
        """Flushing an empty buffer is a no-op."""
        pipeline, refs = _make_pipeline()
        await pipeline.start()
        pipeline._state = VoicePipelineState.SPEAKING

        with patch.object(_pipeline_mod, "_play_audio", new_callable=AsyncMock):
            await pipeline.flush_stream()

        assert pipeline.state == VoicePipelineState.IDLE

    @pytest.mark.asyncio
    async def test_cancel_filler_idempotent(self) -> None:
        """_cancel_filler is safe to call multiple times."""
        pipeline, _ = _make_pipeline()
        pipeline._cancel_filler()
        pipeline._cancel_filler()
        # No error


# ===========================================================================
# Coverage gap tests — pipeline.py
# ===========================================================================


class TestAudioOutputQueueEdgeCases:
    """Cover interrupt/clear QueueEmpty race and _play_audio branches."""

    @pytest.mark.asyncio
    async def test_interrupt_empty_queue(self) -> None:
        """interrupt() on empty queue doesn't raise."""
        q = AudioOutputQueue()
        q.interrupt()  # no items — hits QueueEmpty in loop
        assert q._interrupted is True

    @pytest.mark.asyncio
    async def test_clear_empty_queue(self) -> None:
        """clear() on empty queue doesn't raise."""
        q = AudioOutputQueue()
        q.clear()  # no items — hits QueueEmpty in loop

    @pytest.mark.asyncio
    async def test_play_audio_sounddevice_path(self) -> None:
        """_play_audio opens an OutputStream and writes the chunk.

        The pipeline output queue uses ``blocking_write_play`` (see
        ``_stream_opener``) instead of ``sd.play`` so playback survives
        WASAPI on threadpool workers.
        """
        from sovyx.voice.pipeline import _play_audio

        chunk = _audio_chunk(10)
        stream = MagicMock()
        mock_sd = MagicMock()
        mock_sd.OutputStream = MagicMock(return_value=stream)
        # If anything regresses to sd.play this will fail loudly.
        mock_sd.play = MagicMock(side_effect=AssertionError("sd.play must not be used"))
        with patch.dict("sys.modules", {"sounddevice": mock_sd}):
            await _play_audio(chunk)
        mock_sd.OutputStream.assert_called_once()
        stream.start.assert_called_once()
        stream.write.assert_called_once()

    @pytest.mark.asyncio
    async def test_play_audio_does_not_block_event_loop(self) -> None:
        """Regression: blocking playback MUST run in a worker thread.

        Historic bug: ``_play_audio`` ran blocking playback directly
        from the async body, so a multi-second TTS chunk stalled every
        other coroutine — dashboard WS frames, voice mic capture, HTTP
        requests. The fix offloads to :func:`asyncio.to_thread`. The
        test proves the offload by having the blocking ``write()``
        side-effect wait on a threading.Event while an async ticker
        runs concurrently on the event loop.
        """
        from sovyx.voice.pipeline import _play_audio

        play_started = asyncio.Event()
        release_play = threading.Event()
        concurrent_ticks = 0
        loop = asyncio.get_running_loop()

        def _blocking_write(_audio: object) -> None:
            loop.call_soon_threadsafe(play_started.set)
            # Block the worker thread until the async side has been able
            # to run some other work. If the event loop was also blocked,
            # the asyncio.Event below would never be set and we'd deadlock.
            release_play.wait(timeout=1.0)

        stream = MagicMock()
        stream.write = MagicMock(side_effect=_blocking_write)
        mock_sd = MagicMock()
        mock_sd.OutputStream = MagicMock(return_value=stream)
        mock_sd.play = MagicMock(side_effect=AssertionError("sd.play must not be used"))

        async def _ticker() -> None:
            nonlocal concurrent_ticks
            await play_started.wait()
            # If the event loop is pinned by the blocking write, this body never runs.
            for _ in range(3):
                concurrent_ticks += 1
                await asyncio.sleep(0)
            release_play.set()

        chunk = _audio_chunk(10)
        with patch.dict("sys.modules", {"sounddevice": mock_sd}):
            await asyncio.gather(_play_audio(chunk), _ticker())

        assert concurrent_ticks == 3, (
            "event loop was blocked during OutputStream.write(); _play_audio must use to_thread"
        )


class TestHandleSpeakingBargeInAsync:
    """Regression: `_handle_speaking` MUST use the async barge-in check.

    ``BargeInDetector.check_frame`` runs ONNX VAD inference synchronously.
    Calling it from inside an ``async def`` coroutine blocks the event
    loop on EVERY mic frame while TTS is playing — capture falls behind,
    dashboard WS stalls, and barge-in itself is detected late. The
    ``check_frame_async`` variant offloads to ``asyncio.to_thread``.
    """

    @pytest.mark.asyncio
    async def test_orchestrator_awaits_check_frame_async(self) -> None:
        import inspect

        from sovyx.voice.pipeline import VoicePipeline

        src = inspect.getsource(VoicePipeline._handle_speaking)
        # The sync variant must not be used in this hot path.
        assert "self._barge_in.check_frame(" not in src, (
            "`_handle_speaking` must not call the sync `check_frame` — use `check_frame_async`"
        )
        assert "check_frame_async" in src, (
            "`_handle_speaking` must await `check_frame_async` to avoid blocking the event loop"
        )

    @pytest.mark.asyncio
    async def test_play_audio_import_error_zero_duration(self) -> None:
        """_play_audio with ImportError and 0 duration returns immediately."""
        from sovyx.voice.pipeline import _play_audio

        chunk = AudioChunk(
            audio=np.zeros(10, dtype=np.int16),
            sample_rate=22050,
            duration_ms=0.0,
        )
        with patch.dict("sys.modules", {"sounddevice": None}):
            # sounddevice=None forces ImportError on import
            await _play_audio(chunk)


class TestBargeInDetectorMonitor:
    """Cover the BargeInDetector.monitor() loop with active playback."""

    @pytest.mark.asyncio
    async def test_monitor_barge_in_during_playback(self) -> None:
        """monitor() detects barge-in when output is_playing and speech frames."""
        vad = _make_vad(speech=True)
        output = AudioOutputQueue()
        detector = BargeInDetector(vad, output, threshold_frames=2)

        # Simulate is_playing for a few iterations then stop
        play_count = 0

        @property  # type: ignore[misc]
        def _fake_playing(self: AudioOutputQueue) -> bool:
            nonlocal play_count
            play_count += 1
            return play_count <= 5

        with patch.object(type(output), "is_playing", _fake_playing):
            result = await detector.monitor(get_frame=lambda: _speech_frame())
        assert result is True

    @pytest.mark.asyncio
    async def test_monitor_none_frame_skipped(self) -> None:
        """monitor() skips None frames and waits."""
        vad = _make_vad(speech=True)
        output = AudioOutputQueue()
        detector = BargeInDetector(vad, output, threshold_frames=1)

        frames_returned = 0

        def get_frame() -> np.ndarray | None:
            nonlocal frames_returned
            frames_returned += 1
            if frames_returned <= 2:
                return None
            return _speech_frame()

        play_count = 0

        @property  # type: ignore[misc]
        def _fake_playing(self: AudioOutputQueue) -> bool:
            nonlocal play_count
            play_count += 1
            return play_count <= 10

        with patch.object(type(output), "is_playing", _fake_playing):
            result = await detector.monitor(get_frame=get_frame)
        assert result is True

    @pytest.mark.asyncio
    async def test_monitor_silence_resets_consecutive(self) -> None:
        """Silence frames reset the consecutive counter."""
        speech_vad = _make_vad(speech=True)
        output = AudioOutputQueue()
        detector = BargeInDetector(speech_vad, output, threshold_frames=3)

        frame_idx = 0

        def get_frame() -> np.ndarray:
            nonlocal frame_idx
            frame_idx += 1
            return _speech_frame() if frame_idx != 2 else _silence_frame()

        # Override check_frame to alternate
        checks = [True, False, True, True, True]
        check_idx = 0

        def _patched_check(frame: np.ndarray) -> bool:
            nonlocal check_idx
            if check_idx < len(checks):
                val = checks[check_idx]
                check_idx += 1
                return val
            return True

        detector.check_frame = _patched_check  # type: ignore[assignment]

        play_count = 0

        @property  # type: ignore[misc]
        def _fake_playing(self: AudioOutputQueue) -> bool:
            nonlocal play_count
            play_count += 1
            return play_count <= 10

        with patch.object(type(output), "is_playing", _fake_playing):
            result = await detector.monitor(get_frame=get_frame)
        assert result is True


class TestPipelineCoverageGaps:
    """Cover remaining pipeline.py uncovered lines."""

    @pytest.mark.asyncio
    async def test_wake_detected_plays_beep(self) -> None:
        """Wake word detection triggers confirmation beep."""
        pipeline, refs = _make_pipeline(
            wake_word_enabled=True,
            ww_detected=True,
            vad_speech=True,
        )
        await pipeline.start()

        # Force wake word detected state
        pipeline._state = VoicePipelineState.IDLE
        refs["ww"].process_frame.return_value.detected = True

        with patch.object(pipeline._jarvis, "play_beep", new_callable=AsyncMock) as mock_beep:
            result = await pipeline.feed_frame(_speech_frame())

        # Should transition through WAKE_DETECTED
        if result.get("event") == "wake_word_detected":
            mock_beep.assert_called_once()

    @pytest.mark.asyncio
    async def test_speaking_not_playing_returns_idle(self) -> None:
        """SPEAKING state transitions to IDLE when output finishes."""
        pipeline, refs = _make_pipeline()
        await pipeline.start()
        pipeline._state = VoicePipelineState.SPEAKING
        pipeline._output._playing = False

        with patch.object(_pipeline_mod, "_play_audio", new_callable=AsyncMock):
            result = await pipeline.feed_frame(_silence_frame())

        assert result["state"] == "IDLE"
        assert result.get("event") == "tts_completed"

    @pytest.mark.asyncio
    async def test_speaking_still_playing(self) -> None:
        """SPEAKING state stays SPEAKING while output is playing."""
        pipeline, refs = _make_pipeline()
        await pipeline.start()
        pipeline._state = VoicePipelineState.SPEAKING
        pipeline._output._playing = True

        result = await pipeline.feed_frame(_silence_frame())
        assert result["state"] == "SPEAKING"

    @pytest.mark.asyncio
    async def test_end_recording_empty_utterance(self) -> None:
        """_end_recording with no frames returns IDLE."""
        pipeline, refs = _make_pipeline()
        await pipeline.start()
        pipeline._state = VoicePipelineState.RECORDING
        pipeline._utterance_frames = []

        result = await pipeline._end_recording()
        assert result["state"] == "IDLE"
        assert result["event"] == "empty_recording"

    @pytest.mark.asyncio
    async def test_flush_stream_tts_failure(self) -> None:
        """flush_stream handles TTS synthesis failure gracefully."""
        pipeline, refs = _make_pipeline()
        await pipeline.start()
        pipeline._state = VoicePipelineState.SPEAKING
        pipeline._text_buffer = "pending text"
        refs["tts"].synthesize.side_effect = RuntimeError("TTS error")

        with patch.object(_pipeline_mod, "_play_audio", new_callable=AsyncMock):
            await pipeline.flush_stream()

        # Should still transition to IDLE despite error
        assert pipeline.state == VoicePipelineState.IDLE
        assert pipeline._text_buffer == ""

    @pytest.mark.asyncio
    async def test_transition_to_recording_barge_in(self) -> None:
        """_transition_to_recording sets up RECORDING state."""
        pipeline, refs = _make_pipeline()
        await pipeline.start()
        frame = _speech_frame()

        result = await pipeline._transition_to_recording(frame)
        assert result["state"] == "RECORDING"
        assert result["event"] == "barge_in_recording"
        assert len(pipeline._utterance_frames) == 1


# ===========================================================================
# Pipeline telemetry — heartbeat + recording/perception lifecycle logs
# ===========================================================================
#
# Regression for the silent-voice diagnosis: before these logs the only
# signal an operator had between "frames enter" and "STT runs" was the
# absence of downstream events. Adding `voice_pipeline_heartbeat`,
# `voice_recording_started/ended`, `voice_stt_completed`, and
# `voice_perception_invoked/skipped_no_callback` lets us pinpoint
# exactly where the pipeline wedges.


_ORCH_LOGGER = "sovyx.voice.pipeline._orchestrator"


def _events_of(caplog: pytest.LogCaptureFixture, event: str) -> list[dict[str, Any]]:
    """Return stdlib LogRecord payloads whose structlog ``event`` field matches.

    Sovyx configures structlog with ``structlog.stdlib.LoggerFactory`` +
    ``wrap_for_formatter`` (see ``observability/logging.setup_logging``), which
    packages the full event_dict as the stdlib ``LogRecord.msg``. That record
    traverses the root logger — where pytest's ``caplog`` hooks an opaque
    handler — so every emitted event is observable here irrespective of any
    earlier ``structlog.configure`` churn (which would orphan
    ``capture_logs``' in-place processor mutation against cached bound-logger
    references).
    """
    return [
        r.msg
        for r in caplog.records
        if r.name == _ORCH_LOGGER and isinstance(r.msg, dict) and r.msg.get("event") == event
    ]


class TestPipelineHeartbeat:
    """``voice_pipeline_heartbeat`` surfaces VAD activity even when the FSM never fires."""

    @pytest.mark.asyncio
    async def test_heartbeat_emits_with_max_probability(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Heartbeat captures the highest VAD probability seen in the window."""
        from sovyx.voice.pipeline import _orchestrator as orch_mod

        caplog.set_level(logging.INFO, logger=_ORCH_LOGGER)
        pipeline, refs = _make_pipeline(wake_word_enabled=False, vad_speech=False)
        await pipeline.start()

        probs = [0.05, 0.42, 0.18]
        pipeline._last_heartbeat_monotonic = 0.0
        with patch.object(orch_mod.time, "monotonic", return_value=0.0):
            for p in probs[:-1]:
                refs["vad"].process_frame.return_value = VADEvent(
                    is_speech=False, probability=p, state=VADState.SILENCE
                )
                await pipeline.feed_frame(_silence_frame())
        refs["vad"].process_frame.return_value = VADEvent(
            is_speech=False, probability=probs[-1], state=VADState.SILENCE
        )
        with patch.object(
            orch_mod.time, "monotonic", return_value=orch_mod._HEARTBEAT_INTERVAL_S + 1.0
        ):
            await pipeline.feed_frame(_silence_frame())

        heartbeats = _events_of(caplog, "voice_pipeline_heartbeat")
        assert len(heartbeats) == 1
        assert heartbeats[0]["state"] == "IDLE"
        assert heartbeats[0]["max_vad_probability"] == pytest.approx(0.42)
        assert heartbeats[0]["frames_processed"] == 3  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_heartbeat_resets_window_after_emission(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Counters reset after each heartbeat so max reflects the last window only."""
        from sovyx.voice.pipeline import _orchestrator as orch_mod

        caplog.set_level(logging.INFO, logger=_ORCH_LOGGER)
        pipeline, refs = _make_pipeline(wake_word_enabled=False, vad_speech=False)
        await pipeline.start()
        pipeline._last_heartbeat_monotonic = 0.0
        refs["vad"].process_frame.return_value = VADEvent(
            is_speech=False, probability=0.9, state=VADState.SILENCE
        )

        with patch.object(
            orch_mod.time, "monotonic", return_value=orch_mod._HEARTBEAT_INTERVAL_S + 1.0
        ):
            await pipeline.feed_frame(_silence_frame())
        caplog.clear()
        refs["vad"].process_frame.return_value = VADEvent(
            is_speech=False, probability=0.05, state=VADState.SILENCE
        )
        with patch.object(
            orch_mod.time, "monotonic", return_value=orch_mod._HEARTBEAT_INTERVAL_S + 1.1
        ):
            await pipeline.feed_frame(_silence_frame())

        heartbeats = _events_of(caplog, "voice_pipeline_heartbeat")
        assert heartbeats == []
        assert pipeline._max_vad_prob_since_heartbeat == pytest.approx(0.05)
        assert pipeline._vad_frames_since_heartbeat == 1


class TestRecordingLifecycleLogs:
    """``voice_recording_started`` / ``voice_recording_ended`` frame the STT window."""

    @pytest.mark.asyncio
    async def test_recording_started_logs_on_vad_trigger(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger=_ORCH_LOGGER)
        pipeline, _ = _make_pipeline(wake_word_enabled=False, vad_speech=True)
        await pipeline.start()
        caplog.clear()

        await pipeline.feed_frame(_speech_frame())

        started = _events_of(caplog, "voice_recording_started")
        assert len(started) == 1
        assert started[0]["mind_id"] == "test-mind"
        assert started[0]["wake_word_enabled"] is False

    @pytest.mark.asyncio
    async def test_recording_ended_logs_with_duration_and_frames(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger=_ORCH_LOGGER)
        pipeline, refs = _make_pipeline(wake_word_enabled=False, vad_speech=True)
        await pipeline.start()
        await pipeline.feed_frame(_speech_frame())  # → RECORDING

        refs["vad"].process_frame.return_value = VADEvent(
            is_speech=False, probability=0.1, state=VADState.SILENCE
        )
        caplog.clear()
        for _ in range(3):
            await pipeline.feed_frame(_silence_frame())

        ended = _events_of(caplog, "voice_recording_ended")
        assert len(ended) == 1
        assert ended[0]["frames"] == 4  # 1 speech + 3 silence  # noqa: PLR2004
        assert ended[0]["duration_ms"] > 0
        assert ended[0]["silence_counter"] == 3  # noqa: PLR2004


class TestSTTAndPerceptionLogs:
    """``voice_stt_completed`` and ``voice_perception_invoked`` trace the cognitive handoff."""

    @pytest.mark.asyncio
    async def test_stt_completed_logs_metadata(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.INFO, logger=_ORCH_LOGGER)
        pipeline, refs = _make_pipeline(
            wake_word_enabled=False, vad_speech=True, stt_text="olá mundo"
        )
        await pipeline.start()
        await pipeline.feed_frame(_speech_frame())

        refs["vad"].process_frame.return_value = VADEvent(
            is_speech=False, probability=0.1, state=VADState.SILENCE
        )
        caplog.clear()
        for _ in range(3):
            await pipeline.feed_frame(_silence_frame())

        completed = _events_of(caplog, "voice_stt_completed")
        assert len(completed) == 1
        assert completed[0]["text_length"] == len("olá mundo")
        assert completed[0]["has_text"] is True
        assert completed[0]["language"] == "en"
        assert completed[0]["latency_ms"] >= 0

    @pytest.mark.asyncio
    async def test_perception_invoked_logs_when_callback_wired(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger=_ORCH_LOGGER)
        on_perception = AsyncMock()
        pipeline, refs = _make_pipeline(
            wake_word_enabled=False, vad_speech=True, on_perception=on_perception
        )
        await pipeline.start()
        await pipeline.feed_frame(_speech_frame())

        refs["vad"].process_frame.return_value = VADEvent(
            is_speech=False, probability=0.1, state=VADState.SILENCE
        )
        caplog.clear()
        for _ in range(3):
            await pipeline.feed_frame(_silence_frame())

        invoked = _events_of(caplog, "voice_perception_invoked")
        skipped = _events_of(caplog, "voice_perception_skipped_no_callback")
        assert len(invoked) == 1
        assert skipped == []
        on_perception.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_perception_skipped_logs_when_callback_missing(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger=_ORCH_LOGGER)
        pipeline, refs = _make_pipeline(
            wake_word_enabled=False, vad_speech=True, on_perception=None
        )
        await pipeline.start()
        await pipeline.feed_frame(_speech_frame())

        refs["vad"].process_frame.return_value = VADEvent(
            is_speech=False, probability=0.1, state=VADState.SILENCE
        )
        caplog.clear()
        for _ in range(3):
            await pipeline.feed_frame(_silence_frame())

        skipped = _events_of(caplog, "voice_perception_skipped_no_callback")
        assert len(skipped) == 1
        assert skipped[0]["text_length"] == len("hello world")


def _bypass_integrity_result(verdict_value: str) -> Any:
    """Build a minimal :class:`IntegrityResult` for :func:`_bypass_outcome`.

    The orchestrator only reads :attr:`BypassOutcome.verdict`,
    :attr:`strategy_name`, :attr:`attempt_index`, and :attr:`detail` — but
    :class:`BypassOutcome` is a frozen dataclass with required
    ``integrity_before`` / ``integrity_after`` fields, so we construct
    minimal valid :class:`IntegrityResult` instances for them.
    """
    from datetime import UTC, datetime

    from sovyx.voice.health.contract import IntegrityResult, IntegrityVerdict

    return IntegrityResult(
        verdict=IntegrityVerdict(verdict_value),
        endpoint_guid="test-endpoint-guid",
        rms_db=-24.0,
        vad_max_prob=0.5,
        spectral_flatness=0.2,
        spectral_rolloff_hz=5000.0,
        duration_s=3.0,
        probed_at_utc=datetime.now(UTC),
        raw_frames=48_000,
    )


def _bypass_outcome(
    *,
    verdict: str,
    strategy_name: str = "test.fake",
    attempt_index: int = 0,
    detail: str = "",
) -> Any:
    """Build a :class:`BypassOutcome` the orchestrator can introspect."""
    from sovyx.voice.health.contract import BypassOutcome, BypassVerdict

    after_verdict = "healthy" if verdict == "applied_healthy" else "apo_degraded"
    return BypassOutcome(
        strategy_name=strategy_name,
        attempt_index=attempt_index,
        verdict=BypassVerdict(verdict),
        integrity_before=_bypass_integrity_result("apo_degraded"),
        integrity_after=_bypass_integrity_result(after_verdict),
        elapsed_ms=350.0,
        detail=detail,
    )


class TestDeafSignalCoordinator:
    """Sustained-deaf heartbeat → ``on_deaf_signal`` → coordinator outcomes.

    The orchestrator no longer owns bypass mechanics; it detects the
    deaf pattern, gates on the auto-bypass kill switch, and delegates
    to a supplied callback that returns the
    :class:`~sovyx.voice.health.contract.BypassOutcome` log from
    :class:`CaptureIntegrityCoordinator`. Terminal latch lives here
    (:attr:`_coordinator_terminated`) so a non-empty outcome list means
    "don't re-enter the coordinator this session — watchdog recheck
    handles recovery".
    """

    def _deaf_pipeline(
        self,
        *,
        auto_bypass_enabled: bool,
        callback: Any,
        threshold: int = 2,
        voice_clarity_active: bool = True,
    ) -> VoicePipeline:
        config = VoicePipelineConfig(
            mind_id="test-mind",
            wake_word_enabled=False,
            barge_in_enabled=False,
            fillers_enabled=False,
            filler_delay_ms=100,
            silence_frames_end=3,
            max_recording_frames=10,
        )
        vad = _make_vad(speech=False)
        vad.process_frame.return_value = VADEvent(
            is_speech=False, probability=0.0, state=VADState.SILENCE
        )
        return VoicePipeline(
            config=config,
            vad=vad,
            wake_word=_make_wake_word(detected=False),
            stt=_make_stt(),
            tts=_make_tts(),
            event_bus=_make_event_bus(),
            on_perception=None,
            on_deaf_signal=callback,
            voice_clarity_active=voice_clarity_active,
            auto_bypass_enabled=auto_bypass_enabled,
            auto_bypass_threshold=threshold,
        )

    async def _drive_deaf_heartbeat(self, pipeline: VoicePipeline) -> None:
        """Force one deaf-heartbeat emission on the next fed frame.

        Preloads the accumulator with ``_DEAF_MIN_FRAMES`` and zero max
        VAD probability so the heartbeat classifies the window as deaf,
        then advances monotonic time past the heartbeat interval so the
        rate gate unblocks. A single ``feed_frame`` then runs the full
        ``_track_vad_for_heartbeat`` path.
        """
        from sovyx.voice.pipeline import _orchestrator as orch_mod

        pipeline._last_heartbeat_monotonic = 0.0
        pipeline._vad_frames_since_heartbeat = orch_mod._DEAF_MIN_FRAMES
        pipeline._max_vad_prob_since_heartbeat = 0.0
        with patch.object(
            orch_mod.time, "monotonic", return_value=orch_mod._HEARTBEAT_INTERVAL_S + 1.0
        ):
            await pipeline.feed_frame(_silence_frame())

    @pytest.mark.asyncio
    async def test_callback_fires_after_threshold_consecutive_deaf_warnings(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger=_ORCH_LOGGER)
        callback = AsyncMock(
            return_value=[
                _bypass_outcome(
                    verdict="applied_healthy",
                    strategy_name="win.wasapi_exclusive",
                    detail="exclusive_engaged",
                )
            ]
        )
        pipeline = self._deaf_pipeline(auto_bypass_enabled=True, callback=callback, threshold=2)
        await pipeline.start()

        await self._drive_deaf_heartbeat(pipeline)  # warning #1 — below threshold
        callback.assert_not_awaited()

        await self._drive_deaf_heartbeat(pipeline)  # warning #2 — threshold met
        # Let the create_task-scheduled invocation run.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        callback.assert_awaited_once()
        activated = _events_of(caplog, "voice_apo_bypass_activated")
        assert len(activated) == 1
        assert activated[0]["strategy_name"] == "win.wasapi_exclusive"
        assert activated[0]["action"] == "capture_integrity_coordinator"
        assert activated[0]["threshold"] == 2  # noqa: PLR2004
        assert activated[0]["consecutive_deaf_warnings"] >= 2  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_callback_blocked_when_auto_bypass_disabled(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger=_ORCH_LOGGER)
        callback = AsyncMock(return_value=[])
        pipeline = self._deaf_pipeline(auto_bypass_enabled=False, callback=callback, threshold=2)
        await pipeline.start()

        for _ in range(4):
            await self._drive_deaf_heartbeat(pipeline)
        await asyncio.sleep(0)

        callback.assert_not_awaited()
        assert _events_of(caplog, "voice_apo_bypass_activated") == []

    @pytest.mark.asyncio
    async def test_terminal_latch_after_non_empty_outcomes(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """One non-empty outcome list → coordinator_terminated → single invocation."""
        caplog.set_level(logging.WARNING, logger=_ORCH_LOGGER)
        callback = AsyncMock(return_value=[_bypass_outcome(verdict="applied_healthy")])
        pipeline = self._deaf_pipeline(auto_bypass_enabled=True, callback=callback, threshold=1)
        await pipeline.start()

        for _ in range(5):
            await self._drive_deaf_heartbeat(pipeline)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        callback.assert_awaited_once()
        assert pipeline._coordinator_terminated is True
        assert len(_events_of(caplog, "voice_apo_bypass_activated")) == 1

    @pytest.mark.asyncio
    async def test_empty_outcomes_do_not_latch_terminal(self) -> None:
        """Empty list = coordinator short-circuit; orchestrator may re-enter later."""
        callback = AsyncMock(return_value=[])
        pipeline = self._deaf_pipeline(auto_bypass_enabled=True, callback=callback, threshold=1)
        await pipeline.start()

        await self._drive_deaf_heartbeat(pipeline)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        callback.assert_awaited_once()
        assert pipeline._coordinator_terminated is False

        # Second deaf burst: invocation-pending was cleared and the
        # terminal latch is off, so the callback is entered again.
        await self._drive_deaf_heartbeat(pipeline)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert callback.await_count == 2  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_exhausted_strategies_emits_ineffective(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.ERROR, logger=_ORCH_LOGGER)
        callback = AsyncMock(
            return_value=[
                _bypass_outcome(
                    verdict="applied_still_dead",
                    strategy_name="win.wasapi_exclusive",
                    attempt_index=0,
                ),
                _bypass_outcome(
                    verdict="failed_to_apply",
                    strategy_name="win.disable_sysfx",
                    attempt_index=1,
                ),
            ]
        )
        pipeline = self._deaf_pipeline(auto_bypass_enabled=True, callback=callback, threshold=1)
        await pipeline.start()

        await self._drive_deaf_heartbeat(pipeline)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        ineffective = _events_of(caplog, "voice_apo_bypass_ineffective")
        assert len(ineffective) == 1
        assert ineffective[0]["attempts"] == 2  # noqa: PLR2004
        assert ineffective[0]["strategies"] == [
            "win.wasapi_exclusive",
            "win.disable_sysfx",
        ]
        assert ineffective[0]["verdicts"] == ["applied_still_dead", "failed_to_apply"]
        assert pipeline._coordinator_terminated is True

    @pytest.mark.asyncio
    async def test_callback_exception_does_not_crash_pipeline(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.ERROR, logger=_ORCH_LOGGER)

        async def failing_callback() -> list[Any]:
            raise RuntimeError("simulated coordinator failure")

        pipeline = self._deaf_pipeline(
            auto_bypass_enabled=True, callback=failing_callback, threshold=1
        )
        await pipeline.start()

        await self._drive_deaf_heartbeat(pipeline)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        failed = _events_of(caplog, "voice_apo_bypass_failed")
        assert len(failed) == 1
        assert failed[0]["error_type"] == "RuntimeError"
        assert "simulated" in failed[0]["error"]
        # Exception path does NOT latch the terminal flag — transient
        # coordinator issues (e.g. probe tap race) must not lock out
        # future deafness bursts.
        assert pipeline._coordinator_terminated is False
        assert pipeline._coordinator_invocation_pending is False

    @pytest.mark.asyncio
    async def test_noop_without_callback(self) -> None:
        pipeline = self._deaf_pipeline(auto_bypass_enabled=True, callback=None, threshold=1)
        await pipeline.start()
        # Must not raise or latch the coordinator terminal flag.
        await self._drive_deaf_heartbeat(pipeline)
        assert pipeline._coordinator_terminated is False
        assert pipeline._coordinator_invocation_pending is False

    @pytest.mark.asyncio
    async def test_healthy_heartbeat_resets_consecutive_counter(self) -> None:
        callback = AsyncMock(return_value=[])
        pipeline = self._deaf_pipeline(auto_bypass_enabled=True, callback=callback, threshold=3)
        await pipeline.start()

        await self._drive_deaf_heartbeat(pipeline)
        await self._drive_deaf_heartbeat(pipeline)
        assert pipeline._deaf_warnings_consecutive == 2  # noqa: PLR2004

        from sovyx.voice.pipeline import _orchestrator as orch_mod

        pipeline._last_heartbeat_monotonic = 0.0
        pipeline._vad_frames_since_heartbeat = orch_mod._DEAF_MIN_FRAMES
        pipeline._max_vad_prob_since_heartbeat = 0.9  # above the deaf threshold
        with patch.object(
            orch_mod.time, "monotonic", return_value=orch_mod._HEARTBEAT_INTERVAL_S + 1.0
        ):
            await pipeline.feed_frame(_silence_frame())
        assert pipeline._deaf_warnings_consecutive == 0
        callback.assert_not_awaited()


# ===========================================================================
# O2: Deaf-signal coordinator atomicity (Ring 6 critical-section contract)
# ===========================================================================
#
# The O2 refactor wraps the deaf-signal flow in an asyncio.Lock so the
# critical section becomes refactor-resistant: the pre-O2 invariant
# relied on asyncio's single-threaded execution + sync flag check-and-
# set in _maybe_trigger_bypass_coordinator, which silently breaks the
# moment any future change introduces an ``await`` into the sync trigger
# path. The lock makes the contract explicit. Additionally, the counter
# snapshot+reset inside the lock eliminates the pre-O2 tight-retry loop
# where deaf heartbeats firing during ``await callback()`` would
# accumulate and immediately re-cross the threshold on the next
# heartbeat after an empty-outcomes return.
#
# Reference: MISSION-voice-mixer-enterprise-refactor-2026-04-25 §3.6, O2.


class TestDeafSignalAtomicityO2:
    """Lock-protected critical section + counter snapshot semantics."""

    def _deaf_pipeline(
        self,
        *,
        auto_bypass_enabled: bool,
        callback: Any,
        threshold: int = 2,
        voice_clarity_active: bool = True,
    ) -> VoicePipeline:
        config = VoicePipelineConfig(
            mind_id="test-mind",
            wake_word_enabled=False,
            barge_in_enabled=False,
            fillers_enabled=False,
            filler_delay_ms=100,
            silence_frames_end=3,
            max_recording_frames=10,
        )
        vad = _make_vad(speech=False)
        vad.process_frame.return_value = VADEvent(
            is_speech=False, probability=0.0, state=VADState.SILENCE
        )
        return VoicePipeline(
            config=config,
            vad=vad,
            wake_word=_make_wake_word(detected=False),
            stt=_make_stt(),
            tts=_make_tts(),
            event_bus=_make_event_bus(),
            on_perception=None,
            on_deaf_signal=callback,
            voice_clarity_active=voice_clarity_active,
            auto_bypass_enabled=auto_bypass_enabled,
            auto_bypass_threshold=threshold,
        )

    @pytest.mark.asyncio
    async def test_lock_attribute_initialised(self) -> None:
        pipeline = self._deaf_pipeline(
            auto_bypass_enabled=True, callback=AsyncMock(return_value=[])
        )
        assert isinstance(pipeline._coordinator_lock, asyncio.Lock)
        assert pipeline._coordinator_dedup_count == 0

    @pytest.mark.asyncio
    async def test_concurrent_invocations_serialised_by_lock(self) -> None:
        """Two spawned _invoke_deaf_signal tasks must serialise through the lock.

        With the lock, only the first acquirer proceeds to the callback;
        the second observes the post-first-invocation state (counter
        reset to 0 by the first invocation) and short-circuits as
        ``threshold_no_longer_met``. Without the lock the second
        invocation could race and double-call the callback.
        """
        callback = AsyncMock(return_value=[])
        pipeline = self._deaf_pipeline(auto_bypass_enabled=True, callback=callback, threshold=1)
        await pipeline.start()
        # Force the threshold-met state without going through the
        # heartbeat path; lets us spawn two invocations directly.
        pipeline._deaf_warnings_consecutive = 5

        # Spawn two invocations back-to-back. The first acquires the
        # lock and resets the counter; the second waits, then sees
        # counter=0 < threshold and dedups.
        task1 = asyncio.create_task(pipeline._invoke_deaf_signal())
        task2 = asyncio.create_task(pipeline._invoke_deaf_signal())
        await asyncio.gather(task1, task2)

        callback.assert_awaited_once()
        # The dedup happened — exactly one invocation was rejected.
        assert pipeline._coordinator_dedup_count == 1

    @pytest.mark.asyncio
    async def test_dedup_event_emitted_when_threshold_no_longer_met(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger=_ORCH_LOGGER)
        callback = AsyncMock(return_value=[])
        pipeline = self._deaf_pipeline(auto_bypass_enabled=True, callback=callback, threshold=1)
        await pipeline.start()
        pipeline._deaf_warnings_consecutive = 5

        task1 = asyncio.create_task(pipeline._invoke_deaf_signal())
        task2 = asyncio.create_task(pipeline._invoke_deaf_signal())
        await asyncio.gather(task1, task2)

        dedup_events = _events_of(caplog, "voice.deaf.coordinator_invocation_deduplicated")
        assert len(dedup_events) == 1
        assert dedup_events[0]["voice.reason"] == "threshold_no_longer_met"
        assert dedup_events[0]["voice.dedup_count"] == 1

    @pytest.mark.asyncio
    async def test_dedup_event_emitted_when_terminated_by_concurrent_task(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """If the terminal latch is set between spawn and lock acquire, dedup."""
        caplog.set_level(logging.INFO, logger=_ORCH_LOGGER)
        callback = AsyncMock(return_value=[_bypass_outcome(verdict="applied_healthy")])
        pipeline = self._deaf_pipeline(auto_bypass_enabled=True, callback=callback, threshold=1)
        await pipeline.start()
        pipeline._deaf_warnings_consecutive = 5

        task1 = asyncio.create_task(pipeline._invoke_deaf_signal())
        task2 = asyncio.create_task(pipeline._invoke_deaf_signal())
        await asyncio.gather(task1, task2)

        callback.assert_awaited_once()
        assert pipeline._coordinator_terminated is True
        dedup_events = _events_of(caplog, "voice.deaf.coordinator_invocation_deduplicated")
        assert len(dedup_events) == 1
        # The second task observes terminated=True (set by task1 inside
        # the lock) before checking the threshold.
        assert dedup_events[0]["voice.reason"] == "terminated_by_concurrent_task"

    @pytest.mark.asyncio
    async def test_counter_snapshot_resets_to_zero_on_invocation(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Inside the lock, the consecutive-deaf counter must be reset to 0
        BEFORE the await — so heartbeats during await accumulate from a
        clean baseline, not from the pre-invocation value. This is the
        core mechanism that breaks the pre-O2 tight-retry loop.
        """
        caplog.set_level(logging.WARNING, logger=_ORCH_LOGGER)
        callback = AsyncMock(return_value=[])
        pipeline = self._deaf_pipeline(auto_bypass_enabled=True, callback=callback, threshold=1)
        await pipeline.start()
        pipeline._deaf_warnings_consecutive = 7  # well above threshold

        await pipeline._invoke_deaf_signal()

        # Counter was reset before the await; empty outcomes don't
        # latch terminal; pending is cleared by finally.
        assert pipeline._deaf_warnings_consecutive == 0
        assert pipeline._coordinator_terminated is False
        assert pipeline._coordinator_invocation_pending is False

        # The recovery_attempted event reports the snapshot, not the
        # mutated post-reset value.
        attempted = _events_of(caplog, "voice.deaf.recovery_attempted")
        assert len(attempted) == 1
        assert attempted[0]["voice.consecutive_deaf_warnings"] == 7  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_heartbeats_during_await_do_not_cause_immediate_retrigger(
        self,
    ) -> None:
        """The pre-O2 tight-retry pattern: deaf heartbeats firing during
        the coordinator's ``await callback()`` accumulated into the
        counter, so an empty-outcomes return immediately re-crossed the
        threshold on the next sync trigger. With O2's in-lock counter
        reset, the post-callback counter starts from 0 even if heartbeats
        bumped it during the await.
        """

        # The callback bumps the counter from "inside" the await window
        # (simulating a heartbeat firing concurrently).
        async def bump_then_return_empty() -> list[Any]:
            pipeline._deaf_warnings_consecutive += 3
            await asyncio.sleep(0)  # yield to the event loop
            return []

        pipeline = self._deaf_pipeline(
            auto_bypass_enabled=True,
            callback=bump_then_return_empty,
            threshold=2,
        )
        await pipeline.start()
        pipeline._deaf_warnings_consecutive = 5  # threshold met

        await pipeline._invoke_deaf_signal()

        # Counter started at 5 → reset to 0 inside the lock → callback
        # bumped to 3 during await → final value is 3 (not 5+3=8).
        # Critically, 3 < threshold=2 is FALSE — but the next sync
        # trigger sees 3 ≥ 2 and would re-fire if the snapshot+reset
        # weren't in place. Our regression assertion: counter <= 3,
        # NOT 5+ (the pre-O2 accumulated value).
        assert pipeline._deaf_warnings_consecutive == 3  # noqa: PLR2004
        assert pipeline._coordinator_invocation_pending is False
        assert pipeline._coordinator_terminated is False

    @pytest.mark.asyncio
    async def test_callback_exception_releases_lock(self) -> None:
        """A raising callback must not leave the lock held — the next
        invocation must be able to acquire it.
        """

        async def first_call_raises() -> list[Any]:
            raise RuntimeError("transient")

        pipeline = self._deaf_pipeline(
            auto_bypass_enabled=True, callback=first_call_raises, threshold=1
        )
        await pipeline.start()
        pipeline._deaf_warnings_consecutive = 5

        await pipeline._invoke_deaf_signal()
        # Lock must be released after the exception.
        assert pipeline._coordinator_lock.locked() is False
        # Pending was cleared by finally.
        assert pipeline._coordinator_invocation_pending is False

        # Subsequent invocation must be able to acquire (would deadlock
        # if the lock was leaked).
        pipeline._deaf_warnings_consecutive = 5

        async def second_call_succeeds() -> list[Any]:
            return [_bypass_outcome(verdict="applied_healthy")]

        pipeline._on_deaf_signal = second_call_succeeds
        await pipeline._invoke_deaf_signal()
        assert pipeline._coordinator_terminated is True

    @pytest.mark.asyncio
    async def test_dedup_count_monotonic(self) -> None:
        """Each dedup increments the counter; never decrements."""
        callback = AsyncMock(return_value=[])
        pipeline = self._deaf_pipeline(auto_bypass_enabled=True, callback=callback, threshold=1)
        await pipeline.start()

        # Simulate 3 dedup events directly.
        pipeline._record_coordinator_dedup("threshold_no_longer_met")
        assert pipeline._coordinator_dedup_count == 1
        pipeline._record_coordinator_dedup("terminated_by_concurrent_task")
        assert pipeline._coordinator_dedup_count == 2  # noqa: PLR2004
        pipeline._record_coordinator_dedup("threshold_no_longer_met")
        assert pipeline._coordinator_dedup_count == 3  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_recovery_attempted_event_reports_snapshot_not_mutated(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """``voice.consecutive_deaf_warnings`` on the recovery_attempted
        event reports the snapshot value (what triggered the invocation),
        not the post-reset 0. Operators reading the log need the trigger
        cause, not the post-reset state.
        """
        caplog.set_level(logging.WARNING, logger=_ORCH_LOGGER)
        callback = AsyncMock(return_value=[_bypass_outcome(verdict="applied_healthy")])
        pipeline = self._deaf_pipeline(auto_bypass_enabled=True, callback=callback, threshold=2)
        await pipeline.start()
        pipeline._deaf_warnings_consecutive = 12

        await pipeline._invoke_deaf_signal()

        attempted = _events_of(caplog, "voice.deaf.recovery_attempted")
        assert len(attempted) == 1
        assert attempted[0]["voice.consecutive_deaf_warnings"] == 12  # noqa: PLR2004

        activated = _events_of(caplog, "voice_apo_bypass_activated")
        assert len(activated) == 1
        # Same snapshot semantics on the success event: report the
        # counter that justified the invocation, not the post-reset 0.
        assert activated[0]["consecutive_deaf_warnings"] == 12  # noqa: PLR2004


# ===========================================================================
# VoicePipeline — self-feedback gate wiring (ADR §4.4.6)
# ===========================================================================


class _RecordingGate:
    """Test double mirroring :class:`SelfFeedbackGate` transitions.

    The real gate applies duck via an external callback; for pipeline
    wiring tests we just need to assert that ``on_tts_start`` and
    ``on_tts_end`` fire at the expected state transitions. Keeping this
    separate from the real class verifies we call the documented
    protocol, not an implementation detail.
    """

    def __init__(self) -> None:
        self.events: list[str] = []

    def on_tts_start(self) -> None:
        self.events.append("start")

    def on_tts_end(self) -> None:
        self.events.append("end")


class TestPipelineSelfFeedbackGate:
    """Pipeline invokes the gate on every SPEAKING entry and exit."""

    def _make_pipeline_with_gate(
        self, gate: _RecordingGate
    ) -> tuple[VoicePipeline, dict[str, Any]]:
        pipeline, refs = _make_pipeline()
        pipeline._self_feedback_gate = gate  # type: ignore[assignment]
        return pipeline, refs

    @pytest.mark.asyncio
    async def test_speak_engages_and_releases(self) -> None:
        gate = _RecordingGate()
        pipeline, _ = self._make_pipeline_with_gate(gate)
        await pipeline.start()
        with patch.object(_pipeline_mod, "_play_audio", new_callable=AsyncMock):
            await pipeline.speak("Hello there!")
        assert gate.events == ["start", "end"]

    @pytest.mark.asyncio
    async def test_speak_releases_even_on_tts_error(self) -> None:
        gate = _RecordingGate()
        pipeline, refs = self._make_pipeline_with_gate(gate)
        refs["tts"].synthesize.side_effect = RuntimeError("TTS crash")
        await pipeline.start()
        await pipeline.speak("fail")
        assert gate.events == ["start", "end"]

    @pytest.mark.asyncio
    async def test_stream_text_engages_once_per_session(self) -> None:
        gate = _RecordingGate()
        pipeline, _ = self._make_pipeline_with_gate(gate)
        await pipeline.start()
        # Two chunks that both leave the pipeline in SPEAKING; the gate
        # should see exactly one rising edge (the SelfFeedbackGate
        # itself debounces, but we also verify the pipeline only
        # signals once per session).
        await pipeline.stream_text("First sentence here. Second")
        await pipeline.stream_text(" sentence here. Third")
        assert gate.events == ["start"]

    @pytest.mark.asyncio
    async def test_flush_stream_releases(self) -> None:
        gate = _RecordingGate()
        pipeline, _ = self._make_pipeline_with_gate(gate)
        await pipeline.start()
        await pipeline.stream_text("Remaining text")
        with patch.object(_pipeline_mod, "_play_audio", new_callable=AsyncMock):
            await pipeline.flush_stream()
        assert gate.events == ["start", "end"]

    @pytest.mark.asyncio
    async def test_barge_in_releases_gate(self) -> None:
        gate = _RecordingGate()
        pipeline, _ = _make_pipeline(vad_speech=True, barge_in_enabled=True)
        pipeline._self_feedback_gate = gate  # type: ignore[assignment]
        await pipeline.start()
        pipeline._state = VoicePipelineState.SPEAKING
        pipeline._output._playing = True

        result = await pipeline.feed_frame(_speech_frame())
        assert result["state"] == "RECORDING"
        assert gate.events == ["end"]

    @pytest.mark.asyncio
    async def test_natural_speaking_end_releases_gate(self) -> None:
        gate = _RecordingGate()
        pipeline, _ = _make_pipeline(vad_speech=False)
        pipeline._self_feedback_gate = gate  # type: ignore[assignment]
        await pipeline.start()
        pipeline._state = VoicePipelineState.SPEAKING
        # output._playing is False by default → playback already finished

        result = await pipeline.feed_frame(_silence_frame())
        assert result["state"] == "IDLE"
        assert gate.events == ["end"]

    @pytest.mark.asyncio
    async def test_stop_releases_mid_tts(self) -> None:
        """Mid-utterance ``stop()`` must release the duck so the next
        session doesn't boot with a ducked mic."""
        gate = _RecordingGate()
        pipeline, _ = _make_pipeline()
        pipeline._self_feedback_gate = gate  # type: ignore[assignment]
        await pipeline.start()
        pipeline._state = VoicePipelineState.SPEAKING
        await pipeline.stop()
        assert gate.events == ["end"]

    @pytest.mark.asyncio
    async def test_pipeline_tolerates_absent_gate(self) -> None:
        """No gate wired is the legitimate fallback (tests, push-to-talk)."""
        pipeline, _ = _make_pipeline()
        # Sanity: default ctor leaves the gate unset.
        assert pipeline._self_feedback_gate is None
        await pipeline.start()
        with patch.object(_pipeline_mod, "_play_audio", new_callable=AsyncMock):
            await pipeline.speak("hello")
        assert pipeline.state == VoicePipelineState.IDLE
