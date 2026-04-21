"""Hot-path call counters + periodic snapshot — ``@counted`` + ``HotPathSnapshotter``.

The combination is a low-overhead profiler for "which functions ran a
lot in the last minute?" without paying the cost of full ``cProfile``
or per-call latency tracking. Decorate a hot-path function with
:func:`counted` to increment a thread-safe counter every time it is
called; the :class:`HotPathSnapshotter` wakes every
``perf_hotpath_interval_seconds`` (default 60 s — same knob as
:mod:`sovyx.observability.resources`), emits a
``perf.hotpath.snapshot`` log carrying the top-N busiest functions for
the window, and resets the counters atomically so the next window
starts from zero.

The counter store is a module-level dict guarded by
``threading.Lock``: the same decorator can be applied to sync, async,
or thread-bound functions and still produce correct counts when the
event loop, executor pool, and main thread all touch it. Increment
cost is O(1) lock acquire + dict insert / counter bump — designed to
sit on functions that already do non-trivial work (HTTP request
dispatch, brain queries, LLM calls). Don't decorate microsecond-scale
functions with this.

The snapshotter is intended to be wired in bootstrap (Phase 6 Task
6.8) via :func:`sovyx.observability.tasks.spawn`, sharing the
``async_queue`` feature flag with :class:`ResourceSnapshotter`.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import inspect
import threading
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, TypeVar, cast

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from sovyx.engine.config import ObservabilityConfig

logger = get_logger(__name__)


_F = TypeVar("_F", bound=Callable[..., Any])

# Default number of entries returned by ``snapshot_top_n`` and emitted
# by the snapshotter — a 10-deep top-K is enough to spot the dominant
# hot paths without exploding log payload size.
_DEFAULT_TOP_N: int = 10


class _CounterRegistry:
    """Thread-safe counter store keyed by ``module.qualname``.

    Counters live for the duration of the process. ``snapshot_top_n``
    returns the top-N entries by count and atomically resets every
    counter so the next window starts at zero — this matches the
    "rate per window" semantic the snapshotter logs.
    """

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}
        self._lock = threading.Lock()

    def incr(self, name: str) -> None:
        """Increment the named counter by one."""
        with self._lock:
            self._counts[name] = self._counts.get(name, 0) + 1

    def snapshot_top_n(
        self,
        *,
        top_n: int = _DEFAULT_TOP_N,
        reset: bool = True,
    ) -> tuple[list[tuple[str, int]], int]:
        """Return ``(top_entries, total_calls)`` and optionally reset.

        ``top_entries`` is a list of ``(name, count)`` pairs sorted by
        count descending, length capped at ``top_n``. ``total_calls``
        is the sum across **all** counters in the window — useful for
        spotting "lots of activity in a long tail" cases.
        """
        with self._lock:
            total = sum(self._counts.values())
            ordered = sorted(self._counts.items(), key=lambda kv: kv[1], reverse=True)
            top = ordered[:top_n]
            if reset:
                self._counts.clear()
        return top, total

    def known_keys(self) -> list[str]:
        """Return a snapshot of currently tracked counter names (test helper)."""
        with self._lock:
            return list(self._counts.keys())

    def clear(self) -> None:
        """Drop every counter — used by tests to start clean."""
        with self._lock:
            self._counts.clear()


_REGISTRY: _CounterRegistry = _CounterRegistry()


def get_registry() -> _CounterRegistry:
    """Return the process-global counter registry.

    Exposed so tests and ad-hoc inspection can read counters without
    going through the snapshotter loop.
    """
    return _REGISTRY


def _qualified_name(fn: Callable[..., Any]) -> str:
    """Return ``module.qualname`` for *fn*, falling back gracefully."""
    module = getattr(fn, "__module__", None) or "<unknown>"
    qual = getattr(fn, "__qualname__", None) or getattr(fn, "__name__", "<anonymous>")
    return f"{module}.{qual}"


def counted(fn: _F) -> _F:
    """Decorate *fn* so every call increments a hot-path counter.

    Works on both sync and async callables; the wrapper preserves
    ``functools.wraps`` metadata so introspection (``inspect.iscoroutinefunction``,
    ``__wrapped__``, signature) keeps working. The increment runs
    *before* the wrapped function — failures in the function don't
    cause counts to be lost, which is the correct behaviour for a
    "how many attempts did this hot path see" metric.
    """
    name = _qualified_name(fn)

    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def async_wrapper(*args: object, **kwargs: object) -> object:
            _REGISTRY.incr(name)
            return await cast("Callable[..., Awaitable[object]]", fn)(*args, **kwargs)

        return cast("_F", async_wrapper)

    @functools.wraps(fn)
    def sync_wrapper(*args: object, **kwargs: object) -> object:
        _REGISTRY.incr(name)
        return fn(*args, **kwargs)

    return cast("_F", sync_wrapper)


class HotPathSnapshotter:
    """Periodically emit ``perf.hotpath.snapshot`` with top-N busiest functions.

    Wire it from bootstrap (Phase 6 Task 6.8) when
    :attr:`ObservabilityFeaturesConfig.async_queue` is enabled. Stop it
    during shutdown by cancelling the spawned task or calling
    :meth:`stop`.

    Args:
        observability_config: Active :class:`ObservabilityConfig`. The
            interval is read from
            ``observability_config.sampling.perf_hotpath_interval_seconds``.
        top_n: Maximum entries per snapshot (default 10).
    """

    def __init__(
        self,
        observability_config: ObservabilityConfig,
        *,
        top_n: int = _DEFAULT_TOP_N,
    ) -> None:
        self._config = observability_config
        self._top_n = top_n
        self._stop_event = asyncio.Event()
        self._window_started_at: float | None = None

    def stop(self) -> None:
        """Signal the loop to exit on its next wake-up."""
        self._stop_event.set()

    async def run(self) -> None:
        """Background loop body — call via ``spawn()``.

        Wakes every ``perf_hotpath_interval_seconds``, takes a
        top-N snapshot, emits ``perf.hotpath.snapshot``, and resets
        the counters for the next window. Honours cancellation by
        emitting one final snapshot before re-raising.
        """
        interval = max(1, int(self._config.sampling.perf_hotpath_interval_seconds))
        self._window_started_at = time.monotonic()
        logger.info(
            "perf.hotpath.snapshotter_started",
            **{"perf.hotpath.interval_seconds": interval, "perf.hotpath.top_n": self._top_n},
        )
        try:
            while not self._stop_event.is_set():
                # Wait first, then snapshot — gives the first window a
                # full ``interval`` worth of data instead of an empty
                # snapshot at second 0.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
                if self._stop_event.is_set():
                    break
                self._emit_snapshot(final=False)
        except asyncio.CancelledError:
            self._emit_snapshot(final=True)
            raise
        else:
            self._emit_snapshot(final=True)
        finally:
            logger.info("perf.hotpath.snapshotter_stopped")

    def _emit_snapshot(self, *, final: bool) -> None:
        """Pull a top-N snapshot, log it, advance the window."""
        top, total = _REGISTRY.snapshot_top_n(top_n=self._top_n, reset=True)
        now = time.monotonic()
        window_seconds: float | None = None
        if self._window_started_at is not None:
            window_seconds = round(now - self._window_started_at, 3)
        self._window_started_at = now

        logger.info(
            "perf.hotpath.snapshot",
            **{
                "perf.hotpath.window_seconds": window_seconds,
                "perf.hotpath.total_calls": total,
                "perf.hotpath.top_n": self._top_n,
                "perf.hotpath.entries": [{"name": name, "count": count} for name, count in top],
                "perf.hotpath.snapshot_final": final,
            },
        )
