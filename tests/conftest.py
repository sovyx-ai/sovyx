"""Shared test fixtures for Sovyx test suite."""

from __future__ import annotations

import gc
import threading
import warnings
from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, settings

if TYPE_CHECKING:
    from pathlib import Path

# ── Hypothesis global profile ──────────────────────────────────────────────
# deadline=None prevents flaky failures when the host is under load
# (e.g., CI with parallel workers, low-RAM environments).  The main
# offender is tiktoken's lazy encoding load (>200 ms on first call),
# but any I/O-touching property test can hit the default 200 ms limit.
# suppress_health_check includes too_slow so Hypothesis doesn't
# abort on legitimately expensive strategies (large unicode texts).
settings.register_profile(
    "sovyx",
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.register_profile(
    "ci",
    deadline=None,
    max_examples=30,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.load_profile("sovyx")


def _count_aiosqlite_threads() -> int:
    """Count active aiosqlite worker threads."""
    return sum(
        1 for t in threading.enumerate() if t.is_alive() and "aiosqlite" in type(t).__module__
    )


@pytest.fixture(autouse=True)
def _cleanup_async_resources():
    """Detect and clean up leaked aiosqlite threads between tests.

    aiosqlite creates a background thread per connection. If a test
    (or fixture) opens a connection without closing it, the thread
    survives and can deadlock subsequent tests by polluting the
    asyncio selector.

    This fixture:
      1. Counts aiosqlite threads before the test
      2. After the test, runs gc.collect() to trigger finalizers
      3. Warns if new threads leaked (so we can fix the source)

    Does NOT touch event loops or policies — pytest-asyncio >= 1.2
    manages those correctly, including the #1177 fix for asyncio.run().
    """
    before = _count_aiosqlite_threads()
    yield
    gc.collect()
    after = _count_aiosqlite_threads()
    leaked = after - before
    if leaked > 0:
        warnings.warn(
            f"Test leaked {leaked} aiosqlite worker thread(s). "
            "Ensure all DatabasePool/aiosqlite connections are closed "
            "in fixture teardown (use yield + close, not return).",
            ResourceWarning,
            stacklevel=2,
        )


@pytest.fixture(autouse=True)
def _clear_rate_limiter() -> None:
    """Reset module-level rate limiter between tests.

    The RateLimitMiddleware uses module-level ``_buckets`` shared across
    all TestClient instances.  Without clearing, cumulative requests
    across tests hit the limit and return 429 unexpectedly.
    """
    try:
        from sovyx.dashboard.rate_limit import _buckets

        _buckets.clear()
    except ImportError:
        pass


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """Temporary data directory for test isolation."""
    d = tmp_path / "sovyx-data"
    d.mkdir()
    return d


@pytest.fixture
def mind_dir(data_dir: Path) -> Path:
    """Temporary mind directory."""
    d = data_dir / "minds" / "test-mind"
    d.mkdir(parents=True)
    return d
