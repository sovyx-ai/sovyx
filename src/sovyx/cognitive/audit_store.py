"""Sovyx Safety Audit Store — SQLite persistence for audit events.

Persists SafetyEvents to a local SQLite database for:
- Historical compliance queries (30d+)
- Dashboard analytics
- Incident investigation

Design:
    - Write-behind: events buffered in-memory, flushed periodically
    - WAL mode for concurrent read/write
    - Auto-vacuum to bound disk usage
    - Privacy: NO original content stored, only metadata
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from pathlib import Path

    from sovyx.cognitive.safety_audit import SafetyEvent

logger = get_logger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS safety_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    direction TEXT NOT NULL,
    action TEXT NOT NULL,
    category TEXT NOT NULL,
    tier TEXT NOT NULL,
    pattern_description TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_events_timestamp ON safety_events(timestamp);
CREATE INDEX IF NOT EXISTS idx_events_category ON safety_events(category);
"""

_INSERT = """
INSERT INTO safety_events (timestamp, direction, action, category, tier, pattern_description)
VALUES (?, ?, ?, ?, ?, ?)
"""

_FLUSH_INTERVAL_SEC = 10.0
_BUFFER_MAX = 100


@dataclass(frozen=True, slots=True)
class AuditQueryResult:
    """Result of an audit store query."""

    total: int
    events: list[dict[str, object]]


class AuditStore:
    """SQLite-backed audit event store.

    Thread-safe via write lock. Uses WAL mode for concurrent reads.
    Events are buffered and flushed periodically for performance.
    """

    def __init__(self, db_path: Path | str = "safety_audit.db") -> None:
        self._db_path = str(db_path)
        self._buffer: deque[SafetyEvent] = deque(maxlen=_BUFFER_MAX * 2)
        self._lock = threading.Lock()
        self._last_flush = time.monotonic()
        self._init_db()

    def _init_db(self) -> None:
        """Create tables and indexes if they don't exist."""
        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.executescript(_SCHEMA)
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
        except sqlite3.Error:
            logger.warning("audit_store_init_failed", exc_info=True)

    def append(self, event: SafetyEvent) -> None:
        """Buffer an event for persistence.

        Flushes to disk when buffer is full or interval elapsed.
        """
        self._buffer.append(event)

        now = time.monotonic()
        if len(self._buffer) >= _BUFFER_MAX or now - self._last_flush >= _FLUSH_INTERVAL_SEC:
            self.flush()

    def flush(self) -> int:
        """Write buffered events to SQLite. Returns count written."""
        with self._lock:
            if not self._buffer:
                return 0

            events = list(self._buffer)
            self._buffer.clear()
            self._last_flush = time.monotonic()

        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.executemany(
                    _INSERT,
                    [
                        (
                            e.timestamp,
                            e.direction,
                            e.action,
                            e.category,
                            e.tier,
                            e.pattern_description,
                        )
                        for e in events
                    ],
                )
            return len(events)
        except sqlite3.Error:
            logger.warning(
                "audit_store_flush_failed",
                count=len(events),
                exc_info=True,
            )
            return 0

    def query(
        self,
        *,
        hours: int = 24,
        category: str | None = None,
        direction: str | None = None,
        limit: int = 100,
    ) -> AuditQueryResult:
        """Query persisted events.

        Args:
            hours: Look back this many hours.
            category: Filter by category (optional).
            direction: Filter by direction (optional).
            limit: Max events to return.

        Returns:
            AuditQueryResult with matching events.
        """
        # Flush pending events first
        self.flush()

        since = time.time() - (hours * 3600)
        conditions = ["timestamp >= ?"]
        params: list[object] = [since]

        if category:
            conditions.append("category = ?")
            params.append(category)
        if direction:
            conditions.append("direction = ?")
            params.append(direction)

        where = " AND ".join(conditions)
        params.append(limit)

        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.row_factory = sqlite3.Row
                # Count
                count_row = conn.execute(
                    f"SELECT COUNT(*) FROM safety_events WHERE {where}",  # noqa: S608
                    params[:-1],
                ).fetchone()
                total = count_row[0] if count_row else 0

                # Events
                rows = conn.execute(
                    f"SELECT * FROM safety_events WHERE {where} ORDER BY timestamp DESC LIMIT ?",  # noqa: S608
                    params,
                ).fetchall()

                events = [dict(row) for row in rows]

            return AuditQueryResult(total=total, events=events)
        except sqlite3.Error:
            logger.warning("audit_store_query_failed", exc_info=True)
            return AuditQueryResult(total=0, events=[])

    def count(self, *, hours: int = 24) -> int:
        """Count events in the last N hours."""
        self.flush()
        since = time.time() - (hours * 3600)
        try:
            with sqlite3.connect(self._db_path) as conn:
                row = conn.execute(
                    "SELECT COUNT(*) FROM safety_events WHERE timestamp >= ?",
                    (since,),
                ).fetchone()
                return row[0] if row else 0
        except sqlite3.Error:
            return 0

    def close(self) -> None:
        """Flush remaining events."""
        self.flush()


# ── Module singleton ────────────────────────────────────────────────────
_store: AuditStore | None = None


def get_audit_store(db_path: str = "safety_audit.db") -> AuditStore:
    """Get or create the global AuditStore."""
    global _store  # noqa: PLW0603
    if _store is None:
        _store = AuditStore(db_path=db_path)
    return _store
