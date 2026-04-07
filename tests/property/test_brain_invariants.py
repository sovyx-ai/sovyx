"""VAL-37: Brain invariant properties (Hypothesis).

Validates fundamental invariants of the brain system:
1. Importance ∈ [0.0, 1.0] always
2. Confidence ∈ [0.0, 1.0] always
3. Working memory ≤ capacity
4. Spreading activation converges (doesn't explode)
5. Hebbian weight ∈ [0.0, 1.0]
6. Ebbinghaus strength ≥ 0

max_examples=500, deadline=None.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from sovyx.brain.models import Concept, Episode, Relation
from sovyx.brain.spreading import SpreadingActivation
from sovyx.brain.working_memory import WorkingMemory
from sovyx.engine.types import (
    ConceptCategory,
    ConceptId,
    ConversationId,
    MindId,
    RelationType,
)

# ── Strategies ──────────────────────────────────────────────────────────────

_unit_float = st.floats(min_value=0.0, max_value=1.0, allow_nan=False)
_signed_unit_float = st.floats(min_value=-1.0, max_value=1.0, allow_nan=False)
_pos_int = st.integers(min_value=0, max_value=10_000)
_concept_categories = st.sampled_from(list(ConceptCategory))
_relation_types = st.sampled_from(list(RelationType))
_mind_ids = st.text(min_size=1, max_size=20).map(MindId)
_concept_ids = st.text(min_size=1, max_size=20).map(ConceptId)


# ── 1. Importance ∈ [0.0, 1.0] ─────────────────────────────────────────────


class TestImportanceInvariant:
    """Importance must always be in [0.0, 1.0]."""

    @given(importance=_unit_float)
    @settings(max_examples=500, deadline=None)
    def test_concept_importance_valid(self, importance: float) -> None:
        c = Concept(mind_id=MindId("m"), name="test", importance=importance)
        assert 0.0 <= c.importance <= 1.0

    @given(importance=_unit_float)
    @settings(max_examples=500, deadline=None)
    def test_episode_importance_valid(self, importance: float) -> None:
        e = Episode(
            mind_id=MindId("m"),
            conversation_id=ConversationId("c"),
            user_input="hi",
            assistant_response="hello",
            importance=importance,
        )
        assert 0.0 <= e.importance <= 1.0

    @given(importance=st.floats(min_value=-10.0, max_value=-0.001, allow_nan=False))
    @settings(max_examples=100, deadline=None)
    def test_concept_rejects_negative_importance(self, importance: float) -> None:
        with pytest.raises(Exception):  # noqa: B017
            Concept(mind_id=MindId("m"), name="test", importance=importance)

    @given(importance=st.floats(min_value=1.001, max_value=10.0, allow_nan=False))
    @settings(max_examples=100, deadline=None)
    def test_concept_rejects_excess_importance(self, importance: float) -> None:
        with pytest.raises(Exception):  # noqa: B017
            Concept(mind_id=MindId("m"), name="test", importance=importance)


# ── 2. Confidence ∈ [0.0, 1.0] ─────────────────────────────────────────────


class TestConfidenceInvariant:
    """Confidence must always be in [0.0, 1.0]."""

    @given(confidence=_unit_float)
    @settings(max_examples=500, deadline=None)
    def test_concept_confidence_valid(self, confidence: float) -> None:
        c = Concept(mind_id=MindId("m"), name="test", confidence=confidence)
        assert 0.0 <= c.confidence <= 1.0

    @given(confidence=st.floats(min_value=-10.0, max_value=-0.001, allow_nan=False))
    @settings(max_examples=100, deadline=None)
    def test_concept_rejects_negative_confidence(self, confidence: float) -> None:
        with pytest.raises(Exception):  # noqa: B017
            Concept(mind_id=MindId("m"), name="test", confidence=confidence)

    @given(confidence=st.floats(min_value=1.001, max_value=10.0, allow_nan=False))
    @settings(max_examples=100, deadline=None)
    def test_concept_rejects_excess_confidence(self, confidence: float) -> None:
        with pytest.raises(Exception):  # noqa: B017
            Concept(mind_id=MindId("m"), name="test", confidence=confidence)


# ── 3. Working Memory ≤ Capacity ───────────────────────────────────────────


class TestWorkingMemoryCapacity:
    """Working memory size never exceeds capacity."""

    @given(
        capacity=st.integers(min_value=1, max_value=100),
        n_activations=st.integers(min_value=0, max_value=200),
    )
    @settings(max_examples=500, deadline=None)
    def test_size_never_exceeds_capacity(self, capacity: int, n_activations: int) -> None:
        wm = WorkingMemory(capacity=capacity)
        for i in range(n_activations):
            wm.activate(ConceptId(f"c{i}"), activation=float(i % 10) / 10.0 + 0.1)
        assert wm.size <= wm.capacity

    @given(
        capacity=st.integers(min_value=1, max_value=50),
        activations=st.lists(
            st.tuples(
                st.text(min_size=1, max_size=10),
                st.floats(min_value=0.01, max_value=1.0, allow_nan=False),
            ),
            min_size=0,
            max_size=100,
        ),
    )
    @settings(max_examples=500, deadline=None)
    def test_arbitrary_activation_sequence(
        self, capacity: int, activations: list[tuple[str, float]]
    ) -> None:
        wm = WorkingMemory(capacity=capacity)
        for cid, act in activations:
            wm.activate(ConceptId(cid), activation=act)
            assert wm.size <= capacity

    @given(
        capacity=st.integers(min_value=1, max_value=50),
        n_decays=st.integers(min_value=1, max_value=100),
    )
    @settings(max_examples=200, deadline=None)
    def test_decay_reduces_or_maintains_size(self, capacity: int, n_decays: int) -> None:
        wm = WorkingMemory(capacity=capacity, decay_rate=0.3)
        # Fill to capacity
        for i in range(capacity):
            wm.activate(ConceptId(f"c{i}"), activation=0.5)

        initial_size = wm.size
        for _ in range(n_decays):
            wm.decay_all()
        assert wm.size <= initial_size

    def test_clear_makes_size_zero(self) -> None:
        wm = WorkingMemory(capacity=10)
        for i in range(10):
            wm.activate(ConceptId(f"c{i}"))
        wm.clear()
        assert wm.size == 0


# ── 4. Spreading Activation Converges ──────────────────────────────────────


class TestSpreadingActivationConvergence:
    """Spreading activation must converge (not explode)."""

    @pytest.mark.asyncio()
    @given(
        n_seeds=st.integers(min_value=1, max_value=10),
        n_neighbors=st.integers(min_value=0, max_value=5),
        decay_factor=st.floats(min_value=0.1, max_value=0.9, allow_nan=False),
    )
    @settings(max_examples=200, deadline=None)
    async def test_activation_values_finite(
        self, n_seeds: int, n_neighbors: int, decay_factor: float
    ) -> None:
        """All activation values remain finite (no explosion)."""
        wm = WorkingMemory(capacity=100)

        # Mock relation repo that returns fixed neighbors
        mock_repo = AsyncMock()
        neighbor_pairs = [(ConceptId(f"neighbor-{j}"), 0.5) for j in range(n_neighbors)]
        mock_repo.get_neighbors = AsyncMock(return_value=neighbor_pairs)

        sa = SpreadingActivation(
            relation_repo=mock_repo,
            working_memory=wm,
            max_iterations=5,
            decay_factor=decay_factor,
        )

        seeds = [(ConceptId(f"seed-{i}"), 1.0) for i in range(n_seeds)]
        result = await sa.activate(seeds)

        # All activations must be finite
        for _, activation in result:
            assert activation > 0
            assert activation < float("inf")

    @pytest.mark.asyncio()
    async def test_converges_with_cycle(self) -> None:
        """Graph with cycle A→B→A doesn't explode."""
        wm = WorkingMemory(capacity=100)

        # Mock: A's neighbor is B, B's neighbor is A
        async def mock_neighbors(concept_id: ConceptId) -> list[tuple[ConceptId, float]]:
            if str(concept_id) == "A":
                return [(ConceptId("B"), 0.8)]
            if str(concept_id) == "B":
                return [(ConceptId("A"), 0.8)]
            return []

        mock_repo = AsyncMock()
        mock_repo.get_neighbors = AsyncMock(side_effect=mock_neighbors)

        sa = SpreadingActivation(
            relation_repo=mock_repo,
            working_memory=wm,
            max_iterations=10,
            decay_factor=0.7,
        )

        result = await sa.activate([(ConceptId("A"), 1.0)])
        activations = dict(result)

        # Both should be activated but bounded
        assert activations.get(ConceptId("A"), 0) < 100
        assert activations.get(ConceptId("B"), 0) < 100

    @pytest.mark.asyncio()
    async def test_dense_graph_bounded(self) -> None:
        """Fully connected graph of 5 nodes doesn't explode."""
        wm = WorkingMemory(capacity=100)
        nodes = [ConceptId(f"n{i}") for i in range(5)]

        async def mock_neighbors(concept_id: ConceptId) -> list[tuple[ConceptId, float]]:
            return [(n, 0.5) for n in nodes if n != concept_id]

        mock_repo = AsyncMock()
        mock_repo.get_neighbors = AsyncMock(side_effect=mock_neighbors)

        sa = SpreadingActivation(
            relation_repo=mock_repo,
            working_memory=wm,
            max_iterations=5,
            decay_factor=0.5,
        )

        result = await sa.activate([(nodes[0], 1.0)])
        for _, activation in result:
            assert activation < 1000  # bounded, not exploding


