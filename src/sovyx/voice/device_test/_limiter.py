"""Per-token reconnect limiter for the voice device test WS.

The global :mod:`sovyx.dashboard.rate_limit` middleware explicitly skips
WebSocket upgrades (see ``RateLimitMiddleware.dispatch``), so every WS
endpoint must enforce its own reconnect budget. Otherwise a broken
client (or malicious one) can churn open/close on a PortAudio device
and destabilise the host audio stack.

This limiter is a simple sliding window counter keyed by auth token.
Unlike the HTTP limiter it's keyed by token (not IP) because tokens
are the unit of trust on the dashboard API.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque

from sovyx.engine._lock_dict import LRULockDict


class TokenReconnectLimiter:
    """Sliding-window reconnect limiter keyed by auth token."""

    def __init__(self, *, limit: int, window_seconds: int = 60) -> None:
        if limit <= 0:
            msg = "limit must be > 0"
            raise ValueError(msg)
        if window_seconds <= 0:
            msg = "window_seconds must be > 0"
            raise ValueError(msg)
        self._limit = limit
        self._window_seconds = window_seconds
        self._events: dict[str, deque[float]] = {}
        self._locks: LRULockDict[str] = LRULockDict(maxsize=4_096)

    async def try_acquire(self, token_key: str) -> bool:
        """Return True if under limit (slot reserved), False if over."""
        async with self._locks[token_key]:
            now = time.monotonic()
            dq = self._events.setdefault(token_key, deque())
            # Drop events outside the window.
            threshold = now - self._window_seconds
            while dq and dq[0] < threshold:
                dq.popleft()
            if len(dq) >= self._limit:
                return False
            dq.append(now)
            return True

    async def current_count(self, token_key: str) -> int:
        async with self._locks[token_key]:
            now = time.monotonic()
            dq = self._events.get(token_key)
            if not dq:
                return 0
            threshold = now - self._window_seconds
            while dq and dq[0] < threshold:
                dq.popleft()
            return len(dq)

    async def reset(self, token_key: str) -> None:
        async with self._locks[token_key]:
            self._events.pop(token_key, None)


def hash_token(token: str) -> str:
    """Derive a short non-reversible key from a bearer token.

    We never want the raw token used as a dict key (it would end up in
    logs, memory dumps, and crash reports). SHA-256 truncated to 16
    bytes is indistinguishable from random for our purposes.
    """
    import hashlib

    digest = hashlib.sha256(token.encode("utf-8")).digest()
    return digest[:16].hex()


# Sentinel returned by :meth:`TokenReconnectLimiter.try_acquire` when
# called without a valid token — kept here so tests can patch freely.
_ANON_KEY = "anonymous"


async def acquire_for_token(
    limiter: TokenReconnectLimiter,
    token: str | None,
) -> bool:
    """Convenience: hash the token if present, else use the anon bucket."""
    key = hash_token(token) if token else _ANON_KEY
    return await limiter.try_acquire(key)


# Stub used by tests that don't care about limiting but still need an
# interface-compatible instance.
class NoopLimiter:
    """Always-allow limiter for environments where rate-limiting is off."""

    async def try_acquire(self, token_key: str) -> bool:  # noqa: ARG002
        return True

    async def current_count(self, token_key: str) -> int:  # noqa: ARG002
        return 0

    async def reset(self, token_key: str) -> None:  # noqa: ARG002
        await asyncio.sleep(0)
