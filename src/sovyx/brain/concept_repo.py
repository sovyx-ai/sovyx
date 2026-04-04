"""Sovyx concept repository — neocortex CRUD + embedding + search.

All writes are atomic: concept + embedding in the same transaction.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sovyx.engine.errors import SearchError
from sovyx.engine.types import ConceptCategory, ConceptId, MindId
from sovyx.observability.logging import get_logger
from sovyx.persistence.datetime_utils import parse_db_datetime

if TYPE_CHECKING:
    from sovyx.brain.embedding import EmbeddingEngine
    from sovyx.brain.models import Concept
    from sovyx.persistence.pool import DatabasePool

logger = get_logger(__name__)


class ConceptRepository:
    """Repository for brain concepts — CRUD + embedding + search.

    All writes are atomic: concept + embedding stored in the same
    transaction when embeddings are available.
    """

    def __init__(self, pool: DatabasePool, embedding_engine: EmbeddingEngine) -> None:
        self._pool = pool
        self._embedding = embedding_engine

    async def create(self, concept: Concept) -> ConceptId:
        """Create a concept with optional embedding.

        Args:
            concept: The concept to persist.

        Returns:
            The concept ID.
        """
        embedding: list[float] | None = concept.embedding
        if embedding is None and self._embedding.has_embeddings:
            text = f"{concept.name} {concept.content}".strip()
            if text:
                embedding = await self._embedding.encode(text)

        async with self._pool.transaction() as conn:
            await conn.execute(
                """INSERT INTO concepts
                (id, mind_id, name, content, category, importance, confidence,
                 access_count, last_accessed, emotional_valence, source,
                 metadata, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(concept.id),
                    str(concept.mind_id),
                    concept.name,
                    concept.content,
                    concept.category.value,
                    concept.importance,
                    concept.confidence,
                    concept.access_count,
                    concept.last_accessed.isoformat() if concept.last_accessed else None,
                    concept.emotional_valence,
                    concept.source,
                    json.dumps(concept.metadata),
                    concept.created_at.isoformat(),
                    concept.updated_at.isoformat(),
                ),
            )

            if embedding and self._pool.has_sqlite_vec:
                await conn.execute(
                    "INSERT INTO concept_embeddings (concept_id, embedding) VALUES (?, ?)",
                    (str(concept.id), json.dumps(embedding)),
                )

        logger.debug("concept_created", concept_id=str(concept.id), name=concept.name)
        return concept.id

    async def get(self, concept_id: ConceptId) -> Concept | None:
        """Get a concept by ID.

        Returns:
            The concept, or None if not found.
        """

        async with self._pool.read() as conn:
            cursor = await conn.execute(
                "SELECT * FROM concepts WHERE id = ?",
                (str(concept_id),),
            )
            row = await cursor.fetchone()

        if row is None:
            return None

        return self._row_to_concept(row)

    async def get_by_mind(
        self, mind_id: MindId, limit: int = 100, offset: int = 0
    ) -> list[Concept]:
        """Get concepts for a mind with pagination."""
        async with self._pool.read() as conn:
            cursor = await conn.execute(
                "SELECT * FROM concepts WHERE mind_id = ? "
                "ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (str(mind_id), limit, offset),
            )
            rows = await cursor.fetchall()

        return [self._row_to_concept(r) for r in rows]

    async def get_recent(self, mind_id: MindId, limit: int = 50) -> list[Concept]:
        """Get most recently accessed concepts.

        Concepts without last_accessed sort last (NULLS LAST).
        """
        async with self._pool.read() as conn:
            cursor = await conn.execute(
                "SELECT * FROM concepts WHERE mind_id = ? "
                "ORDER BY "
                "CASE WHEN last_accessed IS NULL THEN 1 ELSE 0 END, "
                "last_accessed DESC, created_at DESC "
                "LIMIT ?",
                (str(mind_id), limit),
            )
            rows = await cursor.fetchall()

        return [self._row_to_concept(r) for r in rows]

    async def update(self, concept: Concept) -> None:
        """Update an existing concept."""
        async with self._pool.transaction() as conn:
            await conn.execute(
                """UPDATE concepts SET
                name=?, content=?, category=?, importance=?, confidence=?,
                access_count=?, last_accessed=?, emotional_valence=?,
                source=?, metadata=?, updated_at=?
                WHERE id=?""",
                (
                    concept.name,
                    concept.content,
                    concept.category.value,
                    concept.importance,
                    concept.confidence,
                    concept.access_count,
                    concept.last_accessed.isoformat() if concept.last_accessed else None,
                    concept.emotional_valence,
                    concept.source,
                    json.dumps(concept.metadata),
                    datetime.now(UTC).isoformat(),
                    str(concept.id),
                ),
            )

    async def delete(self, concept_id: ConceptId) -> None:
        """Delete a concept (relations CASCADE via FK)."""
        async with self._pool.transaction() as conn:
            if self._pool.has_sqlite_vec:
                await conn.execute(
                    "DELETE FROM concept_embeddings WHERE concept_id = ?",
                    (str(concept_id),),
                )
            await conn.execute(
                "DELETE FROM concepts WHERE id = ?",
                (str(concept_id),),
            )

    async def record_access(self, concept_id: ConceptId) -> None:
        """Increment access_count and update last_accessed."""
        now = datetime.now(UTC).isoformat()
        async with self._pool.write() as conn:
            await conn.execute(
                "UPDATE concepts SET access_count = access_count + 1, "
                "last_accessed = ? WHERE id = ?",
                (now, str(concept_id)),
            )
            await conn.commit()

    async def search_by_embedding(
        self,
        query_embedding: list[float],
        mind_id: MindId,
        limit: int = 10,
    ) -> list[tuple[Concept, float]]:
        """Search by vector similarity.

        Returns:
            List of (concept, distance) tuples ordered by distance.

        Raises:
            SearchError: If sqlite-vec is not available.
        """
        if not self._pool.has_sqlite_vec:
            msg = "Vector search unavailable — sqlite-vec not loaded"
            raise SearchError(msg)

        async with self._pool.read() as conn:
            cursor = await conn.execute(
                """SELECT c.*, ce.distance
                FROM concept_embeddings ce
                JOIN concepts c ON c.id = ce.concept_id
                WHERE c.mind_id = ?
                AND ce.embedding MATCH ?
                AND k = ?
                ORDER BY ce.distance""",
                (str(mind_id), json.dumps(query_embedding), limit),
            )
            rows = await cursor.fetchall()

        results: list[tuple[Concept, float]] = []
        for row in rows:
            concept = self._row_to_concept(row[:-1])
            distance = float(row[-1])
            results.append((concept, distance))
        return results

    async def search_by_text(
        self,
        query: str,
        mind_id: MindId,
        limit: int = 10,
    ) -> list[tuple[Concept, float]]:
        """Search by FTS5 full-text.

        Query is sanitized to prevent FTS5 operator injection.

        Returns:
            List of (concept, rank) tuples.
        """
        # Sanitize: wrap in double quotes to force literal phrase
        safe_query = '"' + query.replace('"', '""') + '"'

        async with self._pool.read() as conn:
            cursor = await conn.execute(
                """SELECT c.*, rank
                FROM concepts_fts fts
                JOIN concepts c ON c.rowid = fts.rowid
                WHERE concepts_fts MATCH ?
                AND c.mind_id = ?
                ORDER BY rank
                LIMIT ?""",
                (safe_query, str(mind_id), limit),
            )
            rows = await cursor.fetchall()

        results: list[tuple[Concept, float]] = []
        for row in rows:
            concept = self._row_to_concept(row[:-1])
            rank = float(row[-1])
            results.append((concept, rank))
        return results

    async def count(self, mind_id: MindId) -> int:
        """Count concepts for a mind."""
        async with self._pool.read() as conn:
            cursor = await conn.execute(
                "SELECT COUNT(*) FROM concepts WHERE mind_id = ?",
                (str(mind_id),),
            )
            row = await cursor.fetchone()
            return int(row[0]) if row else 0

    @staticmethod
    def _row_to_concept(row: object) -> Concept:
        """Convert a database row to a Concept model."""
        from sovyx.brain.models import Concept

        r: tuple[Any, ...] = tuple(row)  # type: ignore[arg-type]
        last_accessed = parse_db_datetime(r[8])

        return Concept(
            id=ConceptId(r[0]),
            mind_id=MindId(r[1]),
            name=r[2],
            content=r[3],
            category=ConceptCategory(r[4]),
            importance=float(r[5]),
            confidence=float(r[6]),
            access_count=int(r[7]),
            last_accessed=last_accessed,
            emotional_valence=float(r[9]),
            source=r[10],
            metadata=json.loads(r[11]) if isinstance(r[11], str) else r[11],
            created_at=parse_db_datetime(r[12]),
            updated_at=parse_db_datetime(r[13]),
        )
