"""Dashboard activity timeline — unified view of cognitive processing history.

Queries across brain, conversation, and system databases to build a
chronological timeline of what the engine has processed.  Always returns
data if conversations have occurred, regardless of WebSocket connection.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from sovyx.engine.registry import ServiceRegistry
    from sovyx.persistence.pool import DatabasePool

logger = get_logger(__name__)

# ── Helpers ──────────────────────────────────────────────────────────────


async def _get_brain_pool(  # pragma: no cover — registry wiring
    registry: ServiceRegistry,
) -> DatabasePool | None:
    """Resolve the brain database pool for the active mind."""
    from sovyx.persistence.manager import DatabaseManager

    if not registry.is_registered(DatabaseManager):
        return None
    try:
        from sovyx.dashboard._shared import get_active_mind_id
        from sovyx.engine.types import MindId

        db = await registry.resolve(DatabaseManager)
        mind_id = await get_active_mind_id(registry)
        return db.get_brain_pool(MindId(mind_id))
    except Exception:  # noqa: BLE001
        logger.debug("activity_brain_pool_unavailable")
        return None


async def _get_conversation_pool(  # pragma: no cover — registry wiring
    registry: ServiceRegistry,
) -> DatabasePool | None:
    """Resolve the conversation database pool for the active mind."""
    from sovyx.persistence.manager import DatabaseManager

    if not registry.is_registered(DatabaseManager):
        return None
    try:
        from sovyx.dashboard._shared import get_active_mind_id
        from sovyx.engine.types import MindId

        db = await registry.resolve(DatabaseManager)
        mind_id = await get_active_mind_id(registry)
        return db.get_conversation_pool(MindId(mind_id))
    except Exception:  # noqa: BLE001
        logger.debug("activity_conversation_pool_unavailable")
        return None


def _iso(ts: str | None) -> str:
    """Normalize a timestamp string to ISO-8601 with Z suffix."""
    if ts is None:
        return datetime.now(tz=UTC).isoformat()
    # SQLite stores as "YYYY-MM-DD HH:MM:SS" or ISO — normalize
    s = str(ts).replace(" ", "T")
    if not s.endswith("Z") and "+" not in s:
        s += "Z"
    return s


# ── Query functions ──────────────────────────────────────────────────────


async def _query_conversations(
    pool: DatabasePool,
    cutoff: str,
    limit: int,
) -> list[dict[str, Any]]:
    """Fetch recent conversations with message counts and time range."""
    async with pool.read() as conn:
        cursor = await conn.execute(
            """SELECT id, channel, message_count, started_at, last_message_at, status
               FROM conversations
               WHERE last_message_at >= ?
               ORDER BY last_message_at DESC
               LIMIT ?""",
            (cutoff, limit),
        )
        rows = await cursor.fetchall()

    entries: list[dict[str, Any]] = []
    for row in rows:
        entries.append(
            {
                "type": "conversation",
                "timestamp": _iso(row[4]),  # last_message_at
                "data": {
                    "conversation_id": row[0],
                    "channel": row[1],
                    "message_count": row[2],
                    "started_at": _iso(row[3]),
                    "status": row[5],
                },
            }
        )
    return entries


async def _query_messages(
    pool: DatabasePool,
    cutoff: str,
    limit: int,
) -> list[dict[str, Any]]:
    """Fetch recent conversation turns with preview text."""
    async with pool.read() as conn:
        cursor = await conn.execute(
            """SELECT t.id, t.conversation_id, t.role, t.content,
                      t.tokens, t.metadata, t.created_at
               FROM conversation_turns t
               WHERE t.created_at >= ?
               ORDER BY t.created_at DESC
               LIMIT ?""",
            (cutoff, limit),
        )
        rows = await cursor.fetchall()

    entries: list[dict[str, Any]] = []
    for row in rows:
        content = str(row[3])
        preview = content[:120] + "..." if len(content) > 120 else content
        metadata = _parse_metadata(row[5])

        entries.append(
            {
                "type": "message",
                "timestamp": _iso(row[6]),
                "data": {
                    "turn_id": row[0],
                    "conversation_id": row[1],
                    "role": row[2],
                    "preview": preview,
                    "tokens": row[4] or 0,
                    "model": metadata.get("model", ""),
                    "cost_usd": metadata.get("cost_usd", 0.0),
                },
            }
        )
    return entries


async def _query_concepts(
    pool: DatabasePool,
    cutoff: str,
    limit: int,
) -> list[dict[str, Any]]:
    """Fetch recently created concepts, grouped by creation minute."""
    async with pool.read() as conn:
        cursor = await conn.execute(
            """SELECT id, name, category, importance, created_at
               FROM concepts
               WHERE created_at >= ?
               ORDER BY created_at DESC
               LIMIT ?""",
            (cutoff, limit),
        )
        rows = await cursor.fetchall()

    if not rows:
        return []

    # Group concepts by minute for compact display
    groups: dict[str, list[dict[str, str]]] = {}
    group_timestamps: dict[str, str] = {}

    for row in rows:
        ts = _iso(row[4])
        # Group by minute: "2026-04-08T14:01"
        minute_key = ts[:16]
        if minute_key not in groups:
            groups[minute_key] = []
            group_timestamps[minute_key] = ts
        groups[minute_key].append(
            {
                "id": row[0],
                "name": row[1],
                "category": row[2],
            }
        )

    entries: list[dict[str, Any]] = []
    for minute_key, concepts in groups.items():
        entries.append(
            {
                "type": "concepts_learned",
                "timestamp": group_timestamps[minute_key],
                "data": {
                    "names": [c["name"] for c in concepts],
                    "concepts": [{"name": c["name"], "category": c["category"]} for c in concepts],
                    "count": len(concepts),
                },
            }
        )
    return entries


async def _query_episodes(
    pool: DatabasePool,
    cutoff: str,
    limit: int,
) -> list[dict[str, Any]]:
    """Fetch recently encoded episodes."""
    async with pool.read() as conn:
        cursor = await conn.execute(
            """SELECT id, conversation_id, importance, created_at
               FROM episodes
               WHERE created_at >= ?
               ORDER BY created_at DESC
               LIMIT ?""",
            (cutoff, limit),
        )
        rows = await cursor.fetchall()

    entries: list[dict[str, Any]] = []
    for row in rows:
        entries.append(
            {
                "type": "episode_encoded",
                "timestamp": _iso(row[3]),
                "data": {
                    "episode_id": row[0],
                    "conversation_id": row[1],
                    "importance": round(float(row[2]), 3) if row[2] else 0.0,
                },
            }
        )
    return entries


async def _query_consolidations(
    pool: DatabasePool,
    cutoff: str,
    limit: int,
) -> list[dict[str, Any]]:
    """Fetch recent consolidation runs."""
    try:
        async with pool.read() as conn:
            cursor = await conn.execute(
                """SELECT id, merged, pruned, strengthened, created_at
                   FROM consolidation_log
                   WHERE created_at >= ?
                   ORDER BY created_at DESC
                   LIMIT ?""",
                (cutoff, limit),
            )
            rows = await cursor.fetchall()
    except Exception:  # noqa: BLE001
        # consolidation_log may not exist in older schemas
        return []

    entries: list[dict[str, Any]] = []
    for row in rows:
        entries.append(
            {
                "type": "consolidation",
                "timestamp": _iso(row[4]),
                "data": {
                    "merged": row[1] or 0,
                    "pruned": row[2] or 0,
                    "strengthened": row[3] or 0,
                },
            }
        )
    return entries


def _parse_metadata(raw: str | None) -> dict[str, Any]:
    """Safely parse JSON metadata from a turn row."""
    if not raw:
        return {}
    try:
        import json

        return dict(json.loads(raw))
    except (ValueError, TypeError):
        return {}


# ── Public API ───────────────────────────────────────────────────────────


async def get_activity_timeline(
    registry: ServiceRegistry,
    *,
    hours: int = 24,
    limit: int = 100,
) -> dict[str, Any]:
    """Build a unified activity timeline from all data sources.

    Queries conversations, turns, concepts, episodes, and consolidation
    logs from the past *hours* and merges them into a single sorted list.

    Args:
        registry: Service registry for resolving DB pools.
        hours: Lookback window in hours (default 24).
        limit: Maximum entries to return (default 100).

    Returns:
        Dictionary with ``entries`` (sorted timeline) and ``meta``
        (query metadata).
    """
    cutoff_dt = datetime.now(tz=UTC) - timedelta(hours=hours)
    cutoff = cutoff_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    all_entries: list[dict[str, Any]] = []

    # Query conversations + messages
    conv_pool = await _get_conversation_pool(registry)
    if conv_pool is not None:
        try:
            convos = await _query_conversations(conv_pool, cutoff, limit)
            messages = await _query_messages(conv_pool, cutoff, limit * 3)
            all_entries.extend(convos)
            all_entries.extend(messages)
        except Exception:  # noqa: BLE001
            logger.debug("activity_conversation_query_failed", exc_info=True)

    # Query brain data
    brain_pool = await _get_brain_pool(registry)
    if brain_pool is not None:
        try:
            concepts = await _query_concepts(brain_pool, cutoff, limit)
            episodes = await _query_episodes(brain_pool, cutoff, limit)
            consolidations = await _query_consolidations(brain_pool, cutoff, limit)
            all_entries.extend(concepts)
            all_entries.extend(episodes)
            all_entries.extend(consolidations)
        except Exception:  # noqa: BLE001
            logger.debug("activity_brain_query_failed", exc_info=True)

    # Sort by timestamp descending, take top `limit`
    all_entries.sort(key=lambda e: e["timestamp"], reverse=True)
    trimmed = all_entries[:limit]

    return {
        "entries": trimmed,
        "meta": {
            "hours": hours,
            "limit": limit,
            "total_before_limit": len(all_entries),
            "cutoff": cutoff,
        },
    }
