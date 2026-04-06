"""VAL-05: Coverage gaps for dashboard/conversations.py.

Covers all exception/fallback paths:
- _resolve_person_names: empty list early return, DB error
- list_conversations: exception during query
- get_conversation_messages: exception during query
- count_active_conversations: exception during query
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from sovyx.dashboard.conversations import (
    _resolve_person_names,
    count_active_conversations,
    get_conversation_messages,
    list_conversations,
)


class TestResolvePersonNames:
    @pytest.mark.asyncio()
    async def test_empty_person_ids_returns_empty(self) -> None:
        """Empty list short-circuits to {}."""
        registry = MagicMock()
        result = await _resolve_person_names(registry, [])
        assert result == {}

    @pytest.mark.asyncio()
    async def test_db_error_returns_empty(self) -> None:
        """When DB resolve fails, returns {} gracefully."""
        registry = MagicMock()
        registry.resolve = AsyncMock(side_effect=RuntimeError("db broken"))
        result = await _resolve_person_names(registry, ["person-1", "person-2"])
        assert result == {}

    @pytest.mark.asyncio()
    async def test_query_error_returns_empty(self) -> None:
        """When the SQL query fails inside the read context, returns {} gracefully."""
        from sovyx.persistence.manager import DatabaseManager

        cursor = AsyncMock()
        cursor.fetchall = AsyncMock(side_effect=RuntimeError("SQL error"))

        conn = AsyncMock()
        conn.execute = AsyncMock(return_value=cursor)

        read_ctx = AsyncMock()
        read_ctx.__aenter__ = AsyncMock(return_value=conn)
        read_ctx.__aexit__ = AsyncMock(return_value=False)

        registry = MagicMock()
        mock_db = MagicMock(spec=DatabaseManager)
        mock_pool = MagicMock()
        mock_pool.read = MagicMock(return_value=read_ctx)
        mock_db.get_system_pool.return_value = mock_pool
        registry.resolve = AsyncMock(return_value=mock_db)

        result = await _resolve_person_names(registry, ["person-1"])
        assert result == {}

    @pytest.mark.asyncio()
    async def test_success_returns_name_map(self) -> None:
        """When query succeeds, returns {person_id: display_name} mapping."""
        from sovyx.persistence.manager import DatabaseManager

        # Simulate successful DB query returning person rows
        rows = [("p-1", "Alice"), ("p-2", "Bob")]
        cursor = AsyncMock()
        cursor.fetchall = AsyncMock(return_value=rows)

        conn = AsyncMock()
        conn.execute = AsyncMock(return_value=cursor)

        read_ctx = AsyncMock()
        read_ctx.__aenter__ = AsyncMock(return_value=conn)
        read_ctx.__aexit__ = AsyncMock(return_value=False)

        registry = MagicMock()
        mock_db = MagicMock(spec=DatabaseManager)
        mock_pool = MagicMock()
        mock_pool.read = MagicMock(return_value=read_ctx)
        mock_db.get_system_pool.return_value = mock_pool
        registry.resolve = AsyncMock(return_value=mock_db)

        result = await _resolve_person_names(registry, ["p-1", "p-2"])
        assert result == {"p-1": "Alice", "p-2": "Bob"}


class TestListConversationsError:
    @pytest.mark.asyncio()
    async def test_query_error_returns_empty(self) -> None:
        """When the DB query fails, returns [] gracefully."""
        from sovyx.persistence.manager import DatabaseManager

        registry = MagicMock()
        registry.is_registered.return_value = True

        mock_db = MagicMock(spec=DatabaseManager)
        # get_conversation_pool works but read() context manager fails
        mock_pool = MagicMock()
        mock_pool.read.side_effect = RuntimeError("pool dead")
        mock_db.get_conversation_pool.return_value = mock_pool
        registry.resolve = AsyncMock(return_value=mock_db)

        result = await list_conversations(registry, limit=10, offset=0)
        assert result == []


class TestGetConversationMessagesError:
    @pytest.mark.asyncio()
    async def test_query_error_returns_empty(self) -> None:
        """When the DB query fails, returns [] gracefully."""
        from sovyx.persistence.manager import DatabaseManager

        registry = MagicMock()
        registry.is_registered.return_value = True

        mock_db = MagicMock(spec=DatabaseManager)
        mock_pool = MagicMock()
        mock_pool.read.side_effect = RuntimeError("pool dead")
        mock_db.get_conversation_pool.return_value = mock_pool
        registry.resolve = AsyncMock(return_value=mock_db)

        result = await get_conversation_messages(registry, "conv-123", limit=50)
        assert result == []


class TestCountActiveConversationsError:
    @pytest.mark.asyncio()
    async def test_query_error_returns_zero(self) -> None:
        """When the DB query fails, returns 0 gracefully."""
        from sovyx.persistence.manager import DatabaseManager

        registry = MagicMock()
        registry.is_registered.return_value = True

        mock_db = MagicMock(spec=DatabaseManager)
        mock_pool = MagicMock()
        mock_pool.read.side_effect = RuntimeError("pool dead")
        mock_db.get_conversation_pool.return_value = mock_pool
        registry.resolve = AsyncMock(return_value=mock_db)

        result = await count_active_conversations(registry)
        assert result == 0
