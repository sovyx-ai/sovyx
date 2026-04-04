"""Sovyx PersonResolver — resolve channel users to cross-channel PersonId."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sovyx.engine.types import PersonId
from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from sovyx.engine.types import ChannelType
    from sovyx.persistence.pool import DatabasePool

logger = get_logger(__name__)


class PersonResolver:
    """Resolve channel user → Person.

    Uses system.db persons + channel_mappings tables.
    Auto-creates Person on first contact.
    """

    def __init__(self, system_pool: DatabasePool) -> None:
        self._pool = system_pool

    async def resolve(
        self,
        channel_type: ChannelType,
        channel_user_id: str,
        display_name: str = "",
    ) -> PersonId:
        """Resolve channel user to PersonId. Auto-create if new.

        Args:
            channel_type: Source channel (telegram, cli, etc.).
            channel_user_id: Platform-specific user ID.
            display_name: Human-readable name.

        Returns:
            PersonId (existing or newly created).
        """
        # Lookup existing mapping
        async with self._pool.read() as conn:
            cursor = await conn.execute(
                """SELECT p.id FROM persons p
                   JOIN channel_mappings cm ON cm.person_id = p.id
                   WHERE cm.channel_type = ? AND cm.channel_user_id = ?""",
                (channel_type.value, channel_user_id),
            )
            row = await cursor.fetchone()

        if row is not None:
            return PersonId(str(row[0]))

        # Create new person + mapping (idempotent — race-safe).
        # Two concurrent calls for the same user may both reach this point.
        # INSERT OR IGNORE ensures only the first succeeds; the re-fetch
        # below returns the winner's person_id to both callers.
        person_id = PersonId(str(uuid.uuid4()))
        mapping_id = str(uuid.uuid4())
        name = display_name or channel_user_id

        async with self._pool.transaction() as conn:
            await conn.execute(
                "INSERT INTO persons (id, name, display_name) VALUES (?, ?, ?)",
                (person_id, name, display_name or None),
            )
            await conn.execute(
                """INSERT OR IGNORE INTO channel_mappings
                   (id, person_id, channel_type, channel_user_id)
                   VALUES (?, ?, ?, ?)""",
                (mapping_id, person_id, channel_type.value, channel_user_id),
            )

        # Re-fetch to get the actual winner's person_id (may differ
        # from ours if a concurrent call inserted first).
        async with self._pool.read() as conn:
            cursor = await conn.execute(
                """SELECT p.id FROM persons p
                   JOIN channel_mappings cm ON cm.person_id = p.id
                   WHERE cm.channel_type = ? AND cm.channel_user_id = ?""",
                (channel_type.value, channel_user_id),
            )
            row = await cursor.fetchone()

        resolved_id = PersonId(str(row[0])) if row else person_id
        logger.info(
            "person_created",
            person_id=resolved_id,
            channel=channel_type.value,
        )
        return resolved_id

    async def get_person(self, person_id: PersonId) -> dict[str, object] | None:
        """Get person details by ID."""
        async with self._pool.read() as conn:
            cursor = await conn.execute(
                "SELECT id, name, display_name, metadata FROM persons WHERE id = ?",
                (person_id,),
            )
            row = await cursor.fetchone()

        if row is None:
            return None

        return {
            "id": row[0],
            "name": row[1],
            "display_name": row[2],
            "metadata": row[3],
        }
