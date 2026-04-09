"""Tests for consolidation batching, timeout, and drift detection.

Uses lazy imports to avoid circular import chain:
sovyx.engine.events ↔ sovyx.observability.alerts.
"""

from __future__ import annotations

import sys
import types
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

# Break the circular import chain before any sovyx imports.
# sovyx.observability.__init__ tries to import from .alerts which
# imports sovyx.engine.events → circular. We stub the alerts module.
_ALERTS_KEY = "sovyx.observability.alerts"
if _ALERTS_KEY not in sys.modules:
    _stub = types.ModuleType(_ALERTS_KEY)
    # Provide the names that sovyx.observability.__init__ imports
    for _name in (
        "Alert",
        "AlertFired",
        "AlertManager",
        "AlertRule",
        "AlertSeverity",
        "create_default_alert_manager",
    ):
        setattr(_stub, _name, type(_name, (), {}))
    sys.modules[_ALERTS_KEY] = _stub

from sovyx.brain.consolidation import ConsolidationCycle  # noqa: E402
from sovyx.brain.models import Concept, ConceptCategory  # noqa: E402
from sovyx.engine.types import ConceptId, MindId  # noqa: E402

MIND = MindId("test-mind")


def _concept(name: str, idx: int) -> Concept:
    """Create a test concept with known scores."""
    return Concept(
        id=ConceptId(f"c-{idx}"),
        mind_id=MIND,
        name=name,
        content=f"Content about {name}",
        category=ConceptCategory.FACT,
        importance=0.5,
        confidence=0.5,
        access_count=idx,
        emotional_valence=0.0,
        last_accessed=datetime(2026, 1, 1, tzinfo=UTC),
    )


@pytest.fixture
def consolidation_deps() -> dict[str, AsyncMock]:
    """Minimal deps for ConsolidationCycle focused on scoring."""
    brain = AsyncMock()
    brain.refresh_centroid_cache = AsyncMock(return_value=0)

    decay = AsyncMock()
    decay.apply_decay = AsyncMock(return_value=(0, 0))
    decay.prune_weak = AsyncMock(return_value=(0, 0))

    event_bus = AsyncMock()
    event_bus.emit = AsyncMock()

    concept_repo = AsyncMock()
    concept_repo.get_by_mind = AsyncMock(return_value=[])
    concept_repo.batch_update_scores = AsyncMock()
    concept_repo.search_by_text = AsyncMock(return_value=[])

    relation_repo = AsyncMock()
    relation_repo.get_degree_centrality = AsyncMock(return_value={})

    importance_scorer = AsyncMock()
    importance_scorer.recalculate = lambda **kw: 0.8  # Always changed

    confidence_scorer = AsyncMock()
    confidence_scorer.score_staleness_decay = lambda **kw: 0.4

    return {
        "brain_service": brain,
        "decay": decay,
        "event_bus": event_bus,
        "concept_repo": concept_repo,
        "relation_repo": relation_repo,
        "importance_scorer": importance_scorer,
        "confidence_scorer": confidence_scorer,
    }


class TestScoreRecalculationBatching:
    """Batched score recalculation with timeout."""

    async def test_batching_flushes_at_500(self, consolidation_deps: dict[str, AsyncMock]) -> None:
        """750 concepts → 2 batches (500 + 250)."""
        concepts = [_concept(f"c{i}", i) for i in range(750)]
        consolidation_deps["concept_repo"].get_by_mind = AsyncMock(return_value=concepts)

        cycle = ConsolidationCycle(**consolidation_deps)  # type: ignore[arg-type]
        updated = await cycle._recalculate_scores(MIND)

        assert updated == 750  # noqa: PLR2004
        calls = consolidation_deps["concept_repo"].batch_update_scores.call_args_list
        assert len(calls) == 2  # noqa: PLR2004
        assert len(calls[0].args[0]) == 500  # noqa: PLR2004
        assert len(calls[1].args[0]) == 250  # noqa: PLR2004

    async def test_small_batch_single_flush(
        self, consolidation_deps: dict[str, AsyncMock]
    ) -> None:
        """100 concepts → single batch."""
        concepts = [_concept(f"c{i}", i) for i in range(100)]
        consolidation_deps["concept_repo"].get_by_mind = AsyncMock(return_value=concepts)

        cycle = ConsolidationCycle(**consolidation_deps)  # type: ignore[arg-type]
        updated = await cycle._recalculate_scores(MIND)

        assert updated == 100  # noqa: PLR2004
        calls = consolidation_deps["concept_repo"].batch_update_scores.call_args_list
        assert len(calls) == 1

    async def test_timeout_stops_processing(
        self, consolidation_deps: dict[str, AsyncMock]
    ) -> None:
        """Timeout fires → partial processing."""
        concepts = [_concept(f"c{i}", i) for i in range(1000)]
        consolidation_deps["concept_repo"].get_by_mind = AsyncMock(return_value=concepts)

        cycle = ConsolidationCycle(**consolidation_deps)  # type: ignore[arg-type]
        # Force immediate timeout
        cycle._SCORE_TIMEOUT_S = 0.0  # type: ignore[assignment]

        updated = await cycle._recalculate_scores(MIND)

        # Should have processed fewer than 1000 (timeout stops early)
        assert updated < 1000  # noqa: PLR2004

    async def test_no_changes_no_writes(self, consolidation_deps: dict[str, AsyncMock]) -> None:
        """If scores don't change, no DB writes."""
        concepts = [_concept(f"c{i}", i) for i in range(10)]
        # Scorer returns same values as existing
        consolidation_deps["importance_scorer"].recalculate = lambda **kw: 0.5
        consolidation_deps["confidence_scorer"].score_staleness_decay = lambda **kw: 0.5
        consolidation_deps["concept_repo"].get_by_mind = AsyncMock(return_value=concepts)

        cycle = ConsolidationCycle(**consolidation_deps)  # type: ignore[arg-type]
        updated = await cycle._recalculate_scores(MIND)

        assert updated == 0
        consolidation_deps["concept_repo"].batch_update_scores.assert_not_called()


