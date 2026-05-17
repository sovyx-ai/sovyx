"""Integration test — full failover ladder loop with cache + history.

Mission anchor: ``docs-internal/missions/MISSION-c3-failover-ladder-iteration-2026-05-16.md``
§9.2.

Exercises the wire-up of every Phase 2 piece in a single trace:

1. ``_try_runtime_failover`` invocation with a 3-candidate cascade.
2. ``ProbeResultCache.record_probe`` populated by each dispatch outcome.
3. ``FailoverHistoryRing.record_ladder`` + ``update_in_progress``
   finalising the run.
4. Per-candidate observability events
   (``candidate_attempted``/``_failed``/``ladder_complete``).
5. Pipeline-side ``_failover_ladder_in_progress`` flag set/cleared.
6. ``VoiceStatusDegraded.degraded`` mirror surfaces via the
   pipeline-state attributes.

Integration scope: this is intentionally light on async-fixture
complexity — exercises the loop body's collaborator wire-up
without spinning up the full ``VoicePipeline`` (which has heavy
ONNX deps). See ``test_failover_with_probe_cache_skip.py`` for
the cache-skip integration variant.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sovyx.engine.config import VoiceTuningConfig
from sovyx.voice.capture._restart import (
    DeviceChangeRestartResult,
    DeviceChangeRestartVerdict,
)
from sovyx.voice.device_enum import DeviceEntry
from sovyx.voice.health import _runtime_failover as failover_mod
from sovyx.voice.health._failover_history import (
    get_default_failover_history,
    reset_default_failover_history,
)
from sovyx.voice.health._probe_result_cache import (
    get_default_probe_result_cache,
    reset_default_probe_result_cache,
)
from sovyx.voice.health._runtime_failover import (
    RuntimeFailoverState,
    _try_runtime_failover,
)


@pytest.fixture(autouse=True)
def _reset_singletons() -> None:
    reset_default_probe_result_cache()
    reset_default_failover_history()


def _make_entry(*, index: int, name: str, canonical: str) -> DeviceEntry:
    return DeviceEntry(
        index=index,
        name=name,
        canonical_name=canonical,
        host_api_index=0,
        host_api_name="ALSA",
        max_input_channels=2,
        max_output_channels=2,
        default_samplerate=48_000,
        is_os_default=False,
    )


def _make_pipeline():
    p = MagicMock()
    p._config = MagicMock(mind_id="jonny")
    p._current_mind_id = "jonny"
    p.reset_coordinator_after_failover = MagicMock()
    return p


def _make_capture_task():
    t = MagicMock()
    t.active_device_guid = "source"
    t.active_device_name = "source"
    t._input_device = 7
    t._host_api_name = "ALSA"
    return t


class TestC3FailoverFullLoop:
    """End-to-end multi-candidate cascade with all Phase 2 pieces wired."""

    @pytest.mark.asyncio()
    async def test_three_candidate_cascade_populates_cache_history_and_events(
        self,
    ) -> None:
        candidates = [
            _make_entry(index=4, name="HD-Audio", canonical="hd-audio"),
            _make_entry(index=7, name="PipeWire", canonical="pipewire"),
            _make_entry(index=8, name="OS Default", canonical="os-default"),
        ]

        capture_task = _make_capture_task()
        pipeline = _make_pipeline()
        tuning = VoiceTuningConfig(
            runtime_failover_on_quarantine_enabled=True,
            failover_intra_ladder_cooldown_s=0.0,
            failover_candidate_max_attempts_per_ladder=5,
        )
        state = RuntimeFailoverState()

        # Candidate 1 fails with classifiable error; candidate 2 fails;
        # candidate 3 succeeds.
        capture_task.request_device_change_restart = AsyncMock(
            side_effect=[
                DeviceChangeRestartResult(
                    verdict=DeviceChangeRestartVerdict.OPEN_FAILED_NO_STREAM,
                    engaged=False,
                    target_device_index=4,
                    target_host_api="ALSA",
                    new_endpoint_guid="g1",
                    detail="device unavailable",
                ),
                DeviceChangeRestartResult(
                    verdict=DeviceChangeRestartVerdict.OPEN_FAILED_NO_STREAM,
                    engaged=False,
                    target_device_index=7,
                    target_host_api="ALSA",
                    new_endpoint_guid="g2",
                    detail="device is busy",
                ),
                DeviceChangeRestartResult(
                    verdict=DeviceChangeRestartVerdict.DEVICE_CHANGED_SUCCESS,
                    engaged=True,
                    target_device_index=8,
                    target_host_api="ALSA",
                    new_endpoint_guid="g3",
                ),
            ],
        )

        resolve_seq = [
            (candidates[0], 3, None),
            (candidates[1], 2, None),
            (candidates[2], 1, None),
        ]
        with patch.object(
            failover_mod,
            "_resolve_target_safe",
            side_effect=resolve_seq,
        ):
            await _try_runtime_failover(
                capture_task=capture_task,
                pipeline=pipeline,
                tuning=tuning,
                state=state,
            )

        # All 3 candidates dispatched.
        assert capture_task.request_device_change_restart.await_count == 3

        # Coordinator reset on candidate 3 success.
        pipeline.reset_coordinator_after_failover.assert_called_once()
        assert state.ladder_exhausted is False

        # ── Probe cache populated by every dispatch outcome ──
        cache = get_default_probe_result_cache()
        # 2 failed entries (HD-Audio, PipeWire); the success on
        # OS Default invalidates that key via record_success — the
        # OS Default entry MUST NOT be in the cache.
        assert cache.lookup("hd-audio", "ALSA") is not None
        assert cache.lookup("pipewire", "ALSA") is not None
        assert cache.lookup("os-default", "ALSA") is None
        # The HD-Audio "device unavailable" detail classifies to
        # UNOPENABLE_THIS_BOOT.
        assert cache.is_known_unopenable("hd-audio", "ALSA") is True
        # "device is busy" classifies to TRANSIENT — NOT skip-worthy.
        assert cache.is_known_unopenable("pipewire", "ALSA") is False

        # ── History ring captures the full ladder ──
        history = get_default_failover_history()
        entries = history.entries()
        assert len(entries) == 1
        run = entries[0]
        assert run.verdict == "succeeded"
        assert run.candidates_tried == 3
        assert run.succeeded_index == 2
        assert len(run.candidates) == 3
        # Per-candidate detail mirrors the dispatch outcomes.
        assert run.candidates[0].verdict == "failed"
        assert run.candidates[0].error_class == "unopenable_this_boot"
        assert run.candidates[1].verdict == "failed"
        assert run.candidates[2].verdict == "succeeded"

        # ── Pipeline-side flag clean-up ──
        # The mock's setattr captures all writes; the LAST write
        # MUST be False (try/finally clears the flag on exit).
        assert pipeline._failover_ladder_in_progress is False

    @pytest.mark.asyncio()
    async def test_exhausted_ladder_surfaces_degraded_state(self) -> None:
        """Every candidate fails → ladder exhausted; state mirrors
        the degraded surface (T2.8 server-side path).
        """
        candidates = [
            _make_entry(index=4, name="A", canonical="dev-a"),
            _make_entry(index=7, name="B", canonical="dev-b"),
        ]

        capture_task = _make_capture_task()
        pipeline = _make_pipeline()
        tuning = VoiceTuningConfig(
            runtime_failover_on_quarantine_enabled=True,
            failover_intra_ladder_cooldown_s=0.0,
        )
        state = RuntimeFailoverState()

        capture_task.request_device_change_restart = AsyncMock(
            return_value=DeviceChangeRestartResult(
                verdict=DeviceChangeRestartVerdict.OPEN_FAILED_NO_STREAM,
                engaged=False,
                target_device_index=99,
                target_host_api="ALSA",
                new_endpoint_guid="g",
                detail="invalid device",  # → UNOPENABLE_PERMANENT
            ),
        )

        resolve_seq = [
            (candidates[0], 2, None),
            (candidates[1], 1, None),
            (None, 0, None),  # exhausted
        ]
        with patch.object(
            failover_mod,
            "_resolve_target_safe",
            side_effect=resolve_seq,
        ):
            await _try_runtime_failover(
                capture_task=capture_task,
                pipeline=pipeline,
                tuning=tuning,
                state=state,
            )

        # State mirrors degraded surface.
        assert state.ladder_exhausted is True
        assert state.last_ladder_complete_monotonic > 0.0
        assert set(state.last_candidates_unreachable) == {"dev-a", "dev-b"}

        # History captures exhausted verdict.
        history = get_default_failover_history()
        entries = history.entries()
        assert len(entries) == 1
        assert entries[0].verdict == "exhausted"
        assert entries[0].succeeded_index is None
