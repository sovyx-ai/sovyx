"""Sovyx relation repository — synapse management for concept graph.

CRUD for relations between concepts with graph traversal queries.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sovyx.engine.types import ConceptId, MindId, RelationId, RelationType
from sovyx.observability.logging import get_logger
from sovyx.persistence.datetime_utils import parse_db_datetime

if TYPE_CHECKING:
    from sovyx.brain.models import Relation
    from sovyx.persistence.pool import DatabasePool

logger = get_logger(__name__)


class RelationRepository:
    """Repository for brain relations (synapses between concepts).

    Manages the concept graph with Hebbian-style weight updates
    and co-occurrence tracking.
    """

    def __init__(self, pool: DatabasePool) -> None:
        self._pool = pool

    async def create(self, relation: Relation) -> RelationId:
        """Create a relation between two concepts.

        Args:
            relation: The relation to persist.

        Returns:
            The relation ID.
        """
        async with self._pool.write() as conn:
            await conn.execute(
                """INSERT INTO relations
                (id, source_id, target_id, relation_type, weight,
                 co_occurrence_count, last_activated, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(relation.id),
                    str(relation.source_id),
                    str(relation.target_id),
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
            source=str(relation.source_id),
            target=str(relation.target_id),
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
        """
        now = datetime.now(UTC).isoformat()

        async with self._pool.write() as conn:
            cursor = await conn.execute(
                "SELECT id FROM relations "
                "WHERE source_id = ? AND target_id = ? AND relation_type = ?",
                (str(source_id), str(target_id), RelationType.RELATED_TO.value),
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
                        str(source_id),
                        str(target_id),
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

        Args:
            source_id: Source concept.
            target_id: Target concept.
            relation_type: Type of relation.

        Returns:
            The existing or newly created relation.
        """
        async with self._pool.read() as conn:
            cursor = await conn.execute(
                "SELECT * FROM relations "
                "WHERE source_id = ? AND target_id = ? AND relation_type = ?",
                (str(source_id), str(target_id), relation_type.value),
            )
            row = await cursor.fetchone()

        if row is not None:
            return self._row_to_relation(row)

        from sovyx.brain.models import Relation as RelationModel

        relation = RelationModel(
            source_id=source_id,
            target_id=target_id,
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

    @staticmethod
    def _row_to_relation(row: object) -> Relation:
        """Convert a database row to a Relation model."""
        from sovyx.brain.models import Relation as RelationModel

        r: tuple[Any, ...] = tuple(row)  # type: ignore[arg-type]

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
