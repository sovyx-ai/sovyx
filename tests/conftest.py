"""Shared test fixtures for Sovyx test suite."""

from __future__ import annotations

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
    """Clean up leaked aiosqlite threads between tests.

    Root cause of CI deadlock: aiosqlite spawns daemon threads
    (_connection_worker_thread) for each database connection. Tests that
    don't properly close connections leave these threads alive, blocking
    the asyncio selector when pytest-asyncio creates a new event loop
    for the next test. This causes deadlocks only in CI (slower timing).

    Fix: after each test, find ALL aiosqlite worker threads and force-stop
    them by sending the stop sentinel through their internal queues.

    See: CI Deadlock Hunt mission, 2026-04-13.
    """
    import threading

    yield

    gc.collect()

    # Kill ALL aiosqlite worker threads — not just new ones.
    # The threads from tests 100+ tests ago can still be alive.
    try:
        import contextlib

        import aiosqlite.core

        _stop = aiosqlite.core._STOP_RUNNING_SENTINEL  # noqa: SLF001

        # Approach 1: send stop sentinel to all Connection objects
        for obj in gc.get_objects():
            if isinstance(obj, aiosqlite.core.Connection):
                with contextlib.suppress(Exception):
                    obj._tx.put_nowait((None, lambda _s=_stop: _s))  # noqa: SLF001

        # Approach 2: for threads that survived (Connection already GC'd
        # but thread still alive), we can't reach their queue directly.
        # Mark them daemon and force-join with timeout.
        for thread in threading.enumerate():
            if "_connection_worker" in (thread.name or ""):
                thread.daemon = True
                with contextlib.suppress(Exception):
                    thread.join(timeout=0.1)
    except ImportError:
        pass

    gc.collect()


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
