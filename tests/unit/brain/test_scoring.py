"""Tests for sovyx.brain.scoring — ImportanceScorer, ConfidenceScorer, ScoreNormalizer.

Covers:
- Weight validation (sum-to-1.0)
- Initial scoring with various inputs
- Explicit signal floor guarantee
- Access boost diminishing returns
- Connectivity normalization
- Recency exponential decay
- Full recalculation pipeline with velocity damping + soft ceiling
- Confidence source quality, corroboration, staleness, contradiction
- Score normalization (spread too narrow, all-identical, healthy = no-op)
- Property-based tests (Hypothesis) for boundedness and monotonicity
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from sovyx.brain.scoring import (
    ConfidenceScorer,
    ConfidenceWeights,
    EvolutionWeights,
    ImportanceScorer,
    ImportanceWeights,
    ScoreNormalizer,
)

# ── Weight validation ──────────────────────────────────────────────────


class TestImportanceWeights:
    """ImportanceWeights sum validation."""

    def test_defaults_valid(self) -> None:
        w = ImportanceWeights()
        total = w.category_base + w.llm_assessment + w.emotional + w.novelty + w.explicit_signal
        assert abs(total - 1.0) < 0.001

    def test_custom_valid(self) -> None:
        w = ImportanceWeights(
            category_base=0.20,
            llm_assessment=0.30,
            emotional=0.10,
            novelty=0.10,
            explicit_signal=0.30,
        )
        assert w.category_base == 0.20

    def test_invalid_sum_raises(self) -> None:
        with pytest.raises(ValueError, match="must sum to 1.0"):
            ImportanceWeights(category_base=0.50)


class TestConfidenceWeights:
    """ConfidenceWeights sum validation."""

    def test_defaults_valid(self) -> None:
        w = ConfidenceWeights()
        total = w.source_quality + w.llm_assessment + w.explicitness + w.content_richness
        assert abs(total - 1.0) < 0.001

    def test_invalid_sum_raises(self) -> None:
        with pytest.raises(ValueError, match="must sum to 1.0"):
            ConfidenceWeights(source_quality=0.90)


class TestEvolutionWeights:
    """EvolutionWeights sum validation."""

    def test_defaults_valid(self) -> None:
        w = EvolutionWeights()
        total = w.momentum + w.access + w.connectivity + w.recency + w.emotional
        assert abs(total - 1.0) < 0.001

    def test_invalid_sum_raises(self) -> None:
        with pytest.raises(ValueError, match="must sum to 1.0"):
            EvolutionWeights(momentum=0.90)


# ── ImportanceScorer ───────────────────────────────────────────────────


class TestImportanceScorerInitial:
    """score_initial() — creation-time importance."""

    def test_high_inputs_high_importance(self) -> None:
        scorer = ImportanceScorer()
        result = scorer.score_initial(
            category_base=0.80,
            llm_importance=0.90,
            emotional_valence=0.8,
            novelty=0.9,
        )
        assert result > 0.6

    def test_low_inputs_low_importance(self) -> None:
        scorer = ImportanceScorer()
        result = scorer.score_initial(
            category_base=0.20,
            llm_importance=0.10,
            emotional_valence=0.0,
            novelty=0.1,
        )
        assert result < 0.3

    def test_explicit_signal_floor(self) -> None:
        scorer = ImportanceScorer()
        result = scorer.score_initial(
            category_base=0.20,
            llm_importance=0.10,
            novelty=0.1,
            explicit_signal=True,
        )
        assert result >= 0.85

    def test_all_zero_returns_floor(self) -> None:
        scorer = ImportanceScorer()
        result = scorer.score_initial(0.0, 0.0, 0.0, 0.0, explicit_signal=False)
        assert result == pytest.approx(0.05)

    def test_all_max_returns_ceiling(self) -> None:
        scorer = ImportanceScorer()
        result = scorer.score_initial(1.0, 1.0, 1.0, 1.0, explicit_signal=False)
        # Sum of weights * 1.0 = 0.15+0.35+0.10+0.15+0 = 0.75 (no explicit)
        assert result <= 1.0
        assert result >= 0.7

    def test_negative_valence_uses_absolute(self) -> None:
        scorer = ImportanceScorer()
        pos = scorer.score_initial(0.5, 0.5, 0.8, 0.5)
        neg = scorer.score_initial(0.5, 0.5, -0.8, 0.5)
        assert pos == pytest.approx(neg)

    def test_entity_vs_fact_importance(self) -> None:
        """Entity category (0.80) should produce higher importance than fact (0.60)."""
        scorer = ImportanceScorer()
        entity = scorer.score_initial(0.80)
        fact = scorer.score_initial(0.60)
        assert entity > fact


class TestImportanceScorerAccessBoost:
    """score_access_boost() — diminishing returns."""

    def test_first_access_gives_boost(self) -> None:
        scorer = ImportanceScorer()
        result = scorer.score_access_boost(0.5, access_count=1)
        assert result > 0.5

    def test_diminishing_returns(self) -> None:
        scorer = ImportanceScorer()
        boost_1 = scorer.score_access_boost(0.5, 1) - 0.5
        boost_100 = scorer.score_access_boost(0.5, 100) - 0.5
        assert boost_1 > boost_100 * 2  # Much larger early boost

    def test_never_exceeds_one(self) -> None:
        scorer = ImportanceScorer()
        result = scorer.score_access_boost(0.99, 1)
        assert result <= 1.0


class TestImportanceScorerConnectivity:
    """score_connectivity() — degree centrality normalization."""

    def test_zero_max_degree_returns_zero(self) -> None:
        scorer = ImportanceScorer()
        assert scorer.score_connectivity(5, 0.5, 0) == 0.0

    def test_max_connectivity(self) -> None:
        scorer = ImportanceScorer()
        result = scorer.score_connectivity(100, 1.0, 100)
        assert result == pytest.approx(1.0)

    def test_bounded(self) -> None:
        scorer = ImportanceScorer()
        result = scorer.score_connectivity(50, 0.5, 100)
        assert 0.0 <= result <= 1.0


class TestImportanceScorerRecency:
    """score_recency() — exponential decay."""

    def test_just_accessed_near_one(self) -> None:
        scorer = ImportanceScorer()
        assert scorer.score_recency(0.0) == pytest.approx(1.0)

    def test_30_days_half_decay(self) -> None:
        scorer = ImportanceScorer()
        result = scorer.score_recency(30.0)
        assert result == pytest.approx(0.3679, abs=0.01)

    def test_90_days_very_low(self) -> None:
        scorer = ImportanceScorer()
        assert scorer.score_recency(90.0) < 0.10

    def test_negative_days_treated_as_zero(self) -> None:
        scorer = ImportanceScorer()
        assert scorer.score_recency(-5.0) == pytest.approx(1.0)


class TestImportanceScorerRecalculate:
    """recalculate() — full consolidation pipeline."""

    def test_stable_concept_stays_similar(self) -> None:
        """Well-used concept with good connectivity → stays near current."""
        scorer = ImportanceScorer()
        result = scorer.recalculate(
            current_importance=0.7,
            access_count=20,
            degree=5,
            avg_weight=0.6,
            max_degree=10,
            emotional_valence=0.3,
            days_since_access=2.0,
            max_access=50,
        )
        # Momentum (0.50 weight) keeps it close to 0.7
        assert abs(result - 0.7) < 0.15

    def test_velocity_damping(self) -> None:
        """Large computed change → clamped to ±0.10 per cycle."""
        scorer = ImportanceScorer()
        result = scorer.recalculate(
            current_importance=0.3,
            access_count=1000,
            degree=100,
            avg_weight=1.0,
            max_degree=100,
            emotional_valence=1.0,
            days_since_access=0.0,
            max_access=1000,
        )
        # Despite extreme inputs, change capped at +0.10
        assert result <= 0.3 + 0.10 + 0.01  # small tolerance

    def test_soft_ceiling_above_090(self) -> None:
        """Concepts above 0.90 face increasing resistance."""
        scorer = ImportanceScorer()
        result = scorer.recalculate(
            current_importance=0.95,
            access_count=100,
            degree=50,
            avg_weight=1.0,
            max_degree=50,
            emotional_valence=1.0,
            days_since_access=0.0,
            max_access=100,
        )
        assert result < 1.0
        assert result <= 0.97

    def test_floor_recovery(self) -> None:
        """Recently accessed floor concept gets lifted to 0.15."""
        scorer = ImportanceScorer()
        result = scorer.recalculate(
            current_importance=0.05,
            access_count=1,
            degree=0,
            avg_weight=0.0,
            max_degree=10,
            emotional_valence=0.0,
            days_since_access=1.0,  # Very recent
            max_access=100,
        )
        assert result >= 0.15

    def test_no_floor_recovery_if_stale(self) -> None:
        """Floor concept without recent access stays at floor."""
        scorer = ImportanceScorer()
        result = scorer.recalculate(
            current_importance=0.05,
            access_count=0,
            degree=0,
            avg_weight=0.0,
            max_degree=10,
            emotional_valence=0.0,
            days_since_access=30.0,  # Not recent
            max_access=100,
        )
        assert result < 0.15

    def test_always_above_floor(self) -> None:
        scorer = ImportanceScorer()
        result = scorer.recalculate(
            current_importance=0.05,
            access_count=0,
            degree=0,
            avg_weight=0.0,
            max_degree=0,
            emotional_valence=0.0,
            days_since_access=365.0,
            max_access=0,
        )
        assert result >= 0.05


# ── ConfidenceScorer ───────────────────────────────────────────────────


class TestConfidenceScorerInitial:
    """score_initial() — creation-time confidence."""

    def test_llm_explicit_high(self) -> None:
        scorer = ConfidenceScorer()
        result = scorer.score_initial(
            source_quality="llm_explicit",
            llm_confidence=0.9,
            is_explicit=True,
            content_length=80,
        )
        assert result > 0.7

    def test_regex_fallback_lower(self) -> None:
        scorer = ConfidenceScorer()
        llm = scorer.score_initial("llm_explicit", 0.8, True, 50)
        regex = scorer.score_initial("regex_fallback", 0.5, False, 20)
        assert llm > regex

    def test_inferred_vs_explicit(self) -> None:
        scorer = ConfidenceScorer()
        explicit = scorer.score_initial("llm_explicit", 0.7, True, 50)
        inferred = scorer.score_initial("llm_explicit", 0.7, False, 50)
        assert explicit > inferred

    def test_rich_content_slight_boost(self) -> None:
        scorer = ConfidenceScorer()
        short = scorer.score_initial("llm_explicit", 0.7, True, 10)
        long = scorer.score_initial("llm_explicit", 0.7, True, 200)
        assert long > short

    def test_system_source_highest(self) -> None:
        scorer = ConfidenceScorer()
        system = scorer.score_initial("system", 0.9, True, 50)
        llm = scorer.score_initial("llm_explicit", 0.9, True, 50)
        assert system >= llm


class TestConfidenceScorerSourceConfidence:
    """get_source_confidence() mapping."""

    def test_known_sources(self) -> None:
        scorer = ConfidenceScorer()
        assert scorer.get_source_confidence("llm_explicit") == pytest.approx(0.85)
        assert scorer.get_source_confidence("regex_fallback") == pytest.approx(0.425)

    def test_unknown_source_default(self) -> None:
        scorer = ConfidenceScorer()
        result = scorer.get_source_confidence("unknown")
        assert 0.40 <= result <= 0.60


class TestConfidenceScorerCorroboration:
    """score_corroboration() — diminishing returns."""

    def test_boost_from_low(self) -> None:
        scorer = ConfidenceScorer()
        result = scorer.score_corroboration(0.3, 1)
        assert result > 0.3

    def test_diminishing_at_high(self) -> None:
        scorer = ConfidenceScorer()
        boost_low = scorer.score_corroboration(0.3, 1) - 0.3
        boost_high = scorer.score_corroboration(0.9, 5) - 0.9
        assert boost_low > boost_high

    def test_never_exceeds_one(self) -> None:
        scorer = ConfidenceScorer()
        result = scorer.score_corroboration(0.99, 10)
        assert result <= 1.0


class TestConfidenceScorerStaleness:
    """score_staleness_decay() — gentle time decay."""

    def test_no_decay_when_fresh(self) -> None:
        scorer = ConfidenceScorer()
        result = scorer.score_staleness_decay(0.8, 0.0)
        assert result == pytest.approx(0.8)

    def test_small_decay_90_days(self) -> None:
        scorer = ConfidenceScorer()
        result = scorer.score_staleness_decay(0.8, 90.0)
        # ~2% decay: 0.8 * 0.98 = 0.784
        assert 0.77 < result < 0.80

    def test_decay_capped_at_15_percent(self) -> None:
        scorer = ConfidenceScorer()
        result = scorer.score_staleness_decay(0.8, 1000.0)
        # Max 15% decay: 0.8 * 0.85 = 0.68
        assert result >= 0.68 - 0.01

    def test_never_below_floor(self) -> None:
        scorer = ConfidenceScorer()
        result = scorer.score_staleness_decay(0.06, 1000.0)
        assert result >= 0.05


class TestConfidenceScorerContradiction:
    """score_contradiction() — 40% reduction."""

    def test_high_confidence_reduced(self) -> None:
        scorer = ConfidenceScorer()
        result = scorer.score_contradiction(0.9)
        assert result == pytest.approx(0.54)

    def test_low_confidence_floor(self) -> None:
        scorer = ConfidenceScorer()
        result = scorer.score_contradiction(0.1)
        assert result >= 0.10

    def test_moderate_confidence(self) -> None:
        scorer = ConfidenceScorer()
        result = scorer.score_contradiction(0.5)
        assert result == pytest.approx(0.30)


class TestConfidenceScorerContentUpdate:
    """score_content_update() — small bump."""

    def test_content_grew_boost(self) -> None:
        scorer = ConfidenceScorer()
        result = scorer.score_content_update(0.6, content_grew=True)
        assert result == pytest.approx(0.63)

    def test_no_growth_no_change(self) -> None:
        scorer = ConfidenceScorer()
        result = scorer.score_content_update(0.6, content_grew=False)
        assert result == pytest.approx(0.6)


# ── ScoreNormalizer ────────────────────────────────────────────────────


class TestScoreNormalizer:
    """Score normalization for spread health."""

    def test_no_op_when_healthy(self) -> None:
        normalizer = ScoreNormalizer()
        scores = [("a", 0.2), ("b", 0.5), ("c", 0.8)]
        result = normalizer.normalize(scores)
        # Spread = 0.6 > 0.20 → no-op
        assert result == scores

    def test_stretches_narrow_spread(self) -> None:
        normalizer = ScoreNormalizer()
        scores = [("a", 0.50), ("b", 0.51), ("c", 0.52)]
        result = normalizer.normalize(scores)
        values = [s for _, s in result]
        spread = max(values) - min(values)
        assert spread > 0.5  # Much wider than 0.02

    def test_preserves_order(self) -> None:
        normalizer = ScoreNormalizer()
        scores = [("a", 0.50), ("b", 0.51), ("c", 0.52)]
        result = normalizer.normalize(scores)
        result_dict = dict(result)
        assert result_dict["a"] < result_dict["b"] < result_dict["c"]

    def test_all_identical_distributes_evenly(self) -> None:
        normalizer = ScoreNormalizer()
        scores = [("a", 0.5), ("b", 0.5), ("c", 0.5)]
        result = normalizer.normalize(scores)
        values = [s for _, s in result]
        assert len(set(round(v, 4) for v in values)) == 3  # All different

    def test_never_below_floor(self) -> None:
        normalizer = ScoreNormalizer()
        scores = [("a", 0.001), ("b", 0.002), ("c", 0.003)]
        result = normalizer.normalize(scores)
        for _, val in result:
            assert val >= 0.05

    def test_less_than_3_no_op(self) -> None:
        normalizer = ScoreNormalizer()
        scores = [("a", 0.5), ("b", 0.5)]
        result = normalizer.normalize(scores)
        assert result == scores

    def test_normalize_by_category(self) -> None:
        normalizer = ScoreNormalizer()
        concepts = [
            ("a", "entity", 0.50),
            ("b", "entity", 0.51),
            ("c", "entity", 0.52),
            ("d", "fact", 0.50),
            ("e", "fact", 0.51),
            ("f", "fact", 0.52),
        ]
        result = normalizer.normalize_by_category(concepts)
        assert len(result) == 6


# ── Property-based tests (Hypothesis) ─────────────────────────────────


class TestImportanceProperties:
    """Property-based tests for ImportanceScorer."""

    @given(
        category_base=st.floats(0.0, 1.0),
        llm_importance=st.floats(0.0, 1.0),
        emotional=st.floats(-1.0, 1.0),
        novelty=st.floats(0.0, 1.0),
        explicit=st.booleans(),
    )
    @settings(max_examples=200)
    def test_score_initial_always_bounded(
        self,
        category_base: float,
        llm_importance: float,
        emotional: float,
        novelty: float,
        explicit: bool,
    ) -> None:
        scorer = ImportanceScorer()
        result = scorer.score_initial(
            category_base, llm_importance, emotional, novelty, explicit
        )
        assert 0.05 <= result <= 1.0

    @given(
        category_base=st.floats(0.0, 1.0),
        llm_importance=st.floats(0.0, 1.0),
        novelty=st.floats(0.0, 1.0),
    )
    @settings(max_examples=100)
    def test_explicit_always_above_085(
        self,
        category_base: float,
        llm_importance: float,
        novelty: float,
    ) -> None:
        scorer = ImportanceScorer()
        result = scorer.score_initial(
            category_base, llm_importance, 0.0, novelty, explicit_signal=True
        )
        assert result >= 0.85

    @given(
        current=st.floats(0.05, 1.0),
        access=st.integers(0, 10000),
        degree=st.integers(0, 500),
        avg_w=st.floats(0.0, 1.0),
        max_deg=st.integers(0, 500),
        valence=st.floats(-1.0, 1.0),
        days=st.floats(0.0, 1000.0),
        max_acc=st.integers(1, 10000),
    )
    @settings(max_examples=200)
    def test_recalculate_always_bounded(
        self,
        current: float,
        access: int,
        degree: int,
        avg_w: float,
        max_deg: int,
        valence: float,
        days: float,
        max_acc: int,
    ) -> None:
        scorer = ImportanceScorer()
        result = scorer.recalculate(
            current, access, degree, avg_w, max_deg, valence, days, max_acc
        )
        assert 0.05 <= result <= 1.0

    @given(
        current=st.floats(0.05, 0.95),
        access=st.integers(0, 1000),
    )
    @settings(max_examples=100)
    def test_access_boost_monotonic(self, current: float, access: int) -> None:
        """Access boost never decreases importance."""
        scorer = ImportanceScorer()
        result = scorer.score_access_boost(current, access)
        assert result >= current


class TestConfidenceProperties:
    """Property-based tests for ConfidenceScorer."""

    @given(
        source=st.sampled_from(
            ["llm_explicit", "llm_inferred", "regex_fallback", "system", "unknown"]
        ),
        llm_conf=st.floats(0.0, 1.0),
        explicit=st.booleans(),
        content_len=st.integers(0, 500),
    )
    @settings(max_examples=200)
    def test_score_initial_always_bounded(
        self,
        source: str,
        llm_conf: float,
        explicit: bool,
        content_len: int,
    ) -> None:
        scorer = ConfidenceScorer()
        result = scorer.score_initial(source, llm_conf, explicit, content_len)
        assert 0.05 <= result <= 1.0

    @given(
        current=st.floats(0.05, 1.0),
        days=st.floats(0.0, 2000.0),
    )
    @settings(max_examples=100)
    def test_staleness_never_below_floor(self, current: float, days: float) -> None:
        scorer = ConfidenceScorer()
        result = scorer.score_staleness_decay(current, days)
        assert result >= 0.05

    @given(current=st.floats(0.05, 1.0))
    @settings(max_examples=50)
    def test_contradiction_never_below_floor(self, current: float) -> None:
        scorer = ConfidenceScorer()
        result = scorer.score_contradiction(current)
        assert result >= 0.10


class TestNormalizerProperties:
    """Property-based tests for ScoreNormalizer."""

    @given(
        values=st.lists(
            st.floats(0.05, 1.0),
            min_size=3,
            max_size=50,
        ),
    )
    @settings(max_examples=100)
    def test_preserves_relative_order(self, values: list[float]) -> None:
        """Normalization always preserves relative ordering."""
        normalizer = ScoreNormalizer()
        ids = [str(i) for i in range(len(values))]
        scores = list(zip(ids, values, strict=True))

        # Sort originals by value
        sorted_orig = sorted(scores, key=lambda x: x[1])

        result = normalizer.normalize(scores)
        result_dict = dict(result)

        # Check pairwise ordering
        for i in range(len(sorted_orig) - 1):
            id_a = sorted_orig[i][0]
            id_b = sorted_orig[i + 1][0]
            val_a_orig = sorted_orig[i][1]
            val_b_orig = sorted_orig[i + 1][1]
            # Only check strict ordering if originals were strictly ordered
            if val_a_orig < val_b_orig:
                assert result_dict[id_a] <= result_dict[id_b]


class TestStabilityProperties:
    """End-to-end stability properties for dynamic scoring (TASK-17).

    Validates that the scoring system is well-behaved under arbitrary inputs:
    - Bounded: all outputs in [floor, 1.0]
    - Idempotent: double-recalculation ≈ single
    - Monotonic: more signals → higher scores
    - Convergent: repeated application doesn't diverge
    """

    @given(
        current=st.floats(0.05, 1.0),
        access_count=st.integers(0, 1000),
        degree=st.integers(0, 100),
        avg_weight=st.floats(0.0, 1.0),
        max_degree=st.integers(1, 100),
        emotional=st.floats(-1.0, 1.0),
        days=st.floats(0.0, 365.0),
        max_access=st.integers(1, 1000),
    )
    @settings(max_examples=200)
    def test_recalculate_always_bounded(
        self,
        current: float,
        access_count: int,
        degree: int,
        avg_weight: float,
        max_degree: int,
        emotional: float,
        days: float,
        max_access: int,
    ) -> None:
        """Recalculation output always in [0.05, 1.0]."""
        scorer = ImportanceScorer()
        result = scorer.recalculate(
            current_importance=current,
            access_count=access_count,
            degree=min(degree, max_degree),
            avg_weight=avg_weight,
            max_degree=max_degree,
            emotional_valence=emotional,
            days_since_access=days,
            max_access=max_access,
        )
        assert 0.05 <= result <= 1.0

    @given(
        current=st.floats(0.05, 1.0),
        access_count=st.integers(0, 100),
        degree=st.integers(0, 20),
    )
    @settings(max_examples=100)
    def test_recalculate_near_idempotent(
        self,
        current: float,
        access_count: int,
        degree: int,
    ) -> None:
        """Double recalculation converges (2nd pass ≈ 1st pass within 0.10)."""
        scorer = ImportanceScorer()
        kwargs = dict(
            access_count=access_count,
            degree=degree,
            avg_weight=0.5,
            max_degree=max(20, degree),
            emotional_valence=0.0,
            days_since_access=1.0,
            max_access=max(100, access_count),
        )
        first = scorer.recalculate(current_importance=current, **kwargs)
        second = scorer.recalculate(current_importance=first, **kwargs)
        # Velocity damping means each pass moves ≤ 0.10
        assert abs(second - first) <= 0.11

    @given(
        low_access=st.integers(0, 10),
        high_access=st.integers(50, 200),
    )
    @settings(max_examples=50)
    def test_higher_access_higher_importance(
        self,
        low_access: int,
        high_access: int,
    ) -> None:
        """More access → higher (or equal) importance, all else equal."""
        scorer = ImportanceScorer()
        kwargs = dict(
            current_importance=0.5,
            degree=5,
            avg_weight=0.5,
            max_degree=20,
            emotional_valence=0.0,
            days_since_access=1.0,
        )
        low = scorer.recalculate(access_count=low_access, max_access=high_access, **kwargs)
        high = scorer.recalculate(access_count=high_access, max_access=high_access, **kwargs)
        assert high >= low

    @given(
        confidence=st.floats(0.10, 1.0),
        n_cycles=st.integers(1, 20),
    )
    @settings(max_examples=50)
    def test_staleness_monotonically_decreasing(
        self,
        confidence: float,
        n_cycles: int,
    ) -> None:
        """Repeated staleness decay always decreases (or stays at floor)."""
        scorer = ConfidenceScorer()
        current = confidence
        for _ in range(n_cycles):
            next_val = scorer.score_staleness_decay(current, days_since_access=90.0)
            assert next_val <= current + 0.001  # Allow tiny float rounding
            assert next_val >= 0.05
            current = next_val

    @given(
        importance=st.floats(0.05, 1.0),
        boost_count=st.integers(1, 10),
    )
    @settings(max_examples=50)
    def test_access_boost_diminishing(
        self,
        importance: float,
        boost_count: int,
    ) -> None:
        """Each access boost is smaller than the previous."""
        scorer = ImportanceScorer()
        current = importance
        deltas: list[float] = []
        for i in range(boost_count):
            boosted = scorer.score_access_boost(current, access_count=i + 1)
            delta = boosted - current
            deltas.append(delta)
            current = boosted
        # Deltas should be non-increasing (diminishing returns)
        for i in range(len(deltas) - 1):
            assert deltas[i + 1] <= deltas[i] + 0.001
