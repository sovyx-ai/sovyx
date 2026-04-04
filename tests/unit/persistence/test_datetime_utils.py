"""Tests for sovyx.persistence.datetime_utils — SQLite datetime normalisation."""

from __future__ import annotations

from datetime import UTC, datetime, timezone

from sovyx.persistence.datetime_utils import parse_db_datetime


class TestParseDbDatetime:
    """parse_db_datetime normalises SQLite values to timezone-aware UTC."""

    def test_none_returns_none(self) -> None:
        assert parse_db_datetime(None) is None

    def test_naive_string_becomes_utc(self) -> None:
        result = parse_db_datetime("2026-03-15 10:30:00")
        assert result is not None
        assert result.tzinfo is UTC
        assert result.hour == 10

    def test_aware_string_preserved(self) -> None:
        result = parse_db_datetime("2026-03-15T10:30:00+00:00")
        assert result is not None
        assert result.tzinfo is not None
        assert result.hour == 10

    def test_z_suffix_handled(self) -> None:
        result = parse_db_datetime("2026-03-15T10:30:00Z")
        assert result is not None
        assert result.tzinfo is not None

    def test_naive_datetime_becomes_utc(self) -> None:
        dt = datetime(2026, 3, 15, 10, 30, 0)  # noqa: DTZ001
        result = parse_db_datetime(dt)
        assert result is not None
        assert result.tzinfo is UTC
        assert result.hour == 10

    def test_aware_datetime_passthrough(self) -> None:
        dt = datetime(2026, 3, 15, 10, 30, 0, tzinfo=UTC)
        result = parse_db_datetime(dt)
        assert result is not None
        assert result.tzinfo is UTC
        assert result == dt

    def test_non_utc_timezone_preserved(self) -> None:
        tz = timezone.utc  # Same as UTC for this test
        dt = datetime(2026, 3, 15, 10, 30, 0, tzinfo=tz)
        result = parse_db_datetime(dt)
        assert result is not None
        assert result.tzinfo is not None

    def test_sqlite_current_timestamp_format(self) -> None:
        """SQLite CURRENT_TIMESTAMP produces 'YYYY-MM-DD HH:MM:SS' (no tz)."""
        result = parse_db_datetime("2026-04-04 03:15:00")
        assert result is not None
        assert result.tzinfo is UTC
        assert result.year == 2026
        assert result.month == 4
        assert result.second == 0
