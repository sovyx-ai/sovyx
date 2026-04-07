"""Tests for sovyx.bridge.sessions — ConversationTracker."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from sovyx.bridge.sessions import ConversationTracker
from sovyx.engine.types import (
    ChannelType,
    MindId,
    PersonId,
)
from sovyx.persistence.pool import DatabasePool
from sovyx.persistence.schemas.conversations import (
    get_conversation_migrations,
)

MIND = MindId("aria")
PERSON = PersonId("person-1")


@pytest.fixture
async def pool(tmp_path: object) -> DatabasePool:
    """Create a test database pool with conversation schema."""
    from pathlib import Path

    db_path = Path(str(tmp_path)) / "conversations.db"
    p = DatabasePool(db_path)
    await p.initialize()
    from sovyx.persistence.migrations import MigrationRunner

    mgr = MigrationRunner(p)
    await mgr.initialize()
    await mgr.run_migrations(get_conversation_migrations())
    return p


@pytest.fixture
def tracker(pool: DatabasePool) -> ConversationTracker:
    return ConversationTracker(pool, timeout_minutes=30)


class TestGetOrCreate:
    """Conversation creation and resumption."""

    async def test_creates_new_conversation(self, tracker: ConversationTracker) -> None:
        conv_id, history = await tracker.get_or_create(MIND, PERSON, ChannelType.TELEGRAM)
        assert isinstance(conv_id, str)
        assert history == []

    async def test_resumes_active_conversation(self, tracker: ConversationTracker) -> None:
        conv1, _ = await tracker.get_or_create(MIND, PERSON, ChannelType.TELEGRAM)
        # Add a turn to keep it active
        await tracker.add_turn(conv1, "user", "Hello")

        conv2, history = await tracker.get_or_create(MIND, PERSON, ChannelType.TELEGRAM)
        assert conv1 == conv2
        assert len(history) == 1

    async def test_different_persons_different_conversations(
        self, tracker: ConversationTracker
    ) -> None:
        conv1, _ = await tracker.get_or_create(MIND, PersonId("alice"), ChannelType.TELEGRAM)
        conv2, _ = await tracker.get_or_create(MIND, PersonId("bob"), ChannelType.TELEGRAM)
        assert conv1 != conv2

    async def test_timeout_creates_new(self, pool: DatabasePool) -> None:
        """After timeout, new conversation is created."""
        tracker = ConversationTracker(pool, timeout_minutes=30)
        conv1, _ = await tracker.get_or_create(MIND, PERSON, ChannelType.TELEGRAM)

        # Simulate timeout by backdating last_message_at
        async with pool.transaction() as conn:
            old_time = (datetime.now(UTC) - timedelta(minutes=35)).isoformat()
            await conn.execute(
                "UPDATE conversations SET last_message_at = ? WHERE id = ?",
                (old_time, str(conv1)),
            )

        conv2, _ = await tracker.get_or_create(MIND, PERSON, ChannelType.TELEGRAM)
        assert conv1 != conv2


class TestAddTurn:
    """Turn persistence."""

    async def test_add_and_retrieve(self, tracker: ConversationTracker) -> None:
        conv_id, _ = await tracker.get_or_create(MIND, PERSON, ChannelType.TELEGRAM)
        await tracker.add_turn(conv_id, "user", "Hello")
        await tracker.add_turn(conv_id, "assistant", "Hi!")

        _, history = await tracker.get_or_create(MIND, PERSON, ChannelType.TELEGRAM)
        assert len(history) == 2  # noqa: PLR2004
        assert history[0]["role"] == "user"
        assert history[1]["role"] == "assistant"

    async def test_updates_last_message_at(
        self, tracker: ConversationTracker, pool: DatabasePool
    ) -> None:
        """v14 fix: add_turn updates conversations.last_message_at."""
        conv_id, _ = await tracker.get_or_create(MIND, PERSON, ChannelType.TELEGRAM)

        # Backdate
        async with pool.transaction() as conn:
            old_time = (datetime.now(UTC) - timedelta(minutes=25)).isoformat()
            await conn.execute(
                "UPDATE conversations SET last_message_at = ? WHERE id = ?",
                (old_time, str(conv_id)),
            )

        # add_turn should update last_message_at
        await tracker.add_turn(conv_id, "user", "Still here")

        # Should still resume (not timeout)
        conv2, _ = await tracker.get_or_create(MIND, PERSON, ChannelType.TELEGRAM)
        assert conv_id == conv2

    async def test_updates_message_count(
        self, tracker: ConversationTracker, pool: DatabasePool
    ) -> None:
        """v14 fix: add_turn increments message_count."""
        conv_id, _ = await tracker.get_or_create(MIND, PERSON, ChannelType.TELEGRAM)
        await tracker.add_turn(conv_id, "user", "A")
        await tracker.add_turn(conv_id, "assistant", "B")

        async with pool.read() as conn:
            cursor = await conn.execute(
                "SELECT message_count FROM conversations WHERE id = ?",
                (str(conv_id),),
            )
            row = await cursor.fetchone()

        assert row is not None
        assert row[0] == 2  # noqa: PLR2004


class TestHistory:
    """History loading."""

    async def test_chronological_order(self, tracker: ConversationTracker) -> None:
        conv_id, _ = await tracker.get_or_create(MIND, PERSON, ChannelType.TELEGRAM)
        for i in range(5):
            await tracker.add_turn(conv_id, "user", f"msg-{i}")

        _, history = await tracker.get_or_create(MIND, PERSON, ChannelType.TELEGRAM)
        contents = [h["content"] for h in history]
        assert contents == [f"msg-{i}" for i in range(5)]

    async def test_max_turns_limit(self, pool: DatabasePool) -> None:
        """v14 fix: only last N turns loaded."""
        tracker = ConversationTracker(pool, timeout_minutes=30, max_history_turns=3)
        conv_id, _ = await tracker.get_or_create(MIND, PERSON, ChannelType.TELEGRAM)
        for i in range(10):
            await tracker.add_turn(conv_id, "user", f"msg-{i}")

        _, history = await tracker.get_or_create(MIND, PERSON, ChannelType.TELEGRAM)
        assert len(history) == 3  # noqa: PLR2004
        # Should be the LAST 3 (7, 8, 9), not first 3
        contents = [h["content"] for h in history]
        assert contents == ["msg-7", "msg-8", "msg-9"]


class TestEndConversation:
    """Ending conversations."""

    async def test_end_creates_new_on_next(self, tracker: ConversationTracker) -> None:
        conv1, _ = await tracker.get_or_create(MIND, PERSON, ChannelType.TELEGRAM)
        await tracker.end_conversation(conv1)

        conv2, _ = await tracker.get_or_create(MIND, PERSON, ChannelType.TELEGRAM)
        assert conv1 != conv2


class TestSessionEdgeCases:
    """Cover edge cases in session timestamp parsing."""

    async def test_invalid_timestamp_creates_new(
        self,
        tracker: ConversationTracker,
    ) -> None:
        """When last_message_at is an invalid string, create new conversation."""
        # Create a conversation and corrupt its timestamp
        conv1, _ = await tracker.get_or_create(MIND, PERSON, ChannelType.TELEGRAM)

        async with tracker._pool.transaction() as conn:  # noqa: SLF001
            await conn.execute(
                "UPDATE conversations SET last_message_at = 'not-a-date' WHERE id = ?",
                (str(conv1),),
            )

        # Should detect invalid timestamp and create new
        conv2, _ = await tracker.get_or_create(MIND, PERSON, ChannelType.TELEGRAM)
        assert conv1 != conv2

    async def test_null_timestamp_creates_new(
        self,
        tracker: ConversationTracker,
    ) -> None:
        """When last_message_at is NULL, create new conversation."""
        conv1, _ = await tracker.get_or_create(MIND, PERSON, ChannelType.TELEGRAM)

        async with tracker._pool.transaction() as conn:  # noqa: SLF001
            await conn.execute(
                "UPDATE conversations SET last_message_at = 'garbage' WHERE id = ?",
                (str(conv1),),
            )

        conv2, _ = await tracker.get_or_create(MIND, PERSON, ChannelType.TELEGRAM)
        assert conv1 != conv2
