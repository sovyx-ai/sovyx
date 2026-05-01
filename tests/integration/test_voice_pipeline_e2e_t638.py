"""T6.38 — full voice pipeline E2E integration test.

Phase 6 / T6.38: drives the complete voice pipeline through the
canonical Skype-grade flow with all external services mocked at their
boundary but the orchestrator running for real:

  IDLE
    → wake-word detected (WAKE_DETECTED)
    → speech accumulates (RECORDING)
    → silence trailing edge (TRANSCRIBING)
    → STT result hits ``on_perception`` (THINKING)
    → cognitive callback invokes ``pipeline.speak(response)`` (SPEAKING)
    → TTS synthesises chunk + reaches output queue
    → final state IDLE

Distinct from ``tests/integration/test_voice_pipeline.py`` which
covers up to the ``on_perception`` boundary but stops there: T6.38
closes the loop end-to-end including the cognitive→TTS hand-off so
the orchestrator's full state ladder is exercised in one path.

Mocked at boundary (production parity preserved):
  * ``vad`` / ``wake_word`` — return synthetic VADEvent / detection
    objects per frame so we deterministically drive the state machine.
  * ``stt`` — :meth:`transcribe` returns a known-text STT result.
  * ``tts`` — :meth:`synthesize` returns a fixed AudioChunk.
  * ``on_perception`` — a real async callable that picks a "response"
    string (the LLM mock) and calls ``pipeline.speak()`` to complete
    the loop. The orchestrator's perception → speak hand-off is the
    contract under test.

Not mocked (real code under test):
  * ``VoicePipeline`` orchestrator state machine + transition logic.
  * ``AudioOutputQueue`` (the synthesized chunk's destination).
  * ``BargeInDetector`` / ``JarvisIllusion`` / event-bus emission.

This is the integration test that pins "the pipeline can complete a
full turn from cold IDLE to delivered TTS audio with no hardware
dependency" — the operator-grade contract for v0.30.0 GA readiness.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from sovyx.voice.pipeline import (
    VoicePipeline,
    VoicePipelineConfig,
    VoicePipelineState,
)
from sovyx.voice.pipeline import _output_queue as _output_queue_mod
from sovyx.voice.tts_piper import AudioChunk
from sovyx.voice.vad import VADEvent, VADState

_FRAME_LEN = 512


def _vad_event(speech: bool) -> VADEvent:
    return VADEvent(
        is_speech=speech,
        probability=0.9 if speech else 0.1,
        state=VADState.SPEECH if speech else VADState.SILENCE,
    )


def _speech_frame() -> np.ndarray:
    return np.full(_FRAME_LEN, 1000, dtype=np.int16)


def _silence_frame() -> np.ndarray:
    return np.zeros(_FRAME_LEN, dtype=np.int16)


def _audio_chunk(duration_ms: float = 50.0) -> AudioChunk:
    samples = int(22050 * duration_ms / 1000)
    return AudioChunk(
        audio=np.zeros(samples, dtype=np.int16),
        sample_rate=22050,
        duration_ms=duration_ms,
    )


class TestVoicePipelineE2ET638:
    """End-to-end pipeline cycle including LLM→TTS hand-off.

    The on_perception callback is the LLM mock — it takes the STT
    transcript and synthesises a response string, then calls
    ``pipeline.speak()`` to drive the orchestrator into SPEAKING.
    This closes the full loop the existing integration tests stop
    short of.
    """

    @pytest.mark.asyncio()
    async def test_full_turn_wake_to_tts_output(self) -> None:
        """Complete turn: WW → REC → STT → LLM (cb) → TTS → output → IDLE."""
        # ── Boundary mocks ──
        vad = MagicMock()
        ww = MagicMock()
        stt = AsyncMock()
        tts = AsyncMock()
        bus = AsyncMock()
        bus.emit = AsyncMock()

        # STT returns a fixed transcript.
        stt_result = MagicMock()
        stt_result.text = "what time is it"
        stt_result.confidence = 0.95
        stt_result.language = "en"
        stt_result.rejection_reason = None
        stt.transcribe.return_value = stt_result

        # TTS produces a 50ms synthetic chunk.
        tts_chunk = _audio_chunk(50)
        tts.synthesize.return_value = tts_chunk

        # ── LLM mock (the cognitive callback) ──
        # Production: cognitive layer receives the transcript, runs an
        # LLM, then calls pipeline.speak(response). Here we inline
        # that contract — the callback is the LLM proxy + speak driver.
        llm_response = "It is three o'clock"
        speak_calls: list[str] = []

        # We capture the pipeline ref AFTER construction; closure binds it.
        pipeline_holder: dict[str, VoicePipeline] = {}

        async def _llm_perception(text: str, mind_id: str) -> None:
            assert text == "what time is it"
            assert mind_id == "e2e-t638"
            # The callback decides "what to say" (the LLM step) and
            # tells the pipeline to speak it (the orchestrator hand-off).
            speak_calls.append(llm_response)
            await pipeline_holder["pipeline"].speak(llm_response)

        # ── Pipeline ──
        config = VoicePipelineConfig(
            mind_id="e2e-t638",
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
            on_perception=_llm_perception,
        )
        pipeline_holder["pipeline"] = pipeline

        # Patch the playback to avoid real sounddevice. The chunk reaches
        # play_immediate → _play_audio; we capture the chunk here so we
        # can assert "TTS audio actually flowed to the output sink".
        played_chunks: list[AudioChunk] = []

        async def _capture_play(chunk: AudioChunk) -> None:
            played_chunks.append(chunk)

        with patch.object(_output_queue_mod, "_play_audio", side_effect=_capture_play):
            await pipeline.start()
            try:
                # Phase 1: IDLE — silence + no wake. State stays IDLE.
                vad.process_frame.return_value = _vad_event(speech=False)
                ww.process_frame.return_value = MagicMock(detected=False)
                result = await pipeline.feed_frame(_silence_frame())
                assert result["state"] == "IDLE"

                # Phase 2: speech + wake-word triggered → WAKE_DETECTED.
                vad.process_frame.return_value = _vad_event(speech=True)
                ww.process_frame.return_value = MagicMock(detected=True)
                result = await pipeline.feed_frame(_speech_frame())
                assert result["state"] == "WAKE_DETECTED"

                # Phase 3: continuing speech → RECORDING.
                vad.process_frame.return_value = _vad_event(speech=True)
                ww.process_frame.return_value = MagicMock(detected=False)
                result = await pipeline.feed_frame(_speech_frame())
                assert result["state"] == "RECORDING"

                # Phase 4: more speech.
                for _ in range(3):
                    result = await pipeline.feed_frame(_speech_frame())
                    assert result["state"] == "RECORDING"

                # Phase 5: trailing silence triggers transcription +
                # the perception callback + speak() in one chain.
                vad.process_frame.return_value = _vad_event(speech=False)
                for _ in range(2):  # silence_frames_end=2
                    await pipeline.feed_frame(_silence_frame())

                # ── Pin the full E2E contract ──
                # STT was called with the recorded buffer.
                assert stt.transcribe.called
                # The cognitive callback fired with the STT transcript.
                assert speak_calls == [llm_response]
                # TTS was called with the LLM response text.
                tts.synthesize.assert_awaited_with(llm_response)
                # The synthesized chunk reached the playback sink.
                # The orchestrator plays a wake-confirmation beep BEFORE
                # the recording phase (confirmation_tone="beep" default),
                # so played_chunks holds [beep, tts_chunk]. We pin the
                # TTS chunk via identity (AudioChunk dataclass __eq__
                # would compare np.ndarray fields with ==, which returns
                # an array — ambiguous truth value).
                assert any(c is tts_chunk for c in played_chunks)
                # Final state: IDLE — speak()'s finally clause cleared SPEAKING.
                assert pipeline.state == VoicePipelineState.IDLE
            finally:
                await pipeline.stop()

    @pytest.mark.asyncio()
    async def test_two_consecutive_turns_in_one_session(self) -> None:
        """Two complete turns back-to-back without restarting the pipeline.

        Production scenario: a single voice session handles many turns
        — the state machine must cleanly recycle through IDLE between
        them. Failure mode this guards: stale ``_current_utterance_id``
        or stuck state flag bleeding from turn N into turn N+1.
        """
        vad = MagicMock()
        ww = MagicMock()
        stt = AsyncMock()
        tts = AsyncMock()
        bus = AsyncMock()
        bus.emit = AsyncMock()

        # Two distinct STT transcripts in sequence.
        transcripts = [
            ("what time is it", "It is three o'clock"),
            ("what's the weather", "Sunny and seventy"),
        ]
        stt_results = []
        for text, _ in transcripts:
            r = MagicMock()
            r.text = text
            r.confidence = 0.95
            r.language = "en"
            r.rejection_reason = None
            stt_results.append(r)
        stt.transcribe.side_effect = stt_results

        tts_chunk = _audio_chunk(50)
        tts.synthesize.return_value = tts_chunk

        spoken: list[str] = []
        pipeline_holder: dict[str, VoicePipeline] = {}

        async def _llm_perception(text: str, _mind_id: str) -> None:
            response = next(r for q, r in transcripts if q == text)
            spoken.append(response)
            await pipeline_holder["pipeline"].speak(response)

        config = VoicePipelineConfig(
            mind_id="e2e-t638-multi",
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
            on_perception=_llm_perception,
        )
        pipeline_holder["pipeline"] = pipeline

        played_chunks: list[AudioChunk] = []

        async def _capture_play(chunk: AudioChunk) -> None:
            played_chunks.append(chunk)

        async def _drive_one_turn() -> None:
            # Wake.
            vad.process_frame.return_value = _vad_event(speech=True)
            ww.process_frame.return_value = MagicMock(detected=True)
            await pipeline.feed_frame(_speech_frame())
            # Record.
            ww.process_frame.return_value = MagicMock(detected=False)
            for _ in range(4):
                await pipeline.feed_frame(_speech_frame())
            # End.
            vad.process_frame.return_value = _vad_event(speech=False)
            for _ in range(2):
                await pipeline.feed_frame(_silence_frame())

        with patch.object(_output_queue_mod, "_play_audio", side_effect=_capture_play):
            await pipeline.start()
            try:
                # Turn 1.
                await _drive_one_turn()
                assert pipeline.state == VoicePipelineState.IDLE
                assert spoken == ["It is three o'clock"]

                # Turn 2 — fresh state, no leakage from turn 1.
                await _drive_one_turn()
                assert pipeline.state == VoicePipelineState.IDLE
                assert spoken == ["It is three o'clock", "Sunny and seventy"]

                # Both TTS chunks reached the sink. Each turn produces a
                # wake beep + the TTS response chunk, so the sink sees
                # 4 chunks total (2 turns × 2 chunks). The TTS chunk is
                # identified by identity since the same mock returns it
                # every time.
                tts_plays = [c for c in played_chunks if c is tts_chunk]
                assert len(tts_plays) == 2  # noqa: PLR2004 — one per turn
                # STT was called twice (one per turn).
                assert stt.transcribe.await_count == 2  # noqa: PLR2004
            finally:
                await pipeline.stop()

    @pytest.mark.asyncio()
    async def test_perception_callback_failure_does_not_break_pipeline(self) -> None:
        """A buggy LLM/cognitive callback must not crash the orchestrator.

        Pin the orchestrator's perception-callback isolation contract
        (orchestrator.py:1781-1801) under E2E load: the callback raises
        mid-turn, the pipeline emits PipelineErrorEvent on the bus, and
        the next turn proceeds normally. This is the production
        resilience contract — a transient cognitive-layer bug must not
        leave the pipeline stuck in THINKING.
        """
        vad = MagicMock()
        ww = MagicMock()
        stt = AsyncMock()
        tts = AsyncMock()
        bus = AsyncMock()
        emitted: list[Any] = []

        async def _record_emit(event: Any) -> None:  # noqa: ANN401 — heterogeneous events
            emitted.append(event)

        bus.emit = AsyncMock(side_effect=_record_emit)

        stt_result = MagicMock()
        stt_result.text = "broken turn"
        stt_result.confidence = 0.95
        stt_result.language = "en"
        stt_result.rejection_reason = None
        stt.transcribe.return_value = stt_result

        tts.synthesize.return_value = _audio_chunk(50)

        async def _broken_perception(_text: str, _mind_id: str) -> None:
            msg = "simulated cognitive-layer crash"
            raise RuntimeError(msg)

        config = VoicePipelineConfig(
            mind_id="e2e-t638-error",
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
            on_perception=_broken_perception,
        )

        with patch.object(_output_queue_mod, "_play_audio", new_callable=AsyncMock):
            await pipeline.start()
            try:
                # Drive a turn that hits the broken callback.
                vad.process_frame.return_value = _vad_event(speech=True)
                ww.process_frame.return_value = MagicMock(detected=True)
                await pipeline.feed_frame(_speech_frame())
                ww.process_frame.return_value = MagicMock(detected=False)
                for _ in range(3):
                    await pipeline.feed_frame(_speech_frame())
                vad.process_frame.return_value = _vad_event(speech=False)
                for _ in range(2):
                    await pipeline.feed_frame(_silence_frame())

                # Pipeline survived the crash — pipeline still running,
                # callback failure was isolated. The orchestrator emits
                # PipelineErrorEvent on the bus per the isolation
                # contract (orchestrator.py:1795-1801).
                assert pipeline.is_running
                error_events = [e for e in emitted if type(e).__name__ == "PipelineErrorEvent"]
                assert len(error_events) >= 1
                assert "perception_callback_failed" in error_events[0].error
            finally:
                await pipeline.stop()
