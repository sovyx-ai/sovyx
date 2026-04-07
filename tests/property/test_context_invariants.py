"""POLISH-15: Property-based tests for context budget allocation.

Properties verified:
  1. Total allocation never exceeds context window
  2. All slot allocations are non-negative
  3. Slot sum equals total
  4. Same input always produces same output (deterministic)
  5. Larger context window → proportionally larger allocations
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from sovyx.context.budget import TokenBudgetManager


class TestContextBudgetInvariants:
    """Property-based tests for TokenBudgetManager."""

    @given(
        conv_len=st.integers(min_value=0, max_value=200),
        brain_count=st.integers(min_value=0, max_value=100),
        complexity=st.floats(min_value=0.0, max_value=1.0),
        window=st.integers(min_value=2048, max_value=500_000),
    )
    def test_total_never_exceeds_context_window(
        self,
        conv_len: int,
        brain_count: int,
        complexity: float,
        window: int,
    ) -> None:
        """Budget total never exceeds the context window."""
        mgr = TokenBudgetManager()
        budget = mgr.allocate(conv_len, brain_count, complexity, window)
        assert budget.total <= window

    @given(
        conv_len=st.integers(min_value=0, max_value=200),
        brain_count=st.integers(min_value=0, max_value=100),
        complexity=st.floats(min_value=0.0, max_value=1.0),
        window=st.integers(min_value=2048, max_value=500_000),
    )
    def test_all_slots_non_negative(
        self,
        conv_len: int,
        brain_count: int,
        complexity: float,
        window: int,
    ) -> None:
        """Every slot allocation is ≥ 0."""
        mgr = TokenBudgetManager()
        budget = mgr.allocate(conv_len, brain_count, complexity, window)
        assert budget.system_prompt >= 0
        assert budget.memory_concepts >= 0
        assert budget.memory_episodes >= 0
        assert budget.temporal >= 0
        assert budget.conversation >= 0
        assert budget.response_reserve >= 0

    @given(
        conv_len=st.integers(min_value=0, max_value=200),
        brain_count=st.integers(min_value=0, max_value=100),
        complexity=st.floats(min_value=0.0, max_value=1.0),
        window=st.integers(min_value=2048, max_value=500_000),
    )
    def test_slot_sum_equals_total(
        self,
        conv_len: int,
        brain_count: int,
        complexity: float,
        window: int,
    ) -> None:
        """Sum of all slots equals the reported total."""
        mgr = TokenBudgetManager()
        budget = mgr.allocate(conv_len, brain_count, complexity, window)
        slot_sum = (
            budget.system_prompt
            + budget.memory_concepts
            + budget.memory_episodes
            + budget.temporal
            + budget.conversation
            + budget.response_reserve
        )
        assert slot_sum == budget.total

    @given(
        conv_len=st.integers(min_value=0, max_value=200),
        brain_count=st.integers(min_value=0, max_value=100),
        complexity=st.floats(min_value=0.0, max_value=1.0),
        window=st.integers(min_value=2048, max_value=500_000),
    )
    def test_deterministic(
        self,
        conv_len: int,
        brain_count: int,
        complexity: float,
        window: int,
    ) -> None:
        """Same inputs always produce same outputs."""
        mgr = TokenBudgetManager()
        b1 = mgr.allocate(conv_len, brain_count, complexity, window)
        b2 = mgr.allocate(conv_len, brain_count, complexity, window)
        assert b1 == b2

    @given(
        conv_len=st.integers(min_value=0, max_value=100),
        brain_count=st.integers(min_value=0, max_value=50),
        complexity=st.floats(min_value=0.0, max_value=1.0),
    )
    def test_larger_window_larger_or_equal_total(
        self,
        conv_len: int,
        brain_count: int,
        complexity: float,
    ) -> None:
        """Doubling context window → total allocation increases or stays same."""
        mgr = TokenBudgetManager()
        small = mgr.allocate(conv_len, brain_count, complexity, 4_000)
        large = mgr.allocate(conv_len, brain_count, complexity, 128_000)
        assert large.total >= small.total
