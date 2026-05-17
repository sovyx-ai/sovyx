"""Unit tests for OperatorAcksStore.

Mission anchor: ``docs-internal/missions/MISSION-c4-degraded-mode-banner-2026-05-17.md``
§Phase 3 §T3.2 + §9.1 row "OperatorAcksStore".

Exercises the persistent ack store against a fresh sqlite database
provisioned with Migration 003. Verifies upsert semantics, TTL math,
list-active filtering, prune-expired semantics, and metadata JSON
round-trip.
"""

from __future__ import annotations

import time
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest

from sovyx.engine._operator_acks_store import (
    AckRecord,
    OperatorAcksStore,
    _row_to_record,
)
from sovyx.persistence.pool import DatabasePool
from sovyx.persistence.schemas.system import get_system_migrations


@pytest.fixture()
async def system_pool(tmp_path: Path) -> AsyncGenerator[DatabasePool, None]:
    """Provision a fresh system.db with Migration 003 applied."""
    db_path = tmp_path / "system.db"
    pool = DatabasePool(db_path, read_pool_size=1)
    await pool.initialize()
    # Run migrations manually (engine bootstrap wires the
    # MigrationRunner; for unit tests we apply directly).
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
            cursor = await conn.execute(
                "SELECT version FROM _schema WHERE version = ?",
                (m.version,),
            )
            row = await cursor.fetchone()
            if row is None:
                await conn.executescript(m.sql_up)
                await conn.execute(
                    "INSERT INTO _schema (version, description, checksum) VALUES (?, ?, ?)",
                    (m.version, m.description, m.checksum),
                )
                await conn.commit()
    yield pool
    await pool.close()


@pytest.fixture()
async def store(system_pool: DatabasePool) -> OperatorAcksStore:
    return OperatorAcksStore(system_pool)


class TestRecordAck:
    @pytest.mark.asyncio()
    async def test_record_inserts_new_ack(self, store: OperatorAcksStore) -> None:
        record = await store.record_ack(reason="voice.test", ttl_sec=120)
        assert record.reason == "voice.test"
        assert record.ttl_sec == 120
        assert record.operator_id == ""
        assert record.acked_at_ts > 0

    @pytest.mark.asyncio()
    async def test_record_upsert_replaces_existing(
        self,
        store: OperatorAcksStore,
    ) -> None:
        await store.record_ack(reason="voice.test", ttl_sec=120)
        time.sleep(1.0)  # ensure acked_at_ts changes
        second = await store.record_ack(reason="voice.test", ttl_sec=240)
        assert second.ttl_sec == 240
        listed = await store.list_active_acks()
        assert len(listed) == 1
        assert listed[0].ttl_sec == 240

    @pytest.mark.asyncio()
    async def test_metadata_round_trips_as_json(
        self,
        store: OperatorAcksStore,
    ) -> None:
        await store.record_ack(
            reason="voice.test",
            ttl_sec=120,
            metadata={"candidates_unreachable": ["a", "b"], "count": 2},
        )
        retrieved = await store.get_ack("voice.test")
        assert retrieved is not None
        assert retrieved.metadata == {
            "candidates_unreachable": ["a", "b"],
            "count": 2,
        }


class TestGetAck:
    @pytest.mark.asyncio()
    async def test_returns_none_when_absent(
        self,
        store: OperatorAcksStore,
    ) -> None:
        assert await store.get_ack("nonexistent") is None

    @pytest.mark.asyncio()
    async def test_returns_record_when_active(
        self,
        store: OperatorAcksStore,
    ) -> None:
        await store.record_ack(reason="voice.test", ttl_sec=3600)
        retrieved = await store.get_ack("voice.test")
        assert retrieved is not None
        assert retrieved.reason == "voice.test"

    @pytest.mark.asyncio()
    async def test_returns_none_when_expired(
        self,
        store: OperatorAcksStore,
        system_pool: DatabasePool,
    ) -> None:
        """Direct DB insert with acked_at_ts in the past so the TTL is
        already exhausted — get_ack MUST return None."""
        past_ts = int(time.time()) - 1000
        async with system_pool.write() as conn:
            await conn.execute(
                """INSERT INTO operator_acks
                   (reason, acked_at_ts, ttl_sec, operator_id, metadata)
                   VALUES (?, ?, ?, '', '{}')""",
                ("voice.expired", past_ts, 60),
            )
            await conn.commit()
        assert await store.get_ack("voice.expired") is None


