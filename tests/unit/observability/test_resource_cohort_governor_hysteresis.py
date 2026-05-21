"""Mission B B-P0-3 — clear_axis hysteresis regression suite.

Forensic context. At HEAD ``2985245a`` (v0.49.36) the governor's class
docstring promised "HEALTHY — Clears any prior engine_resources.<axis>
entries from the EngineDegradedStore per C4 ADR-D5 axis-clear-on-success"
but ``emit_axis_entries`` silently skipped every non-BUDGET_EXCEEDED
verdict — once any cohort breached, the composite-store entry stuck
forever (until process restart). Mission B classified this as the
**consumer-side recreation of the exact pre-A.1.P2 "permanently
breached" pathology** that Mission A.1.P2 had just closed on the
producer side (F-002+F-003 ``retained_bytes_estimate`` monotonic
accumulator under gauge-implying name).

B.1.P3 closure (this test):
* ``emit_axis_entries`` now iterates ALL evaluations + tracks per-axis
  last verdict + consecutive-HEALTHY counter; after N consecutive
  HEALTHY ticks (default ``cohort_clear_consecutive_healthy_threshold=3``)
  it calls ``EngineDegradedStore.clear_reason()`` (per-reason — protects
  sibling cohorts) on the matching ``_REASON_FOR_AXIS[axis]``.
* Feature flag ``observability.features.cohort_axis_auto_clear``
  (default True per anti-pattern #34 inverse) gates the entire clear
  path; setting it False restores v0.49.36 stuck-banner behavior.

This file pins the following invariants:

1. Sustained-recovery sequence ``[HEALTHY×2, BUDGET, BUDGET, HEALTHY×3]``
   transitions store entries ``0→0→1→1→1→1→0`` (clear on N-th HEALTHY).
2. Oscillation under threshold-adjacent workload
   ``[BUDGET, HEALTHY, BUDGET, HEALTHY, BUDGET]`` never reaches N
   consecutive HEALTHY → entry persists throughout (anti-flicker).
3. Per-reason (NOT per-axis) clear — when RSS_GROWTH recovers, an
   unrelated ONNX_SESSION breach entry is preserved.
4. Feature-flag-off restores v0.49.36 behavior (stuck-banner).
5. Single-shot recovery (one HEALTHY after BUDGET, never reaching N)
   leaves the entry in place.

Mission anchor:
``docs-internal/MISSION-B-FINDINGS-REGISTER-2026-05-21.md`` §1 B-P0-3 +
``docs-internal/MISSION-B-REMEDIATION-PLAN-2026-05-21.md`` §5 B.1.P3.
Anti-pattern #54 (composite-store record-clear pairing).
"""

from __future__ import annotations

from typing import Any

import pytest

import sovyx.observability._resource_cohort_governor as _governor_mod
from sovyx.engine._degraded_store import (
    get_default_degraded_store,
    reset_default_degraded_store,
)
from sovyx.observability._resource_cohort_governor import (
    _REASON_FOR_AXIS,
    _SINGLETON_LOCK,
    CohortEvaluation,
    CohortVerdict,
    ResourceCohortGovernor,
    emit_axis_entries,
    reset_default_resource_cohort_governor,
)
from sovyx.observability._resource_registry import CohortAxis


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    reset_default_resource_cohort_governor()
    reset_default_degraded_store()
    yield
    reset_default_resource_cohort_governor()
    reset_default_degraded_store()


def _install_governor(
    *, clear_threshold: int = 3, auto_clear_enabled: bool = True
) -> ResourceCohortGovernor:
    """Install a governor singleton with explicit hysteresis tuning."""
    governor = ResourceCohortGovernor(
        clear_threshold=clear_threshold,
        auto_clear_enabled=auto_clear_enabled,
    )
    with _SINGLETON_LOCK:
        _governor_mod._SINGLETON = governor
    return governor


def _eval(
    axis: CohortAxis, verdict: CohortVerdict, observed: int = 0, budget: int = 0
) -> CohortEvaluation:
    return CohortEvaluation(
        axis=axis,
        verdict=verdict,
        observed=observed,
        budget=budget,
    )


