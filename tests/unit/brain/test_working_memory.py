"""Tests for sovyx.brain.working_memory — prefrontal cortex cache."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from sovyx.brain.working_memory import WorkingMemory
from sovyx.engine.types import ConceptId


class TestActivation:
    """Concept activation."""

    def test_activate_new_concept(self) -> None:
        wm = WorkingMemory()
        wm.activate(ConceptId("c1"), 0.8)
        assert wm.get_activation(ConceptId("c1")) == 0.8

    def test_reinforce_existing(self) -> None:
        wm = WorkingMemory()
        wm.activate(ConceptId("c1"), 0.5)
        wm.activate(ConceptId("c1"), 0.9)
        assert wm.get_activation(ConceptId("c1")) == 0.9

    def test_reinforce_keeps_higher(self) -> None:
        wm = WorkingMemory()
        wm.activate(ConceptId("c1"), 0.9)
        wm.activate(ConceptId("c1"), 0.3)
        assert wm.get_activation(ConceptId("c1")) == 0.9

    def test_inactive_returns_zero(self) -> None:
        wm = WorkingMemory()
        assert wm.get_activation(ConceptId("nonexistent")) == 0.0

    def test_default_activation(self) -> None:
        wm = WorkingMemory()
        wm.activate(ConceptId("c1"))
        assert wm.get_activation(ConceptId("c1")) == 1.0


class TestCapacity:
    """Capacity enforcement."""

    def test_evicts_weakest_at_capacity(self) -> None:
        wm = WorkingMemory(capacity=3)
        wm.activate(ConceptId("c1"), 0.3)
        wm.activate(ConceptId("c2"), 0.5)
        wm.activate(ConceptId("c3"), 0.8)
        # c1 is weakest, should be evicted
        wm.activate(ConceptId("c4"), 0.6)

        assert wm.size == 3  # noqa: PLR2004
        assert wm.get_activation(ConceptId("c1")) == 0.0
        assert wm.get_activation(ConceptId("c4")) == 0.6

    def test_capacity_property(self) -> None:
        wm = WorkingMemory(capacity=10)
        assert wm.capacity == 10


class TestGetActiveConcepts:
    """Active concepts listing."""

    def test_ordered_by_activation_desc(self) -> None:
        wm = WorkingMemory()
        wm.activate(ConceptId("low"), 0.2)
        wm.activate(ConceptId("high"), 0.9)
        wm.activate(ConceptId("mid"), 0.5)

        active = wm.get_active_concepts()
        assert active[0][0] == ConceptId("high")
        assert active[-1][0] == ConceptId("low")

    def test_min_activation_filter(self) -> None:
        wm = WorkingMemory()
        wm.activate(ConceptId("strong"), 0.8)
        wm.activate(ConceptId("weak"), 0.05)

        active = wm.get_active_concepts(min_activation=0.1)
        assert len(active) == 1
        assert active[0][0] == ConceptId("strong")

    def test_empty_memory(self) -> None:
        wm = WorkingMemory()
        assert wm.get_active_concepts() == []


class TestDecay:
    """Activation decay."""

    def test_decay_reduces_activation(self) -> None:
        wm = WorkingMemory(decay_rate=0.5)
        wm.activate(ConceptId("c1"), 1.0)
        wm.decay_all()
        assert wm.get_activation(ConceptId("c1")) == 0.5

    def test_decay_removes_below_threshold(self) -> None:
        wm = WorkingMemory(decay_rate=0.99)
        wm.activate(ConceptId("c1"), 0.5)
        wm.decay_all()
        # 0.5 * 0.01 = 0.005 < 0.01 → removed
        assert wm.get_activation(ConceptId("c1")) == 0.0
        assert wm.size == 0

    def test_multiple_decays(self) -> None:
        wm = WorkingMemory(decay_rate=0.1)
        wm.activate(ConceptId("c1"), 1.0)
        for _ in range(5):
            wm.decay_all()
        # 1.0 * 0.9^5 ≈ 0.59
        activation = wm.get_activation(ConceptId("c1"))
        assert 0.55 < activation < 0.65  # noqa: PLR2004


class TestClear:
    """Memory clearing."""

    def test_clear_removes_all(self) -> None:
        wm = WorkingMemory()
        wm.activate(ConceptId("c1"), 1.0)
        wm.activate(ConceptId("c2"), 0.5)
        wm.clear()
        assert wm.size == 0


class TestSize:
    """Size tracking."""

    def test_size_tracks_count(self) -> None:
        wm = WorkingMemory()
        assert wm.size == 0
        wm.activate(ConceptId("c1"))
        assert wm.size == 1
        wm.activate(ConceptId("c2"))
        assert wm.size == 2  # noqa: PLR2004


class TestLock:
    """Asyncio lock availability."""

    def test_has_lock(self) -> None:
        wm = WorkingMemory()
        assert wm.lock is not None


class TestPropertyBased:
    """Property-based tests."""

    @settings(max_examples=50)
    @given(
        activations=st.lists(
            st.tuples(
                st.text(min_size=1, max_size=10),
                st.floats(min_value=0.01, max_value=1.0),
            ),
            min_size=0,
            max_size=100,
        ),
    )
    def test_size_never_exceeds_capacity(self, activations: list[tuple[str, float]]) -> None:
        wm = WorkingMemory(capacity=20)
        for name, act in activations:
            wm.activate(ConceptId(name), act)
        assert wm.size <= 20  # noqa: PLR2004

    @settings(max_examples=50)
    @given(
        decay_rate=st.floats(min_value=0.01, max_value=0.99),
        initial=st.floats(min_value=0.1, max_value=1.0),
        n_decays=st.integers(min_value=1, max_value=50),
    )
    def test_decay_monotonically_decreases(
        self, decay_rate: float, initial: float, n_decays: int
    ) -> None:
        wm = WorkingMemory(decay_rate=decay_rate)
        wm.activate(ConceptId("test"), initial)
        prev = initial
        for _ in range(n_decays):
            wm.decay_all()
            current = wm.get_activation(ConceptId("test"))
            assert current <= prev
            prev = current
