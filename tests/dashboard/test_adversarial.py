"""Adversarial edge-case tests for dashboard modules.

Tests unusual inputs, boundary conditions, concurrent access,
large data, malformed content, and error recovery.
"""

from __future__ import annotations

import json
import threading
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

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
            t.join()

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
            t.join()

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
            t.join()

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
            version="0.1.0", uptime_seconds=0.0, mind_name="test",
            active_conversations=0, memory_concepts=0, memory_episodes=0,
            llm_cost_today=0.0, llm_calls_today=0, tokens_today=0,
        )
        d = snap.to_dict()
        assert d["uptime_seconds"] == 0.0

    def test_huge_uptime(self) -> None:
        snap = StatusSnapshot(
            version="0.1.0", uptime_seconds=31536000.123456,  # 1 year
            mind_name="test", active_conversations=0, memory_concepts=0,
            memory_episodes=0, llm_cost_today=0.0, llm_calls_today=0,
            tokens_today=0,
        )
        d = snap.to_dict()
        assert d["uptime_seconds"] == 31536000.1

    def test_unicode_mind_name(self) -> None:
        snap = StatusSnapshot(
            version="0.1.0", uptime_seconds=100, mind_name="Ñyx 🔮 日本語",
            active_conversations=0, memory_concepts=0, memory_episodes=0,
            llm_cost_today=0.0, llm_calls_today=0, tokens_today=0,
        )
        d = snap.to_dict()
        assert d["mind_name"] == "Ñyx 🔮 日本語"

    def test_very_small_cost(self) -> None:
        snap = StatusSnapshot(
            version="0.1.0", uptime_seconds=100, mind_name="test",
            active_conversations=0, memory_concepts=0, memory_episodes=0,
            llm_cost_today=0.00001, llm_calls_today=1, tokens_today=1,
        )
        d = snap.to_dict()
        assert d["llm_cost_today"] == 0.0  # Rounded to 4 decimals


# ── Log Query Adversarial ──


class TestLogQueryAdversarial:
    def test_large_file(self, tmp_path: Path) -> None:
        """10k log lines should not OOM."""
        f = tmp_path / "big.log"
        lines = [json.dumps({"event": f"ev-{i}", "level": "info"}) for i in range(10000)]
        f.write_text("\n".join(lines) + "\n")

        result = query_logs(f, limit=50)
        assert len(result) == 50
        assert result[0]["event"] == "ev-9999"  # Most recent first

    def test_unicode_in_logs(self, tmp_path: Path) -> None:
        f = tmp_path / "unicode.log"
        entries = [
            json.dumps({"event": "日本語テスト", "level": "info"}),
            json.dumps({"event": "emoji 🔮✨", "level": "info"}),
            json.dumps({"event": "nullbyte\x00test", "level": "info"}),
        ]
        f.write_text("\n".join(entries) + "\n")

        result = query_logs(f)
        assert len(result) == 3

    def test_search_unicode(self, tmp_path: Path) -> None:
        f = tmp_path / "uni.log"
        f.write_text(json.dumps({"event": "概念作成 🧠", "level": "info"}) + "\n")

        result = query_logs(f, search="概念")
        assert len(result) == 1

    def test_binary_garbage_in_log(self, tmp_path: Path) -> None:
        """Binary data mixed with JSON should not crash."""
        f = tmp_path / "garbage.log"
        good = json.dumps({"event": "good", "level": "info"})
        f.write_bytes(b'\x80\xff\xfe\n' + good.encode() + b'\n' + b'\x00\x01\x02\n')

        result = query_logs(f)
        assert len(result) == 1
        assert result[0]["event"] == "good"

    def test_empty_json_objects(self, tmp_path: Path) -> None:
        f = tmp_path / "empty.log"
        f.write_text("{}\n{}\n{}\n")

        result = query_logs(f)
        assert len(result) == 3

    def test_nested_json(self, tmp_path: Path) -> None:
        f = tmp_path / "nested.log"
        entry = {"event": "complex", "level": "info", "data": {"nested": {"deep": True}}}
        f.write_text(json.dumps(entry) + "\n")

        result = query_logs(f, search="deep")
        assert len(result) == 1

    def test_very_long_line(self, tmp_path: Path) -> None:
        """Single log line with 100KB content should not crash."""
        f = tmp_path / "long.log"
        entry = {"event": "x" * 100000, "level": "info"}
        f.write_text(json.dumps(entry) + "\n")

        result = query_logs(f)
        assert len(result) == 1
        assert len(result[0]["event"]) == 100000

    def test_limit_zero(self, tmp_path: Path) -> None:
        f = tmp_path / "test.log"
        f.write_text(json.dumps({"event": "test", "level": "info"}) + "\n")

        result = query_logs(f, limit=0)
        assert result == []

    def test_filter_nonexistent_level(self, tmp_path: Path) -> None:
        f = tmp_path / "test.log"
        f.write_text(json.dumps({"event": "test", "level": "info"}) + "\n")

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
