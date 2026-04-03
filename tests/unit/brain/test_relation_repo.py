"""Tests for sovyx.brain.relation_repo — relation repository."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from sovyx.brain.models import Relation
from sovyx.brain.relation_repo import RelationRepository
from sovyx.engine.types import ConceptId, MindId, RelationId, RelationType
from sovyx.persistence.migrations import MigrationRunner
from sovyx.persistence.pool import DatabasePool
from sovyx.persistence.schemas.brain import get_brain_migrations

if TYPE_CHECKING:
    from pathlib import Path

MIND = MindId("aria")


@pytest.fixture
async def pool(tmp_path: Path) -> DatabasePool:
    """Pool with brain schema applied + seed concepts."""
    p = DatabasePool(db_path=tmp_path / "brain.db", read_pool_size=1)
    await p.initialize()
    runner = MigrationRunner(p)
    await runner.initialize()
    await runner.run_migrations(get_brain_migrations(has_sqlite_vec=False))

    # Seed concepts for FK constraints
    async with p.write() as conn:
        for cid in ("c1", "c2", "c3", "c4"):
            await conn.execute(
                "INSERT INTO concepts (id, mind_id, name) VALUES (?, ?, ?)",
                (cid, str(MIND), f"concept {cid}"),
            )
        await conn.commit()

    yield p  # type: ignore[misc]
    await p.close()


@pytest.fixture
def repo(pool: DatabasePool) -> RelationRepository:
    return RelationRepository(pool)


def _make_relation(
    source: str = "c1",
    target: str = "c2",
    **kwargs: object,
) -> Relation:
    return Relation(
        source_id=ConceptId(source),
        target_id=ConceptId(target),
        **kwargs,  # type: ignore[arg-type]
    )


class TestCRUD:
    """Basic CRUD operations."""

    async def test_create_and_get(self, repo: RelationRepository) -> None:
        rel = _make_relation()
        rid = await repo.create(rel)
        fetched = await repo.get(rid)
        assert fetched is not None
        assert str(fetched.source_id) == "c1"
        assert str(fetched.target_id) == "c2"

    async def test_get_nonexistent(self, repo: RelationRepository) -> None:
        result = await repo.get(RelationId("nonexistent"))
        assert result is None

    async def test_delete(self, repo: RelationRepository) -> None:
        rel = _make_relation()
        rid = await repo.create(rel)
        await repo.delete(rid)
        assert await repo.get(rid) is None


class TestGetRelationsFor:
    """Relations for a concept."""

    async def test_source_and_target(self, repo: RelationRepository) -> None:
        await repo.create(_make_relation("c1", "c2"))
        await repo.create(_make_relation("c3", "c1"))

        relations = await repo.get_relations_for(ConceptId("c1"))
        assert len(relations) == 2  # noqa: PLR2004

    async def test_no_relations(self, repo: RelationRepository) -> None:
        relations = await repo.get_relations_for(ConceptId("c4"))
        assert relations == []


class TestGetNeighbors:
    """Neighbor discovery."""

    async def test_returns_neighbors_by_weight(self, repo: RelationRepository) -> None:
        await repo.create(_make_relation("c1", "c2", weight=0.9))
        await repo.create(_make_relation("c1", "c3", weight=0.3))

        neighbors = await repo.get_neighbors(ConceptId("c1"))
        assert len(neighbors) == 2  # noqa: PLR2004
        assert neighbors[0][0] == ConceptId("c2")  # higher weight first
        assert neighbors[0][1] == 0.9

    async def test_bidirectional(self, repo: RelationRepository) -> None:
        await repo.create(_make_relation("c2", "c1", weight=0.7))

        neighbors = await repo.get_neighbors(ConceptId("c1"))
        assert len(neighbors) == 1
        assert neighbors[0][0] == ConceptId("c2")

    async def test_respects_limit(self, repo: RelationRepository) -> None:
        await repo.create(_make_relation("c1", "c2", weight=0.9))
        await repo.create(_make_relation("c1", "c3", weight=0.5))

        neighbors = await repo.get_neighbors(ConceptId("c1"), limit=1)
        assert len(neighbors) == 1


class TestUpdateWeight:
    """Weight updates."""

    async def test_update_weight(self, repo: RelationRepository) -> None:
        rel = _make_relation(weight=0.5)
        rid = await repo.create(rel)
        await repo.update_weight(rid, 0.8)
        fetched = await repo.get(rid)
        assert fetched is not None
        assert fetched.weight == 0.8


class TestIncrementCoOccurrence:
    """Co-occurrence tracking."""

    async def test_increments_existing(self, repo: RelationRepository) -> None:
        rel = _make_relation()
        await repo.create(rel)

        await repo.increment_co_occurrence(ConceptId("c1"), ConceptId("c2"))
        relations = await repo.get_relations_for(ConceptId("c1"))
        found = [r for r in relations if str(r.target_id) == "c2"]
        assert len(found) >= 1
        assert found[0].co_occurrence_count == 2  # noqa: PLR2004

    async def test_creates_if_not_exists(self, repo: RelationRepository) -> None:
        await repo.increment_co_occurrence(ConceptId("c3"), ConceptId("c4"))
        relations = await repo.get_relations_for(ConceptId("c3"))
        assert len(relations) == 1
        assert relations[0].weight == 0.3
        assert relations[0].co_occurrence_count == 1


class TestGetOrCreate:
    """Get or create relations."""

    async def test_creates_new(self, repo: RelationRepository) -> None:
        rel = await repo.get_or_create(ConceptId("c1"), ConceptId("c3"))
        assert str(rel.source_id) == "c1"
        assert str(rel.target_id) == "c3"

    async def test_returns_existing(self, repo: RelationRepository) -> None:
        await repo.create(_make_relation("c1", "c2", weight=0.7))
        rel = await repo.get_or_create(ConceptId("c1"), ConceptId("c2"))
        assert rel.weight == 0.7


class TestDeleteWeak:
    """Pruning weak relations."""

    async def test_deletes_below_threshold(self, repo: RelationRepository) -> None:
        await repo.create(_make_relation("c1", "c2", weight=0.01))
        await repo.create(_make_relation("c1", "c3", weight=0.8))

        deleted = await repo.delete_weak(MIND, threshold=0.05)
        assert deleted == 1

        relations = await repo.get_relations_for(ConceptId("c1"))
        assert len(relations) == 1
        assert relations[0].weight == 0.8

    async def test_nothing_to_delete(self, repo: RelationRepository) -> None:
        await repo.create(_make_relation("c1", "c2", weight=0.5))
        deleted = await repo.delete_weak(MIND, threshold=0.05)
        assert deleted == 0


class TestUniqueConstraint:
    """UNIQUE(source_id, target_id, relation_type)."""

    async def test_duplicate_raises(self, repo: RelationRepository) -> None:
        import aiosqlite

        await repo.create(_make_relation("c1", "c2", relation_type=RelationType.RELATED_TO))
        with pytest.raises(aiosqlite.IntegrityError):
            await repo.create(_make_relation("c1", "c2", relation_type=RelationType.RELATED_TO))

    async def test_different_type_ok(self, repo: RelationRepository) -> None:
        await repo.create(_make_relation("c1", "c2", relation_type=RelationType.RELATED_TO))
        r2 = _make_relation("c1", "c2", relation_type=RelationType.CAUSES)
        rid = await repo.create(r2)
        assert await repo.get(rid) is not None
