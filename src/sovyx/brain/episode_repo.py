"""Sovyx episode repository — hippocampus CRUD + embedding + search.

All writes are atomic: episode + embedding in the same transaction.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from sovyx.engine.errors import SearchError
from sovyx.engine.types import ConceptId, ConversationId, EpisodeId, MindId
from sovyx.observability.logging import get_logger
from sovyx.persistence.datetime_utils import parse_db_datetime

if TYPE_CHECKING:
    from datetime import datetime

    from sovyx.brain.embedding import EmbeddingEngine
    from sovyx.brain.models import Episode
    from sovyx.persistence.pool import DatabasePool

logger = get_logger(__name__)


class EpisodeRepository:
    """Repository for brain episodes — CRUD + embedding + search.

    Episodes are conversation exchanges stored in the hippocampus.
    Embeddings are generated from user_input + assistant_response.
    """

    def __init__(self, pool: DatabasePool, embedding_engine: EmbeddingEngine) -> None:
        self._pool = pool
        self._embedding = embedding_engine

    async def create(self, episode: Episode) -> EpisodeId:
        """Create an episode with optional embedding.

        Embedding is generated from "{user_input} {assistant_response}".

        Args:
            episode: The episode to persist.

        Returns:
            The episode ID.
        """
        embedding: list[float] | None = episode.embedding
        if embedding is None and self._embedding.has_embeddings:
            text = f"{episode.user_input} {episode.assistant_response}".strip()
            if text:
                embedding = await self._embedding.encode(text)

        async with self._pool.transaction() as conn:
            await conn.execute(
                """INSERT INTO episodes
                (id, mind_id, conversation_id, user_input, assistant_response,
                 summary, importance,
                 emotional_valence, emotional_arousal, emotional_dominance,
                 concepts_mentioned, metadata, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(episode.id),
                    str(episode.mind_id),
                    str(episode.conversation_id),
                    episode.user_input,
                    episode.assistant_response,
                    episode.summary,
                    episode.importance,
                    episode.emotional_valence,
                    episode.emotional_arousal,
                    episode.emotional_dominance,
                    json.dumps([str(c) for c in episode.concepts_mentioned]),
                    json.dumps(episode.metadata),
                    episode.created_at.isoformat(),
                ),
            )

            if embedding and self._pool.has_sqlite_vec:
                await conn.execute(
                    "INSERT INTO episode_embeddings (episode_id, embedding) VALUES (?, ?)",
                    (str(episode.id), json.dumps(embedding)),
                )

        logger.debug("episode_created", episode_id=str(episode.id))
        return episode.id

    async def get(self, episode_id: EpisodeId) -> Episode | None:
        """Get an episode by ID.

        Returns:
            The episode, or None if not found.
        """
        async with self._pool.read() as conn:
            cursor = await conn.execute(
                "SELECT * FROM episodes WHERE id = ?",
                (str(episode_id),),
            )
            row = await cursor.fetchone()

        if row is None:
            return None

        return self._row_to_episode(row)

    async def get_by_conversation(
        self, conversation_id: ConversationId, limit: int = 50
    ) -> list[Episode]:
        """Get episodes for a conversation in chronological order."""
        async with self._pool.read() as conn:
            cursor = await conn.execute(
                "SELECT * FROM episodes WHERE conversation_id = ? ORDER BY created_at ASC LIMIT ?",
                (str(conversation_id), limit),
            )
            rows = await cursor.fetchall()

        return [self._row_to_episode(r) for r in rows]

    async def get_recent(self, mind_id: MindId, limit: int = 20) -> list[Episode]:
        """Get most recent episodes ordered by created_at DESC."""
        async with self._pool.read() as conn:
            cursor = await conn.execute(
                "SELECT * FROM episodes WHERE mind_id = ? ORDER BY created_at DESC LIMIT ?",
                (str(mind_id), limit),
            )
            rows = await cursor.fetchall()

        return [self._row_to_episode(r) for r in rows]

    async def get_since(
        self,
        mind_id: MindId,
        since: datetime,
        limit: int = 500,
    ) -> list[Episode]:
        """Get episodes created at or after ``since`` (chronological order).

        Used by the DREAM phase to fetch a lookback window of recent
        conversations for pattern extraction. Chronological order
        (oldest first) gives the LLM a natural temporal narrative.

        Args:
            mind_id: Scope to a single mind.
            since: Lower bound (inclusive). Timezone-aware datetimes
                are serialized in ISO format for the SQL comparison.
            limit: Hard cap on rows returned. Default 500 is generous
                for a 24-hour window even in heavy-use minds.

        Returns:
            Episodes created at or after ``since``, oldest first.
        """
        async with self._pool.read() as conn:
            cursor = await conn.execute(
                """SELECT * FROM episodes
                WHERE mind_id = ? AND created_at >= ?
                ORDER BY created_at ASC
                LIMIT ?""",
                (str(mind_id), since.isoformat(), limit),
            )
            rows = await cursor.fetchall()

        return [self._row_to_episode(r) for r in rows]

    async def search_by_embedding(
        self,
        query_embedding: list[float],
        mind_id: MindId,
        limit: int = 5,
    ) -> list[tuple[Episode, float]]:
        """Search by vector similarity.

        Returns:
            List of (episode, distance) tuples ordered by distance.

        Raises:
            SearchError: If sqlite-vec is not available.
        """
        if not self._pool.has_sqlite_vec:
            msg = "Vector search unavailable — sqlite-vec not loaded"
            raise SearchError(msg)

        async with self._pool.read() as conn:
            cursor = await conn.execute(
                """SELECT e.*, ee.distance
                FROM episode_embeddings ee
                JOIN episodes e ON e.id = ee.episode_id
                WHERE e.mind_id = ?
                AND ee.embedding MATCH ?
                AND k = ?
                ORDER BY ee.distance""",
                (str(mind_id), json.dumps(query_embedding), limit),
            )
            rows = await cursor.fetchall()

        results: list[tuple[Episode, float]] = []
        for row in rows:
            episode = self._row_to_episode(row[:-1])
            distance = float(row[-1])
            results.append((episode, distance))
        return results

    async def delete(self, episode_id: EpisodeId) -> None:
        """Delete an episode and its embedding."""
        async with self._pool.transaction() as conn:
            if self._pool.has_sqlite_vec:
                await conn.execute(
                    "DELETE FROM episode_embeddings WHERE episode_id = ?",
                    (str(episode_id),),
                )
            await conn.execute(
                "DELETE FROM episodes WHERE id = ?",
                (str(episode_id),),
            )

    async def count(self, mind_id: MindId) -> int:
        """Count episodes for a mind."""
        async with self._pool.read() as conn:
            cursor = await conn.execute(
                "SELECT COUNT(*) FROM episodes WHERE mind_id = ?",
                (str(mind_id),),
            )
            row = await cursor.fetchone()
            return int(row[0]) if row else 0

    @staticmethod
    def _row_to_episode(row: object) -> Episode:
        """Convert a database row to an Episode model.

        Column layout after migration 006 (ALTER TABLE appends at end):
        0 id, 1 mind_id, 2 conversation_id, 3 user_input,
        4 assistant_response, 5 summary, 6 importance,
        7 emotional_valence, 8 emotional_arousal,
        9 concepts_mentioned, 10 metadata, 11 created_at,
        12 emotional_dominance.
        """
        from sovyx.brain.models import Episode  # noqa: PLC0415

        r = tuple(row)  # type: ignore[arg-type,var-annotated]  # aiosqlite.Row → tuple

        # Defensive fallback for pre-migration rows (same rationale as
        # ``_row_to_concept``).
        dominance = float(r[12]) if len(r) > 12 else 0.0  # noqa: PLR2004

        return Episode(
            id=EpisodeId(r[0]),
            mind_id=MindId(r[1]),
            conversation_id=ConversationId(r[2]),
            user_input=r[3],
            assistant_response=r[4],
            summary=r[5],
            importance=float(r[6]),
            emotional_valence=float(r[7]),
            emotional_arousal=float(r[8]),
            emotional_dominance=dominance,
            concepts_mentioned=[ConceptId(c) for c in json.loads(r[9])]
            if isinstance(r[9], str)
            else r[9],
            metadata=json.loads(r[10]) if isinstance(r[10], str) else r[10],
            created_at=parse_db_datetime(r[11]),
        )
