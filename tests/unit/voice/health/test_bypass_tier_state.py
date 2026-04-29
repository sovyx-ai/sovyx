"""Tests for :mod:`sovyx.voice.health._bypass_tier_state`.

Voice Windows Paranoid Mission Phase 3 / master mission §Phase 3.T3.13
backend wire-up: pin the in-memory counter mirror that backs the
``GET /api/voice/bypass-tier-status`` dashboard endpoint. Each
``record_tier*_*`` and ``record_bypass_strategy_verdict`` helper in
:mod:`sovyx.voice.health._metrics` MUST update this mirror synchronously
so the dashboard observes the same counts that flow to the OTel exporter.
"""

from __future__ import annotations

import pytest

from sovyx.voice.health._bypass_tier_state import (
    BypassTierSnapshot,
    mark_strategy_verdict,
    mark_tier1_raw_attempted,
    mark_tier1_raw_outcome,
    mark_tier2_host_api_rotate_attempted,
    mark_tier2_host_api_rotate_outcome,
    reset_for_tests,
    snapshot,
)


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    """Isolate every test against a clean global mirror."""
    reset_for_tests()


class TestSnapshotInitialState:
    """The mirror starts at zero on every counter and current_bypass_tier=None."""

    def test_initial_snapshot_is_empty(self) -> None:
        result = snapshot()
        assert result == {
            "current_bypass_tier": None,
            "tier1_raw_attempted": 0,
            "tier1_raw_succeeded": 0,
            "tier2_host_api_rotate_attempted": 0,
            "tier2_host_api_rotate_succeeded": 0,
            "tier3_wasapi_exclusive_attempted": 0,
            "tier3_wasapi_exclusive_succeeded": 0,
        }

    def test_dataclass_default_matches_snapshot(self) -> None:
        defaults = BypassTierSnapshot()
        assert defaults.current_bypass_tier is None
        assert defaults.tier1_raw_attempted == 0
        assert defaults.tier3_wasapi_exclusive_succeeded == 0


class TestTier1RawCounters:
    """Tier 1 RAW + Communications mirror (`win.raw_communications`)."""

    def test_attempted_increments(self) -> None:
        mark_tier1_raw_attempted()
        mark_tier1_raw_attempted()
        mark_tier1_raw_attempted()
        assert snapshot()["tier1_raw_attempted"] == 3

    def test_outcome_raw_engaged_increments_succeeded(self) -> None:
        mark_tier1_raw_outcome("raw_engaged")
        assert snapshot()["tier1_raw_succeeded"] == 1

    def test_outcome_other_verdicts_do_not_increment_succeeded(self) -> None:
        for verdict in (
            "property_rejected_by_driver",
            "open_failed_no_stream",
            "open_failed_fallback_to_plain",
            "not_running",
            "not_win32",
        ):
            mark_tier1_raw_outcome(verdict)
        assert snapshot()["tier1_raw_succeeded"] == 0

    def test_outcome_does_not_increment_attempted(self) -> None:
        mark_tier1_raw_outcome("raw_engaged")
        assert snapshot()["tier1_raw_attempted"] == 0


class TestTier2HostApiRotateCounters:
    """Tier 2 host_api_rotate_then_exclusive mirror."""

    def test_attempted_increments(self) -> None:
        mark_tier2_host_api_rotate_attempted()
        mark_tier2_host_api_rotate_attempted()
        assert snapshot()["tier2_host_api_rotate_attempted"] == 2

    def test_outcome_phase_a_rotated_phase_b_engaged_succeeds(self) -> None:
        mark_tier2_host_api_rotate_outcome(
            phase_a_verdict="rotated_success",
            phase_b_verdict="exclusive_engaged",
        )
        assert snapshot()["tier2_host_api_rotate_succeeded"] == 1

    def test_outcome_combined_token_succeeds(self) -> None:
        # The strategy may emit the combined success token directly.
        mark_tier2_host_api_rotate_outcome(
            phase_a_verdict="rotated_success",
            phase_b_verdict="rotated_then_exclusive_engaged",
        )
        assert snapshot()["tier2_host_api_rotate_succeeded"] == 1

    def test_outcome_phase_a_failed_skips_phase_b_does_not_succeed(self) -> None:
        mark_tier2_host_api_rotate_outcome(
            phase_a_verdict="no_target_sibling",
            phase_b_verdict="skipped",
        )
        assert snapshot()["tier2_host_api_rotate_succeeded"] == 0

    def test_outcome_phase_b_downgraded_does_not_succeed(self) -> None:
        mark_tier2_host_api_rotate_outcome(
            phase_a_verdict="rotated_success",
            phase_b_verdict="exclusive_downgraded_to_shared",
        )
        assert snapshot()["tier2_host_api_rotate_succeeded"] == 0


