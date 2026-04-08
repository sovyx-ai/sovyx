"""Tests for dashboard activity timeline endpoint and queries."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from sovyx.dashboard.activity import (
    _iso,
    _parse_metadata,
    _query_concepts,
    _query_consolidations,
    _query_conversations,
    _query_episodes,
    _query_messages,
    get_activity_timeline,
)

# ── Helpers ──────────────────────────────────────────────────────────────


class MockCursor:
    """Mock aiosqlite cursor."""

    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    async def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows


class MockConnection:
    """Mock aiosqlite connection — can dispatch rows by SQL table name."""

    def __init__(
        self,
        rows: list[tuple[Any, ...]] | None = None,
        *,
        rows_by_table: dict[str, list[tuple[Any, ...]]] | None = None,
    ) -> None:
        self._default = rows or []
        self._by_table = rows_by_table or {}

    async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> MockCursor:
        sql_lower = sql.lower()
        for table, table_rows in self._by_table.items():
            if table in sql_lower:
                return MockCursor(table_rows)
        return MockCursor(self._default)


class _ReadCM:
    """Async context manager for MockPool.read()."""

    def __init__(self, conn: MockConnection) -> None:
        self._conn = conn

    async def __aenter__(self) -> MockConnection:
        return self._conn

    async def __aexit__(self, *args: object) -> None:
        pass


class MockPool:
    """Mock DatabasePool with a read() context manager."""

    def __init__(
        self,
        rows: list[tuple[Any, ...]] | None = None,
        *,
        rows_by_table: dict[str, list[tuple[Any, ...]]] | None = None,
    ) -> None:
        self._conn = MockConnection(rows, rows_by_table=rows_by_table)

    def read(self) -> _ReadCM:
        return _ReadCM(self._conn)


class _FailCM:
    """Async CM that raises on enter — simulates missing table."""

    async def __aenter__(self) -> None:
        msg = "no such table: consolidation_log"
        raise Exception(msg)  # noqa: TRY002

    async def __aexit__(self, *args: object) -> None:
        pass


class FailPool:
    """Pool whose read() always fails."""

    def read(self) -> _FailCM:
        return _FailCM()


# ── Unit tests: helpers ──────────────────────────────────────────────────


class TestIso:
    """Test _iso timestamp normalizer."""

    def test_sqlite_format(self) -> None:
        assert _iso("2026-04-08 14:01:49") == "2026-04-08T14:01:49Z"

    def test_already_iso(self) -> None:
        assert _iso("2026-04-08T14:01:49Z") == "2026-04-08T14:01:49Z"

    def test_with_timezone(self) -> None:
        result = _iso("2026-04-08T14:01:49+00:00")
        assert "2026-04-08T14:01:49+00:00" in result

    def test_none_returns_now(self) -> None:
        result = _iso(None)
        assert result.startswith("20")
        assert "T" in result


class TestParseMetadata:
    """Test _parse_metadata JSON parser."""

    def test_valid_json(self) -> None:
        assert _parse_metadata('{"model": "gpt-4o", "cost_usd": 0.01}') == {
            "model": "gpt-4o",
            "cost_usd": 0.01,
        }

    def test_none(self) -> None:
        assert _parse_metadata(None) == {}

    def test_empty_string(self) -> None:
        assert _parse_metadata("") == {}

    def test_invalid_json(self) -> None:
        assert _parse_metadata("{broken") == {}

    def test_default_empty(self) -> None:
        assert _parse_metadata("{}") == {}


# ── Unit tests: query functions ──────────────────────────────────────────


class TestQueryConversations:
    """Test _query_conversations."""

    async def test_returns_entries(self) -> None:
        rows = [
            (
                "conv-1",
                "dashboard",
                5,
                "2026-04-08 14:00:00",
                "2026-04-08 14:05:00",
                "active",
            ),
            (
                "conv-2",
                "telegram",
                3,
                "2026-04-08 13:00:00",
                "2026-04-08 13:30:00",
                "active",
            ),
        ]
        pool = MockPool(rows)
        cutoff = "2026-04-08T00:00:00Z"
        result = await _query_conversations(pool, cutoff, 50)  # type: ignore[arg-type]

        assert len(result) == 2
        assert result[0]["type"] == "conversation"
        assert result[0]["data"]["conversation_id"] == "conv-1"
        assert result[0]["data"]["message_count"] == 5
        assert result[0]["data"]["channel"] == "dashboard"

    async def test_empty(self) -> None:
        pool = MockPool([])
        cutoff = "2026-04-08T00:00:00Z"
        result = await _query_conversations(pool, cutoff, 50)  # type: ignore[arg-type]
        assert result == []


class TestQueryMessages:
    """Test _query_messages."""

    async def test_returns_with_preview(self) -> None:
        long_content = "x" * 200
        meta = '{"model": "gpt-4o"}'
        rows = [
            (
                "turn-1",
                "conv-1",
                "user",
                long_content,
                50,
                meta,
                "2026-04-08 14:01:40",
            ),
        ]
        pool = MockPool(rows)
        cutoff = "2026-04-08T00:00:00Z"
        result = await _query_messages(pool, cutoff, 50)  # type: ignore[arg-type]

        assert len(result) == 1
        assert result[0]["type"] == "message"
        assert result[0]["data"]["role"] == "user"
        assert len(result[0]["data"]["preview"]) == 123  # 120 + "..."  # noqa: PLR2004
        assert result[0]["data"]["model"] == "gpt-4o"

    async def test_short_content_no_ellipsis(self) -> None:
        rows = [
            ("turn-1", "conv-1", "user", "Hello", 5, "{}", "2026-04-08 14:01:40"),
        ]
        pool = MockPool(rows)
        cutoff = "2026-04-08T00:00:00Z"
        result = await _query_messages(pool, cutoff, 50)  # type: ignore[arg-type]
        assert result[0]["data"]["preview"] == "Hello"

    async def test_null_metadata(self) -> None:
        rows = [
            ("turn-1", "conv-1", "assistant", "Hi", 10, None, "2026-04-08 14:01:44"),
        ]
        pool = MockPool(rows)
        cutoff = "2026-04-08T00:00:00Z"
        result = await _query_messages(pool, cutoff, 50)  # type: ignore[arg-type]
        assert result[0]["data"]["model"] == ""
        assert result[0]["data"]["cost_usd"] == 0.0


class TestQueryConcepts:
    """Test _query_concepts with minute grouping."""

    async def test_groups_by_minute(self) -> None:
        rows = [
            ("c1", "Python", "fact", 0.8, "2026-04-08 14:01:50"),
            ("c2", "FastAPI", "fact", 0.7, "2026-04-08 14:01:49"),
            ("c3", "PostgreSQL", "fact", 0.6, "2026-04-08 14:02:03"),
        ]
        pool = MockPool(rows)
        cutoff = "2026-04-08T00:00:00Z"
        result = await _query_concepts(pool, cutoff, 50)  # type: ignore[arg-type]

        # 2 groups: 14:01 (Python+FastAPI) and 14:02 (PostgreSQL)
        assert len(result) == 2  # noqa: PLR2004
        assert result[0]["type"] == "concepts_learned"

        group_2 = [e for e in result if e["data"]["count"] == 2]
        assert len(group_2) == 1
        names = [c["name"] for c in group_2[0]["data"]["concepts"]]
        assert "Python" in names
        assert "FastAPI" in names

    async def test_empty(self) -> None:
        pool = MockPool([])
        cutoff = "2026-04-08T00:00:00Z"
        result = await _query_concepts(pool, cutoff, 50)  # type: ignore[arg-type]
        assert result == []


class TestQueryEpisodes:
    """Test _query_episodes."""

    async def test_returns_entries(self) -> None:
        rows = [("ep-1", "conv-1", 0.85, "2026-04-08 14:01:49")]
        pool = MockPool(rows)
        cutoff = "2026-04-08T00:00:00Z"
        result = await _query_episodes(pool, cutoff, 50)  # type: ignore[arg-type]

        assert len(result) == 1
        assert result[0]["type"] == "episode_encoded"
        assert result[0]["data"]["importance"] == 0.85  # noqa: PLR2004

    async def test_null_importance(self) -> None:
        rows = [("ep-1", "conv-1", None, "2026-04-08 14:01:49")]
        pool = MockPool(rows)
        cutoff = "2026-04-08T00:00:00Z"
        result = await _query_episodes(pool, cutoff, 50)  # type: ignore[arg-type]
        assert result[0]["data"]["importance"] == 0.0


class TestQueryConsolidations:
    """Test _query_consolidations."""

    async def test_returns_entries(self) -> None:
        rows = [("cl-1", 5, 3, 12, "2026-04-08 14:05:00")]
        pool = MockPool(rows)
        cutoff = "2026-04-08T00:00:00Z"
        result = await _query_consolidations(pool, cutoff, 50)  # type: ignore[arg-type]

        assert len(result) == 1
        assert result[0]["type"] == "consolidation"
        assert result[0]["data"]["merged"] == 5  # noqa: PLR2004
        assert result[0]["data"]["pruned"] == 3  # noqa: PLR2004
        assert result[0]["data"]["strengthened"] == 12  # noqa: PLR2004

    async def test_missing_table_returns_empty(self) -> None:
        """Gracefully handle missing consolidation_log table."""
        cutoff = "2026-04-08T00:00:00Z"
        result = await _query_consolidations(FailPool(), cutoff, 50)  # type: ignore[arg-type]
        assert result == []


# ── Integration test: get_activity_timeline ──────────────────────────────


class TestGetActivityTimeline:
    """Test the full timeline assembly."""

    async def test_merges_and_sorts(self) -> None:
        """Timeline entries from multiple sources are merged and sorted."""
        registry = MagicMock()
        conv_pool = MockPool(
            rows_by_table={
                "conversations": [
                    (
                        "conv-1",
                        "dashboard",
                        3,
                        "2026-04-08 14:00:00",
                        "2026-04-08 14:02:00",
                        "active",
                    ),
                ],
                "conversation_turns": [],
            },
        )
        brain_pool = MockPool(
            rows_by_table={"concepts": [], "episodes": [], "consolidation_log": []},
        )

        with (
            patch(
                "sovyx.dashboard.activity._get_conversation_pool",
                new_callable=AsyncMock,
                return_value=conv_pool,
            ),
            patch(
                "sovyx.dashboard.activity._get_brain_pool",
                new_callable=AsyncMock,
                return_value=brain_pool,
            ),
        ):
            result = await get_activity_timeline(registry, hours=24, limit=100)

        assert "entries" in result
        assert "meta" in result
        assert result["meta"]["hours"] == 24  # noqa: PLR2004
        assert len(result["entries"]) >= 1

    async def test_no_pools_returns_empty(self) -> None:
        """When no DB pools available, returns empty timeline."""
        registry = MagicMock()

        with (
            patch(
                "sovyx.dashboard.activity._get_conversation_pool",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "sovyx.dashboard.activity._get_brain_pool",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            result = await get_activity_timeline(registry, hours=24, limit=100)

        assert result["entries"] == []
        assert result["meta"]["total_before_limit"] == 0

    async def test_respects_limit(self) -> None:
        """Limit parameter trims results."""
        registry = MagicMock()
        conv_rows = [
            (
                f"conv-{i}",
                "dashboard",
                i,
                "2026-04-08 14:00:00",
                f"2026-04-08 14:0{i}:00",
                "active",
            )
            for i in range(5)
        ]
        conv_pool = MockPool(
            rows_by_table={"conversations": conv_rows, "conversation_turns": []},
        )

        with (
            patch(
                "sovyx.dashboard.activity._get_conversation_pool",
                new_callable=AsyncMock,
                return_value=conv_pool,
            ),
            patch(
                "sovyx.dashboard.activity._get_brain_pool",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            result = await get_activity_timeline(registry, hours=24, limit=3)

        assert len(result["entries"]) == 3  # noqa: PLR2004
        assert result["meta"]["total_before_limit"] == 5  # noqa: PLR2004

    async def test_entries_sorted_desc(self) -> None:
        """Entries should be sorted newest-first."""
        registry = MagicMock()
        conv_rows = [
            ("conv-1", "dashboard", 1, "2026-04-08 13:00:00", "2026-04-08 13:00:00", "active"),
            ("conv-2", "dashboard", 2, "2026-04-08 15:00:00", "2026-04-08 15:00:00", "active"),
            ("conv-3", "dashboard", 3, "2026-04-08 14:00:00", "2026-04-08 14:00:00", "active"),
        ]
        conv_pool = MockPool(
            rows_by_table={"conversations": conv_rows, "conversation_turns": []},
        )

        with (
            patch(
                "sovyx.dashboard.activity._get_conversation_pool",
                new_callable=AsyncMock,
                return_value=conv_pool,
            ),
            patch(
                "sovyx.dashboard.activity._get_brain_pool",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            result = await get_activity_timeline(registry, hours=24, limit=100)

        timestamps = [e["timestamp"] for e in result["entries"]]
        assert timestamps == sorted(timestamps, reverse=True)

    async def test_exception_in_conv_still_returns_brain(self) -> None:
        """If conversation queries fail, brain data still returned."""
        registry = MagicMock()

        failing_conv_pool = MagicMock()
        failing_conv_pool.read = MagicMock(side_effect=Exception("DB error"))

        brain_pool = MockPool(
            rows_by_table={
                "episodes": [("ep-1", "conv-1", 0.9, "2026-04-08 14:01:49")],
                "concepts": [],
                "consolidation_log": [],
            },
        )

        with (
            patch(
                "sovyx.dashboard.activity._get_conversation_pool",
                new_callable=AsyncMock,
                return_value=failing_conv_pool,
            ),
            patch(
                "sovyx.dashboard.activity._get_brain_pool",
                new_callable=AsyncMock,
                return_value=brain_pool,
            ),
        ):
            result = await get_activity_timeline(registry, hours=24, limit=100)

        assert len(result["entries"]) >= 1
