"""FastPathHandler — synchronous, fsync-on-every-emit path for CRITICAL/security.

The async queue handler is great for throughput (sub-50 µs p99 emit
latency) but during pressure (queue near capacity, writer thread
behind) records can sit waiting. For a normal ``audio.frame`` that's
fine — we sample those anyway. For ``security.incident.detected`` or
``engine.bootstrap.fatal`` it is *not* fine: if the daemon dies in
the next 50 ms, that single most-important entry never reaches disk.

This module routes a small allow-list of events around the queue
through a synchronous handler that ``flush()`` + ``os.fsync()`` on
every emit. Latency rises to ~500 µs (one disk seek) but durability
becomes deterministic — a forced ``os._exit(1)`` 1 µs after the
emit returns still finds the entry on disk.

Trade-off intentional: the caller of a CRITICAL record is already in
a degraded state (fatal/security/queue-full); blocking it for an
extra ~500 µs is the right call. Performance budget §23 caps the
fast-path emit p99 at 2 ms, leaving 4× headroom for unusual disks.

Wire-up convention in :func:`setup_logging`:

- :class:`FastPathHandler` is attached to the root logger BEFORE the
  async handler, with a :class:`FastPathFilter` that admits only
  fast-path records.
- The async handler (and any non-fast-path file handler) gets a
  :class:`NonFastPathFilter` so fast-path records aren't
  double-emitted through both routes.

Aligned with docs-internal/plans/IMPL-OBSERVABILITY-001 §22.8.
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


# Allow-list of event names that bypass the async queue. Kept narrow on
# purpose: every entry on this list pays the fsync cost, so any
# high-frequency event would torch the perf budget.
_FAST_PATH_EVENTS: frozenset[str] = frozenset(
    {
        "engine.bootstrap.fatal",
        "engine.shutdown.requested",
        "security.incident.detected",
        "security.tamper_chain.broken",
        # Meta-events about the logger itself: if the queue is dropping
        # records, we MUST persist that fact synchronously — otherwise
        # the operator can't tell the difference between "no errors"
        # and "all errors silently dropped".
        "logging.dropped",
        "logging.queue.full",
    }
)


def is_fast_path_record(record: logging.LogRecord) -> bool:
    """Return True if *record* should bypass the async queue.

    Three criteria, OR-ed:

    1. ``record.levelno >= CRITICAL`` — every CRITICAL or higher entry
       is durability-critical by convention.
    2. The event name starts with ``security.`` — namespace contract;
       any incident anywhere in security.* is auditable evidence.
    3. The event name is in :data:`_FAST_PATH_EVENTS` — a small
       allow-list of named events that earn the fsync.

    Tolerant of structlog's two record shapes: the modern
    ``ProcessorFormatter`` chain stuffs the processed event_dict into
    ``record.msg`` (dict with ``"event"`` key); legacy code that still
    calls ``logger.info("event_name")`` produces ``record.msg`` as a
    plain string. Both forms are handled.
    """
    if record.levelno >= logging.CRITICAL:
        return True
    msg = record.msg
    event: str | None = None
    if isinstance(msg, dict):
        candidate = msg.get("event")
        if isinstance(candidate, str):
            event = candidate
    elif isinstance(msg, str):
        event = msg
    if event is None:
        return False
    return event.startswith("security.") or event in _FAST_PATH_EVENTS


class FastPathFilter(logging.Filter):
    """Admit only fast-path records — attached to :class:`FastPathHandler`.

    Defining the gate as a :class:`logging.Filter` (instead of an
    ``if`` inside ``emit``) lets the stdlib filter chain short-circuit
    BEFORE ``Handler.emit`` runs ``Handler.format``, saving the JSON
    render cost for records that won't be written here anyway.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return is_fast_path_record(record)


class NonFastPathFilter(logging.Filter):
    """Reject fast-path records — attached to async/file handlers.

    Symmetric companion to :class:`FastPathFilter`. Without this on
    the async handler, every CRITICAL/security entry would land in
    BOTH the fast-path file AND the main async-rotated file, doubling
    storage cost and confusing log forwarders that dedupe by content.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return not is_fast_path_record(record)


class FastPathHandler(logging.Handler):
    """Synchronous handler that ``flush() + fsync()`` on every emit.

    Owns a single append-mode file handle for the lifetime of the
    process (no rotation — fast-path file is small by design and
    expected to be tailed/forwarded out-of-band). Writes are
    serialized through a :class:`threading.Lock` so concurrent
    callers can't interleave partial JSON lines.

    ``os.fsync`` failures are swallowed: if fsync isn't supported on
    the underlying filesystem (``OSError: [Errno 22]`` on some FUSE
    mounts; ``OSError`` on certain Windows network shares), we still
    want the entry persisted — the OS write cache will get it to
    disk on its own schedule, which is strictly better than the entry
    being lost because we raised.
    """

    def __init__(self, path: Path) -> None:
        super().__init__()
        self._path = path
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        # Append mode + line-buffered (buffering=1) keeps stdlib's
        # internal buffer small; we still call flush() explicitly to
        # be sure, but the small buffer caps worst-case data loss to
        # one line if a non-fsync failure happens mid-write.
        self._stream = path.open("a", encoding="utf-8", buffering=1)

    def emit(self, record: logging.LogRecord) -> None:
        """Write *record* synchronously with flush + fsync."""
        try:
            line = self.format(record) + "\n"
            with self._lock:
                self._stream.write(line)
                self._stream.flush()
                with contextlib.suppress(OSError):
                    os.fsync(self._stream.fileno())
        except Exception:  # noqa: BLE001 — handleError is the documented stdlib entry point.
            self.handleError(record)

    def close(self) -> None:
        """Flush + close the file handle, then super().close()."""
        with self._lock:
            with contextlib.suppress(Exception):
                self._stream.flush()
            with contextlib.suppress(Exception):
                self._stream.close()
        super().close()


__all__ = [
    "FastPathFilter",
    "FastPathHandler",
    "NonFastPathFilter",
    "is_fast_path_record",
]
