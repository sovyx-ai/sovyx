"""Sovyx token counter — precise token counting for context budget.

Uses tiktoken (cl100k_base) compatible with Claude/GPT models.
Encoding lazy-loaded and cached. Thread-safe.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import tiktoken

if TYPE_CHECKING:
    from collections.abc import Sequence

from sovyx.observability.logging import get_logger

logger = get_logger(__name__)

# Overhead per message in chat format (~4 tokens)
_MESSAGE_OVERHEAD = 4


class TokenCounter:
    """Count tokens using tiktoken (cl100k_base).

    Cache: encoding lazy-loaded and cached on first use.
    Thread-safe: tiktoken is thread-safe.

    Note (ADR-008 "zero internet boot"):
        tiktoken downloads encoding (~1.7MB) on first call if cache empty.
        `sovyx init` (TASK-043) pre-caches during setup.
    """

    def __init__(self, encoding_name: str = "cl100k_base") -> None:
        self._encoding_name = encoding_name
        self._encoding: tiktoken.Encoding | None = None

    def _get_encoding(self) -> tiktoken.Encoding:
        """Lazy-load and cache encoding."""
        if self._encoding is None:
            self._encoding = tiktoken.get_encoding(self._encoding_name)
        return self._encoding

    def count(self, text: str) -> int:
        """Count tokens in text.

        Args:
            text: Input text.

        Returns:
            Number of tokens.
        """
        if not text:
            return 0
        return len(self._get_encoding().encode(text))

    def count_messages(
        self, messages: Sequence[dict[str, str]]
    ) -> int:
        """Count tokens in a list of messages (OpenAI/Anthropic format).

        Includes overhead of ~4 tokens per message for formatting.

        Args:
            messages: List of {"role": "...", "content": "..."} dicts.

        Returns:
            Total token count including overhead.
        """
        total = 0
        for msg in messages:
            total += _MESSAGE_OVERHEAD
            for value in msg.values():
                total += self.count(value)
        return total

    def truncate(self, text: str, max_tokens: int) -> str:
        """Truncate text to fit within max_tokens.

        Tries to avoid cutting words in the middle by truncating
        at token boundaries (tiktoken tokens align well with words).

        Args:
            text: Input text.
            max_tokens: Maximum token budget.

        Returns:
            Truncated text fitting within budget.
        """
        if not text or max_tokens <= 0:
            return ""
        enc = self._get_encoding()
        tokens = enc.encode(text)
        if len(tokens) <= max_tokens:
            return text
        truncated = enc.decode(tokens[:max_tokens])
        return truncated

    def fits(self, text: str, max_tokens: int) -> bool:
        """Check if text fits within max_tokens.

        Args:
            text: Input text.
            max_tokens: Token budget.

        Returns:
            True if text fits.
        """
        return self.count(text) <= max_tokens
