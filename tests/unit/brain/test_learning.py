"""Tests for sovyx.brain.learning — Hebbian learning and Ebbinghaus decay."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from sovyx.brain.concept_repo import ConceptRepository
from sovyx.brain.learning import EbbinghausDecay, HebbianLearning
from sovyx.brain.models import Concept
from sovyx.brain.relation_repo import RelationRepository
from sovyx.engine.types import ConceptId, MindId
from sovyx.persistence.migrations import MigrationRunner
from sovyx.persistence.pool import DatabasePool
from sovyx.persistence.schemas.brain import get_brain_migrations

if TYPE_CHECKING:
    from pathlib import Path

MIND = MindId("aria")


@pytest.fixture
async def pool(tmp_path: Path) -> DatabasePool:
    """Pool with brain schema."""
    p = DatabasePool(db_path=tmp_path / "brain.db", read_pool_size=1)
    await p.initialize()
    runner = MigrationRunner(p)
    await runner.initialize()
    await runner.run_migrations(get_brain_migrations(has_sqlite_vec=False))
    yield p  # type: ignore[misc]
    await p.close()


@pytest.fixture
def mock_embedding() -> AsyncMock:
    engine = AsyncMock()
    engine.has_embeddings = False
    return engine


@pytest.fixture
def concept_repo(pool: DatabasePool, mock_embedding: AsyncMock) -> ConceptRepository:
    return ConceptRepository(pool, mock_embedding)


@pytest.fixture
def relation_repo(pool: DatabasePool) -> RelationRepository:
    return RelationRepository(pool)


async def _seed_concepts(
    repo: ConceptRepository, *names: str
) -> list[ConceptId]:
    """Create concepts and return their IDs."""
    ids: list[ConceptId] = []
    for name in names:
        c = Concept(mind_id=MIND, name=name)
        cid = await repo.create(c)
        ids.append(cid)
    return ids


class TestHebbianLearning:
    """Hebbian strengthening."""

    async def test_strengthen_two_concepts(
        self,
        relation_repo: RelationRepository,
        concept_repo: ConceptRepository,
    ) -> None:
        ids = await _seed_concepts(concept_repo, "A", "B")
        hebbian = HebbianLearning(relation_repo)

        count = await hebbian.strengthen(ids)
        assert count == 1

        # Relation should exist
        relations = await relation_repo.get_relations_for(ids[0])
        assert len(relations) == 1
        assert relations[0].weight > 0.5  # default 0.5 + delta

    async def test_strengthen_creates_relation(
        self,
        relation_repo: RelationRepository,
        concept_repo: ConceptRepository,
    ) -> None:
        ids = await _seed_concepts(concept_repo, "X", "Y")
        hebbian = HebbianLearning(relation_repo)

        await hebbian.strengthen(ids)
        neighbors = await relation_repo.get_neighbors(ids[0])
        assert len(neighbors) == 1

    async def test_weight_converges_to_one(
        self,
        relation_repo: RelationRepository,
        concept_repo: ConceptRepository,
    ) -> None:
        """Repeated strengthening converges to 1.0 but never exceeds."""
        ids = await _seed_concepts(concept_repo, "A", "B")
        hebbian = HebbianLearning(relation_repo, learning_rate=0.5)

        for _ in range(20):
            await hebbian.strengthen(ids)

        relations = await relation_repo.get_relations_for(ids[0])
        # Find the actual relation (not just co-occurrence created ones)
        assert len(relations) >= 1
        for rel in relations:
            assert rel.weight <= 1.0

    async def test_weight_never_exceeds_one(
        self,
        relation_repo: RelationRepository,
        concept_repo: ConceptRepository,
    ) -> None:
        """Even with high co_activation, weight stays ≤ 1.0."""
        ids = await _seed_concepts(concept_repo, "A", "B")
        hebbian = HebbianLearning(relation_repo, learning_rate=0.9)

        # High activation values
        activations = {ids[0]: 5.0, ids[1]: 5.0}
        await hebbian.strengthen(ids, activations=activations)

        relations = await relation_repo.get_relations_for(ids[0])
        for rel in relations:
            assert rel.weight <= 1.0

    async def test_single_concept_no_op(
        self, relation_repo: RelationRepository
    ) -> None:
        hebbian = HebbianLearning(relation_repo)
        count = await hebbian.strengthen([ConceptId("c1")])
        assert count == 0

    async def test_empty_list_no_op(
        self, relation_repo: RelationRepository
    ) -> None:
        hebbian = HebbianLearning(relation_repo)
        count = await hebbian.strengthen([])
        assert count == 0

    async def test_three_concepts_creates_three_pairs(
        self,
        relation_repo: RelationRepository,
        concept_repo: ConceptRepository,
    ) -> None:
        ids = await _seed_concepts(concept_repo, "A", "B", "C")
        hebbian = HebbianLearning(relation_repo)

        count = await hebbian.strengthen(ids)
        assert count == 3  # noqa: PLR2004  # A-B, A-C, B-C

    async def test_with_activations(
        self,
        relation_repo: RelationRepository,
        concept_repo: ConceptRepository,
    ) -> None:
        ids = await _seed_concepts(concept_repo, "A", "B")
        hebbian = HebbianLearning(relation_repo)

        activations = {ids[0]: 0.8, ids[1]: 0.3}
        await hebbian.strengthen(ids, activations=activations)

        relations = await relation_repo.get_relations_for(ids[0])
        assert len(relations) >= 1


class TestEbbinghausDecay:
    """Ebbinghaus forgetting curve."""

    async def test_decay_reduces_importance(
        self,
        concept_repo: ConceptRepository,
        relation_repo: RelationRepository,
    ) -> None:
        c = Concept(mind_id=MIND, name="test", importance=0.8)
        cid = await concept_repo.create(c)

        decay = EbbinghausDecay(concept_repo, relation_repo)
        concepts_decayed, _ = await decay.apply_decay(MIND)
        assert concepts_decayed >= 1

        fetched = await concept_repo.get(cid)
        assert fetched is not None
        assert fetched.importance < 0.8

    async def test_access_count_reduces_decay(
        self,
        concept_repo: ConceptRepository,
        relation_repo: RelationRepository,
    ) -> None:
        """Higher access_count → less decay (rehearsal effect)."""
        # Concept A: never accessed
        ca = Concept(mind_id=MIND, name="never accessed", importance=0.8)
        cid_a = await concept_repo.create(ca)

        # Concept B: accessed 10 times
        cb = Concept(
            mind_id=MIND, name="well accessed", importance=0.8
        )
        cid_b = await concept_repo.create(cb)
        for _ in range(10):
            await concept_repo.record_access(cid_b)

        decay = EbbinghausDecay(concept_repo, relation_repo)
        await decay.apply_decay(MIND)

        a = await concept_repo.get(cid_a)
        b = await concept_repo.get(cid_b)
        assert a is not None
        assert b is not None
        # B should retain more importance than A
        assert b.importance > a.importance

    async def test_highly_accessed_nearly_immune(
        self,
        concept_repo: ConceptRepository,
        relation_repo: RelationRepository,
    ) -> None:
        """100 accesses → nearly immune to decay."""
        c = Concept(mind_id=MIND, name="veteran", importance=0.8)
        cid = await concept_repo.create(c)
        for _ in range(100):
            await concept_repo.record_access(cid)

        decay = EbbinghausDecay(concept_repo, relation_repo, decay_rate=0.1)
        await decay.apply_decay(MIND)

        fetched = await concept_repo.get(cid)
        assert fetched is not None
        # decay_factor = 1/(1 + 100*0.1) = 1/11 ≈ 0.09
        # loss = 0.8 * 0.1 * 0.09 = ~0.007
        assert fetched.importance > 0.78

    async def test_relation_decay(
        self,
        concept_repo: ConceptRepository,
        relation_repo: RelationRepository,
    ) -> None:
        """Relations also decay based on co_occurrence_count."""
        ids = await _seed_concepts(concept_repo, "A", "B")
        from sovyx.brain.models import Relation

        rel = Relation(
            source_id=ids[0], target_id=ids[1], weight=0.8
        )
        rid = await relation_repo.create(rel)

        decay = EbbinghausDecay(concept_repo, relation_repo)
        _, relations_decayed = await decay.apply_decay(MIND)
        assert relations_decayed >= 1

        fetched = await relation_repo.get(rid)
        assert fetched is not None
        assert fetched.weight < 0.8

    async def test_prune_weak_concepts(
        self,
        concept_repo: ConceptRepository,
        relation_repo: RelationRepository,
    ) -> None:
        """Concepts below min_strength are pruned."""
        weak = Concept(mind_id=MIND, name="weak", importance=0.005)
        strong = Concept(mind_id=MIND, name="strong", importance=0.9)
        await concept_repo.create(weak)
        cid_strong = await concept_repo.create(strong)

        decay = EbbinghausDecay(
            concept_repo, relation_repo, min_strength=0.01
        )
        concepts_pruned, _ = await decay.prune_weak(MIND)
        assert concepts_pruned == 1

        # Strong concept still exists
        assert await concept_repo.get(cid_strong) is not None
        assert await concept_repo.count(MIND) == 1

    async def test_prune_weak_relations(
        self,
        concept_repo: ConceptRepository,
        relation_repo: RelationRepository,
    ) -> None:
        """Relations below threshold are pruned."""
        ids = await _seed_concepts(concept_repo, "A", "B", "C")
        from sovyx.brain.models import Relation

        weak_rel = Relation(
            source_id=ids[0], target_id=ids[1], weight=0.001
        )
        strong_rel = Relation(
            source_id=ids[0], target_id=ids[2], weight=0.9
        )
        await relation_repo.create(weak_rel)
        rid_strong = await relation_repo.create(strong_rel)

        decay = EbbinghausDecay(
            concept_repo, relation_repo, min_strength=0.05
        )
        _, relations_pruned = await decay.prune_weak(MIND)
        assert relations_pruned == 1

        assert await relation_repo.get(rid_strong) is not None
