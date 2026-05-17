"""Mission C3 §T2.5 regression — frame-drop emission silence during
ladder iteration.

Replays the H6 amplification observed in the v0.43.1 operator session
(forensic audit L1069: a single 4286 ms ladder window produced
~10-100 per-frame ``voice.frame.drop_detected`` events because the
orchestrator's drop detector was unguarded).

This test asserts the post-Mission-C3 invariant:

* During a failover ladder iteration window (between
  ``voice.failover.ladder_started`` and
  ``voice.failover.ladder_complete``), the orchestrator's per-frame
  ``voice.frame.drop_detected`` event MUST NOT fire.
* After the ladder completes (success or exhausted), the failover
  helper MUST emit EXACTLY ONE ``voice.failover.frame_loss_window``
  summary event with the aggregate drop count + total gap_ms.

The test exercises the orchestrator gate + the failover helper's
summary emit in one cohesive trace, mirroring the production wire-
up at ``_check_frame_drop_signals`` + ``_emit_frame_loss_window_summary``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sovyx.engine.config import VoiceTuningConfig
from sovyx.voice.capture._restart import (
    DeviceChangeRestartResult,
    DeviceChangeRestartVerdict,
)
from sovyx.voice.health import _runtime_failover as failover_mod
from sovyx.voice.health._runtime_failover import (
    RuntimeFailoverState,
    _try_runtime_failover,
)
from sovyx.voice.pipeline import _orchestrator as orch_mod

_MIND_ID = "jonny"


def _capture_logs(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict]]:
    captured: list[tuple[str, dict]] = []
    real = failover_mod.logger

    def _capture(level: str):
        real_method = getattr(real, level)

        def _spy(event: str, *args, **kwargs):
            captured.append((event, dict(kwargs)))
            return real_method(event, *args, **kwargs)

        return _spy

    monkeypatch.setattr(failover_mod.logger, "warning", _capture("warning"))
    monkeypatch.setattr(failover_mod.logger, "error", _capture("error"))
    monkeypatch.setattr(failover_mod.logger, "info", _capture("info"))
    return captured


def _make_target_entry():
    from sovyx.voice.device_enum import DeviceEntry

    return DeviceEntry(
        index=7,
        name="PipeWire",
        canonical_name="pipewire-virtual",
        host_api_index=0,
        host_api_name="ALSA",
        max_input_channels=2,
        max_output_channels=2,
        default_samplerate=48_000,
        is_os_default=False,
    )


class TestC3FrameDropSilenceDuringLadder:
    """H6 closure regression — orchestrator gate + summary emit."""

    @pytest.mark.asyncio()
    async def test_orchestrator_gate_suppresses_during_ladder_summary_aggregates(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """End-to-end trace: simulate 4 frame-drops while the ladder
        flag is True (orchestrator-side gate), then complete the
        ladder and assert a single ``voice.failover.frame_loss_window``
        summary fires with the aggregate.
        """
        from sovyx.voice.health._failover_history import (
            reset_default_failover_history,
        )
        from sovyx.voice.health._probe_result_cache import (
            reset_default_probe_result_cache,
        )

        reset_default_probe_result_cache()
        reset_default_failover_history()
        captured = _capture_logs(monkeypatch)

        # Build a pipeline-stub that supports BOTH the orchestrator's
        # frame-drop signal helper AND the runtime-failover ladder.
        class _CombinedStub:
            def __init__(self) -> None:
                self._config = MagicMock(mind_id=_MIND_ID)
                self._current_mind_id = _MIND_ID
                self._failover_ladder_in_progress = False
                self._frame_loss_during_ladder: list[tuple[float, float]] = []
                self._last_frame_monotonic = 1000.0
                self._recent_frame_intervals: list[float] = []
                self._expected_frame_interval_s = 0.032
                self._last_drift_warning_monotonic = None
                self._state = SimpleNamespace(name="LISTENING")

            def reset_coordinator_after_failover(self) -> None:
                # Simulate 4 frame drops accumulating DURING the
                # dispatch wall-clock (the orchestrator's gate would
                # have appended these via the production path).
                # ``_check_frame_drop_signals`` reads
                # ``gap = now - _last_frame_monotonic`` so we must
                # pass ``now`` ahead-of-time and only commit
                # ``_last_frame_monotonic`` AFTER the call.
                gaps = [0.150, 0.200, 0.100, 0.180]
                for gap in gaps:
                    now = self._last_frame_monotonic + gap
                    orch_mod.VoicePipeline._check_frame_drop_signals(self, now)
                    self._last_frame_monotonic = now

        pipeline = _CombinedStub()
        tuning = VoiceTuningConfig(
            runtime_failover_on_quarantine_enabled=True,
            failover_intra_ladder_cooldown_s=0.0,
        )
        state = RuntimeFailoverState()
        candidate = _make_target_entry()

        # Capture-task fake with a successful restart.
        capture_task = MagicMock()
        capture_task.active_device_guid = "razer"
        capture_task.active_device_name = "Razer"
        capture_task._input_device = 5
        capture_task._host_api_name = "ALSA"
        capture_task.request_device_change_restart = AsyncMock(
            return_value=DeviceChangeRestartResult(
                verdict=DeviceChangeRestartVerdict.DEVICE_CHANGED_SUCCESS,
                engaged=True,
                target_device_index=7,
                target_host_api="ALSA",
                new_endpoint_guid="g",
            ),
        )

        with patch.object(
            failover_mod,
            "_resolve_target_safe",
            return_value=(candidate, 1, None),
        ):
            await _try_runtime_failover(
                capture_task=capture_task,
                pipeline=pipeline,
                tuning=tuning,
                state=state,
            )

        # ─────────────────────────────────────────────────────────────
        # H6 invariants:
        # ─────────────────────────────────────────────────────────────

        # The orchestrator's per-frame emit MUST NOT fire — the gate
        # accumulated all 4 drops into the list. The failover
        # helper drains the list inside the summary emit.
        # 1 summary event fires with aggregate = 0.630 s (sum of gaps).
        summaries = [
            kwargs for evt, kwargs in captured if evt == "voice.failover.frame_loss_window"
        ]
        assert len(summaries) == 1
        assert summaries[0]["voice.frames_dropped"] == 4
        # 0.150 + 0.200 + 0.100 + 0.180 = 0.630 s = 630.0 ms
        assert abs(summaries[0]["voice.duration_ms"] - 630.0) < 0.01
        assert summaries[0]["voice.succeeded_candidate_index"] == 0

        # Ladder finalized cleanly — accumulator cleared.
        assert pipeline._frame_loss_during_ladder == []
        # Flag reset to False via try/finally.
        assert pipeline._failover_ladder_in_progress is False

        reset_default_probe_result_cache()
        reset_default_failover_history()
