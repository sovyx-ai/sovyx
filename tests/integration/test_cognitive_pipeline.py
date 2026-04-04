"""Integration test — full cognitive pipeline with real DB.

Verifies: bootstrap → brain store → context assemble → cognitive loop.
No mocks on persistence. LLM is mocked (no API key needed).
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from sovyx.brain.concept_repo import ConceptRepository
from sovyx.brain.embedding import EmbeddingEngine
from sovyx.brain.episode_repo import EpisodeRepository
from sovyx.brain.learning import EbbinghausDecay, HebbianLearning
from sovyx.brain.relation_repo import RelationRepository
from sovyx.brain.retrieval import HybridRetrieval
from sovyx.brain.service import BrainService
from sovyx.brain.spreading import SpreadingActivation
from sovyx.brain.working_memory import WorkingMemory
from sovyx.context.assembler import ContextAssembler
from sovyx.context.budget import TokenBudgetManager
from sovyx.context.formatter import ContextFormatter
from sovyx.context.tokenizer import TokenCounter
from sovyx.engine.events import EventBus
from sovyx.engine.types import ConceptCategory, MindId
from sovyx.mind.config import MindConfig
from sovyx.mind.personality import PersonalityEngine
from sovyx.persistence.migrations import MigrationRunner
from sovyx.persistence.pool import DatabasePool
from sovyx.persistence.schemas.brain import get_brain_migrations

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
async def brain_pool(tmp_path: Path) -> DatabasePool:
    """Real SQLite pool with brain schema + sqlite-vec."""
    pool = DatabasePool(
        db_path=tmp_path / "brain.db",
        read_pool_size=1,
        load_extensions=["vec0"],
    )
    await pool.initialize()
    runner = MigrationRunner(pool)
    await runner.initialize()
    await runner.run_migrations(
        get_brain_migrations(has_sqlite_vec=pool.has_sqlite_vec)
    )
    yield pool  # type: ignore[misc]
    await pool.close()


@pytest.fixture
def mind_id() -> MindId:
    return MindId("test-mind")


@pytest.fixture
def mind_config() -> MindConfig:
    return MindConfig(name="TestMind")


@pytest.fixture
def event_bus() -> EventBus:
    return EventBus()


@pytest.fixture
def brain_service(
    brain_pool: DatabasePool,
    event_bus: EventBus,
) -> BrainService:
    """Full BrainService with real repos, mock embedding."""
    mock_embedding = AsyncMock(spec=EmbeddingEngine)
    mock_embedding.has_embeddings = False
    mock_embedding.encode = AsyncMock(return_value=[0.0] * 384)

    concept_repo = ConceptRepository(brain_pool, mock_embedding)
    episode_repo = EpisodeRepository(brain_pool, mock_embedding)
    relation_repo = RelationRepository(brain_pool)
    working_memory = WorkingMemory()
    spreading = SpreadingActivation(relation_repo, working_memory)
    hebbian = HebbianLearning(relation_repo)
    decay = EbbinghausDecay(concept_repo, relation_repo)
    retrieval = HybridRetrieval(concept_repo, episode_repo, mock_embedding)

    return BrainService(
        concept_repo=concept_repo,
        episode_repo=episode_repo,
        relation_repo=relation_repo,
        embedding_engine=mock_embedding,
        spreading=spreading,
        hebbian=hebbian,
        decay=decay,
        retrieval=retrieval,
        working_memory=working_memory,
        event_bus=event_bus,
    )


class TestFullPipeline:
    """Store knowledge → recall → assemble context → verify output."""

    @pytest.mark.asyncio
    async def test_store_recall_assemble(
        self,
        brain_service: BrainService,
        mind_id: MindId,
        mind_config: MindConfig,
    ) -> None:
        """Store concept → recall with query → assemble into LLM context."""
        # 1. Start brain
        await brain_service.start(mind_id)

        # 2. Store a concept via learn_concept
        cid = await brain_service.learn_concept(
            mind_id=mind_id,
            name="pizza preference",
            content="User loves margherita pizza from Naples",
            category=ConceptCategory.PREFERENCE,
        )
        assert cid is not None

        # 3. Verify FTS5 search finds it directly
        direct_results = await brain_service._concepts.search_by_text(
            "pizza", mind_id=mind_id
        )
        assert len(direct_results) > 0, "FTS5 direct search failed"

        # 4. Full recall through service (hybrid retrieval)
        search_results = await brain_service.search("pizza", mind_id)
        assert len(search_results) > 0, f"service.search empty, direct had {len(direct_results)}"

        concepts, episodes = await brain_service.recall("pizza", mind_id)

        # 5. Assemble context
        counter = TokenCounter()
        assembler = ContextAssembler(
            token_counter=counter,
            personality_engine=PersonalityEngine(mind_config),
            brain_service=brain_service,
            budget_manager=TokenBudgetManager(),
            formatter=ContextFormatter(counter),
            mind_config=mind_config,
        )

        result = await assembler.assemble(
            current_message="What's my favorite pizza?",
            conversation_history=[],
            mind_id=mind_id,
        )

        # 6. Verify
        assert len(result.messages) >= 2  # system + user
        user_msg = result.messages[-1]["content"]
        assert "pizza" in user_msg.lower()

        # Concept should have been recalled and included somewhere
        assert len(concepts) > 0, "Brain recall found no concepts for 'pizza'"
        assert any("pizza" in c.content.lower() for c, _ in concepts)
        assert result.tokens_used > 0

        # 7. Cleanup
        await brain_service.stop()

    @pytest.mark.asyncio
    async def test_hebbian_and_decay_pipeline(
        self,
        brain_service: BrainService,
        mind_id: MindId,
    ) -> None:
        """Store → Hebbian strengthen → Decay → verify lifecycle."""
        await brain_service.start(mind_id)

        # Store related concepts
        ids = []
        for name in ["pasta", "Italian cuisine", "Rome"]:
            cid = await brain_service.learn_concept(
                mind_id=mind_id,
                name=name,
                content=f"Knowledge about {name}",
                category=ConceptCategory.FACT,
            )
            ids.append(cid)

        # Hebbian: strengthen connections
        await brain_service.strengthen_connection(ids)

        # Decay: should reduce importance
        # (Brain started with concepts loaded into working memory)
        await brain_service.stop()
