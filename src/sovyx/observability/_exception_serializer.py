"""Exception tree serializer — preserves cause/context chains and PEP 654 groups.

Default structlog rendering of ``exc_info`` produces a single
formatted traceback string. That collapses two pieces of forensic
information operators routinely need:

1. **Chained causes** — ``raise X from Y`` and implicit
   ``__context__`` from ``except`` blocks. A flattened traceback shows
   them visually but you can't ``jq`` on them.
2. **PEP 654 ExceptionGroup tree** — ``asyncio.TaskGroup`` failures
   wrap N sub-exceptions inside a ``BaseExceptionGroup``. The default
   renderer prints "ExceptionGroup: 3 sub-exceptions" — the actual
   sub-exceptions become opaque, which is a forensics hole exactly
   when you most need detail.

This module emits a structured tree instead. Each node carries
``type`` / ``module`` / ``message`` / ``cause`` / ``context``, plus
``group_message`` + ``sub_exceptions`` for groups, plus a one-line
``cause_chain`` (the ``traceback.format_exception_only`` summary
for each link) for at-a-glance debugging.

A cycle guard (``id`` set) prevents infinite recursion when
``__cause__`` / ``__context__`` form a loop — pathological but legal.
A depth cap keeps the serialized payload bounded so a 1000-deep
chain can't OOM the writer thread.

Aligned with docs-internal/plans/IMPL-OBSERVABILITY-001 §22.2.
"""

from __future__ import annotations

import sys
import traceback
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import MutableMapping
    from types import TracebackType


# Defence-in-depth caps. A pathological chain (recursive __cause__ or
# 500-level deep ExceptionGroup nesting) must not be able to inflate
# a single log entry into something the per-field clamp can't handle
# downstream.
_MAX_CHAIN_DEPTH: int = 32
_MAX_GROUP_BREADTH: int = 64
_MAX_TRACEBACK_FRAMES: int = 32


def _format_one_liner(exc: BaseException) -> str:
    """Return ``"ClassName: message"`` for *exc* (no traceback).

    Wraps :func:`traceback.format_exception_only` and strips the
    trailing newline so the result composes cleanly into a list
    field. ``format_exception_only`` returns a list of strings (one
    per line for multi-line messages); we join with ``" "`` to keep
    every link single-line.
    """
    parts = traceback.format_exception_only(type(exc), exc)
    return " ".join(p.rstrip("\n") for p in parts).strip()


def _format_traceback(tb: TracebackType | None) -> list[str]:
    """Return up to ``_MAX_TRACEBACK_FRAMES`` formatted traceback lines.

    Operators want the bottom of the stack (where the exception
    originated) more than the top, so we keep the last N frames.
    Python's :func:`traceback.format_tb` already returns frames
    bottom-up after slicing, so a simple ``[-N:]`` keeps the most
    relevant context.
    """
    if tb is None:
        return []
    formatted = traceback.format_tb(tb)
    return [line.rstrip("\n") for line in formatted[-_MAX_TRACEBACK_FRAMES:]]


def serialize_exception(
    exc: BaseException,
    *,
    _seen: set[int] | None = None,
    _depth: int = 0,
) -> dict[str, Any]:
    """Serialize *exc* into a JSON-friendly dict tree.

    Recurses into ``__cause__`` (set by ``raise X from Y``) and
    ``__context__`` (implicit, suppressed via ``raise X from None``).
    For :class:`BaseExceptionGroup` (Python 3.11+) also recurses into
    each sub-exception under ``sub_exceptions``.

    The ``_seen`` and ``_depth`` parameters are recursion bookkeeping
    — callers should not pass them. They guard against cyclic
    ``__cause__`` chains and pathologically deep nesting respectively.
    """
    if _seen is None:
        _seen = set()
    exc_id = id(exc)
    if exc_id in _seen or _depth >= _MAX_CHAIN_DEPTH:
        return {
            "type": type(exc).__name__,
            "module": type(exc).__module__,
            "message": str(exc),
            "truncated": True,
        }
    _seen.add(exc_id)

    node: dict[str, Any] = {
        "type": type(exc).__name__,
        "module": type(exc).__module__,
        "message": str(exc),
        "cause": None,
        "context": None,
    }

    if exc.__cause__ is not None:
        node["cause"] = serialize_exception(exc.__cause__, _seen=_seen, _depth=_depth + 1)
    elif exc.__context__ is not None and not exc.__suppress_context__:
        # When __cause__ is set, Python suppresses the implicit
        # __context__ in the default printer — match that here so the
        # tree mirrors what the operator sees in stderr.
        node["context"] = serialize_exception(
            exc.__context__,
            _seen=_seen,
            _depth=_depth + 1,
        )

    # PEP 654 (Python 3.11+): BaseExceptionGroup is the parent of both
    # ExceptionGroup (Exception subclass) and the BaseException variant,
    # so a single isinstance check catches both. Sovyx targets 3.11+
    # (CI matrix), so the builtin is always available.
    if isinstance(exc, BaseExceptionGroup):
        # `BaseExceptionGroup.message` is the human-readable header
        # stored separately from str(self) (which formats sub-counts).
        node["group_message"] = exc.message
        sub_serialised = [
            serialize_exception(sub, _seen=_seen, _depth=_depth + 1)
            for sub in exc.exceptions[:_MAX_GROUP_BREADTH]
        ]
        node["sub_exceptions"] = sub_serialised
        total = len(exc.exceptions)
        if total > _MAX_GROUP_BREADTH:
            node["sub_exceptions_truncated"] = total - _MAX_GROUP_BREADTH

    return node


