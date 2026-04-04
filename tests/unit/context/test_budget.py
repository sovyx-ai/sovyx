"""Tests for sovyx.context.budget — Token budget manager."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from sovyx.context.budget import (
    MIN_CONTEXT_WINDOW,
    MIN_CONVERSATION,
    MIN_RESPONSE,
    MIN_SYSTEM_PROMPT,
    MIN_TEMPORAL,
    TokenBudgetError,
    TokenBudgetManager,
)


@pytest.fixture
def manager() -> TokenBudgetManager:
    return TokenBudgetManager()


class TestBasicAllocation:
    """Basic budget allocation."""

    def test_default_allocation(self, manager: TokenBudgetManager) -> None:
        b = manager.allocate(5, 10)
        assert b.total == 128_000
        assert b.system_prompt >= MIN_SYSTEM_PROMPT
        assert b.conversation >= MIN_CONVERSATION
        assert b.response_reserve >= MIN_RESPONSE
        assert b.temporal >= MIN_TEMPORAL

    def test_small_context_window(self, manager: TokenBudgetManager) -> None:
        b = manager.allocate(5, 10, context_window=4096)
        assert b.total == 4096
        assert b.system_prompt >= MIN_SYSTEM_PROMPT
        assert b.response_reserve >= MIN_RESPONSE

    def test_too_small_context_raises(self, manager: TokenBudgetManager) -> None:
        with pytest.raises(TokenBudgetError, match="too small"):
            manager.allocate(5, 10, context_window=1024)


class TestAdaptation:
    """Adaptive budget based on context."""

    def test_long_conversation_more_history(self, manager: TokenBudgetManager) -> None:
        short = manager.allocate(5, 10)
        long = manager.allocate(20, 10)
        assert long.conversation > short.conversation

    def test_short_conversation_more_memory(self, manager: TokenBudgetManager) -> None:
        short = manager.allocate(1, 10)
        medium = manager.allocate(5, 10)
        assert short.memory_concepts > medium.memory_concepts

    def test_complex_query_more_response(self, manager: TokenBudgetManager) -> None:
        simple = manager.allocate(5, 10, complexity=0.3)
        complex_ = manager.allocate(5, 10, complexity=0.9)
        assert complex_.response_reserve > simple.response_reserve

    def test_many_brain_results_more_concepts(self, manager: TokenBudgetManager) -> None:
        few = manager.allocate(5, 5)
        many = manager.allocate(5, 30)
        assert many.memory_concepts > few.memory_concepts


class TestMinimums:
    """Minimum allocations enforced."""

    def test_min_context_window(self) -> None:
        assert MIN_CONTEXT_WINDOW == 2048

    def test_minimum_allocations(self, manager: TokenBudgetManager) -> None:
        b = manager.allocate(5, 10, context_window=MIN_CONTEXT_WINDOW)
        assert b.system_prompt >= MIN_SYSTEM_PROMPT
        assert b.conversation >= MIN_CONVERSATION
        assert b.response_reserve >= MIN_RESPONSE
        assert b.temporal >= MIN_TEMPORAL


class TestPropertyBased:
    """Property-based tests."""

    @given(
        conv_len=st.integers(min_value=0, max_value=100),
        brain_count=st.integers(min_value=0, max_value=50),
        complexity=st.floats(min_value=0.0, max_value=1.0),
        window=st.integers(min_value=2048, max_value=200_000),
    )
    @settings(max_examples=30)
    def test_allocations_non_negative(
        self,
        conv_len: int,
        brain_count: int,
        complexity: float,
        window: int,
    ) -> None:
        m = TokenBudgetManager()
        b = m.allocate(conv_len, brain_count, complexity, window)
        assert b.system_prompt >= 0
        assert b.memory_concepts >= 0
        assert b.memory_episodes >= 0
        assert b.temporal >= 0
        assert b.conversation >= 0
        assert b.response_reserve >= 0


class TestOverflowNormalisation:
    """Budget overflow normalisation for small windows (Q15 fix)."""

    def test_small_window_floors_dont_exceed_total(self) -> None:
        """With MIN_CONTEXT_WINDOW, allocations must not exceed available."""
        m = TokenBudgetManager()
        b = m.allocate(
            conversation_length=0,
            brain_result_count=0,
            complexity=0.0,
            context_window=2048,
        )
        # Sum of non-response allocations must not exceed total - response_reserve
        usable = b.total - b.response_reserve
        allocated = (
            b.system_prompt + b.memory_concepts + b.memory_episodes
            + b.temporal + b.conversation
        )
        assert allocated <= usable

    def test_all_allocations_positive_on_small_window(self) -> None:
        """Even with tiny window, no allocation goes negative."""
        m = TokenBudgetManager()
        b = m.allocate(conversation_length=0, brain_result_count=0, context_window=2048)
        assert b.system_prompt > 0
        assert b.response_reserve > 0
        assert b.conversation >= 0
