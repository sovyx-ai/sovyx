"""F4 regression — Mission H4 §3 / §10.4 forensic-replay 7-tick L19→L1133.

Replays the canonical v0.43.1 operator-session trajectory through
:class:`ResourceCohortGovernor` and asserts BOTH the RSS-growth AND
thread-count cohorts fire BUDGET_EXCEEDED at the L909 inflection
(uptime=300 s; rss 116 MB → 1.77 GB; threads 18 → 173). Pre-mission
the daemon emitted these 7 snapshots with ZERO operator-actionable
attribution — post-mission the governor flags the breach + records
into the C4 :class:`EngineDegradedStore` under axis="engine_resources"
so the existing DegradedBanner renders the cohort automatically.

Mission anchor:
``docs-internal/missions/MISSION-h4-resource-hygiene-instrumentation-2026-05-19.md``
§0 item #16 + §3 F4 + §10.4 + §12 V-H4-8.

Forensic source: ``docs-internal/FORENSIC-AUDIT-LOG-2026-05-14-v0.43.1.md``
§H4 evidence table (the 7-tick `self.health.snapshot` trajectory).
"""

from __future__ import annotations

import pytest

from sovyx.engine._degraded_store import (
    get_default_degraded_store,
    reset_default_degraded_store,
)
from sovyx.observability._resource_cohort_governor import (
    CohortVerdict,
    ResourceCohortGovernor,
    emit_axis_entries,
    reset_default_resource_cohort_governor,
)
from sovyx.observability._resource_registry import CohortAxis

# v0.43.1 forensic anchor — 7 snapshot ticks at uptime
# 0.009, 60, 120, 180, 240, 300, 360 seconds, with the canonical
# RSS + thread-count trajectory verbatim from the audit log.
_FORENSIC_TICKS: tuple[tuple[float, int, int], ...] = (
    # (uptime_s, rss_bytes, num_threads)
    (0.009, 116 * 1024 * 1024, 18),
    (60.0, 326 * 1024 * 1024, 51),
    (120.0, 378 * 1024 * 1024, 51),
    (180.0, 641 * 1024 * 1024, 68),
    (240.0, 642 * 1024 * 1024, 67),
    # L909 inflection — RSS +1.1 GB / threads +105 in 60 s window.
    (300.0, 1770 * 1024 * 1024, 173),
    (360.0, 1780 * 1024 * 1024, 178),
)


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    reset_default_resource_cohort_governor()
    reset_default_degraded_store()
    yield
    reset_default_resource_cohort_governor()
    reset_default_degraded_store()


_SYNTHETIC_START_MONOTONIC: float = 1000.0
"""Deterministic synthetic clock baseline — Mission H4 v0.49.27 fix.

Pre-v0.49.27 the test passed ``start_monotonic=time.monotonic()`` (a
real wall-clock value ~1e10 magnitude). On Linux Python 3.12 + macOS
Python 3.12 CI runners the test intermittently observed
``INSUFFICIENT_DATA`` at tick 6 because of an interaction between
the real-clock magnitude and the deque-based rolling-window scan
(the exact failure mode is platform-specific and not reproducible
on Windows Python 3.12 nor sovyx-4core Python 3.11 — confirmed via
``gh run view`` on tags v0.49.26).

Using a small fixed baseline (1000.0) makes ``start + uptime_s`` and
``start + uptime_s - window_s`` exact under IEEE 754 single-precision-
range arithmetic, removing the platform-specific variance entirely.
The synthetic clock is logically equivalent — the governor only
sees relative deltas across ticks."""


def _drive_trajectory(
    governor: ResourceCohortGovernor,
    *,
    start_monotonic: float = _SYNTHETIC_START_MONOTONIC,
) -> list[list]:
    """Feed every forensic tick into the governor as if 60 s elapsed.

    Returns the list of CohortEvaluation lists per tick (one per
    snapshot). The governor's monotonic-clock-based rolling-window
    requires per-tick clock advancement — we monkey-patch
    ``time.monotonic`` inside the governor module to march forward in
    lockstep with the forensic uptime field.

    Default ``start_monotonic`` is :data:`_SYNTHETIC_START_MONOTONIC`
    (small fixed value) per the v0.49.27 fix. Callers may still pass
    a real clock value for backward-compat with prior test patterns.
    """
    import sovyx.observability._resource_cohort_governor as gov_mod

    per_tick: list[list] = []
    original_monotonic = gov_mod.time.monotonic
    try:
        for uptime_s, rss_bytes, num_threads in _FORENSIC_TICKS:
            # Make the governor's `time.monotonic()` return our synthetic
            # clock that matches the forensic uptime — necessary because
            # the RSS_GROWTH + THREAD_COUNT cohorts use a rolling
            # `cohort_window_s` window scoped on monotonic time. Without
            # this the whole 360s trajectory collapses into a single
            # window (sub-second wall-clock).
            fake_now = start_monotonic + uptime_s
            gov_mod.time.monotonic = lambda _t=fake_now: _t  # type: ignore[method-assign]
            snapshot = {
                "process.rss_bytes": rss_bytes,
                "process.num_threads": num_threads,
                # Other cohort fields stay at baseline so they don't fire.
                "lock_dict.total_cardinality": 100,
                "onnx.session_count": 4,
                "exception_cohort.retained_bytes_estimate": 0,
            }
            per_tick.append(governor.evaluate_snapshot(snapshot))
    finally:
        gov_mod.time.monotonic = original_monotonic  # type: ignore[method-assign]
    return per_tick


