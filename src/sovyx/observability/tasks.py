"""Sovyx observability — asyncio task spawning with lifecycle telemetry.

Wraps :func:`asyncio.create_task` so every fire-and-forget background
task in the daemon publishes a structured ``task.spawned`` →
``task.completed`` / ``task.failed`` / ``task.cancelled`` lifecycle
record. Each task gets:

* a short ``task_id`` (8 hex chars from ``uuid4``) bound to
  ``structlog.contextvars`` for the duration of the coroutine, so any
  log emitted from inside the spawned task automatically carries it;
* an auto-detected ``owner`` string (``file:line:function`` from the
  caller's frame) — overridable for tests or wrapper indirection;
* inheritance of the active ``saga_id`` and ``event_id`` (as
  ``cause_id``) so causal chains survive the create_task boundary
  without callers re-binding the context manually.

A bounded :class:`TaskRegistry` (LRU, default cap 1000 terminated
entries) lets ``/api/tasks`` and ``sovyx doctor tasks`` enumerate
in-flight + recently-finished tasks for operator inspection. Live
tasks are tracked separately and never evicted while running.

Orphan detection: if a successful task's result is never marked
consumed (via :func:`mark_consumed`) within 30 s of completion, a
``task.orphaned`` WARNING fires. Fire-and-forget callers who legitimately
don't care about the result should call :func:`mark_consumed` to
suppress the signal. Failed tasks are not flagged as orphans because
the failure log itself already records the unhandled outcome.

Aligned with docs-internal/plans/IMPL-OBSERVABILITY-001 §6 Task 6.2.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import threading
import time
import weakref
from collections import OrderedDict
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from structlog.contextvars import bind_contextvars, reset_contextvars

from sovyx.observability.logging import get_logger
from sovyx.observability.saga import current_event_id, current_saga_id

if TYPE_CHECKING:
    from collections.abc import Coroutine


logger = get_logger(__name__)


# ── Tunables ────────────────────────────────────────────────────────

# 8 hex chars = 32 bits of entropy: collision-resistant for any
# realistic per-process task spawn rate, short enough to read in logs.
_TASK_ID_LEN: int = 8

# LRU cap on terminated tasks kept for inspection. Live tasks are
# tracked separately and never evicted while still running.
_REGISTRY_MAX: int = 1000

# Grace period before a successful unconsumed task is flagged as
# orphaned. Long enough that brief "spawn then await" patterns don't
# false-positive, short enough to surface real leaks within a turn.
_ORPHAN_THRESHOLD_S: float = 30.0


# ── Task info record ────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True, slots=True)
class TaskInfo:
    """Snapshot of a tracked task's identity + outcome.

    Returned from :meth:`TaskRegistry.inspect`. Live tasks have
    ``status="live"`` with ``duration_ms=None`` and ``exc_type=None``;
    terminated tasks carry the final outcome.
    """

    task_id: str
    name: str
    owner: str
    saga_id: str | None
    started_at: float
    status: str
    duration_ms: int | None
    exc_type: str | None


# ── Registry ────────────────────────────────────────────────────────


class TaskRegistry:
    """Bounded LRU registry of spawned asyncio tasks.

    Live tasks are stored by ``task_id`` and are NOT subject to LRU
    eviction — only terminated tasks compete for the bounded slot
    budget. A ``threading.Lock`` guards the dicts so the registry is
    safe to read from non-asyncio threads (e.g. a sync diagnostic
    tool inspecting the daemon).
    """

    def __init__(self, *, maxsize: int = _REGISTRY_MAX) -> None:
        self._lock = threading.Lock()
        self._live: OrderedDict[str, asyncio.Task[Any]] = OrderedDict()
        self._live_meta: dict[str, tuple[str, str, str | None, float]] = {}
        self._terminated: OrderedDict[str, TaskInfo] = OrderedDict()
        self._maxsize = maxsize

    def register_live(
        self,
        task_id: str,
        task: asyncio.Task[Any],
        *,
        name: str,
        owner: str,
        saga_id: str | None,
        started_at: float,
    ) -> None:
        """Record a freshly-spawned task as live."""
        with self._lock:
            self._live[task_id] = task
            self._live_meta[task_id] = (name, owner, saga_id, started_at)

    def mark_terminated(self, task_id: str, info: TaskInfo) -> None:
        """Move a task from live → terminated and enforce the LRU cap."""
        with self._lock:
            self._live.pop(task_id, None)
            self._live_meta.pop(task_id, None)
            if task_id in self._terminated:
                self._terminated.move_to_end(task_id)
            self._terminated[task_id] = info
            while len(self._terminated) > self._maxsize:
                self._terminated.popitem(last=False)

    def inspect(self) -> list[TaskInfo]:
        """Return a snapshot of all live + terminated TaskInfos."""
        with self._lock:
            live_infos: list[TaskInfo] = []
            for task_id, _task in self._live.items():
                meta = self._live_meta.get(task_id)
                if meta is None:
                    continue
                name, owner, saga_id, started_at = meta
                live_infos.append(
                    TaskInfo(
                        task_id=task_id,
                        name=name,
                        owner=owner,
                        saga_id=saga_id,
                        started_at=started_at,
                        status="live",
                        duration_ms=None,
                        exc_type=None,
                    ),
                )
            terminated = list(self._terminated.values())
        return live_infos + terminated

    @property
    def live_count(self) -> int:
        """Number of currently-live tracked tasks."""
        with self._lock:
            return len(self._live)

    @property
    def terminated_count(self) -> int:
        """Number of retained terminated TaskInfos (≤ maxsize)."""
        with self._lock:
            return len(self._terminated)

    def clear(self) -> None:
        """Drop all live + terminated entries. Test-only."""
        with self._lock:
            self._live.clear()
            self._live_meta.clear()
            self._terminated.clear()


_REGISTRY: TaskRegistry = TaskRegistry()

# Tracks consumption flag per task. Keyed by task identity (weak ref),
# so dropping the task object releases the entry automatically.
_CONSUMED: weakref.WeakKeyDictionary[asyncio.Task[Any], bool] = weakref.WeakKeyDictionary()


def get_registry() -> TaskRegistry:
    """Return the process-wide singleton :class:`TaskRegistry`."""
    return _REGISTRY


def mark_consumed(task: asyncio.Task[Any]) -> None:
    """Tell the orphan detector that *task*'s result was intentionally observed.

    Call this when fire-and-forget semantics are intended and the
    spawn site genuinely doesn't care about the return value — the
    orphan watcher will then suppress the WARNING for this task.
    Awaiting the task directly does NOT mark it consumed; this is an
    explicit signal so casual ``await spawn(...)`` patterns still
    benefit from the orphan check.
    """
    _CONSUMED[task] = True


# ── Owner auto-detection ────────────────────────────────────────────


def _resolve_owner() -> str:
    """Return ``file:line:function`` for the caller of :func:`spawn`.

    Frame [0] is this helper, [1] is :func:`spawn`, [2] is the caller
    we want. Falls back to ``"unknown"`` if the stack walk fails (it
    will, e.g., under aggressive optimization or unusual interpreters).
    """
    try:
        frame = inspect.stack()[2]
    except (IndexError, OSError):
        return "unknown"
    try:
        return f"{frame.filename}:{frame.lineno}:{frame.function}"
    finally:
        del frame


# ── spawn() ─────────────────────────────────────────────────────────


def spawn(
    coro: Coroutine[Any, Any, Any],
    *,
    name: str,
    owner: str | None = None,
    saga_id: str | None = None,
) -> asyncio.Task[Any]:
    """Spawn *coro* as an asyncio task with full lifecycle telemetry.

    Parameters
    ----------
    coro
        The coroutine to schedule. Must not have been awaited.
    name
        Short stable label used as ``task.name`` in logs and as the
        underlying ``asyncio.Task`` name (visible in ``asyncio``
        debug introspection).
    owner
        Optional caller identifier. Defaults to
        ``file:line:function`` of the call site, derived via
        :mod:`inspect`. Pass an explicit value when calling through a
        wrapper that would otherwise be reported as the owner.
    saga_id
        Optional saga override. Defaults to the active
        :func:`current_saga_id` so the spawned task's logs link back
        to the saga that scheduled it.

    Returns
    -------
    asyncio.Task
        The created task. Awaiting it yields the coroutine's result;
        cancelling it cancels the inner coroutine. The lifecycle
        telemetry runs regardless of whether the caller awaits.
    """
    if owner is None:
        owner = _resolve_owner()
    if saga_id is None:
        saga_id = current_saga_id()
    cause_id = current_event_id()
    task_id = uuid4().hex[:_TASK_ID_LEN]
    started_at = time.monotonic()

    async def _runner() -> Any:  # noqa: ANN401 — passthrough.
        bind_kwargs: dict[str, Any] = {
            "task_id": task_id,
            "task_name": name,
            "task_owner": owner,
        }
        if saga_id is not None:
            bind_kwargs["saga_id"] = saga_id
        if cause_id is not None:
            bind_kwargs["cause_id"] = cause_id
        tokens = bind_contextvars(**bind_kwargs)
        try:
            return await coro
        finally:
            reset_contextvars(**tokens)

    task = asyncio.create_task(_runner(), name=name)
    _REGISTRY.register_live(
        task_id,
        task,
        name=name,
        owner=owner,
        saga_id=saga_id,
        started_at=started_at,
    )

    logger.info(
        "task.spawned",
        **{
            "task.id": task_id,
            "task.name": name,
            "task.owner": owner,
            "task.saga_id": saga_id or "",
        },
    )

    task.add_done_callback(
        lambda t: _on_done(t, task_id=task_id, name=name, owner=owner, saga_id=saga_id, started_at=started_at)
    )
    return task


# ── Done-callback + orphan check ────────────────────────────────────


def _on_done(
    task: asyncio.Task[Any],
    *,
    task_id: str,
    name: str,
    owner: str,
    saga_id: str | None,
    started_at: float,
) -> None:
    """Lifecycle callback: emit completed/failed/cancelled + register outcome."""
    duration_ms = int((time.monotonic() - started_at) * 1000)

    if task.cancelled():
        status = "cancelled"
        exc_type: str | None = None
        logger.info(
            "task.cancelled",
            **{
                "task.id": task_id,
                "task.name": name,
                "task.owner": owner,
                "task.duration_ms": duration_ms,
            },
        )
    else:
        # task.exception() does not raise here because task is done.
        exc = task.exception()
        if exc is not None:
            status = "failed"
            exc_type = type(exc).__name__
            logger.error(
                "task.failed",
                **{
                    "task.id": task_id,
                    "task.name": name,
                    "task.owner": owner,
                    "task.duration_ms": duration_ms,
                    "task.exc_type": exc_type,
                    "task.exc_msg": str(exc),
                },
                exc_info=(type(exc), exc, exc.__traceback__),
            )
        else:
            status = "completed"
            exc_type = None
            logger.info(
                "task.completed",
                **{
                    "task.id": task_id,
                    "task.name": name,
                    "task.owner": owner,
                    "task.duration_ms": duration_ms,
                },
            )

    info = TaskInfo(
        task_id=task_id,
        name=name,
        owner=owner,
        saga_id=saga_id,
        started_at=started_at,
        status=status,
        duration_ms=duration_ms,
        exc_type=exc_type,
    )
    _REGISTRY.mark_terminated(task_id, info)

    # Orphan detection only fires for successful results — a failed
    # task already produced an error log, so flagging it again as
    # "orphaned" would be redundant noise.
    if status == "completed":
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            return
        loop.call_later(
            _ORPHAN_THRESHOLD_S,
            _check_orphan,
            task,
            task_id,
            name,
            owner,
        )


def _check_orphan(
    task: asyncio.Task[Any],
    task_id: str,
    name: str,
    owner: str,
) -> None:
    """Emit ``task.orphaned`` if the task's result was never marked consumed."""
    if _CONSUMED.get(task, False):
        return
    logger.warning(
        "task.orphaned",
        **{
            "task.id": task_id,
            "task.name": name,
            "task.owner": owner,
            "task.threshold_s": _ORPHAN_THRESHOLD_S,
        },
    )


__all__ = [
    "TaskInfo",
    "TaskRegistry",
    "get_registry",
    "mark_consumed",
    "spawn",
]