def build_cause_chain(exc: BaseException, *, max_depth: int = _MAX_CHAIN_DEPTH) -> list[str]:
    """Return one-line summaries of *exc* and every link in its chain.

    Walks ``__cause__`` first (explicit ``raise X from Y``), falling
    back to ``__context__`` when ``__suppress_context__`` is False.
    Result is ordered most-recent-first, matching how operators
    naturally read traces ("the failure was X, because Y, because Z").

    A separate utility from :func:`serialize_exception` because
    operators frequently want just the chain summary on a hot path
    (e.g. for log search facets) without paying the cost of building
    the full tree.
    """
    chain: list[str] = []
    seen: set[int] = set()
    cur: BaseException | None = exc
    depth = 0
    while cur is not None and depth < max_depth:
        if id(cur) in seen:
            break
        seen.add(id(cur))
        chain.append(_format_one_liner(cur))
        if cur.__cause__ is not None:
            cur = cur.__cause__
        elif cur.__context__ is not None and not cur.__suppress_context__:
            cur = cur.__context__
        else:
            cur = None
        depth += 1
    return chain


class ExceptionTreeProcessor:
    """Structlog processor that converts ``exc_info`` into a structured tree.

    Hot-path: the typical record has no exception attached, so the
    processor short-circuits in O(1). When a record IS an exception
    (caller passed ``exc_info=True`` or set ``exc_info=...``),
    we serialize the chain into ``exc`` plus a flat
    ``exc.cause_chain`` summary, then drop ``exc_info`` from the
    event_dict so structlog/stdlib don't double-render the same
    failure as a string traceback.

    The processor swallows its own serialization errors and emits
    a placeholder ``exc.serialize_error`` field instead of raising.
    A bug in the serializer must not be able to crash the caller's
    ``logger.exception(...)`` call — observability-of-observability
    rule §27.4.
    """

    __slots__ = ()

    def __call__(
        self,
        logger: Any,  # noqa: ANN401 — structlog protocol; opaque logger ref.
        method_name: str,
        event_dict: MutableMapping[str, Any],
    ) -> MutableMapping[str, Any]:
        """Replace ``exc_info`` with structured ``exc`` + ``exc.cause_chain``.

        Accepts any of the structlog-conventional shapes for
        ``exc_info``: ``True`` (read from :func:`sys.exc_info`), a
        ``BaseException`` instance, or a 3-tuple
        ``(type, value, traceback)``. Anything else is left untouched.
        """
        exc_info = event_dict.pop("exc_info", None)
        exc = self._extract_exception(exc_info)
        if exc is None:
            return event_dict

        try:
            event_dict["exc"] = serialize_exception(exc)
            event_dict["exc.cause_chain"] = build_cause_chain(exc)
            tb_lines = _format_traceback(exc.__traceback__)
            if tb_lines:
                event_dict["exc.traceback"] = tb_lines
        except Exception as serializer_failure:  # noqa: BLE001 — defence in depth; see §27.4.
            event_dict["exc.serialize_error"] = (
                f"{type(serializer_failure).__name__}: {serializer_failure}"
            )
            event_dict["exc.fallback"] = repr(exc)
        return event_dict

    @staticmethod
    def _extract_exception(exc_info: Any) -> BaseException | None:  # noqa: ANN401 — input is structlog-shaped, must accept Any.
        """Coerce *exc_info* into a single :class:`BaseException`, or ``None``.

        Tolerates the three shapes structlog accepts (``True``,
        instance, tuple) plus ``False``/``None`` (no-op). An invalid
        shape returns ``None`` rather than raising — see the class
        docstring for the rationale.
        """
        if exc_info is None or exc_info is False:
            return None
        if exc_info is True:
            current = sys.exc_info()
            return current[1]
        if isinstance(exc_info, BaseException):
            return exc_info
        if isinstance(exc_info, tuple) and len(exc_info) == 3:
            value = exc_info[1]
            return value if isinstance(value, BaseException) else None
        return None


__all__ = ["ExceptionTreeProcessor", "build_cause_chain", "serialize_exception"]
