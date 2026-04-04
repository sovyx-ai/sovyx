"""Integration tests — real SQLite database, no mocks.

Tests the full path from dashboard query functions through
DatabasePool to actual SQLite tables with real data.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

from sovyx.dashboard.conversations import (
    count_active_conversations,
    get_conversation_messages,
    list_conversations,
)

if TYPE_CHECKING:
    from pathlib import Path


# ── Lightweight pool that wraps a single aiosqlite connection ──


class _TestPool:
    """Minimal pool replacement for integration tests using in-memory SQLite."""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    class _ReadCtx:
        def __init__(self, conn: aiosqlite.Connection) -> None:
            self._conn = conn

        async def __aenter__(self) -> aiosqlite.Connection:
            return self._conn

        async def __aexit__(self, *args: object) -> None:
            pass

    def read(self) -> _ReadCtx:
        return self._ReadCtx(self._conn)


# ── Conversation Schema + Seed Data ──

CONV_SCHEMA = """\
CREATE TABLE conversations (
    id TEXT PRIMARY KEY,
    mind_id TEXT NOT NULL,
    person_id TEXT,
    channel TEXT NOT NULL,
    started_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_message_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    message_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',
    metadata TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE conversation_turns (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    tokens INTEGER,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

CONV_SEED = """\
INSERT INTO conversations (id, mind_id, person_id, channel, message_count, status, last_message_at)
VALUES
    ('conv-1', 'mind-1', 'person-1', 'telegram', 3, 'active', '2026-04-04T20:00:00'),
    ('conv-2', 'mind-1', 'person-2', 'signal', 5, 'active', '2026-04-04T19:00:00'),
    ('conv-3', 'mind-1', 'person-1', 'telegram', 1, 'ended', '2026-04-04T18:00:00');

INSERT INTO conversation_turns (id, conversation_id, role, content, created_at)
VALUES
    ('t1', 'conv-1', 'user', 'Hello Sovyx', '2026-04-04T20:00:01'),
    ('t2', 'conv-1', 'assistant', 'Hello! How can I help?', '2026-04-04T20:00:02'),
    ('t3', 'conv-1', 'user', 'Tell me about memory', '2026-04-04T20:00:03'),
    ('t4', 'conv-2', 'user', 'What is DDD?', '2026-04-04T19:00:01');
"""

# ── Brain Schema + Seed Data ──

BRAIN_SCHEMA = """\
CREATE TABLE concepts (
    id TEXT PRIMARY KEY,
    mind_id TEXT NOT NULL,
    name TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT 'fact',
    importance REAL NOT NULL DEFAULT 0.5,
    confidence REAL NOT NULL DEFAULT 0.5,
    access_count INTEGER NOT NULL DEFAULT 0,
    last_accessed TIMESTAMP,
    emotional_valence REAL NOT NULL DEFAULT 0.0,
    source TEXT NOT NULL DEFAULT 'conversation',
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE relations (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    relation_type TEXT NOT NULL DEFAULT 'related_to',
    weight REAL NOT NULL DEFAULT 0.5,
    co_occurrence_count INTEGER NOT NULL DEFAULT 1,
    last_activated TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

BRAIN_SEED = """\
INSERT INTO concepts (id, mind_id, name, category, importance, confidence, access_count)
VALUES
    ('c1', 'mind-1', 'Python', 'skill', 0.9, 0.95, 42),
    ('c2', 'mind-1', 'FastAPI', 'skill', 0.8, 0.9, 30),
    ('c3', 'mind-1', 'SQLite', 'fact', 0.7, 0.85, 15),
    ('c4', 'mind-1', 'DDD', 'fact', 0.6, 0.8, 10);

INSERT INTO relations (id, source_id, target_id, relation_type, weight)
VALUES
    ('r1', 'c1', 'c2', 'related_to', 0.85),
    ('r2', 'c2', 'c3', 'uses', 0.7),
    ('r3', 'c1', 'c3', 'related_to', 0.5);
"""


def _make_registry_with_pool(
    pool: _TestPool,
    *,
    pool_type: str = "conversation",
) -> MagicMock:
    """Create a mock registry that returns a real pool from DatabaseManager."""
    db_manager = MagicMock()

    if pool_type == "conversation":
        db_manager.get_conversation_pool.return_value = pool
        db_manager.get_system_pool.return_value = pool
    else:
        db_manager.get_brain_pool.return_value = pool

    registry = MagicMock()

    def is_registered(cls: type) -> bool:
        name = cls.__name__
        return name in ("DatabaseManager", "MindManager")

    registry.is_registered.side_effect = is_registered

    mind_manager = MagicMock()
    mind_manager.get_active_minds.return_value = ["mind-1"]

    async def resolve(cls: type) -> object:
        name = cls.__name__
        if name == "DatabaseManager":
            return db_manager
        if name == "MindManager":
            return mind_manager
        msg = f"Unknown: {name}"
        raise ValueError(msg)

    registry.resolve = AsyncMock(side_effect=resolve)
    return registry


# ── Conversation Integration Tests ──


class TestConversationIntegration:
    @pytest.fixture()
    async def conv_pool(self, tmp_path: Path) -> _TestPool:
        db_path = tmp_path / "conv.db"
        conn = await aiosqlite.connect(str(db_path))
        await conn.executescript(CONV_SCHEMA)
        await conn.executescript(CONV_SEED)
        await conn.commit()
        yield _TestPool(conn)  # type: ignore[misc]
        await conn.close()

    @pytest.mark.asyncio()
    async def test_list_conversations_real_db(self, conv_pool: _TestPool) -> None:
        registry = _make_registry_with_pool(conv_pool)
        result = await list_conversations(registry, limit=10)

        assert len(result) == 3
        assert result[0]["id"] == "conv-1"
        assert result[0]["participant"] == "person-1"
        assert result[0]["channel"] == "telegram"
        assert result[0]["message_count"] == 3
        assert result[0]["status"] == "active"
        assert result[1]["id"] == "conv-2"
        assert result[2]["id"] == "conv-3"
        assert result[2]["status"] == "ended"

    @pytest.mark.asyncio()
    async def test_list_conversations_with_limit(self, conv_pool: _TestPool) -> None:
        registry = _make_registry_with_pool(conv_pool)
        result = await list_conversations(registry, limit=2)
        assert len(result) == 2

    @pytest.mark.asyncio()
    async def test_list_conversations_with_offset(self, conv_pool: _TestPool) -> None:
        registry = _make_registry_with_pool(conv_pool)
        result = await list_conversations(registry, limit=10, offset=2)
        assert len(result) == 1
        assert result[0]["id"] == "conv-3"

    @pytest.mark.asyncio()
    async def test_get_messages_real_db(self, conv_pool: _TestPool) -> None:
        registry = _make_registry_with_pool(conv_pool)
        result = await get_conversation_messages(registry, "conv-1")

        assert len(result) == 3
        assert result[0]["role"] == "user"
        assert result[0]["content"] == "Hello Sovyx"
        assert result[1]["role"] == "assistant"
        assert result[2]["content"] == "Tell me about memory"

    @pytest.mark.asyncio()
    async def test_get_messages_empty_conversation(self, conv_pool: _TestPool) -> None:
        registry = _make_registry_with_pool(conv_pool)
        result = await get_conversation_messages(registry, "nonexistent")
        assert result == []

    @pytest.mark.asyncio()
    async def test_count_active_real_db(self, conv_pool: _TestPool) -> None:
        registry = _make_registry_with_pool(conv_pool)
        count = await count_active_conversations(registry)
        assert count == 2

    @pytest.mark.asyncio()
    async def test_get_messages_with_limit(self, conv_pool: _TestPool) -> None:
        registry = _make_registry_with_pool(conv_pool)
        result = await get_conversation_messages(registry, "conv-1", limit=1)
        assert len(result) == 1

    @pytest.mark.asyncio()
    async def test_conversation_pool_fallback_to_system(
        self, conv_pool: _TestPool,
    ) -> None:
        """When get_conversation_pool throws, falls back to system pool."""
        db_manager = MagicMock()
        db_manager.get_conversation_pool.side_effect = RuntimeError("no mind")
        db_manager.get_system_pool.return_value = conv_pool

        registry = MagicMock()
        registry.is_registered.return_value = True

        mind_manager = MagicMock()
        mind_manager.get_active_minds.return_value = ["mind-1"]

        async def resolve(cls: type) -> object:
            name = cls.__name__
            if name == "DatabaseManager":
                return db_manager
            if name == "MindManager":
                return mind_manager
            msg = f"Unknown: {name}"
            raise ValueError(msg)

        registry.resolve = AsyncMock(side_effect=resolve)

        result = await list_conversations(registry, limit=10)
        assert len(result) == 3

    @pytest.mark.asyncio()
    async def test_conversation_pool_both_fail(self) -> None:
        """When both mind pool and system pool fail, returns empty."""
        db_manager = MagicMock()
        db_manager.get_conversation_pool.side_effect = RuntimeError("no mind")
        db_manager.get_system_pool.side_effect = RuntimeError("no system")

        registry = MagicMock()
        registry.is_registered.return_value = True

        async def resolve(cls: type) -> object:
            name = cls.__name__
            if name == "DatabaseManager":
                return db_manager
            if name == "MindManager":
                return MagicMock(get_active_minds=MagicMock(return_value=["m1"]))
            msg = f"Unknown: {name}"
            raise ValueError(msg)

        registry.resolve = AsyncMock(side_effect=resolve)

        result = await list_conversations(registry)
        assert result == []

        count = await count_active_conversations(registry)
        assert count == 0


# ── Brain Graph Integration Tests ──


class TestBrainGraphIntegration:
    @pytest.fixture()
    async def brain_pool(self, tmp_path: Path) -> _TestPool:
        db_path = tmp_path / "brain.db"
        conn = await aiosqlite.connect(str(db_path))
        await conn.executescript(BRAIN_SCHEMA)
        await conn.executescript(BRAIN_SEED)
        await conn.commit()
        yield _TestPool(conn)  # type: ignore[misc]
        await conn.close()

    @pytest.mark.asyncio()
    async def test_brain_graph_real_db(self, brain_pool: _TestPool) -> None:
        registry = _make_registry_with_pool(brain_pool, pool_type="brain")

        from sovyx.dashboard.brain import _get_relations

        node_ids = {"c1", "c2", "c3", "c4"}
        links = await _get_relations(registry, node_ids)

        assert len(links) == 3
        sources = {link["source"] for link in links}
        targets = {link["target"] for link in links}
        assert "c1" in sources
        assert "c2" in sources or "c2" in targets

        for link in links:
            assert 0 <= link["weight"] <= 1.0
            assert link["relation_type"] in ("related_to", "uses")

    @pytest.mark.asyncio()
    async def test_relations_filters_to_node_set(
        self, brain_pool: _TestPool,
    ) -> None:
        registry = _make_registry_with_pool(brain_pool, pool_type="brain")

        from sovyx.dashboard.brain import _get_relations

        node_ids = {"c1", "c2"}
        links = await _get_relations(registry, node_ids)

        assert len(links) == 1
        assert links[0]["source"] == "c1"
        assert links[0]["target"] == "c2"

    @pytest.mark.asyncio()
    async def test_relations_empty_db(self, tmp_path: Path) -> None:
        db_path = tmp_path / "empty_brain.db"
        conn = await aiosqlite.connect(str(db_path))
        await conn.executescript(BRAIN_SCHEMA)
        await conn.commit()

        pool = _TestPool(conn)
        registry = _make_registry_with_pool(pool, pool_type="brain")

        from sovyx.dashboard.brain import _get_relations

        links = await _get_relations(registry, {"c1", "c2"})
        assert links == []

        await conn.close()
