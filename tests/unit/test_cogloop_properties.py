"""Property-based tests for CogLoop and context assembly."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from sovyx.context.budget import TokenBudgetManager
from sovyx.context.tokenizer import TokenCounter


class TestTokenBudgetProperties:
    """Property-based tests for TokenBudgetManager."""

    @given(
        context_window=st.integers(2048, 200000),
        conv_len=st.integers(0, 100),
        complexity=st.floats(0.0, 1.0),
        brain_results=st.integers(0, 50),
    )
    @settings(max_examples=50)
    def test_allocations_sum_to_budget(
        self,
        context_window: int,
        conv_len: int,
        complexity: float,
        brain_results: int,
    ) -> None:
        """All slot allocations sum to <= context_window."""
        mgr = TokenBudgetManager()
        alloc = mgr.allocate(
            context_window=context_window,
            conversation_length=conv_len,
            complexity=complexity,
            brain_result_count=brain_results,
        )
        # total field is the constrained budget (respects context_window)
        assert alloc.total <= context_window

    @given(
        context_window=st.integers(2048, 200000),
    )
    @settings(max_examples=30)
    def test_response_reserve_always_positive(self, context_window: int) -> None:
        """Response reserve is always > 0."""
        mgr = TokenBudgetManager()
        alloc = mgr.allocate(
            context_window=context_window,
            conversation_length=5,
            brain_result_count=0,
        )
        assert alloc.response_reserve > 0


class TestEdgeCaseInputs:
    """Edge case inputs for cognitive pipeline."""

    def test_empty_string_tokens(self) -> None:
        """Empty string has 0 tokens."""
        tc = TokenCounter()
        assert tc.count("") == 0

    def test_very_long_string(self) -> None:
        """100K character string doesn't crash."""
        tc = TokenCounter()
        text = "a" * 100_000
        count = tc.count(text)
        assert count > 0

    def test_unicode_extreme(self) -> None:
        """Unicode edge cases don't crash tokenizer."""
        tc = TokenCounter()
        texts = [
            "🔮🧠💀",  # Emoji
            "مرحبا",  # Arabic (RTL)
            "こんにちは",  # Japanese
            "\u200b\u200c\u200d",  # Zero-width chars
            "a\u0301",  # Combining accent
            "\U0001f468\u200d\U0001f469\u200d\U0001f467",  # Family emoji
        ]
        for text in texts:
            count = tc.count(text)
            assert count >= 0

    def test_null_bytes(self) -> None:
        """Null bytes in string don't crash."""
        tc = TokenCounter()
        count = tc.count("hello\x00world")
        assert count > 0

    @given(
        text=st.text(
            alphabet=st.characters(
                whitelist_categories=("L", "M", "N", "P", "S", "Z"),
            ),
            min_size=0,
            max_size=1000,
        )
    )
    @settings(max_examples=100)
    def test_arbitrary_unicode_safe(self, text: str) -> None:
        """Arbitrary unicode text never crashes tokenizer."""
        tc = TokenCounter()
        count = tc.count(text)
        assert count >= 0
