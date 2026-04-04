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

        # Atomic create: person + mapping in one transaction.
        # Re-check inside write lock to prevent race (two concurrent
        # resolve() calls for the same new user).
        person_id = PersonId(str(uuid.uuid4()))
        mapping_id = str(uuid.uuid4())
        name = display_name or channel_user_id

        async with self._pool.transaction() as conn:
            # Double-check inside write lock (eliminates race window)
            cursor = await conn.execute(
                """SELECT p.id FROM persons p
                   JOIN channel_mappings cm ON cm.person_id = p.id
                   WHERE cm.channel_type = ? AND cm.channel_user_id = ?""",
                (channel_type.value, channel_user_id),
            )
            existing = await cursor.fetchone()
            if existing is not None:
                return PersonId(str(existing[0]))

            await conn.execute(
                "INSERT INTO persons (id, name, display_name) VALUES (?, ?, ?)",
                (person_id, name, display_name or None),
            )
            await conn.execute(
                """INSERT INTO channel_mappings
                   (id, person_id, channel_type, channel_user_id)
                   VALUES (?, ?, ?, ?)""",
                (mapping_id, person_id, channel_type.value, channel_user_id),
            )

        logger.info(
            "person_created",
            person_id=person_id,
            channel=channel_type.value,
        )
        return person_id

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
