"""Tests for the frame-typed pipeline observability layer (Step 11).

Pin the 9 frame types' construction + immutability + JSON serialization
contracts. The frames are observability-only (Mission §1.1 Hybrid C);
nothing in production depends on them yet. Step 12 wires them into
PipelineStateMachine.

Reference: MISSION-voice-100pct-autonomous-2026-04-25.md step 11.
"""

from __future__ import annotations

import time

import pytest

from sovyx.voice.pipeline._frame_types import (
    BargeInInterruptionFrame,
    CaptureRestartFrame,
    CaptureRestartReason,
    EndFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    OutputAudioRawFrame,
    PipelineFrame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
    _frame_to_dict,
)


class TestPipelineFrameBase:
    def test_construction_carries_required_fields(self) -> None:
        frame = PipelineFrame(
            frame_type="generic",
            timestamp_monotonic=time.monotonic(),
            utterance_id="uuid-1",
        )
        assert frame.frame_type == "generic"
        assert frame.utterance_id == "uuid-1"

    def test_frozen_dataclass_rejects_mutation(self) -> None:
        frame = PipelineFrame(
            frame_type="generic",
            timestamp_monotonic=1.0,
        )
        with pytest.raises((AttributeError, TypeError, Exception)):
            frame.frame_type = "mutated"  # type: ignore[misc]


class TestSubclassConstruction:
    def test_user_started_speaking_with_source(self) -> None:
        frame = UserStartedSpeakingFrame(
            frame_type="UserStartedSpeaking",
            timestamp_monotonic=time.monotonic(),
            utterance_id="uuid-1",
            source="wake_word",
        )
        assert frame.source == "wake_word"

    def test_user_stopped_speaking_carries_silero_snapshot(self) -> None:
        frame = UserStoppedSpeakingFrame(
            frame_type="UserStoppedSpeaking",
            timestamp_monotonic=time.monotonic(),
            silero_prob_snapshot=0.42,
        )
        assert frame.silero_prob_snapshot == 0.42

    def test_transcription_frame_carries_text(self) -> None:
        frame = TranscriptionFrame(
            frame_type="Transcription",
            timestamp_monotonic=time.monotonic(),
            text="hello world",
            confidence=0.95,
            language="en",
        )
        assert frame.text == "hello world"
        assert frame.confidence == 0.95
        assert frame.language == "en"

    def test_llm_response_start_carries_model_and_request(self) -> None:
        frame = LLMFullResponseStartFrame(
            frame_type="LLMFullResponseStart",
            timestamp_monotonic=time.monotonic(),
            model="claude-opus-4-7",
            request_id="req-abc",
        )
        assert frame.model == "claude-opus-4-7"
        assert frame.request_id == "req-abc"

    def test_llm_response_end_carries_lengths(self) -> None:
        frame = LLMFullResponseEndFrame(
            frame_type="LLMFullResponseEnd",
            timestamp_monotonic=time.monotonic(),
            output_chars=120,
            elapsed_ms=850,
        )
        assert frame.output_chars == 120
        assert frame.elapsed_ms == 850

    def test_output_audio_raw_frame_carries_chunk_metadata(self) -> None:
        frame = OutputAudioRawFrame(
            frame_type="OutputAudioRaw",
            timestamp_monotonic=time.monotonic(),
            chunk_index=3,
            pcm_bytes=8192,
            sample_rate=24000,
            synthesis_health="ok",
        )
        assert frame.chunk_index == 3
        assert frame.pcm_bytes == 8192

    def test_barge_in_interruption_carries_step_results(self) -> None:
        frame = BargeInInterruptionFrame(
            frame_type="BargeInInterruption",
            timestamp_monotonic=time.monotonic(),
            utterance_id="uuid-1",
            reason="barge_in",
            step_results={
                "output_flush": "ok",
                "tts_tasks_cancel": "ok",
                "llm_cancel": "ok",
                "filler_and_gate": "ok",
                "text_buffer_cleanup": "ok",
            },
        )
        assert frame.reason == "barge_in"
        assert frame.step_results["output_flush"] == "ok"
        assert len(frame.step_results) == 5

    def test_end_frame_carries_terminal_reason(self) -> None:
        frame = EndFrame(
            frame_type="End",
            timestamp_monotonic=time.monotonic(),
            reason="tts_finished",
        )
        assert frame.reason == "tts_finished"


class TestFrameToDict:
    def test_user_started_speaking_serialises_source(self) -> None:
        frame = UserStartedSpeakingFrame(
            frame_type="UserStartedSpeaking",
            timestamp_monotonic=42.0,
            utterance_id="uuid-1",
            source="wake_word",
        )
        d = _frame_to_dict(frame)
        assert d["frame_type"] == "UserStartedSpeaking"
        assert d["utterance_id"] == "uuid-1"
        assert d["source"] == "wake_word"
        assert d["timestamp_monotonic"] == 42.0

    def test_barge_in_serialises_step_results_dict(self) -> None:
        frame = BargeInInterruptionFrame(
            frame_type="BargeInInterruption",
            timestamp_monotonic=42.0,
            reason="barge_in",
            step_results={"output_flush": "ok"},
        )
        d = _frame_to_dict(frame)
        assert d["step_results"] == {"output_flush": "ok"}
        assert d["reason"] == "barge_in"

    def test_dict_has_no_unexpected_keys(self) -> None:
        """The serialised form must not leak private fields."""
        frame = TranscriptionFrame(
            frame_type="Transcription",
            timestamp_monotonic=42.0,
            text="hi",
            confidence=0.9,
            language="en",
        )
        d = _frame_to_dict(frame)
        expected_keys = {
            "frame_type",
            "timestamp_monotonic",
            "utterance_id",
            "text",
            "confidence",
            "language",
        }
        assert set(d.keys()) == expected_keys


