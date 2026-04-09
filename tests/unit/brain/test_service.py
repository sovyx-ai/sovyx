"""Tests for sovyx.brain.service — BrainService unified API."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from sovyx.brain.models import Concept, ConceptCategory, Episode
from sovyx.brain.service import BrainService
from sovyx.brain.working_memory import WorkingMemory
from sovyx.engine.events import ConceptCreated, EpisodeEncoded
from sovyx.engine.types import (
    ConceptId,
    ConversationId,
    EpisodeId,
    MindId,
)

MIND = MindId("aria")


def _concept(name: str, cid: str = "") -> Concept:
    return Concept(
        id=ConceptId(cid or name),
        mind_id=MIND,
        name=name,
        content=f"Content about {name}",
        importance=0.7,
    )


def _episode(user: str, eid: str = "") -> Episode:
    return Episode(
        id=EpisodeId(eid or user),
        mind_id=MIND,
        conversation_id=ConversationId("conv1"),
        user_input=user,
        assistant_response="response",
    )


@pytest.fixture
def mock_deps() -> dict[str, AsyncMock | WorkingMemory]:
    """All BrainService dependencies as mocks."""
    concept_repo = AsyncMock()
    concept_repo.get_recent = AsyncMock(return_value=[])
    concept_repo.search_by_text = AsyncMock(return_value=[])
    concept_repo.create = AsyncMock(return_value=ConceptId("new-id"))
    concept_repo.get = AsyncMock(return_value=None)
    concept_repo.update = AsyncMock()
    concept_repo.record_access = AsyncMock()

    episode_repo = AsyncMock()
    episode_repo.create = AsyncMock(return_value=EpisodeId("ep-id"))

    relation_repo = AsyncMock()
    relation_repo.get_neighbors = AsyncMock(return_value=[])

    embedding_engine = AsyncMock()
    embedding_engine.has_embeddings = False

    spreading = AsyncMock()
    spreading.activate = AsyncMock(return_value=[])

    hebbian = AsyncMock()
    hebbian.strengthen = AsyncMock(return_value=0)

    decay = AsyncMock()

    retrieval = AsyncMock()
    retrieval.search_concepts = AsyncMock(return_value=[])
    retrieval.search_episodes = AsyncMock(return_value=[])

    wm = WorkingMemory()

    event_bus = AsyncMock()
    event_bus.emit = AsyncMock()

    return {
        "concept_repo": concept_repo,
        "episode_repo": episode_repo,
        "relation_repo": relation_repo,
        "embedding_engine": embedding_engine,
        "spreading": spreading,
        "hebbian": hebbian,
        "decay": decay,
        "retrieval": retrieval,
        "working_memory": wm,
        "event_bus": event_bus,
    }


@pytest.fixture
def brain(mock_deps: dict[str, AsyncMock | WorkingMemory]) -> BrainService:
    return BrainService(**mock_deps)  # type: ignore[arg-type]


class TestLifecycle:
    """start/stop lifecycle."""

    async def test_start_loads_recent_concepts(
        self, brain: BrainService, mock_deps: dict[str, AsyncMock | WorkingMemory]
    ) -> None:
        c1 = _concept("test", "c1")
        mock_deps["concept_repo"].get_recent = AsyncMock(return_value=[c1])  # type: ignore[union-attr]

        await brain.start(MIND)
        wm = mock_deps["working_memory"]
        assert isinstance(wm, WorkingMemory)
        assert wm.get_activation(ConceptId("c1")) > 0

    async def test_stop_clears_memory(
        self, brain: BrainService, mock_deps: dict[str, AsyncMock | WorkingMemory]
    ) -> None:
        wm = mock_deps["working_memory"]
        assert isinstance(wm, WorkingMemory)
        wm.activate(ConceptId("c1"))
        await brain.stop()
        assert wm.size == 0


class TestSearch:
    """search() — hybrid + spreading + access tracking."""

    async def test_search_returns_results(
        self, brain: BrainService, mock_deps: dict[str, AsyncMock | WorkingMemory]
    ) -> None:
        c1 = _concept("quantum", "c1")
        mock_deps["retrieval"].search_concepts = AsyncMock(  # type: ignore[union-attr]
            return_value=[(c1, 0.5)]
        )
        mock_deps["spreading"].activate = AsyncMock(  # type: ignore[union-attr]
            return_value=[(ConceptId("c1"), 0.8)]
        )

        results = await brain.search("quantum", MIND)
        assert len(results) == 1
        assert results[0][0].name == "quantum"

    async def test_search_empty(self, brain: BrainService) -> None:
        results = await brain.search("nothing", MIND)
        assert results == []

    async def test_search_calls_access_tracking(
        self, brain: BrainService, mock_deps: dict[str, AsyncMock | WorkingMemory]
    ) -> None:
        c1 = _concept("test", "c1")
        mock_deps["retrieval"].search_concepts = AsyncMock(  # type: ignore[union-attr]
            return_value=[(c1, 0.5)]
        )
        mock_deps["spreading"].activate = AsyncMock(  # type: ignore[union-attr]
            return_value=[(ConceptId("c1"), 0.5)]
        )

        await brain.search("test", MIND)
        # Give fire-and-forget a chance
        await asyncio.sleep(0.05)

        mock_deps["concept_repo"].record_access.assert_called()  # type: ignore[union-attr]

    async def test_access_tracking_failure_no_crash(
        self, brain: BrainService, mock_deps: dict[str, AsyncMock | WorkingMemory]
    ) -> None:
        c1 = _concept("test", "c1")
        mock_deps["retrieval"].search_concepts = AsyncMock(  # type: ignore[union-attr]
            return_value=[(c1, 0.5)]
        )
        mock_deps["spreading"].activate = AsyncMock(  # type: ignore[union-attr]
            return_value=[(ConceptId("c1"), 0.5)]
        )
        mock_deps["concept_repo"].record_access = AsyncMock(  # type: ignore[union-attr]
            side_effect=RuntimeError("DB error")
        )

        # Should not crash
        results = await brain.search("test", MIND)
        assert len(results) == 1
        await asyncio.sleep(0.05)


class TestRecall:
    """recall() — concepts + episodes."""

    async def test_recall_returns_both(
        self, brain: BrainService, mock_deps: dict[str, AsyncMock | WorkingMemory]
    ) -> None:
        c1 = _concept("memory", "c1")
        e1 = _episode("hello", "e1")
        mock_deps["retrieval"].search_concepts = AsyncMock(  # type: ignore[union-attr]
            return_value=[(c1, 0.5)]
        )
        mock_deps["retrieval"].search_episodes = AsyncMock(  # type: ignore[union-attr]
            return_value=[(e1, 0.3)]
        )
        mock_deps["spreading"].activate = AsyncMock(  # type: ignore[union-attr]
            return_value=[(ConceptId("c1"), 0.5)]
        )

        concepts, episodes = await brain.recall("memory", MIND)
        assert len(concepts) == 1
        assert len(episodes) == 1
        assert concepts[0][0].name == "memory"
        assert episodes[0].user_input == "hello"


class TestLearnConcept:
    """learn_concept() — create + dedup."""

    async def test_learn_new_concept(
        self, brain: BrainService, mock_deps: dict[str, AsyncMock | WorkingMemory]
    ) -> None:
        cid = await brain.learn_concept(MIND, "Python", "A programming language")
        assert cid == ConceptId("new-id")
        mock_deps["concept_repo"].create.assert_called_once()  # type: ignore[union-attr]

    async def test_learn_emits_event(
        self, brain: BrainService, mock_deps: dict[str, AsyncMock | WorkingMemory]
    ) -> None:
        await brain.learn_concept(MIND, "Python", "A language")
        mock_deps["event_bus"].emit.assert_called_once()  # type: ignore[union-attr]
        event = mock_deps["event_bus"].emit.call_args[0][0]  # type: ignore[union-attr]
        assert isinstance(event, ConceptCreated)
        assert event.title == "Python"

    async def test_learn_event_includes_scores(
        self, brain: BrainService, mock_deps: dict[str, AsyncMock | WorkingMemory]
    ) -> None:
        """ConceptCreated event carries importance + confidence (TASK-15)."""
        await brain.learn_concept(
            MIND, "Python", "A language", importance=0.8, confidence=0.7,
        )
        event = mock_deps["event_bus"].emit.call_args[0][0]  # type: ignore[union-attr]
        assert isinstance(event, ConceptCreated)
        assert event.importance == pytest.approx(0.8, abs=0.01)
        assert event.confidence == pytest.approx(0.7, abs=0.01)

    async def test_learn_activates_working_memory(
        self, brain: BrainService, mock_deps: dict[str, AsyncMock | WorkingMemory]
    ) -> None:
        await brain.learn_concept(MIND, "Python", "A language")
        wm = mock_deps["working_memory"]
        assert isinstance(wm, WorkingMemory)
        assert wm.get_activation(ConceptId("new-id")) > 0

    async def test_learn_dedup_returns_existing(
        self, brain: BrainService, mock_deps: dict[str, AsyncMock | WorkingMemory]
    ) -> None:
        """v13 fix: duplicate name+category → return existing."""
        existing = _concept("Python", "existing-id")
        existing.category = ConceptCategory.FACT
        mock_deps["concept_repo"].search_by_text = AsyncMock(  # type: ignore[union-attr]
            return_value=[(existing, -1.0)]
        )

        cid = await brain.learn_concept(MIND, "Python", "short")
        assert cid == ConceptId("existing-id")
        mock_deps["concept_repo"].create.assert_not_called()  # type: ignore[union-attr]

    async def test_learn_dedup_updates_longer_content(
        self, brain: BrainService, mock_deps: dict[str, AsyncMock | WorkingMemory]
    ) -> None:
        """v13 fix: duplicate with longer content → update."""
        existing = _concept("Python", "existing-id")
        existing.content = "short"
        existing.category = ConceptCategory.FACT
        mock_deps["concept_repo"].search_by_text = AsyncMock(  # type: ignore[union-attr]
            return_value=[(existing, -1.0)]
        )

        await brain.learn_concept(MIND, "Python", "A much longer and more detailed description")
        mock_deps["concept_repo"].update.assert_called_once()  # type: ignore[union-attr]

    async def test_learn_dedup_records_access(
        self, brain: BrainService, mock_deps: dict[str, AsyncMock | WorkingMemory]
    ) -> None:
        """v13 fix: duplicate → record_access called."""
        existing = _concept("Python", "existing-id")
        existing.category = ConceptCategory.FACT
        mock_deps["concept_repo"].search_by_text = AsyncMock(  # type: ignore[union-attr]
            return_value=[(existing, -1.0)]
        )

        await brain.learn_concept(MIND, "Python", "content")
        mock_deps["concept_repo"].record_access.assert_called_with(  # type: ignore[union-attr]
            ConceptId("existing-id")
        )

    async def test_learn_different_category_creates_new(
        self, brain: BrainService, mock_deps: dict[str, AsyncMock | WorkingMemory]
    ) -> None:
        """v13 fix: same name, different category → not a duplicate."""
        existing = _concept("Python", "existing-id")
        existing.category = ConceptCategory.FACT
        mock_deps["concept_repo"].search_by_text = AsyncMock(  # type: ignore[union-attr]
            return_value=[(existing, -1.0)]
        )

        cid = await brain.learn_concept(
            MIND, "Python", "content", category=ConceptCategory.PREFERENCE
        )
        assert cid == ConceptId("new-id")
        mock_deps["concept_repo"].create.assert_called_once()  # type: ignore[union-attr]

    async def test_learn_dedup_activates_working_memory(
        self, brain: BrainService, mock_deps: dict[str, AsyncMock | WorkingMemory]
    ) -> None:
        """Dedup path re-activates concept in working memory with actual importance."""
        existing = _concept("Python", "existing-id")
        existing.category = ConceptCategory.FACT
        mock_deps["concept_repo"].search_by_text = AsyncMock(  # type: ignore[union-attr]
            return_value=[(existing, -1.0)]
        )

        # Pre-decay: activate then decay to simulate old concept
        wm = mock_deps["working_memory"]
        assert isinstance(wm, WorkingMemory)
        wm.activate(ConceptId("existing-id"), 0.5)
        wm.decay_all()
        decayed_activation = wm.get_activation(ConceptId("existing-id"))
        assert decayed_activation < 0.5  # confirm it decayed

        # Re-learn same concept (dedup path)
        await brain.learn_concept(MIND, "Python", "content")

        # Should be re-activated to concept's actual importance (not flat 0.5)
        # existing importance=0.7 + 0.02 standard reinforcement = 0.72
        new_activation = wm.get_activation(ConceptId("existing-id"))
        assert new_activation > decayed_activation  # re-activated above decay level
        assert new_activation >= 0.7  # at least the concept's importance


    # ── TASK-01: importance + confidence params ──

    async def test_learn_with_explicit_importance(
        self, brain: BrainService, mock_deps: dict[str, AsyncMock | WorkingMemory]
    ) -> None:
        """Explicit importance → concept created with that importance."""
        await brain.learn_concept(MIND, "Alice", "User name", importance=0.9)
        created_concept = mock_deps["concept_repo"].create.call_args[0][0]  # type: ignore[union-attr]
        assert created_concept.importance == pytest.approx(0.9)

    async def test_learn_with_explicit_confidence(
        self, brain: BrainService, mock_deps: dict[str, AsyncMock | WorkingMemory]
    ) -> None:
        """Explicit confidence → concept created with that confidence."""
        await brain.learn_concept(MIND, "Alice", "User name", confidence=0.85)
        created_concept = mock_deps["concept_repo"].create.call_args[0][0]  # type: ignore[union-attr]
        assert created_concept.confidence == pytest.approx(0.85)

    async def test_learn_without_params_defaults(
        self, brain: BrainService, mock_deps: dict[str, AsyncMock | WorkingMemory]
    ) -> None:
        """No importance/confidence → defaults to 0.5/0.5 (backwards compat)."""
        await brain.learn_concept(MIND, "Test", "content")
        created_concept = mock_deps["concept_repo"].create.call_args[0][0]  # type: ignore[union-attr]
        assert created_concept.importance == pytest.approx(0.5)
        assert created_concept.confidence == pytest.approx(0.5)

    async def test_learn_importance_clamped(
        self, brain: BrainService, mock_deps: dict[str, AsyncMock | WorkingMemory]
    ) -> None:
        """Out-of-range importance → clamped to [0.0, 1.0]."""
        await brain.learn_concept(MIND, "Test", "content", importance=1.5)
        created = mock_deps["concept_repo"].create.call_args[0][0]  # type: ignore[union-attr]
        assert created.importance == pytest.approx(1.0)

        # Reset mock for second call
        mock_deps["concept_repo"].create.reset_mock()  # type: ignore[union-attr]
        await brain.learn_concept(MIND, "Test2", "content", importance=-0.3)
        created2 = mock_deps["concept_repo"].create.call_args[0][0]  # type: ignore[union-attr]
        assert created2.importance == pytest.approx(0.0)

    async def test_learn_dedup_confidence_diminishing_returns(
        self, brain: BrainService, mock_deps: dict[str, AsyncMock | WorkingMemory]
    ) -> None:
        """Dedup corroboration: confidence boost has diminishing returns."""
        existing = _concept("Python", "existing-id")
        existing.category = ConceptCategory.FACT
        existing.confidence = 0.8  # Already high
        mock_deps["concept_repo"].search_by_text = AsyncMock(  # type: ignore[union-attr]
            return_value=[(existing, -1.0)]
        )

        await brain.learn_concept(MIND, "Python", "content")

        # Diminishing returns: 0.08 * (1.0 - 0.8) = 0.016
        # New confidence: 0.8 + 0.016 = 0.816
        mock_deps["concept_repo"].update.assert_called_once()  # type: ignore[union-attr]
        updated = mock_deps["concept_repo"].update.call_args[0][0]  # type: ignore[union-attr]
        assert updated.confidence == pytest.approx(0.816, abs=0.005)
        # Must be less than old flat +0.1 would give (0.9)
        assert updated.confidence < 0.9

    async def test_learn_dedup_importance_weighted_reinforcement(
        self, brain: BrainService, mock_deps: dict[str, AsyncMock | WorkingMemory]
    ) -> None:
        """Dedup with higher incoming importance → weighted boost."""
        existing = _concept("Python", "existing-id")
        existing.category = ConceptCategory.FACT
        existing.importance = 0.5
        mock_deps["concept_repo"].search_by_text = AsyncMock(  # type: ignore[union-attr]
            return_value=[(existing, -1.0)]
        )

        await brain.learn_concept(MIND, "Python", "content", importance=0.8)

        updated = mock_deps["concept_repo"].update.call_args[0][0]  # type: ignore[union-attr]
        # importance=0.8 > current 0.5, so weighted boost applies:
        # boost = 0.03 * (0.8 - 0.5) = 0.009, + 0.02 = 0.029
        # new = 0.5 + 0.029 = 0.529
        assert updated.importance > 0.5  # Definitely increased
        assert updated.importance < 0.6  # But not by flat +0.05 amount

    async def test_learn_dedup_standard_reinforcement(
        self, brain: BrainService, mock_deps: dict[str, AsyncMock | WorkingMemory]
    ) -> None:
        """Dedup without incoming importance → standard +0.02 boost."""
        existing = _concept("Python", "existing-id")
        existing.category = ConceptCategory.FACT
        existing.importance = 0.6
        mock_deps["concept_repo"].search_by_text = AsyncMock(  # type: ignore[union-attr]
            return_value=[(existing, -1.0)]
        )

        await brain.learn_concept(MIND, "Python", "content")

        updated = mock_deps["concept_repo"].update.call_args[0][0]  # type: ignore[union-attr]
        # No incoming importance → standard +0.02
        assert updated.importance == pytest.approx(0.62, abs=0.005)


class TestContradictionDetection:
    """Contradiction detection during dedup (TASK-09)."""

    async def test_contradiction_reduces_confidence(
        self, brain: BrainService, mock_deps: dict[str, AsyncMock | WorkingMemory]
    ) -> None:
        """Contradicting content → confidence drops 40%."""
        existing = _concept("favorite_color", "existing-id")
        existing.category = ConceptCategory.PREFERENCE
        existing.content = "Favorite color is blue"
        existing.confidence = 0.80
        mock_deps["concept_repo"].search_by_text = AsyncMock(  # type: ignore[union-attr]
            return_value=[(existing, -1.0)]
        )

        # New content contradicts (different value, not an extension)
        await brain.learn_concept(
            MIND, "favorite_color", "Favorite color is red",
            category=ConceptCategory.PREFERENCE,
        )

        updated = mock_deps["concept_repo"].update.call_args[0][0]  # type: ignore[union-attr]
        # Contradiction: 0.80 * 0.60 = 0.48
        assert updated.confidence == pytest.approx(0.48, abs=0.02)
        assert updated.content == "Favorite color is red"  # Updated to new
        assert updated.metadata.get("last_contradiction") is True

    async def test_corroboration_no_contradiction(
        self, brain: BrainService, mock_deps: dict[str, AsyncMock | WorkingMemory]
    ) -> None:
        """Same content → corroboration, not contradiction."""
        existing = _concept("name", "existing-id")
        existing.category = ConceptCategory.ENTITY
        existing.content = "Name is Alice"
        existing.confidence = 0.60
        mock_deps["concept_repo"].search_by_text = AsyncMock(  # type: ignore[union-attr]
            return_value=[(existing, -1.0)]
        )

        # Longer content → extension, not contradiction
        await brain.learn_concept(
            MIND, "name", "Name is Alice and she lives in NY",
            category=ConceptCategory.ENTITY,
        )

        updated = mock_deps["concept_repo"].update.call_args[0][0]  # type: ignore[union-attr]
        # Content grew → corroboration + bump
        assert updated.confidence > 0.60  # noqa: PLR2004
        assert updated.content == "Name is Alice and she lives in NY"

    async def test_identical_content_no_contradiction(
        self, brain: BrainService, mock_deps: dict[str, AsyncMock | WorkingMemory]
    ) -> None:
        """Identical content → standard corroboration."""
        existing = _concept("hobby", "existing-id")
        existing.category = ConceptCategory.PREFERENCE
        existing.content = "Likes running"
        existing.confidence = 0.70
        mock_deps["concept_repo"].search_by_text = AsyncMock(  # type: ignore[union-attr]
            return_value=[(existing, -1.0)]
        )

        await brain.learn_concept(
            MIND, "hobby", "Likes running",
            category=ConceptCategory.PREFERENCE,
        )

        updated = mock_deps["concept_repo"].update.call_args[0][0]  # type: ignore[union-attr]
        # Standard corroboration: 0.70 + 0.08*(1-0.70) = 0.724
        assert updated.confidence > 0.70  # noqa: PLR2004


class TestComputeNovelty:
    """3-tier novelty detection (refinement TASK-01)."""

    async def test_cold_start_below_threshold(
        self, brain: BrainService, mock_deps: dict[str, AsyncMock | WorkingMemory]
    ) -> None:
        """Category with < 10 concepts → cold start novelty (0.70)."""
        mock_deps["concept_repo"].count_by_category = AsyncMock(return_value=5)  # type: ignore[union-attr]
        result = await brain.compute_novelty("quantum physics", "fact", MIND)
        assert result == pytest.approx(0.70)

    async def test_embedding_tier_high_similarity(
        self, brain: BrainService, mock_deps: dict[str, AsyncMock | WorkingMemory]
    ) -> None:
        """Embedding: high cosine similarity → low novelty."""
        mock_deps["concept_repo"].count_by_category = AsyncMock(return_value=50)  # type: ignore[union-attr]
        mock_deps["concept_repo"].get_embeddings_by_category = AsyncMock(  # type: ignore[union-attr]
            return_value=[[0.5] * 384]
        )
        mock_deps["embedding_engine"].has_embeddings = True
        mock_deps["embedding_engine"].encode = AsyncMock(return_value=[0.5] * 384)
        mock_deps["embedding_engine"].compute_category_centroid = AsyncMock(return_value=[0.5] * 384)

        result = await brain.compute_novelty("existing topic", "fact", MIND)
        # Cosine similarity of identical vectors = 1.0 → novelty = 0.05
        assert result == pytest.approx(0.05)

    async def test_embedding_tier_low_similarity(
        self, brain: BrainService, mock_deps: dict[str, AsyncMock | WorkingMemory]
    ) -> None:
        """Embedding: low cosine similarity → high novelty."""
        mock_deps["concept_repo"].count_by_category = AsyncMock(return_value=50)  # type: ignore[union-attr]
        # Centroid points in opposite direction
        centroid = [1.0] + [0.0] * 383
        new_vec = [0.0] * 383 + [1.0]
        mock_deps["concept_repo"].get_embeddings_by_category = AsyncMock(  # type: ignore[union-attr]
            return_value=[centroid]
        )
        mock_deps["embedding_engine"].has_embeddings = True
        mock_deps["embedding_engine"].encode = AsyncMock(return_value=new_vec)
        mock_deps["embedding_engine"].compute_category_centroid = AsyncMock(return_value=centroid)

        result = await brain.compute_novelty("totally new", "fact", MIND)
        # Cosine similarity ≈ 0.0 → novelty ≈ 0.95
        assert result > 0.85  # noqa: PLR2004

    async def test_fts5_fallback_when_no_embeddings(
        self, brain: BrainService, mock_deps: dict[str, AsyncMock | WorkingMemory]
    ) -> None:
        """No embeddings → falls back to FTS5 search."""
        mock_deps["concept_repo"].count_by_category = AsyncMock(return_value=50)  # type: ignore[union-attr]
        mock_deps["embedding_engine"].has_embeddings = False
        # FTS5: no matches → novelty 1.0
        mock_deps["retrieval"].search_concepts = AsyncMock(return_value=[])  # type: ignore[union-attr]

        result = await brain.compute_novelty("totally new", "fact", MIND)
        assert result >= 0.70  # noqa: PLR2004  # FTS5 returns 1.0 or cold start

    async def test_embedding_error_falls_back_to_fts5(
        self, brain: BrainService, mock_deps: dict[str, AsyncMock | WorkingMemory]
    ) -> None:
        """Embedding failure → graceful fallback to FTS5."""
        mock_deps["concept_repo"].count_by_category = AsyncMock(return_value=50)  # type: ignore[union-attr]
        mock_deps["embedding_engine"].has_embeddings = True
        mock_deps["embedding_engine"].encode = AsyncMock(side_effect=RuntimeError("model crashed"))
        # FTS5 should handle it
        mock_deps["retrieval"].search_concepts = AsyncMock(return_value=[])  # type: ignore[union-attr]

        result = await brain.compute_novelty("test", "fact", MIND)
        assert 0.05 <= result <= 1.0

    async def test_count_error_returns_cold_start(
        self, brain: BrainService, mock_deps: dict[str, AsyncMock | WorkingMemory]
    ) -> None:
        """count_by_category error → cold start."""
        mock_deps["concept_repo"].count_by_category = AsyncMock(  # type: ignore[union-attr]
            side_effect=RuntimeError("db error")
        )
        result = await brain.compute_novelty("test", "fact", MIND)
        assert result == pytest.approx(0.70)


class TestCentroidCache:
    """Centroid cache lifecycle (refinement TASK-02)."""

    async def test_cache_hit_skips_db(
        self, brain: BrainService, mock_deps: dict[str, AsyncMock | WorkingMemory]
    ) -> None:
        """Pre-cached centroid → no get_embeddings_by_category call."""
        mock_deps["concept_repo"].count_by_category = AsyncMock(return_value=50)  # type: ignore[union-attr]
        mock_deps["embedding_engine"].has_embeddings = True
        mock_deps["embedding_engine"].encode = AsyncMock(return_value=[0.5] * 384)

        # Pre-populate cache
        brain._centroid_cache[(str(MIND), "fact")] = [0.5] * 384

        await brain.compute_novelty("test", "fact", MIND)
        # Should NOT call get_embeddings_by_category (cache hit)
        mock_deps["concept_repo"].get_embeddings_by_category.assert_not_called()  # type: ignore[union-attr]

    async def test_cache_miss_populates_cache(
        self, brain: BrainService, mock_deps: dict[str, AsyncMock | WorkingMemory]
    ) -> None:
        """Cache miss → compute + cache for next call."""
        mock_deps["concept_repo"].count_by_category = AsyncMock(return_value=50)  # type: ignore[union-attr]
        mock_deps["concept_repo"].get_embeddings_by_category = AsyncMock(  # type: ignore[union-attr]
            return_value=[[0.5] * 384]
        )
        mock_deps["embedding_engine"].has_embeddings = True
        mock_deps["embedding_engine"].encode = AsyncMock(return_value=[0.5] * 384)
        mock_deps["embedding_engine"].compute_category_centroid = AsyncMock(return_value=[0.5] * 384)

        assert (str(MIND), "fact") not in brain._centroid_cache
        await brain.compute_novelty("test", "fact", MIND)
        assert (str(MIND), "fact") in brain._centroid_cache

    async def test_refresh_populates_all_categories(
        self, brain: BrainService, mock_deps: dict[str, AsyncMock | WorkingMemory]
    ) -> None:
        """refresh_centroid_cache fills cache for all eligible categories."""
        mock_deps["concept_repo"].get_categories = AsyncMock(  # type: ignore[union-attr]
            return_value=["fact", "entity", "preference"]
        )
        mock_deps["concept_repo"].count_by_category = AsyncMock(return_value=50)  # type: ignore[union-attr]
        mock_deps["concept_repo"].get_embeddings_by_category = AsyncMock(  # type: ignore[union-attr]
            return_value=[[0.5] * 384]
        )
        mock_deps["embedding_engine"].has_embeddings = True
        mock_deps["embedding_engine"].compute_category_centroid = AsyncMock(return_value=[0.5] * 384)

        cached = await brain.refresh_centroid_cache(MIND)
        assert cached == 3  # noqa: PLR2004
        assert len(brain._centroid_cache) == 3  # noqa: PLR2004

    async def test_refresh_skips_cold_start_categories(
        self, brain: BrainService, mock_deps: dict[str, AsyncMock | WorkingMemory]
    ) -> None:
        """Categories with < 10 concepts skipped."""
        mock_deps["concept_repo"].get_categories = AsyncMock(  # type: ignore[union-attr]
            return_value=["fact", "entity"]
        )
        # fact: 50 (eligible), entity: 5 (cold start)
        mock_deps["concept_repo"].count_by_category = AsyncMock(  # type: ignore[union-attr]
            side_effect=[50, 5]
        )
        mock_deps["concept_repo"].get_embeddings_by_category = AsyncMock(  # type: ignore[union-attr]
            return_value=[[0.5] * 384]
        )
        mock_deps["embedding_engine"].has_embeddings = True
        mock_deps["embedding_engine"].compute_category_centroid = AsyncMock(return_value=[0.5] * 384)

        cached = await brain.refresh_centroid_cache(MIND)
        assert cached == 1

    async def test_invalidate_all(
        self, brain: BrainService, mock_deps: dict[str, AsyncMock | WorkingMemory]
    ) -> None:
        brain._centroid_cache[("mind1", "fact")] = [0.1]
        brain._centroid_cache[("mind2", "fact")] = [0.2]
        brain.invalidate_centroid_cache()
        assert len(brain._centroid_cache) == 0

    async def test_invalidate_specific_mind(
        self, brain: BrainService, mock_deps: dict[str, AsyncMock | WorkingMemory]
    ) -> None:
        brain._centroid_cache[(str(MIND), "fact")] = [0.1]
        brain._centroid_cache[("other", "fact")] = [0.2]
        brain.invalidate_centroid_cache(MIND)
        assert (str(MIND), "fact") not in brain._centroid_cache
        assert ("other", "fact") in brain._centroid_cache

    async def test_no_embeddings_returns_zero(
        self, brain: BrainService, mock_deps: dict[str, AsyncMock | WorkingMemory]
    ) -> None:
        mock_deps["embedding_engine"].has_embeddings = False
        cached = await brain.refresh_centroid_cache(MIND)
        assert cached == 0


class TestDecayWorkingMemory:
    """decay_working_memory() — delegates to working memory."""

    async def test_decay_reduces_activation(
        self, brain: BrainService, mock_deps: dict[str, AsyncMock | WorkingMemory]
    ) -> None:
        wm = mock_deps["working_memory"]
        assert isinstance(wm, WorkingMemory)
        wm.activate(ConceptId("c1"), 0.8)

        brain.decay_working_memory()

        activation = wm.get_activation(ConceptId("c1"))
        assert activation < 0.8
        # decay_rate=0.15: 0.8 * 0.85 = 0.68
        assert abs(activation - 0.68) < 0.01


class TestEncodeEpisode:
    """encode_episode() — create + Hebbian."""

    async def test_encode_creates_episode(
        self, brain: BrainService, mock_deps: dict[str, AsyncMock | WorkingMemory]
    ) -> None:
        eid = await brain.encode_episode(MIND, ConversationId("conv1"), "hello", "hi there")
        assert eid == EpisodeId("ep-id")
        mock_deps["episode_repo"].create.assert_called_once()  # type: ignore[union-attr]

    async def test_encode_emits_event(
        self, brain: BrainService, mock_deps: dict[str, AsyncMock | WorkingMemory]
    ) -> None:
        await brain.encode_episode(MIND, ConversationId("conv1"), "hello", "hi")
        mock_deps["event_bus"].emit.assert_called_once()  # type: ignore[union-attr]
        event = mock_deps["event_bus"].emit.call_args[0][0]  # type: ignore[union-attr]
        assert isinstance(event, EpisodeEncoded)

    async def test_encode_triggers_hebbian(
        self, brain: BrainService, mock_deps: dict[str, AsyncMock | WorkingMemory]
    ) -> None:
        """Star topology Hebbian fires when ≥2 concepts active."""
        wm = mock_deps["working_memory"]
        assert isinstance(wm, WorkingMemory)
        wm.activate(ConceptId("c1"), 0.8)
        wm.activate(ConceptId("c2"), 0.6)

        await brain.encode_episode(MIND, ConversationId("conv1"), "hello", "hi")
        mock_deps["hebbian"].strengthen_star.assert_called_once()  # type: ignore[union-attr]

    async def test_encode_no_hebbian_with_single_concept(
        self, brain: BrainService, mock_deps: dict[str, AsyncMock | WorkingMemory]
    ) -> None:
        wm = mock_deps["working_memory"]
        assert isinstance(wm, WorkingMemory)
        wm.activate(ConceptId("c1"), 0.8)

        await brain.encode_episode(MIND, ConversationId("conv1"), "hello", "hi")
        mock_deps["hebbian"].strengthen_star.assert_not_called()  # type: ignore[union-attr]


class TestGetRelated:
    """get_related() — graph traversal."""

    async def test_get_related(
        self, brain: BrainService, mock_deps: dict[str, AsyncMock | WorkingMemory]
    ) -> None:
        c2 = _concept("related", "c2")
        mock_deps["relation_repo"].get_neighbors = AsyncMock(  # type: ignore[union-attr]
            return_value=[(ConceptId("c2"), 0.8)]
        )
        mock_deps["concept_repo"].get = AsyncMock(return_value=c2)  # type: ignore[union-attr]

        related = await brain.get_related(ConceptId("c1"))
        assert len(related) == 1
        assert related[0].name == "related"


class TestStrengthenConnection:
    """strengthen_connection() — manual Hebbian."""

    async def test_delegates_to_hebbian(
        self, brain: BrainService, mock_deps: dict[str, AsyncMock | WorkingMemory]
    ) -> None:
        ids = [ConceptId("c1"), ConceptId("c2")]
        await brain.strengthen_connection(ids)
        mock_deps["hebbian"].strengthen.assert_called_once_with(ids, relation_types=None)  # type: ignore[union-attr]


class TestProtocolCompliance:
    """BrainService satisfies BrainReader + BrainWriter protocols."""

    def test_is_brain_reader(self, brain: BrainService) -> None:
        from sovyx.engine.protocols import BrainReader

        assert isinstance(brain, BrainReader)

    def test_is_brain_writer(self, brain: BrainService) -> None:
        from sovyx.engine.protocols import BrainWriter

        assert isinstance(brain, BrainWriter)
