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


class TestCanonicalOrdering:
    """Canonical ordering: min(source, target) as source — no bidirectional duplicates."""

    async def test_create_canonicalizes(self, repo: RelationRepository) -> None:
        """create(B, A) stores as (A, B) when A < B."""
        rel = _make_relation("c2", "c1")  # c2 > c1 → should flip
        rid = await repo.create(rel)
        fetched = await repo.get(rid)
        assert fetched is not None
        assert str(fetched.source_id) == "c1"  # canonical: min first
        assert str(fetched.target_id) == "c2"

    async def test_get_or_create_dedup(self, repo: RelationRepository) -> None:
        """get_or_create(A, B) then get_or_create(B, A) → same row."""
        r1 = await repo.get_or_create(ConceptId("c1"), ConceptId("c2"))
        r2 = await repo.get_or_create(ConceptId("c2"), ConceptId("c1"))
        assert str(r1.id) == str(r2.id)

    async def test_get_or_create_reverse_finds_existing(self, repo: RelationRepository) -> None:
        """Creating (A,B) then querying (B,A) returns the existing relation."""
        await repo.create(_make_relation("c1", "c3", weight=0.7))
        rel = await repo.get_or_create(ConceptId("c3"), ConceptId("c1"))
        assert rel.weight == 0.7

    async def test_increment_canonical(self, repo: RelationRepository) -> None:
        """increment_co_occurrence(B, A) updates (A, B) row."""
        await repo.create(_make_relation("c1", "c2"))
        # Increment with reversed order
        await repo.increment_co_occurrence(ConceptId("c2"), ConceptId("c1"))
        relations = await repo.get_relations_for(ConceptId("c1"))
        found = [r for r in relations if str(r.source_id) == "c1" and str(r.target_id) == "c2"]
        assert len(found) == 1
        assert found[0].co_occurrence_count == 2  # noqa: PLR2004

    async def test_increment_creates_canonical(self, repo: RelationRepository) -> None:
        """increment_co_occurrence(B, A) with no existing row creates (A, B)."""
        await repo.increment_co_occurrence(ConceptId("c4"), ConceptId("c3"))
        relations = await repo.get_relations_for(ConceptId("c3"))
        assert len(relations) == 1
        assert str(relations[0].source_id) == "c3"  # c3 < c4, canonical
        assert str(relations[0].target_id) == "c4"

    async def test_reverse_create_then_get_or_create(self, repo: RelationRepository) -> None:
        """create(B, A) canonicalizes; get_or_create(A, B) finds it."""
        rel = _make_relation("c3", "c1", weight=0.6)
        rid = await repo.create(rel)
        found = await repo.get_or_create(ConceptId("c1"), ConceptId("c3"))
        assert str(found.id) == str(rid)
        assert found.weight == 0.6

    async def test_duplicate_via_reverse_raises(self, repo: RelationRepository) -> None:
        """create(A, B) then create(B, A) raises — both canonicalize to same key."""
        import aiosqlite

        await repo.create(_make_relation("c1", "c2"))
        with pytest.raises(aiosqlite.IntegrityError):
            await repo.create(_make_relation("c2", "c1"))


