"""VAL-28: Brain roundtrip expanded — multi-mind isolation, update, search ranking."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from sovyx.brain.concept_repo import ConceptRepository
from sovyx.brain.embedding import EmbeddingEngine
from sovyx.brain.models import Concept
from sovyx.brain.relation_repo import RelationRepository
from sovyx.engine.types import ConceptCategory, ConceptId, MindId
from sovyx.persistence.migrations import MigrationRunner
from sovyx.persistence.pool import DatabasePool
from sovyx.persistence.schemas.brain import get_brain_migrations


@pytest.fixture()
async def brain_pool(tmp_path: Path) -> DatabasePool:
    pool = DatabasePool(db_path=tmp_path / "brain.db", read_pool_size=1)
    await pool.initialize()
    runner = MigrationRunner(pool)
    await runner.initialize()
    await runner.run_migrations(get_brain_migrations(has_sqlite_vec=pool.has_sqlite_vec))
    yield pool  # type: ignore[misc]
    await pool.close()


@pytest.fixture()
def embedding() -> EmbeddingEngine:
    return EmbeddingEngine()


@pytest.fixture()
def concept_repo(brain_pool: DatabasePool, embedding: EmbeddingEngine) -> ConceptRepository:
    return ConceptRepository(brain_pool, embedding)


@pytest.fixture()
def relation_repo(brain_pool: DatabasePool) -> RelationRepository:
    return RelationRepository(brain_pool)


class TestMultiMindIsolation:
    """Concepts from different minds are isolated."""

    async def test_search_only_returns_own_mind(self, concept_repo: ConceptRepository) -> None:
        mind_a = MindId("mind-alpha")
        mind_b = MindId("mind-beta")

        await concept_repo.create(
            Concept(
                id=ConceptId("iso-a1"),
                mind_id=mind_a,
                name="alpha secret",
                content="Alpha likes chess",
                category=ConceptCategory.PREFERENCE,
            )
        )
        await concept_repo.create(
            Concept(
                id=ConceptId("iso-b1"),
                mind_id=mind_b,
                name="beta secret",
                content="Beta likes chess too",
                category=ConceptCategory.PREFERENCE,
            )
        )

        results_a = await concept_repo.search_by_text("chess", mind_id=mind_a)
        results_b = await concept_repo.search_by_text("chess", mind_id=mind_b)

        # Each mind only sees its own concepts
        assert all(c.mind_id == mind_a for c, _ in results_a)
        assert all(c.mind_id == mind_b for c, _ in results_b)
        assert len(results_a) == 1
        assert len(results_b) == 1

    async def test_get_by_id_cross_mind(self, concept_repo: ConceptRepository) -> None:
        """get() returns concept regardless of mind (by ID)."""
        mind = MindId("mind-cross")
        cid = await concept_repo.create(
            Concept(
                id=ConceptId("cross-1"),
                mind_id=mind,
                name="cross test",
                content="Should be accessible by ID",
                category=ConceptCategory.FACT,
            )
        )
        fetched = await concept_repo.get(cid)
        assert fetched is not None
        assert fetched.name == "cross test"


class TestConceptUpdate:
    """Updating concept content and metadata."""

    async def test_update_content(self, concept_repo: ConceptRepository) -> None:
        mind = MindId("mind-upd")
        cid = await concept_repo.create(
            Concept(
                id=ConceptId("upd-1"),
                mind_id=mind,
                name="changeable",
                content="Original content",
                category=ConceptCategory.FACT,
            )
        )

        fetched = await concept_repo.get(cid)
        assert fetched is not None
        fetched.content = "Updated content"
        await concept_repo.update(fetched)

        refetched = await concept_repo.get(cid)
        assert refetched is not None
        assert refetched.content == "Updated content"


class TestSearchRanking:
    """FTS5 search returns more relevant results first."""

    async def test_exact_match_ranks_higher(self, concept_repo: ConceptRepository) -> None:
        mind = MindId("mind-rank")

        # Create a concept with exact match
        await concept_repo.create(
            Concept(
                id=ConceptId("rank-1"),
                mind_id=mind,
                name="python programming",
                content="Python is a versatile programming language",
                category=ConceptCategory.FACT,
            )
        )
        # Create a tangentially related concept
        await concept_repo.create(
            Concept(
                id=ConceptId("rank-2"),
                mind_id=mind,
                name="pet snake",
                content="User has a pet python snake named Monty",
                category=ConceptCategory.FACT,
            )
        )

        results = await concept_repo.search_by_text("python programming", mind_id=mind)
        assert len(results) >= 1
        # The programming concept should come first
        assert results[0][0].name == "python programming"


class TestConceptCategories:
    """All concept categories can be stored and retrieved."""

    async def test_all_categories(self, concept_repo: ConceptRepository) -> None:
        mind = MindId("mind-cat")
        for i, cat in enumerate(ConceptCategory):
            cid = await concept_repo.create(
                Concept(
                    id=ConceptId(f"cat-{i}"),
                    mind_id=mind,
                    name=f"concept {cat.value}",
                    content=f"Content for {cat.value}",
                    category=cat,
                )
            )
            fetched = await concept_repo.get(cid)
            assert fetched is not None
            # Compare by value to handle cross-namespace enum identity
            assert fetched.category.value == cat.value
