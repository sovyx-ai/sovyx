"""VAL-38: Token budget invariant properties — Hypothesis."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from sovyx.context.budget import TokenBudgetManager


class TestBudgetInvariants:
    """TokenBudgetManager invariants under any valid input."""

    @settings(deadline=None, max_examples=50)
    @given(
        conv_len=st.integers(0, 100),
        brain_count=st.integers(0, 50),
        complexity=st.floats(0, 1),
        context_window=st.integers(2048, 200000),
    )
    def test_total_equals_context_window(
        self, conv_len: int, brain_count: int, complexity: float, context_window: int,
    ) -> None:
        budget = TokenBudgetManager()
        alloc = budget.allocate(conv_len, brain_count, complexity, context_window)
        assert alloc.total == context_window

    @settings(deadline=None, max_examples=50)
    @given(
        conv_len=st.integers(0, 100),
        brain_count=st.integers(0, 50),
        complexity=st.floats(0, 1),
        context_window=st.integers(2048, 200000),
    )
    def test_all_slots_non_negative(
        self, conv_len: int, brain_count: int, complexity: float, context_window: int,
    ) -> None:
        budget = TokenBudgetManager()
        alloc = budget.allocate(conv_len, brain_count, complexity, context_window)
        assert alloc.system_prompt >= 0
        assert alloc.memory_concepts >= 0
        assert alloc.memory_episodes >= 0
        assert alloc.temporal >= 0
        assert alloc.conversation >= 0
        assert alloc.response_reserve >= 0

    @settings(deadline=None, max_examples=50)
    @given(
        conv_len=st.integers(0, 100),
        brain_count=st.integers(0, 50),
        complexity=st.floats(0, 1),
        context_window=st.integers(2048, 200000),
    )
    def test_slots_sum_to_total(
        self, conv_len: int, brain_count: int, complexity: float, context_window: int,
    ) -> None:
        budget = TokenBudgetManager()
        alloc = budget.allocate(conv_len, brain_count, complexity, context_window)
        slot_sum = (
            alloc.system_prompt + alloc.memory_concepts + alloc.memory_episodes
            + alloc.temporal + alloc.conversation + alloc.response_reserve
        )
        # Rounding: each of 6 slots is floored, so sum can be up to 6 less than total
        assert alloc.total - 6 <= slot_sum <= alloc.total

    @settings(deadline=None, max_examples=20)
    @given(
        complexity=st.floats(0, 1),
    )
    def test_high_complexity_increases_response_reserve(self, complexity: float) -> None:
        budget = TokenBudgetManager()
        low = budget.allocate(5, 5, 0.1, 10000)
        high = budget.allocate(5, 5, 0.9, 10000)
        # Higher complexity should give at least as much response reserve
        assert high.response_reserve >= low.response_reserve