class TestCaptureRestartReason:
    """Voice Windows Paranoid Mission §C — restart-reason discriminator."""

    def test_str_enum_value_equality(self) -> None:
        """StrEnum values compare equal to their underlying string —
        anti-pattern #9 immunity guarantee."""
        assert CaptureRestartReason.DEVICE_CHANGED == "device_changed"
        assert CaptureRestartReason.APO_DEGRADED == "apo_degraded"
        assert CaptureRestartReason.OVERFLOW == "overflow"
        assert CaptureRestartReason.MANUAL == "manual"

    def test_all_four_variants_present(self) -> None:
        """Pin the variant set so a future addition / rename is loud."""
        assert {r.value for r in CaptureRestartReason} == {
            "device_changed",
            "apo_degraded",
            "overflow",
            "manual",
        }


class TestCaptureRestartFrame:
    """Voice Windows Paranoid Mission §C — capture-restart frame shape."""

    def test_default_construction_zero_field_values(self) -> None:
        """Every payload field has a zero-value default — base-class
        compatibility for the PipelineFrame inheritance contract."""
        frame = CaptureRestartFrame(
            frame_type="CaptureRestart",
            timestamp_monotonic=42.0,
        )
        assert frame.restart_reason == ""
        assert frame.old_host_api == ""
        assert frame.new_host_api == ""
        assert frame.old_device_id == ""
        assert frame.new_device_id == ""
        assert frame.old_signal_processing_mode == ""
        assert frame.new_signal_processing_mode == ""
        assert frame.recovery_latency_ms == 0
        assert frame.bypass_tier == 0

    def test_device_changed_population(self) -> None:
        """IMMNotificationClient-driven substrate change — the
        recovery_latency_ms field populates and bypass_tier stays 0."""
        frame = CaptureRestartFrame(
            frame_type="CaptureRestart",
            timestamp_monotonic=100.0,
            utterance_id="utt-1",
            restart_reason=CaptureRestartReason.DEVICE_CHANGED.value,
            old_host_api="Windows WASAPI",
            new_host_api="Windows WASAPI",
            old_device_id="{old-guid}",
            new_device_id="{new-guid}",
            old_signal_processing_mode="Default",
            new_signal_processing_mode="Default",
            recovery_latency_ms=312,
            bypass_tier=0,
        )
        assert frame.restart_reason == "device_changed"
        assert frame.old_device_id == "{old-guid}"
        assert frame.new_device_id == "{new-guid}"
        assert frame.recovery_latency_ms == 312
        assert frame.bypass_tier == 0

    def test_apo_degraded_population_with_bypass_tier(self) -> None:
        """Coordinator-driven bypass — bypass_tier carries 1/2/3 to
        let dashboards split bypass success rate per tier."""
        frame = CaptureRestartFrame(
            frame_type="CaptureRestart",
            timestamp_monotonic=200.0,
            restart_reason=CaptureRestartReason.APO_DEGRADED.value,
            old_host_api="MME",
            new_host_api="Windows WASAPI",
            old_device_id="{guid}",
            new_device_id="{guid}",
            old_signal_processing_mode="Default",
            new_signal_processing_mode="RAW",
            bypass_tier=2,
        )
        assert frame.restart_reason == "apo_degraded"
        assert frame.bypass_tier == 2
        assert frame.old_host_api == "MME"
        assert frame.new_host_api == "Windows WASAPI"
        assert frame.old_signal_processing_mode == "Default"
        assert frame.new_signal_processing_mode == "RAW"

    def test_frozen_rejects_mutation(self) -> None:
        """frozen=True / slots=True — observers can share the frame
        across the state-machine lock + dashboard read path."""
        frame = CaptureRestartFrame(
            frame_type="CaptureRestart",
            timestamp_monotonic=1.0,
            restart_reason="manual",
        )
        with pytest.raises((AttributeError, TypeError, Exception)):
            frame.restart_reason = "device_changed"  # type: ignore[misc]

    def test_serialisation_round_trip(self) -> None:
        """``_frame_to_dict`` must surface every payload field — it's
        the wire contract for ``GET /api/voice/restart-history``."""
        frame = CaptureRestartFrame(
            frame_type="CaptureRestart",
            timestamp_monotonic=42.0,
            utterance_id="utt-2",
            restart_reason=CaptureRestartReason.OVERFLOW.value,
            old_host_api="Windows WASAPI",
            new_host_api="Windows WASAPI",
            old_device_id="{guid}",
            new_device_id="{guid}",
            old_signal_processing_mode="Default",
            new_signal_processing_mode="Default",
            recovery_latency_ms=120,
            bypass_tier=0,
        )
        d = _frame_to_dict(frame)
        assert d["frame_type"] == "CaptureRestart"
        assert d["restart_reason"] == "overflow"
        assert d["recovery_latency_ms"] == 120
        assert d["bypass_tier"] == 0
        # No leaked private fields:
        expected = {
            "frame_type",
            "timestamp_monotonic",
            "utterance_id",
            "restart_reason",
            "old_host_api",
            "new_host_api",
            "old_device_id",
            "new_device_id",
            "old_signal_processing_mode",
            "new_signal_processing_mode",
            "recovery_latency_ms",
            "bypass_tier",
        }
        assert set(d.keys()) == expected

    def test_inherits_pipeline_frame_base(self) -> None:
        """Subclass must derive from PipelineFrame so the ring-buffer
        consumer (PipelineStateMachine.record_frame) accepts it."""
        frame = CaptureRestartFrame(
            frame_type="CaptureRestart",
            timestamp_monotonic=1.0,
        )
        assert isinstance(frame, PipelineFrame)
