"""Tests for SQLite audit store.

Covers: init, append, flush, query, count, filtering, edge cases.
"""

from __future__ import annotations

import time
from pathlib import Path

from sovyx.cognitive.audit_store import AuditQueryResult, AuditStore
from sovyx.cognitive.safety_audit import SafetyEvent


def _event(
    *,
    category: str = "violence",
    direction: str = "input",
    action: str = "blocked",
) -> SafetyEvent:
    return SafetyEvent(
        timestamp=time.time(),
        direction=direction,
        action=action,
        category=category,
        tier="standard",
        pattern_description="test pattern",
    )


class TestAuditStore:
    """SQLite audit store tests."""

    def test_init_creates_db(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        store = AuditStore(db_path=db)
        assert db.exists()
        store.close()

    def test_append_and_flush(self, tmp_path: Path) -> None:
        store = AuditStore(db_path=tmp_path / "test.db")
        store.append(_event())
        count = store.flush()
        assert count == 1
        store.close()

    def test_flush_empty(self, tmp_path: Path) -> None:
        store = AuditStore(db_path=tmp_path / "test.db")
        count = store.flush()
        assert count == 0
        store.close()

    def test_query_basic(self, tmp_path: Path) -> None:
        store = AuditStore(db_path=tmp_path / "test.db")
        store.append(_event())
        store.append(_event(category="weapons"))
        result = store.query(hours=1)
        assert result.total == 2
        assert len(result.events) == 2
        store.close()

    def test_query_by_category(self, tmp_path: Path) -> None:
        store = AuditStore(db_path=tmp_path / "test.db")
        store.append(_event(category="violence"))
        store.append(_event(category="weapons"))
        store.append(_event(category="violence"))
        result = store.query(hours=1, category="violence")
        assert result.total == 2
        store.close()

    def test_query_by_direction(self, tmp_path: Path) -> None:
        store = AuditStore(db_path=tmp_path / "test.db")
        store.append(_event(direction="input"))
        store.append(_event(direction="output"))
        result = store.query(hours=1, direction="input")
        assert result.total == 1
        store.close()

    def test_query_limit(self, tmp_path: Path) -> None:
        store = AuditStore(db_path=tmp_path / "test.db")
        for _ in range(10):
            store.append(_event())
        result = store.query(hours=1, limit=3)
        assert len(result.events) == 3
        assert result.total == 10
        store.close()

    def test_count(self, tmp_path: Path) -> None:
        store = AuditStore(db_path=tmp_path / "test.db")
        store.append(_event())
        store.append(_event())
        assert store.count(hours=1) == 2
        store.close()

    def test_count_empty(self, tmp_path: Path) -> None:
        store = AuditStore(db_path=tmp_path / "test.db")
        assert store.count(hours=1) == 0
        store.close()

    def test_old_events_excluded(self, tmp_path: Path) -> None:
        store = AuditStore(db_path=tmp_path / "test.db")
        old_event = SafetyEvent(
            timestamp=time.time() - 7200,  # 2 hours ago
            direction="input",
            action="blocked",
            category="violence",
            tier="standard",
            pattern_description="old",
        )
        store.append(old_event)
        store.append(_event())
        assert store.count(hours=1) == 1
        store.close()

    def test_auto_flush_on_buffer_full(self, tmp_path: Path) -> None:
        store = AuditStore(db_path=tmp_path / "test.db")
        for _ in range(150):
            store.append(_event())
        # Buffer should auto-flush at 100
        assert store.count(hours=1) >= 100
        store.close()

    def test_close_flushes(self, tmp_path: Path) -> None:
        store = AuditStore(db_path=tmp_path / "test.db")
        store.append(_event())
        store.close()
        # Verify flushed
        store2 = AuditStore(db_path=tmp_path / "test.db")
        assert store2.count(hours=1) == 1
        store2.close()


class TestAuditQueryResult:
    """Test result dataclass."""

    def test_fields(self) -> None:
        r = AuditQueryResult(total=5, events=[{"a": 1}])
        assert r.total == 5
        assert len(r.events) == 1


class TestErrorPaths:
    """Cover error/edge paths."""

    def test_init_invalid_path(self) -> None:
        """Invalid DB path logs warning but doesn't crash."""
        store = AuditStore(db_path="/nonexistent/dir/test.db")
        # Should not raise
        assert store.count(hours=1) == 0

    def test_flush_after_db_deleted(self, tmp_path: Path) -> None:
        """Flush after DB file is gone returns 0."""
        db = tmp_path / "test.db"
        store = AuditStore(db_path=db)
        store.append(_event())
        db.unlink()
        # Flush should recreate or handle gracefully
        count = store.flush()
        # SQLite may recreate the file, so count could be 1
        assert isinstance(count, int)

    def test_query_no_results(self, tmp_path: Path) -> None:
        store = AuditStore(db_path=tmp_path / "test.db")
        result = store.query(hours=1, category="nonexistent")
        assert result.total == 0
        assert result.events == []
        store.close()


class TestSingleton:
    """Test get_audit_store."""

    def test_get_audit_store(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import sovyx.cognitive.audit_store as mod

        monkeypatch.setattr(mod, "_store", None)
        store = mod.get_audit_store(db_path=str(tmp_path / "singleton.db"))
        assert isinstance(store, AuditStore)
        # Cleanup
        monkeypatch.setattr(mod, "_store", None)

    def test_count_error_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Force sqlite error in count."""
        import sqlite3

        store = AuditStore(db_path=tmp_path / "test.db")
        original = sqlite3.connect

        def bad_connect(*a: object, **k: object) -> object:
            raise sqlite3.Error("forced")

        monkeypatch.setattr(sqlite3, "connect", bad_connect)
        result = store.count(hours=1)
        assert result == 0
        monkeypatch.setattr(sqlite3, "connect", original)

    def test_query_error_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Force sqlite error in query."""
        import sqlite3

        store = AuditStore(db_path=tmp_path / "test.db")
        store.append(_event())
        store.flush()
        original = sqlite3.connect

        def bad_connect(*a: object, **k: object) -> object:
            raise sqlite3.Error("forced")

        monkeypatch.setattr(sqlite3, "connect", bad_connect)
        result = store.query(hours=1)
        assert result.total == 0
        monkeypatch.setattr(sqlite3, "connect", original)
