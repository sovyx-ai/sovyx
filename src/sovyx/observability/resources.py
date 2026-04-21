"""Periodic process-health snapshots — RSS, CPU, threads, fds, queues.

Background snapshotter that emits a structured ``self.health.snapshot``
record at a configurable interval (default 60 s — read from
:attr:`ObservabilitySamplingConfig.perf_hotpath_interval_seconds`). The
snapshot bundles process resource usage and async-loop pressure so a
single line in the log stream describes the daemon's overall load
without having to cross-reference multiple sources.

The snapshotter is started during bootstrap (Phase 6 Task 6.8) via
:func:`sovyx.observability.tasks.spawn` so it inherits the project's
task-tracking discipline; cancellation during shutdown is honoured.

``psutil`` is optional. When it is missing, the snapshotter still emits
asyncio-loop metrics (task counts) so operators don't lose all
observability — only the OS-level fields go to ``None`` and a one-time
WARNING ``self.health.psutil_missing`` flags the gap.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import time
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, NamedTuple

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from sovyx.engine.config import ObservabilityConfig

logger = get_logger(__name__)


_PSUTIL_WARNED: bool = False


class QueueSnapshot(NamedTuple):
    """Single named queue's current depth and capacity at snapshot time."""

    name: str
    depth: int
    maxsize: int | None


# Provider returns the live (depth, maxsize) tuple at call time. Maxsize
# may be ``None`` for unbounded queues. Providers must be cheap and
# non-blocking — they are called from the snapshotter loop.
QueueProvider = Callable[[], tuple[int, int | None]]


def _capture_psutil_metrics() -> dict[str, object]:
    """Return ``psutil``-derived process metrics, or ``None`` fields on miss.

    Emits a one-time WARNING when ``psutil`` cannot be imported so the
    dependency gap is visible in the log stream without spamming every
    snapshot tick.
    """
    global _PSUTIL_WARNED  # noqa: PLW0603 — module-level latch for "warn once".
    try:
        import psutil
    except ImportError:
        if not _PSUTIL_WARNED:
            _PSUTIL_WARNED = True
            logger.warning(
                "self.health.psutil_missing",
                **{"self.health.reason": "psutil unavailable; OS metrics dropped"},
            )
        return {
            "process.rss_bytes": None,
            "process.vms_bytes": None,
            "process.cpu_percent": None,
            "process.num_threads": None,
            "process.num_handles_or_fds": None,
            "process.open_files_count": None,
            "process.connections_count": None,
        }

    proc = psutil.Process()
    # cpu_percent() with interval=None returns the value since the last
    # call; the snapshotter's first tick will report 0.0, subsequent
    # ticks return a meaningful delta. We accept that tradeoff to keep
    # the snapshot non-blocking.
    try:
        cpu_percent = proc.cpu_percent(interval=None)
    except Exception:  # noqa: BLE001 — psutil can raise NoSuchProcess on edge cases.
        cpu_percent = None

    try:
        mem = proc.memory_info()
        rss_bytes: int | None = int(mem.rss)
        vms_bytes: int | None = int(mem.vms)
    except Exception:  # noqa: BLE001
        rss_bytes = None
        vms_bytes = None

    try:
        num_threads: int | None = int(proc.num_threads())
    except Exception:  # noqa: BLE001
        num_threads = None

    # File descriptor count is platform-specific. Windows exposes
    # ``num_handles``; POSIX exposes ``num_fds``. Probe both so a
    # snapshot always has *something* meaningful in this slot.
    handles_or_fds: int | None
    try:
        if sys.platform == "win32":
            handles_or_fds = int(proc.num_handles())
        else:
            handles_or_fds = int(proc.num_fds())
    except Exception:  # noqa: BLE001
        handles_or_fds = None

    # ``open_files()`` and ``connections()`` can be expensive on
    # Windows (each call enumerates the kernel handle table). Wrap in
    # try/except and accept ``None`` if the OS denies access — the
    # snapshot is best-effort, not a forensic capture.
    try:
        open_files_count: int | None = len(proc.open_files())
    except Exception:  # noqa: BLE001
        open_files_count = None
    try:
        connections_count: int | None = len(proc.net_connections(kind="inet"))
    except Exception:  # noqa: BLE001
        connections_count = None

    return {
        "process.rss_bytes": rss_bytes,
        "process.vms_bytes": vms_bytes,
        "process.cpu_percent": cpu_percent,
        "process.num_threads": num_threads,
        "process.num_handles_or_fds": handles_or_fds,
        "process.open_files_count": open_files_count,
        "process.connections_count": connections_count,
    }


