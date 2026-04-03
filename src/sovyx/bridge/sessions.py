"""Sovyx ConversationTracker — manage active conversations with timeout."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sovyx.engine.types import ConversationId
from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from sovyx.engine.types import ChannelType, MindId, PersonId
    from sovyx.persistence.pool import DatabasePool

logger = get_logger(__name__)


class ConversationTracker:
    """Track active conversations per person.

    - New message: create or resume conversation (30min timeout).
    - Maintains recent history.
    - Persists in conversations.db.
    """

    def __init__(
        self,
        conversation_pool: DatabasePool,
        timeout_minutes: int = 30,
        max_history_turns: int = 50,
    ) -> None:
        self._pool = conversation_pool
        self._timeout = timedelta(minutes=timeout_minutes)
        self._max_turns = max_history_turns

    async def get_or_create(
        self,
        mind_id: MindId,
        person_id: PersonId,
        channel_type: ChannelType,
    ) -> tuple[ConversationId, list[dict[str, str]]]:
        """Get active conversation or create new one.

        Returns:
            (conversation_id, recent_history) where history is
            list of {"role": ..., "content": ...} dicts in chronological order.
        """
        now = datetime.now(UTC)
        cutoff = now - self._timeout

        # 1. Find active conversation
        async with self._pool.read() as conn:
            cursor = await conn.execute(
                """SELECT id, last_message_at FROM conversations
                   WHERE mind_id = ? AND person_id = ? AND status = 'active'
                   ORDER BY last_message_at DESC LIMIT 1""",
                (str(mind_id), str(person_id)),
            )
            row = await cursor.fetchone()

        if row is not None:
            conv_id = ConversationId(str(row[0]))
            last_msg = row[1]

            # Parse last_message_at
            if isinstance(last_msg, str):
                try:
                    last_at = datetime.fromisoformat(last_msg).replace(tzinfo=UTC)
                except ValueError:
                    last_at = cutoff  # Force new conversation
            else:
                last_at = cutoff

            if last_at >= cutoff:
                # Resume existing conversation
                history = await self._load_history(conv_id)
                return conv_id, history

            # Timeout expired — end old, create new
            await self._end_conversation(conv_id)

        # 3. Create new conversation
        conv_id = ConversationId(str(uuid.uuid4()))
        async with self._pool.transaction() as conn:
            await conn.execute(
                """INSERT INTO conversations
                   (id, mind_id, person_id, channel, started_at, last_message_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    str(conv_id),
                    str(mind_id),
                    str(person_id),
                    channel_type.value,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )

        logger.debug(
            "conversation_created",
            conversation_id=str(conv_id),
            mind_id=str(mind_id),
        )
        return conv_id, []

    async def add_turn(
        self,
        conversation_id: ConversationId,
        role: str,
        content: str,
    ) -> None:
        """Persist turn AND update conversation metadata.

        v14 fix: INSERT turn + UPDATE conversation in atomic transaction.
        Without UPDATE, last_message_at freezes and conversations die after timeout.
        """
        turn_id = str(uuid.uuid4())
        async with self._pool.transaction() as conn:
            await conn.execute(
                """INSERT INTO conversation_turns
                   (id, conversation_id, role, content)
                   VALUES (?, ?, ?, ?)""",
                (turn_id, str(conversation_id), role, content),
            )
            await conn.execute(
                """UPDATE conversations
                   SET last_message_at = CURRENT_TIMESTAMP,
                       message_count = message_count + 1
                   WHERE id = ?""",
                (str(conversation_id),),
            )

    async def end_conversation(self, conversation_id: ConversationId) -> None:
        """End a conversation (public API)."""
        await self._end_conversation(conversation_id)

    async def _end_conversation(self, conversation_id: ConversationId) -> None:
        """Mark conversation as ended."""
        async with self._pool.transaction() as conn:
            await conn.execute(
                "UPDATE conversations SET status = 'ended' WHERE id = ?",
                (str(conversation_id),),
            )
        logger.debug(
            "conversation_ended",
            conversation_id=str(conversation_id),
        )

    async def _load_history(self, conversation_id: ConversationId) -> list[dict[str, str]]:
        """Load last N turns in chronological order.

        v14 fix: subquery gets N most recent (DESC),
        outer query re-orders chronologically (ASC).
        """
        # B608: self._max_turns is an internal int, not user input
        query = (  # noqa: S608
            "SELECT role, content FROM ("
            "  SELECT role, content, rowid FROM conversation_turns"
            "  WHERE conversation_id = ?"
            "  ORDER BY rowid DESC"
            f"  LIMIT {self._max_turns}"
            ") sub ORDER BY rowid ASC"
        )
        async with self._pool.read() as conn:
            cursor = await conn.execute(
                query,
                (str(conversation_id),),
            )
            rows = await cursor.fetchall()

        return [{"role": row[0], "content": row[1]} for row in rows]
