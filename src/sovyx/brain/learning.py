"""Sovyx brain learning — Hebbian strengthening and Ebbinghaus decay.

"Neurons that fire together wire together" + forgetting curve.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sovyx.engine.types import RelationType
from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from sovyx.brain.concept_repo import ConceptRepository
    from sovyx.brain.relation_repo import RelationRepository
    from sovyx.brain.scoring import ImportanceScorer
    from sovyx.engine.types import ConceptId, MindId

logger = get_logger(__name__)

# Default K for star topology cross-turn pairing.
# Each new concept pairs with top-K existing by activation.
_STAR_K = 15


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

    _CO_ACTIVATION_THRESHOLD = 0.7
    _IMPORTANCE_BOOST = 0.02

    def __init__(
        self,
        relation_repo: RelationRepository,
        learning_rate: float = 0.1,
        concept_repo: ConceptRepository | None = None,
        importance_scorer: ImportanceScorer | None = None,
    ) -> None:
        self._relations = relation_repo
        self._learning_rate = learning_rate
        self._concepts = concept_repo
        self._scorer = importance_scorer

    async def strengthen(
        self,
        concept_ids: list[ConceptId],
        activations: dict[ConceptId, float] | None = None,
        *,
        relation_types: dict[tuple[str, str], str] | None = None,
    ) -> int:
        """Strengthen relations between all pairs — within-turn only.

        Used by ``strengthen_connection()`` for small concept sets
        extracted from a single message (typically 2-8 concepts).
        For cross-turn Hebbian learning, use ``strengthen_star()``.

        Creates relations if they don't exist. Increments co-occurrence.

        Args:
            concept_ids: Concepts that co-occurred in the same turn.
            activations: Optional activation levels per concept.
            relation_types: Optional mapping of (id_a_str, id_b_str) →
                RelationType value. Keys use canonical order (min, max).
                Pairs not in the map default to RELATED_TO.

        Returns:
            Number of relations strengthened or created.
        """
        if len(concept_ids) < 2:  # noqa: PLR2004
            return 0

        count = 0
        for i, id_a in enumerate(concept_ids):
            for id_b in concept_ids[i + 1 :]:
                rel_type = self._lookup_relation_type(id_a, id_b, relation_types)
                count += await self._strengthen_pair(
                    id_a, id_b, activations, relation_type=rel_type
                )

        logger.debug(
            "hebbian_strengthen",
            concepts=len(concept_ids),
            relations_updated=count,
        )
        return count

    async def strengthen_star(
        self,
        new_ids: list[ConceptId],
        existing_ids: list[ConceptId],
        activations: dict[ConceptId, float] | None = None,
        *,
        k: int = _STAR_K,
    ) -> int:
        """Star topology Hebbian — linear scaling, zero islands.

        Three pairing layers:
        1. **Within-turn:** all new_ids paired with each other (O(n²) on
           small set — typically 2-8 concepts per message).
        2. **Cross-turn:** each new_id paired with top-K existing_ids by
           activation. Linear: O(new × K) instead of O(n²) on full set.
        3. **Existing reinforcement:** for remaining existing_ids,
           strengthen ONLY pre-existing relations (SELECT before UPDATE,
           never create new). Prevents spurious edges between unrelated
           old concepts.

        Args:
            new_ids: Concepts learned this turn.
            existing_ids: Previously active concepts from working memory.
            activations: Activation levels per concept (for co_activation
                weighting and top-K selection).
            k: Number of existing concepts each new concept connects to.

        Returns:
            Number of relations strengthened or created.
        """
        if not new_ids and not existing_ids:
            return 0

        count = 0

        # Layer 1: Within-turn — new concepts pair with each other
        for i, id_a in enumerate(new_ids):
            for id_b in new_ids[i + 1 :]:
                count += await self._strengthen_pair(id_a, id_b, activations)

        # Layer 2: Cross-turn — each new concept pairs with top-K existing
        if new_ids and existing_ids:
            top_existing = self._top_k_by_activation(existing_ids, activations, k)
            for new_id in new_ids:
                for existing_id in top_existing:
                    count += await self._strengthen_pair(new_id, existing_id, activations)

        # Layer 3: Existing reinforcement — update ONLY pre-existing relations
        if len(existing_ids) >= 2:  # noqa: PLR2004
            count += await self._reinforce_existing(existing_ids, activations)

        logger.debug(
            "hebbian_star",
            new=len(new_ids),
            existing=len(existing_ids),
            k=k,
            relations_updated=count,
        )
        return count

    async def _strengthen_pair(
        self,
        id_a: ConceptId,
        id_b: ConceptId,
        activations: dict[ConceptId, float] | None,
        *,
        relation_type: RelationType | None = None,
    ) -> int:
        """Strengthen a single pair — get_or_create + Hebbian formula.

        Args:
            relation_type: If provided, used for the relation. Defaults
                to RELATED_TO when None.

        Returns:
            1 if strengthened, 0 otherwise.
        """
        co_activation = 1.0
        if activations:
            act_a = activations.get(id_a, 1.0)
            act_b = activations.get(id_b, 1.0)
            co_activation = min(act_a, act_b)

        rt = relation_type or RelationType.RELATED_TO
        relation = await self._relations.get_or_create(id_a, id_b, relation_type=rt)

        old_weight = relation.weight
        delta = self._learning_rate * (1.0 - old_weight) * co_activation
        new_weight = min(1.0, old_weight + delta)

        await self._relations.update_weight(relation.id, new_weight)
        await self._relations.increment_co_occurrence(id_a, id_b)

        # Importance reinforcement: highly co-activated pairs get a small
        # importance boost (counters Ebbinghaus decay).
        # When ImportanceScorer is available, uses diminishing returns
        # based on current importance + access count.
        if co_activation > self._CO_ACTIVATION_THRESHOLD and self._concepts is not None:
            if self._scorer:
                # Scorer-based: diminishing returns, respects soft ceiling
                for cid in (id_a, id_b):
                    concept = await self._concepts.get(cid)
                    if concept is not None:
                        new_imp = self._scorer.score_access_boost(
                            concept.importance,
                            concept.access_count,
                        )
                        boost = new_imp - concept.importance
                        # Dampen boost above 0.90 (soft ceiling consistency)
                        if concept.importance > 0.90:
                            boost *= 0.2
                        if boost > 0.001:
                            await self._concepts.boost_importance(cid, boost)
            else:
                # Fallback: flat boost (backwards compat)
                await self._concepts.boost_importance(id_a, self._IMPORTANCE_BOOST)
                await self._concepts.boost_importance(id_b, self._IMPORTANCE_BOOST)

        return 1

    @staticmethod
    def _lookup_relation_type(
        id_a: ConceptId,
        id_b: ConceptId,
        relation_types: dict[tuple[str, str], str] | None,
    ) -> RelationType | None:
        """Look up a relation type from the classification map.

        Uses canonical ordering (min, max) for the key lookup.

        Returns:
            The RelationType if found in the map, else None.
        """
        if not relation_types:
            return None

        key = (
            min(str(id_a), str(id_b)),
            max(str(id_a), str(id_b)),
        )
        raw = relation_types.get(key)
        if raw is None:
            return None

        try:
            return RelationType(raw)
        except ValueError:
            return None

    async def _reinforce_existing(
        self,
        existing_ids: list[ConceptId],
        activations: dict[ConceptId, float] | None,
    ) -> int:
        """Reinforce only pre-existing relations between existing concepts.

        Does NOT create new relations — prevents spurious edges between
        unrelated old concepts that happen to both be in working memory.

        Uses ``get_relations_for`` to fetch actual existing relations
        instead of ``get_or_create`` to guarantee no spurious creation.

        Returns:
            Number of relations reinforced.
        """
        count = 0
        existing_set = set(str(eid) for eid in existing_ids)
        checked: set[str] = set()

        for cid in existing_ids:
            relations = await self._relations.get_relations_for(cid)
            for relation in relations:
                # Both ends must be in existing set
                src, tgt = str(relation.source_id), str(relation.target_id)
                if src not in existing_set or tgt not in existing_set:
                    continue
                # Deduplicate by relation ID
                rid = str(relation.id)
                if rid in checked:
                    continue
                checked.add(rid)

                co_activation = 1.0
                if activations:
                    act_a = activations.get(relation.source_id, 1.0)
                    act_b = activations.get(relation.target_id, 1.0)
                    co_activation = min(act_a, act_b)

                old_weight = relation.weight
                delta = self._learning_rate * (1.0 - old_weight) * co_activation
                new_weight = min(1.0, old_weight + delta)
                await self._relations.update_weight(relation.id, new_weight)
                count += 1

        return count

    @staticmethod
    def _top_k_by_activation(
        concept_ids: list[ConceptId],
        activations: dict[ConceptId, float] | None,
        k: int,
    ) -> list[ConceptId]:
        """Return top-K concepts sorted by activation descending.

        If no activations provided, returns first K concepts.
        """
        if not activations:
            return concept_ids[:k]
        return sorted(
            concept_ids,
            key=lambda cid: activations.get(cid, 0.0),
            reverse=True,
        )[:k]


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
