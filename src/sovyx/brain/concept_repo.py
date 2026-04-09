"""Sovyx concept repository — neocortex CRUD + embedding + search.

All writes are atomic: concept + embedding in the same transaction.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sovyx.engine.errors import SearchError
from sovyx.engine.types import ConceptCategory, ConceptId, MindId
from sovyx.observability.logging import get_logger
from sovyx.persistence.datetime_utils import parse_db_datetime


def _levenshtein(s: str, t: str) -> int:
    """Compute Levenshtein edit distance between two strings.

    Simple DP implementation — only used on short concept names.
    """
    if len(s) < len(t):
        return _levenshtein(t, s)
    if not t:
        return len(s)

    prev = list(range(len(t) + 1))
    for i, sc in enumerate(s):
        curr = [i + 1]
        for j, tc in enumerate(t):
            cost = 0 if sc == tc else 1
            curr.append(min(curr[j] + 1, prev[j + 1] + 1, prev[j] + cost))
        prev = curr
    return prev[-1]


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

    async def boost_importance(self, concept_id: ConceptId, delta: float) -> None:
        """Boost a concept's importance by delta, capped at 1.0.

        Args:
            concept_id: The concept to boost.
            delta: Amount to add (positive). Clamped to [0.0, 1.0].
        """
        async with self._pool.write() as conn:
            await conn.execute(
                "UPDATE concepts SET importance = MIN(1.0, importance + ?) WHERE id = ?",
                (max(0.0, delta), str(concept_id)),
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

    async def find_merge_candidates(self, mind_id: MindId) -> list[tuple[Concept, Concept]]:
        """Find pairs of concepts that are merge candidates.

        Criteria: same mind, same category, and one name is a substring
        of the other (e.g. "PostgreSQL" and "PostgreSQL Preference").

        Returns:
            List of (survivor, to_merge) tuples. Survivor has higher
            importance. Limited to 10 pairs per cycle to avoid overload.
        """
        concepts = await self.get_by_mind(mind_id)
        pairs: list[tuple[Concept, Concept]] = []

        # Group by category for efficient comparison
        by_cat: dict[str, list[Concept]] = {}
        for c in concepts:
            by_cat.setdefault(c.category.value, []).append(c)

        for cat_concepts in by_cat.values():
            for i, a in enumerate(cat_concepts):
                for b in cat_concepts[i + 1 :]:
                    if self._is_merge_candidate(a, b):
                        # Survivor = higher importance
                        if a.importance >= b.importance:
                            pairs.append((a, b))
                        else:
                            pairs.append((b, a))
                        if len(pairs) >= 10:  # noqa: PLR2004
                            return pairs

        return pairs

    @staticmethod
    def _is_merge_candidate(a: Concept, b: Concept) -> bool:
        """Check if two concepts should be merged.

        Criteria (must match ALL):
        - Same category (enforced by caller grouping)
        - One name contains the other OR Levenshtein distance ≤ 3

        Returns:
            True if the pair should be merged.
        """
        na = a.name.lower().strip()
        nb = b.name.lower().strip()

        # Exact match (shouldn't happen with dedup, but defensive)
        if na == nb:
            return True

        # Name containment: "PostgreSQL" in "PostgreSQL Preference"
        if na in nb or nb in na:
            return True

        # Simple Levenshtein ≤ 3
        if len(na) > 2 and len(nb) > 2:  # noqa: PLR2004
            dist = _levenshtein(na, nb)
            if dist <= 3:  # noqa: PLR2004
                return True

        return False

    async def batch_update_scores(
        self,
        updates: list[tuple[ConceptId, float, float]],
    ) -> int:
        """Batch update importance + confidence for multiple concepts.

        Used by consolidation to apply recalculated scores efficiently
        in a single transaction.

        Args:
            updates: List of (concept_id, new_importance, new_confidence).

        Returns:
            Number of concepts updated.
        """
        if not updates:
            return 0
        async with self._pool.write() as conn:
            await conn.executemany(
                """UPDATE concepts
                SET importance = ?, confidence = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?""",
                [(imp, conf, str(cid)) for cid, imp, conf in updates],
            )
            await conn.commit()
        return len(updates)

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

        r = tuple(row)  # type: ignore[arg-type,var-annotated]  # aiosqlite.Row → tuple
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
