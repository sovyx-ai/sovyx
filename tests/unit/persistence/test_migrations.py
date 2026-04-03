"""Tests for sovyx.persistence.migrations — migration framework."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from sovyx.engine.errors import MigrationError
from sovyx.persistence.migrations import Migration, MigrationRunner
from sovyx.persistence.pool import DatabasePool


@pytest.fixture
async def pool(tmp_path: Path) -> DatabasePool:
    """Initialized pool for migration tests."""
    db_path = tmp_path / "test.db"
    p = DatabasePool(db_path=db_path, read_pool_size=1)
    await p.initialize()
    yield p  # type: ignore[misc]
    await p.close()


@pytest.fixture
def sample_migrations() -> list[Migration]:
    """Two simple migrations for testing."""
    sql1 = "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL);"
    sql2 = (
        "CREATE TABLE posts (id INTEGER PRIMARY KEY, "
        "user_id INTEGER REFERENCES users(id), title TEXT);"
    )
    return [
        Migration(
            version=1,
            description="create users table",
            sql_up=sql1,
            checksum=Migration.compute_checksum(sql1),
        ),
        Migration(
            version=2,
            description="create posts table",
            sql_up=sql2,
            checksum=Migration.compute_checksum(sql2),
        ),
    ]


class TestMigration:
    """Migration dataclass."""

    def test_compute_checksum(self) -> None:
        sql = "CREATE TABLE test (id INTEGER);"
        checksum = Migration.compute_checksum(sql)
        assert len(checksum) == 64  # SHA-256 hex
        assert checksum == Migration.compute_checksum(sql)  # deterministic

    def test_different_sql_different_checksum(self) -> None:
        c1 = Migration.compute_checksum("CREATE TABLE a (id INTEGER);")
        c2 = Migration.compute_checksum("CREATE TABLE b (id INTEGER);")
        assert c1 != c2

    def test_frozen(self) -> None:
        sql = "SELECT 1;"
        m = Migration(
            version=1,
            description="test",
            sql_up=sql,
            checksum=Migration.compute_checksum(sql),
        )
        with pytest.raises(AttributeError):
            m.version = 2  # type: ignore[misc]


class TestMigrationRunner:
    """MigrationRunner functionality."""

    async def test_initialize_creates_schema_table(self, pool: DatabasePool) -> None:
        runner = MigrationRunner(pool)
        await runner.initialize()

        async with pool.read() as conn:
            cursor = await conn.execute("SELECT name FROM sqlite_master WHERE name='_schema'")
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] == "_schema"

    async def test_current_version_empty_db(self, pool: DatabasePool) -> None:
        runner = MigrationRunner(pool)
        await runner.initialize()
        assert await runner.get_current_version() == 0

    async def test_first_run_applies_all(
        self,
        pool: DatabasePool,
        sample_migrations: list[Migration],
    ) -> None:
        runner = MigrationRunner(pool)
        await runner.initialize()
        applied = await runner.run_migrations(sample_migrations)
        assert applied == 2  # noqa: PLR2004
        assert await runner.get_current_version() == 2  # noqa: PLR2004

        # Verify tables exist
        async with pool.read() as conn:
            cursor = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='users'"
            )
            assert await cursor.fetchone() is not None

    async def test_idempotent(
        self,
        pool: DatabasePool,
        sample_migrations: list[Migration],
    ) -> None:
        """Second run applies nothing."""
        runner = MigrationRunner(pool)
        await runner.initialize()

        applied1 = await runner.run_migrations(sample_migrations)
        assert applied1 == 2  # noqa: PLR2004

        applied2 = await runner.run_migrations(sample_migrations)
        assert applied2 == 0

    async def test_incremental(
        self,
        pool: DatabasePool,
        sample_migrations: list[Migration],
    ) -> None:
        """Applying one, then both, applies only the new one."""
        runner = MigrationRunner(pool)
        await runner.initialize()

        applied1 = await runner.run_migrations(sample_migrations[:1])
        assert applied1 == 1
        assert await runner.get_current_version() == 1

        applied2 = await runner.run_migrations(sample_migrations)
        assert applied2 == 1
        assert await runner.get_current_version() == 2  # noqa: PLR2004

    async def test_migrations_apply_in_order(
        self,
        pool: DatabasePool,
    ) -> None:
        """Migrations apply in version order regardless of input order."""
        sql1 = "CREATE TABLE first (id INTEGER PRIMARY KEY);"
        sql2 = "CREATE TABLE second (id INTEGER PRIMARY KEY);"
        migrations = [
            Migration(2, "second", sql2, Migration.compute_checksum(sql2)),
            Migration(1, "first", sql1, Migration.compute_checksum(sql1)),
        ]

        runner = MigrationRunner(pool)
        await runner.initialize()
        applied = await runner.run_migrations(migrations)
        assert applied == 2  # noqa: PLR2004

        # Both tables should exist
        async with pool.read() as conn:
            for table in ("first", "second"):
                cursor = await conn.execute(
                    "SELECT name FROM sqlite_master WHERE name=?", (table,)
                )
                assert await cursor.fetchone() is not None

    async def test_checksum_mismatch_raises(
        self,
        pool: DatabasePool,
        sample_migrations: list[Migration],
    ) -> None:
        runner = MigrationRunner(pool)
        await runner.initialize()
        await runner.run_migrations(sample_migrations)

        # Tamper with migration 1's checksum
        tampered = [
            Migration(
                version=1,
                description="create users table",
                sql_up=sample_migrations[0].sql_up,
                checksum="bad_checksum",
            ),
            sample_migrations[1],
        ]

        with pytest.raises(MigrationError, match="checksum mismatch"):
            await runner.run_migrations(tampered)

    async def test_invalid_sql_raises(self, pool: DatabasePool) -> None:
        bad_sql = "THIS IS NOT VALID SQL;"
        migration = Migration(
            version=1,
            description="bad migration",
            sql_up=bad_sql,
            checksum=Migration.compute_checksum(bad_sql),
        )

        runner = MigrationRunner(pool)
        await runner.initialize()

        with pytest.raises(MigrationError, match="failed"):
            await runner.run_migrations([migration])

        # Version should still be 0 (rolled back)
        assert await runner.get_current_version() == 0

    async def test_verify_integrity_passes(
        self,
        pool: DatabasePool,
        sample_migrations: list[Migration],
    ) -> None:
        runner = MigrationRunner(pool)
        await runner.initialize()
        await runner.run_migrations(sample_migrations)

        assert await runner.verify_integrity(sample_migrations) is True

    async def test_verify_integrity_fails_on_tamper(
        self,
        pool: DatabasePool,
        sample_migrations: list[Migration],
    ) -> None:
        runner = MigrationRunner(pool)
        await runner.initialize()
        await runner.run_migrations(sample_migrations)

        tampered = [
            Migration(1, "tampered", "SELECT 1;", "bad_checksum"),
            sample_migrations[1],
        ]
        assert await runner.verify_integrity(tampered) is False

    async def test_verify_integrity_unapplied_ignored(
        self,
        pool: DatabasePool,
    ) -> None:
        """Unapplied migrations don't fail integrity check."""
        sql = "CREATE TABLE x (id INTEGER);"
        migrations = [
            Migration(1, "test", sql, Migration.compute_checksum(sql)),
        ]
        runner = MigrationRunner(pool)
        await runner.initialize()

        assert await runner.verify_integrity(migrations) is True

    async def test_schema_table_has_duration(
        self,
        pool: DatabasePool,
        sample_migrations: list[Migration],
    ) -> None:
        """Applied migrations record duration_ms."""
        runner = MigrationRunner(pool)
        await runner.initialize()
        await runner.run_migrations(sample_migrations)

        async with pool.read() as conn:
            cursor = await conn.execute("SELECT duration_ms FROM _schema WHERE version = 1")
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] >= 0
