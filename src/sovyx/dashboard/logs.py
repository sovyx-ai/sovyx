"""Dashboard log queries — read structured JSON log files.

Reads the RotatingFileHandler JSON log file and returns parsed entries.
Supports filtering by level, module, and text search.
"""

from __future__ import annotations

import json
from collections import deque
from typing import TYPE_CHECKING, Any

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from pathlib import Path

logger = get_logger(__name__)


def query_logs(
    log_file: Path | None,
    *,
    level: str | None = None,
    module: str | None = None,
    search: str | None = None,
    after: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Query structured JSON log file with filters.

    Reads from the end of file (most recent first).
    Returns up to ``limit`` matching entries.

    Args:
        log_file: Path to JSON log file. Returns [] if None or missing.
        level: Filter by log level (DEBUG, INFO, WARNING, ERROR).
        module: Filter by logger name (prefix match).
        search: Full-text search in event/message field.
        after: ISO-8601 timestamp — only return entries **after** this time.
            Used for incremental polling (dashboard fetches new logs since
            the last known timestamp).
        limit: Maximum entries to return.

    Returns:
        List of log entries (most recent first).
    """
    if log_file is None or not log_file.exists():
        return []

    try:
        return _read_and_filter(
            log_file,
            level=level,
            module=module,
            search=search,
            after=after,
            limit=limit,
        )
    except Exception:  # noqa: BLE001
        logger.debug("query_logs_failed", log_file=str(log_file))
        return []


def query_saga(
    log_file: Path | None,
    saga_id: str,
    *,
    limit: int = 10000,
) -> list[dict[str, Any]]:
    """Return every log entry whose ``saga_id`` matches *saga_id*.

    File-scan implementation (Phase 2 stop-gap). The FTS5 sidecar
    index in Phase 10 will replace the linear scan with an indexed
    lookup, but the response shape is identical so dashboard callers
    don't need to change.

    Reads the last ~8 MB of the log tail (covers an active saga's
    entire lifetime under normal voice/cognitive volume). Older sagas
    that fall outside the tail window will be reachable through FTS5
    once Phase 10 lands.

    Returns entries sorted chronologically (oldest first) so the
    saga reads as a story top-to-bottom.

    Args:
        log_file: Path to JSON log file. Returns [] if None or missing.
        saga_id: Exact saga identifier to filter on.
        limit: Maximum entries to return (cap to bound memory usage).

    Returns:
        List of log entries belonging to the saga, oldest first.
    """
    if log_file is None or not log_file.exists() or not saga_id:
        return []

    try:
        # Read a wider tail than /api/logs because a single saga can
        # legitimately produce hundreds of entries across many minutes.
        lines = _tail_lines(log_file, max_lines=limit * 4, max_bytes=8 * 1024 * 1024)
    except Exception:  # noqa: BLE001
        logger.debug("query_saga_failed_read", log_file=str(log_file))
        return []

    matches: list[dict[str, Any]] = []
    for line in lines:
        entry = _parse_line(line)
        if entry is None:
            continue
        if entry.get("saga_id") != saga_id:
            continue
        matches.append(entry)
        if len(matches) >= limit:
            break

    matches.sort(key=lambda e: str(e.get("timestamp", "")))
    return matches


def _read_and_filter(
    log_file: Path,
    *,
    level: str | None,
    module: str | None,
    search: str | None,
    after: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    """Read log file lines and apply filters."""
    results: deque[dict[str, Any]] = deque(maxlen=limit)

    lines = _tail_lines(log_file, max_lines=limit * 10)

    for line in reversed(lines):
        if len(results) >= limit:
            break

        entry = _parse_line(line)
        if entry is None:
            continue

        # Incremental filter: skip entries at or before the cursor.
        # Uses string comparison on ISO-8601 timestamps (lexicographic
        # ordering matches chronological ordering for ISO format).
        if after is not None:
            entry_ts = entry.get("timestamp", entry.get("ts", ""))
            if isinstance(entry_ts, str) and entry_ts <= after:
                continue

        if not _matches_filters(entry, level=level, module=module, search=search):
            continue

        results.append(entry)

    return list(results)


_DEFAULT_TAIL_BYTES = 1024 * 1024  # 1 MB


def _tail_lines(
    path: Path,
    max_lines: int = 1000,
    *,
    max_bytes: int = _DEFAULT_TAIL_BYTES,
) -> list[str]:
    """Read last N lines from a file efficiently using seek-from-end.

    For files up to ``max_bytes``, reads the whole file (fast enough).
    For larger files, seeks to the last ``max_bytes`` and reads from
    there. Default ``max_bytes`` of 1 MB matches the original
    ``/api/logs`` behaviour; saga queries override to 8 MB so a long
    saga's entire trail fits in the scanned window.

    Rotation resilience:
        ``RotatingFileHandler`` may rotate the file (rename to ``.1``)
        between our ``stat()`` and ``open()`` calls.  If the primary
        file vanishes or is empty (just rotated), we retry once after
        a short sleep and then fall back to the ``.1`` backup.
    """
    lines = _try_read_file(path, max_lines, max_bytes=max_bytes)
    if lines is not None:
        return lines

    # Primary file missing or empty — might be mid-rotation.
    # Brief pause then retry (rotation is fast, typically < 50ms).
    import time

    time.sleep(0.1)
    lines = _try_read_file(path, max_lines, max_bytes=max_bytes)
    if lines is not None:
        return lines

    # Still empty/missing — try the .1 backup (most recent rotated file).
    backup = path.parent / f"{path.name}.1"
    lines = _try_read_file(backup, max_lines, max_bytes=max_bytes)
    return lines if lines is not None else []


def _try_read_file(
    path: Path,
    max_lines: int,
    *,
    max_bytes: int = _DEFAULT_TAIL_BYTES,
) -> list[str] | None:
    """Attempt to read tail lines from a single file.

    Returns:
        List of lines if successful, None if file is missing/empty/unreadable.
    """
    try:
        file_size = path.stat().st_size
        if file_size == 0:
            return None

        with path.open("rb") as f:
            if file_size > max_bytes:
                f.seek(-max_bytes, 2)
                f.readline()  # Skip partial first line
            data = f.read()

        lines = data.decode("utf-8", errors="replace").splitlines()
        if len(lines) > max_lines:
            lines = lines[-max_lines:]
        return lines if lines else None
    except OSError:
        return None


def _parse_line(line: str) -> dict[str, Any] | None:
    """Parse and normalize a single JSON log line.

    Normalizes field names to the canonical schema expected by the
    dashboard frontend (``LogEntry`` TypeScript type):

    - ``timestamp`` ← ``timestamp`` or ``ts``
    - ``level`` ← ``level`` or ``severity`` (uppercased)
    - ``logger`` ← ``logger`` or ``module``
    - ``event`` ← ``event`` or ``message``

    Entries missing both ``timestamp``/``ts`` or both ``event``/``message``
    are discarded as corrupted (returns None).

    All extra fields are preserved for search and display.
    """
    line = line.strip()
    if not line:
        return None
    try:
        parsed: dict[str, Any] = json.loads(line)
    except json.JSONDecodeError:
        return None

    # ── Normalize required fields ──
    # timestamp (required)
    ts = parsed.get("timestamp") or parsed.get("ts")
    if not ts:
        return None  # Corrupted: no timestamp
    parsed["timestamp"] = ts
    parsed.pop("ts", None)

    # event (required)
    event = parsed.get("event") or parsed.get("message")
    if not event:
        return None  # Corrupted: no event/message
    parsed["event"] = event
    parsed.pop("message", None)  # Only remove if it was a fallback

    # level (optional, default INFO)
    level = parsed.get("level") or parsed.get("severity", "INFO")
    parsed["level"] = str(level).upper()
    parsed.pop("severity", None)

    # logger (optional, default unknown)
    logger_name = parsed.get("logger") or parsed.get("module", "unknown")
    parsed["logger"] = logger_name
    parsed.pop("module", None)  # Only remove if it was a fallback

    return parsed


def _matches_filters(
    entry: dict[str, Any],
    *,
    level: str | None,
    module: str | None,
    search: str | None,
) -> bool:
    """Check if a log entry matches all given filters."""
    if level is not None:
        entry_level = entry.get("level", entry.get("severity", "")).upper()
        if entry_level != level.upper():
            return False

    if module is not None:
        entry_logger = entry.get("logger", entry.get("module", ""))
        if not str(entry_logger).startswith(module):
            return False

    if search is not None:
        search_lower = search.lower()
        # Fast path: check event/message field
        event = str(entry.get("event", entry.get("message", ""))).lower()
        if search_lower in event:
            return True  # Early match — skip other filters below
        # Medium path: check all string values without serialization
        for val in entry.values():
            if isinstance(val, str) and search_lower in val.lower():
                return True
        # Slow path: nested dicts/lists need json serialization
        has_nested = any(isinstance(v, (dict, list)) for v in entry.values())
        return has_nested and search_lower in json.dumps(entry, default=str).lower()

    return True
