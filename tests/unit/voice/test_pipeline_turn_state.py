"""Tests for the SPEAKING-turn ownership + dwell-watchdog surgery.

Covers the conversation-breaking defect class found in the 2026-07-01
audio-engine audit — the live streaming turn was never exercised
end-to-end before (voice never ran live; unit tests mocked the seams),
so these regressions guard the repaired semantics:

* ``_speech_session_active`` — ``_handle_speaking`` must not misread
  "not yet playing" as "finished playing" while a TTS-out surface owns
  the turn (pre-fix: SPEAKING↔IDLE flapping on every frame of the
  LLM-generation window; duplicate TTSStarted/Completed; self-feedback
  duck released mid-turn; self-echo re-recording with wake disabled).
* Background drainer — ``stream_text`` starts playback on the first
  synthesized segment instead of parking all audio until
  ``flush_stream`` (pre-fix: zero incremental audio; the advertised
  ~300 ms streaming latency did not exist).
* ``flush_stream(discard_buffer=True)`` — the barge-in cancellation
  path drops the interrupted response's tail instead of synthesizing
  it (pre-fix: a fragment of the cancelled utterance played after the
  user barged in).
* RECORDING clobber guards — a late ``flush_stream``/``speak`` finally
  must not overwrite the barge-in's RECORDING handoff with IDLE.
* Dwell watchdog — the previously-unwired ``PipelineStateMachine``
  dwell machinery now force-recovers zombie transient states
  (THINKING latch, RECORDING stall) from the heartbeat loop.
* Per-segment guard hook — streamed segments run through the
  registered output/PII guard BEFORE synthesis; a guard failure drops
  the segment (fail-closed).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from sovyx.voice.pipeline import TTSCompletedEvent, VoicePipelineState
from sovyx.voice.pipeline import _heartbeat_mixin as _heartbeat_mod
from sovyx.voice.pipeline import _output_queue as _output_queue_mod
from sovyx.voice.pipeline._state_machine import is_transition_allowed
from tests.unit.voice.test_pipeline import (
    _frame,
    _make_pipeline,
    _vad_event,
)


def _tts_completed_events(bus: AsyncMock) -> list[TTSCompletedEvent]:
    """Extract every TTSCompletedEvent emitted on the mock bus."""
    return [
        call.args[0]
        for call in bus.emit.await_args_list
        if isinstance(call.args[0], TTSCompletedEvent)
    ]


class TestSpeakingSessionOwnership:
    """_handle_speaking defers to the TTS-out surfaces while a session is open."""

    async def test_speaking_holds_while_session_active_and_not_playing(self) -> None:
        """The LLM-generation window (enqueued-but-not-yet-playing) must
        NOT be misread as playback-finished."""
        pipeline, refs = _make_pipeline()
        pipeline._state = VoicePipelineState.SPEAKING
        pipeline._speech_session_active = True

        result = await pipeline._handle_speaking(_frame(), _vad_event(False))

        assert result["state"] == "SPEAKING"
        assert pipeline._state is VoicePipelineState.SPEAKING
        assert _tts_completed_events(refs["bus"]) == []

    async def test_speaking_falls_back_to_idle_when_session_closed(self) -> None:
        """With no open session and no playback the fallback still recovers."""
        pipeline, refs = _make_pipeline()
        pipeline._state = VoicePipelineState.SPEAKING
        pipeline._speech_session_active = False

        result = await pipeline._handle_speaking(_frame(), _vad_event(False))

        assert result["event"] == "tts_completed"
        assert pipeline._state is VoicePipelineState.IDLE
        assert len(_tts_completed_events(refs["bus"])) == 1

    async def test_stream_text_opens_session_and_flush_closes_it(self) -> None:
        pipeline, _refs = _make_pipeline()
        with patch.object(_output_queue_mod, "_play_audio", AsyncMock()):
            await pipeline.stream_text("Hello there. ")
            assert pipeline._speech_session_active is True
            await pipeline.flush_stream()
        assert pipeline._speech_session_active is False
        assert pipeline._state is VoicePipelineState.IDLE


class TestStreamingBackgroundDrainer:
    """stream_text plays audio incrementally instead of parking it."""

    async def test_first_segment_plays_before_flush(self) -> None:
        pipeline, _refs = _make_pipeline()
        play_mock = AsyncMock()
        with patch.object(_output_queue_mod, "_play_audio", play_mock):
            await pipeline.stream_text("First sentence is done. still generating more here")
            drainer = pipeline._stream_drain_task
            assert drainer is not None
            await drainer
            # The complete segment played WITHOUT any flush_stream call.
            assert play_mock.await_count >= 1
            await pipeline.flush_stream()

    async def test_flush_awaits_drainer_single_flight(self) -> None:
        """flush_stream must hand off from the background drainer — after
        flush the drainer slot is cleared and all audio has played."""
        pipeline, _refs = _make_pipeline()
        with patch.object(_output_queue_mod, "_play_audio", AsyncMock()):
            await pipeline.stream_text(
                "Sentence number one done. Sentence number two done. still generating more here"
            )
            await pipeline.flush_stream()
        assert pipeline._stream_drain_task is None
        assert pipeline._output._queue.empty()


class TestFlushDiscardAndClobberGuards:
    """Barge-in tail discard + RECORDING ownership protection."""

    async def test_flush_discard_buffer_drops_tail(self) -> None:
        pipeline, refs = _make_pipeline()
        pipeline._text_buffer = "tail of the cancelled response"

        await pipeline.flush_stream(discard_buffer=True)

        refs["tts"].synthesize.assert_not_awaited()
        assert pipeline._text_buffer == ""
        assert pipeline._state is VoicePipelineState.IDLE

    async def test_flush_does_not_clobber_recording(self) -> None:
        """A late flush after the barge-in handoff must leave RECORDING
        alone — pre-fix it overwrote the state with IDLE and the
        barged-in utterance was silently dropped."""
        pipeline, refs = _make_pipeline()
        pipeline._state = VoicePipelineState.RECORDING
        pipeline._speech_session_active = True

        await pipeline.flush_stream(discard_buffer=True)

        assert pipeline._state is VoicePipelineState.RECORDING
        assert pipeline._speech_session_active is False
        assert _tts_completed_events(refs["bus"]) == []

    async def test_speak_finally_does_not_clobber_recording(self) -> None:
        pipeline, refs = _make_pipeline()

        async def _synth_then_barge(_text: str) -> object:
            # Simulate a barge-in handing the turn to RECORDING while
            # the batch synthesis is still in flight.
            pipeline._state_value = VoicePipelineState.RECORDING
            raise RuntimeError("synth interrupted")

        refs["tts"].synthesize = AsyncMock(side_effect=_synth_then_barge)

        await pipeline.speak("hello")

        assert pipeline._state is VoicePipelineState.RECORDING


class TestDwellWatchdog:
    """Heartbeat-driven recovery of zombie transient states."""

    async def test_thinking_dwell_recovers_to_idle(self) -> None:
        pipeline, refs = _make_pipeline()
        pipeline._state = VoicePipelineState.THINKING
        with patch.object(_heartbeat_mod, "_DWELL_WATCHDOG_S", 0.001):
            await asyncio.sleep(0.06)
            await pipeline._check_dwell_watchdog()

        assert pipeline._state is VoicePipelineState.IDLE
        error_events = [
            call.args[0]
            for call in refs["bus"].emit.await_args_list
            if type(call.args[0]).__name__ == "PipelineErrorEvent"
        ]
        assert len(error_events) == 1
        assert "dwell_watchdog_fired" in error_events[0].error
        assert "THINKING" in error_events[0].error

    async def test_recording_stall_recovers_to_idle(self) -> None:
        pipeline, _refs = _make_pipeline()
        pipeline._state = VoicePipelineState.RECORDING
        with patch.object(_heartbeat_mod, "_DWELL_WATCHDOG_S", 0.001):
            await asyncio.sleep(0.06)
            await pipeline._check_dwell_watchdog()
        assert pipeline._state is VoicePipelineState.IDLE

    async def test_speaking_is_exempt(self) -> None:
        """Long TTS playback is legitimate — SPEAKING never dwell-fires."""
        pipeline, _refs = _make_pipeline()
        pipeline._state = VoicePipelineState.SPEAKING
        with patch.object(_heartbeat_mod, "_DWELL_WATCHDOG_S", 0.001):
            await asyncio.sleep(0.06)
            await pipeline._check_dwell_watchdog()
        assert pipeline._state is VoicePipelineState.SPEAKING

    async def test_below_threshold_is_noop(self) -> None:
        pipeline, _refs = _make_pipeline()
        pipeline._state = VoicePipelineState.THINKING
        with patch.object(_heartbeat_mod, "_DWELL_WATCHDOG_S", 3600.0):
            await pipeline._check_dwell_watchdog()
        assert pipeline._state is VoicePipelineState.THINKING

    async def test_zero_threshold_disables(self) -> None:
        pipeline, _refs = _make_pipeline()
        pipeline._state = VoicePipelineState.THINKING
        with patch.object(_heartbeat_mod, "_DWELL_WATCHDOG_S", 0.0):
            await asyncio.sleep(0.06)
            await pipeline._check_dwell_watchdog()
        assert pipeline._state is VoicePipelineState.THINKING


class TestStreamSegmentGuard:
    """Per-segment output/PII guard applied before synthesis (P0 safety)."""

    async def test_guard_filters_segment_before_synthesis(self) -> None:
        pipeline, refs = _make_pipeline()
        pipeline.set_stream_segment_guard(lambda text: text.replace("secret", "[X]"))
        with patch.object(_output_queue_mod, "_play_audio", AsyncMock()):
            await pipeline.stream_text("my secret plan is here. still generating more here")
            await pipeline.flush_stream()

        synthesized = [call.args[0] for call in refs["tts"].synthesize.await_args_list]
        assert any("[X]" in text for text in synthesized)
        assert all("secret" not in text for text in synthesized)

    async def test_guard_exception_drops_segment_fail_closed(self) -> None:
        pipeline, refs = _make_pipeline()

        def _broken_guard(_text: str) -> str:
            raise ValueError("guard bug")

        pipeline.set_stream_segment_guard(_broken_guard)
        with patch.object(_output_queue_mod, "_play_audio", AsyncMock()):
            await pipeline.stream_text("This sentence is dropped. still generating more here")
            await pipeline.flush_stream()

        refs["tts"].synthesize.assert_not_awaited()

    async def test_no_guard_passes_through(self) -> None:
        pipeline, refs = _make_pipeline()
        with patch.object(_output_queue_mod, "_play_audio", AsyncMock()):
            await pipeline.stream_text("Plain sentence goes here. still generating more here")
            await pipeline.flush_stream()
        synthesized = [call.args[0] for call in refs["tts"].synthesize.await_args_list]
        assert synthesized[0] == "Plain sentence goes here."


class TestCanonicalTableProactiveSpeech:
    """IDLE → SPEAKING is a real edge (proactive speak/stream from idle)."""

    def test_idle_to_speaking_allowed(self) -> None:
        assert is_transition_allowed(
            VoicePipelineState.IDLE,
            VoicePipelineState.SPEAKING,
        )
