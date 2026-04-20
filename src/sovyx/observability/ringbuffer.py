"""RingBufferHandler — keep the last N log entries for crash forensics.

A daemon that crashes loses the most valuable evidence: the seconds
just before the failure. The file handler is asynchronous (see
:class:`sovyx.observability.async_handler.AsyncQueueHandler`) and may
not have flushed those records to disk yet. This module attaches a
synchronous ring buffer to the root logger so a snapshot of the most
recent N entries is always in memory and can be dumped to a JSONL
file on uncaught exception, asyncio loop error, or normal exit.

The buffer is bounded by :class:`collections.deque(maxlen=...)`,
which is O(1) for both append and overflow eviction — important
because this handler runs synchronously on the emit path.

Crash hook installation is centralised in
:func:`install_crash_hooks` so the same dump path is wired into
``sys.excepthook``, ``asyncio.AbstractEventLoop.set_exception_handler``
and :func:`atexit.register`. Each hook chains to the previous one
(rather than replacing it), so a downstream tool's excepthook is not
clobbered.

Aligned with docs-internal/plans/IMPL-OBSERVABILITY-001 §7 Task 1.7.
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import json
import logging
import sys
import threading
from collections import deque
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path
    from types import TracebackType


class RingBufferHandler(logging.Handler):
    """Logging handler that retains the last *capacity* entries in memory.

    Stores ``(timestamp, formatted_record)`` tuples. The ``timestamp``
    is captured by the standard logging machinery (``record.created``)
    so the dump is post-hoc reorderable even if records arrive out of
    order from multiple threads.

    Formatting happens inline (synchronously) so the ring buffer
    survives a crash mid-emit: there is no deferred work to lose. The
    handler is intentionally cheap — :meth:`format` defaults to
    ``record.getMessage()`` plus exception info — so it can stay
    attached at DEBUG level without dominating the perf budget.

    Thread-safe: deque append is atomic in CPython (single bytecode
    op under the GIL), and the dump path holds an explicit lock to
    snapshot consistently.
    """

    def __init__(self, capacity: int) -> None:
        super().__init__()
        self._buffer: deque[tuple[float, str]] = deque(maxlen=capacity)
        self._dump_lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        """Append the formatted record to the ring (oldest evicted on overflow)."""
        try:
            formatted = self.format(record)
        except Exception:  # noqa: BLE001 — emit must never raise; swallow + drop the record.
            self.handleError(record)
            return
        self._buffer.append((record.created, formatted))

    def snapshot(self) -> list[tuple[float, str]]:
        """Return a list copy of the current buffer (oldest first)."""
        with self._dump_lock:
            return list(self._buffer)

    def dump_to_file(self, path: Path) -> None:
        """Serialise the buffer to *path* as JSONL (one ``{ts, msg}`` per line).

        The parent directory is created if missing — the dump path is
        typically resolved from ``data_dir`` and the data dir may not
        exist yet on a fresh install. The dump is best-effort: if the
        write fails (disk full, permission denied), the exception is
        re-raised so the caller's crash handler can record it.
        """
        with self._dump_lock:
            entries = list(self._buffer)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as fh:
            for ts, msg in entries:
                fh.write(json.dumps({"ts": ts, "msg": msg}, ensure_ascii=False))
                fh.write("\n")


def install_crash_hooks(handler: RingBufferHandler, dump_path: Path) -> None:
    """Wire *handler* to dump to *dump_path* on uncaught exception or exit.

    Three hook points are installed, each chaining to the previous
    handler so other tools (debuggers, sentry, etc.) keep working:

    - ``sys.excepthook`` — fires for uncaught exceptions in the main
      thread. The dump runs *before* delegating so a Python-internal
      crash during the previous excepthook still leaves evidence.
    - ``asyncio.AbstractEventLoop.set_exception_handler`` — wired
      onto the running loop if one exists. Asyncio swallows
      task exceptions silently by default; this surfaces them.
    - :func:`atexit.register` — fires on normal interpreter shutdown,
      so a clean stop also yields a snapshot for trend analysis.

    Idempotent at the chain level: re-calling this function adds a
    second layer of hooks that both dump (the second to overwrite the
    first). In practice it is called once during setup_logging.
    """
    previous_excepthook = sys.excepthook

    def _excepthook(
        exc_type: type[BaseException],
        exc_value: BaseException,
        exc_tb: TracebackType | None,
    ) -> None:
        # Never mask the original crash with a dump failure.
        with contextlib.suppress(Exception):
            handler.dump_to_file(dump_path)
        previous_excepthook(exc_type, exc_value, exc_tb)

    sys.excepthook = _excepthook

    def _asyncio_handler(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        # Never mask the loop error.
        with contextlib.suppress(Exception):
            handler.dump_to_file(dump_path)
        loop.default_exception_handler(context)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        loop.set_exception_handler(_asyncio_handler)

    def _atexit_dump() -> None:
        # Best-effort on shutdown.
        with contextlib.suppress(Exception):
            handler.dump_to_file(dump_path)

    atexit.register(_atexit_dump)


__all__ = ["RingBufferHandler", "install_crash_hooks"]
