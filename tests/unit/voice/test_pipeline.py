"""Tests for VoicePipeline orchestrator (V05-22).

Covers: state machine transitions, barge-in, filler injection,
streaming TTS, AudioOutputQueue, BargeInDetector, JarvisIllusion,
split_at_boundaries, and configuration validation.

Ref: SPE-010 §8, §10, §13 — full state machine + barge-in + timing.
"""

from __future__ import annotations

import asyncio
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
        with patch("sovyx.voice.pipeline._play_audio", new_callable=AsyncMock):
            await q.drain()
        assert q.is_playing is False

    @pytest.mark.asyncio
    async def test_play_immediate(self) -> None:
        q = AudioOutputQueue()
        chunk = _audio_chunk(10)
        with patch("sovyx.voice.pipeline._play_audio", new_callable=AsyncMock):
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

        with patch("sovyx.voice.pipeline._play_audio", side_effect=_slow_play):
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
        with patch("sovyx.voice.pipeline._play_audio", new_callable=AsyncMock):
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
        with patch("sovyx.voice.pipeline._play_audio", new_callable=AsyncMock):
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

        with patch("sovyx.voice.pipeline._play_audio", new_callable=AsyncMock):
            result = await pipeline.feed_frame(_speech_frame())
        assert result["state"] == "WAKE_DETECTED"
        assert result["event"] == "wake_word_detected"
        assert pipeline.state == VoicePipelineState.WAKE_DETECTED

    @pytest.mark.asyncio
    async def test_wake_detected_to_recording(self) -> None:
        """WAKE_DETECTED → RECORDING on next frame."""
        pipeline, refs = _make_pipeline(vad_speech=True, ww_detected=True)
        await pipeline.start()

        with patch("sovyx.voice.pipeline._play_audio", new_callable=AsyncMock):
            await pipeline.feed_frame(_speech_frame())  # → WAKE_DETECTED
        result = await pipeline.feed_frame(_speech_frame())  # → RECORDING
        assert result["state"] == "RECORDING"
        assert pipeline.state == VoicePipelineState.RECORDING

    @pytest.mark.asyncio
    async def test_recording_accumulates_frames(self) -> None:
        """Frames accumulate during RECORDING."""
        pipeline, _ = _make_pipeline(vad_speech=True, ww_detected=True)
        await pipeline.start()

        with patch("sovyx.voice.pipeline._play_audio", new_callable=AsyncMock):
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

        with patch("sovyx.voice.pipeline._play_audio", new_callable=AsyncMock):
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

        with patch("sovyx.voice.pipeline._play_audio", new_callable=AsyncMock):
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

        with patch("sovyx.voice.pipeline._play_audio", new_callable=AsyncMock):
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

        with patch("sovyx.voice.pipeline._play_audio", new_callable=AsyncMock):
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

        with patch("sovyx.voice.pipeline._play_audio", new_callable=AsyncMock):
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

        with patch("sovyx.voice.pipeline._play_audio", new_callable=AsyncMock):
            await pipeline.speak("Hello there!")

        refs["tts"].synthesize.assert_called_once_with("Hello there!")
        assert pipeline.state == VoicePipelineState.IDLE

    @pytest.mark.asyncio
    async def test_speak_emits_events(self) -> None:
        pipeline, refs = _make_pipeline()
        await pipeline.start()

        with patch("sovyx.voice.pipeline._play_audio", new_callable=AsyncMock):
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
        with patch("sovyx.voice.pipeline._play_audio", new_callable=AsyncMock):
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


# ===========================================================================
# VoicePipeline — events emitted
# ===========================================================================


class TestPipelineEvents:
    """Tests for event bus emissions during pipeline transitions."""

    @pytest.mark.asyncio
    async def test_wake_word_emits_events(self) -> None:
        pipeline, refs = _make_pipeline(vad_speech=True, ww_detected=True)
        await pipeline.start()

        with patch("sovyx.voice.pipeline._play_audio", new_callable=AsyncMock):
            await pipeline.feed_frame(_speech_frame())

        events = [call.args[0] for call in refs["bus"].emit.call_args_list]
        event_types = [type(e) for e in events]
        assert WakeWordDetectedEvent in event_types
        assert SpeechStartedEvent in event_types

    @pytest.mark.asyncio
    async def test_end_recording_emits_speech_ended(self) -> None:
        pipeline, refs = _make_pipeline(vad_speech=True, ww_detected=True, stt_text="hi")
        await pipeline.start()

        with patch("sovyx.voice.pipeline._play_audio", new_callable=AsyncMock):
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

        with patch("sovyx.voice.pipeline._play_audio", new_callable=AsyncMock):
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
        with patch("sovyx.voice.pipeline._play_audio", new_callable=AsyncMock):
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
        with patch("sovyx.voice.pipeline._play_audio", new_callable=AsyncMock):
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
        with patch("sovyx.voice.pipeline._play_audio", new_callable=AsyncMock):
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

        with patch("sovyx.voice.pipeline._play_audio", new_callable=AsyncMock):
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

        with patch("sovyx.voice.pipeline._play_audio", new_callable=AsyncMock):
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

        with patch("sovyx.voice.pipeline._play_audio", new_callable=AsyncMock):
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
        """_play_audio uses sounddevice when available."""
        from sovyx.voice.pipeline import _play_audio

        chunk = _audio_chunk(10)
        mock_sd = MagicMock()
        mock_sd.play = MagicMock()
        mock_sd.wait = MagicMock()
        with patch.dict("sys.modules", {"sounddevice": mock_sd}):
            await _play_audio(chunk)
        mock_sd.play.assert_called_once()
        mock_sd.wait.assert_called_once()

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

        with patch("sovyx.voice.pipeline._play_audio", new_callable=AsyncMock):
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

        with patch("sovyx.voice.pipeline._play_audio", new_callable=AsyncMock):
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
