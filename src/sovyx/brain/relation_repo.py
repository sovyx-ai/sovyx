"""Sovyx relation repository — synapse management for concept graph.

CRUD for relations between concepts with graph traversal queries.

Canonical ordering: all relations are stored with ``min(source, target)``
as ``source_id`` to eliminate bidirectional duplicates.  ``A→B`` and
``B→A`` both resolve to the same canonical row.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sovyx.engine.types import ConceptId, MindId, RelationId, RelationType
from sovyx.observability.logging import get_logger
from sovyx.persistence.datetime_utils import parse_db_datetime

if TYPE_CHECKING:
    from sovyx.brain.models import Relation
    from sovyx.persistence.pool import DatabasePool

logger = get_logger(__name__)


def _canonical_order(a: ConceptId, b: ConceptId) -> tuple[ConceptId, ConceptId]:
    """Return (source, target) in canonical order: min first.

    Ensures that the pair (A, B) and (B, A) always map to the same
    database row, eliminating bidirectional duplicates.

    Uses string comparison on the ID values — stable and deterministic
    regardless of ID format (UUIDs, ULIDs, etc.).
    """
    if str(a) <= str(b):
        return a, b
    return b, a


class RelationRepository:
    """Repository for brain relations (synapses between concepts).

    Manages the concept graph with Hebbian-style weight updates
    and co-occurrence tracking.
    """

    def __init__(self, pool: DatabasePool) -> None:
        self._pool = pool

    async def create(self, relation: Relation) -> RelationId:
        """Create a relation between two concepts.

        Source and target are canonicalized (``min`` first) on write to
        prevent bidirectional duplicates.

        Args:
            relation: The relation to persist.

        Returns:
            The relation ID.
        """
        src, tgt = _canonical_order(relation.source_id, relation.target_id)

        async with self._pool.write() as conn:
            await conn.execute(
                """INSERT INTO relations
                (id, source_id, target_id, relation_type, weight,
                 co_occurrence_count, last_activated, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(relation.id),
                    str(src),
                    str(tgt),
                    relation.relation_type.value,
                    relation.weight,
                    relation.co_occurrence_count,
                    relation.last_activated.isoformat(),
                    relation.created_at.isoformat(),
                ),
            )
            await conn.commit()

        logger.debug(
            "relation_created",
            relation_id=str(relation.id),
            source=str(src),
            target=str(tgt),
        )
        return relation.id

    async def get(self, relation_id: RelationId) -> Relation | None:
        """Get a relation by ID."""
        async with self._pool.read() as conn:
            cursor = await conn.execute(
                "SELECT * FROM relations WHERE id = ?",
                (str(relation_id),),
            )
            row = await cursor.fetchone()

        if row is None:
            return None
        return self._row_to_relation(row)

    async def get_relations_for(
        self, concept_id: ConceptId, mind_id: MindId | None = None
    ) -> list[Relation]:
        """Get all relations where concept is source OR target.

        Args:
            concept_id: The concept to find relations for.
            mind_id: Ignored in v0.1 (single-mind). Prepared for v1.0.
        """
        async with self._pool.read() as conn:
            cursor = await conn.execute(
                "SELECT * FROM relations WHERE source_id = ? OR target_id = ?",
                (str(concept_id), str(concept_id)),
            )
            rows = await cursor.fetchall()

        return [self._row_to_relation(r) for r in rows]

    async def get_neighbors(
        self,
        concept_id: ConceptId,
        mind_id: MindId | None = None,
        limit: int = 20,
    ) -> list[tuple[ConceptId, float]]:
        """Get neighboring concept IDs ordered by weight DESC.

        Returns both directions: concepts where this concept is
        source OR target.

        Args:
            concept_id: The center concept.
            mind_id: Ignored in v0.1 (single-mind).
            limit: Max neighbors to return.

        Returns:
            List of (concept_id, weight) tuples.
        """
        async with self._pool.read() as conn:
            cursor = await conn.execute(
                """SELECT
                    CASE WHEN source_id = ? THEN target_id ELSE source_id END AS neighbor_id,
                    weight
                FROM relations
                WHERE source_id = ? OR target_id = ?
                ORDER BY weight DESC
                LIMIT ?""",
                (str(concept_id), str(concept_id), str(concept_id), limit),
            )
            rows = await cursor.fetchall()

        return [(ConceptId(r[0]), float(r[1])) for r in rows]

    async def update_weight(self, relation_id: RelationId, new_weight: float) -> None:
        """Update a relation's weight."""
        async with self._pool.write() as conn:
            await conn.execute(
                "UPDATE relations SET weight = ?, last_activated = ? WHERE id = ?",
                (new_weight, datetime.now(UTC).isoformat(), str(relation_id)),
            )
            await conn.commit()

    async def increment_co_occurrence(self, source_id: ConceptId, target_id: ConceptId) -> None:
        """Increment co-occurrence count and update last_activated.

        If the relation doesn't exist, creates it with weight=0.3.
        Input order does not matter — canonicalized before query.
        """
        src, tgt = _canonical_order(source_id, target_id)
        now = datetime.now(UTC).isoformat()

        async with self._pool.write() as conn:
            cursor = await conn.execute(
                "SELECT id FROM relations "
                "WHERE source_id = ? AND target_id = ? AND relation_type = ?",
                (str(src), str(tgt), RelationType.RELATED_TO.value),
            )
            row = await cursor.fetchone()

            if row is not None:
                await conn.execute(
                    "UPDATE relations SET "
                    "co_occurrence_count = co_occurrence_count + 1, "
                    "last_activated = ? WHERE id = ?",
                    (now, row[0]),
                )
            else:
                from sovyx.engine.types import generate_id

                new_id = generate_id()
                await conn.execute(
                    """INSERT INTO relations
                    (id, source_id, target_id, relation_type, weight,
                     co_occurrence_count, last_activated, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        new_id,
                        str(src),
                        str(tgt),
                        RelationType.RELATED_TO.value,
                        0.3,
                        1,
                        now,
                        now,
                    ),
                )
            await conn.commit()

    async def get_or_create(
        self,
        source_id: ConceptId,
        target_id: ConceptId,
        relation_type: RelationType = RelationType.RELATED_TO,
    ) -> Relation:
        """Get existing relation or create a new one.

        Input order does not matter — the pair is canonicalized before
        lookup and creation.  ``get_or_create(A, B)`` and
        ``get_or_create(B, A)`` always resolve to the same row.

        Args:
            source_id: One concept in the pair.
            target_id: The other concept in the pair.
            relation_type: Type of relation.

        Returns:
            The existing or newly created relation.
        """
        src, tgt = _canonical_order(source_id, target_id)

        async with self._pool.read() as conn:
            cursor = await conn.execute(
                "SELECT * FROM relations "
                "WHERE source_id = ? AND target_id = ? AND relation_type = ?",
                (str(src), str(tgt), relation_type.value),
            )
            row = await cursor.fetchone()

        if row is not None:
            return self._row_to_relation(row)

        from sovyx.brain.models import Relation as RelationModel

        relation = RelationModel(
            source_id=src,
            target_id=tgt,
            relation_type=relation_type,
        )
        await self.create(relation)
        return relation

    async def delete(self, relation_id: RelationId) -> None:
        """Delete a relation."""
        async with self._pool.write() as conn:
            await conn.execute(
                "DELETE FROM relations WHERE id = ?",
                (str(relation_id),),
            )
            await conn.commit()

    async def delete_weak(self, mind_id: MindId, threshold: float = 0.05) -> int:
        """Remove relations with weight below threshold.

        Args:
            mind_id: The mind to clean up (filters via concept join).
            threshold: Weight threshold.

        Returns:
            Number of relations deleted.
        """
        async with self._pool.write() as conn:
            cursor = await conn.execute(
                """DELETE FROM relations WHERE weight < ?
                AND source_id IN (SELECT id FROM concepts WHERE mind_id = ?)""",
                (threshold, str(mind_id)),
            )
            count = cursor.rowcount
            await conn.commit()

        if count > 0:
            logger.info("weak_relations_deleted", mind_id=str(mind_id), count=count)
        return count

    async def transfer_relations(self, from_id: ConceptId, to_id: ConceptId) -> int:
        """Transfer all relations from one concept to another.

        Used during concept merging. Updates source_id/target_id
        to point to the surviving concept. Deletes duplicate relations
        that would violate the canonical order unique constraint.

        Args:
            from_id: The concept being merged (will be deleted).
            to_id: The surviving concept.

        Returns:
            Number of relations transferred.
        """
        async with self._pool.write() as conn:
            # Get all relations involving from_id
            cursor = await conn.execute(
                "SELECT id, source_id, target_id FROM relations "
                "WHERE source_id = ? OR target_id = ?",
                (str(from_id), str(from_id)),
            )
            rows = await cursor.fetchall()
            transferred = 0

            for row in rows:
                rid, src, tgt = str(row[0]), str(row[1]), str(row[2])
                # Compute new endpoints
                new_src = str(to_id) if src == str(from_id) else src
                new_tgt = str(to_id) if tgt == str(from_id) else tgt

                # Skip self-loops
                if new_src == new_tgt:
                    await conn.execute("DELETE FROM relations WHERE id = ?", (rid,))
                    continue

                # Canonical order
                can_src = min(new_src, new_tgt)
                can_tgt = max(new_src, new_tgt)

                # Check if relation already exists for survivor
                dup = await conn.execute(
                    "SELECT id FROM relations WHERE source_id = ? AND target_id = ?",
                    (can_src, can_tgt),
                )
                existing = await dup.fetchone()

                if existing:
                    # Duplicate — delete the transferred one
                    await conn.execute("DELETE FROM relations WHERE id = ?", (rid,))
                else:
                    # Transfer
                    await conn.execute(
                        "UPDATE relations SET source_id = ?, target_id = ? WHERE id = ?",
                        (can_src, can_tgt, rid),
                    )
                    transferred += 1

            await conn.commit()

        return transferred

    async def get_degree_centrality(
        self,
        mind_id: MindId,
    ) -> dict[str, tuple[int, float]]:
        """Return (degree, avg_weight) per concept for importance scoring.

        Counts bidirectional relations (both source and target) and
        computes average weight per concept. Only includes concepts
        belonging to the given mind.

        Args:
            mind_id: Mind to compute centrality for.

        Returns:
            Dict mapping concept_id → (degree, avg_weight).
        """
        async with self._pool.read() as conn:
            cursor = await conn.execute(
                """SELECT concept_id, COUNT(*) as degree, AVG(weight) as avg_w
                FROM (
                    SELECT source_id as concept_id, weight FROM relations
                    WHERE source_id IN (SELECT id FROM concepts WHERE mind_id = ?)
                    UNION ALL
                    SELECT target_id as concept_id, weight FROM relations
                    WHERE target_id IN (SELECT id FROM concepts WHERE mind_id = ?)
                )
                GROUP BY concept_id""",
                (str(mind_id), str(mind_id)),
            )
            rows = await cursor.fetchall()
        return {str(r[0]): (int(r[1]), float(r[2])) for r in rows}

    @staticmethod
    def _row_to_relation(row: object) -> Relation:
        """Convert a database row to a Relation model."""
        from sovyx.brain.models import Relation as RelationModel

        r = tuple(row)  # type: ignore[arg-type,var-annotated]  # aiosqlite.Row → tuple

        return RelationModel(
            id=RelationId(r[0]),
            source_id=ConceptId(r[1]),
            target_id=ConceptId(r[2]),
            relation_type=RelationType(r[3]),
            weight=float(r[4]),
            co_occurrence_count=int(r[5]),
            last_activated=parse_db_datetime(r[6]),
            created_at=parse_db_datetime(r[7]),
        )
