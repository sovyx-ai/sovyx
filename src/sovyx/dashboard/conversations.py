"""Dashboard conversation queries — read-only access to conversation data.

Queries the SQLite database directly to avoid coupling with ConversationTracker.
All methods are read-only and safe to call from the dashboard API.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from sovyx.engine.registry import ServiceRegistry
    from sovyx.persistence.pool import DatabasePool

logger = get_logger(__name__)


async def _get_conversation_pool(registry: ServiceRegistry) -> DatabasePool | None:
    """Get the conversation database pool from the registry.

    Returns the pool or None if unavailable.
    """
    from sovyx.persistence.manager import DatabaseManager

    if not registry.is_registered(DatabaseManager):
        return None

    db = await registry.resolve(DatabaseManager)
    # Conversations are per-mind; use first available mind
    try:
        from sovyx.dashboard._shared import get_active_mind_id
        from sovyx.engine.types import MindId

        mind_id = await get_active_mind_id(registry)
        return db.get_conversation_pool(MindId(mind_id))
    except Exception:  # noqa: BLE001
        logger.debug("_get_conversation_pool_failed")

    # Fallback: try system pool for legacy single-db setups
    try:
        return db.get_system_pool()
    except Exception:  # noqa: BLE001
        return None


async def list_conversations(
    registry: ServiceRegistry,
    *,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """List conversations ordered by most recent activity."""
    try:
        pool = await _get_conversation_pool(registry)
        if pool is None:
            return []

        async with pool.read() as conn:
            cursor = await conn.execute(
                """SELECT id, person_id, channel, message_count,
                          last_message_at, status
                   FROM conversations
                   ORDER BY last_message_at DESC
                   LIMIT ? OFFSET ?""",
                (limit, offset),
            )
            rows = await cursor.fetchall()

        return [
            {
                "id": row[0],
                "participant": row[1],
                "channel": row[2],
                "message_count": row[3],
                "last_message_at": row[4],
                "status": row[5] if len(row) > 5 else "active",
            }
            for row in rows
        ]
    except Exception:  # noqa: BLE001
        logger.debug("list_conversations_failed")
        return []


async def get_conversation_messages(
    registry: ServiceRegistry,
    conversation_id: str,
    *,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Get messages for a specific conversation."""
    try:
        pool = await _get_conversation_pool(registry)
        if pool is None:
            return []

        async with pool.read() as conn:
            cursor = await conn.execute(
                """SELECT id, role, content, created_at
                   FROM conversation_turns
                   WHERE conversation_id = ?
                   ORDER BY created_at ASC
                   LIMIT ?""",
                (conversation_id, limit),
            )
            rows = await cursor.fetchall()

        return [
            {
                "id": row[0],
                "role": row[1],
                "content": row[2],
                "timestamp": row[3],
            }
            for row in rows
        ]
    except Exception:  # noqa: BLE001
        logger.debug("get_conversation_messages_failed", conversation_id=conversation_id)
        return []


async def count_active_conversations(registry: ServiceRegistry) -> int:
    """Count currently active conversations."""
    try:
        pool = await _get_conversation_pool(registry)
        if pool is None:
            return 0

        async with pool.read() as conn:
            cursor = await conn.execute(
                "SELECT COUNT(*) FROM conversations WHERE status = 'active'",
            )
            row = await cursor.fetchone()
            return int(row[0]) if row else 0
    except Exception:  # noqa: BLE001
        logger.debug("count_active_conversations_failed")
        return 0