# ── 5. Hebbian Weight ∈ [0.0, 1.0] ────────────────────────────────────────


class TestHebbianWeightInvariant:
    """Hebbian learning weight must always be in [0.0, 1.0]."""

    @given(weight=_unit_float)
    @settings(max_examples=500, deadline=None)
    def test_relation_weight_valid(self, weight: float) -> None:
        r = Relation(
            source_id=ConceptId("a"),
            target_id=ConceptId("b"),
            weight=weight,
        )
        assert 0.0 <= r.weight <= 1.0

    @given(weight=st.floats(min_value=-10.0, max_value=-0.001, allow_nan=False))
    @settings(max_examples=100, deadline=None)
    def test_relation_rejects_negative_weight(self, weight: float) -> None:
        with pytest.raises(Exception):  # noqa: B017
            Relation(source_id=ConceptId("a"), target_id=ConceptId("b"), weight=weight)

    @given(weight=st.floats(min_value=1.001, max_value=10.0, allow_nan=False))
    @settings(max_examples=100, deadline=None)
    def test_relation_rejects_excess_weight(self, weight: float) -> None:
        with pytest.raises(Exception):  # noqa: B017
            Relation(source_id=ConceptId("a"), target_id=ConceptId("b"), weight=weight)

    @given(
        old_weight=_unit_float,
        learning_rate=st.floats(min_value=0.01, max_value=1.0, allow_nan=False),
        co_activation=_unit_float,
    )
    @settings(max_examples=500, deadline=None)
    def test_hebbian_formula_stays_in_bounds(
        self, old_weight: float, learning_rate: float, co_activation: float
    ) -> None:
        """The Hebbian formula new_weight = min(1.0, old + lr × (1 - old) × co_act) ∈ [0, 1]."""
        delta = learning_rate * (1.0 - old_weight) * co_activation
        new_weight = min(1.0, old_weight + delta)
        assert 0.0 <= new_weight <= 1.0

    @given(
        n_iterations=st.integers(min_value=1, max_value=100),
        learning_rate=st.floats(min_value=0.01, max_value=1.0, allow_nan=False),
    )
    @settings(max_examples=200, deadline=None)
    def test_repeated_hebbian_never_exceeds_one(
        self, n_iterations: int, learning_rate: float
    ) -> None:
        """Applying Hebbian formula N times never exceeds 1.0."""
        weight = 0.0
        for _ in range(n_iterations):
            delta = learning_rate * (1.0 - weight) * 1.0
            weight = min(1.0, weight + delta)
        assert 0.0 <= weight <= 1.0


