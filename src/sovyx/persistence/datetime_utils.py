"""Datetime parsing utilities for SQLite ↔ Python conversion.

SQLite stores datetime as TEXT (ISO-8601). Python's ``fromisoformat()``
may return naive datetimes depending on the stored format.  This module
normalises on read so the rest of the codebase always works with
timezone-aware UTC datetimes.

Design decision (v0.1): normalise on read — zero migration needed.
See ``sovyx-imm-d4-persistence`` §3 for full rationale.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import overload


@overload
def parse_db_datetime(value: None) -> None: ...


@overload
def parse_db_datetime(value: str | datetime) -> datetime: ...


def parse_db_datetime(value: str | datetime | None) -> datetime | None:
    """Parse a datetime value from SQLite into a timezone-aware UTC datetime.

    Handles three cases:
    1. ``None`` → ``None``
    2. ``datetime`` (already parsed) → ensure timezone-aware
    3. ``str`` (ISO-8601 from SQLite) → parse and ensure timezone-aware

    Naive datetimes are assumed to be UTC (SQLite default is
    ``CURRENT_TIMESTAMP`` which produces UTC).

    Args:
        value: Raw value from SQLite row — may be str, datetime, or None.

    Returns:
        Timezone-aware UTC datetime, or None if input is None.
    """
    if value is None:
        return None

    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value

    # str from SQLite — may or may not have timezone suffix
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed
