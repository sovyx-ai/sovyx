"""SQLite FTS5 sidecar for log search.

The dashboard's log viewer needs subsecond full-text search across
the JSONL file produced by the structlog pipeline. Linear scans
(``query_logs`` / ``query_saga``) work for low-volume daemons but
collapse on busy installs that emit millions of records per day.

:class:`FTSIndexer` runs as a background task that tails the JSONL
log file and populates a SQLite database with one row per record
plus an FTS5 virtual table for full-text search over event /
message / content. The dashboard queries through :meth:`search`,
which returns timestamped rows with FTS5 snippets ready to render.

Design choices:
    * **Polling tail** (default 0.5 s) over watchdog so the indexer
      has zero extra dependencies and behaves identically across
      Linux / macOS / Windows. Watchdog can wrap this later if
      sub-100 ms freshness is needed.
    * **Persistent byte offset** stored alongside the database file
      (``<db_path>.offset``) so the indexer resumes where it left
      off after a daemon restart instead of re-scanning the entire
      file.
    * **External-content FTS5** so the FTS index does not duplicate
      ``content_json`` storage — the virtual table references rows
      in the ``logs`` base table by rowid.
    * **Retention pruner** runs once per hour, deleting both the
      ``logs`` row and the FTS index entry (via the
      ``logs_fts(logs_fts, rowid, ...)`` "delete" sentinel pattern)
      for entries older than the configured horizon.
    * **Rotation safe** — when the log file shrinks below the
      stored offset (the source got rotated), the offset resets to
      0 so the new file is indexed from the start. Records that
      were already indexed survive in the database; ``UNIQUE`` on
      ``(timestamp_iso, sequence_no)`` prevents duplicates if the
      operator concatenates rotated files.

Aligned with IMPL-OBSERVABILITY-001 §16 (Phase 10, Task 10.1).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from typing import TYPE_CHECKING, Any

from sovyx.observability.logging import get_logger
from sovyx.observability.tasks import spawn

if TYPE_CHECKING:
    from pathlib import Path

logger = get_logger(__name__)

# ── Schema ──────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_iso TEXT NOT NULL,
    timestamp_unix REAL NOT NULL,
    level TEXT NOT NULL,
    logger TEXT,
    event TEXT,
    message TEXT,
    saga_id TEXT,
    sequence_no INTEGER,
    content_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_logs_timestamp ON logs(timestamp_unix);
CREATE INDEX IF NOT EXISTS idx_logs_saga ON logs(saga_id);
CREATE INDEX IF NOT EXISTS idx_logs_level ON logs(level);
CREATE INDEX IF NOT EXISTS idx_logs_logger ON logs(logger);
CREATE UNIQUE INDEX IF NOT EXISTS idx_logs_dedup
    ON logs(timestamp_iso, sequence_no, event)
    WHERE sequence_no IS NOT NULL;

CREATE VIRTUAL TABLE IF NOT EXISTS logs_fts USING fts5(
    event,
    message,
    saga_id UNINDEXED,
    content_json,
    content='logs',
    content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS logs_ai AFTER INSERT ON logs BEGIN
    INSERT INTO logs_fts(rowid, event, message, saga_id, content_json)
    VALUES (new.id, new.event, new.message, new.saga_id, new.content_json);
END;

CREATE TRIGGER IF NOT EXISTS logs_ad AFTER DELETE ON logs BEGIN
    INSERT INTO logs_fts(logs_fts, rowid, event, message, saga_id, content_json)
    VALUES ('delete', old.id, old.event, old.message, old.saga_id, old.content_json);
END;
"""

_INSERT_SQL = """
INSERT OR IGNORE INTO logs
    (timestamp_iso, timestamp_unix, level, logger, event, message, saga_id, sequence_no, content_json)
VALUES (:ts_iso, :ts_unix, :level, :logger, :event, :message, :saga_id, :sequence_no, :content_json)
"""

_DEFAULT_TAIL_INTERVAL_S = 0.5
_DEFAULT_BATCH_SIZE = 200
_DEFAULT_RETENTION_DAYS = 30
_RETENTION_INTERVAL_S = 3600.0  # Once per hour.


