"""Tests for :class:`TokenReconnectLimiter` and helpers."""

from __future__ import annotations

import asyncio

import pytest

from sovyx.voice.device_test._limiter import (
    NoopLimiter,
    TokenReconnectLimiter,
    acquire_for_token,
    hash_token,
)


class TestConstructor:
    """Guard clauses on :class:`TokenReconnectLimiter.__init__`."""

    def test_zero_limit_rejected(self) -> None:
        with pytest.raises(ValueError, match="limit must be > 0"):
            TokenReconnectLimiter(limit=0)

    def test_negative_limit_rejected(self) -> None:
        with pytest.raises(ValueError, match="limit must be > 0"):
            TokenReconnectLimiter(limit=-1)

    def test_zero_window_rejected(self) -> None:
        with pytest.raises(ValueError, match="window_seconds must be > 0"):
            TokenReconnectLimiter(limit=5, window_seconds=0)


class TestTryAcquire:
    """Core sliding-window semantics."""

    @pytest.mark.asyncio()
    async def test_under_limit_allows(self) -> None:
        limiter = TokenReconnectLimiter(limit=3, window_seconds=60)
        for _ in range(3):
            assert await limiter.try_acquire("tok") is True

    @pytest.mark.asyncio()
    async def test_over_limit_rejects(self) -> None:
        limiter = TokenReconnectLimiter(limit=2, window_seconds=60)
        assert await limiter.try_acquire("tok") is True
        assert await limiter.try_acquire("tok") is True
        assert await limiter.try_acquire("tok") is False

    @pytest.mark.asyncio()
    async def test_different_tokens_isolated(self) -> None:
        limiter = TokenReconnectLimiter(limit=1, window_seconds=60)
        assert await limiter.try_acquire("a") is True
        # Token "b" has its own budget.
        assert await limiter.try_acquire("b") is True
        # Token "a" is already at its cap.
        assert await limiter.try_acquire("a") is False

    @pytest.mark.asyncio()
    async def test_window_expires(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        limiter = TokenReconnectLimiter(limit=1, window_seconds=1)

        clock = {"t": 1_000.0}

        def fake_monotonic() -> float:
            return clock["t"]

        import sovyx.voice.device_test._limiter as mod

        monkeypatch.setattr(mod.time, "monotonic", fake_monotonic)

        assert await limiter.try_acquire("tok") is True
        assert await limiter.try_acquire("tok") is False

        # Advance past the window.
        clock["t"] += 2.0
        assert await limiter.try_acquire("tok") is True

    @pytest.mark.asyncio()
    async def test_current_count_tracks_acquires(self) -> None:
        limiter = TokenReconnectLimiter(limit=5, window_seconds=60)
        assert await limiter.current_count("tok") == 0
        await limiter.try_acquire("tok")
        await limiter.try_acquire("tok")
        assert await limiter.current_count("tok") == 2

    @pytest.mark.asyncio()
    async def test_reset_clears_token(self) -> None:
        limiter = TokenReconnectLimiter(limit=1, window_seconds=60)
        await limiter.try_acquire("tok")
        assert await limiter.try_acquire("tok") is False
        await limiter.reset("tok")
        assert await limiter.try_acquire("tok") is True


class TestHashToken:
    """:func:`hash_token` produces stable, non-reversible keys."""

    def test_deterministic(self) -> None:
        assert hash_token("hello") == hash_token("hello")

    def test_distinct_tokens_distinct_keys(self) -> None:
        assert hash_token("a") != hash_token("b")

    def test_key_is_hex_and_short(self) -> None:
        key = hash_token("anything")
        assert len(key) == 32  # 16 bytes × 2 hex chars
        # All lowercase hex digits.
        int(key, 16)


class TestAcquireForToken:
    """Convenience wrapper dispatches to hash_token or anon bucket."""

    @pytest.mark.asyncio()
    async def test_uses_hashed_key_for_token(self) -> None:
        limiter = TokenReconnectLimiter(limit=1, window_seconds=60)
        assert await acquire_for_token(limiter, "my-token") is True
        # Same token, now over cap.
        assert await acquire_for_token(limiter, "my-token") is False

    @pytest.mark.asyncio()
    async def test_anon_bucket_shared_for_none(self) -> None:
        limiter = TokenReconnectLimiter(limit=1, window_seconds=60)
        assert await acquire_for_token(limiter, None) is True
        assert await acquire_for_token(limiter, None) is False

    @pytest.mark.asyncio()
    async def test_hashed_and_anon_are_isolated(self) -> None:
        limiter = TokenReconnectLimiter(limit=1, window_seconds=60)
        assert await acquire_for_token(limiter, "tok") is True
        # Anon bucket has its own budget.
        assert await acquire_for_token(limiter, None) is True


class TestNoopLimiter:
    """:class:`NoopLimiter` always allows, never counts."""

    @pytest.mark.asyncio()
    async def test_try_acquire_always_true(self) -> None:
        limiter = NoopLimiter()
        for _ in range(100):
            assert await limiter.try_acquire("tok") is True

    @pytest.mark.asyncio()
    async def test_current_count_always_zero(self) -> None:
        limiter = NoopLimiter()
        await limiter.try_acquire("tok")
        assert await limiter.current_count("tok") == 0

    @pytest.mark.asyncio()
    async def test_reset_is_no_op(self) -> None:
        limiter = NoopLimiter()
        # Should not raise.
        await limiter.reset("tok")


class TestConcurrency:
    """Per-token locking prevents TOCTOU when acquiring."""

    @pytest.mark.asyncio()
    async def test_concurrent_acquires_respect_limit(self) -> None:
        limiter = TokenReconnectLimiter(limit=10, window_seconds=60)
        results = await asyncio.gather(
            *(limiter.try_acquire("tok") for _ in range(20)),
        )
        # Exactly ten should have succeeded.
        assert sum(results) == 10
