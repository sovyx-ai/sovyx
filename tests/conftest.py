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
def _cleanup_async_resources(request: pytest.FixtureRequest) -> None:  # type: ignore[type-arg]
    """Clean up leaked aiosqlite threads between tests.

    Root cause of CI deadlock: aiosqlite spawns daemon threads
    (_connection_worker_thread) for each connection. If tests don't
    close connections, these threads accumulate and block the asyncio
    event loop selector (selector.select/poll), deadlocking any test
    that touches async — including sync CLI tests under asyncio_mode=auto,
    because pytest-asyncio creates an event loop for every test.

    Fix: after each test, force-stop any lingering aiosqlite threads
    by sending a stop sentinel through their queue.

    See: CI Deadlock Hunt mission, 2026-04-13.
    Traceback evidence: Thread-4132..4141 (_connection_worker_thread)
    blocked on tx.get() in aiosqlite/core.py:59.
    """
    import os
    import sys
    import threading

    if os.environ.get("CI"):
        print(f"\n>>> START {request.node.nodeid}", file=sys.stderr, flush=True)

    # Snapshot threads before test to know what's new
    threads_before = set(threading.enumerate())

    yield

    # Force GC first to trigger __del__ on abandoned connections
    gc.collect()

    # Find and stop leaked aiosqlite worker threads
    for thread in threading.enumerate():
        if thread not in threads_before and "_connection_worker" in (thread.name or ""):
            # aiosqlite workers block on self._tx.get().
            # The clean way to stop them is to put a stop sentinel.
            # But we don't have a reference to the Connection object.
            # Mark as daemon so they don't block process exit,
            # and rely on GC + the sentinel approach below.
            thread.daemon = True

    # More aggressive: find all aiosqlite Connection objects and close them
    try:
        import contextlib

        import aiosqlite.core

        for obj in gc.get_objects():
            if isinstance(obj, aiosqlite.core.Connection):
                with contextlib.suppress(Exception):
                    # Send stop sentinel to unblock the worker thread.
                    # aiosqlite worker loops on tx.get() → (future, function).
                    # When function() returns _STOP_RUNNING_SENTINEL, it breaks.
                    # We send (None, lambda: sentinel) to stop the thread cleanly.
                    _stop = aiosqlite.core._STOP_RUNNING_SENTINEL  # noqa: SLF001
                    obj._tx.put_nowait((None, lambda _s=_stop: _s))  # noqa: SLF001
    except (ImportError, TypeError):
        pass

    gc.collect()

    if os.environ.get("CI"):
        print(f"<<< END {request.node.nodeid}", file=sys.stderr, flush=True)


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