def _capture_asyncio_metrics() -> dict[str, object]:
    """Return current event-loop task counts.

    ``asyncio.all_tasks()`` requires a running loop; if called outside
    one, fall back to zeros rather than raising — the snapshotter loop
    itself is async, so this branch only triggers in test fixtures
    that import the helper directly.
    """
    try:
        tasks = asyncio.all_tasks()
    except RuntimeError:
        return {
            "asyncio.task_count": 0,
            "asyncio.running_count": 0,
            "asyncio.pending_count": 0,
        }
    running = sum(1 for t in tasks if not t.done())
    pending = sum(1 for t in tasks if not t.done() and not _is_currently_running(t))
    return {
        "asyncio.task_count": len(tasks),
        "asyncio.running_count": running,
        "asyncio.pending_count": pending,
    }


def _is_currently_running(task: asyncio.Task[object]) -> bool:
    """Best-effort check for whether *task* is mid-step on the loop.

    asyncio doesn't expose this directly; we treat the *current* task
    as "running" and everything else with ``done()`` false as
    "pending" (i.e. awaiting something). This is good enough for a
    coarse load metric.
    """
    try:
        return task is asyncio.current_task()
    except RuntimeError:
        return False


def _capture_queue_metrics(
    providers: Iterable[tuple[str, QueueProvider]],
) -> list[QueueSnapshot]:
    """Drain every registered queue provider into a list of snapshots.

    Providers that raise are logged at DEBUG (so a flaky source doesn't
    poison the whole snapshot) and skipped.
    """
    out: list[QueueSnapshot] = []
    for name, provider in providers:
        try:
            depth, maxsize = provider()
        except Exception:  # noqa: BLE001 — providers must never break the snapshot.
            logger.debug(
                "self.health.queue_provider_failed",
                **{"queue.name": name},
                exc_info=True,
            )
            continue
        out.append(QueueSnapshot(name=name, depth=int(depth), maxsize=maxsize))
    return out


class ResourceSnapshotter:
    """Periodically emit ``self.health.snapshot`` with process + loop metrics.

    Wire it from bootstrap (Phase 6 Task 6.8) when
    :attr:`ObservabilityFeaturesConfig.async_queue` is enabled. Stop it
    during shutdown by cancelling the task returned by
    :meth:`spawn`-ing :meth:`run`.

    Args:
        observability_config: The active :class:`ObservabilityConfig`.
            The interval is read from
            ``observability_config.sampling.perf_hotpath_interval_seconds``.
        queue_providers: Optional iterable of ``(name, provider)`` tuples.
            Each provider returns the live ``(depth, maxsize)`` of a
            named queue. Cheap, synchronous, must not block.
    """

    def __init__(
        self,
        observability_config: ObservabilityConfig,
        queue_providers: Iterable[tuple[str, QueueProvider]] | None = None,
    ) -> None:
        self._config = observability_config
        self._providers: list[tuple[str, QueueProvider]] = list(queue_providers or [])
        self._stop_event = asyncio.Event()
        self._started_at: float | None = None

    def register_queue(self, name: str, provider: QueueProvider) -> None:
        """Add a queue provider after construction.

        Useful when subsystems with their own lifecycle (audio capture,
        output queue) come up after the snapshotter has already been
        started — they can register their queue without restarting the
        loop.
        """
        self._providers.append((name, provider))

    def stop(self) -> None:
        """Signal the loop to exit on its next wake-up."""
        self._stop_event.set()

    async def run(self) -> None:
        """Background loop body — call via ``spawn()``.

        Wakes every ``perf_hotpath_interval_seconds`` (configurable),
        captures a snapshot, and emits ``self.health.snapshot``.
        Cancellation triggers a final snapshot tagged
        ``self.health.snapshot_final=True`` so a graceful shutdown
        leaves a closing line in the log.
        """
        interval = max(1, int(self._config.sampling.perf_hotpath_interval_seconds))
        self._started_at = time.monotonic()
        logger.info(
            "self.health.snapshotter_started",
            **{"self.health.interval_seconds": interval},
        )
        try:
            while not self._stop_event.is_set():
                self._emit_snapshot(final=False)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
        except asyncio.CancelledError:
            self._emit_snapshot(final=True)
            raise
        else:
            self._emit_snapshot(final=True)
        finally:
            logger.info("self.health.snapshotter_stopped")

    def _emit_snapshot(self, *, final: bool) -> None:
        """Capture and emit a single snapshot record.

        Failures inside the capture helpers are absorbed there; this
        function only fails if the structured logger itself raises,
        which is treated as a bug worth surfacing.
        """
        psutil_metrics = _capture_psutil_metrics()
        asyncio_metrics = _capture_asyncio_metrics()
        queues = _capture_queue_metrics(self._providers)

        uptime_s: float | None = None
        if self._started_at is not None:
            uptime_s = round(time.monotonic() - self._started_at, 3)

        payload: dict[str, object] = {
            "self.health.snapshot_final": final,
            "self.health.uptime_s": uptime_s,
            **psutil_metrics,
            **asyncio_metrics,
            "self.health.queue_count": len(queues),
            "self.health.queues": [
                {
                    "name": q.name,
                    "depth": q.depth,
                    "maxsize": q.maxsize,
                }
                for q in queues
            ],
        }
        logger.info("self.health.snapshot", **payload)
