"""Tests for sovyx.observability.async_handler — non-blocking log plumbing."""

from __future__ import annotations

import logging
import logging.handlers
import queue
import time
from collections.abc import Generator
from pathlib import Path

import pytest

from sovyx.observability import _health_state
from sovyx.observability.async_handler import (
    AsyncQueueHandler,
    BackgroundLogWriter,
    TrackingRotatingFileHandler,
)


@pytest.fixture(autouse=True)
def _reset_health_state() -> Generator[None, None, None]:
    """Each test starts with empty drop / handler-error windowed counters."""
    _health_state.reset_for_testing()
    yield
    _health_state.reset_for_testing()


def _make_record(msg: str = "hello", level: int = logging.INFO) -> logging.LogRecord:
    """Build a minimal stdlib LogRecord for queue tests."""
    return logging.LogRecord(
        name="test.logger",
        level=level,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=None,
        exc_info=None,
    )


class TestAsyncQueueHandlerInit:
    """Construction binds a bounded internal queue."""

    def test_owns_bounded_queue_of_requested_maxsize(self) -> None:
        handler = AsyncQueueHandler(maxsize=8)
        assert isinstance(handler.queue_ref, queue.Queue)
        assert handler.queue_ref.maxsize == 8
        assert handler.dropped_count() == 0

    def test_queue_ref_is_the_same_underlying_queue(self) -> None:
        handler = AsyncQueueHandler(maxsize=4)
        assert handler.queue_ref is handler.queue


class TestAsyncQueueHandlerEnqueue:
    """``enqueue`` is non-blocking and counts overflow drops."""

    def test_enqueue_puts_record_on_queue(self) -> None:
        handler = AsyncQueueHandler(maxsize=4)
        record = _make_record("a")
        handler.enqueue(record)
        assert handler.queue_ref.qsize() == 1
        assert handler.queue_ref.get_nowait() is record

    def test_enqueue_does_not_raise_when_queue_full(self) -> None:
        handler = AsyncQueueHandler(maxsize=2)
        handler.enqueue(_make_record("a"))
        handler.enqueue(_make_record("b"))
        # Queue is now full — third enqueue must NOT raise.
        handler.enqueue(_make_record("c"))
        assert handler.dropped_count() == 1

    def test_dropped_count_accumulates_across_overflows(self) -> None:
        handler = AsyncQueueHandler(maxsize=1)
        handler.enqueue(_make_record("first"))
        for _ in range(5):
            handler.enqueue(_make_record("overflow"))
        assert handler.dropped_count() == 5

    def test_drop_increments_60s_health_counter(self) -> None:
        handler = AsyncQueueHandler(maxsize=1)
        handler.enqueue(_make_record("first"))
        handler.enqueue(_make_record("dropped"))
        assert _health_state.count_drops_60s() == 1


class TestAsyncQueueHandlerPrepare:
    """``prepare`` must NOT pre-format — structlog ProcessorFormatter needs the raw event_dict."""

    def test_prepare_returns_record_unchanged(self) -> None:
        handler = AsyncQueueHandler(maxsize=4)
        record = _make_record({"event": "structured.payload"})  # type: ignore[arg-type]
        prepared = handler.prepare(record)
        assert prepared is record
        assert prepared.msg == {"event": "structured.payload"}

    def test_prepare_does_not_call_format(self) -> None:
        handler = AsyncQueueHandler(maxsize=4)
        record = _make_record("dict-as-msg")
        # If prepare formatted the record, ``msg`` would be replaced by ``message``
        # and ``args`` would be cleared (default QueueHandler.prepare behaviour).
        record.args = ("arg1",)
        original_args = record.args
        handler.prepare(record)
        assert record.args == original_args


