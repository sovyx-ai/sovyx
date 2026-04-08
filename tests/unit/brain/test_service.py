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
        """Dedup path re-activates concept in working memory (decay fix)."""
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

        # Should be re-activated to 0.5 (not left at decayed value)
        new_activation = wm.get_activation(ConceptId("existing-id"))
        assert new_activation == 0.5


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
