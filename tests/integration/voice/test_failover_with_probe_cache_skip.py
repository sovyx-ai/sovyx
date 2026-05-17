"""Integration test — failover loop with probe-cache short-circuit.

Mission anchor: ``docs-internal/missions/MISSION-c3-failover-ladder-iteration-2026-05-16.md``
§9.2.

Exercises the cache-skip path end-to-end: a candidate flagged in the
``ProbeResultCache`` (e.g. via a pre-boot-cascade probe that found
``NO_SIGNAL``) is skipped by the loop body WITHOUT dispatch,
emitting ``voice.failover.candidate_skipped`` with the cached
verdict; the loop then advances to the next candidate.

This is the core efficiency win promised by ADR-D4: unopenable
devices skipped without paying the ~1 s open-thrash per skipped
device the operator's v0.43.1 session paid for HD-Audio Generic
idx=4.
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
    ProbeResultEntry,
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


def _make_entry(*, index: int, canonical: str) -> DeviceEntry:
    return DeviceEntry(
        index=index,
        name=canonical,
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


class TestC3CacheSkipIntegration:
    """ADR-D4 cache short-circuit — pre-flagged candidate skipped
    without dispatch."""

    @pytest.mark.asyncio()
    async def test_cache_flagged_candidate_skipped_loop_advances(self) -> None:
        cache = get_default_probe_result_cache()

        # Pre-flag candidate 1 as UNOPENABLE_PERMANENT — simulates the
        # boot cascade probe verdict NO_SIGNAL or a prior dispatch
        # that returned ``-9996 paInvalidDevice``.
        cache.record_probe(
            ProbeResultEntry(
                endpoint_guid="dead-device",
                host_api="ALSA",
                verdict="",
                error_code="-9996",  # paInvalidDevice → UNOPENABLE_PERMANENT
            ),
        )

        dead = _make_entry(index=4, canonical="dead-device")
        good = _make_entry(index=7, canonical="good-device")

        capture_task = _make_capture_task()
        pipeline = _make_pipeline()
        tuning = VoiceTuningConfig(
            runtime_failover_on_quarantine_enabled=True,
            failover_intra_ladder_cooldown_s=0.0,
        )
        state = RuntimeFailoverState()

        capture_task.request_device_change_restart = AsyncMock(
            return_value=DeviceChangeRestartResult(
                verdict=DeviceChangeRestartVerdict.DEVICE_CHANGED_SUCCESS,
                engaged=True,
                target_device_index=7,
                target_host_api="ALSA",
                new_endpoint_guid="g",
            ),
        )

        # Resolver returns dead first (which the loop will skip via
        # cache lookup), then the good candidate.
        resolve_seq = [
            (dead, 2, None),
            (good, 1, None),
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

        # The dead candidate was SKIPPED via cache short-circuit —
        # only ONE dispatch fired (for the good candidate).
        assert capture_task.request_device_change_restart.await_count == 1
        # Success path engaged.
        pipeline.reset_coordinator_after_failover.assert_called_once()
        assert state.ladder_exhausted is False

        # History captures the skipped candidate row.
        history = get_default_failover_history()
        entries = history.entries()
        assert len(entries) == 1
        run = entries[0]
        assert run.verdict == "succeeded"
        assert run.candidates_tried == 1  # only the dispatched one counts toward "tried"
        # The skipped candidate is recorded with verdict="skipped".
        skipped = [c for c in run.candidates if c.verdict == "skipped"]
        assert len(skipped) == 1
        assert skipped[0].target_endpoint == "dead-device"
        assert skipped[0].skipped_reason == "probe_cache_unopenable"

    @pytest.mark.asyncio()
    async def test_cache_lookup_does_not_skip_transient_codes(self) -> None:
        """ADR-D4: ``AUDCLNT_E_DEVICE_IN_USE`` classifies to TRANSIENT
        — the cache MUST NOT short-circuit; the loop must dispatch.
        """
        cache = get_default_probe_result_cache()
        cache.record_probe(
            ProbeResultEntry(
                endpoint_guid="busy-device",
                host_api="ALSA",
                verdict="",
                error_code="audclnt_e_device_in_use",
            ),
        )

        busy = _make_entry(index=4, canonical="busy-device")

        capture_task = _make_capture_task()
        pipeline = _make_pipeline()
        tuning = VoiceTuningConfig(
            runtime_failover_on_quarantine_enabled=True,
            failover_intra_ladder_cooldown_s=0.0,
        )
        state = RuntimeFailoverState()

        capture_task.request_device_change_restart = AsyncMock(
            return_value=DeviceChangeRestartResult(
                verdict=DeviceChangeRestartVerdict.DEVICE_CHANGED_SUCCESS,
                engaged=True,
                target_device_index=4,
                target_host_api="ALSA",
                new_endpoint_guid="g",
            ),
        )

        with patch.object(
            failover_mod,
            "_resolve_target_safe",
            return_value=(busy, 1, None),
        ):
            await _try_runtime_failover(
                capture_task=capture_task,
                pipeline=pipeline,
                tuning=tuning,
                state=state,
            )

        # Transient code → cache MUST NOT skip → dispatch fires.
        assert capture_task.request_device_change_restart.await_count == 1
        # Successful open invalidated the stale TRANSIENT entry.
        assert cache.lookup("busy-device", "ALSA") is None
