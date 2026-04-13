"""Shared test fixtures for Sovyx test suite."""

from __future__ import annotations

import asyncio
import gc
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


@pytest.fixture(autouse=True)
def _cleanup_async_resources() -> None:
    """Force-close lingering event loops and threads between tests.

    Prevents deadlocks caused by leaked asyncio event loops or background
    threads (e.g., from watchdog, aiosqlite, or CLI runner.invoke) that
    survive between test modules and block pytest in CI.

    Root cause: test_main.py::TestWithDaemon leaves async resources that
    block the next test's collection on GitHub Actions runners (but not
    locally due to different cleanup timing).

    See: MISSION-CI-FIX, 2026-04-13.
    """
    yield
    # Force garbage collection to trigger __del__ on abandoned objects
    gc.collect()
    # Close any event loop that wasn't properly cleaned up
    try:
        loop = asyncio.get_event_loop_policy().get_event_loop()
        if not loop.is_closed() and not loop.is_running():
            # Cancel all pending tasks
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
    except (RuntimeError, AttributeError):
        pass
    # Reset the event loop policy to get a fresh loop next time
    asyncio.set_event_loop_policy(None)


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
