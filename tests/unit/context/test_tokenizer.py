"""Tests for sovyx.context.tokenizer — Token counter."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from sovyx.context.tokenizer import TokenCounter


@pytest.fixture
def counter() -> TokenCounter:
    return TokenCounter()


class TestCount:
    """Token counting."""

    def test_empty_string(self, counter: TokenCounter) -> None:
        assert counter.count("") == 0

    def test_single_word(self, counter: TokenCounter) -> None:
        count = counter.count("hello")
        assert count == 1

    def test_sentence(self, counter: TokenCounter) -> None:
        count = counter.count("The quick brown fox jumps over the lazy dog.")
        assert count > 5  # noqa: PLR2004

    def test_unicode(self, counter: TokenCounter) -> None:
        count = counter.count("こんにちは世界")
        assert count > 0

    def test_code(self, counter: TokenCounter) -> None:
        count = counter.count("def hello():\n    return 'world'")
        assert count > 5  # noqa: PLR2004

    def test_consistency(self, counter: TokenCounter) -> None:
        """Same input → same output."""
        text = "consistency test"
        assert counter.count(text) == counter.count(text)


class TestCountMessages:
    """Message list counting."""

    def test_empty_list(self, counter: TokenCounter) -> None:
        assert counter.count_messages([]) == 0

    def test_single_message(self, counter: TokenCounter) -> None:
        msgs = [{"role": "user", "content": "hello"}]
        count = counter.count_messages(msgs)
        # role + content tokens + 4 overhead
        assert count > 4  # noqa: PLR2004

    def test_multiple_messages(self, counter: TokenCounter) -> None:
        msgs = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello!"},
        ]
        count = counter.count_messages(msgs)
        # At least 3 messages × 4 overhead = 12
        assert count >= 12  # noqa: PLR2004

    def test_overhead_per_message(self, counter: TokenCounter) -> None:
        """Each message adds ~4 tokens overhead."""
        msgs1 = [{"role": "user", "content": "test"}]
        msgs2 = [
            {"role": "user", "content": "test"},
            {"role": "user", "content": "test"},
        ]
        diff = counter.count_messages(msgs2) - counter.count_messages(msgs1)
        # Second message adds content tokens + 4 overhead
        content_tokens = counter.count("user") + counter.count("test")
        assert diff == content_tokens + 4


class TestTruncate:
    """Text truncation."""

    def test_short_text_unchanged(self, counter: TokenCounter) -> None:
        text = "hello"
        assert counter.truncate(text, 100) == text

    def test_long_text_truncated(self, counter: TokenCounter) -> None:
        text = "word " * 100
        result = counter.truncate(text, 10)
        assert counter.count(result) <= 10

    def test_empty_text(self, counter: TokenCounter) -> None:
        assert counter.truncate("", 10) == ""

    def test_zero_tokens(self, counter: TokenCounter) -> None:
        assert counter.truncate("hello world", 0) == ""

    def test_exact_fit(self, counter: TokenCounter) -> None:
        text = "hello"
        tokens = counter.count(text)
        assert counter.truncate(text, tokens) == text

    def test_truncated_is_decodable(self, counter: TokenCounter) -> None:
        """Truncated text should be valid UTF-8."""
        text = "The quick brown fox " * 50
        result = counter.truncate(text, 20)
        assert isinstance(result, str)
        result.encode("utf-8")  # Should not raise


class TestFits:
    """Budget check."""

    def test_fits_under(self, counter: TokenCounter) -> None:
        assert counter.fits("hello", 100) is True

    def test_does_not_fit(self, counter: TokenCounter) -> None:
        text = "word " * 200
        assert counter.fits(text, 5) is False

    def test_exact_fit(self, counter: TokenCounter) -> None:
        text = "hello"
        tokens = counter.count(text)
        assert counter.fits(text, tokens) is True

    def test_empty_fits_anywhere(self, counter: TokenCounter) -> None:
        assert counter.fits("", 0) is True


class TestCaching:
    """Encoding caching."""

    def test_encoding_cached(self) -> None:
        c = TokenCounter()
        c.count("test")
        enc1 = c._encoding
        c.count("test again")
        enc2 = c._encoding
        assert enc1 is enc2


class TestPropertyBased:
    """Property-based tests."""

    @given(text=st.text(min_size=0, max_size=500))
    @settings(max_examples=30)
    def test_count_non_negative(self, text: str) -> None:
        """Token count is always ≥ 0."""
        c = TokenCounter()
        assert c.count(text) >= 0

    @given(
        text=st.text(min_size=1, max_size=200),
        max_tokens=st.integers(min_value=1, max_value=50),
    )
    @settings(max_examples=30)
    def test_truncate_respects_budget(self, text: str, max_tokens: int) -> None:
        """Truncated text always fits within budget."""
        c = TokenCounter()
        result = c.truncate(text, max_tokens)
        assert c.count(result) <= max_tokens

    @given(text=st.text(min_size=0, max_size=100))
    @settings(max_examples=20)
    def test_fits_consistent_with_count(self, text: str) -> None:
        """fits() agrees with count()."""
        c = TokenCounter()
        tokens = c.count(text)
        assert c.fits(text, tokens) is True
        if tokens > 0:
            assert c.fits(text, tokens - 1) is False
