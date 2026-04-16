"""Tests for sovyx.persistence.schemas.brain — brain database schema."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from sovyx.persistence.migrations import MigrationRunner
from sovyx.persistence.pool import DatabasePool
from sovyx.persistence.schemas.brain import get_brain_migrations

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
async def pool(tmp_path: Path) -> DatabasePool:
    """Initialized pool for schema tests."""
    db_path = tmp_path / "brain.db"
    p = DatabasePool(db_path=db_path, read_pool_size=1)
    await p.initialize()
    yield p  # type: ignore[misc]
    await p.close()


@pytest.fixture
async def migrated_pool(pool: DatabasePool) -> DatabasePool:
    """Pool with all non-vec migrations applied."""
    runner = MigrationRunner(pool)
    await runner.initialize()
    migrations = get_brain_migrations(has_sqlite_vec=False)
    await runner.run_migrations(migrations)
    return pool


class TestGetBrainMigrations:
    """Migration list generation."""

    def test_without_vec_returns_five(self) -> None:
        migrations = get_brain_migrations(has_sqlite_vec=False)
        assert len(migrations) == 5  # noqa: PLR2004
        assert migrations[0].version == 1
        assert migrations[1].version == 3  # noqa: PLR2004  # canonical ordering
        assert migrations[2].version == 4  # noqa: PLR2004  # covering indices
        assert migrations[3].version == 5  # noqa: PLR2004  # conversation_imports
        assert migrations[4].version == 6  # noqa: PLR2004  # PAD 3D (ADR-001)

    def test_with_vec_returns_six(self) -> None:
        migrations = get_brain_migrations(has_sqlite_vec=True)
        assert len(migrations) == 6  # noqa: PLR2004
        assert migrations[0].version == 1
        assert migrations[1].version == 2  # noqa: PLR2004
        assert migrations[2].version == 3  # noqa: PLR2004
        assert migrations[3].version == 4  # noqa: PLR2004
        assert migrations[4].version == 5  # noqa: PLR2004
        assert migrations[5].version == 6  # noqa: PLR2004  # PAD 3D (ADR-001)

    def test_checksums_are_valid(self) -> None:
        from sovyx.persistence.migrations import Migration

        for migration in get_brain_migrations(has_sqlite_vec=True):
            expected = Migration.compute_checksum(migration.sql_up)
            assert migration.checksum == expected

    def test_default_includes_vec_canonical_indices_and_conv_imports(self) -> None:
        migrations = get_brain_migrations()
        assert len(migrations) == 6  # noqa: PLR2004


class TestMigration001:
    """Core tables, indexes, FTS5, triggers."""

    async def test_applies_without_error(self, pool: DatabasePool) -> None:
        runner = MigrationRunner(pool)
        await runner.initialize()
        migrations = get_brain_migrations(has_sqlite_vec=False)
        applied = await runner.run_migrations(migrations)
        assert applied == 5  # noqa: PLR2004  # migrations 1 + 3 + 4 + 5 + 6

    async def test_tables_created(self, migrated_pool: DatabasePool) -> None:
        expected_tables = {
            "concepts",
            "episodes",
            "relations",
            "consolidation_log",
        }
        async with migrated_pool.read() as conn:
            cursor = await conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            rows = await cursor.fetchall()
            tables = {r[0] for r in rows}
            for table in expected_tables:
                assert table in tables, f"Missing table: {table}"

    async def test_indexes_created(self, migrated_pool: DatabasePool) -> None:
        expected_indexes = {
            "idx_concepts_mind",
            "idx_concepts_category",
            "idx_concepts_importance",
            "idx_concepts_accessed",
            "idx_episodes_mind",
            "idx_episodes_conversation",
            "idx_episodes_importance",
            "idx_episodes_created",
            "idx_relations_source",
            "idx_relations_target",
            "idx_relations_weight",
        }
        async with migrated_pool.read() as conn:
            cursor = await conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
            rows = await cursor.fetchall()
            indexes = {r[0] for r in rows}
            for idx in expected_indexes:
                assert idx in indexes, f"Missing index: {idx}"

    async def test_fts5_table_created(self, migrated_pool: DatabasePool) -> None:
        async with migrated_pool.read() as conn:
            cursor = await conn.execute("SELECT name FROM sqlite_master WHERE name='concepts_fts'")
            assert await cursor.fetchone() is not None

    async def test_fts5_insert_trigger(self, migrated_pool: DatabasePool) -> None:
        """INSERT into concepts → searchable via FTS5."""
        async with migrated_pool.write() as conn:
            await conn.execute(
                "INSERT INTO concepts (id, mind_id, name, content) "
                "VALUES ('c1', 'aria', 'quantum physics', 'study of particles')"
            )
            await conn.commit()

        async with migrated_pool.read() as conn:
            cursor = await conn.execute(
                "SELECT name FROM concepts_fts WHERE concepts_fts MATCH 'quantum'"
            )
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] == "quantum physics"

    async def test_fts5_delete_trigger(self, migrated_pool: DatabasePool) -> None:
        """DELETE from concepts → removed from FTS5."""
        async with migrated_pool.write() as conn:
            await conn.execute(
                "INSERT INTO concepts (id, mind_id, name, content) "
                "VALUES ('c1', 'aria', 'test concept', 'test content')"
            )
            await conn.commit()
            await conn.execute("DELETE FROM concepts WHERE id = 'c1'")
            await conn.commit()

        async with migrated_pool.read() as conn:
            cursor = await conn.execute(
                "SELECT name FROM concepts_fts WHERE concepts_fts MATCH 'test'"
            )
            assert await cursor.fetchone() is None

    async def test_fts5_update_trigger(self, migrated_pool: DatabasePool) -> None:
        """UPDATE concepts → FTS5 reflects new values."""
        async with migrated_pool.write() as conn:
            await conn.execute(
                "INSERT INTO concepts (id, mind_id, name, content) "
                "VALUES ('c1', 'aria', 'old name', 'old content')"
            )
            await conn.commit()
            await conn.execute(
                "UPDATE concepts SET name = 'new name', content = 'new content' WHERE id = 'c1'"
            )
            await conn.commit()

        async with migrated_pool.read() as conn:
            # Old name gone
            cursor = await conn.execute(
                "SELECT name FROM concepts_fts WHERE concepts_fts MATCH 'old'"
            )
            assert await cursor.fetchone() is None
            # New name present
            cursor = await conn.execute(
                "SELECT name FROM concepts_fts WHERE concepts_fts MATCH 'new'"
            )
            assert await cursor.fetchone() is not None

    async def test_foreign_key_cascade(self, migrated_pool: DatabasePool) -> None:
        """DELETE concept → CASCADE deletes its relations."""
        async with migrated_pool.write() as conn:
            await conn.execute(
                "INSERT INTO concepts (id, mind_id, name) VALUES ('c1', 'aria', 'concept 1')"
            )
            await conn.execute(
                "INSERT INTO concepts (id, mind_id, name) VALUES ('c2', 'aria', 'concept 2')"
            )
            await conn.execute(
                "INSERT INTO relations (id, source_id, target_id) VALUES ('r1', 'c1', 'c2')"
            )
            await conn.commit()

            # Delete source concept
            await conn.execute("DELETE FROM concepts WHERE id = 'c1'")
            await conn.commit()

        async with migrated_pool.read() as conn:
            cursor = await conn.execute("SELECT COUNT(*) FROM relations WHERE id = 'r1'")
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] == 0

    async def test_consolidation_log_table(self, migrated_pool: DatabasePool) -> None:
        async with migrated_pool.write() as conn:
            await conn.execute("INSERT INTO consolidation_log (mind_id) VALUES ('aria')")
            await conn.commit()

        async with migrated_pool.read() as conn:
            cursor = await conn.execute("SELECT mind_id FROM consolidation_log")
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] == "aria"


class TestMigration002:
    """sqlite-vec embedding tables (conditional)."""

    async def test_vec_tables_created_when_available(self, tmp_path: Path) -> None:
        """If sqlite-vec is loadable, migration 002 creates vec tables."""
        db_path = tmp_path / "brain_vec.db"
        pool = DatabasePool(
            db_path=db_path,
            read_pool_size=1,
            load_extensions=["vec0"],
        )
        await pool.initialize()

        if not pool.has_sqlite_vec:
            await pool.close()
            pytest.skip("sqlite-vec not available")

        runner = MigrationRunner(pool)
        await runner.initialize()
        migrations = get_brain_migrations(has_sqlite_vec=True)
        applied = await runner.run_migrations(migrations)
        assert applied == 6  # noqa: PLR2004  # migrations 1 + 2 + 3 + 4 + 5 + 6

        async with pool.read() as conn:
            for table in ("concept_embeddings", "episode_embeddings"):
                cursor = await conn.execute(
                    "SELECT name FROM sqlite_master WHERE name=?",
                    (table,),
                )
                assert await cursor.fetchone() is not None, f"Missing: {table}"

        await pool.close()

    async def test_without_vec_only_migration_001_003_004_005_006(
        self,
        pool: DatabasePool,
    ) -> None:
        """Without sqlite-vec, migrations 001 + 003 + 004 + 005 + 006 apply (skip 002)."""
        runner = MigrationRunner(pool)
        await runner.initialize()
        migrations = get_brain_migrations(has_sqlite_vec=False)
        applied = await runner.run_migrations(migrations)
        assert applied == 5  # noqa: PLR2004  # migrations 1 + 3 + 4 + 5 + 6
        assert await runner.get_current_version() == 6  # noqa: PLR2004
