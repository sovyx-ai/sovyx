"""Mission C1 commit 4b §T1.6 — factory consumer outcome-predicate tests.

Covers the two factory-level predicates used by ``_on_deaf_signal`` to
decide downstream remediation:

* :func:`_outcomes_have_applied_healthy` — recovery success (legacy
  strategy OR Mission C1 ladder).
* :func:`_outcomes_have_normalizer_engagement_request` — coordinator
  dispatched a NORMALIZER_ENGAGEMENT_REQUESTED outcome.

The end-to-end ``_on_deaf_signal`` integration tests live next to the
factory build path (the closure is constructed inside
:func:`create_voice_pipeline` and is hard to reach in isolation); the
unit-level coverage here pins the predicate logic so refactors don't
silently drift.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sovyx.voice.factory import (
    _outcomes_have_applied_healthy,
    _outcomes_have_normalizer_engagement_request,
)
from sovyx.voice.health.contract import (
    BypassOutcome,
    BypassVerdict,
    IntegrityResult,
    IntegrityVerdict,
)


def _outcome(verdict: BypassVerdict) -> BypassOutcome:
    return BypassOutcome(
        strategy_name="x",
        attempt_index=0,
        verdict=verdict,
        integrity_before=IntegrityResult(
            verdict=IntegrityVerdict.VAD_FRONTEND_DEAD,
            endpoint_guid="g",
            rms_db=-40.0,
            vad_max_prob=0.0,
            spectral_flatness=0.2,
            spectral_rolloff_hz=4000.0,
            duration_s=3.0,
            probed_at_utc=datetime.now(UTC),
            raw_frames=48_000,
        ),
        integrity_after=None,
        elapsed_ms=0.0,
    )


class TestOutcomesHaveAppliedHealthy:
    """Mission C1 §T1.6 — predicate covers legacy + ladder success."""

    def test_legacy_applied_healthy_counts(self) -> None:
        assert _outcomes_have_applied_healthy([_outcome(BypassVerdict.APPLIED_HEALTHY)]) is True

    def test_ladder_applied_healthy_counts(self) -> None:
        assert (
            _outcomes_have_applied_healthy(
                [_outcome(BypassVerdict.VAD_FRONTEND_RESET_APPLIED_HEALTHY)],
            )
            is True
        )

    def test_still_dead_does_not_count(self) -> None:
        assert (
            _outcomes_have_applied_healthy(
                [_outcome(BypassVerdict.APPLIED_STILL_DEAD)],
            )
            is False
        )

    def test_ladder_still_dead_does_not_count(self) -> None:
        assert (
            _outcomes_have_applied_healthy(
                [_outcome(BypassVerdict.VAD_FRONTEND_RESET_APPLIED_STILL_DEAD)],
            )
            is False
        )

    def test_dispatch_outcomes_do_not_count(self) -> None:
        """CASCADE / NORMALIZER request outcomes are not success — they
        are dispatch REQUESTS, not recovery."""
        assert (
            _outcomes_have_applied_healthy(
                [_outcome(BypassVerdict.CASCADE_REEVALUATION_REQUESTED)],
            )
            is False
        )
        assert (
            _outcomes_have_applied_healthy(
                [_outcome(BypassVerdict.NORMALIZER_ENGAGEMENT_REQUESTED)],
            )
            is False
        )

    def test_empty_list(self) -> None:
        assert _outcomes_have_applied_healthy([]) is False


class TestOutcomesHaveNormalizerEngagementRequest:
    """Mission C1 §T1.6 — predicate detects coordinator dispatch."""

    def test_request_present(self) -> None:
        assert (
            _outcomes_have_normalizer_engagement_request(
                [_outcome(BypassVerdict.NORMALIZER_ENGAGEMENT_REQUESTED)],
            )
            is True
        )

    def test_request_absent(self) -> None:
        assert (
            _outcomes_have_normalizer_engagement_request(
                [_outcome(BypassVerdict.APPLIED_HEALTHY)],
            )
            is False
        )

    def test_cascade_request_does_not_count(self) -> None:
        """CASCADE request is a SIBLING dispatch verdict, NOT the
        normalizer engagement request — predicates stay disjoint."""
        assert (
            _outcomes_have_normalizer_engagement_request(
                [_outcome(BypassVerdict.CASCADE_REEVALUATION_REQUESTED)],
            )
            is False
        )

    def test_empty_list(self) -> None:
        assert _outcomes_have_normalizer_engagement_request([]) is False
