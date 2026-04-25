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


def _make_stt(
    text: str = "hello world",
    confidence: float = 0.95,
    rejection_reason: str | None = None,
) -> AsyncMock:
    """Create a mock STT engine.

    ``rejection_reason`` defaults to ``None`` so the orchestrator's
    S1/S2 wire-up takes the "user said nothing" path on empty
    transcripts (the pre-rejection_reason behaviour). Pass an explicit
    string ("hallucination_stoplist", "transcribe_timeout", etc.) to
    test the new ``voice.stt.transcription_dropped`` event path.
    """
    stt = AsyncMock()
    result = MagicMock()
    result.text = text
    result.confidence = confidence
    result.language = "en"
    result.rejection_reason = rejection_reason
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


class TestValidateConfigUpperBoundsHardening:
    """Mission band-aids #11/#37/#38 — upper-bound + consistency checks.

    Pre-hardening only lower bounds were enforced. Pathological values
    like ``max_recording_frames=99999`` (5+ minutes per turn) or
    ``filler_delay_ms=300000`` (5 minutes of dead air) silently passed
    and produced mysterious user-facing failures at runtime.
    """

    # mind_id sanity ──────────────────────────────────────────────────

    def test_empty_mind_id_rejected(self) -> None:
        with pytest.raises(ValueError, match="mind_id"):
            validate_config(VoicePipelineConfig(mind_id=""))

    def test_whitespace_only_mind_id_rejected(self) -> None:
        with pytest.raises(ValueError, match="mind_id"):
            validate_config(VoicePipelineConfig(mind_id="   "))

    # filler_delay_ms upper bound ────────────────────────────────────

    def test_filler_delay_ms_above_ceiling_rejected(self) -> None:
        from sovyx.voice.pipeline._config import _FILLER_DELAY_MS_MAX

        with pytest.raises(ValueError, match="filler_delay_ms"):
            validate_config(VoicePipelineConfig(filler_delay_ms=_FILLER_DELAY_MS_MAX + 1))

    def test_filler_delay_ms_at_ceiling_accepted(self) -> None:
        from sovyx.voice.pipeline._config import _FILLER_DELAY_MS_MAX

        validate_config(VoicePipelineConfig(filler_delay_ms=_FILLER_DELAY_MS_MAX))

    # silence_frames_end upper bound ─────────────────────────────────

    def test_silence_frames_end_above_ceiling_rejected(self) -> None:
        from sovyx.voice.pipeline._config import _SILENCE_FRAMES_END_MAX

        with pytest.raises(ValueError, match="silence_frames_end"):
            validate_config(VoicePipelineConfig(silence_frames_end=_SILENCE_FRAMES_END_MAX + 1))

    def test_silence_frames_end_at_ceiling_accepted(self) -> None:
        from sovyx.voice.pipeline._config import _SILENCE_FRAMES_END_MAX

        validate_config(VoicePipelineConfig(silence_frames_end=_SILENCE_FRAMES_END_MAX))

    # max_recording_frames upper bound ───────────────────────────────

    def test_max_recording_frames_above_ceiling_rejected(self) -> None:
        from sovyx.voice.pipeline._config import _MAX_RECORDING_FRAMES_MAX

        with pytest.raises(ValueError, match="max_recording_frames"):
            validate_config(
                VoicePipelineConfig(max_recording_frames=_MAX_RECORDING_FRAMES_MAX + 1)
            )

    def test_max_recording_frames_at_ceiling_accepted(self) -> None:
        from sovyx.voice.pipeline._config import _MAX_RECORDING_FRAMES_MAX

        validate_config(VoicePipelineConfig(max_recording_frames=_MAX_RECORDING_FRAMES_MAX))

    # barge_in_threshold upper bound ─────────────────────────────────

    def test_barge_in_threshold_above_ceiling_rejected(self) -> None:
        from sovyx.voice.pipeline._config import _BARGE_IN_THRESHOLD_MAX

        with pytest.raises(ValueError, match="barge_in_threshold"):
            validate_config(VoicePipelineConfig(barge_in_threshold=_BARGE_IN_THRESHOLD_MAX + 1))

    def test_barge_in_threshold_at_ceiling_accepted(self) -> None:
        from sovyx.voice.pipeline._config import _BARGE_IN_THRESHOLD_MAX

        validate_config(VoicePipelineConfig(barge_in_threshold=_BARGE_IN_THRESHOLD_MAX))

    # filler_phrases consistency ─────────────────────────────────────

    def test_fillers_enabled_with_empty_catalog_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-empty filler_phrases"):
            validate_config(VoicePipelineConfig(fillers_enabled=True, filler_phrases=()))

    def test_fillers_disabled_with_empty_catalog_accepted(self) -> None:
        """Disabling fillers makes the empty catalog harmless."""
        validate_config(VoicePipelineConfig(fillers_enabled=False, filler_phrases=()))

    def test_filler_phrases_above_catalog_ceiling_rejected(self) -> None:
        from sovyx.voice.pipeline._config import _FILLER_PHRASES_MAX

        too_many = tuple(f"phrase {i}" for i in range(_FILLER_PHRASES_MAX + 1))
        with pytest.raises(ValueError, match="filler_phrases catalog"):
            validate_config(VoicePipelineConfig(filler_phrases=too_many))

    def test_empty_filler_phrase_rejected(self) -> None:
        with pytest.raises(ValueError, match=r"filler_phrases\[1\]"):
            validate_config(
                VoicePipelineConfig(
                    filler_phrases=("Let me think...", "", "One moment..."),
                ),
            )

    def test_whitespace_filler_phrase_rejected(self) -> None:
        with pytest.raises(ValueError, match=r"filler_phrases\[0\]"):
            validate_config(
                VoicePipelineConfig(filler_phrases=("   \t\n   ", "Sure...")),
            )

    def test_default_config_passes_hardened_validation(self) -> None:
        """Backwards-compat regression — the shipped default must
        continue to pass all the new hardening checks."""
        validate_config(VoicePipelineConfig())


class TestConfigBoundsConstants:
    """Mission #11/#37/#38 — public-surface tuning constants don't drift."""

    def test_filler_delay_ms_max(self) -> None:
        from sovyx.voice.pipeline._config import _FILLER_DELAY_MS_MAX

        assert _FILLER_DELAY_MS_MAX == 10_000

    def test_silence_frames_end_max(self) -> None:
        from sovyx.voice.pipeline._config import _SILENCE_FRAMES_END_MAX

        assert _SILENCE_FRAMES_END_MAX == 250  # noqa: PLR2004

    def test_max_recording_frames_max(self) -> None:
        from sovyx.voice.pipeline._config import _MAX_RECORDING_FRAMES_MAX

        assert _MAX_RECORDING_FRAMES_MAX == 1_875

    def test_barge_in_threshold_max(self) -> None:
        from sovyx.voice.pipeline._config import _BARGE_IN_THRESHOLD_MAX

        assert _BARGE_IN_THRESHOLD_MAX == 50  # noqa: PLR2004

    def test_filler_phrases_max(self) -> None:
        from sovyx.voice.pipeline._config import _FILLER_PHRASES_MAX

        assert _FILLER_PHRASES_MAX == 50  # noqa: PLR2004


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
# M2 wire-up — USE depth + saturation_pct on enqueue
# ===========================================================================


