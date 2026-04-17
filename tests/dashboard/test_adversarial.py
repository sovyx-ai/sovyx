"""Adversarial edge-case tests for dashboard modules.

Tests unusual inputs, boundary conditions, concurrent access,
large data, malformed content, and error recovery.
"""

from __future__ import annotations

import json
import threading
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from sovyx.dashboard.logs import query_logs
from sovyx.dashboard.status import DashboardCounters, StatusSnapshot

if TYPE_CHECKING:
    from pathlib import Path


# ── DashboardCounters Adversarial ──


class TestCountersThreadSafety:
    def test_concurrent_increments(self) -> None:
        """Multiple threads incrementing should not lose counts."""
        c = DashboardCounters()
        n_threads = 10
        n_increments = 1000

        def worker() -> None:
            for _ in range(n_increments):
                c.record_llm_call(cost=0.001, tokens=1)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
            assert not t.is_alive(), "worker thread did not finish in 5s"

        assert c.llm_calls == n_threads * n_increments
        assert c.tokens == n_threads * n_increments
        assert abs(c.llm_cost - n_threads * n_increments * 0.001) < 0.01

    def test_concurrent_mixed_operations(self) -> None:
        """Mixed record_llm_call and record_message from multiple threads."""
        c = DashboardCounters()

        def llm_worker() -> None:
            for _ in range(500):
                c.record_llm_call(cost=0.01, tokens=10)

        def msg_worker() -> None:
            for _ in range(500):
                c.record_message()

        threads = [
            threading.Thread(target=llm_worker),
            threading.Thread(target=llm_worker),
            threading.Thread(target=msg_worker),
            threading.Thread(target=msg_worker),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
            assert not t.is_alive(), "worker thread did not finish in 5s"

        assert c.llm_calls == 1000
        assert c.messages_received == 1000

    def test_snapshot_atomic(self) -> None:
        """Snapshot should return consistent values."""
        c = DashboardCounters()
        c.record_llm_call(cost=0.05, tokens=500)
        c.record_llm_call(cost=0.03, tokens=300)

        calls, cost, tokens, msgs = c.snapshot()
        assert calls == 2
        assert tokens == 800
        assert abs(cost - 0.08) < 0.001
        assert msgs == 0

    def test_day_boundary_under_contention(self) -> None:
        """Force day boundary during concurrent writes."""
        c = DashboardCounters()
        c._day_key = "2020-01-01"  # Force old date

        def worker() -> None:
            for _ in range(100):
                c.record_llm_call(cost=0.001, tokens=1)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
            assert not t.is_alive(), "worker thread did not finish in 5s"

        # After reset + 500 increments, should be exactly 500
        assert c.llm_calls == 500


class TestCountersEdgeCases:
    def test_zero_cost_and_tokens(self) -> None:
        c = DashboardCounters()
        c.record_llm_call(cost=0.0, tokens=0)
        assert c.llm_calls == 1
        assert c.llm_cost == 0.0
        assert c.tokens == 0

    def test_very_large_values(self) -> None:
        c = DashboardCounters()
        c.record_llm_call(cost=999999.99, tokens=2**31)
        assert c.llm_cost == 999999.99
        assert c.tokens == 2**31

    def test_negative_cost(self) -> None:
        """Negative cost (refund?) should still work."""
        c = DashboardCounters()
        c.record_llm_call(cost=0.10, tokens=100)
        c.record_llm_call(cost=-0.05, tokens=0)
        assert abs(c.llm_cost - 0.05) < 0.001

    def test_float_precision(self) -> None:
        """Many small additions should not lose precision badly."""
        c = DashboardCounters()
        for _ in range(10000):
            c.record_llm_call(cost=0.001, tokens=1)
        assert abs(c.llm_cost - 10.0) < 0.01  # Allow small float drift


# ── StatusSnapshot Adversarial ──


class TestSnapshotEdgeCases:
    def test_zero_uptime(self) -> None:
        snap = StatusSnapshot(
            version="0.1.0",
            uptime_seconds=0.0,
            mind_name="test",
            active_conversations=0,
            memory_concepts=0,
            memory_episodes=0,
            llm_cost_today=0.0,
            llm_calls_today=0,
            tokens_today=0,
            messages_today=0,
        )
        d = snap.to_dict()
        assert d["uptime_seconds"] == 0.0

    def test_huge_uptime(self) -> None:
        snap = StatusSnapshot(
            version="0.1.0",
            uptime_seconds=31536000.123456,  # 1 year
            mind_name="test",
            active_conversations=0,
            memory_concepts=0,
            memory_episodes=0,
            llm_cost_today=0.0,
            llm_calls_today=0,
            tokens_today=0,
            messages_today=0,
        )
        d = snap.to_dict()
        assert d["uptime_seconds"] == 31536000.1

    def test_unicode_mind_name(self) -> None:
        snap = StatusSnapshot(
            version="0.1.0",
            uptime_seconds=100,
            mind_name="Ñyx 🔮 日本語",
            active_conversations=0,
            memory_concepts=0,
            memory_episodes=0,
            llm_cost_today=0.0,
            llm_calls_today=0,
            tokens_today=0,
            messages_today=0,
        )
        d = snap.to_dict()
        assert d["mind_name"] == "Ñyx 🔮 日本語"

    def test_very_small_cost(self) -> None:
        snap = StatusSnapshot(
            version="0.1.0",
            uptime_seconds=100,
            mind_name="test",
            active_conversations=0,
            memory_concepts=0,
            memory_episodes=0,
            llm_cost_today=0.00001,
            llm_calls_today=1,
            tokens_today=1,
            messages_today=0,
        )
        d = snap.to_dict()
        assert d["llm_cost_today"] == 0.0  # Rounded to 4 decimals


# ── Log Query Adversarial ──


class TestLogQueryAdversarial:
    def test_large_file(self, tmp_path: Path) -> None:
        """10k log lines should not OOM."""
        f = tmp_path / "big.log"
        lines = [
            json.dumps(
                {
                    "event": f"ev-{i}",
                    "level": "info",
                    "timestamp": f"2026-01-01T{i // 3600:02d}:{(i % 3600) // 60:02d}:{i % 60:02d}",
                }
            )
            for i in range(10000)
        ]
        f.write_text("\n".join(lines) + "\n")

        result = query_logs(f, limit=50)
        assert len(result) == 50
        assert result[0]["event"] == "ev-9999"  # Most recent first

    def test_unicode_in_logs(self, tmp_path: Path) -> None:
        f = tmp_path / "unicode.log"
        entries = [
            json.dumps(
                {"event": "日本語テスト", "level": "info", "timestamp": "2026-01-01T00:00:00"}
            ),
            json.dumps(
                {"event": "emoji 🔮✨", "level": "info", "timestamp": "2026-01-01T00:00:00"}
            ),
            json.dumps(
                {"event": "nullbyte\x00test", "level": "info", "timestamp": "2026-01-01T00:00:00"}
            ),
        ]
        f.write_text("\n".join(entries) + "\n")

        result = query_logs(f)
        assert len(result) == 3

    def test_search_unicode(self, tmp_path: Path) -> None:
        f = tmp_path / "uni.log"
        f.write_text(
            json.dumps(
                {"event": "概念作成 🧠", "level": "info", "timestamp": "2026-01-01T00:00:00"}
            )
            + "\n"
        )

        result = query_logs(f, search="概念")
        assert len(result) == 1

    def test_binary_garbage_in_log(self, tmp_path: Path) -> None:
        """Binary data mixed with JSON should not crash."""
        f = tmp_path / "garbage.log"
        good = json.dumps({"event": "good", "level": "info", "timestamp": "2026-01-01T00:00:00"})
        f.write_bytes(b"\x80\xff\xfe\n" + good.encode() + b"\n" + b"\x00\x01\x02\n")

        result = query_logs(f)
        assert len(result) == 1
        assert result[0]["event"] == "good"

    def test_empty_json_objects(self, tmp_path: Path) -> None:
        f = tmp_path / "empty.log"
        f.write_text("{}\n{}\n{}\n")

        result = query_logs(f)
        assert len(result) == 0  # Empty objects lack required timestamp+event

    def test_nested_json(self, tmp_path: Path) -> None:
        f = tmp_path / "nested.log"
        entry = {
            "event": "complex",
            "level": "info",
            "data": {"nested": {"deep": True}},
            "timestamp": "2026-01-01T00:00:00",
        }
        f.write_text(json.dumps(entry) + "\n")

        result = query_logs(f, search="deep")
        assert len(result) == 1

    def test_very_long_line(self, tmp_path: Path) -> None:
        """Single log line with 100KB content should not crash."""
        f = tmp_path / "long.log"
        entry = {"event": "x" * 100000, "level": "info", "timestamp": "2026-01-01T00:00:00"}
        f.write_text(json.dumps(entry) + "\n")

        result = query_logs(f)
        assert len(result) == 1
        assert len(result[0]["event"]) == 100000

    def test_limit_zero(self, tmp_path: Path) -> None:
        f = tmp_path / "test.log"
        f.write_text(
            json.dumps({"event": "test", "level": "info", "timestamp": "2026-01-01T00:00:00"})
            + "\n"
        )

        result = query_logs(f, limit=0)
        assert result == []

    def test_filter_nonexistent_level(self, tmp_path: Path) -> None:
        f = tmp_path / "test.log"
        f.write_text(
            json.dumps({"event": "test", "level": "info", "timestamp": "2026-01-01T00:00:00"})
            + "\n"
        )

        result = query_logs(f, level="TRACE")
        assert result == []

    def test_file_deleted_during_read(self, tmp_path: Path) -> None:
        """File disappearing should return empty, not crash."""
        f = tmp_path / "ghost.log"
        result = query_logs(f)
        assert result == []


# ── Brain Graph Adversarial ──


class TestBrainGraphAdversarial:
    @pytest.mark.asyncio()
    async def test_empty_node_ids(self) -> None:
        from sovyx.dashboard.brain import _get_relations

        registry = MagicMock()
        result = await _get_relations(registry, set())
        assert result == []

    @pytest.mark.asyncio()
    async def test_graph_with_zero_limit(self) -> None:
        from sovyx.dashboard.brain import get_brain_graph

        registry = MagicMock()
        registry.is_registered.return_value = False

        result = await get_brain_graph(registry, limit=0)
        assert result["nodes"] == []
        assert result["links"] == []


# ── Query Param Validation ──


class TestBroadcastConcurrency:
    """Verify broadcast doesn't hold lock during sends."""

    @pytest.mark.asyncio()
    async def test_broadcast_releases_lock_before_send(self) -> None:
        """A slow client shouldn't block connect/disconnect."""
        import asyncio

        from sovyx.dashboard.server import ConnectionManager

        mgr = ConnectionManager()

        # Create mock websockets
        fast_ws = MagicMock()
        fast_ws.send_json = AsyncMock()
        slow_ws = MagicMock()

        async def slow_send(msg: object) -> None:
            await asyncio.sleep(0.1)

        slow_ws.send_json = AsyncMock(side_effect=slow_send)
        slow_ws.accept = AsyncMock()
        fast_ws.accept = AsyncMock()

        await mgr.connect(slow_ws)
        await mgr.connect(fast_ws)

        # Start broadcast (will be slow due to slow_ws)
        broadcast_task = asyncio.create_task(mgr.broadcast({"test": 1}))

        # Meanwhile, a new connection should NOT be blocked
        new_ws = MagicMock()
        new_ws.accept = AsyncMock()
        # Give broadcast a moment to start
        await asyncio.sleep(0.01)
        # This should complete without waiting for broadcast
        connect_task = asyncio.create_task(mgr.connect(new_ws))
        done, _pending = await asyncio.wait({connect_task}, timeout=0.05)
        assert len(done) == 1, "connect() was blocked by broadcast()"

        await broadcast_task
        assert mgr.active_count == 3

    @pytest.mark.asyncio()
    async def test_broadcast_removes_stale_after_send(self) -> None:
        """Stale connections removed after failed sends."""
        from sovyx.dashboard.server import ConnectionManager

        mgr = ConnectionManager()
        good_ws = MagicMock()
        good_ws.send_json = AsyncMock()
        good_ws.accept = AsyncMock()
        bad_ws = MagicMock()
        bad_ws.send_json = AsyncMock(side_effect=ConnectionError("gone"))
        bad_ws.accept = AsyncMock()

        await mgr.connect(good_ws)
        await mgr.connect(bad_ws)
        assert mgr.active_count == 2

        await mgr.broadcast({"test": 1})
        assert mgr.active_count == 1
        good_ws.send_json.assert_called_once()


class TestBrainGraphLinksCap:
    """Verify brain graph links are capped."""

    @pytest.mark.asyncio()
    async def test_links_respect_max_cap(self) -> None:
        """Links returned should not exceed max_links."""
        import aiosqlite

        from sovyx.dashboard.brain import _get_relations

        conn = await aiosqlite.connect(":memory:")
        await conn.executescript(
            "CREATE TABLE relations (id TEXT, source_id TEXT, target_id TEXT, "
            "relation_type TEXT, weight REAL)"
        )
        # Insert 50 relations between 10 nodes
        for i in range(50):
            src = f"c{i % 10}"
            tgt = f"c{(i + 1) % 10}"
            await conn.execute(
                "INSERT INTO relations VALUES (?, ?, ?, 'related', 0.5)",
                (f"r{i}", src, tgt),
            )
        await conn.commit()

        class _Pool:
            class _Ctx:
                def __init__(self, c: aiosqlite.Connection) -> None:
                    self._c = c

                async def __aenter__(self) -> aiosqlite.Connection:
                    return self._c

                async def __aexit__(self, *a: object) -> None:
                    pass

            def __init__(self, c: aiosqlite.Connection) -> None:
                self._c = c

            def read(self) -> _Pool._Ctx:
                return self._Ctx(self._c)

        pool = _Pool(conn)

        db_manager = MagicMock()
        db_manager.get_brain_pool.return_value = pool

        registry = MagicMock()
        registry.is_registered.return_value = True
        registry.resolve = AsyncMock(return_value=db_manager)

        node_ids = {f"c{i}" for i in range(10)}
        links = await _get_relations(registry, node_ids, max_links=5)

        assert len(links) <= 5

        await conn.close()


class TestSettingsInputValidation:
    """PUT /api/settings rejects malformed input gracefully."""

    @pytest.fixture()
    def _client(self) -> TestClient:
        from sovyx.dashboard import server as srv

        app = srv.create_app()
        self._headers = {"Authorization": f"Bearer {srv._server_token}"}
        return TestClient(app, raise_server_exceptions=False)

    def test_invalid_json_body(self, _client: TestClient) -> None:
        headers = {**self._headers, "Content-Type": "application/json"}
        r = _client.put("/api/settings", content="not json", headers=headers)
        assert r.status_code == 422
        assert r.json()["ok"] is False

    def test_array_body_rejected(self, _client: TestClient) -> None:
        r = _client.put("/api/settings", json=[1, 2], headers=self._headers)
        assert r.status_code == 422
        assert "object" in r.json()["error"].lower()

    def test_string_body_rejected(self, _client: TestClient) -> None:
        r = _client.put("/api/settings", json="hello", headers=self._headers)
        assert r.status_code == 422

    def test_number_body_rejected(self, _client: TestClient) -> None:
        r = _client.put("/api/settings", json=42, headers=self._headers)
        assert r.status_code == 422

    def test_null_body_rejected(self, _client: TestClient) -> None:
        r = _client.put("/api/settings", json=None, headers=self._headers)
        assert r.status_code == 422

    def test_valid_dict_accepted(self, _client: TestClient) -> None:
        r = _client.put("/api/settings", json={"log_level": "INFO"}, headers=self._headers)
        assert r.status_code == 200
        assert r.json()["ok"] is True


class TestQueryParamValidation:
    """Ensure API routes reject invalid query params."""

    @pytest.fixture()
    def client(self) -> TestClient:
        from sovyx.dashboard.server import TOKEN_FILE, create_app

        app = create_app()
        token = TOKEN_FILE.read_text().strip()
        self._headers = {"Authorization": f"Bearer {token}"}
        return TestClient(app)

    def test_conversations_negative_limit(self, client: TestClient) -> None:
        resp = client.get("/api/conversations?limit=-1", headers=self._headers)
        assert resp.status_code == 422

    def test_conversations_huge_limit(self, client: TestClient) -> None:
        resp = client.get("/api/conversations?limit=999999", headers=self._headers)
        assert resp.status_code == 422

    def test_conversations_negative_offset(self, client: TestClient) -> None:
        resp = client.get("/api/conversations?offset=-5", headers=self._headers)
        assert resp.status_code == 422

    def test_brain_graph_negative_limit(self, client: TestClient) -> None:
        resp = client.get("/api/brain/graph?limit=-1", headers=self._headers)
        assert resp.status_code == 422

    def test_brain_graph_huge_limit(self, client: TestClient) -> None:
        resp = client.get("/api/brain/graph?limit=5000", headers=self._headers)
        assert resp.status_code == 422

    def test_logs_negative_limit(self, client: TestClient) -> None:
        resp = client.get("/api/logs?limit=-1", headers=self._headers)
        assert resp.status_code == 422

    def test_logs_huge_limit(self, client: TestClient) -> None:
        resp = client.get("/api/logs?limit=9999", headers=self._headers)
        assert resp.status_code == 422


# ── Event Serialization Adversarial ──


class TestEventSerializationAdversarial:
    def test_unknown_event_type(self) -> None:
        """Unknown event types should serialize with empty data."""
        from sovyx.dashboard.events import _serialize_event
        from sovyx.engine.events import Event

        # Create a custom event subclass
        event = Event()
        result = _serialize_event(event)
        assert result["type"] == "Event"
        assert result["data"] == {}

    def test_event_timestamp_is_isoformat(self) -> None:
        """Event timestamp is always an ISO format string."""
        from sovyx.dashboard.events import _serialize_event
        from sovyx.engine.events import EngineStarted

        event = EngineStarted()
        result = _serialize_event(event)
        assert isinstance(result["timestamp"], str)
        assert "T" in result["timestamp"]  # ISO 8601