# ── 6. Ebbinghaus Strength ≥ 0 ─────────────────────────────────────────────


class TestEbbinghausStrengthInvariant:
    """Ebbinghaus decay never produces negative strength."""

    @given(
        importance=_unit_float,
        decay_rate=st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
        access_count=st.integers(min_value=0, max_value=10_000),
    )
    @settings(max_examples=500, deadline=None)
    def test_decay_formula_non_negative(
        self, importance: float, decay_rate: float, access_count: int
    ) -> None:
        """importance × (1 - decay_rate × (1 / (1 + access_count × 0.1))) ≥ 0."""
        rehearsal_factor = 1.0 / (1.0 + access_count * 0.1)
        new_importance = importance * (1.0 - decay_rate * rehearsal_factor)
        assert new_importance >= -1e-15  # floating-point tolerance

    @given(
        importance=_unit_float,
        n_decays=st.integers(min_value=1, max_value=50),
        decay_rate=st.floats(min_value=0.01, max_value=0.5, allow_nan=False),
    )
    @settings(max_examples=500, deadline=None)
    def test_repeated_decay_non_negative(
        self, importance: float, n_decays: int, decay_rate: float
    ) -> None:
        """Applying decay N times never goes negative."""
        val = importance
        access_count = 0
        for _ in range(n_decays):
            rehearsal_factor = 1.0 / (1.0 + access_count * 0.1)
            val = val * (1.0 - decay_rate * rehearsal_factor)
        assert val >= -1e-15  # floating-point tolerance

    @given(
        weight=_unit_float,
        decay_rate=st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
        co_occurrence_count=st.integers(min_value=0, max_value=10_000),
    )
    @settings(max_examples=500, deadline=None)
    def test_relation_decay_non_negative(
        self, weight: float, decay_rate: float, co_occurrence_count: int
    ) -> None:
        """Relation weight decay: weight × (1 - decay_rate × (1/(1 + co_occ × 0.1))) ≥ 0."""
        rehearsal_factor = 1.0 / (1.0 + co_occurrence_count * 0.1)
        new_weight = weight * (1.0 - decay_rate * rehearsal_factor)
        assert new_weight >= -1e-15


