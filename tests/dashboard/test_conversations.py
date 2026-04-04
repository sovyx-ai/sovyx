"""Tests for sovyx.dashboard.conversations — conversation queries."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sovyx.dashboard.conversations import (
    count_active_conversations,
    get_conversation_messages,
    list_conversations,
)


def _make_registry_with_pool(rows: list[tuple[object, ...]]) -> MagicMock:
    """Create a mock registry where _get_conversation_pool returns a pool."""
    cursor = AsyncMock()
    cursor.fetchall = AsyncMock(return_value=rows)
    cursor.fetchone = AsyncMock(return_value=rows[0] if rows else None)

    conn = AsyncMock()
    conn.execute = AsyncMock(return_value=cursor)

    read_ctx = AsyncMock()
    read_ctx.__aenter__ = AsyncMock(return_value=conn)
    read_ctx.__aexit__ = AsyncMock(return_value=False)

    pool = MagicMock()
    pool.read = MagicMock(return_value=read_ctx)

    return MagicMock(), pool


class TestListConversations:
    @pytest.mark.asyncio()
    async def test_returns_conversations(self) -> None:
        rows = [
            ("conv-1", "person-1", "telegram", 5, "2026-04-04T19:00:00", "active"),
            ("conv-2", "person-2", "signal", 3, "2026-04-04T18:00:00", "ended"),
        ]
        registry, pool = _make_registry_with_pool(rows)

        with patch(
            "sovyx.dashboard.conversations._get_conversation_pool",
            new_callable=AsyncMock,
            return_value=pool,
        ):
            result = await list_conversations(registry, limit=10)

        assert len(result) == 2
        assert result[0]["id"] == "conv-1"
        assert result[0]["participant"] == "person-1"
        assert result[0]["channel"] == "telegram"
        assert result[0]["message_count"] == 5
        assert result[1]["status"] == "ended"

    @pytest.mark.asyncio()
    async def test_empty_when_no_pool(self) -> None:
        registry = MagicMock()
        with patch(
            "sovyx.dashboard.conversations._get_conversation_pool",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await list_conversations(registry)
        assert result == []


class TestGetConversationMessages:
    @pytest.mark.asyncio()
    async def test_returns_messages(self) -> None:
        rows = [
            ("turn-1", "user", "Hello", "2026-04-04T19:00:00"),
            ("turn-2", "assistant", "Hi!", "2026-04-04T19:00:01"),
        ]
        registry, pool = _make_registry_with_pool(rows)

        with patch(
            "sovyx.dashboard.conversations._get_conversation_pool",
            new_callable=AsyncMock,
            return_value=pool,
        ):
            result = await get_conversation_messages(registry, "conv-1")

        assert len(result) == 2
        assert result[0]["role"] == "user"
        assert result[0]["content"] == "Hello"
        assert result[1]["role"] == "assistant"

    @pytest.mark.asyncio()
    async def test_empty_when_no_pool(self) -> None:
        registry = MagicMock()
        with patch(
            "sovyx.dashboard.conversations._get_conversation_pool",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await get_conversation_messages(registry, "conv-1")
        assert result == []


class TestCountActiveConversations:
    @pytest.mark.asyncio()
    async def test_returns_count(self) -> None:
        registry, pool = _make_registry_with_pool([(5,)])

        with patch(
            "sovyx.dashboard.conversations._get_conversation_pool",
            new_callable=AsyncMock,
            return_value=pool,
        ):
            result = await count_active_conversations(registry)
        assert result == 5

    @pytest.mark.asyncio()
    async def test_zero_when_no_pool(self) -> None:
        registry = MagicMock()
        with patch(
            "sovyx.dashboard.conversations._get_conversation_pool",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await count_active_conversations(registry)
        assert result == 0

    @pytest.mark.asyncio()
    async def test_survives_error(self) -> None:
        registry = MagicMock()
        with patch(
            "sovyx.dashboard.conversations._get_conversation_pool",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ):
            result = await count_active_conversations(registry)
        assert result == 0