class TestMigrationV3MergesDuplicates:
    """Migration v3: merge pre-existing bidirectional duplicates."""

    async def test_migration_merges_duplicates(self, tmp_path: Path) -> None:
        """Insert A→B and B→A manually, run migration v3, verify single row."""
        p = DatabasePool(db_path=tmp_path / "brain_mig.db", read_pool_size=1)
        await p.initialize()
        runner = MigrationRunner(p)
        await runner.initialize()

        # Apply only migrations 1+2 (pre-canonical)
        await runner.run_migrations(get_brain_migrations(has_sqlite_vec=False)[:1])

        # Seed concepts
        async with p.write() as conn:
            for cid in ("c1", "c2", "c3"):
                await conn.execute(
                    "INSERT INTO concepts (id, mind_id, name) VALUES (?, ?, ?)",
                    (cid, "aria", f"concept {cid}"),
                )
            # Insert bidirectional duplicates manually (pre-canonical data)
            await conn.execute(
                "INSERT INTO relations (id, source_id, target_id, relation_type, "
                "weight, co_occurrence_count, last_activated, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                ("r1", "c1", "c2", "related_to", 0.5, 3),
            )
            await conn.execute(
                "INSERT INTO relations (id, source_id, target_id, relation_type, "
                "weight, co_occurrence_count, last_activated, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                ("r2", "c2", "c1", "related_to", 0.7, 2),
            )
            # A non-canonical single row (no counterpart) — should be flipped
            await conn.execute(
                "INSERT INTO relations (id, source_id, target_id, relation_type, "
                "weight, co_occurrence_count, last_activated, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                ("r3", "c3", "c1", "related_to", 0.4, 1),
            )
            await conn.commit()

        # Verify 3 rows before migration
        async with p.read() as conn:
            cursor = await conn.execute("SELECT COUNT(*) FROM relations")
            assert (await cursor.fetchone())[0] == 3  # noqa: PLR2004

        # Run migration v3
        all_migs = get_brain_migrations(has_sqlite_vec=False)
        mig3 = [m for m in all_migs if m.version == 3]
        assert len(mig3) == 1
        await runner.run_migrations(mig3)

        # Verify: 2 rows remain (merged + flipped)
        async with p.read() as conn:
            cursor = await conn.execute(
                "SELECT source_id, target_id, weight, co_occurrence_count "
                "FROM relations ORDER BY source_id, target_id"
            )
            rows = await cursor.fetchall()

        assert len(rows) == 2  # noqa: PLR2004

        # Row 1: c1→c2 merged (co_occurrence: 3+2=5, weight: max(0.5, 0.7)=0.7)
        assert rows[0][0] == "c1"
        assert rows[0][1] == "c2"
        assert rows[0][2] == 0.7
        assert rows[0][3] == 5  # noqa: PLR2004

        # Row 2: c3→c1 flipped to c1→c3 (canonical)
        assert rows[1][0] == "c1"
        assert rows[1][1] == "c3"
        assert rows[1][2] == 0.4
        assert rows[1][3] == 1

        await p.close()

    async def test_migration_idempotent_on_clean_data(self, tmp_path: Path) -> None:
        """Migration v3 on data with no duplicates is a no-op."""
        p = DatabasePool(db_path=tmp_path / "brain_clean.db", read_pool_size=1)
        await p.initialize()
        runner = MigrationRunner(p)
        await runner.initialize()

        # Apply only migration 1
        await runner.run_migrations(get_brain_migrations(has_sqlite_vec=False)[:1])

        # Seed concepts + one canonical relation
        async with p.write() as conn:
            for cid in ("c1", "c2"):
                await conn.execute(
                    "INSERT INTO concepts (id, mind_id, name) VALUES (?, ?, ?)",
                    (cid, "aria", f"concept {cid}"),
                )
            await conn.execute(
                "INSERT INTO relations (id, source_id, target_id, relation_type, "
                "weight, co_occurrence_count, last_activated, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                ("r1", "c1", "c2", "related_to", 0.5, 3),
            )
            await conn.commit()

        # Run migration v3
        all_migs = get_brain_migrations(has_sqlite_vec=False)
        mig3 = [m for m in all_migs if m.version == 3]
        await runner.run_migrations(mig3)

        # Verify unchanged
        async with p.read() as conn:
            cursor = await conn.execute(
                "SELECT source_id, target_id, weight, co_occurrence_count FROM relations"
            )
            rows = await cursor.fetchall()

        assert len(rows) == 1
        assert rows[0][0] == "c1"
        assert rows[0][1] == "c2"
        assert rows[0][2] == 0.5
        assert rows[0][3] == 3  # noqa: PLR2004

        await p.close()