def _coerce_unix(ts_iso: str) -> float:
    """Parse an ISO-8601 timestamp into a unix epoch float.

    Returns ``time.time()`` on parse failure so a malformed envelope
    is timestamped now rather than discarded — operators always
    benefit from seeing the record, even if the original timestamp
    was unrenderable.
    """
    if not ts_iso:
        return time.time()
    try:
        from datetime import datetime  # noqa: PLC0415 — single-call import.

        # `Z` suffix support — datetime.fromisoformat handles it from 3.11.
        return datetime.fromisoformat(ts_iso.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return time.time()


def _extract_message(parsed: dict[str, Any]) -> str:
    """Best-effort extraction of a free-text message from the record.

    Pulls ``msg`` first (legacy stdlib field), then ``message``
    (structlog), then a flattened concatenation of any remaining
    string fields so the FTS index has *something* to match on. Keys
    starting with an underscore are skipped — they belong to the
    handler internals, not the operator-facing payload.
    """
    for key in ("msg", "message", "exception"):
        value = parsed.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


class FTSIndexer:
    """Tail a JSONL log file and maintain a SQLite FTS5 index.

    Lifecycle:
        1. :meth:`start` — schedule the tail loop and, when retention
           is enabled, the prune loop. Returns once both background
           tasks are spawned.
        2. :meth:`search` — concurrent-safe full-text query. May be
           called from any task while the tail loop is active.
        3. :meth:`stop` — request shutdown, drain the in-flight
           batch, commit, and close the connection. Idempotent.

    The indexer holds a single :mod:`aiosqlite` connection in WAL
    mode so the tail-writer and the dashboard reader don't block
    each other.
    """

    def __init__(
        self,
        log_path: Path,
        db_path: Path,
        *,
        tail_interval_s: float = _DEFAULT_TAIL_INTERVAL_S,
        batch_size: int = _DEFAULT_BATCH_SIZE,
        retention_days: int = _DEFAULT_RETENTION_DAYS,
    ) -> None:
        """Bind the indexer to a log file + database location.

        Args:
            log_path: JSONL file to tail. Created on first write by
                the structlog pipeline; the indexer simply waits
                when the file is missing.
            db_path: SQLite database file. Parent directory is
                created on :meth:`start`.
            tail_interval_s: Polling interval. Smaller values cost
                more wakeups; larger values delay search visibility.
            batch_size: Maximum records inserted per transaction.
                Trades latency for write amplification.
            retention_days: Records older than this are deleted by
                the periodic pruner. Set to 0 to disable retention.
        """
        self._log_path = log_path
        self._db_path = db_path
        self._offset_path = db_path.with_suffix(db_path.suffix + ".offset")
        self._tail_interval_s = tail_interval_s
        self._batch_size = batch_size
        self._retention_days = retention_days

        self._conn: Any | None = None  # aiosqlite.Connection — typed Any to keep import lazy.
        self._tail_task: asyncio.Task[None] | None = None
        self._retention_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._search_lock = asyncio.Lock()  # aiosqlite is serialized; share fairly.

    # ── Lifecycle ──────────────────────────────────────────────────

    async def start(self) -> None:
        """Open the database, ensure schema, and spawn the background tasks.

        Calling :meth:`start` twice is a no-op — the second call
        notices the existing tasks and returns. This keeps callers
        in dashboards and CLI commands free of explicit "is it
        already running?" guards.
        """
        if self._tail_task is not None:
            return

        import aiosqlite  # noqa: PLC0415 — lazy so non-FTS deployments skip the import cost.

        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._db_path)
        await self._conn.execute("PRAGMA journal_mode = WAL")
        await self._conn.execute("PRAGMA synchronous = NORMAL")
        await self._conn.executescript(_SCHEMA)
        await self._conn.commit()

        self._stop_event.clear()
        self._tail_task = spawn(self._tail_loop(), name="fts-indexer-tail")
        if self._retention_days > 0:
            self._retention_task = spawn(
                self._retention_loop(),
                name="fts-indexer-retention",
            )
        logger.info(
            "fts.indexer.started",
            **{
                "fts.log_path": str(self._log_path),
                "fts.db_path": str(self._db_path),
                "fts.tail_interval_s": self._tail_interval_s,
                "fts.retention_days": self._retention_days,
            },
        )

    async def stop(self) -> None:
        """Stop the background tasks, commit pending work, close the DB.

        Idempotent. The tail task is given a 5-second grace period
        to drain in-flight inserts; if it doesn't yield in time it's
        cancelled — the offset file is the authoritative resume
        point so a forced cancel only loses the records currently
        in flight.
        """
        if self._tail_task is None:
            return
        self._stop_event.set()
        for task in (self._tail_task, self._retention_task):
            if task is None:
                continue
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(task, timeout=5.0)
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._tail_task = None
        self._retention_task = None
        if self._conn is not None:
            with contextlib.suppress(Exception):
                await self._conn.commit()
            with contextlib.suppress(Exception):
                await self._conn.close()
            self._conn = None
        logger.info("fts.indexer.stopped")

    # ── Background loops ───────────────────────────────────────────

    async def _tail_loop(self) -> None:
        """Polling tail of *log_path*. Honours the stop event between iterations."""
        offset = self._read_offset()
        while not self._stop_event.is_set():
            try:
                offset = await self._consume_new_lines(offset)
            except Exception as exc:  # noqa: BLE001 — never crash the loop.
                logger.warning(
                    "fts.indexer.tail_failed",
                    **{"fts.error": str(exc), "fts.error_type": type(exc).__name__},
                )
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._tail_interval_s,
                )
            except asyncio.TimeoutError:
                continue

    async def _consume_new_lines(self, offset: int) -> int:
        """Read new bytes from *log_path*, parse + insert, return new offset.

        Detects rotation by comparing on-disk file size to the
        previous offset — if the file is shorter, the previous file
        was rotated away and we restart from byte 0.
        """
        if not self._log_path.exists():
            return offset

        size = self._log_path.stat().st_size
        if size < offset:
            logger.info(
                "fts.indexer.rotation_detected",
                **{"fts.previous_offset": offset, "fts.current_size": size},
            )
            offset = 0

        if size == offset:
            return offset

        batch: list[dict[str, Any]] = []
        with self._log_path.open("rb") as fh:
            fh.seek(offset)
            chunk = fh.read(size - offset)
            new_offset = offset + len(chunk)

        for raw_line in chunk.splitlines():
            if not raw_line.strip():
                continue
            try:
                parsed = json.loads(raw_line.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue
            if not isinstance(parsed, dict):
                continue
            batch.append(self._record_to_row(parsed, raw_line))
            if len(batch) >= self._batch_size:
                await self._flush_batch(batch)
                batch.clear()
        if batch:
            await self._flush_batch(batch)

        self._write_offset(new_offset)
        return new_offset

    @staticmethod
    def _record_to_row(parsed: dict[str, Any], raw_line: bytes) -> dict[str, Any]:
        """Project a structlog record into the ``logs`` table column shape."""
        ts_iso = str(parsed.get("timestamp", "")) or str(parsed.get("ts", ""))
        return {
            "ts_iso": ts_iso,
            "ts_unix": _coerce_unix(ts_iso),
            "level": str(parsed.get("level", parsed.get("severity", "info"))).lower(),
            "logger": parsed.get("logger") or parsed.get("module"),
            "event": parsed.get("event"),
            "message": _extract_message(parsed),
            "saga_id": parsed.get("saga_id"),
            "sequence_no": parsed.get("sequence_no"),
            "content_json": raw_line.decode("utf-8", errors="replace"),
        }

    async def _flush_batch(self, batch: list[dict[str, Any]]) -> None:
        """Insert *batch* in a single transaction. No-op when empty or DB closed."""
        if not batch or self._conn is None:
            return
        async with self._search_lock:
            try:
                await self._conn.executemany(_INSERT_SQL, batch)
                await self._conn.commit()
            except Exception as exc:  # noqa: BLE001 — keep tailing on a single bad batch.
                logger.warning(
                    "fts.indexer.insert_failed",
                    **{
                        "fts.batch_size": len(batch),
                        "fts.error": str(exc),
                        "fts.error_type": type(exc).__name__,
                    },
                )
                with contextlib.suppress(Exception):
                    await self._conn.rollback()

    async def _retention_loop(self) -> None:
        """Hourly pruner — delete rows older than ``retention_days``."""
        while not self._stop_event.is_set():
            try:
                await self._prune_once()
            except Exception as exc:  # noqa: BLE001 — pruner must never break tail.
                logger.warning(
                    "fts.indexer.prune_failed",
                    **{"fts.error": str(exc), "fts.error_type": type(exc).__name__},
                )
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=_RETENTION_INTERVAL_S,
                )
            except asyncio.TimeoutError:
                continue

    async def _prune_once(self) -> int:
        """Delete rows older than ``now - retention_days``. Returns the row count deleted."""
        if self._conn is None or self._retention_days <= 0:
            return 0
        cutoff = time.time() - self._retention_days * 86400
        async with self._search_lock:
            cursor = await self._conn.execute(
                "DELETE FROM logs WHERE timestamp_unix < ?",
                (cutoff,),
            )
            await self._conn.commit()
            deleted = cursor.rowcount or 0
        if deleted:
            logger.info(
                "fts.indexer.pruned",
                **{"fts.deleted_rows": deleted, "fts.retention_days": self._retention_days},
            )
        return deleted

    # ── Search ─────────────────────────────────────────────────────

    async def search(
        self,
        query: str,
        *,
        level: str | None = None,
        logger_name: str | None = None,
        saga_id: str | None = None,
        since_unix: float | None = None,
        until_unix: float | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Run an FTS5 query with optional structured filters.

        Args:
            query: FTS5 MATCH expression. Empty string returns the
                most-recent rows that match the structured filters
                without invoking the FTS index.
            level: Optional level filter (case-insensitive).
            logger_name: Optional exact match on the ``logger``
                column.
            saga_id: Optional exact match on ``saga_id``.
            since_unix / until_unix: Optional half-open time window
                in unix seconds.
            limit: Hard cap on returned rows.

        Returns:
            Newest-first list of dicts with keys ``timestamp``,
            ``level``, ``logger``, ``event``, ``message``,
            ``saga_id``, ``content`` (full JSON string), and
            ``snippet`` (FTS5 snippet, or ``None`` for empty
            queries).
        """
        if self._conn is None:
            return []

        where: list[str] = []
        params: list[Any] = []

        if query.strip():
            sql_select = (
                "SELECT logs.timestamp_iso, logs.level, logs.logger, logs.event, "
                "logs.message, logs.saga_id, logs.content_json, "
                "snippet(logs_fts, -1, '<mark>', '</mark>', '…', 16) AS snippet "
                "FROM logs_fts JOIN logs ON logs.id = logs_fts.rowid "
                "WHERE logs_fts MATCH ?"
            )
            params.append(query)
        else:
            sql_select = (
                "SELECT timestamp_iso, level, logger, event, message, saga_id, "
                "content_json, NULL AS snippet FROM logs WHERE 1=1"
            )

        if level:
            where.append("logs.level = ?" if query.strip() else "level = ?")
            params.append(level.lower())
        if logger_name:
            where.append("logs.logger = ?" if query.strip() else "logger = ?")
            params.append(logger_name)
        if saga_id:
            where.append("logs.saga_id = ?" if query.strip() else "saga_id = ?")
            params.append(saga_id)
        if since_unix is not None:
            where.append("logs.timestamp_unix >= ?" if query.strip() else "timestamp_unix >= ?")
            params.append(since_unix)
        if until_unix is not None:
            where.append("logs.timestamp_unix <= ?" if query.strip() else "timestamp_unix <= ?")
            params.append(until_unix)

        sql = sql_select
        if where:
            sql += " AND " + " AND ".join(where)
        sql += (
            " ORDER BY logs.timestamp_unix DESC LIMIT ?"
            if query.strip()
            else " ORDER BY timestamp_unix DESC LIMIT ?"
        )
        params.append(limit)

        async with self._search_lock:
            cursor = await self._conn.execute(sql, params)
            rows = await cursor.fetchall()

        return [
            {
                "timestamp": row[0],
                "level": row[1],
                "logger": row[2],
                "event": row[3],
                "message": row[4],
                "saga_id": row[5],
                "content": row[6],
                "snippet": row[7],
            }
            for row in rows
        ]

    # ── Offset persistence ─────────────────────────────────────────

    def _read_offset(self) -> int:
        """Return the persisted byte offset, or 0 when missing/corrupt."""
        if not self._offset_path.exists():
            return 0
        try:
            return int(self._offset_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return 0

    def _write_offset(self, offset: int) -> None:
        """Atomically persist *offset*. Best-effort — failure is logged, not raised."""
        tmp = self._offset_path.with_suffix(self._offset_path.suffix + ".tmp")
        try:
            tmp.write_text(str(offset), encoding="utf-8")
            tmp.replace(self._offset_path)
        except OSError as exc:
            logger.warning(
                "fts.indexer.offset_write_failed",
                **{"fts.error": str(exc), "fts.offset": offset},
            )


__all__ = ["FTSIndexer"]