class TestAudioOutputQueueM2WireUp:
    """AudioOutputQueue.enqueue must emit M2 USE telemetry.

    Mirrors the four cognitive-stage RED wire-ups (capture, VAD,
    STT, TTS) — proves the M2 USE foundation is wired into the
    output mixer queue too.
    """

    @pytest.mark.asyncio
    async def test_enqueue_records_queue_depth(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:

        recorded: list[tuple[Any, int, int]] = []

        from sovyx.voice._stage_metrics import VoiceStage
        from sovyx.voice.pipeline import _output_queue as oq_mod

        def _capture(stage: Any, depth: int, capacity: int) -> None:
            recorded.append((stage, depth, capacity))

        monkeypatch.setattr(oq_mod, "record_queue_depth", _capture)

        q = AudioOutputQueue()
        await q.enqueue(_audio_chunk(100))

        assert len(recorded) == 1
        stage, depth, capacity = recorded[0]
        assert stage == VoiceStage.OUTPUT
        assert depth == 1
        assert capacity == 256  # noqa: PLR2004 — default reference

    @pytest.mark.asyncio
    async def test_capacity_reference_is_configurable(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:

        recorded: list[tuple[Any, int, int]] = []

        from sovyx.voice.pipeline import _output_queue as oq_mod

        def _capture(stage: Any, depth: int, capacity: int) -> None:
            recorded.append((stage, depth, capacity))

        monkeypatch.setattr(oq_mod, "record_queue_depth", _capture)

        q = AudioOutputQueue(usage_capacity_reference=64)
        await q.enqueue(_audio_chunk(100))

        assert recorded[0][2] == 64  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_each_enqueue_records_growing_depth(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:

        recorded: list[tuple[Any, int, int]] = []

        from sovyx.voice.pipeline import _output_queue as oq_mod

        def _capture(stage: Any, depth: int, capacity: int) -> None:
            recorded.append((stage, depth, capacity))

        monkeypatch.setattr(oq_mod, "record_queue_depth", _capture)

        q = AudioOutputQueue()
        for _ in range(3):
            await q.enqueue(_audio_chunk(100))

        depths = [d for (_, d, _) in recorded]
        assert depths == [1, 2, 3]


# ===========================================================================
# TS3 chaos wire-up — OUTPUT_QUEUE_DROP injection
# ===========================================================================


class TestAudioOutputQueueChaosWireUp:
    """AudioOutputQueue.enqueue must honour the chaos injector.

    With chaos enabled, an artificial saturation reading is reported
    (depth = 2x capacity) — exercises the M2 USE
    voice.queue.saturation_overflow WARN path.
    """

    @pytest.mark.asyncio
    async def test_chaos_disabled_no_saturation_record(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from sovyx.voice._chaos import _ENABLED_ENV_VAR, _RATE_ENV_VAR_PREFIX
        from sovyx.voice.pipeline import _output_queue as oq_mod

        recorded: list[tuple[Any, int, int]] = []

        def _capture(stage: Any, depth: int, capacity: int) -> None:
            recorded.append((stage, depth, capacity))

        monkeypatch.setattr(oq_mod, "record_queue_depth", _capture)
        monkeypatch.delenv(_ENABLED_ENV_VAR, raising=False)
        monkeypatch.setenv(f"{_RATE_ENV_VAR_PREFIX}OUTPUT_QUEUE_DROP_PCT", "100")

        q = AudioOutputQueue()
        await q.enqueue(_audio_chunk(100))

        # No chaos injection — only the normal record_queue_depth call.
        assert len(recorded) == 1

    @pytest.mark.asyncio
    async def test_chaos_at_100_pct_records_saturation_overflow(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from sovyx.voice._chaos import _ENABLED_ENV_VAR, _RATE_ENV_VAR_PREFIX
        from sovyx.voice.pipeline import _output_queue as oq_mod

        recorded: list[tuple[Any, int, int]] = []

        def _capture(stage: Any, depth: int, capacity: int) -> None:
            recorded.append((stage, depth, capacity))

        monkeypatch.setattr(oq_mod, "record_queue_depth", _capture)
        monkeypatch.setenv(_ENABLED_ENV_VAR, "true")
        monkeypatch.setenv(f"{_RATE_ENV_VAR_PREFIX}OUTPUT_QUEUE_DROP_PCT", "100")

        q = AudioOutputQueue(usage_capacity_reference=64)
        await q.enqueue(_audio_chunk(100))

        # Chaos injected the synthetic over-cap reading (depth =
        # 2 * capacity = 128, capacity = 64) BEFORE the normal
        # depth=1 reading.
        assert len(recorded) == 2  # noqa: PLR2004
        synthetic, normal = recorded
        assert synthetic[1] == 128 and synthetic[2] == 64  # noqa: PLR2004
        assert normal[1] == 1 and normal[2] == 64  # noqa: PLR2004


# ===========================================================================
# O1 wire-up — PipelineStateMachine observer in the orchestrator
# ===========================================================================


class TestPipelineStateMachineWireUp:
    """The orchestrator's _state setter must record every transition.

    Wire-up is via property-setter interception — zero call-site
    changes, every existing ``self._state = X`` flows through the
    observer automatically. Adoption is observability-grade
    (lenient mode default), so invalid transitions log WARN
    without raising.
    """

    @pytest.mark.asyncio
    async def test_orchestrator_initialises_state_machine(self) -> None:
        from sovyx.voice.pipeline._state_machine import PipelineStateMachine

        pipeline, _ = _make_pipeline()
        assert isinstance(pipeline._state_machine, PipelineStateMachine)
        # Transition count should still be 0 — construction uses the
        # backing _state_value directly, bypassing the setter (per
        # the orchestrator's own design comment).
        assert pipeline._state_machine.transition_count == 0
        assert pipeline._state_machine.current_state == VoicePipelineState.IDLE

    @pytest.mark.asyncio
    async def test_state_assignment_records_transition(self) -> None:
        pipeline, _ = _make_pipeline()
        # Direct setter use (legitimate orchestrator pattern).
        pipeline._state = VoicePipelineState.WAKE_DETECTED
        assert pipeline._state_machine.transition_count == 1
        assert pipeline._state_machine.current_state == VoicePipelineState.WAKE_DETECTED
        history = pipeline._state_machine.history()
        assert len(history) == 1
        assert history[0].from_state == VoicePipelineState.IDLE
        assert history[0].to_state == VoicePipelineState.WAKE_DETECTED
        assert history[0].valid is True

    @pytest.mark.asyncio
    async def test_self_loop_recorded_in_history(self) -> None:
        """IDLE → IDLE is a legitimate self-loop in the canonical
        table — orchestrator does this on shutdown / reset paths."""
        pipeline, _ = _make_pipeline()
        pipeline._state = VoicePipelineState.IDLE
        assert pipeline._state_machine.transition_count == 1

    @pytest.mark.asyncio
    async def test_invalid_transition_logged_not_raised(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Lenient mode (default) — invalid transition emits WARN
        without raising, so adoption has zero behavioural risk."""
        import logging

        pipeline, _ = _make_pipeline()
        with caplog.at_level(logging.WARNING):
            # IDLE → THINKING is rejected by the canonical table.
            pipeline._state = VoicePipelineState.THINKING
        # No exception raised; current_state still advanced.
        assert pipeline._state_machine.current_state == VoicePipelineState.THINKING
        assert pipeline._state_machine.invalid_transition_count == 1
        # Structured WARN fired.
        assert any("pipeline.state.invalid_transition" in str(r.msg) for r in caplog.records)


# ===========================================================================
# TS3 chaos wire-up — PIPELINE_INVALID_TRANSITION injection
# ===========================================================================


class TestPipelineInvalidTransitionChaos:
    """Orchestrator state setter must honour the chaos injector.

    With chaos enabled, every state mutation ALSO triggers a
    synthetic invalid transition (IDLE→THINKING) through the O1
    validator — exercises the lenient-mode WARN path under
    realistic operating conditions. The orchestrator's actual
    state remains intact.
    """

    @pytest.mark.asyncio
    async def test_chaos_disabled_no_synthetic_transition(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from sovyx.voice._chaos import _ENABLED_ENV_VAR, _RATE_ENV_VAR_PREFIX

        monkeypatch.delenv(_ENABLED_ENV_VAR, raising=False)
        monkeypatch.setenv(
            f"{_RATE_ENV_VAR_PREFIX}PIPELINE_INVALID_TRANSITION_PCT",
            "100",
        )

        pipeline, _ = _make_pipeline()
        # Trigger a valid transition.
        pipeline._state = VoicePipelineState.WAKE_DETECTED
        # No chaos = no invalid_transition_count bump.
        assert pipeline._state_machine.invalid_transition_count == 0

    @pytest.mark.asyncio
    async def test_chaos_at_100_pct_injects_invalid_transition(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Chaos fires synthetic IDLE→THINKING through the validator
        on every state mutation — the invalid_transition_count grows
        by 1 per real transition, and the actual orchestrator state
        flows normally."""
        from sovyx.voice._chaos import _ENABLED_ENV_VAR, _RATE_ENV_VAR_PREFIX

        monkeypatch.setenv(_ENABLED_ENV_VAR, "true")
        monkeypatch.setenv(
            f"{_RATE_ENV_VAR_PREFIX}PIPELINE_INVALID_TRANSITION_PCT",
            "100",
        )

        pipeline, _ = _make_pipeline()
        pipeline._state = VoicePipelineState.WAKE_DETECTED
        # Real transition happened normally.
        assert pipeline._state_machine.current_state == VoicePipelineState.THINKING
        # The synthetic IDLE→THINKING was the LAST recorded transition.
        # invalid_transition_count = 1 (the synthetic one).
        assert pipeline._state_machine.invalid_transition_count == 1


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
    async def test_stt_rejection_reason_emits_dropped_event(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """S1/S2 wire-up: when STT returns a rejection_reason (not just
        empty text), the orchestrator must emit
        ``voice.stt.transcription_dropped`` with the reason — distinct
        from "user said nothing" (no rejection_reason)."""
        # Build a pipeline whose STT mock returns text="" + rejection_reason
        # set to "hallucination_stoplist" — simulating an S1 reject.
        config = VoicePipelineConfig(
            mind_id="test-mind",
            wake_word_enabled=True,
            barge_in_enabled=False,
            fillers_enabled=False,
            filler_delay_ms=100,
            silence_frames_end=3,
            max_recording_frames=10,
        )
        vad = _make_vad(speech=True)
        ww = _make_wake_word(detected=True)
        stt = _make_stt(text="", rejection_reason="hallucination_stoplist")
        tts = _make_tts()
        bus = _make_event_bus()

        pipeline = VoicePipeline(
            config=config,
            vad=vad,
            wake_word=ww,
            stt=stt,
            tts=tts,
            event_bus=bus,
            on_perception=AsyncMock(),
        )

        caplog.set_level(logging.WARNING, logger=_ORCH_LOGGER)
        await pipeline.start()

        with patch.object(_pipeline_mod, "_play_audio", new_callable=AsyncMock):
            await pipeline.feed_frame(_speech_frame())
        await pipeline.feed_frame(_speech_frame())
        vad.process_frame.return_value = _vad_event(False)
        for _ in range(3):
            result = await pipeline.feed_frame(_silence_frame())

        # Pre-S1/S2 wire-up returned event="empty_transcription";
        # post wire-up returns "transcription_dropped" with the reason.
        assert result["event"] == "transcription_dropped"
        assert result["rejection_reason"] == "hallucination_stoplist"
        # Structured event fired so dashboards can attribute.
        events = _events_of(caplog, "voice.stt.transcription_dropped")
        assert len(events) == 1
        assert events[0]["voice.rejection_reason"] == "hallucination_stoplist"
        assert "user_did_not_get_a_response" in str(events[0]["voice.action_required"])

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


# ===========================================================================
# O3: Frame-drop detection — absolute budget + cumulative drift
# ===========================================================================
#
# Pre-O3 the pipeline only checked ``gap > 2× expected``. That hides
# the "all frames consistently late" failure mode: every frame at 1.5×
# the expected interval produces no warning while cumulative latency
# audibly degrades the response loop. O3 keeps the per-frame absolute
# budget (perceptually meaningful) and adds a rolling-window drift
# detector so sustained-degradation conditions surface independently.
#
# Reference: MISSION-voice-mixer-enterprise-refactor-2026-04-25 §3.6, O3.


class TestFrameDropDetectionO3:
    """O3 absolute-budget + cumulative-drift detectors."""

    def test_frame_drop_constants_match_canonical(self) -> None:
        """Public-surface tuning constants must not drift silently."""
        from sovyx.voice.pipeline import _orchestrator as mod

        assert mod._FRAME_DROP_ABSOLUTE_BUDGET_S == 0.064  # noqa: PLR2004
        assert mod._FRAME_DROP_DRIFT_RATIO == 1.10  # noqa: PLR2004
        assert mod._FRAME_DROP_DRIFT_WINDOW_FRAMES == 32  # noqa: PLR2004
        assert mod._FRAME_DROP_DRIFT_RATE_LIMIT_S == 1.0

    def _drive_inter_arrival(
        self,
        pipeline: VoicePipeline,
        gaps_s: list[float],
        *,
        start_time: float = 1000.0,
    ) -> None:
        """Synchronously walk ``_check_frame_drop_signals`` across ``gaps_s``.

        Bypasses ``feed_frame``'s VAD/STT/TTS path (which would need
        coroutine plumbing). The signal logic under test is pure
        sync — driving ``_check_frame_drop_signals`` directly with an
        injected monotonic gives us deterministic frame-drop tests
        without needing fake VAD frames.
        """
        clock_t = start_time
        # First frame initialises the monotonic anchor (no inter-arrival yet).
        pipeline._check_frame_drop_signals(clock_t)
        pipeline._last_frame_monotonic = clock_t
        for gap in gaps_s:
            clock_t += gap
            pipeline._check_frame_drop_signals(clock_t)
            pipeline._last_frame_monotonic = clock_t

    @pytest.mark.asyncio
    async def test_no_warning_when_cadence_matches_expected(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger=_ORCH_LOGGER)
        pipeline, _ = _make_pipeline()
        await pipeline.start()
        # All frames arrive at exactly the expected cadence (32 ms).
        expected = pipeline._expected_frame_interval_s
        self._drive_inter_arrival(pipeline, [expected] * 50)
        assert _events_of(caplog, "voice.frame.drop_detected") == []
        assert _events_of(caplog, "voice.frame.cumulative_drift_detected") == []

    @pytest.mark.asyncio
    async def test_absolute_budget_warning_on_single_late_frame(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger=_ORCH_LOGGER)
        pipeline, _ = _make_pipeline()
        await pipeline.start()
        from sovyx.voice.pipeline import _orchestrator as mod

        expected = pipeline._expected_frame_interval_s
        # One large gap above the absolute budget (64 ms).
        late_gap = mod._FRAME_DROP_ABSOLUTE_BUDGET_S + 0.010  # 74 ms
        self._drive_inter_arrival(pipeline, [expected, late_gap, expected])
        events = _events_of(caplog, "voice.frame.drop_detected")
        assert len(events) == 1
        assert events[0]["voice.threshold_kind"] == "absolute_budget"
        assert events[0]["voice.gap_ms"] == pytest.approx(74.0, abs=0.5)

    @pytest.mark.asyncio
    async def test_no_drift_warning_below_ratio_threshold(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """5% sustained drift (below 10% threshold) must NOT fire."""
        caplog.set_level(logging.WARNING, logger=_ORCH_LOGGER)
        pipeline, _ = _make_pipeline()
        await pipeline.start()
        expected = pipeline._expected_frame_interval_s
        # 5% drift — below the 10% threshold.
        slightly_late = expected * 1.05
        # 50 frames so the rolling window fills up.
        self._drive_inter_arrival(pipeline, [slightly_late] * 50)
        assert _events_of(caplog, "voice.frame.cumulative_drift_detected") == []

    @pytest.mark.asyncio
    async def test_drift_warning_when_all_frames_consistently_late(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The exact pre-O3 silent-failure mode: 50% drift every frame.

        Pre-O3 the relative ``2× expected`` check NEVER fires for this
        pattern (1.5× < 2×), but cumulative latency audibly degrades.
        With O3 the rolling-window drift detector catches it.
        """
        caplog.set_level(logging.WARNING, logger=_ORCH_LOGGER)
        pipeline, _ = _make_pipeline()
        await pipeline.start()
        expected = pipeline._expected_frame_interval_s
        # 50% sustained drift — well above 10% threshold.
        drifted = expected * 1.5
        from sovyx.voice.pipeline import _orchestrator as mod

        # Need to fill the window (32 frames) before drift fires.
        self._drive_inter_arrival(pipeline, [drifted] * mod._FRAME_DROP_DRIFT_WINDOW_FRAMES)
        events = _events_of(caplog, "voice.frame.cumulative_drift_detected")
        assert len(events) == 1
        assert events[0]["voice.threshold_kind"] == "rolling_window_drift"
        assert events[0]["voice.drift_ratio"] == pytest.approx(1.5, abs=0.01)

    @pytest.mark.asyncio
    async def test_drift_warning_rate_limited(self, caplog: pytest.LogCaptureFixture) -> None:
        """Sustained drift must produce ≤1 event per rate-limit window."""
        caplog.set_level(logging.WARNING, logger=_ORCH_LOGGER)
        pipeline, _ = _make_pipeline()
        await pipeline.start()
        expected = pipeline._expected_frame_interval_s
        drifted = expected * 1.5
        from sovyx.voice.pipeline import _orchestrator as mod

        # Fill window + many extra frames within the same rate-limit window.
        # 32 frames at drifted=48ms = 1.536s — already past the 1s rate limit
        # window when window-fill completes. So we test with less frames OR
        # we expect just 1 event since drift detection happens on EACH frame.
        # Use a tighter drift_window-only test to ensure proper rate limit.
        # Drive 32 frames quickly → first emission. Then drive more frames
        # at unchanged drifted cadence — should NOT re-emit because
        # rate-limit is gauged by the SAME monotonic clock the frames
        # advance.
        gaps = [drifted] * mod._FRAME_DROP_DRIFT_WINDOW_FRAMES
        # Total monotonic advance = 32 * 48ms = 1.536s — past the 1s
        # rate-limit, so we instead use shorter-than-expected drifted
        # increments by injecting an artificially-tight monotonic clock.
        # Easier: drive less drift per frame to fit window-fill within
        # 1 second.
        small_drift = expected * 1.15  # 36.8ms
        # 32 frames * 36.8ms = 1.18s (just past rate limit), so reduce
        # frames slightly to stay inside the window.
        # Actually, the rate-limit is measured between WARNING emissions,
        # not from t=0. First warning happens at frame 32 (window full).
        # Subsequent frames at 36.8ms each — we need (next - first) < 1s
        # to verify rate-limiting suppression. So use ≤27 frames after
        # the first emission (27 * 36.8 = ~993ms < 1s).
        self._drive_inter_arrival(
            pipeline, [small_drift] * (mod._FRAME_DROP_DRIFT_WINDOW_FRAMES + 27)
        )
        events = _events_of(caplog, "voice.frame.cumulative_drift_detected")
        assert len(events) == 1, (
            f"expected exactly 1 event under rate-limit window, got {len(events)}"
        )

    @pytest.mark.asyncio
    async def test_drift_re_arms_after_rate_limit_window(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """After rate-limit window elapses, a new drift event fires."""
        caplog.set_level(logging.WARNING, logger=_ORCH_LOGGER)
        pipeline, _ = _make_pipeline()
        await pipeline.start()
        from sovyx.voice.pipeline import _orchestrator as mod

        expected = pipeline._expected_frame_interval_s
        drifted = expected * 1.5  # 48ms
        # 32 frames * 48ms = 1.536s — second emission becomes eligible
        # after the rate-limit elapses (~1s), so the second half of the
        # 50-frame run produces a new event.
        self._drive_inter_arrival(
            pipeline,
            [drifted] * (mod._FRAME_DROP_DRIFT_WINDOW_FRAMES * 2),
        )
        events = _events_of(caplog, "voice.frame.cumulative_drift_detected")
        # First fires at frame 32 (~1.536s in). Second fires after rate
        # limit elapses — the loop drives (drift_window * 2) = 64 frames
        # total = ~3.07s. Rate-limit is 1s → we expect at least 2 events.
        assert len(events) >= 2  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_first_frame_never_fires_either_signal(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The first frame can't produce inter-arrival, so no event fires."""
        caplog.set_level(logging.WARNING, logger=_ORCH_LOGGER)
        pipeline, _ = _make_pipeline()
        await pipeline.start()
        # Just one anchor — _check_frame_drop_signals returns early.
        pipeline._check_frame_drop_signals(1000.0)
        assert _events_of(caplog, "voice.frame.drop_detected") == []
        assert _events_of(caplog, "voice.frame.cumulative_drift_detected") == []

    @pytest.mark.asyncio
    async def test_recent_intervals_window_is_bounded(self) -> None:
        """The rolling-window deque must stay constant size."""
        pipeline, _ = _make_pipeline()
        await pipeline.start()
        from sovyx.voice.pipeline import _orchestrator as mod

        expected = pipeline._expected_frame_interval_s
        # Drive way more than the window size — ensure no unbounded growth.
        self._drive_inter_arrival(pipeline, [expected] * 200)
        assert len(pipeline._recent_frame_intervals) == mod._FRAME_DROP_DRIFT_WINDOW_FRAMES


# ===========================================================================
# T1: Atomic cancellation chain (Ring 5 transactional barge-in)
# ===========================================================================
#
# Pre-T1 the barge-in path stopped the output queue but left in-flight TTS
# synthesis running and didn't signal upstream LLM token streams to stop.
# T1 introduces a transactional four-step chain executed under a single
# asyncio.Lock so concurrent barge-ins serialise; per-step success/failure
# is surfaced on a structured ``voice.tts.cancellation_chain`` event.
#
# Reference: MISSION-voice-mixer-enterprise-refactor-2026-04-25 §3.4, T1.


class TestCancellationChainT1:
    """Four-step transactional cancellation chain with structured event."""

    @pytest.mark.asyncio
    async def test_lock_attribute_initialised(self) -> None:
        pipeline, _ = _make_pipeline()
        assert isinstance(pipeline._cancellation_lock, asyncio.Lock)
        assert pipeline._in_flight_tts_tasks == set()
        assert pipeline._llm_cancel_hook is None

    @pytest.mark.asyncio
    async def test_chain_emits_structured_event_with_per_step_verdicts(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level(logging.INFO, logger=_ORCH_LOGGER)
        pipeline, _ = _make_pipeline()
        await pipeline.start()
        await pipeline.cancel_speech_chain(reason="test")
        events = _events_of(caplog, "voice.tts.cancellation_chain")
        assert len(events) == 1
        evt = events[0]
        assert evt["voice.reason"] == "test"
        assert evt["voice.step_output_flush"] == "ok"
        assert evt["voice.step_tts_tasks_cancel"] == "ok"
        assert evt["voice.step_llm_cancel"] == "no_hook_registered"
        assert evt["voice.step_filler_and_gate"] == "ok"
        assert evt["voice.tasks_cancelled"] == 0
        assert evt["voice.tasks_timed_out"] == 0
        assert evt["voice.has_llm_hook"] is False

    @pytest.mark.asyncio
    async def test_in_flight_tts_task_cancelled_by_chain(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A long-running TTS task is cancelled when the chain fires."""
        caplog.set_level(logging.INFO, logger=_ORCH_LOGGER)
        pipeline, _ = _make_pipeline()
        await pipeline.start()

        async def _slow_synth() -> None:
            await asyncio.sleep(10.0)

        task = asyncio.create_task(_slow_synth())
        pipeline._track_tts_task(task)
        try:
            await pipeline.cancel_speech_chain(reason="barge_in")
            assert task.cancelled() or task.done()
        finally:
            pipeline._untrack_tts_task(task)

        events = _events_of(caplog, "voice.tts.cancellation_chain")
        assert len(events) == 1
        assert events[0]["voice.tasks_cancelled"] == 1
        assert events[0]["voice.tasks_timed_out"] == 0

    @pytest.mark.asyncio
    async def test_register_llm_cancel_hook_sets_attribute(self) -> None:
        pipeline, _ = _make_pipeline()
        called = {"n": 0}

        async def _hook() -> None:
            called["n"] += 1

        pipeline.register_llm_cancel_hook(_hook)
        assert pipeline._llm_cancel_hook is _hook
        # Unwire
        pipeline.register_llm_cancel_hook(None)
        assert pipeline._llm_cancel_hook is None

    @pytest.mark.asyncio
    async def test_chain_invokes_registered_llm_cancel_hook(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level(logging.INFO, logger=_ORCH_LOGGER)
        pipeline, _ = _make_pipeline()
        await pipeline.start()
        called = {"n": 0}

        async def _hook() -> None:
            called["n"] += 1

        pipeline.register_llm_cancel_hook(_hook)
        await pipeline.cancel_speech_chain(reason="barge_in")
        assert called["n"] == 1
        events = _events_of(caplog, "voice.tts.cancellation_chain")
        assert events[0]["voice.step_llm_cancel"] == "ok"
        assert events[0]["voice.has_llm_hook"] is True

    @pytest.mark.asyncio
    async def test_chain_records_failed_when_llm_hook_raises(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # Chain summary fires at INFO; hook failure also logs WARNING.
        # Capture INFO so we see both.
        caplog.set_level(logging.INFO, logger=_ORCH_LOGGER)
        pipeline, _ = _make_pipeline()
        await pipeline.start()

        async def _bad_hook() -> None:
            raise RuntimeError("hook bug")

        pipeline.register_llm_cancel_hook(_bad_hook)
        # Chain itself MUST NOT raise — it shields the hook.
        await pipeline.cancel_speech_chain(reason="barge_in")
        events = _events_of(caplog, "voice.tts.cancellation_chain")
        assert events[0]["voice.step_llm_cancel"] == "failed"

    @pytest.mark.asyncio
    async def test_chain_records_timeout_when_llm_hook_hangs(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level(logging.INFO, logger=_ORCH_LOGGER)
        pipeline, _ = _make_pipeline()
        await pipeline.start()

        async def _hung_hook() -> None:
            await asyncio.sleep(10.0)

        pipeline.register_llm_cancel_hook(_hung_hook)
        await pipeline.cancel_speech_chain(reason="barge_in")
        events = _events_of(caplog, "voice.tts.cancellation_chain")
        assert events[0]["voice.step_llm_cancel"] == "timeout"

    @pytest.mark.asyncio
    async def test_concurrent_chains_serialised_by_lock(self) -> None:
        """Two concurrent cancellation invocations serialise — the
        second observes the post-first state."""
        pipeline, _ = _make_pipeline()
        await pipeline.start()
        # No tasks in flight, so both chains should complete cleanly
        # but they must NOT interleave (lock guarantees this).
        results = await asyncio.gather(
            pipeline.cancel_speech_chain(reason="first"),
            pipeline.cancel_speech_chain(reason="second"),
        )
        assert results == [None, None]

    @pytest.mark.asyncio
    async def test_chain_cancels_filler_task(self) -> None:
        pipeline, _ = _make_pipeline(fillers_enabled=True)
        await pipeline.start()

        async def _filler() -> bool:
            await asyncio.sleep(10.0)
            return False

        pipeline._filler_task = asyncio.create_task(_filler())
        await pipeline.cancel_speech_chain(reason="barge_in")
        # Filler task is cancelled by step 4.
        assert pipeline._filler_task is None

    @pytest.mark.asyncio
    async def test_chain_releases_self_feedback_gate(self) -> None:
        pipeline, _ = _make_pipeline()

        class _Gate:
            def __init__(self) -> None:
                self.tts_end_called = 0

            def on_tts_start(self) -> None: ...

            def on_tts_end(self) -> None:
                self.tts_end_called += 1

        gate = _Gate()
        pipeline._self_feedback_gate = gate  # type: ignore[assignment]
        await pipeline.start()
        await pipeline.cancel_speech_chain(reason="barge_in")
        assert gate.tts_end_called == 1

    @pytest.mark.asyncio
    async def test_track_and_untrack_tts_task(self) -> None:
        pipeline, _ = _make_pipeline()

        async def _noop() -> None:
            return None

        task = asyncio.create_task(_noop())
        pipeline._track_tts_task(task)
        assert task in pipeline._in_flight_tts_tasks
        pipeline._untrack_tts_task(task)
        assert task not in pipeline._in_flight_tts_tasks
        # Untrack is idempotent.
        pipeline._untrack_tts_task(task)
        assert task not in pipeline._in_flight_tts_tasks
        await task

    # ── Band-aid #15 final fix: text-buffer cleanup in chain ───
    @pytest.mark.asyncio
    async def test_cancel_chain_clears_text_buffer(self) -> None:
        """Step 5: cancel_speech_chain unconditionally clears
        ``_text_buffer``. Pre-step-5 the buffer kept a residue like
        "Hello, this is a long respo" after a mid-stream barge-in,
        and the next utterance prepended that residue to its own
        output."""
        pipeline, _ = _make_pipeline()
        await pipeline.start()
        # Simulate mid-stream state: text buffered, awaiting more chunks.
        pipeline._text_buffer = "Hello, this is a long respo"
        await pipeline.cancel_speech_chain(reason="barge_in")
        assert pipeline._text_buffer == ""

    @pytest.mark.asyncio
    async def test_cancel_chain_clears_buffer_on_empty_state(self) -> None:
        """Buffer cleanup is unconditional — works even when
        the buffer was already empty (idempotent)."""
        pipeline, _ = _make_pipeline()
        await pipeline.start()
        assert pipeline._text_buffer == ""
        await pipeline.cancel_speech_chain(reason="shutdown")
        assert pipeline._text_buffer == ""

    @pytest.mark.asyncio
    async def test_cancel_chain_emits_buffer_chars_dropped_field(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The structured ``voice.tts.cancellation_chain`` event
        records how many chars the cleanup dropped — operators
        see whether barge-in interrupted a long generation or a
        short one."""
        import logging

        pipeline, _ = _make_pipeline()
        await pipeline.start()
        pipeline._text_buffer = "X" * 42
        with caplog.at_level(logging.INFO):
            await pipeline.cancel_speech_chain(reason="manual_cancel")
        events = [
            r.msg
            for r in caplog.records
            if isinstance(r.msg, dict) and r.msg.get("event") == "voice.tts.cancellation_chain"
        ]
        assert len(events) >= 1
        assert events[-1].get("voice.text_buffer_chars_dropped") == 42  # noqa: PLR2004
        assert events[-1].get("voice.step_text_buffer_cleanup") == "ok"


# ===========================================================================
# Band-aid #50 — VAD inference timeout guard
# ===========================================================================


class TestVADInferenceTimeoutGuard:
    """Band-aid #50: ``feed_frame`` wraps the VAD ``to_thread`` call in
    a per-frame timeout (``_VAD_INFERENCE_TIMEOUT_S``). On timeout the
    frame is skipped (returns ``vad_timeout`` event) and a rate-limited
    structured WARN attributes the cause to VAD specifically — vs. the
    downstream O3 frame-drop signal which only sees the symptom.
    """

    @pytest.mark.asyncio
    async def test_vad_timeout_skips_frame_and_increments_counter(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A timed-out VAD inference returns a vad_timeout event,
        increments the lifetime counter, and emits the WARN."""
        pipeline, _ = _make_pipeline()
        await pipeline.start()

        # Patch wait_for in the orchestrator's namespace to raise
        # TimeoutError, simulating a VAD inference that exceeds the
        # 250 ms budget without the test actually having to wait.
        from sovyx.voice.pipeline import _orchestrator as _orch_mod

        async def _raise_timeout(coro: Any, *_args: Any, **_kwargs: Any) -> Any:
            # Close the inner ``to_thread`` coroutine so the runtime
            # doesn't warn "coroutine was never awaited" — production
            # ``asyncio.wait_for`` awaits + cancels the coroutine
            # itself; our patched stand-in must do the same.
            if hasattr(coro, "close"):
                coro.close()
            raise TimeoutError

        with (
            caplog.at_level(logging.WARNING),
            patch.object(_orch_mod.asyncio, "wait_for", _raise_timeout),
        ):
            result = await pipeline.feed_frame(_silence_frame())

        assert result["event"] == "vad_timeout"
        assert pipeline.vad_inference_timeout_count == 1
        events = [
            r.msg
            for r in caplog.records
            if isinstance(r.msg, dict) and r.msg.get("event") == "voice.vad.inference_timeout"
        ]
        assert len(events) == 1
        evt = events[0]
        assert evt["voice.timeout_s"] == 0.250  # noqa: PLR2004
        assert evt["voice.lifetime_timeout_count"] == 1
        assert "host CPU saturation" in evt["voice.action_required"]

    @pytest.mark.asyncio
    async def test_vad_timeout_warn_rate_limited(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Multiple consecutive timeouts within the rate-limit window
        emit only one WARN — the counter still increments per timeout
        so attribution is preserved."""
        pipeline, _ = _make_pipeline()
        await pipeline.start()

        from sovyx.voice.pipeline import _orchestrator as _orch_mod

        async def _raise_timeout(coro: Any, *_args: Any, **_kwargs: Any) -> Any:
            # Close the inner ``to_thread`` coroutine so the runtime
            # doesn't warn "coroutine was never awaited" — production
            # ``asyncio.wait_for`` awaits + cancels the coroutine
            # itself; our patched stand-in must do the same.
            if hasattr(coro, "close"):
                coro.close()
            raise TimeoutError

        with (
            caplog.at_level(logging.WARNING),
            patch.object(_orch_mod.asyncio, "wait_for", _raise_timeout),
        ):
            # Three consecutive timeouts back-to-back (same
            # monotonic-tick window from caller perspective).
            for _ in range(3):
                await pipeline.feed_frame(_silence_frame())

        assert pipeline.vad_inference_timeout_count == 3
        events = [
            r.msg
            for r in caplog.records
            if isinstance(r.msg, dict) and r.msg.get("event") == "voice.vad.inference_timeout"
        ]
        # Only the first timeout fires the WARN (rate-limited).
        assert len(events) == 1

    @pytest.mark.asyncio
    async def test_vad_success_path_unchanged(self) -> None:
        """Healthy VAD (within timeout) processes normally — counter
        stays at zero, no WARN."""
        pipeline, _ = _make_pipeline(vad_speech=False)
        await pipeline.start()
        result = await pipeline.feed_frame(_silence_frame())
        # Normal IDLE path returns state, not vad_timeout.
        assert result["state"] == "IDLE"
        assert "event" not in result or result.get("event") != "vad_timeout"
        assert pipeline.vad_inference_timeout_count == 0

    @pytest.mark.asyncio
    async def test_vad_timeout_does_not_corrupt_state_machine(self) -> None:
        """Skipped frame must not advance the state machine — the
        pipeline should still be IDLE after a timed-out frame so the
        next healthy frame starts cleanly."""
        pipeline, _ = _make_pipeline(vad_speech=True, ww_detected=True)
        await pipeline.start()
        from sovyx.voice.pipeline import _orchestrator as _orch_mod

        async def _raise_timeout(coro: Any, *_args: Any, **_kwargs: Any) -> Any:
            # Close the inner ``to_thread`` coroutine so the runtime
            # doesn't warn "coroutine was never awaited" — production
            # ``asyncio.wait_for`` awaits + cancels the coroutine
            # itself; our patched stand-in must do the same.
            if hasattr(coro, "close"):
                coro.close()
            raise TimeoutError

        with patch.object(_orch_mod.asyncio, "wait_for", _raise_timeout):
            await pipeline.feed_frame(_speech_frame())
        # Despite the wake word + speech VAD that would normally
        # transition to WAKE_DETECTED, the timeout path returned
        # early so state is still IDLE.
        assert pipeline.state == VoicePipelineState.IDLE


# ===========================================================================
# Band-aid #46 — false-wake recovery via STT confidence gate
# ===========================================================================


def _make_pipeline_with_false_wake_threshold(
    *,
    threshold: float,
    stt_text: str = "hey sovyx do something",
    stt_confidence: float = 0.95,
    on_perception: AsyncMock | None = None,
) -> tuple[VoicePipeline, dict[str, Any]]:
    """Build a pipeline configured with a non-zero false-wake threshold.

    Mirrors `_make_pipeline` but parameterises the band-aid #46 gate
    AND the STT confidence so tests can drive the gate independently
    of the default-confidence helper."""
    config = VoicePipelineConfig(
        mind_id="test-mind",
        wake_word_enabled=True,
        barge_in_enabled=False,
        fillers_enabled=False,
        filler_delay_ms=100,
        silence_frames_end=3,
        max_recording_frames=10,
        false_wake_min_confidence=threshold,
    )
    vad = _make_vad(speech=True)
    ww = _make_wake_word(detected=True)
    stt = _make_stt(text=stt_text, confidence=stt_confidence)
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
    return pipeline, {"vad": vad, "ww": ww, "stt": stt, "tts": tts, "bus": bus, "config": config}


class TestFalseWakeRecovery:
    """Band-aid #46: STT-confidence gate after the wake → speech →
    transcribe path. When the operator opts-in by setting
    ``false_wake_min_confidence > 0.0``, transcriptions whose
    confidence falls below the threshold are dropped — the pipeline
    returns to IDLE without invoking the perception callback (no
    spurious LLM call). Default 0.0 = disabled (no behaviour change
    pre-adoption)."""

    def test_default_threshold_is_zero_disabled(self) -> None:
        """The factory default leaves the gate off, so existing
        behaviour is preserved unless the operator opts in."""
        config = VoicePipelineConfig(mind_id="test-mind")
        assert config.false_wake_min_confidence == 0.0

    def test_validator_accepts_zero(self) -> None:
        validate_config(VoicePipelineConfig(false_wake_min_confidence=0.0))

    def test_validator_accepts_typical_opt_in(self) -> None:
        for v in (0.1, 0.3, 0.5, 0.7, 0.95):
            validate_config(VoicePipelineConfig(false_wake_min_confidence=v))

    def test_validator_rejects_negative(self) -> None:
        with pytest.raises(ValueError, match="false_wake_min_confidence"):
            validate_config(VoicePipelineConfig(false_wake_min_confidence=-0.1))

    def test_validator_rejects_above_ceiling(self) -> None:
        with pytest.raises(ValueError, match="band-aid #46"):
            validate_config(VoicePipelineConfig(false_wake_min_confidence=1.0))

    def test_validator_rejects_unit_confusion(self) -> None:
        """A user passing 50 thinking "50%" must loud-fail."""
        with pytest.raises(ValueError, match=r"\[0\.0, 0\.99\]"):
            validate_config(VoicePipelineConfig(false_wake_min_confidence=50.0))

    @pytest.mark.asyncio
    async def test_default_pipeline_passes_low_confidence(self) -> None:
        """With the gate disabled (default 0.0), even 0.01 confidence
        text reaches the perception callback. Regression guard against
        accidental default activation."""
        cb = AsyncMock()
        pipeline, _ = _make_pipeline(
            vad_speech=True, ww_detected=True, stt_text="hello", on_perception=cb
        )
        # Override stt confidence to 0.01 — would be rejected by any
        # non-zero threshold, must pass with the default 0.0.
        await pipeline.start()
        result_obj = MagicMock()
        result_obj.text = "hello"
        result_obj.confidence = 0.01
        result_obj.language = "en"
        result_obj.rejection_reason = None
        pipeline._stt.transcribe.return_value = result_obj  # type: ignore[attr-defined]

        with patch.object(_pipeline_mod, "_play_audio", new_callable=AsyncMock):
            await pipeline.feed_frame(_speech_frame())
        await pipeline.feed_frame(_speech_frame())
        pipeline._vad.process_frame.return_value = _vad_event(False)  # type: ignore[attr-defined]
        for _ in range(3):
            await pipeline.feed_frame(_silence_frame())
        cb.assert_called_once_with("hello", "test-mind")
        assert pipeline.false_wake_rejected_count == 0

    @pytest.mark.asyncio
    async def test_below_threshold_rejected_no_perception(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Opt-in threshold 0.5 + STT confidence 0.3 → rejection.
        Perception NOT called; counter increments; WARN fires with
        the structured payload operators key on."""
        cb = AsyncMock()
        pipeline, _ = _make_pipeline_with_false_wake_threshold(
            threshold=0.5, stt_text="kjlsdf askdjf", stt_confidence=0.3, on_perception=cb
        )
        caplog.set_level(logging.WARNING, logger=_ORCH_LOGGER)
        await pipeline.start()

        with patch.object(_pipeline_mod, "_play_audio", new_callable=AsyncMock):
            await pipeline.feed_frame(_speech_frame())
        await pipeline.feed_frame(_speech_frame())
        pipeline._vad.process_frame.return_value = _vad_event(False)  # type: ignore[attr-defined]
        for _ in range(3):
            result = await pipeline.feed_frame(_silence_frame())

        assert result["state"] == "IDLE"
        assert result["event"] == "false_wake_rejected"
        assert result["confidence"] == 0.3  # noqa: PLR2004
        assert result["threshold"] == 0.5  # noqa: PLR2004
        cb.assert_not_called()
        assert pipeline.false_wake_rejected_count == 1
        events = _events_of(caplog, "voice.wake.false_positive_rejected")
        assert len(events) == 1
        evt = events[0]
        assert evt["voice.confidence"] == 0.3  # noqa: PLR2004
        assert evt["voice.threshold"] == 0.5  # noqa: PLR2004
        assert evt["voice.text_length"] == len("kjlsdf askdjf")
        assert evt["voice.lifetime_rejected_count"] == 1
        assert "false positive" in evt["voice.action_required"]

    @pytest.mark.asyncio
    async def test_above_threshold_passes_perception_invoked(self) -> None:
        """Opt-in threshold 0.5 + STT confidence 0.7 → passes."""
        cb = AsyncMock()
        pipeline, _ = _make_pipeline_with_false_wake_threshold(
            threshold=0.5, stt_text="real command", stt_confidence=0.7, on_perception=cb
        )
        await pipeline.start()
        with patch.object(_pipeline_mod, "_play_audio", new_callable=AsyncMock):
            await pipeline.feed_frame(_speech_frame())
        await pipeline.feed_frame(_speech_frame())
        pipeline._vad.process_frame.return_value = _vad_event(False)  # type: ignore[attr-defined]
        for _ in range(3):
            await pipeline.feed_frame(_silence_frame())
        cb.assert_called_once_with("real command", "test-mind")
        assert pipeline.false_wake_rejected_count == 0

    @pytest.mark.asyncio
    async def test_at_threshold_inclusive_passes(self) -> None:
        """STT confidence exactly equal to threshold → passes
        (gate is strict ``<``, not ``<=``). Documents the choice
        — equal is good enough."""
        cb = AsyncMock()
        pipeline, _ = _make_pipeline_with_false_wake_threshold(
            threshold=0.5, stt_text="edge case", stt_confidence=0.5, on_perception=cb
        )
        await pipeline.start()
        with patch.object(_pipeline_mod, "_play_audio", new_callable=AsyncMock):
            await pipeline.feed_frame(_speech_frame())
        await pipeline.feed_frame(_speech_frame())
        pipeline._vad.process_frame.return_value = _vad_event(False)  # type: ignore[attr-defined]
        for _ in range(3):
            await pipeline.feed_frame(_silence_frame())
        cb.assert_called_once()
        assert pipeline.false_wake_rejected_count == 0

    @pytest.mark.asyncio
    async def test_counter_accumulates_across_rejections(self) -> None:
        """Three consecutive low-confidence transcripts → counter == 3."""
        cb = AsyncMock()
        pipeline, refs = _make_pipeline_with_false_wake_threshold(
            threshold=0.5, stt_text="garbage", stt_confidence=0.1, on_perception=cb
        )
        await pipeline.start()
        for _ in range(3):
            with patch.object(_pipeline_mod, "_play_audio", new_callable=AsyncMock):
                await pipeline.feed_frame(_speech_frame())
            await pipeline.feed_frame(_speech_frame())
            refs["vad"].process_frame.return_value = _vad_event(False)
            for _ in range(3):
                await pipeline.feed_frame(_silence_frame())
            # Reset VAD to speech for the next wake cycle.
            refs["vad"].process_frame.return_value = _vad_event(True)
        assert pipeline.false_wake_rejected_count == 3  # noqa: PLR2004
        cb.assert_not_called()


# ===========================================================================
# Per-utterance trace ID (Ring 6 — Mission §2.6 / §9.4.6)
# ===========================================================================


class TestUtteranceTraceId:
    """Mission §2.6 Ring 6 contract — every event in the wake → STT →
    LLM → TTS chain stamps the same UUID4 trace id minted at the
    utterance boundary, the orchestrator clears the trace at every
    terminal back-to-IDLE transition, and barge-in mints a fresh trace
    for the interrupting recording.

    Closes audit Gap 3 (per-utterance trace ID at 0% adoption).
    """

    @pytest.mark.asyncio
    async def test_idle_pipeline_has_empty_trace_id(self) -> None:
        """Trace id is empty between utterances by construction."""
        pipeline, _refs = _make_pipeline()
        await pipeline.start()
        assert pipeline.current_utterance_id == ""

    @pytest.mark.asyncio
    async def test_wake_word_mints_trace_id(self) -> None:
        """Wake-word fire mints a non-empty UUID4 visible on the public
        accessor and stamped on both head events (WakeWordDetectedEvent
        + SpeechStartedEvent) with byte-for-byte equality."""
        pipeline, refs = _make_pipeline(vad_speech=True, ww_detected=True)
        await pipeline.start()

        with patch.object(_pipeline_mod, "_play_audio", new_callable=AsyncMock):
            await pipeline.feed_frame(_speech_frame())

        utterance_id = pipeline.current_utterance_id
        assert utterance_id != ""
        # UUID4 canonical 36-char form — `mint_utterance_id` uses
        # str(uuid.uuid4()), so the format is fixed.
        assert len(utterance_id) == 36  # noqa: PLR2004
        assert utterance_id.count("-") == 4  # noqa: PLR2004

        events = [call.args[0] for call in refs["bus"].emit.call_args_list]
        wake_events = [e for e in events if isinstance(e, WakeWordDetectedEvent)]
        speech_started_events = [e for e in events if isinstance(e, SpeechStartedEvent)]
        assert len(wake_events) == 1
        assert len(speech_started_events) == 1
        assert wake_events[0].utterance_id == utterance_id
        assert speech_started_events[0].utterance_id == utterance_id

    @pytest.mark.asyncio
    async def test_full_wake_cycle_carries_single_trace_id(self) -> None:
        """One wake → one STT → one TTS playback all stamp the SAME
        trace id. Mission §9.4.6 acceptance gate: the dashboard can
        join the full per-turn span set on a single utterance_id."""
        pipeline, refs = _make_pipeline(vad_speech=True, ww_detected=True, stt_text="hello")
        await pipeline.start()

        with patch.object(_pipeline_mod, "_play_audio", new_callable=AsyncMock):
            await pipeline.feed_frame(_speech_frame())
        await pipeline.feed_frame(_speech_frame())

        refs["vad"].process_frame.return_value = _vad_event(False)
        for _ in range(3):
            await pipeline.feed_frame(_silence_frame())

        # The wake-word path's id is what every emission until TTS
        # completion should carry.
        events = [call.args[0] for call in refs["bus"].emit.call_args_list]
        trace_ids = {
            getattr(e, "utterance_id", None)
            for e in events
            if isinstance(
                e,
                (
                    WakeWordDetectedEvent,
                    SpeechStartedEvent,
                    SpeechEndedEvent,
                    TranscriptionCompletedEvent,
                ),
            )
        }
        # One utterance ⇒ one trace id covering all 4 head/middle events.
        assert len(trace_ids) == 1
        the_id = trace_ids.pop()
        assert the_id is not None
        assert the_id != ""
        assert len(the_id) == 36  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_tts_completed_clears_trace_id(self) -> None:
        """After speak() returns, the trace id is back to ``""`` so
        the next utterance is guaranteed a fresh mint."""
        pipeline, refs = _make_pipeline(wake_word_enabled=False)
        await pipeline.start()

        with patch.object(_pipeline_mod, "_play_audio", new_callable=AsyncMock):
            await pipeline.speak("hello")

        assert pipeline.current_utterance_id == ""
        events = [call.args[0] for call in refs["bus"].emit.call_args_list]
        tts_started = [e for e in events if isinstance(e, TTSStartedEvent)]
        tts_completed = [e for e in events if isinstance(e, TTSCompletedEvent)]
        assert len(tts_started) == 1
        assert len(tts_completed) == 1
        # External speak() mints its own trace id since no wake came
        # before it; both events carry the same id.
        assert tts_started[0].utterance_id != ""
        assert tts_started[0].utterance_id == tts_completed[0].utterance_id

    @pytest.mark.asyncio
    async def test_sequential_utterances_get_distinct_trace_ids(self) -> None:
        """Two consecutive proactive ``speak`` calls mint two different
        UUIDs — collision would mean the clear-on-IDLE step never
        ran or the mint helper is non-pure."""
        pipeline, _refs = _make_pipeline(wake_word_enabled=False)
        await pipeline.start()

        with patch.object(_pipeline_mod, "_play_audio", new_callable=AsyncMock):
            await pipeline.speak("hello")
            first_id_after = pipeline.current_utterance_id  # cleared
            await pipeline.speak("again")
            second_id_after = pipeline.current_utterance_id  # cleared

        # Both should be cleared post-completion.
        assert first_id_after == ""
        assert second_id_after == ""
        # The actual ids on the wire must differ — otherwise the dashboard
        # cannot distinguish two turns.
        bus = pipeline._event_bus  # noqa: SLF001 — test-only
        assert bus is not None
        events = [call.args[0] for call in bus.emit.call_args_list]
        tts_completed = [e for e in events if isinstance(e, TTSCompletedEvent)]
        assert len(tts_completed) == 2  # noqa: PLR2004
        assert tts_completed[0].utterance_id != tts_completed[1].utterance_id

    @pytest.mark.asyncio
    async def test_no_wake_recording_path_mints_trace_id(self) -> None:
        """``_transition_to_recording`` (continuous-listen path with
        ``wake_word_enabled=False``) is the head of the trace and
        must mint when invoked without a prior id."""
        pipeline, refs = _make_pipeline(wake_word_enabled=False, vad_speech=True, stt_text="hi")
        await pipeline.start()

        # IDLE → speech detected → straight into RECORDING (no wake gate).
        await pipeline.feed_frame(_speech_frame())

        utterance_id = pipeline.current_utterance_id
        assert utterance_id != ""
        events = [call.args[0] for call in refs["bus"].emit.call_args_list]
        # No WakeWordDetectedEvent in the no-wake path; SpeechStarted
        # is the head and must carry the trace.
        wake_events = [e for e in events if isinstance(e, WakeWordDetectedEvent)]
        assert wake_events == []
        speech_started = [e for e in events if isinstance(e, SpeechStartedEvent)]
        assert len(speech_started) == 1
        assert speech_started[0].utterance_id == utterance_id

    @pytest.mark.asyncio
    async def test_stt_error_clears_trace_id(self) -> None:
        """STT failure path clears the trace id on the IDLE return so
        the next utterance is guaranteed a fresh mint, and the
        PipelineErrorEvent stamps the failed utterance's id for
        post-incident attribution."""
        pipeline, refs = _make_pipeline(vad_speech=True, ww_detected=True, stt_text="hi")
        refs["stt"].transcribe.side_effect = RuntimeError("ONNX exploded")
        await pipeline.start()

        with patch.object(_pipeline_mod, "_play_audio", new_callable=AsyncMock):
            await pipeline.feed_frame(_speech_frame())
        await pipeline.feed_frame(_speech_frame())
        # Snapshot the trace id before silence triggers _end_recording.
        in_flight_id = pipeline.current_utterance_id
        assert in_flight_id != ""

        refs["vad"].process_frame.return_value = _vad_event(False)
        for _ in range(3):
            await pipeline.feed_frame(_silence_frame())

        # IDLE return cleared the id.
        assert pipeline.current_utterance_id == ""
        events = [call.args[0] for call in refs["bus"].emit.call_args_list]
        errors = [e for e in events if isinstance(e, PipelineErrorEvent)]
        assert len(errors) == 1
        assert errors[0].utterance_id == in_flight_id

    @pytest.mark.asyncio
    async def test_empty_transcription_clears_trace_id(self) -> None:
        """STT returning an empty transcript still routes the IDLE
        return through ``_clear_utterance_id`` (regression guard for
        the silent-return code path)."""
        pipeline, refs = _make_pipeline(vad_speech=True, ww_detected=True, stt_text="")
        await pipeline.start()

        with patch.object(_pipeline_mod, "_play_audio", new_callable=AsyncMock):
            await pipeline.feed_frame(_speech_frame())
        await pipeline.feed_frame(_speech_frame())
        refs["vad"].process_frame.return_value = _vad_event(False)
        for _ in range(3):
            await pipeline.feed_frame(_silence_frame())

        assert pipeline.current_utterance_id == ""

    @pytest.mark.asyncio
    async def test_existing_id_preserved_on_speak_after_wake(self) -> None:
        """When a wake-word path has already minted an id and the
        cognitive layer then calls ``speak`` to respond, the same id
        is reused — wake → STT → think → speak is one logical
        utterance, not two."""
        cb = AsyncMock()
        pipeline, refs = _make_pipeline(
            vad_speech=True, ww_detected=True, stt_text="hi", on_perception=cb
        )
        await pipeline.start()

        with patch.object(_pipeline_mod, "_play_audio", new_callable=AsyncMock):
            await pipeline.feed_frame(_speech_frame())
        await pipeline.feed_frame(_speech_frame())
        refs["vad"].process_frame.return_value = _vad_event(False)
        for _ in range(3):
            await pipeline.feed_frame(_silence_frame())

        # After STT completes the pipeline is in THINKING with the
        # wake-minted id still set.
        wake_minted_id = pipeline.current_utterance_id
        assert wake_minted_id != ""

        with patch.object(_pipeline_mod, "_play_audio", new_callable=AsyncMock):
            await pipeline.speak("the answer")

        events = [call.args[0] for call in refs["bus"].emit.call_args_list]
        tts_started = [e for e in events if isinstance(e, TTSStartedEvent)]
        assert len(tts_started) == 1
        # Same id as the wake mint — single trace covers the entire turn.
        assert tts_started[0].utterance_id == wake_minted_id
        # Cleared on TTS completion.
        assert pipeline.current_utterance_id == ""

    def test_mint_helper_is_pure_uuid4(self) -> None:
        """Two consecutive mints produce different ids (UUID4 entropy
        contract). Guards against an accidental constant mint that
        would silently map every utterance to the same trace."""
        pipeline, _refs = _make_pipeline()
        first = pipeline._mint_new_utterance_id()  # noqa: SLF001 — test-only
        second = pipeline._mint_new_utterance_id()  # noqa: SLF001 — test-only
        assert first != second
        assert len(first) == 36  # noqa: PLR2004
        assert len(second) == 36  # noqa: PLR2004

    def test_clear_helper_is_idempotent(self) -> None:
        """Calling ``_clear_utterance_id`` when already empty is a
        no-op — guards against AttributeError on double-clear paths
        (stop() during cleanup, error handler entered twice)."""
        pipeline, _refs = _make_pipeline()
        assert pipeline.current_utterance_id == ""
        pipeline._clear_utterance_id()  # noqa: SLF001 — test-only
        assert pipeline.current_utterance_id == ""
        pipeline._clear_utterance_id()  # noqa: SLF001 — test-only
        assert pipeline.current_utterance_id == ""
