"""Tests for sovyx.persistence.schemas.system."""

from __future__ import annotations

from typing import TYPE_CHECKING

import aiosqlite
import pytest

from sovyx.persistence.migrations import MigrationRunner
from sovyx.persistence.pool import DatabasePool
from sovyx.persistence.schemas.system import get_system_migrations

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
async def pool(tmp_path: Path) -> DatabasePool:
    """Initialized pool."""
    p = DatabasePool(db_path=tmp_path / "system.db", read_pool_size=1)
    await p.initialize()
    yield p  # type: ignore[misc]
    await p.close()


@pytest.fixture
async def migrated(pool: DatabasePool) -> DatabasePool:
    """Pool with system schema applied."""
    runner = MigrationRunner(pool)
    await runner.initialize()
    await runner.run_migrations(get_system_migrations())
    return pool


class TestSystemMigrations:
    """System schema tests."""

    def test_returns_two_migrations(self) -> None:
        migrations = get_system_migrations()
        assert len(migrations) == 2
        assert migrations[0].version == 1
        assert migrations[1].version == 2

    async def test_applies_without_error(self, pool: DatabasePool) -> None:
        runner = MigrationRunner(pool)
        await runner.initialize()
        applied = await runner.run_migrations(get_system_migrations())
        assert applied == 2

    async def test_tables_created(self, migrated: DatabasePool) -> None:
        async with migrated.read() as conn:
            cursor = await conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            rows = await cursor.fetchall()
            tables = {r[0] for r in rows}
            assert "engine_state" in tables
            assert "persons" in tables
            assert "channel_mappings" in tables
            assert "daily_stats" in tables

    async def test_engine_state_crud(self, migrated: DatabasePool) -> None:
        """Key-value store for engine state."""
        async with migrated.write() as conn:
            await conn.execute("INSERT INTO engine_state (key, value) VALUES ('version', '0.1.0')")
            await conn.commit()

        async with migrated.read() as conn:
            cursor = await conn.execute("SELECT value FROM engine_state WHERE key = 'version'")
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] == "0.1.0"

        # Update
        async with migrated.write() as conn:
            await conn.execute("UPDATE engine_state SET value = '0.2.0' WHERE key = 'version'")
            await conn.commit()

        async with migrated.read() as conn:
            cursor = await conn.execute("SELECT value FROM engine_state WHERE key = 'version'")
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] == "0.2.0"

    async def test_persons_crud(self, migrated: DatabasePool) -> None:
        async with migrated.write() as conn:
            await conn.execute(
                "INSERT INTO persons (id, name, display_name) VALUES ('p1', 'Renan', 'Guipe')"
            )
            await conn.commit()

        async with migrated.read() as conn:
            cursor = await conn.execute("SELECT name, display_name FROM persons WHERE id = 'p1'")
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] == "Renan"
            assert row[1] == "Guipe"

    async def test_channel_mapping_unique_constraint(self, migrated: DatabasePool) -> None:
        """Same channel_type + channel_user_id → IntegrityError."""
        async with migrated.write() as conn:
            await conn.execute("INSERT INTO persons (id, name) VALUES ('p1', 'Person 1')")
            await conn.execute("INSERT INTO persons (id, name) VALUES ('p2', 'Person 2')")
            await conn.execute(
                "INSERT INTO channel_mappings "
                "(id, person_id, channel_type, channel_user_id) "
                "VALUES ('m1', 'p1', 'telegram', '12345')"
            )
            await conn.commit()

            with pytest.raises(aiosqlite.IntegrityError):
                await conn.execute(
                    "INSERT INTO channel_mappings "
                    "(id, person_id, channel_type, channel_user_id) "
                    "VALUES ('m2', 'p2', 'telegram', '12345')"
                )

    async def test_cascade_delete_person(self, migrated: DatabasePool) -> None:
        """DELETE person → CASCADE deletes channel_mappings."""
        async with migrated.write() as conn:
            await conn.execute("INSERT INTO persons (id, name) VALUES ('p1', 'Test')")
            await conn.execute(
                "INSERT INTO channel_mappings "
                "(id, person_id, channel_type, channel_user_id) "
                "VALUES ('m1', 'p1', 'telegram', '99999')"
            )
            await conn.commit()
            await conn.execute("DELETE FROM persons WHERE id = 'p1'")
            await conn.commit()

        async with migrated.read() as conn:
            cursor = await conn.execute("SELECT COUNT(*) FROM channel_mappings")
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] == 0

    async def test_indexes_created(self, migrated: DatabasePool) -> None:
        async with migrated.read() as conn:
            cursor = await conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
            rows = await cursor.fetchall()
            indexes = {r[0] for r in rows}
            assert "idx_mapping_person" in indexes
            assert "idx_mapping_channel" in indexes
            assert "idx_daily_stats_date" in indexes


