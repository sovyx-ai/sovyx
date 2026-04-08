"""Sovyx brain learning — Hebbian strengthening and Ebbinghaus decay.

"Neurons that fire together wire together" + forgetting curve.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from sovyx.brain.concept_repo import ConceptRepository
    from sovyx.brain.relation_repo import RelationRepository
    from sovyx.engine.types import ConceptId, MindId

logger = get_logger(__name__)

# Maximum concepts for O(n²) Hebbian pairing.
# 20 → 190 pairs; 50 → 1225 pairs (too slow for per-request).
_MAX_HEBBIAN_CONCEPTS = 20


class HebbianLearning:
    """Strengthen connections between co-activated concepts.

    When concepts are mentioned in the same context, the relation
    between them is strengthened.

    Formula (IMPL-002):
        new_weight = min(1.0, old_weight + learning_rate × (1 - old_weight) × co_activation)

    co_activation = min(activation_A, activation_B) when activations provided,
    otherwise defaults to 1.0.

    Weight is always clamped to [0, 1] to maintain invariant.
    """

    def __init__(
        self,
        relation_repo: RelationRepository,
        learning_rate: float = 0.1,
    ) -> None:
        self._relations = relation_repo
        self._learning_rate = learning_rate

    async def strengthen(
        self,
        concept_ids: list[ConceptId],
        activations: dict[ConceptId, float] | None = None,
        *,
        priority_ids: list[ConceptId] | None = None,
    ) -> int:
        """Strengthen relations between all pairs of provided concepts.

        Creates relations if they don't exist. Increments co-occurrence.

        Args:
            concept_ids: Concepts that co-occurred.
            activations: Optional activation levels per concept.
            priority_ids: Concepts that MUST be included even when capping
                (e.g. newly learned concepts from this turn). Prevents
                island formation in the knowledge graph.

        Returns:
            Number of relations strengthened or created.
        """
        if len(concept_ids) < 2:  # noqa: PLR2004
            return 0

        # Cap to top-K by activation to bound O(n²) pair generation.
        # 20 concepts → 190 pairs (max ~570 DB ops). Acceptable in background.
        if len(concept_ids) > _MAX_HEBBIAN_CONCEPTS:
            priority_set = set(priority_ids or [])
            if activations:
                # Split into priority (must-include) and rest
                rest = [c for c in concept_ids if c not in priority_set]
                rest_sorted = sorted(
                    rest,
                    key=lambda cid: activations.get(cid, 0.0),
                    reverse=True,
                )
                # Priority first, fill remaining slots with top-activated
                slots = max(0, _MAX_HEBBIAN_CONCEPTS - len(priority_set))
                concept_ids = list(priority_set) + rest_sorted[:slots]
            else:
                # Without activations, priority first, then fill
                rest = [c for c in concept_ids if c not in priority_set]
                slots = max(0, _MAX_HEBBIAN_CONCEPTS - len(priority_set))
                concept_ids = list(priority_set) + rest[:slots]
            logger.debug(
                "hebbian_concepts_capped",
                capped_to=_MAX_HEBBIAN_CONCEPTS,
                priority_kept=len(priority_set),
            )

        count = 0
        for i, id_a in enumerate(concept_ids):
            for id_b in concept_ids[i + 1 :]:
                co_activation = 1.0
                if activations:
                    act_a = activations.get(id_a, 1.0)
                    act_b = activations.get(id_b, 1.0)
                    co_activation = min(act_a, act_b)

                # Get or create the relation
                relation = await self._relations.get_or_create(id_a, id_b)

                # Apply Hebbian formula with clamp
                old_weight = relation.weight
                delta = self._learning_rate * (1.0 - old_weight) * co_activation
                new_weight = min(1.0, old_weight + delta)

                await self._relations.update_weight(relation.id, new_weight)
                await self._relations.increment_co_occurrence(id_a, id_b)
                count += 1

        logger.debug(
            "hebbian_strengthen",
            concepts=len(concept_ids),
            relations_updated=count,
        )
        return count


class EbbinghausDecay:
    """Forgetting curve — memories weaken without reinforcement.

    Formula (SPE-004 §forgetting):
        new_importance = importance × (1 - decay_rate × (1 / (1 + access_count × 0.1)))

    access_count acts as rehearsal factor:
        - 0 accesses: full decay rate
        - 10 accesses: half decay rate
        - 100 accesses: ~10% decay rate (nearly immune)

    Uses batch SQL for efficiency (1 query, not N queries).
    """

    def __init__(
        self,
        concept_repo: ConceptRepository,
        relation_repo: RelationRepository,
        decay_rate: float = 0.1,
        min_strength: float = 0.01,
    ) -> None:
        self._concepts = concept_repo
        self._relations = relation_repo
        self._decay_rate = decay_rate
        self._min_strength = min_strength

    async def apply_decay(self, mind_id: MindId) -> tuple[int, int]:
        """Apply decay to all concepts and relations for a mind.

        Uses batch SQL for efficiency.

        Returns:
            (concepts_decayed, relations_decayed)
        """
        # Decay concepts (batch SQL)
        async with self._concepts._pool.write() as conn:
            cursor = await conn.execute(
                """UPDATE concepts
                SET importance = importance * (1 - ? * (1.0 / (1 + access_count * 0.1))),
                    updated_at = CURRENT_TIMESTAMP
                WHERE mind_id = ?""",
                (self._decay_rate, str(mind_id)),
            )
            concepts_decayed = cursor.rowcount
            await conn.commit()

        # Decay relations (batch SQL via concept join)
        async with self._relations._pool.write() as conn:
            cursor = await conn.execute(
                """UPDATE relations
                SET weight = weight * (1 - ? * (1.0 / (1 + co_occurrence_count * 0.1))),
                    last_activated = CURRENT_TIMESTAMP
                WHERE source_id IN (SELECT id FROM concepts WHERE mind_id = ?)""",
                (self._decay_rate, str(mind_id)),
            )
            relations_decayed = cursor.rowcount
            await conn.commit()

        logger.info(
            "ebbinghaus_decay_applied",
            mind_id=str(mind_id),
            concepts=concepts_decayed,
            relations=relations_decayed,
        )
        return (concepts_decayed, relations_decayed)

    async def prune_weak(self, mind_id: MindId) -> tuple[int, int]:
        """Remove concepts and relations below strength threshold.

        Returns:
            (concepts_pruned, relations_pruned)
        """
        # Prune weak relations first (FK safety)
        relations_pruned = await self._relations.delete_weak(mind_id, threshold=self._min_strength)

        # Prune weak concepts
        async with self._concepts._pool.write() as conn:
            cursor = await conn.execute(
                """DELETE FROM concepts
                WHERE mind_id = ? AND importance < ?""",
                (str(mind_id), self._min_strength),
            )
            concepts_pruned = cursor.rowcount
            await conn.commit()

        if concepts_pruned > 0 or relations_pruned > 0:
            logger.info(
                "weak_memories_pruned",
                mind_id=str(mind_id),
                concepts=concepts_pruned,
                relations=relations_pruned,
            )
        return (concepts_pruned, relations_pruned)