def _store_entries_for_axis(axis_name: str = "engine_resources") -> list[Any]:
    snapshot = get_default_degraded_store().snapshot()
    return [e for e in snapshot if e.axis == axis_name]


class TestSustainedRecoverySequence:
    """The canonical B-P0-3 trajectory: BREACH then sustained HEALTHY."""

    def test_three_consecutive_healthy_clears_entry_at_default_threshold(self) -> None:
        """Per the §5 B.1.P3 mapping, drive [HEALTHY×2, BUDGET×2, HEALTHY×3].

        Expected store-entry counts per tick: 0, 0, 1, 1, 1, 1, 0.

        Falsifiability: pre-fix the final HEALTHY tick was a no-op;
        the entry persisted forever. The 7th-tick assertion below is
        the load-bearing post-fix invariant.
        """
        _install_governor(clear_threshold=3)

        # Ticks 1-2: HEALTHY before any breach. Nothing to clear; store stays empty.
        emit_axis_entries([_eval(CohortAxis.RSS_GROWTH, CohortVerdict.HEALTHY)])
        assert len(_store_entries_for_axis()) == 0
        emit_axis_entries([_eval(CohortAxis.RSS_GROWTH, CohortVerdict.HEALTHY)])
        assert len(_store_entries_for_axis()) == 0

        # Ticks 3-4: BUDGET_EXCEEDED. Entry recorded; persists across ticks.
        emit_axis_entries(
            [
                _eval(
                    CohortAxis.RSS_GROWTH,
                    CohortVerdict.BUDGET_EXCEEDED,
                    600 * 1024 * 1024,
                    512 * 1024 * 1024,
                )
            ]
        )
        assert len(_store_entries_for_axis()) == 1
        emit_axis_entries(
            [
                _eval(
                    CohortAxis.RSS_GROWTH,
                    CohortVerdict.BUDGET_EXCEEDED,
                    700 * 1024 * 1024,
                    512 * 1024 * 1024,
                )
            ]
        )
        assert len(_store_entries_for_axis()) == 1

        # Ticks 5-6: HEALTHY recovery starts. consecutive_healthy=1,2 (< 3).
        # Entry MUST persist.
        emit_axis_entries([_eval(CohortAxis.RSS_GROWTH, CohortVerdict.HEALTHY)])
        assert len(_store_entries_for_axis()) == 1
        emit_axis_entries([_eval(CohortAxis.RSS_GROWTH, CohortVerdict.HEALTHY)])
        assert len(_store_entries_for_axis()) == 1

        # Tick 7: consecutive_healthy reaches 3 — clear_reason fires.
        emit_axis_entries([_eval(CohortAxis.RSS_GROWTH, CohortVerdict.HEALTHY)])
        assert len(_store_entries_for_axis()) == 0, (
            "B-P0-3 regression: 3 consecutive HEALTHY ticks did not "
            "trigger clear_reason. Anti-pattern #54."
        )


class TestOscillationDoesNotClear:
    """Threshold-adjacent oscillation must not flicker the banner.

    The same workload value flapping above/below threshold should NOT
    repeatedly clear+record. Hysteresis is the protection.
    """

    def test_alternating_breach_healthy_never_clears(self) -> None:
        """Drive [BUDGET, HEALTHY, BUDGET, HEALTHY, BUDGET, HEALTHY].

        Each HEALTHY tick is preceded by a fresh breach; the
        consecutive-healthy counter resets to 0 on every BUDGET so it
        never reaches N=3. Store entry persists throughout.

        Falsifiability: if clear_threshold defaulted to 1 (no
        hysteresis) the banner would flicker on every HEALTHY tick;
        operators would distrust the signal. Anti-pattern #28 sibling.
        """
        _install_governor(clear_threshold=3)

        for tick in range(6):
            verdict = CohortVerdict.BUDGET_EXCEEDED if tick % 2 == 0 else CohortVerdict.HEALTHY
            observed = (
                600 * 1024 * 1024
                if verdict == CohortVerdict.BUDGET_EXCEEDED
                else 100 * 1024 * 1024
            )
            emit_axis_entries([_eval(CohortAxis.RSS_GROWTH, verdict, observed, 512 * 1024 * 1024)])
            # After ANY tick in this oscillation, the breach entry must
            # remain because no HEALTHY streak ever reaches N.
            entries = _store_entries_for_axis()
            assert len(entries) == 1, (
                f"B-P0-3 oscillation regression at tick {tick}: "
                f"expected 1 entry, got {len(entries)}. The hysteresis "
                f"counter must reset on every BUDGET_EXCEEDED."
            )


