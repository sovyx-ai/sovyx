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

    def test_returns_one_migration(self) -> None:
        assert len(get_system_migrations()) == 1

    async def test_applies_without_error(self, pool: DatabasePool) -> None:
        runner = MigrationRunner(pool)
        await runner.initialize()
        applied = await runner.run_migrations(get_system_migrations())
        assert applied == 1

    async def test_tables_created(self, migrated: DatabasePool) -> None:
        async with migrated.read() as conn:
            cursor = await conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            rows = await cursor.fetchall()
            tables = {r[0] for r in rows}
            assert "engine_state" in tables
            assert "persons" in tables
            assert "channel_mappings" in tables

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