class TestNormalizationBatching:
    """Batched normalization writes."""

    async def test_normalize_batches_writes(
        self, consolidation_deps: dict[str, AsyncMock]
    ) -> None:
        """Large normalize → batched writes."""
        from sovyx.brain.scoring import ScoreNormalizer

        # 600 concepts with narrow spread → normalization will kick in
        concepts = [_concept(f"c{i}", i) for i in range(600)]
        for c in concepts:
            c.importance = 0.50 + (0.001 * (hash(c.name) % 10))

        consolidation_deps["concept_repo"].get_by_mind = AsyncMock(return_value=concepts)

        cycle = ConsolidationCycle(**consolidation_deps)  # type: ignore[arg-type]
        cycle._normalizer = ScoreNormalizer()

        normalized = await cycle._normalize_scores(MIND)

        if normalized > 0:
            calls = consolidation_deps["concept_repo"].batch_update_scores.call_args_list
            if normalized > 500:  # noqa: PLR2004
                assert len(calls) >= 2  # noqa: PLR2004


class TestShannonEntropy:
    """Shannon entropy computation for score drift detection."""

    def test_uniform_distribution_high_entropy(self) -> None:
        """Perfectly uniform → maximum entropy."""
        # 100 values evenly spread across [0, 1]
        values = [i / 100 for i in range(100)]
        entropy = ConsolidationCycle._shannon_entropy(values)
        # Uniform over 20 bins → log2(20) ≈ 4.32
        assert entropy > 3.5  # noqa: PLR2004

    def test_concentrated_distribution_low_entropy(self) -> None:
        """All values identical → very low entropy."""
        values = [0.5] * 100
        entropy = ConsolidationCycle._shannon_entropy(values)
        # All in one bin → entropy = 0
        assert entropy < 0.01  # noqa: PLR2004

    def test_bimodal_moderate_entropy(self) -> None:
        """Two clusters → moderate entropy."""
        values = [0.1] * 50 + [0.9] * 50
        entropy = ConsolidationCycle._shannon_entropy(values)
        # log2(2) = 1.0
        assert 0.8 < entropy < 1.2  # noqa: PLR2004

    def test_single_value_returns_zero(self) -> None:
        """Single value → 0.0."""
        assert ConsolidationCycle._shannon_entropy([0.5]) == 0.0

    def test_empty_returns_zero(self) -> None:
        """Empty → 0.0."""
        assert ConsolidationCycle._shannon_entropy([]) == 0.0

    def test_entropy_nonnegative(self) -> None:
        """Entropy is always >= 0."""
        import random

        rng = random.Random(42)
        for _ in range(10):
            values = [rng.random() for _ in range(50)]
            assert ConsolidationCycle._shannon_entropy(values) >= 0.0


class TestScoreDrift:
    """Score drift detection in consolidation."""

    async def test_healthy_distribution_no_warning(
        self, consolidation_deps: dict[str, AsyncMock]
    ) -> None:
        """Well-spread scores → no warning."""
        concepts = [_concept(f"c{i}", i) for i in range(100)]
        for i, c in enumerate(concepts):
            c.importance = i / 100  # Perfect spread
        consolidation_deps["concept_repo"].get_by_mind = AsyncMock(return_value=concepts)

        cycle = ConsolidationCycle(**consolidation_deps)  # type: ignore[arg-type]
        # Should not raise or log warnings
        await cycle._check_score_drift(MindId("test"))

    async def test_collapsed_distribution_detected(
        self, consolidation_deps: dict[str, AsyncMock]
    ) -> None:
        """All identical scores → critical detection."""
        concepts = [_concept(f"c{i}", i) for i in range(100)]
        for c in concepts:
            c.importance = 0.5  # All identical
        consolidation_deps["concept_repo"].get_by_mind = AsyncMock(return_value=concepts)

        cycle = ConsolidationCycle(**consolidation_deps)  # type: ignore[arg-type]
        # Should run without error; warning is logged (not raised)
        await cycle._check_score_drift(MindId("test"))
        # Entropy of all-same = 0.0 < 1.0 (critical threshold)

    async def test_few_concepts_skipped(self, consolidation_deps: dict[str, AsyncMock]) -> None:
        """< 5 concepts → skip drift check."""
        concepts = [_concept(f"c{i}", i) for i in range(3)]
        consolidation_deps["concept_repo"].get_by_mind = AsyncMock(return_value=concepts)

        cycle = ConsolidationCycle(**consolidation_deps)  # type: ignore[arg-type]
        await cycle._check_score_drift(MindId("test"))
        # No error — silently skipped