class TestPerReasonClearProtectsSiblings:
    """When ONE cohort recovers, OTHER cohorts' entries must survive.

    The fix uses ``clear_reason()`` not ``clear_axis()`` — protecting
    sibling cohort entries that share ``axis="engine_resources"``.
    """

    def test_rss_recovery_does_not_clear_onnx_breach(self) -> None:
        """RSS_GROWTH breaches → recovers; ONNX_SESSION breaches concurrently.

        After RSS sustained recovery, the ONNX entry MUST remain.
        Falsifiability: a naive ``clear_axis("engine_resources")`` would
        silently drop the live ONNX breach.
        """
        _install_governor(clear_threshold=3)

        # Both cohorts breach.
        emit_axis_entries(
            [
                _eval(
                    CohortAxis.RSS_GROWTH,
                    CohortVerdict.BUDGET_EXCEEDED,
                    600 * 1024 * 1024,
                    512 * 1024 * 1024,
                ),
                _eval(CohortAxis.ONNX_SESSION, CohortVerdict.BUDGET_EXCEEDED, 9, 8),
            ]
        )
        assert len(_store_entries_for_axis()) == 2

        # RSS recovers for 3 ticks; ONNX stays breached.
        for _ in range(3):
            emit_axis_entries(
                [
                    _eval(CohortAxis.RSS_GROWTH, CohortVerdict.HEALTHY),
                    _eval(CohortAxis.ONNX_SESSION, CohortVerdict.BUDGET_EXCEEDED, 9, 8),
                ]
            )

        # ONNX entry must survive; RSS entry must be cleared.
        entries = {e.reason for e in _store_entries_for_axis()}
        assert _REASON_FOR_AXIS[CohortAxis.RSS_GROWTH] not in entries, (
            "B-P0-3 regression: RSS sustained recovery did not clear its own reason."
        )
        assert _REASON_FOR_AXIS[CohortAxis.ONNX_SESSION] in entries, (
            "B-P0-3 regression: RSS recovery cleared the unrelated "
            "ONNX_SESSION entry — clear_axis() instead of clear_reason() "
            "would cause this. clear_reason() is the correct API."
        )


class TestFeatureFlagOff:
    """Restores v0.49.36 stuck-banner behavior — operator escape hatch."""

    def test_auto_clear_disabled_preserves_pre_b_p0_3_behavior(self) -> None:
        """With cohort_axis_auto_clear=False, sustained HEALTHY does NOT clear.

        Falsifiability: this assertion would have passed unmodified at
        v0.49.36 (which had no auto-clear). Operators who tune the flag
        OFF get the exact previous behavior, providing an instant
        rollback path WITHOUT a hotfix tag.
        """
        _install_governor(clear_threshold=3, auto_clear_enabled=False)

        emit_axis_entries(
            [
                _eval(
                    CohortAxis.RSS_GROWTH,
                    CohortVerdict.BUDGET_EXCEEDED,
                    600 * 1024 * 1024,
                    512 * 1024 * 1024,
                )
            ]
        )
        for _ in range(10):
            emit_axis_entries([_eval(CohortAxis.RSS_GROWTH, CohortVerdict.HEALTHY)])
        assert len(_store_entries_for_axis()) == 1, (
            "B-P0-3 feature flag regression: cohort_axis_auto_clear=False "
            "should preserve the v0.49.36 stuck-banner behavior."
        )