class TestDailyStatsMigration:
    """Migration v2 — daily_stats table tests."""

    async def test_daily_stats_columns(self, migrated: DatabasePool) -> None:
        """All expected columns exist with correct defaults."""
        async with migrated.read() as conn:
            cursor = await conn.execute("PRAGMA table_info(daily_stats)")
            rows = await cursor.fetchall()
            columns = {r[1]: {"type": r[2], "notnull": r[3], "default": r[4]} for r in rows}

        assert "date" in columns
        assert "mind_id" in columns
        assert "messages" in columns
        assert "llm_calls" in columns
        assert "tokens" in columns
        assert "cost_usd" in columns
        assert "cost_by_provider" in columns
        assert "cost_by_model" in columns
        assert "conversations" in columns

        # Check defaults
        assert columns["mind_id"]["default"] == "'aria'"
        assert columns["messages"]["default"] == "0"
        assert columns["cost_usd"]["default"] == "0.0"
        assert columns["cost_by_provider"]["default"] == "'{}'"
        assert columns["cost_by_model"]["default"] == "'{}'"

    async def test_daily_stats_primary_key(self, migrated: DatabasePool) -> None:
        """Primary key is (date, mind_id) — rejects duplicates."""
        async with migrated.write() as conn:
            await conn.execute(
                "INSERT INTO daily_stats (date, mind_id, cost_usd) "
                "VALUES ('2026-04-10', 'aria', 1.50)"
            )
            await conn.commit()

            with pytest.raises(aiosqlite.IntegrityError):
                await conn.execute(
                    "INSERT INTO daily_stats (date, mind_id, cost_usd) "
                    "VALUES ('2026-04-10', 'aria', 2.00)"
                )

    async def test_daily_stats_upsert(self, migrated: DatabasePool) -> None:
        """INSERT OR REPLACE updates existing row (same date+mind_id)."""
        async with migrated.write() as conn:
            await conn.execute(
                "INSERT INTO daily_stats (date, mind_id, messages, cost_usd) "
                "VALUES ('2026-04-10', 'aria', 10, 1.50)"
            )
            await conn.commit()

            # Upsert with new values
            await conn.execute(
                "INSERT OR REPLACE INTO daily_stats "
                "(date, mind_id, messages, cost_usd) "
                "VALUES ('2026-04-10', 'aria', 20, 3.00)"
            )
            await conn.commit()

        async with migrated.read() as conn:
            cursor = await conn.execute(
                "SELECT messages, cost_usd FROM daily_stats "
                "WHERE date = '2026-04-10' AND mind_id = 'aria'"
            )
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] == 20
            assert row[1] == pytest.approx(3.00)

    async def test_daily_stats_multi_mind(self, migrated: DatabasePool) -> None:
        """Different mind_ids coexist for the same date."""
        async with migrated.write() as conn:
            await conn.execute(
                "INSERT INTO daily_stats (date, mind_id, cost_usd) "
                "VALUES ('2026-04-10', 'aria', 1.00)"
            )
            await conn.execute(
                "INSERT INTO daily_stats (date, mind_id, cost_usd) "
                "VALUES ('2026-04-10', 'bob', 2.00)"
            )
            await conn.commit()

        async with migrated.read() as conn:
            cursor = await conn.execute(
                "SELECT COUNT(*) FROM daily_stats WHERE date = '2026-04-10'"
            )
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] == 2

    async def test_migration_idempotent(self, pool: DatabasePool) -> None:
        """Running migrations twice applies nothing the second time."""
        runner = MigrationRunner(pool)
        await runner.initialize()
        first = await runner.run_migrations(get_system_migrations())
        second = await runner.run_migrations(get_system_migrations())
        assert first == 2
        assert second == 0

    async def test_v1_data_survives_v2_migration(self, pool: DatabasePool) -> None:
        """Existing v1 data is preserved when v2 migration runs."""
        runner = MigrationRunner(pool)
        await runner.initialize()

        # Apply only v1
        migrations = get_system_migrations()
        await runner.run_migrations([migrations[0]])

        # Insert v1 data
        async with pool.write() as conn:
            await conn.execute(
                "INSERT INTO engine_state (key, value) VALUES ('test', 'preserved')"
            )
            await conn.execute("INSERT INTO persons (id, name) VALUES ('p1', 'TestUser')")
            await conn.commit()

        # Now apply v2
        applied = await runner.run_migrations(migrations)
        assert applied == 1  # only v2

        # Verify v1 data survived
        async with pool.read() as conn:
            cursor = await conn.execute("SELECT value FROM engine_state WHERE key = 'test'")
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] == "preserved"

            cursor = await conn.execute("SELECT name FROM persons WHERE id = 'p1'")
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] == "TestUser"

            # And daily_stats exists
            cursor = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='daily_stats'"
            )
            assert await cursor.fetchone() is not None

    async def test_daily_stats_date_index_query(self, migrated: DatabasePool) -> None:
        """Index on date enables efficient range queries."""
        async with migrated.write() as conn:
            for day in range(1, 31):
                await conn.execute(
                    "INSERT INTO daily_stats (date, mind_id, cost_usd) VALUES (?, 'aria', ?)",
                    (f"2026-04-{day:02d}", day * 0.10),
                )
            await conn.commit()

        async with migrated.read() as conn:
            # Query last 7 days
            cursor = await conn.execute(
                "SELECT date, cost_usd FROM daily_stats WHERE date >= '2026-04-24' ORDER BY date"
            )
            rows = await cursor.fetchall()
            assert len(rows) == 7
            assert rows[0][0] == "2026-04-24"
            assert rows[-1][0] == "2026-04-30"
