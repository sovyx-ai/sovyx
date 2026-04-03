"""Tests for sovyx.persistence.manager — database manager."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from sovyx.engine.config import DatabaseConfig, EngineConfig
from sovyx.engine.errors import DatabaseConnectionError
from sovyx.engine.events import EventBus
from sovyx.engine.types import MindId
from sovyx.persistence.manager import DatabaseManager

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def config(tmp_path: Path) -> EngineConfig:
    """Config with temp data dir."""
    return EngineConfig(database=DatabaseConfig(data_dir=tmp_path / "data"))


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
async def manager(config: EngineConfig, bus: EventBus) -> DatabaseManager:
    """Started database manager."""
    mgr = DatabaseManager(config, bus)
    await mgr.start()
    yield mgr  # type: ignore[misc]
    await mgr.stop()


class TestLifecycle:
    """Start/stop lifecycle."""

    async def test_start_creates_data_dir(self, config: EngineConfig, bus: EventBus) -> None:
        mgr = DatabaseManager(config, bus)
        await mgr.start()
        assert config.database.data_dir.exists()
        assert mgr.is_running is True
        await mgr.stop()

    async def test_stop_clears_state(self, config: EngineConfig, bus: EventBus) -> None:
        mgr = DatabaseManager(config, bus)
        await mgr.start()
        await mgr.stop()
        assert mgr.is_running is False

    async def test_system_db_created(self, manager: DatabaseManager) -> None:
        pool = manager.get_system_pool()
        assert pool.is_initialized is True

    async def test_system_db_has_schema(self, manager: DatabaseManager) -> None:
        pool = manager.get_system_pool()
        async with pool.read() as conn:
            cursor = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='engine_state'"
            )
            assert await cursor.fetchone() is not None


class TestMindDatabases:
    """Per-mind database initialization."""

    async def test_initialize_mind(self, manager: DatabaseManager) -> None:
        mind_id = MindId("aria")
        await manager.initialize_mind_databases(mind_id)

        brain = manager.get_brain_pool(mind_id)
        assert brain.is_initialized is True

        conv = manager.get_conversation_pool(mind_id)
        assert conv.is_initialized is True

    async def test_brain_has_schema(self, manager: DatabaseManager) -> None:
        mind_id = MindId("aria")
        await manager.initialize_mind_databases(mind_id)
        pool = manager.get_brain_pool(mind_id)

        async with pool.read() as conn:
            cursor = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='concepts'"
            )
            assert await cursor.fetchone() is not None

    async def test_conversation_has_schema(self, manager: DatabaseManager) -> None:
        mind_id = MindId("aria")
        await manager.initialize_mind_databases(mind_id)
        pool = manager.get_conversation_pool(mind_id)

        async with pool.read() as conn:
            cursor = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='conversations'"
            )
            assert await cursor.fetchone() is not None

    async def test_multiple_minds_isolated(self, manager: DatabaseManager) -> None:
        """Each mind gets its own databases."""
        mind_a = MindId("aria")
        mind_b = MindId("nova")
        await manager.initialize_mind_databases(mind_a)
        await manager.initialize_mind_databases(mind_b)

        pool_a = manager.get_brain_pool(mind_a)
        pool_b = manager.get_brain_pool(mind_b)
        assert pool_a is not pool_b

        # Insert in A, verify not in B
        async with pool_a.write() as conn:
            await conn.execute(
                "INSERT INTO concepts (id, mind_id, name) VALUES ('c1', 'aria', 'test concept')"
            )
            await conn.commit()

        async with pool_b.read() as conn:
            cursor = await conn.execute("SELECT COUNT(*) FROM concepts")
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] == 0

    async def test_has_sqlite_vec_property(self, manager: DatabaseManager) -> None:
        # No brain pools yet
        assert manager.has_sqlite_vec is False

        mind_id = MindId("aria")
        await manager.initialize_mind_databases(mind_id)
        # Result depends on sqlite-vec availability
        assert isinstance(manager.has_sqlite_vec, bool)

    async def test_stop_closes_mind_pools(self, config: EngineConfig, bus: EventBus) -> None:
        mgr = DatabaseManager(config, bus)
        await mgr.start()
        await mgr.initialize_mind_databases(MindId("aria"))
        await mgr.stop()
        assert mgr.is_running is False


class TestErrors:
    """Error handling."""

    def test_get_system_pool_not_started(self, config: EngineConfig, bus: EventBus) -> None:
        mgr = DatabaseManager(config, bus)
        with pytest.raises(DatabaseConnectionError):
            mgr.get_system_pool()

    def test_get_brain_pool_not_initialized(self, manager: DatabaseManager) -> None:
        with pytest.raises(DatabaseConnectionError):
            manager.get_brain_pool(MindId("nonexistent"))

    def test_get_conversation_pool_not_initialized(self, manager: DatabaseManager) -> None:
        with pytest.raises(DatabaseConnectionError):
            manager.get_conversation_pool(MindId("nonexistent"))
