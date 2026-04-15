"""Tests for the source_hash dedup helper."""

from __future__ import annotations

from sovyx.upgrade.conv_import._hash import source_hash


class TestSourceHash:
    """Properties that must hold for the dedup key."""

    def test_is_stable(self) -> None:
        """Same inputs → same hash across calls."""
        h1 = source_hash("chatgpt", "abc-123")
        h2 = source_hash("chatgpt", "abc-123")
        assert h1 == h2

    def test_returns_hex_digest(self) -> None:
        """Output is a lowercase 64-char hex string."""
        h = source_hash("chatgpt", "abc")
        assert len(h) == 64  # noqa: PLR2004
        assert all(c in "0123456789abcdef" for c in h)

    def test_platform_changes_hash(self) -> None:
        """Same conversation_id but different platform → different hash."""
        assert source_hash("chatgpt", "abc") != source_hash("claude", "abc")

    def test_conversation_id_changes_hash(self) -> None:
        """Same platform, different conversation_id → different hash."""
        assert source_hash("chatgpt", "a") != source_hash("chatgpt", "b")

    def test_handles_unicode(self) -> None:
        """Non-ASCII conversation IDs still produce valid hashes."""
        h = source_hash("chatgpt", "id-ümlaut-🎉")
        assert len(h) == 64  # noqa: PLR2004
