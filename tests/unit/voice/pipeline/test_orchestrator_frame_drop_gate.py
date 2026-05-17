"""Tests for the Mission C3 §T2.5 frame-drop emission gate.

Mission anchor: ``docs-internal/missions/MISSION-c3-failover-ladder-iteration-2026-05-16.md``
§T2.5.

Pin the orchestrator-side gate in ``_check_frame_drop_signals``:

* When ``pipeline._failover_ladder_in_progress=True``, per-frame
  ``voice.frame.drop_detected`` events MUST NOT fire; the gap+ts
  tuple MUST be appended to ``pipeline._frame_loss_during_ladder``.
* When the flag is False (or absent — default-False sentinel per
  anti-pattern #35), the legacy per-frame emit fires unchanged.
* On ladder complete, the failover helper drains the accumulator
  via ``_emit_frame_loss_window_summary`` (validated separately in
  ``test_runtime_failover.py::TestRuntimeFailoverFrameLossWindow``);
  this file verifies only the orchestrator-side write path.

Tests exercise the surface at the call-site of
``_check_frame_drop_signals`` by injecting a minimal pipeline-like
object with the relevant attrs + invoking the helper directly.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from sovyx.voice.pipeline import _orchestrator as orch_mod


def _make_pipeline_stub(
    *,
    ladder_in_progress: bool = False,
    accumulator: list | None = None,
):
    """Build a stub pipeline object that mimics the orchestrator's
    private attrs the gate reads/writes. SimpleNamespace gives stable
    setattr semantics + clean test introspection.
    """
    return SimpleNamespace(
        _failover_ladder_in_progress=ladder_in_progress,
        _frame_loss_during_ladder=accumulator if accumulator is not None else [],
        _last_frame_monotonic=1000.0,
        _recent_frame_intervals=[],
        _expected_frame_interval_s=0.032,
        _last_drift_warning_monotonic=None,
        _state=SimpleNamespace(name="LISTENING"),
        _config=SimpleNamespace(mind_id="jonny"),
    )


class TestFrameDropGateOff:
    """``ladder_in_progress=False`` — per-frame emit fires legacy event."""

    def test_emit_fires_when_ladder_inactive(self) -> None:
        stub = _make_pipeline_stub(ladder_in_progress=False)
        # Simulate a frame that exceeds the absolute budget (>64ms).
        now = stub._last_frame_monotonic + 0.150  # 150ms gap

        with patch.object(orch_mod, "logger") as mock_logger:
            orch_mod.VoicePipeline._check_frame_drop_signals(stub, now)

        # Legacy emit fired.
        warning_calls = [
            c
            for c in mock_logger.warning.call_args_list
            if c.args and c.args[0] == "voice.frame.drop_detected"
        ]
        assert len(warning_calls) == 1
        # Accumulator MUST NOT have grown.
        assert stub._frame_loss_during_ladder == []


class TestFrameDropGateOn:
    """``ladder_in_progress=True`` — per-frame emit SUPPRESSED, accumulator grows."""

    def test_emit_suppressed_when_ladder_active(self) -> None:
        stub = _make_pipeline_stub(ladder_in_progress=True)
        now = stub._last_frame_monotonic + 0.150  # 150ms gap

        with patch.object(orch_mod, "logger") as mock_logger:
            orch_mod.VoicePipeline._check_frame_drop_signals(stub, now)

        # Legacy emit MUST NOT have fired.
        drop_calls = [
            c
            for c in mock_logger.warning.call_args_list
            if c.args and c.args[0] == "voice.frame.drop_detected"
        ]
        assert drop_calls == []
        # Accumulator grew by exactly 1 (gap, monotonic_ts) tuple.
        assert len(stub._frame_loss_during_ladder) == 1
        gap_s, ts = stub._frame_loss_during_ladder[0]
        assert gap_s > 0.064  # exceeded budget
        assert ts == now

    def test_multiple_drops_accumulate_without_emit(self) -> None:
        stub = _make_pipeline_stub(ladder_in_progress=True)
        gaps = [0.100, 0.200, 0.150]

        with patch.object(orch_mod, "logger") as mock_logger:
            for gap in gaps:
                now = stub._last_frame_monotonic + gap
                orch_mod.VoicePipeline._check_frame_drop_signals(stub, now)
                stub._last_frame_monotonic = now

        # Zero legacy emits.
        drop_calls = [
            c
            for c in mock_logger.warning.call_args_list
            if c.args and c.args[0] == "voice.frame.drop_detected"
        ]
        assert drop_calls == []
        # All 3 drops accumulated.
        assert len(stub._frame_loss_during_ladder) == 3


class TestFrameDropGateMissingFlag:
    """Pipelines missing ``_failover_ladder_in_progress`` MUST fall
    through to legacy emit (anti-pattern #35 sentinel — absent attr
    is False).
    """

    def test_missing_flag_falls_through_to_legacy_emit(self) -> None:
        stub = SimpleNamespace(
            _last_frame_monotonic=1000.0,
            _recent_frame_intervals=[],
            _expected_frame_interval_s=0.032,
            _last_drift_warning_monotonic=None,
            _state=SimpleNamespace(name="LISTENING"),
            _config=SimpleNamespace(mind_id="jonny"),
        )
        # Deliberately no _failover_ladder_in_progress, no _frame_loss_during_ladder.
        now = stub._last_frame_monotonic + 0.150

        with patch.object(orch_mod, "logger") as mock_logger:
            orch_mod.VoicePipeline._check_frame_drop_signals(stub, now)

        # Legacy emit fired (anti-pattern #35 sentinel: getattr(..., False)).
        drop_calls = [
            c
            for c in mock_logger.warning.call_args_list
            if c.args and c.args[0] == "voice.frame.drop_detected"
        ]
        assert len(drop_calls) == 1


class TestFrameDropBelowBudget:
    """Frames below the absolute budget MUST NOT trigger the gate at all."""

    def test_subbudget_frame_neither_emits_nor_accumulates(self) -> None:
        stub = _make_pipeline_stub(ladder_in_progress=True)
        # 30ms gap — below the 64ms absolute budget.
        now = stub._last_frame_monotonic + 0.030

        with patch.object(orch_mod, "logger") as mock_logger:
            orch_mod.VoicePipeline._check_frame_drop_signals(stub, now)

        # No emit.
        drop_calls = [
            c
            for c in mock_logger.warning.call_args_list
            if c.args and c.args[0] == "voice.frame.drop_detected"
        ]
        assert drop_calls == []
        # No accumulation.
        assert stub._frame_loss_during_ladder == []