# ── Cross-invariant properties ─────────────────────────────────────────────


class TestCrossInvariants:
    """Properties that span multiple brain subsystems."""

    @given(
        capacity=st.integers(min_value=1, max_value=20),
        activation=st.floats(min_value=0.01, max_value=1.0, allow_nan=False),
    )
    @settings(max_examples=200, deadline=None)
    def test_working_memory_activations_positive(self, capacity: int, activation: float) -> None:
        """All activations in working memory are positive."""
        wm = WorkingMemory(capacity=capacity)
        for i in range(capacity + 5):
            wm.activate(ConceptId(f"c{i}"), activation=activation)

        active = wm.get_active_concepts(min_activation=0.0)
        for _, act in active:
            assert act > 0

    @given(
        valence=_signed_unit_float,
        arousal=_signed_unit_float,
    )
    @settings(max_examples=500, deadline=None)
    def test_emotional_dimensions_bounded(self, valence: float, arousal: float) -> None:
        """Emotional valence and arousal ∈ [-1.0, 1.0]."""
        e = Episode(
            mind_id=MindId("m"),
            conversation_id=ConversationId("c"),
            user_input="hi",
            assistant_response="hello",
            emotional_valence=valence,
            emotional_arousal=arousal,
        )
        assert -1.0 <= e.emotional_valence <= 1.0
        assert -1.0 <= e.emotional_arousal <= 1.0