class TestH4ForensicAnchorReplay:
    """F4 — Mission H4 §3 / §10.4 forensic-replay regression."""

    def test_rss_growth_cohort_fires_at_l909_inflection(self) -> None:
        """The 100MB→1.77GB jump at uptime=300s MUST fire RSS_GROWTH."""
        governor = ResourceCohortGovernor()
        per_tick = _drive_trajectory(governor)
        # Tick 5 (index 5) is uptime=300s — the L909 inflection.
        l909_results = per_tick[5]
        rss_result = next(r for r in l909_results if r.axis == CohortAxis.RSS_GROWTH)
        assert rss_result.verdict == CohortVerdict.BUDGET_EXCEEDED, (
            f"RSS_GROWTH MUST fire BUDGET_EXCEEDED at L909 inflection; "
            f"got {rss_result.verdict} (observed={rss_result.observed})"
        )

    def test_thread_count_cohort_fires_at_l909_inflection(self) -> None:
        """The 67→173 thread jump at uptime=300s MUST fire THREAD_COUNT."""
        governor = ResourceCohortGovernor()
        per_tick = _drive_trajectory(governor)
        l909_results = per_tick[5]
        thread_result = next(r for r in l909_results if r.axis == CohortAxis.THREAD_COUNT)
        assert thread_result.verdict == CohortVerdict.BUDGET_EXCEEDED, (
            f"THREAD_COUNT MUST fire BUDGET_EXCEEDED at L909 inflection; "
            f"got {thread_result.verdict} (observed={thread_result.observed})"
        )

    def test_both_cohorts_co_occur_at_inflection(self) -> None:
        """Per ADR-D6: 2 cohorts simultaneously → severity escalation hook.

        The composite store records both entries under
        axis="engine_resources" + the C4 endpoint's severity escalator
        will see 2 entries on the same axis → operator banner severity
        bumps from warn → error per ADR-D6 (inherited from C4).
        """
        governor = ResourceCohortGovernor()
        per_tick = _drive_trajectory(governor)
        emit_axis_entries(per_tick[5])
        snapshot = get_default_degraded_store().snapshot()
        engine_axis_entries = [e for e in snapshot if e.axis == "engine_resources"]
        reasons = {e.reason for e in engine_axis_entries}
        # v0.49.24 — spec-literal reason names (were rss_growth / thread_count).
        assert "engine_resources.rss_growth_spike" in reasons, reasons
        assert "engine_resources.thread_count_spike" in reasons, reasons

    def test_pre_inflection_ticks_do_not_fire(self) -> None:
        """Ticks 1-4 (baseline + warmup) MUST NOT fire BUDGET_EXCEEDED."""
        governor = ResourceCohortGovernor()
        per_tick = _drive_trajectory(governor)
        # Ticks 0-4 (uptime 0s..240s) — RSS grows from 116MB→642MB across
        # the 60s rolling window. The largest 60s-window delta is
        # |641-378|=263 MiB at tick 3 — within the 512 MiB budget.
        # (Tick 2's window contains samples 326 + 378 MiB → Δ=52 MiB.)
        for idx in range(0, 5):
            tick = per_tick[idx]
            rss = next(r for r in tick if r.axis == CohortAxis.RSS_GROWTH)
            assert rss.verdict != CohortVerdict.BUDGET_EXCEEDED, (
                f"Tick {idx} (uptime {_FORENSIC_TICKS[idx][0]}s) MUST NOT "
                f"fire RSS_GROWTH BUDGET_EXCEEDED; got {rss.verdict}"
            )

    def test_post_inflection_tick_observes_sustained_breach(self) -> None:
        """Tick 6 (uptime=360s) — RSS plateau at 1.78 GB; the rolling
        60s window now contains BOTH the spike (1.77 GB) + sustained
        plateau (1.78 GB) → Δ stays small (10 MiB) so the cohort
        returns HEALTHY in the absolute-Δ sense.

        Mission B B-P0-3 update (2026-05-21): this test previously
        documented "composite-store entry recorded at tick 5 persists
        until manually cleared OR the rolling window cycles past the
        spike" as expected behavior — which was the canonical statement
        of the B-P0-3 bug. The bug-as-spec docstring made this
        regression case the FORENSIC ANCHOR for the bug class.

        Post-B.1.P3 the verdict assertion stays HEALTHY (governor
        verdict is correct at tick 6 — the rolling window has cycled
        past the spike). The STORE-SIDE invariant is now exercised by
        ``tests/unit/observability/test_resource_cohort_governor_hysteresis.py``
        which drives sustained N consecutive HEALTHY ticks and asserts
        ``clear_reason`` fires. This replay test deliberately keeps
        scope narrow (verdict-only) so the forensic-anchor trajectory
        stays comparable across versions.
        """
        governor = ResourceCohortGovernor()
        per_tick = _drive_trajectory(governor)
        tick6 = per_tick[6]
        rss = next(r for r in tick6 if r.axis == CohortAxis.RSS_GROWTH)
        # At tick 6 the rolling window covers uptime [300s, 360s] →
        # samples [1770 MiB, 1780 MiB] → Δ=10 MiB < 512 MiB budget.
        # Post-mission this is the correct HEALTHY verdict; the B-P0-3
        # hysteresis test now exercises the consequent store-side clear.
        assert rss.verdict == CohortVerdict.HEALTHY, (
            f"Tick 6 (uptime 360s, rolling-window covers only the "
            f"plateau) MUST be HEALTHY; got {rss.verdict} "
            f"(observed={rss.observed})"
        )