class TestTier3WasapiExclusiveCounters:
    """Tier 3 WASAPI exclusive mirror via coordinator-level verdict hook."""

    def test_attempt_via_strategy_verdict_increments_attempted(self) -> None:
        mark_strategy_verdict(strategy="win.wasapi_exclusive", verdict="applied_healthy")
        assert snapshot()["tier3_wasapi_exclusive_attempted"] == 1
        assert snapshot()["tier3_wasapi_exclusive_succeeded"] == 1

    def test_applied_still_dead_increments_attempted_only(self) -> None:
        mark_strategy_verdict(strategy="win.wasapi_exclusive", verdict="applied_still_dead")
        assert snapshot()["tier3_wasapi_exclusive_attempted"] == 1
        assert snapshot()["tier3_wasapi_exclusive_succeeded"] == 0

    def test_failed_to_apply_increments_attempted_only(self) -> None:
        mark_strategy_verdict(strategy="win.wasapi_exclusive", verdict="failed_to_apply")
        assert snapshot()["tier3_wasapi_exclusive_attempted"] == 1
        assert snapshot()["tier3_wasapi_exclusive_succeeded"] == 0

    def test_not_applicable_does_not_count_as_attempt(self) -> None:
        # not_applicable signals eligibility rejection, not an attempt.
        mark_strategy_verdict(strategy="win.wasapi_exclusive", verdict="not_applicable")
        assert snapshot()["tier3_wasapi_exclusive_attempted"] == 0
        assert snapshot()["tier3_wasapi_exclusive_succeeded"] == 0

    def test_other_strategies_do_not_touch_tier3_counters(self) -> None:
        # Tier 1 + Tier 2 fire their own helpers; the coordinator hook
        # filters strictly to win.wasapi_exclusive.
        mark_strategy_verdict(strategy="win.raw_communications", verdict="applied_healthy")
        mark_strategy_verdict(
            strategy="win.host_api_rotate_then_exclusive", verdict="applied_healthy"
        )
        mark_strategy_verdict(strategy="linux.alsa_hw_direct", verdict="applied_healthy")
        s = snapshot()
        assert s["tier3_wasapi_exclusive_attempted"] == 0
        assert s["tier3_wasapi_exclusive_succeeded"] == 0


class TestSnapshotIsolation:
    """``snapshot()`` returns a copy — mutating it must not affect state."""

    def test_snapshot_is_a_copy(self) -> None:
        mark_tier1_raw_attempted()
        first = snapshot()
        first["tier1_raw_attempted"] = 999
        second = snapshot()
        assert second["tier1_raw_attempted"] == 1

    def test_reset_clears_all_counters(self) -> None:
        mark_tier1_raw_attempted()
        mark_tier1_raw_outcome("raw_engaged")
        mark_tier2_host_api_rotate_attempted()
        mark_strategy_verdict(strategy="win.wasapi_exclusive", verdict="applied_healthy")
        reset_for_tests()
        assert snapshot() == BypassTierSnapshot().__dict__


class TestMixedScenarios:
    """End-to-end scenarios mixing all three tiers."""

    def test_three_tiers_independent(self) -> None:
        # Tier 1: 5 attempts, 3 succeeded.
        for _ in range(5):
            mark_tier1_raw_attempted()
        for _ in range(3):
            mark_tier1_raw_outcome("raw_engaged")
        for _ in range(2):
            mark_tier1_raw_outcome("property_rejected_by_driver")
        # Tier 2: 4 attempts, 2 succeeded.
        for _ in range(4):
            mark_tier2_host_api_rotate_attempted()
        for _ in range(2):
            mark_tier2_host_api_rotate_outcome(
                phase_a_verdict="rotated_success",
                phase_b_verdict="exclusive_engaged",
            )
        for _ in range(2):
            mark_tier2_host_api_rotate_outcome(
                phase_a_verdict="no_target_sibling",
                phase_b_verdict="skipped",
            )
        # Tier 3: 6 verdicts, 4 succeeded.
        for _ in range(4):
            mark_strategy_verdict(strategy="win.wasapi_exclusive", verdict="applied_healthy")
        for _ in range(2):
            mark_strategy_verdict(strategy="win.wasapi_exclusive", verdict="applied_still_dead")

        assert snapshot() == {
            "current_bypass_tier": None,
            "tier1_raw_attempted": 5,
            "tier1_raw_succeeded": 3,
            "tier2_host_api_rotate_attempted": 4,
            "tier2_host_api_rotate_succeeded": 2,
            "tier3_wasapi_exclusive_attempted": 6,
            "tier3_wasapi_exclusive_succeeded": 4,
        }
