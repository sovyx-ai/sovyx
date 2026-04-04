"""Tests for sovyx.brain.working_memory — prefrontal working memory."""

from __future__ import annotations

from sovyx.brain.working_memory import WorkingMemory
from sovyx.engine.types import ConceptId


class TestActivation:
    """Concept activation."""

    def test_activate_and_get(self) -> None:
        wm = WorkingMemory()
        wm.activate(ConceptId("c1"), 0.8)
        assert wm.get_activation(ConceptId("c1")) == 0.8

    def test_inactive_returns_zero(self) -> None:
        wm = WorkingMemory()
        assert wm.get_activation(ConceptId("c1")) == 0.0

    def test_reinforce_keeps_max(self) -> None:
        wm = WorkingMemory()
        wm.activate(ConceptId("c1"), 0.5)
        wm.activate(ConceptId("c1"), 0.8)
        assert wm.get_activation(ConceptId("c1")) == 0.8

    def test_reinforce_does_not_lower(self) -> None:
        wm = WorkingMemory()
        wm.activate(ConceptId("c1"), 0.9)
        wm.activate(ConceptId("c1"), 0.3)
        assert wm.get_activation(ConceptId("c1")) == 0.9

    def test_default_activation_is_one(self) -> None:
        wm = WorkingMemory()
        wm.activate(ConceptId("c1"))
        assert wm.get_activation(ConceptId("c1")) == 1.0


class TestCapacity:
    """Capacity management and eviction."""

    def test_capacity_respected(self) -> None:
        wm = WorkingMemory(capacity=3)
        for i in range(5):
            wm.activate(ConceptId(f"c{i}"), float(i) / 10)
        assert wm.size <= 3  # noqa: PLR2004

    def test_evicts_weakest(self) -> None:
        wm = WorkingMemory(capacity=3)
        wm.activate(ConceptId("weak"), 0.1)
        wm.activate(ConceptId("medium"), 0.5)
        wm.activate(ConceptId("strong"), 0.9)
        # Full, adding new one should evict "weak"
        wm.activate(ConceptId("new"), 0.7)
        assert wm.get_activation(ConceptId("weak")) == 0.0
        assert wm.get_activation(ConceptId("new")) == 0.7

    def test_size_property(self) -> None:
        wm = WorkingMemory()
        assert wm.size == 0
        wm.activate(ConceptId("c1"))
        assert wm.size == 1

    def test_capacity_property(self) -> None:
        wm = WorkingMemory(capacity=25)
        assert wm.capacity == 25


class TestGetActiveConcepts:
    """Active concept listing."""

    def test_ordered_by_activation_desc(self) -> None:
        wm = WorkingMemory()
        wm.activate(ConceptId("low"), 0.2)
        wm.activate(ConceptId("high"), 0.9)
        wm.activate(ConceptId("mid"), 0.5)

        active = wm.get_active_concepts()
        assert len(active) == 3  # noqa: PLR2004
        assert active[0][0] == ConceptId("high")
        assert active[1][0] == ConceptId("mid")
        assert active[2][0] == ConceptId("low")

    def test_min_activation_filter(self) -> None:
        wm = WorkingMemory()
        wm.activate(ConceptId("high"), 0.8)
        wm.activate(ConceptId("low"), 0.05)

        active = wm.get_active_concepts(min_activation=0.1)
        assert len(active) == 1
        assert active[0][0] == ConceptId("high")

    def test_empty_memory(self) -> None:
        wm = WorkingMemory()
        assert wm.get_active_concepts() == []


class TestDecay:
    """Activation decay."""

    def test_decay_reduces_activation(self) -> None:
        wm = WorkingMemory(decay_rate=0.1)
        wm.activate(ConceptId("c1"), 1.0)
        wm.decay_all()
        assert abs(wm.get_activation(ConceptId("c1")) - 0.9) < 0.001

    def test_decay_removes_below_threshold(self) -> None:
        wm = WorkingMemory(decay_rate=0.99)
        wm.activate(ConceptId("c1"), 0.5)
        wm.decay_all()
        # 0.5 * 0.01 = 0.005 < 0.01 → removed
        assert wm.get_activation(ConceptId("c1")) == 0.0
        assert wm.size == 0

    def test_multiple_decay_cycles(self) -> None:
        wm = WorkingMemory(decay_rate=0.5)
        wm.activate(ConceptId("c1"), 1.0)
        wm.decay_all()  # 0.5
        wm.decay_all()  # 0.25
        wm.decay_all()  # 0.125
        assert abs(wm.get_activation(ConceptId("c1")) - 0.125) < 0.001

    def test_decay_on_empty_memory(self) -> None:
        wm = WorkingMemory()
        wm.decay_all()  # should not crash
        assert wm.size == 0


class TestClear:
    """Memory clearing."""

    def test_clear_removes_all(self) -> None:
        wm = WorkingMemory()
        wm.activate(ConceptId("c1"))
        wm.activate(ConceptId("c2"))
        wm.clear()
        assert wm.size == 0
        assert wm.get_activation(ConceptId("c1")) == 0.0


class TestConcurrency:
    """Concurrency model documentation."""

    def test_no_lock_needed(self) -> None:
        """WorkingMemory has no lock — sync methods are atomic under asyncio."""
        wm = WorkingMemory()
        # All methods are synchronous — no concurrent mutation possible
        assert not hasattr(wm, "lock")
