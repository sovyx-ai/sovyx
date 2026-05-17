"""Unit tests for AckResurfaceScheduler.

Mission anchor: ``docs-internal/missions/MISSION-c4-degraded-mode-banner-2026-05-17.md``
§Phase 3 §T3.5 + §9.1 row "TTL re-surface scheduler".
"""

from __future__ import annotations

import time
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest

from sovyx.engine._ack_resurface_scheduler import (
    DEFAULT_PRUNE_INTERVAL_S,
    AckResurfaceScheduler,
)
from sovyx.engine._operator_acks_store import OperatorAcksStore
from sovyx.persistence.pool import DatabasePool
from sovyx.persistence.schemas.system import get_system_migrations


@pytest.fixture()
async def store(tmp_path: Path) -> AsyncGenerator[OperatorAcksStore, None]:
    db_path = tmp_path / "system.db"
    pool = DatabasePool(db_path, read_pool_size=1)
    await pool.initialize()
    migrations = get_system_migrations()
    async with pool.write() as conn:
        await conn.execute(
            """CREATE TABLE IF NOT EXISTS _schema (
                 version INTEGER PRIMARY KEY,
                 description TEXT NOT NULL,
                 checksum TEXT NOT NULL,
                 applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                 duration_ms INTEGER
               )""",
        )
        await conn.commit()
        for m in migrations:
            await conn.executescript(m.sql_up)
            await conn.execute(
                "INSERT OR IGNORE INTO _schema (version, description, checksum) VALUES (?, ?, ?)",
                (m.version, m.description, m.checksum),
            )
            await conn.commit()
    yield OperatorAcksStore(pool)
    await pool.close()


class TestSchedulerDefaults:
    def test_default_prune_interval(self) -> None:
        assert DEFAULT_PRUNE_INTERVAL_S == 30.0

    def test_minimum_interval_clamped_to_5s(self) -> None:
        """Operator-supplied interval below 5s clamps to 5s — defense
        against pathological test fixtures."""

        class _Stub:
            async def prune_expired(self) -> list[object]:
                return []

        scheduler = AckResurfaceScheduler(_Stub(), prune_interval_s=0.1)
        assert scheduler._interval_s == 5.0


class TestTickOnce:
    @pytest.mark.asyncio()
    async def test_no_expired_no_op(self, store: OperatorAcksStore) -> None:
        scheduler = AckResurfaceScheduler(store)
        removed = await scheduler.tick_once()
        assert removed == 0

    @pytest.mark.asyncio()
    async def test_active_ack_not_pruned(
        self,
        store: OperatorAcksStore,
    ) -> None:
        await store.record_ack(reason="voice.active", ttl_sec=3600)
        scheduler = AckResurfaceScheduler(store)
        removed = await scheduler.tick_once()
        assert removed == 0

    @pytest.mark.asyncio()
    async def test_expired_ack_pruned(
        self,
        store: OperatorAcksStore,
    ) -> None:
        past_ts = int(time.time()) - 1000
        async with store._pool.write() as conn:
            await conn.execute(
                """INSERT INTO operator_acks
                   (reason, acked_at_ts, ttl_sec, operator_id, metadata)
                   VALUES (?, ?, ?, '', '{}')""",
                ("voice.expired", past_ts, 60),
            )
            await conn.commit()
        scheduler = AckResurfaceScheduler(store)
        removed = await scheduler.tick_once()
        assert removed == 1

    @pytest.mark.asyncio()
    async def test_store_exception_swallowed(self) -> None:
        """A wedged store cannot crash the scheduler — tick_once
        returns 0 + logs."""

        class _Broken:
            async def prune_expired(self) -> list[object]:
                raise RuntimeError("test_injected")

        scheduler = AckResurfaceScheduler(_Broken())
        removed = await scheduler.tick_once()
        assert removed == 0


class TestSchedulerLifecycle:
    @pytest.mark.asyncio()
    async def test_start_stop_cycle(
        self,
        store: OperatorAcksStore,
    ) -> None:
        scheduler = AckResurfaceScheduler(store, prune_interval_s=5.0)
        await scheduler.start()
        assert scheduler._running is True
        await scheduler.stop()
        assert scheduler._running is False

    @pytest.mark.asyncio()
    async def test_double_start_idempotent(
        self,
        store: OperatorAcksStore,
    ) -> None:
        scheduler = AckResurfaceScheduler(store)
        await scheduler.start()
        task_id_first = id(scheduler._task)
        await scheduler.start()
        task_id_second = id(scheduler._task)
        # Second start is a no-op
        assert task_id_first == task_id_second
        await scheduler.stop()

    @pytest.mark.asyncio()
    async def test_shutdown_alias_calls_stop(
        self,
        store: OperatorAcksStore,
    ) -> None:
        scheduler = AckResurfaceScheduler(store)
        await scheduler.start()
        await scheduler.shutdown()
        assert scheduler._running is False