class TestClearAck:
    @pytest.mark.asyncio()
    async def test_clear_returns_true_when_present(
        self,
        store: OperatorAcksStore,
    ) -> None:
        await store.record_ack(reason="voice.test", ttl_sec=120)
        assert await store.clear_ack("voice.test") is True
        assert await store.get_ack("voice.test") is None

    @pytest.mark.asyncio()
    async def test_clear_returns_false_when_absent(
        self,
        store: OperatorAcksStore,
    ) -> None:
        assert await store.clear_ack("nonexistent") is False


class TestListActiveAcks:
    @pytest.mark.asyncio()
    async def test_empty_store_returns_empty_list(
        self,
        store: OperatorAcksStore,
    ) -> None:
        assert await store.list_active_acks() == []

    @pytest.mark.asyncio()
    async def test_filters_out_expired(
        self,
        store: OperatorAcksStore,
        system_pool: DatabasePool,
    ) -> None:
        # Active
        await store.record_ack(reason="voice.active", ttl_sec=3600)
        # Expired
        past_ts = int(time.time()) - 1000
        async with system_pool.write() as conn:
            await conn.execute(
                """INSERT INTO operator_acks
                   (reason, acked_at_ts, ttl_sec, operator_id, metadata)
                   VALUES (?, ?, ?, '', '{}')""",
                ("voice.expired", past_ts, 60),
            )
            await conn.commit()
        active = await store.list_active_acks()
        assert len(active) == 1
        assert active[0].reason == "voice.active"


class TestPruneExpired:
    @pytest.mark.asyncio()
    async def test_returns_removed_records(
        self,
        store: OperatorAcksStore,
        system_pool: DatabasePool,
    ) -> None:
        past_ts = int(time.time()) - 1000
        async with system_pool.write() as conn:
            for reason in ("a", "b"):
                await conn.execute(
                    """INSERT INTO operator_acks
                       (reason, acked_at_ts, ttl_sec, operator_id, metadata)
                       VALUES (?, ?, ?, '', '{}')""",
                    (reason, past_ts, 60),
                )
            await conn.commit()
        removed = await store.prune_expired()
        assert len(removed) == 2
        assert {r.reason for r in removed} == {"a", "b"}
        # Empty after prune
        async with system_pool.read() as conn:
            cursor = await conn.execute("SELECT COUNT(*) FROM operator_acks")
            row = await cursor.fetchone()
            assert row[0] == 0

    @pytest.mark.asyncio()
    async def test_no_op_when_nothing_expired(
        self,
        store: OperatorAcksStore,
    ) -> None:
        await store.record_ack(reason="voice.active", ttl_sec=3600)
        removed = await store.prune_expired()
        assert removed == []


class TestAckRecord:
    def test_ttl_remaining_sec_clamped_to_zero(self) -> None:
        past = int(time.time()) - 1000
        record = AckRecord(reason="r", acked_at_ts=past, ttl_sec=60)
        assert record.ttl_remaining_sec() == 0
        assert record.is_expired() is True

    def test_ttl_remaining_sec_for_active(self) -> None:
        now = int(time.time())
        record = AckRecord(reason="r", acked_at_ts=now, ttl_sec=3600)
        assert record.ttl_remaining_sec(now) == 3600
        assert record.is_expired(now) is False

    def test_row_to_record_handles_invalid_json_metadata(self) -> None:
        record = _row_to_record(("r", 100, 60, "op", "not json"))
        assert record.metadata == {}
