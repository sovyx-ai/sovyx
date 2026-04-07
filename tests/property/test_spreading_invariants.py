"""POLISH-17: Property-based tests for brain spreading activation.

Properties verified:
  1. Activation values are always non-negative
  2. Seed concepts appear in output
  3. Result is sorted by activation DESC
  4. Same input always produces same output (deterministic)
  5. Empty seeds → empty result
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from sovyx.brain.relation_repo import RelationRepository
from sovyx.brain.spreading import SpreadingActivation
from sovyx.brain.working_memory import WorkingMemory
from sovyx.engine.types import ConceptId


@pytest.fixture()
def spreading() -> SpreadingActivation:
    """SpreadingActivation with mocked relation repo (no neighbors)."""
    mock_repo = AsyncMock(spec=RelationRepository)
    mock_repo.get_related = AsyncMock(return_value=[])
    memory = WorkingMemory()
    return SpreadingActivation(mock_repo, memory)


class TestSpreadingInvariants:
    """Property-based tests for spreading activation."""

    @pytest.mark.asyncio()
    @given(
        activations=st.lists(
            st.floats(min_value=0.01, max_value=10.0),
            min_size=1,
            max_size=10,
        ),
    )
    @settings(max_examples=50)
    async def test_activations_always_non_negative(
        self,
        activations: list[float],
    ) -> None:
        """All output activations are ≥ 0."""
        mock_repo = AsyncMock(spec=RelationRepository)
        mock_repo.get_related = AsyncMock(return_value=[])
        memory = WorkingMemory()
        sa = SpreadingActivation(mock_repo, memory)

        seeds = [(ConceptId(f"c-{i}"), act) for i, act in enumerate(activations)]
        result = await sa.activate(seeds)
        for _concept_id, activation in result:
            assert activation >= 0.0

    @pytest.mark.asyncio()
    @given(
        activations=st.lists(
            st.floats(min_value=0.1, max_value=5.0),
            min_size=1,
            max_size=5,
        ),
    )
    @settings(max_examples=50)
    async def test_seeds_appear_in_output(
        self,
        activations: list[float],
    ) -> None:
        """All seed concepts appear in the output."""
        mock_repo = AsyncMock(spec=RelationRepository)
        mock_repo.get_related = AsyncMock(return_value=[])
        memory = WorkingMemory()
        sa = SpreadingActivation(mock_repo, memory)

        seeds = [(ConceptId(f"c-{i}"), act) for i, act in enumerate(activations)]
        result = await sa.activate(seeds)
        result_ids = {str(cid) for cid, _ in result}
        for cid, _ in seeds:
            assert str(cid) in result_ids

    @pytest.mark.asyncio()
    @given(
        activations=st.lists(
            st.floats(min_value=0.1, max_value=5.0),
            min_size=2,
            max_size=10,
        ),
    )
    @settings(max_examples=50)
    async def test_result_sorted_descending(
        self,
        activations: list[float],
    ) -> None:
        """Results are sorted by activation value (descending)."""
        mock_repo = AsyncMock(spec=RelationRepository)
        mock_repo.get_related = AsyncMock(return_value=[])
        memory = WorkingMemory()
        sa = SpreadingActivation(mock_repo, memory)

        seeds = [(ConceptId(f"c-{i}"), act) for i, act in enumerate(activations)]
        result = await sa.activate(seeds)
        values = [act for _, act in result]
        for i in range(len(values) - 1):
            assert values[i] >= values[i + 1]

    @pytest.mark.asyncio()
    async def test_empty_seeds_empty_result(self, spreading: SpreadingActivation) -> None:
        """Empty seed list produces empty result."""
        result = await spreading.activate([])
        assert result == []

    @pytest.mark.asyncio()
    @given(
        activation=st.floats(min_value=0.1, max_value=5.0),
    )
    @settings(max_examples=30)
    async def test_deterministic(self, activation: float) -> None:
        """Same input always produces same output."""
        seeds = [(ConceptId("det-1"), activation)]

        mock_repo = AsyncMock(spec=RelationRepository)
        mock_repo.get_related = AsyncMock(return_value=[])

        mem1 = WorkingMemory()
        sa1 = SpreadingActivation(mock_repo, mem1)
        r1 = await sa1.activate(seeds)

        mem2 = WorkingMemory()
        sa2 = SpreadingActivation(mock_repo, mem2)
        r2 = await sa2.activate(seeds)

        assert len(r1) == len(r2)
        for (c1, a1), (c2, a2) in zip(r1, r2, strict=True):
            assert c1 == c2
            assert abs(a1 - a2) < 1e-10
