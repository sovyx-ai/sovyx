"""Sovyx spreading activation — Collins & Loftus (1975).

Activation spreads through the concept graph via weighted relations.
Operates on WorkingMemory + RelationRepository without modifying the database.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sovyx.engine.types import ConceptId
from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from sovyx.brain.relation_repo import RelationRepository
    from sovyx.brain.working_memory import WorkingMemory

logger = get_logger(__name__)


class SpreadingActivation:
    """Activation spreads from seed concepts to neighbors via relations.

    Algorithm (IMPL-002):
        1. Seed concepts receive initial activation
        2. For each active concept, spread to neighbors:
           neighbor.activation += concept.activation × relation.weight × decay_factor
        3. Repeat for max_iterations
        4. Activation attenuated by distance (geometric decay)
        5. Threshold: concepts below min_activation are ignored

    Does NOT modify the database. Operates on WorkingMemory + RelationRepository.

    Note: per-node activation is NOT clamped to [0, 1]. Multiple paths
    converging on a node SUM activations. This is intentional — scores
    are used for ranking, not as probabilities.
    """

    def __init__(
        self,
        relation_repo: RelationRepository,
        working_memory: WorkingMemory,
        max_iterations: int = 3,
        decay_factor: float = 0.7,
        min_activation: float = 0.01,
    ) -> None:
        self._relations = relation_repo
        self._memory = working_memory
        self._max_iterations = max_iterations
        self._decay_factor = decay_factor
        self._min_activation = min_activation

    async def activate(
        self,
        seed_concepts: list[tuple[ConceptId, float]],
    ) -> list[tuple[ConceptId, float]]:
        """Execute spreading activation from seeds.

        Args:
            seed_concepts: List of (concept_id, initial_activation) pairs.

        Returns:
            All activated concepts sorted by activation DESC,
            including seeds and spread-activated concepts.
        """
        # Initialize activations from seeds
        activations: dict[str, float] = {}
        for concept_id, activation in seed_concepts:
            key = str(concept_id)
            activations[key] = activations.get(key, 0.0) + activation
            self._memory.activate(concept_id, activation)

        # Iterate spreading
        for iteration in range(self._max_iterations):
            new_activations: dict[str, float] = {}

            for key, activation in activations.items():
                if activation < self._min_activation:
                    continue

                # Get neighbors and spread
                neighbors = await self._relations.get_neighbors(ConceptId(key))

                spread = activation * self._decay_factor
                for neighbor_id, weight in neighbors:
                    nkey = str(neighbor_id)
                    contribution = spread * weight
                    if contribution >= self._min_activation:
                        new_activations[nkey] = new_activations.get(nkey, 0.0) + contribution

            if not new_activations:
                logger.debug(
                    "spreading_converged",
                    iteration=iteration + 1,
                )
                break

            # Merge new activations
            for key, val in new_activations.items():
                old = activations.get(key, 0.0)
                activations[key] = old + val
                self._memory.activate(ConceptId(key), activations[key])

        # Build sorted result
        result = [(ConceptId(k), v) for k, v in activations.items() if v >= self._min_activation]
        result.sort(key=lambda x: x[1], reverse=True)

        logger.debug(
            "spreading_complete",
            seeds=len(seed_concepts),
            activated=len(result),
        )
        return result

    async def activate_from_text(
        self,
        concept_ids: list[ConceptId],
    ) -> list[tuple[ConceptId, float]]:
        """Simplified version: all seeds with activation=1.0.

        Args:
            concept_ids: Concepts to activate as seeds.

        Returns:
            All activated concepts sorted by activation DESC.
        """
        seeds = [(cid, 1.0) for cid in concept_ids]
        return await self.activate(seeds)
