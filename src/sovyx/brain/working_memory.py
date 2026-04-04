"""Sovyx working memory — prefrontal cortex simulation.

In-memory cache of currently active concepts with activation decay.
Capacity-limited with eviction of the least active concept.
Thread-safe for asyncio cooperative scheduling.
"""

from __future__ import annotations

from sovyx.engine.types import ConceptId


class WorkingMemory:
    """Working memory — currently active concepts.

    In-process Python dicts. Reconstructed from SQLite on startup.
    Capacity limited (default: 50 active concepts).
    Concepts decay over time if not reactivated.

    Concurrency: all methods are synchronous — atomic under asyncio cooperative scheduling.
    Read-only methods (get_activation, get_active_concepts) are safe without
    lock since dict reads are atomic in CPython.
    """

    def __init__(self, capacity: int = 50, decay_rate: float = 0.1) -> None:
        self._capacity = capacity
        self._decay_rate = decay_rate
        self._activations: dict[str, float] = {}

    def activate(self, concept_id: ConceptId, activation: float = 1.0) -> None:
        """Activate a concept. Reinforces if already active. Evicts weakest if full.

        Args:
            concept_id: The concept to activate.
            activation: Activation level to set/add.
        """
        key = str(concept_id)

        if key in self._activations:
            # Reinforce existing activation
            self._activations[key] = max(self._activations[key], activation)
        else:
            # Evict weakest if at capacity
            if len(self._activations) >= self._capacity:
                weakest = min(self._activations, key=self._activations.get)  # type: ignore[arg-type]
                del self._activations[weakest]
            self._activations[key] = activation

    def get_activation(self, concept_id: ConceptId) -> float:
        """Return activation level (0.0 if not active).

        Args:
            concept_id: The concept to check.

        Returns:
            Current activation level.
        """
        return self._activations.get(str(concept_id), 0.0)

    def get_active_concepts(self, min_activation: float = 0.1) -> list[tuple[ConceptId, float]]:
        """Return active concepts ordered by activation DESC.

        Args:
            min_activation: Minimum activation threshold.

        Returns:
            List of (concept_id, activation) tuples.
        """
        return sorted(
            [(ConceptId(k), v) for k, v in self._activations.items() if v >= min_activation],
            key=lambda x: x[1],
            reverse=True,
        )

    def decay_all(self) -> None:
        """Apply decay to all active concepts.

        Concepts that fall below 0.01 are removed.
        """
        to_remove: list[str] = []
        for key in self._activations:
            self._activations[key] *= 1.0 - self._decay_rate
            if self._activations[key] < 0.01:
                to_remove.append(key)

        for key in to_remove:
            del self._activations[key]

    def clear(self) -> None:
        """Remove all active concepts."""
        self._activations.clear()

    @property
    def size(self) -> int:
        """Number of currently active concepts."""
        return len(self._activations)

    @property
    def capacity(self) -> int:
        """Maximum capacity."""
        return self._capacity

    # Lock removed (P13): all methods are synchronous and atomic
    # under asyncio cooperative scheduling. No concurrent mutation possible.
