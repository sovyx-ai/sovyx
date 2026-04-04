"""Integration tests — real SQLite, no mocks on persistence.

Verifies the full brain subsystem: store → recall → verify,
consolidation decay, and Hebbian strengthening with real DB operations.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from sovyx.brain.concept_repo import ConceptRepository
from sovyx.brain.embedding import EmbeddingEngine
from sovyx.brain.learning import EbbinghausDecay, HebbianLearning
from sovyx.brain.models import Concept
from sovyx.brain.relation_repo import RelationRepository
from sovyx.engine.types import ConceptCategory, ConceptId, MindId
from sovyx.persistence.migrations import MigrationRunner
from sovyx.persistence.pool import DatabasePool
from sovyx.persistence.schemas.brain import get_brain_migrations


@pytest.fixture
async def brain_pool(tmp_path: Path) -> DatabasePool:
    """Real SQLite pool with brain schema."""
    pool = DatabasePool(db_path=tmp_path / "brain.db", read_pool_size=1)
    await pool.initialize()
    runner = MigrationRunner(pool)
    await runner.initialize()
    await runner.run_migrations(get_brain_migrations(has_sqlite_vec=pool.has_sqlite_vec))
    yield pool  # type: ignore[misc]
    await pool.close()


@pytest.fixture
def mind_id() -> MindId:
    """Test mind ID."""
    return MindId("test-mind")


@pytest.fixture
def embedding() -> EmbeddingEngine:
    """EmbeddingEngine in FTS5 fallback mode (no ONNX in CI)."""
    return EmbeddingEngine()


@pytest.fixture
def concept_repo(brain_pool: DatabasePool, embedding: EmbeddingEngine) -> ConceptRepository:
    """Real ConceptRepository."""
    return ConceptRepository(brain_pool, embedding)


@pytest.fixture
def relation_repo(brain_pool: DatabasePool) -> RelationRepository:
    """Real RelationRepository."""
    return RelationRepository(brain_pool)


class TestConceptRoundtrip:
    """Store concept → read back → verify content."""

    @pytest.mark.asyncio
    async def test_create_and_get(
        self, concept_repo: ConceptRepository, mind_id: MindId
    ) -> None:

        concept = Concept(
            id=ConceptId("c1"),
            mind_id=mind_id,
            name="pizza preference",
            content="User loves margherita pizza",
            category=ConceptCategory.PREFERENCE,
        )
        cid = await concept_repo.create(concept)
        fetched = await concept_repo.get(cid)
        assert fetched is not None
        assert fetched.name == "pizza preference"
        assert fetched.content == "User loves margherita pizza"
        assert fetched.category == ConceptCategory.PREFERENCE

    @pytest.mark.asyncio
    async def test_fts5_search_finds_stored_concept(
        self, concept_repo: ConceptRepository, mind_id: MindId
    ) -> None:

        concept = Concept(
            id=ConceptId("c2"),
            mind_id=mind_id,
            name="running hobby",
            content="User enjoys running marathons every weekend",
            category=ConceptCategory.PREFERENCE,
        )
        await concept_repo.create(concept)
        results = await concept_repo.search_by_text("running", mind_id=mind_id)
        assert len(results) > 0
        assert any("running" in c.content for c, _ in results)


class TestHebbianIntegration:
    """Hebbian learning creates and strengthens relations in real DB."""

    @pytest.mark.asyncio
    async def test_strengthen_creates_relations(
        self,
        concept_repo: ConceptRepository,
        relation_repo: RelationRepository,
        mind_id: MindId,
    ) -> None:

        # Create 3 concepts
        ids = []
        for i, name in enumerate(["pizza", "pasta", "italian"]):
            c = Concept(
                id=ConceptId(f"h{i}"),
                mind_id=mind_id,
                name=name,
                content=f"User likes {name}",
                category=ConceptCategory.PREFERENCE,
            )
            cid = await concept_repo.create(c)
            ids.append(cid)

        # Run Hebbian
        hebbian = HebbianLearning(relation_repo=relation_repo)
        count = await hebbian.strengthen(ids)
        assert count == 3  # 3 pairs from 3 concepts

        # Verify relations exist
        relations = await relation_repo.get_relations_for(ids[0])
        assert len(relations) > 0


class TestDecayIntegration:
    """Ebbinghaus decay reduces importance in real DB."""

    @pytest.mark.asyncio
    async def test_decay_reduces_importance(
        self,
        concept_repo: ConceptRepository,
        relation_repo: RelationRepository,
        mind_id: MindId,
    ) -> None:

        concept = Concept(
            id=ConceptId("d1"),
            mind_id=mind_id,
            name="decayable",
            content="Something to forget",
            category=ConceptCategory.FACT,
            importance=0.5,
        )
        cid = await concept_repo.create(concept)

        decay = EbbinghausDecay(concept_repo=concept_repo, relation_repo=relation_repo)
        concepts_decayed, _ = await decay.apply_decay(mind_id)
        assert concepts_decayed > 0

        fetched = await concept_repo.get(cid)
        assert fetched is not None
        assert fetched.importance < 0.5  # Decayed