class TestBackgroundLogWriterDrain:
    """The drain thread forwards queued records to downstream handlers."""

    def test_records_reach_downstream_handler_after_start(self, tmp_path: Path) -> None:
        log_path = tmp_path / "drain.log"
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setFormatter(logging.Formatter("%(message)s"))
        async_handler = AsyncQueueHandler(maxsize=16)
        writer = BackgroundLogWriter(async_handler, [file_handler])
        try:
            writer.start()
            for i in range(3):
                async_handler.enqueue(_make_record(f"line-{i}"))
            # Allow the daemon thread time to drain.
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                if log_path.exists() and log_path.stat().st_size > 0:
                    break
                time.sleep(0.01)
        finally:
            writer.drain_and_stop(timeout=2.0)
            file_handler.close()
        contents = log_path.read_text(encoding="utf-8").splitlines()
        assert contents == ["line-0", "line-1", "line-2"]

    def test_drain_and_stop_flushes_pending_records(self, tmp_path: Path) -> None:
        log_path = tmp_path / "flush.log"
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setFormatter(logging.Formatter("%(message)s"))
        async_handler = AsyncQueueHandler(maxsize=64)
        writer = BackgroundLogWriter(async_handler, [file_handler])
        writer.start()
        try:
            for i in range(10):
                async_handler.enqueue(_make_record(f"x{i}"))
        finally:
            writer.drain_and_stop(timeout=3.0)
            file_handler.close()
        contents = log_path.read_text(encoding="utf-8").splitlines()
        assert len(contents) == 10
        assert contents[0] == "x0"
        assert contents[-1] == "x9"

    def test_drain_and_stop_is_safe_when_queue_full_at_shutdown(self, tmp_path: Path) -> None:
        # Tiny queue; no consumer started yet — sentinel from drain_and_stop
        # would normally collide with full-queue. Must NOT raise.
        async_handler = AsyncQueueHandler(maxsize=1)
        async_handler.enqueue(_make_record("blocker"))
        downstream = logging.NullHandler()
        writer = BackgroundLogWriter(async_handler, [downstream])
        # No start() — listener thread doesn't exist; drain_and_stop must
        # tolerate the missing thread plus the full queue without raising.
        writer.drain_and_stop(timeout=0.2)


class TestBackgroundLogWriterDaemonThread:
    """The drain thread is a daemon (process exit doesn't wait on it)."""

    def test_thread_is_daemon_after_start(self) -> None:
        async_handler = AsyncQueueHandler(maxsize=4)
        writer = BackgroundLogWriter(async_handler, [logging.NullHandler()])
        try:
            writer.start()
            thread = writer._listener._thread  # noqa: SLF001 — test introspection.
            assert thread is not None
            assert thread.daemon is True
        finally:
            writer.drain_and_stop(timeout=1.0)


class TestTrackingRotatingFileHandler:
    """``handleError`` increments the §27.2 windowed handler-error counter."""

    def test_handle_error_counts_into_health_state(self, tmp_path: Path) -> None:
        log_path = tmp_path / "tracking.log"
        handler = TrackingRotatingFileHandler(
            log_path, maxBytes=1024, backupCount=1, encoding="utf-8"
        )
        try:
            assert _health_state.count_handler_errors_60s() == 0
            # ``logging.Handler.handleError`` reads ``sys.exc_info()``, so it must be
            # called from inside an ``except`` block — that's how stdlib drives it
            # from ``emit()``. Outside one, the super() call crashes on a None tb.
            for label in ("bad", "worse"):
                try:
                    msg = label
                    raise RuntimeError(msg)
                except RuntimeError:
                    handler.handleError(_make_record(label))
            assert _health_state.count_handler_errors_60s() == 2
        finally:
            handler.close()

    def test_handle_error_delegates_to_super(self, tmp_path: Path) -> None:
        log_path = tmp_path / "delegate.log"
        handler = TrackingRotatingFileHandler(
            log_path, maxBytes=1024, backupCount=1, encoding="utf-8"
        )
        try:
            # Same exc_info contract as above — super().handleError prints a
            # traceback to stderr but must not propagate.
            try:
                msg = "boom"
                raise RuntimeError(msg)
            except RuntimeError:
                handler.handleError(_make_record("non-raising"))
        finally:
            handler.close()

    def test_subclass_inherits_rotating_behaviour(self, tmp_path: Path) -> None:
        log_path = tmp_path / "rotating.log"
        handler = TrackingRotatingFileHandler(
            log_path, maxBytes=64, backupCount=2, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        try:
            for i in range(20):
                handler.emit(_make_record("x" * 16 + f"-{i}"))
            handler.flush()
        finally:
            handler.close()
        # At least one rotation file should have been produced.
        rotated = list(tmp_path.glob("rotating.log.*"))
        assert rotated, "expected rotation backup to exist after exceeding maxBytes"
