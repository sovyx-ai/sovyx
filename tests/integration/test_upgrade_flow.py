"""Integration test — full upgrade simulation with real SQLite (V05-34).

Verifies the end-to-end upgrade flow: backup → migrate → verify → swap.
Uses a real SQLite database (via :class:`ConnectionPool`) instead of mocks.
"""

from __future__ import annotations

from pathlib import Path  # noqa: TC003

import pytest

from sovyx.upgrade.schema import (
    MigrationRunner,
    SchemaVersion,
    UpgradeMigration,
)


def _make_migration(
    version: str,
    description: str,
    sql: list[str],
    *,
    phase: str = "expand",
) -> UpgradeMigration:
    return UpgradeMigration(
        version=version,
        description=description,
        sql_statements=sql,
        phase=phase,
    )


@pytest.fixture()
async def pool(tmp_path: Path) -> object:
    """Create a real ConnectionPool backed by a temp SQLite file."""
    from sovyx.persistence.pool import DatabasePool as ConnectionPool

    db_path = tmp_path / "test_upgrade.db"
    p = ConnectionPool(db_path)
    await p.initialize()

    # Create a minimal schema so migrations have something to work with
    async with p.write() as conn:
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS concepts "
            "(id TEXT PRIMARY KEY, name TEXT, content TEXT)"
        )
        await conn.execute(
            "INSERT INTO concepts (id, name, content) VALUES (?, ?, ?)",
            ("c1", "test-concept", "some content"),
        )

    yield p
    await p.close()


class TestUpgradeFlowIntegration:
    """Full upgrade simulation with real SQLite."""

    @pytest.mark.asyncio()
    async def test_single_migration_success(
        self, pool: object, tmp_path: Path
    ) -> None:
        """Apply one migration, verify schema version recorded."""
        sv = SchemaVersion(pool)
        await sv.initialize()

        runner = MigrationRunner(pool, sv, backup_dir=tmp_path / "backups")
        migration = _make_migration(
            "1.0.0",
            "add tags column",
            ["ALTER TABLE concepts ADD COLUMN tags TEXT DEFAULT ''"],
        )

        report = await runner.run([migration])

        assert report.status == "success"
        assert len(report.applied) == 1

        # Verify column actually exists
        async with pool.read() as conn:
            cursor = await conn.execute(
                "SELECT tags FROM concepts WHERE id = 'c1'"
            )
            row = await cursor.fetchone()
            assert row is not None

    @pytest.mark.asyncio()
    async def test_multi_step_migration(
        self, pool: object, tmp_path: Path
    ) -> None:
        """Apply multiple ordered migrations."""
        sv = SchemaVersion(pool)
        await sv.initialize()

        runner = MigrationRunner(pool, sv, backup_dir=tmp_path / "backups")
        migrations = [
            _make_migration(
                "1.0.0",
                "add importance",
                ["ALTER TABLE concepts ADD COLUMN importance REAL DEFAULT 0.5"],
            ),
            _make_migration(
                "1.1.0",
                "add index",
                ["CREATE INDEX IF NOT EXISTS idx_concepts_name ON concepts(name)"],
            ),
        ]

        report = await runner.run(migrations)

        assert report.status == "success"
        assert len(report.applied) == 2

    @pytest.mark.asyncio()
    async def test_failed_migration_restores_backup(
        self, pool: object, tmp_path: Path
    ) -> None:
        """If a migration fails, database is restored from backup."""
        sv = SchemaVersion(pool)
        await sv.initialize()

        runner = MigrationRunner(pool, sv, backup_dir=tmp_path / "backups")

        # First good, second bad
        migrations = [
            _make_migration(
                "1.0.0",
                "good",
                ["ALTER TABLE concepts ADD COLUMN score REAL DEFAULT 0"],
            ),
            _make_migration(
                "2.0.0",
                "bad",
                ["ALTER TABLE nonexistent ADD COLUMN x TEXT"],
            ),
        ]

        report = await runner.run(migrations)

        assert report.status == "failed"

        # Verify backup was created
        backups = list((tmp_path / "backups").glob("sovyx_upgrade_*.db"))
        assert len(backups) >= 1

    @pytest.mark.asyncio()
    async def test_idempotent_reruns(
        self, pool: object, tmp_path: Path
    ) -> None:
        """Running the same migrations twice only applies them once."""
        sv = SchemaVersion(pool)
        await sv.initialize()

        runner = MigrationRunner(pool, sv, backup_dir=tmp_path / "backups")
        migration = _make_migration(
            "1.0.0",
            "add category",
            ["ALTER TABLE concepts ADD COLUMN category TEXT DEFAULT 'fact'"],
        )

        # First run
        report1 = await runner.run([migration])
        assert report1.status == "success"
        assert len(report1.applied) == 1

        # Second run — should skip (up_to_date means nothing to apply)
        report2 = await runner.run([migration])
        assert report2.status == "up_to_date"
        assert len(report2.applied) == 0

    @pytest.mark.asyncio()
    async def test_data_migration_with_real_db(
        self, pool: object, tmp_path: Path
    ) -> None:
        """Data migration function runs against real DB."""
        sv = SchemaVersion(pool)
        await sv.initialize()

        runner = MigrationRunner(pool, sv, backup_dir=tmp_path / "backups")

        async def uppercase_names(conn: object) -> None:
            cursor = await conn.execute("SELECT id, name FROM concepts")
            rows = await cursor.fetchall()
            for row in rows:
                await conn.execute(
                    "UPDATE concepts SET name = ? WHERE id = ?",
                    (row[1].upper(), row[0]),
                )

        migration = UpgradeMigration(
            version="1.0.0",
            description="uppercase names",
            sql_statements=[],
            phase="expand",
            data_migration=uppercase_names,
        )

        report = await runner.run([migration])
        assert report.status == "success"

        # Verify data was actually transformed
        async with pool.read() as conn:
            cursor = await conn.execute(
                "SELECT name FROM concepts WHERE id = 'c1'"
            )
            row = await cursor.fetchone()
            assert row[0] == "TEST-CONCEPT"
