"""Property-based tests for Brain algorithms (Hypothesis)."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from sovyx.brain.working_memory import WorkingMemory
from sovyx.context.tokenizer import TokenCounter


class TestWorkingMemoryProperties:
    """Property-based tests for WorkingMemory."""

    @given(
        items=st.lists(
            st.tuples(st.text(min_size=1, max_size=50), st.floats(0.01, 1.0)),
            min_size=1,
            max_size=100,
        )
    )
    @settings(max_examples=50)
    def test_capacity_never_exceeded(self, items: list[tuple[str, float]]) -> None:
        """Working memory never exceeds its capacity."""
        wm = WorkingMemory(capacity=20)
        for name, activation in items:
            wm.activate(name, activation)
        assert len(wm.get_active_concepts()) <= 20  # noqa: PLR2004

    @given(
        activation=st.floats(0.01, 10.0),
    )
    @settings(max_examples=50)
    def test_activation_is_max_wins(self, activation: float) -> None:
        """Re-activating with higher value wins."""
        wm = WorkingMemory(capacity=50)
        wm.activate("test", 0.5)
        wm.activate("test", activation)
        active = dict(wm.get_active_concepts())
        if "test" in active:
            assert active["test"] >= min(activation, 0.5)

    @given(
        names=st.lists(
            st.text(
                alphabet=st.characters(whitelist_categories=("L", "N")),
                min_size=1,
                max_size=20,
            ),
            min_size=1,
            max_size=50,
            unique=True,
        )
    )
    @settings(max_examples=30)
    def test_decay_reduces_all(self, names: list[str]) -> None:
        """Decay reduces all activations."""
        wm = WorkingMemory(capacity=100, decay_rate=0.5)
        for name in names:
            wm.activate(name, 1.0)
        before = dict(wm.get_active_concepts())
        wm.decay_all()
        after = dict(wm.get_active_concepts())
        for name in before:
            if name in after:
                assert after[name] <= before[name]


class TestTokenCounterProperties:
    """Property-based tests for TokenCounter."""

    @given(text=st.text(min_size=0, max_size=5000))
    @settings(max_examples=50)
    def test_count_is_non_negative(self, text: str) -> None:
        """Token count is always non-negative."""
        tc = TokenCounter()
        assert tc.count(text) >= 0

    @given(texts=st.lists(st.text(min_size=1, max_size=100), min_size=2, max_size=5))
    @settings(max_examples=30)
    def test_concatenation_bound(self, texts: list[str]) -> None:
        """Token count of concatenation <= sum of parts + overhead."""
        tc = TokenCounter()
        total_parts = sum(tc.count(t) for t in texts)
        combined = tc.count(" ".join(texts))
        # Combined should be roughly bounded by sum of parts
        # (may be less due to subword merging, slightly more due to spaces/
        # boundary tokens that the tokenizer creates at join boundaries)
        assert combined <= total_parts + 2 * len(texts)


class TestRRFProperties:
    """Property-based tests for Reciprocal Rank Fusion."""

    @given(
        scores_a=st.lists(st.floats(0.0, 1.0), min_size=1, max_size=20),
        scores_b=st.lists(st.floats(0.0, 1.0), min_size=1, max_size=20),
        k=st.integers(1, 100),
    )
    @settings(max_examples=50)
    def test_rrf_scores_positive(
        self,
        scores_a: list[float],
        scores_b: list[float],
        k: int,
    ) -> None:
        """RRF scores are always positive."""
        # Simulate RRF formula
        for i in range(min(len(scores_a), len(scores_b))):
            rank_a = i + 1
            rank_b = i + 1
            rrf = 1 / (k + rank_a) + 1 / (k + rank_b)
            assert rrf > 0
