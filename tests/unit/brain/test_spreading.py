"""Tests for sovyx.brain.spreading — spreading activation algorithm."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

from hypothesis import given, settings
from hypothesis import strategies as st

from sovyx.brain.spreading import SpreadingActivation
from sovyx.brain.working_memory import WorkingMemory
from sovyx.engine.types import ConceptId

if TYPE_CHECKING:
    from sovyx.brain.relation_repo import RelationRepository


def _mock_relation_repo(
    graph: dict[str, list[tuple[str, float]]],
) -> RelationRepository:
    """Create a mock RelationRepository from an adjacency list.

    graph: {"c1": [("c2", 0.8), ("c3", 0.5)], ...}
    """
    repo = AsyncMock()

    async def get_neighbors(
        concept_id: ConceptId,
        mind_id: object = None,
        limit: int = 20,
    ) -> list[tuple[ConceptId, float]]:
        key = str(concept_id)
        neighbors = graph.get(key, [])
        return [(ConceptId(n), w) for n, w in neighbors[:limit]]

    repo.get_neighbors = AsyncMock(side_effect=get_neighbors)
    return repo


class TestBasicSpreading:
    """Core spreading activation behavior."""

    async def test_seed_activation(self) -> None:
        """Seeds appear in output with their activation."""
        repo = _mock_relation_repo({})
        wm = WorkingMemory()
        sa = SpreadingActivation(repo, wm)

        result = await sa.activate([(ConceptId("c1"), 1.0)])
        assert len(result) == 1
        assert result[0][0] == ConceptId("c1")
        assert result[0][1] == 1.0

    async def test_spreads_to_neighbors(self) -> None:
        """Activation spreads from seed to neighbors."""
        repo = _mock_relation_repo(
            {
                "c1": [("c2", 0.8), ("c3", 0.5)],
            }
        )
        wm = WorkingMemory()
        sa = SpreadingActivation(repo, wm, max_iterations=1)

        result = await sa.activate([(ConceptId("c1"), 1.0)])
        ids = {str(r[0]) for r in result}
        assert "c1" in ids
        assert "c2" in ids
        assert "c3" in ids

    async def test_decay_by_distance(self) -> None:
        """2-hop activation is weaker than 1-hop."""
        repo = _mock_relation_repo(
            {
                "c1": [("c2", 1.0)],
                "c2": [("c3", 1.0)],
            }
        )
        wm = WorkingMemory()
        sa = SpreadingActivation(repo, wm, max_iterations=2, decay_factor=0.7)

        result = await sa.activate([(ConceptId("c1"), 1.0)])
        act_map = {str(r[0]): r[1] for r in result}

        # c2 gets 1.0 * 0.7 * 1.0 = 0.7
        assert act_map.get("c2", 0) > act_map.get("c3", 0)

    async def test_no_relations_no_spread(self) -> None:
        """Concepts without relations don't spread."""
        repo = _mock_relation_repo({"c1": []})
        wm = WorkingMemory()
        sa = SpreadingActivation(repo, wm)

        result = await sa.activate([(ConceptId("c1"), 1.0)])
        assert len(result) == 1

    async def test_min_activation_threshold(self) -> None:
        """Concepts below min_activation are excluded."""
        repo = _mock_relation_repo(
            {
                "c1": [("c2", 0.001)],  # very weak
            }
        )
        wm = WorkingMemory()
        sa = SpreadingActivation(repo, wm, min_activation=0.01)

        result = await sa.activate([(ConceptId("c1"), 1.0)])
        ids = {str(r[0]) for r in result}
        # c2 gets 1.0 * 0.7 * 0.001 = 0.0007 < 0.01 → filtered
        assert "c2" not in ids


class TestMaxIterations:
    """Iteration control."""

    async def test_respects_max_iterations(self) -> None:
        """Spreading stops after max_iterations."""
        # Long chain: c1 → c2 → c3 → c4 → c5
        repo = _mock_relation_repo(
            {
                "c1": [("c2", 1.0)],
                "c2": [("c3", 1.0)],
                "c3": [("c4", 1.0)],
                "c4": [("c5", 1.0)],
            }
        )
        wm = WorkingMemory()
        sa = SpreadingActivation(repo, wm, max_iterations=2, decay_factor=0.9)

        result = await sa.activate([(ConceptId("c1"), 1.0)])
        ids = {str(r[0]) for r in result}
        # With 2 iterations: c1, c2 (iter 1), c3 (iter 2)
        assert "c1" in ids
        assert "c2" in ids
        assert "c3" in ids
        # c4 might get some activation from iter 2 spreading c2's neighbors
        # but c5 should not be reached
        assert "c5" not in ids


