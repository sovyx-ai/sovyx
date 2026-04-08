"""Integration tests — concept merging in consolidation.

Verifies that similar concepts are merged during consolidation,
relations are transferred, and attributes are combined correctly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from sovyx.brain.concept_repo import ConceptRepository, _levenshtein
from sovyx.brain.consolidation import ConsolidationCycle
from sovyx.brain.embedding import EmbeddingEngine
from sovyx.brain.learning import EbbinghausDecay
from sovyx.brain.models import Concept
from sovyx.brain.relation_repo import RelationRepository
from sovyx.engine.types import ConceptCategory, ConceptId, MindId
from sovyx.persistence.migrations import MigrationRunner
from sovyx.persistence.pool import DatabasePool
from sovyx.persistence.schemas.brain import get_brain_migrations

MIND = MindId("test-mind")


@pytest.fixture
async def brain_pool(tmp_path: Path) -> DatabasePool:
    pool = DatabasePool(db_path=tmp_path / "brain.db", read_pool_size=1)
    await pool.initialize()
    runner = MigrationRunner(pool)
    await runner.initialize()
    await runner.run_migrations(get_brain_migrations(has_sqlite_vec=pool.has_sqlite_vec))
    return pool


@pytest.fixture
async def concept_repo(brain_pool: DatabasePool) -> ConceptRepository:
    embedding = EmbeddingEngine()
    return ConceptRepository(pool=brain_pool, embedding_engine=embedding)


@pytest.fixture
async def relation_repo(brain_pool: DatabasePool) -> RelationRepository:
    return RelationRepository(pool=brain_pool)


async def _create_concept(
    repo: ConceptRepository,
    name: str,
    content: str = "",
    category: ConceptCategory = ConceptCategory.FACT,
    importance: float = 0.5,
    access_count: int = 1,
) -> ConceptId:
    c = Concept(
        mind_id=MIND,
        name=name,
        content=content or f"Content for {name}",
        category=category,
        importance=importance,
        access_count=access_count,
    )
    return await repo.create(c)


class TestLevenshtein:
    """Levenshtein distance function."""

    def test_identical(self) -> None:
        assert _levenshtein("python", "python") == 0

    def test_one_edit(self) -> None:
        assert _levenshtein("python", "pyhton") == 2

    def test_different_length(self) -> None:
        assert _levenshtein("cat", "cats") == 1

    def test_empty(self) -> None:
        assert _levenshtein("", "abc") == 3
        assert _levenshtein("abc", "") == 3

    def test_completely_different(self) -> None:
        assert _levenshtein("abc", "xyz") == 3


class TestMergeCandidates:
    """find_merge_candidates logic."""

    async def test_name_containment(self, concept_repo: ConceptRepository) -> None:
        """'PostgreSQL' and 'PostgreSQL Preference' → merge candidates."""
        await _create_concept(concept_repo, "PostgreSQL", category=ConceptCategory.PREFERENCE)
        await _create_concept(
            concept_repo,
            "PostgreSQL Preference",
            category=ConceptCategory.PREFERENCE,
        )
        pairs = await concept_repo.find_merge_candidates(MIND)
        assert len(pairs) == 1
        survivor, to_merge = pairs[0]
        assert "PostgreSQL" in survivor.name
        assert "PostgreSQL" in to_merge.name

    async def test_levenshtein_close(self, concept_repo: ConceptRepository) -> None:
        """Names within Levenshtein 3 → merge candidates."""
        await _create_concept(concept_repo, "Python")
        await _create_concept(concept_repo, "Pythons")  # dist=1
        pairs = await concept_repo.find_merge_candidates(MIND)
        assert len(pairs) == 1

    async def test_different_category_no_merge(self, concept_repo: ConceptRepository) -> None:
        """Same name, different category → NOT candidates."""
        await _create_concept(concept_repo, "Python", category=ConceptCategory.SKILL)
        await _create_concept(concept_repo, "Python", category=ConceptCategory.ENTITY)
        pairs = await concept_repo.find_merge_candidates(MIND)
        assert len(pairs) == 0

    async def test_unrelated_names_no_merge(self, concept_repo: ConceptRepository) -> None:
        """Completely different names → NOT candidates."""
        await _create_concept(concept_repo, "Python")
        await _create_concept(concept_repo, "Kubernetes")
        pairs = await concept_repo.find_merge_candidates(MIND)
        assert len(pairs) == 0

    async def test_max_10_pairs(self, concept_repo: ConceptRepository) -> None:
        """At most 10 pairs returned per cycle."""
        for i in range(25):
            await _create_concept(concept_repo, f"Item{i}")
            await _create_concept(concept_repo, f"Item{i}s")  # dist=1
        pairs = await concept_repo.find_merge_candidates(MIND)
        assert len(pairs) <= 10  # noqa: PLR2004


class TestTransferRelations:
    """Relation transfer during merge."""

    async def test_relations_transferred(
        self,
        concept_repo: ConceptRepository,
        relation_repo: RelationRepository,
    ) -> None:
        """Relations from merged concept move to survivor."""
        id_a = await _create_concept(concept_repo, "Alpha")
        id_b = await _create_concept(concept_repo, "Beta")
        id_c = await _create_concept(concept_repo, "Charlie")

        # Create relation B→C
        await relation_repo.get_or_create(id_b, id_c)

        # Transfer B→A (merge B into A)
        transferred = await relation_repo.transfer_relations(id_b, id_a)
        assert transferred == 1

        # A should now have relation to C
        neighbors = await relation_repo.get_neighbors(id_a)
        neighbor_ids = [str(n[0]) for n in neighbors]
        assert str(id_c) in neighbor_ids

    async def test_self_loop_deleted(
        self,
        concept_repo: ConceptRepository,
        relation_repo: RelationRepository,
    ) -> None:
        """Relation between merged and survivor → deleted (self-loop)."""
        id_a = await _create_concept(concept_repo, "Alpha")
        id_b = await _create_concept(concept_repo, "Beta")

        # Create relation A→B
        await relation_repo.get_or_create(id_a, id_b)

        # Merge B into A — the A→B relation becomes A→A (self-loop)
        transferred = await relation_repo.transfer_relations(id_b, id_a)
        assert transferred == 0  # self-loop deleted, not transferred


class TestConsolidationMerge:
    """Full consolidation cycle with merging."""

    async def test_merge_in_consolidation(
        self,
        brain_pool: DatabasePool,
        concept_repo: ConceptRepository,
        relation_repo: RelationRepository,
    ) -> None:
        """Consolidation merges similar concepts."""
        # Create similar concepts
        await _create_concept(
            concept_repo,
            "PostgreSQL",
            content="Database",
            category=ConceptCategory.PREFERENCE,
            importance=0.8,
            access_count=5,
        )
        await _create_concept(
            concept_repo,
            "PostgreSQL Preference",
            content="User prefers PostgreSQL for everything",
            category=ConceptCategory.PREFERENCE,
            importance=0.5,
            access_count=2,
        )

        # Verify 2 concepts exist
        count_before = await concept_repo.count(MIND)
        assert count_before == 2  # noqa: PLR2004

        # Run consolidation
        decay = EbbinghausDecay(concept_repo=concept_repo, relation_repo=relation_repo)
        events = AsyncMock()
        brain = AsyncMock()

        cycle = ConsolidationCycle(
            brain_service=brain,
            decay=decay,
            event_bus=events,
            concept_repo=concept_repo,
            relation_repo=relation_repo,
        )
        result = await cycle.run(MIND)

        # Should have merged 1 concept
        assert result.merged == 1
        count_after = await concept_repo.count(MIND)
        assert count_after == 1

        # Survivor should have combined attributes
        concepts = await concept_repo.get_by_mind(MIND)
        assert len(concepts) == 1
        survivor = concepts[0]
        # Importance may be slightly decayed by Ebbinghaus before merge
        assert survivor.importance >= 0.7
        assert survivor.access_count == 7  # 5+2  # noqa: PLR2004
        # Longer content wins
        assert "prefers PostgreSQL" in survivor.content

    async def test_no_merge_without_repos(
        self,
        brain_pool: DatabasePool,
        concept_repo: ConceptRepository,
        relation_repo: RelationRepository,
    ) -> None:
        """Backward compat: no repos → no merging."""
        await _create_concept(concept_repo, "Test")
        await _create_concept(concept_repo, "Tests")

        decay = EbbinghausDecay(concept_repo=concept_repo, relation_repo=relation_repo)
        events = AsyncMock()
        brain = AsyncMock()

        cycle = ConsolidationCycle(
            brain_service=brain,
            decay=decay,
            event_bus=events,
            # No concept_repo/relation_repo
        )
        result = await cycle.run(MIND)
        assert result.merged == 0
