"""Tests for the rate limiting middleware."""

from __future__ import annotations

import time

from sovyx.dashboard.rate_limit import (
    RateLimitMiddleware,
    _buckets,
    _get_client_ip,
    _get_limit,
    _SlidingWindow,
)


class TestSlidingWindow:
    """Unit tests for the sliding window counter."""

    def test_single_hit(self) -> None:
        w = _SlidingWindow()
        count, _ = w.hit(time.monotonic(), 60)
        assert count == 1

    def test_multiple_hits(self) -> None:
        w = _SlidingWindow()
        now = time.monotonic()
        for _ in range(5):
            w.hit(now, 60)
        count, _ = w.hit(now, 60)
        assert count == 6

    def test_expired_hits_pruned(self) -> None:
        w = _SlidingWindow()
        old = time.monotonic() - 120  # 2 minutes ago
        w.hit(old, 60)
        count, _ = w.hit(time.monotonic(), 60)
        assert count == 1  # old hit pruned


class TestGetLimit:
    """Test limit resolution logic."""

    def test_chat_endpoint(self) -> None:
        assert _get_limit("/api/chat", "POST") == 20

    def test_export_endpoint(self) -> None:
        assert _get_limit("/api/export", "GET") == 5

    def test_import_endpoint(self) -> None:
        assert _get_limit("/api/import", "POST") == 10

    def test_default_get(self) -> None:
        assert _get_limit("/api/status", "GET") == 120

    def test_default_post(self) -> None:
        assert _get_limit("/api/settings", "POST") == 30

    def test_default_put(self) -> None:
        assert _get_limit("/api/config", "PUT") == 30


class TestGetClientIp:
    """Test client IP extraction."""

    def test_direct_connection(self) -> None:

        class FakeClient:
            host = "192.168.1.1"

        class FakeRequest:
            headers: dict[str, str] = {}
            client = FakeClient()

        assert _get_client_ip(FakeRequest()) == "192.168.1.1"  # type: ignore[arg-type]

    def test_forwarded_header(self) -> None:

        class FakeClient:
            host = "127.0.0.1"

        class FakeRequest:
            headers = {"x-forwarded-for": "10.0.0.1, 172.16.0.1"}
            client = FakeClient()

        assert _get_client_ip(FakeRequest()) == "10.0.0.1"  # type: ignore[arg-type]


class TestRateLimitMiddleware:
    """Integration-style tests for the middleware class."""

    def test_class_exists(self) -> None:
        assert RateLimitMiddleware is not None

    def test_get_or_create_bucket_returns_sliding_window(self) -> None:
        # Bucket dict is now a bounded LRU OrderedDict — accessor lives in
        # ``_get_or_create_bucket`` which mirrors LRULockDict.setdefault.
        from sovyx.dashboard.rate_limit import _get_or_create_bucket

        key = f"test-{time.monotonic()}"
        window = _get_or_create_bucket(key)
        assert isinstance(window, _SlidingWindow)
        # Same key returns same bucket (MRU promotion, no duplicate).
        assert _get_or_create_bucket(key) is window
        # Cleanup
        del _buckets[key]

    def test_buckets_evicts_oldest_at_capacity(self) -> None:
        """Eviction-on-insert keeps ``_buckets`` bounded."""
        from sovyx.dashboard.rate_limit import _MAX_BUCKETS, _get_or_create_bucket

        # Ensure clean slate for the slice of keys this test owns.
        prefix = f"evict-{time.monotonic()}"
        try:
            # Fill up to (but not over) the cap with our prefix,
            # marking the first one so we can detect its eviction.
            first_key = f"{prefix}-first"
            first = _get_or_create_bucket(first_key)
            assert first_key in _buckets

            # Now fill enough to push past the cap.
            for i in range(_MAX_BUCKETS + 5):
                _get_or_create_bucket(f"{prefix}-{i}")

            # The first key must have been evicted (no longer present).
            assert first_key not in _buckets
            # Dict size never exceeds the cap.
            assert len(_buckets) <= _MAX_BUCKETS
        finally:
            # Clean up our prefix to avoid polluting other tests.
            for k in [k for k in list(_buckets) if k.startswith(prefix)]:
                del _buckets[k]
