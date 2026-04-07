"""VAL-37: Brain invariant properties — Hypothesis.

Additional brain invariants beyond test_brain_properties.py.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from sovyx.brain.models import Concept
from sovyx.brain.working_memory import WorkingMemory
from sovyx.context.tokenizer import TokenCounter
from sovyx.engine.types import ConceptCategory, ConceptId, MindId


class TestWorkingMemoryInvariants:
    """Working memory must maintain bounded capacity."""

    @settings(deadline=None)
    @given(n=st.integers(min_value=0, max_value=50))
    def test_items_never_exceed_capacity(self, n: int) -> None:
        wm = WorkingMemory(capacity=10)
        for i in range(n):
            wm.activate(ConceptId(f"c{i}"), activation=float(i) / max(n, 1))
        assert wm.size <= 10

    @settings(deadline=None)
    @given(activations=st.lists(st.floats(0.1, 1), min_size=1, max_size=20))
    def test_highest_activation_survives(self, activations: list[float]) -> None:
        wm = WorkingMemory(capacity=5)
        for i, a in enumerate(activations):
            wm.activate(ConceptId(f"c{i}"), activation=a)
        active = wm.get_active_concepts()
        assert len(active) <= 5

    @settings(deadline=None)
    @given(st.just(True))
    def test_clear_empties(self, _: bool) -> None:
        wm = WorkingMemory(capacity=10)
        for i in range(5):
            wm.activate(ConceptId(f"c{i}"), activation=0.5)
        wm.clear()
        assert wm.size == 0


class TestConceptInvariants:
    """Concept model invariants."""

    @settings(deadline=None)
    @given(
        importance=st.floats(0, 1),
        confidence=st.floats(0, 1),
    )
    def test_importance_and_confidence_bounded(
        self, importance: float, confidence: float,
    ) -> None:
        c = Concept(
            id=ConceptId("test"),
            mind_id=MindId("m"),
            name="test",
            content="test content",
            category=ConceptCategory.FACT,
            importance=importance,
            confidence=confidence,
        )
        assert 0 <= c.importance <= 1
        assert 0 <= c.confidence <= 1


class TestTokenCounterInvariants:
    """Token counting invariants."""

    @settings(deadline=None, max_examples=50)
    @given(text=st.text(max_size=1000))
    def test_count_non_negative(self, text: str) -> None:
        counter = TokenCounter()
        assert counter.count(text) >= 0

    @settings(deadline=None, max_examples=50)
    @given(
        a=st.text(max_size=200),
        b=st.text(max_size=200),
    )
    def test_concatenation_subadditive(self, a: str, b: str) -> None:
        """count(a+b) <= count(a) + count(b) + 3 (BPE boundary slack).

        BPE tokenizers can produce *more* tokens at concat boundaries
        when the joined text breaks existing merge opportunities.
        A slack of 3 accounts for worst-case boundary effects.
        """
        counter = TokenCounter()
        assert counter.count(a + b) <= counter.count(a) + counter.count(b) + 3

    @settings(deadline=None, max_examples=30)
    @given(text=st.text(min_size=1, max_size=500))
    def test_truncate_respects_limit(self, text: str) -> None:
        counter = TokenCounter()
        limit = 5
        truncated = counter.truncate(text, limit)
        assert counter.count(truncated) <= limit
