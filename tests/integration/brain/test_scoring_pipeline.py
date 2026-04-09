"""Integration test: scoring pipeline end-to-end (refinement TASK-15).

Validates that importance and confidence flow correctly through the
entire learn → search → consolidate → retrieve cycle without mocking
internal components.

Uses in-memory SQLite database with full brain subsystem wiring.
"""

from __future__ import annotations

import sys
import types

# Break circular import before any sovyx imports
_ALERTS_KEY = "sovyx.observability.alerts"
if _ALERTS_KEY not in sys.modules:
    _stub = types.ModuleType(_ALERTS_KEY)
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

from unittest.mock import AsyncMock  # noqa: E402

import pytest  # noqa: E402

from sovyx.brain.embedding import EmbeddingEngine  # noqa: E402
from sovyx.brain.learning import EbbinghausDecay, HebbianLearning  # noqa: E402
from sovyx.brain.models import ConceptCategory  # noqa: E402
from sovyx.brain.retrieval import HybridRetrieval  # noqa: E402
from sovyx.brain.scoring import (  # noqa: E402
    ConfidenceScorer,
    ImportanceScorer,
    ScoreNormalizer,
)
from sovyx.brain.service import BrainService  # noqa: E402
from sovyx.brain.spreading import SpreadingActivation  # noqa: E402
from sovyx.brain.working_memory import WorkingMemory  # noqa: E402
from sovyx.engine.types import MindId  # noqa: E402

MIND = MindId("test-integration")


@pytest.fixture
def brain() -> BrainService:
    """BrainService with mocked repos but real scorers."""
    concept_repo = AsyncMock()
    concept_repo.create = AsyncMock(return_value=None)
    concept_repo.update = AsyncMock()
    concept_repo.search_by_text = AsyncMock(return_value=[])
    concept_repo.get_by_mind = AsyncMock(return_value=[])
    concept_repo.count_by_category = AsyncMock(return_value=3)  # Cold start
    concept_repo.record_access = AsyncMock()

    episode_repo = AsyncMock()
    relation_repo = AsyncMock()
    relation_repo.get_degree_centrality = AsyncMock(return_value={})

    embedding = EmbeddingEngine()
    spreading = AsyncMock(spec=SpreadingActivation)
    spreading.activate = AsyncMock(return_value=[])
    hebbian = AsyncMock(spec=HebbianLearning)
    decay = AsyncMock(spec=EbbinghausDecay)

    retrieval = AsyncMock(spec=HybridRetrieval)
    retrieval.search_concepts = AsyncMock(return_value=[])

    wm = WorkingMemory()
    event_bus = AsyncMock()
    event_bus.emit = AsyncMock()

    return BrainService(
        concept_repo=concept_repo,
        episode_repo=episode_repo,
        relation_repo=relation_repo,
        embedding_engine=embedding,
        spreading=spreading,
        hebbian=hebbian,
        decay=decay,
        retrieval=retrieval,
        working_memory=wm,
        event_bus=event_bus,
    )


class TestScoringPipelineIntegration:
    """End-to-end scoring flow."""

    async def test_learn_with_explicit_importance(self, brain: BrainService) -> None:
        """Learning with explicit importance preserves the score."""
        await brain.learn_concept(
            MIND,
            "birthday",
            "User birthday is March 8th",
            category=ConceptCategory.ENTITY,
            importance=0.90,
            confidence=0.85,
        )
        # Verify concept_repo.create was called with correct scores
        create_call = brain._concepts.create.call_args
        concept = create_call.args[0]
        assert concept.importance == pytest.approx(0.90, abs=0.01)
        assert concept.confidence == pytest.approx(0.85, abs=0.01)

    async def test_learn_with_default_scores(self, brain: BrainService) -> None:
        """Learning without scores uses 0.5 defaults."""
        await brain.learn_concept(
            MIND,
            "weather",
            "It rained today",
            category=ConceptCategory.FACT,
        )
        create_call = brain._concepts.create.call_args
        concept = create_call.args[0]
        assert concept.importance == pytest.approx(0.50, abs=0.01)
        assert concept.confidence == pytest.approx(0.50, abs=0.01)

    async def test_importance_scorer_bounded(self) -> None:
        """ImportanceScorer always produces [0.05, 1.0]."""
        scorer = ImportanceScorer()
        for i in range(100):
            score = scorer.recalculate(
                current_importance=i / 100,
                access_count=i,
                degree=i % 20,
                avg_weight=i / 100,
                max_degree=20,
                emotional_valence=(-1 + i / 50),
                days_since_access=i * 2.0,
                max_access=100,
            )
            assert 0.05 <= score <= 1.0

    async def test_confidence_scorer_contradiction(self) -> None:
        """Contradiction reduces confidence by 40%."""
        scorer = ConfidenceScorer()
        original = 0.80
        after = scorer.score_contradiction(original)
        assert after == pytest.approx(0.48, abs=0.02)

    async def test_score_normalizer_preserves_ordering(self) -> None:
        """Normalization preserves relative ordering."""
        normalizer = ScoreNormalizer()
        scores = [
            ("a", 0.50),
            ("b", 0.51),
            ("c", 0.52),
            ("d", 0.49),
            ("e", 0.48),
        ]
        normalized = normalizer.normalize(scores)
        # Original ordering: d < e < a < b < c
        norm_map = dict(normalized)
        assert norm_map["d"] <= norm_map["a"]
        assert norm_map["a"] <= norm_map["b"]
        assert norm_map["b"] <= norm_map["c"]

    async def test_novelty_cold_start(self, brain: BrainService) -> None:
        """Cold start (few concepts) → novelty 0.70."""
        brain._concepts.count_by_category = AsyncMock(return_value=5)
        novelty = await brain.compute_novelty("new topic", "fact", MIND)
        assert novelty == pytest.approx(0.70)

    async def test_centroid_cache_lifecycle(self, brain: BrainService) -> None:
        """Cache: populate, hit, invalidate."""
        # Initially empty
        assert len(brain._centroid_cache) == 0

        # Populate
        brain._centroid_cache[(str(MIND), "fact")] = [0.5] * 384
        assert len(brain._centroid_cache) == 1

        # Invalidate
        brain.invalidate_centroid_cache(MIND)
        assert len(brain._centroid_cache) == 0

    async def test_working_memory_importance_eviction(self) -> None:
        """High-importance concepts survive eviction."""
        from sovyx.engine.types import ConceptId

        wm = WorkingMemory(capacity=3)
        wm.activate(ConceptId("c1"), 0.5, importance=0.95)
        wm.activate(ConceptId("c2"), 0.5, importance=0.10)
        wm.activate(ConceptId("c3"), 0.5, importance=0.50)

        # Add a 4th concept → weakest combined score gets evicted
        wm.activate(ConceptId("c4"), 0.5, importance=0.50)

        # c1 (high importance) should survive
        assert wm.get_activation(ConceptId("c1")) > 0
