"""Tests for sovyx.persistence.schemas.conversations."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from sovyx.persistence.migrations import MigrationRunner
from sovyx.persistence.pool import DatabasePool
from sovyx.persistence.schemas.conversations import get_conversation_migrations

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
async def pool(tmp_path: Path) -> DatabasePool:
    """Initialized pool."""
    p = DatabasePool(db_path=tmp_path / "conv.db", read_pool_size=1)
    await p.initialize()
    yield p  # type: ignore[misc]
    await p.close()


@pytest.fixture
async def migrated(pool: DatabasePool) -> DatabasePool:
    """Pool with conversation schema applied."""
    runner = MigrationRunner(pool)
    await runner.initialize()
    await runner.run_migrations(get_conversation_migrations())
    return pool


class TestConversationMigrations:
    """Conversation schema tests."""

    def test_returns_one_migration(self) -> None:
        assert len(get_conversation_migrations()) == 1

    async def test_applies_without_error(self, pool: DatabasePool) -> None:
        runner = MigrationRunner(pool)
        await runner.initialize()
        applied = await runner.run_migrations(get_conversation_migrations())
        assert applied == 1

    async def test_tables_created(self, migrated: DatabasePool) -> None:
        async with migrated.read() as conn:
            cursor = await conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            rows = await cursor.fetchall()
            tables = {r[0] for r in rows}
            assert "conversations" in tables
            assert "conversation_turns" in tables

    async def test_fts5_insert_trigger(self, migrated: DatabasePool) -> None:
        """INSERT turn → searchable via FTS5."""
        async with migrated.write() as conn:
            await conn.execute(
                "INSERT INTO conversations (id, mind_id, channel) "
                "VALUES ('conv1', 'aria', 'telegram')"
            )
            await conn.execute(
                "INSERT INTO conversation_turns (id, conversation_id, role, content) "
                "VALUES ('t1', 'conv1', 'user', 'quero café com leite')"
            )
            await conn.commit()

        async with migrated.read() as conn:
            cursor = await conn.execute(
                "SELECT content FROM turns_fts WHERE turns_fts MATCH 'café'"
            )
            row = await cursor.fetchone()
            assert row is not None
            assert "café" in row[0]

    async def test_fts5_delete_trigger(self, migrated: DatabasePool) -> None:
        """DELETE turn → removed from FTS5."""
        async with migrated.write() as conn:
            await conn.execute(
                "INSERT INTO conversations (id, mind_id, channel) "
                "VALUES ('conv1', 'aria', 'telegram')"
            )
            await conn.execute(
                "INSERT INTO conversation_turns (id, conversation_id, role, content) "
                "VALUES ('t1', 'conv1', 'user', 'delete this message')"
            )
            await conn.commit()
            await conn.execute("DELETE FROM conversation_turns WHERE id = 't1'")
            await conn.commit()

        async with migrated.read() as conn:
            cursor = await conn.execute(
                "SELECT content FROM turns_fts WHERE turns_fts MATCH 'delete'"
            )
            assert await cursor.fetchone() is None

    async def test_fts5_update_trigger(self, migrated: DatabasePool) -> None:
        """UPDATE turn → FTS5 reflects new content."""
        async with migrated.write() as conn:
            await conn.execute(
                "INSERT INTO conversations (id, mind_id, channel) VALUES ('conv1', 'aria', 'cli')"
            )
            await conn.execute(
                "INSERT INTO conversation_turns (id, conversation_id, role, content) "
                "VALUES ('t1', 'conv1', 'assistant', 'original response')"
            )
            await conn.commit()
            await conn.execute(
                "UPDATE conversation_turns SET content = 'updated response' WHERE id = 't1'"
            )
            await conn.commit()

        async with migrated.read() as conn:
            cursor = await conn.execute(
                "SELECT content FROM turns_fts WHERE turns_fts MATCH 'original'"
            )
            assert await cursor.fetchone() is None
            cursor = await conn.execute(
                "SELECT content FROM turns_fts WHERE turns_fts MATCH 'updated'"
            )
            assert await cursor.fetchone() is not None

    async def test_fts5_special_chars(self, migrated: DatabasePool) -> None:
        """FTS5 handles accents, emojis, quotes without crash."""
        async with migrated.write() as conn:
            await conn.execute(
                "INSERT INTO conversations (id, mind_id, channel) "
                "VALUES ('conv1', 'aria', 'telegram')"
            )
            for i, text in enumerate(
                [
                    "ação com acentuação",
                    '🔥 fire emoji "quoted"',
                    "café ☕ délicieux",
                ]
            ):
                await conn.execute(
                    "INSERT INTO conversation_turns "
                    "(id, conversation_id, role, content) "
                    "VALUES (?, 'conv1', 'user', ?)",
                    (f"t{i}", text),
                )
            await conn.commit()

        async with migrated.read() as conn:
            cursor = await conn.execute(
                "SELECT content FROM turns_fts WHERE turns_fts MATCH 'ação'"
            )
            assert await cursor.fetchone() is not None

    async def test_cascade_delete_conversation(self, migrated: DatabasePool) -> None:
        """DELETE conversation → CASCADE deletes turns."""
        async with migrated.write() as conn:
            await conn.execute(
                "INSERT INTO conversations (id, mind_id, channel) "
                "VALUES ('conv1', 'aria', 'telegram')"
            )
            await conn.execute(
                "INSERT INTO conversation_turns (id, conversation_id, role, content) "
                "VALUES ('t1', 'conv1', 'user', 'hello')"
            )
            await conn.commit()
            await conn.execute("DELETE FROM conversations WHERE id = 'conv1'")
            await conn.commit()

        async with migrated.read() as conn:
            cursor = await conn.execute("SELECT COUNT(*) FROM conversation_turns")
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] == 0
