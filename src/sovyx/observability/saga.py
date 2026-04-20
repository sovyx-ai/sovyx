"""Saga tracing — causal chains via structlog contextvars.

A *saga* is a top-level user-initiated operation (cognitive turn,
voice turn, dashboard chat request, bridge message). Every log entry
emitted within the saga's scope carries the same ``saga_id`` so an
operator can reconstruct the full causal chain by filtering on it.

A *span* is a sub-operation within a saga (LLM call, plugin invoke,
DB write). Spans carry their own ``span_id`` and may inherit a
``cause_id`` from a parent span/event for explicit lineage.

Implementation uses ``structlog.contextvars`` as the single source of
truth — the same store that the ``merge_contextvars`` processor reads
when rendering each entry. ``bind_contextvars`` returns
:class:`contextvars.Token` objects which we feed to
``reset_contextvars`` on exit, restoring the parent scope's bindings.
This makes nesting safe: opening saga B inside saga A cleanly returns
to A's ``saga_id`` when B exits.

Aligned with docs-internal/plans/IMPL-OBSERVABILITY-001 §8 (Phase 2).
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import time
from typing import TYPE_CHECKING, Any, TypeVar
from uuid import uuid4

from structlog.contextvars import (
    bind_contextvars,
    get_contextvars,
    reset_contextvars,
)

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterator

logger = get_logger(__name__)

# All three IDs share the same shape: 16 hex chars (64 bits of
# entropy, collision-free for any realistic per-process saga rate).
# Long enough to grep, short enough to fit comfortably in dashboards
# and JSON envelopes.
_ID_HEX_LEN: int = 16


def _new_id() -> str:
    """Return a fresh 16-hex-char identifier (saga, event, or span)."""
    return uuid4().hex[:_ID_HEX_LEN]


def _read_str(name: str) -> str | None:
    """Return the string-typed contextvar named *name*, or ``None``."""
    value = get_contextvars().get(name)
    return value if isinstance(value, str) else None


def current_saga_id() -> str | None:
    """Return the active saga_id bound in this async/sync context, or ``None``."""
    return _read_str("saga_id")


def current_event_id() -> str | None:
    """Return the active event_id (set by EventBus during handler dispatch)."""
    return _read_str("event_id")


def current_span_id() -> str | None:
    """Return the active span_id, or ``None`` outside any span."""
    return _read_str("span_id")


F = TypeVar("F", bound="Callable[..., Any]")


def trace_saga(name: str, *, kind: str = "default") -> Callable[[F], F]:
    """Decorate a function to run inside a fresh saga scope.

    Dispatches on :func:`asyncio.iscoroutinefunction` so the same
    decorator works on ``def`` and ``async def`` callables. The
    wrapped function runs inside :func:`saga_scope` (sync) or
    :func:`async_saga_scope` (async) — see those for the lifecycle
    contract.

    Nested usage is supported: opening a saga from inside another
    saga creates a child scope with its own ``saga_id``; the parent
    ``saga_id`` is restored on exit via :class:`contextvars.Token`
    reset tokens.
    """

    def decorator(fn: F) -> F:
        if asyncio.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401 — passthrough decorator; *args/**kwargs are caller-defined.
                async with async_saga_scope(name, kind=kind):
                    return await fn(*args, **kwargs)

            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(fn)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401 — passthrough decorator; *args/**kwargs are caller-defined.
            with saga_scope(name, kind=kind):
                return fn(*args, **kwargs)

        return sync_wrapper  # type: ignore[return-value]

    return decorator


def _emit_failed(
    event: str,
    started: float,
    exc: BaseException,
    **fields: Any,  # noqa: ANN401 — structlog log record; fields are event-specific.
) -> None:
    """Emit a ``*.failed`` entry with duration + exception summary."""
    duration_ms = (time.perf_counter() - started) * 1000.0
    logger.error(
        event,
        duration_ms=round(duration_ms, 3),
        exc_type=type(exc).__name__,
        exc_msg=str(exc),
        **fields,
    )


def _emit_completed(
    event: str,
    started: float,
    **fields: Any,  # noqa: ANN401 — structlog log record; fields are event-specific.
) -> None:
    """Emit a ``*.completed`` entry with duration."""
    duration_ms = (time.perf_counter() - started) * 1000.0
    logger.info(event, duration_ms=round(duration_ms, 3), **fields)


@contextlib.contextmanager
def saga_scope(name: str, *, kind: str = "default") -> Iterator[str]:
    """Sync context manager that opens a saga scope.

    Generates ``saga_id``, binds it to structlog contextvars, emits
    ``saga.started``, then ``saga.completed`` (success) or
    ``saga.failed`` (any :class:`BaseException`, re-raised). Always
    restores the parent scope's bindings via reset tokens — nested
    sagas survive correctly.

    Catches :class:`BaseException` so KeyboardInterrupt / SystemExit
    inside a saga still produce a ``saga.failed`` entry. The
    exception is re-raised in all cases — this is observability
    only, not error handling.
    """
    saga_id = _new_id()
    tokens = bind_contextvars(saga_id=saga_id)
    started = time.perf_counter()
    logger.info("saga.started", saga_name=name, kind=kind)
    try:
        yield saga_id
    except BaseException as exc:  # noqa: BLE001 — observability layer; we re-raise.
        _emit_failed("saga.failed", started, exc, saga_name=name, kind=kind)
        raise
    else:
        _emit_completed("saga.completed", started, saga_name=name, kind=kind)
    finally:
        reset_contextvars(**tokens)


@contextlib.asynccontextmanager
async def async_saga_scope(name: str, *, kind: str = "default") -> AsyncIterator[str]:
    """Async equivalent of :func:`saga_scope` — same contract."""
    saga_id = _new_id()
    tokens = bind_contextvars(saga_id=saga_id)
    started = time.perf_counter()
    logger.info("saga.started", saga_name=name, kind=kind)
    try:
        yield saga_id
    except BaseException as exc:  # noqa: BLE001 — observability layer; we re-raise.
        _emit_failed("saga.failed", started, exc, saga_name=name, kind=kind)
        raise
    else:
        _emit_completed("saga.completed", started, saga_name=name, kind=kind)
    finally:
        reset_contextvars(**tokens)


@contextlib.contextmanager
def span_scope(name: str, *, cause_id: str | None = None) -> Iterator[str]:
    """Open a span (sub-operation) inside the current saga.

    Generates a fresh ``span_id`` and inherits ``cause_id`` from the
    current event scope when the caller doesn't supply one — this is
    how event-handler chains acquire automatic lineage without every
    caller having to thread the parent id manually.

    Emits ``span.started`` / ``span.completed`` / ``span.failed`` with
    the same lifecycle contract as :func:`saga_scope`.
    """
    span_id = _new_id()
    bind_kwargs: dict[str, Any] = {"span_id": span_id}
    inherited = cause_id if cause_id is not None else current_event_id()
    if inherited is not None:
        bind_kwargs["cause_id"] = inherited
    tokens = bind_contextvars(**bind_kwargs)
    started = time.perf_counter()
    logger.info("span.started", span_name=name)
    try:
        yield span_id
    except BaseException as exc:  # noqa: BLE001 — observability layer; we re-raise.
        _emit_failed("span.failed", started, exc, span_name=name)
        raise
    else:
        _emit_completed("span.completed", started, span_name=name)
    finally:
        reset_contextvars(**tokens)


@contextlib.asynccontextmanager
async def async_span_scope(
    name: str,
    *,
    cause_id: str | None = None,
) -> AsyncIterator[str]:
    """Async equivalent of :func:`span_scope` — same contract."""
    span_id = _new_id()
    bind_kwargs: dict[str, Any] = {"span_id": span_id}
    inherited = cause_id if cause_id is not None else current_event_id()
    if inherited is not None:
        bind_kwargs["cause_id"] = inherited
    tokens = bind_contextvars(**bind_kwargs)
    started = time.perf_counter()
    logger.info("span.started", span_name=name)
    try:
        yield span_id
    except BaseException as exc:  # noqa: BLE001 — observability layer; we re-raise.
        _emit_failed("span.failed", started, exc, span_name=name)
        raise
    else:
        _emit_completed("span.completed", started, span_name=name)
    finally:
        reset_contextvars(**tokens)


__all__ = [
    "async_saga_scope",
    "async_span_scope",
    "current_event_id",
    "current_saga_id",
    "current_span_id",
    "saga_scope",
    "span_scope",
    "trace_saga",
]
