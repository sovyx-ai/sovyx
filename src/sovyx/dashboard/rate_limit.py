"""Lightweight in-memory rate limiter for the dashboard API.

Uses a sliding-window counter per client IP. No external dependencies.

Limits:
- GET endpoints: 120 req/min (generous for polling dashboards)
- POST/PUT/DELETE: 30 req/min
- /api/chat: 20 req/min (LLM calls are expensive)
- /api/export: 5 req/min (heavy I/O)

Headers returned:
- X-RateLimit-Limit: max requests in window
- X-RateLimit-Remaining: requests left
- X-RateLimit-Reset: seconds until window resets
"""

from __future__ import annotations

import time
from collections import defaultdict
from threading import Lock
from typing import TYPE_CHECKING

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response

# Window size in seconds
_WINDOW = 60

# Per-endpoint limits (requests per window)
_LIMITS: dict[str, int] = {
    "/api/chat": 20,
    "/api/export": 5,
    "/api/import": 10,
}

# Method-based defaults
_METHOD_DEFAULTS: dict[str, int] = {
    "GET": 120,
    "POST": 30,
    "PUT": 30,
    "PATCH": 30,
    "DELETE": 30,
}


class _SlidingWindow:
    """Thread-safe sliding window counter."""

    __slots__ = ("_hits", "_lock")

    def __init__(self) -> None:
        self._hits: list[float] = []
        self._lock = Lock()

    def hit(self, now: float, window: int) -> tuple[int, float]:
        """Record a hit, return (count_in_window, oldest_expiry)."""
        cutoff = now - window
        with self._lock:
            self._hits = [t for t in self._hits if t > cutoff]
            self._hits.append(now)
            count = len(self._hits)
            oldest = self._hits[0] if self._hits else now
        return count, oldest + window


# Global state: {client_key: SlidingWindow}
_buckets: dict[str, _SlidingWindow] = defaultdict(_SlidingWindow)
_cleanup_lock = Lock()
_last_cleanup = 0.0


def _cleanup_stale(now: float) -> None:
    """Periodically prune stale buckets (every 5 minutes)."""
    global _last_cleanup  # noqa: PLW0603
    if now - _last_cleanup < 300:
        return
    with _cleanup_lock:
        if now - _last_cleanup < 300:
            return
        _last_cleanup = now
        stale = [k for k, v in _buckets.items() if not v._hits or v._hits[-1] < now - _WINDOW * 2]
        for k in stale:
            del _buckets[k]


def _get_client_ip(request: Request) -> str:
    """Extract client IP, respecting X-Forwarded-For behind reverse proxy."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _get_limit(path: str, method: str) -> int:
    """Determine rate limit for a given path and method."""
    # Exact path match first
    if path in _LIMITS:
        return _LIMITS[path]
    # Method default
    return _METHOD_DEFAULTS.get(method, 60)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Sliding-window rate limiter middleware.

    Applies per-IP rate limits with informative headers.
    Skips non-API paths (static assets, health checks).
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path
        method = request.method

        # Only rate-limit /api/ paths
        if not path.startswith("/api/"):
            return await call_next(request)

        # Skip WebSocket upgrades
        if request.headers.get("upgrade", "").lower() == "websocket":
            return await call_next(request)

        client_ip = _get_client_ip(request)
        limit = _get_limit(path, method)
        bucket_key = f"{client_ip}:{path}"

        now = time.monotonic()
        _cleanup_stale(now)

        count, reset_at = _buckets[bucket_key].hit(now, _WINDOW)
        remaining = max(0, limit - count)
        reset_seconds = max(0, int(reset_at - now))

        # Rate limit headers (always)
        headers = {
            "X-RateLimit-Limit": str(limit),
            "X-RateLimit-Remaining": str(remaining),
            "X-RateLimit-Reset": str(reset_seconds),
        }

        if count > limit:
            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded. Try again later."},
                headers={**headers, "Retry-After": str(reset_seconds)},
            )

        response = await call_next(request)

        # Inject headers into successful responses
        for k, v in headers.items():
            response.headers[k] = v

        return response