class TestSingleHealthyTickInsufficient:
    """Single HEALTHY after BREACH must NOT clear (anti-flicker bedrock)."""

    def test_one_healthy_after_breach_leaves_entry(self) -> None:
        """Drive [BUDGET, HEALTHY]. After tick 2, entry must persist.

        consecutive_healthy=1 < default clear_threshold=3.
        """
        _install_governor(clear_threshold=3)

        emit_axis_entries(
            [
                _eval(
                    CohortAxis.RSS_GROWTH,
                    CohortVerdict.BUDGET_EXCEEDED,
                    600 * 1024 * 1024,
                    512 * 1024 * 1024,
                )
            ]
        )
        assert len(_store_entries_for_axis()) == 1

        emit_axis_entries([_eval(CohortAxis.RSS_GROWTH, CohortVerdict.HEALTHY)])
        assert len(_store_entries_for_axis()) == 1, (
            "B-P0-3 regression: single HEALTHY tick cleared entry. "
            "Hysteresis broken; flicker risk."
        )


class TestInsufficientDataDoesNotResetHysteresis:
    """An observation gap mid-recovery should not lose progress."""

    def test_insufficient_data_does_not_reset_consecutive_healthy(self) -> None:
        """Drive [BUDGET, HEALTHY, INSUFFICIENT_DATA, HEALTHY, HEALTHY].

        At the final HEALTHY, consecutive_healthy should be 3 (the
        INSUFFICIENT_DATA tick does not interrupt the recovery
        sequence). Clear_reason fires.

        Falsifiability: if INSUFFICIENT_DATA reset the counter,
        operators who briefly lose psutil readings (denied permission,
        process-fault state) would have their recovery progress
        wiped — a poor UX. The design treats INSUFFICIENT_DATA as a
        transient observation gap, not as a re-breach.
        """
        _install_governor(clear_threshold=3)

        emit_axis_entries(
            [
                _eval(
                    CohortAxis.RSS_GROWTH,
                    CohortVerdict.BUDGET_EXCEEDED,
                    600 * 1024 * 1024,
                    512 * 1024 * 1024,
                )
            ]
        )
        emit_axis_entries([_eval(CohortAxis.RSS_GROWTH, CohortVerdict.HEALTHY)])
        emit_axis_entries([_eval(CohortAxis.RSS_GROWTH, CohortVerdict.INSUFFICIENT_DATA)])
        emit_axis_entries([_eval(CohortAxis.RSS_GROWTH, CohortVerdict.HEALTHY)])
        emit_axis_entries([_eval(CohortAxis.RSS_GROWTH, CohortVerdict.HEALTHY)])

        assert len(_store_entries_for_axis()) == 0, (
            "B-P0-3 regression: INSUFFICIENT_DATA mid-recovery should "
            "not reset the hysteresis counter."
        )


class TestOperatorAckClearsCompositeStore:
    """Operator ack on cohort breaker now ALSO drops composite-store entry (B-P1-03)."""

    def test_clear_breaker_does_not_clear_store_directly(self) -> None:
        """``governor.clear_breaker(axis)`` alone does NOT touch the store.

        The store-side clear lives in the ack ENDPOINT
        (``engine_resources.py::post_cohort_ack``) which calls
        ``clear_reason()`` alongside ``clear_breaker()``. This split is
        intentional — the two stores are decoupled at API level so
        test fixtures can exercise each path independently. The
        endpoint integration test
        (``test_engine_resources_phase1d.py``) covers the wired
        behavior; this unit test pins the decoupling.
        """
        governor = _install_governor(clear_threshold=3)

        emit_axis_entries(
            [
                _eval(
                    CohortAxis.RSS_GROWTH,
                    CohortVerdict.BUDGET_EXCEEDED,
                    600 * 1024 * 1024,
                    512 * 1024 * 1024,
                )
            ]
        )
        assert len(_store_entries_for_axis()) == 1

        # Pre-B-P1-03 behavior: clear_breaker alone leaves the store entry.
        governor.clear_breaker(CohortAxis.RSS_GROWTH)
        assert len(_store_entries_for_axis()) == 1, (
            "Sanity check: clear_breaker is the in-process governor "
            "API. The store-side clear is wired at the endpoint layer "
            "in engine_resources.py — exercised by the endpoint test."
        )
        # And the breaker hysteresis state is reset.
        assert governor._consecutive_healthy.get(CohortAxis.RSS_GROWTH, 0) == 0
        assert governor._last_verdict.get(CohortAxis.RSS_GROWTH) == CohortVerdict.HEALTHY
