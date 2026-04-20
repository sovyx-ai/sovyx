"""Async logging plumbing — non-blocking handler + background drain thread.

Synchronous file IO from the hot path is the single biggest source of
tail-latency in a structured-logging pipeline: every WAL fsync stalls
the event loop for milliseconds. This module decouples emit from
write by sitting a bounded :class:`queue.Queue` between the two:

- :class:`AsyncQueueHandler` is the handler attached to the root
  logger. ``emit()`` does a single ``queue.put_nowait`` — measured at
  sub-50 µs p99 in the §23 perf budget — and increments a drop
  counter when the queue is full instead of blocking.
- :class:`BackgroundLogWriter` wraps :class:`logging.handlers.QueueListener`
  with a daemon-thread runner and a :meth:`drain_and_stop` that
  flushes pending records on shutdown.

The hand-off model is back-pressure-free by design: when the writer
falls behind, records are dropped at the queue boundary rather than
blocking application code. Drops are observable via
:meth:`AsyncQueueHandler.dropped_count` so the operator can size
``async_queue_size`` to the workload.

Aligned with docs-internal/plans/IMPL-OBSERVABILITY-001 §7 Task 1.6
and §23 (perf budgets).
"""

from __future__ import annotations

import contextlib
import logging
import logging.handlers
import queue
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable


class AsyncQueueHandler(logging.handlers.QueueHandler):
    """Non-blocking handler that drops records when its queue is full.

    Owns the underlying :class:`queue.Queue` so the matching
    :class:`BackgroundLogWriter` can subscribe without the caller
    juggling two references. ``maxsize`` is bounded — an unbounded
    queue would just defer the OOM until the writer can't keep up.
    """

    def __init__(self, maxsize: int) -> None:
        self._queue: queue.Queue[logging.LogRecord] = queue.Queue(maxsize=maxsize)
        super().__init__(self._queue)
        self._dropped: int = 0

    def enqueue(self, record: logging.LogRecord) -> None:
        """Put *record* on the queue without blocking; count drops on overflow.

        Override of :meth:`logging.handlers.QueueHandler.enqueue` —
        the default re-raises :class:`queue.Full`, which would
        propagate up through the structlog chain into application
        code. We swallow + count instead so a noisy moment can't
        crash the caller.
        """
        try:
            self._queue.put_nowait(record)
        except queue.Full:
            self._dropped += 1

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        """Hand the unformatted record to the writer thread.

        The default :meth:`logging.handlers.QueueHandler.prepare`
        eagerly formats the record so the queue contains plain
        strings — fine for ``%``-style stdlib logging, but disastrous
        with structlog's :class:`structlog.stdlib.ProcessorFormatter`
        which expects ``record.msg`` to be the original ``event_dict``.
        Pre-formatting on the producer thread also defeats the whole
        point of off-loading IO. We skip prepare entirely and let the
        downstream handlers in :class:`BackgroundLogWriter` format
        on the drain thread.
        """
        return record

    def dropped_count(self) -> int:
        """Return cumulative count of records dropped due to a full queue."""
        return self._dropped

    @property
    def queue_ref(self) -> queue.Queue[logging.LogRecord]:
        """Expose the underlying queue for the background writer."""
        return self._queue


class BackgroundLogWriter:
    """Daemon-thread drain of the async queue into the real handlers.

    Wraps :class:`logging.handlers.QueueListener`; the listener
    spawns a worker thread on :meth:`start` that pops records and
    forwards them to *handlers* (typically a
    :class:`logging.handlers.RotatingFileHandler`). The thread is
    flagged daemon so a forgotten :meth:`drain_and_stop` cannot
    prevent process exit.

    ``respect_handler_level=True`` defers level filtering to each
    downstream handler, which keeps the queue handler permissive (it
    enqueues anything the root logger lets through) while still
    allowing per-file thresholds.
    """

    __slots__ = ("_handlers", "_listener")

    def __init__(
        self,
        async_handler: AsyncQueueHandler,
        handlers: Iterable[logging.Handler],
    ) -> None:
        self._handlers: tuple[logging.Handler, ...] = tuple(handlers)
        self._listener = logging.handlers.QueueListener(
            async_handler.queue_ref,
            *self._handlers,
            respect_handler_level=True,
        )

    def start(self) -> None:
        """Spawn the daemon drain thread.

        :class:`logging.handlers.QueueListener` already flags its
        worker as a daemon on Python 3.10+, so we just delegate.
        """
        self._listener.start()

    def drain_and_stop(self, timeout: float = 5.0) -> None:
        """Send the sentinel and wait *timeout* seconds for the thread to exit.

        On full-queue shutdown the sentinel may not enqueue (we are
        consistent with :meth:`AsyncQueueHandler.enqueue`: full means
        drop). In that case the thread keeps draining whatever it
        already has and the join times out — acceptable because the
        thread is daemon and the process is about to exit anyway.
        """
        sentinel = self._listener._sentinel  # type: ignore[attr-defined]  # noqa: SLF001 — _sentinel is the documented stdlib stop marker.
        with contextlib.suppress(queue.Full):
            self._listener.queue.put_nowait(sentinel)
        thread = self._listener._thread  # noqa: SLF001 — same rationale as start().
        if thread is not None:
            thread.join(timeout=timeout)
            self._listener._thread = None  # noqa: SLF001 — mirror QueueListener.stop() bookkeeping.
        for handler in self._handlers:
            handler.flush()


__all__ = ["AsyncQueueHandler", "BackgroundLogWriter"]