class TestCyclicGraphs:
    """Cyclic graph handling."""

    async def test_cycle_does_not_infinite_loop(self) -> None:
        """Cyclic graph terminates in max_iterations."""
        repo = _mock_relation_repo(
            {
                "c1": [("c2", 0.8)],
                "c2": [("c3", 0.8)],
                "c3": [("c1", 0.8)],  # cycle!
            }
        )
        wm = WorkingMemory()
        sa = SpreadingActivation(repo, wm, max_iterations=3)

        result = await sa.activate([(ConceptId("c1"), 1.0)])
        assert len(result) > 0  # terminated, didn't hang


class TestConvergence:
    """Convergence behavior."""

    async def test_converges_early_if_no_new_activations(self) -> None:
        """Stops early when no new nodes are activated."""
        repo = _mock_relation_repo(
            {
                "c1": [("c2", 0.5)],
                "c2": [],  # dead end
            }
        )
        wm = WorkingMemory()
        sa = SpreadingActivation(repo, wm, max_iterations=10)

        result = await sa.activate([(ConceptId("c1"), 1.0)])
        # Should converge in 1-2 iterations, not 10
        assert len(result) <= 2  # noqa: PLR2004


class TestMultipleSeeds:
    """Multiple seed concepts."""

    async def test_multiple_seeds(self) -> None:
        repo = _mock_relation_repo(
            {
                "c1": [("c3", 0.5)],
                "c2": [("c3", 0.5)],
            }
        )
        wm = WorkingMemory()
        sa = SpreadingActivation(repo, wm, max_iterations=1)

        result = await sa.activate(
            [
                (ConceptId("c1"), 1.0),
                (ConceptId("c2"), 1.0),
            ]
        )
        act_map = {str(r[0]): r[1] for r in result}
        # c3 gets activation from BOTH c1 and c2
        assert act_map.get("c3", 0) > 0.3


class TestActivateFromText:
    """Simplified activation API."""

    async def test_activate_from_text(self) -> None:
        repo = _mock_relation_repo({"c1": [("c2", 0.8)]})
        wm = WorkingMemory()
        sa = SpreadingActivation(repo, wm, max_iterations=1)

        result = await sa.activate_from_text([ConceptId("c1")])
        ids = {str(r[0]) for r in result}
        assert "c1" in ids
        assert "c2" in ids


class TestWorkingMemoryIntegration:
    """Spreading updates working memory."""

    async def test_seeds_in_working_memory(self) -> None:
        repo = _mock_relation_repo({})
        wm = WorkingMemory()
        sa = SpreadingActivation(repo, wm)

        await sa.activate([(ConceptId("c1"), 0.9)])
        assert wm.get_activation(ConceptId("c1")) == 0.9

    async def test_spread_concepts_in_working_memory(self) -> None:
        repo = _mock_relation_repo({"c1": [("c2", 1.0)]})
        wm = WorkingMemory()
        sa = SpreadingActivation(repo, wm, max_iterations=1)

        await sa.activate([(ConceptId("c1"), 1.0)])
        assert wm.get_activation(ConceptId("c2")) > 0


class TestPropertyBased:
    """Property-based tests with Hypothesis."""

    @given(
        n_seeds=st.integers(min_value=1, max_value=5),
        n_neighbors=st.integers(min_value=0, max_value=5),
        decay=st.floats(min_value=0.1, max_value=0.9),
        max_iter=st.integers(min_value=1, max_value=5),
    )
    @settings(max_examples=50)
    async def test_always_terminates(
        self,
        n_seeds: int,
        n_neighbors: int,
        decay: float,
        max_iter: int,
    ) -> None:
        """Any graph config → algorithm terminates."""
        graph: dict[str, list[tuple[str, float]]] = {}
        for i in range(n_seeds):
            neighbors = [(f"n{j}", 0.5) for j in range(n_neighbors)]
            graph[f"s{i}"] = neighbors

        repo = _mock_relation_repo(graph)
        wm = WorkingMemory()
        sa = SpreadingActivation(
            repo,
            wm,
            max_iterations=max_iter,
            decay_factor=decay,
        )

        seeds = [(ConceptId(f"s{i}"), 1.0) for i in range(n_seeds)]
        result = await sa.activate(seeds)

        # Terminates
        assert isinstance(result, list)
        # No NaN or Inf
        for _, activation in result:
            assert activation == activation  # NaN check  # noqa: PLR0124
            assert activation != float("inf")
