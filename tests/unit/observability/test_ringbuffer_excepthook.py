"""Tests for sovyx.observability.ringbuffer — in-memory crash forensics buffer."""

from __future__ import annotations

import asyncio
import atexit
import json
import logging
import sys
from collections.abc import Generator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from sovyx.observability.ringbuffer import RingBufferHandler, install_crash_hooks


def _make_record(msg: str = "hello", level: int = logging.INFO) -> logging.LogRecord:
    """Build a minimal stdlib LogRecord for ring-buffer tests."""
    return logging.LogRecord(
        name="ring.test",
        level=level,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=None,
        exc_info=None,
    )


@pytest.fixture()
def _isolate_excepthook() -> Generator[None, None, None]:
    """Snapshot ``sys.excepthook`` so install_crash_hooks doesn't leak across tests."""
    original = sys.excepthook
    yield
    sys.excepthook = original


@pytest.fixture()
def _no_atexit_register() -> Generator[MagicMock, None, None]:
    """Patch ``atexit.register`` so test runs don't accumulate dumpers at interpreter exit."""
    with patch.object(atexit, "register") as mock:
        yield mock


class TestRingBufferHandlerInit:
    """Construction binds a bounded deque + a handler-level lock."""

    def test_capacity_zero_is_pathological_but_allowed(self) -> None:
        # ``deque(maxlen=0)`` accepts everything but always reports empty.
        handler = RingBufferHandler(capacity=0)
        handler.emit(_make_record("anything"))
        assert handler.snapshot() == []

    def test_capacity_one_keeps_only_latest(self) -> None:
        handler = RingBufferHandler(capacity=1)
        handler.emit(_make_record("first"))
        handler.emit(_make_record("second"))
        snap = handler.snapshot()
        assert len(snap) == 1
        assert snap[0][1] == "second"

    def test_handler_is_a_logging_handler(self) -> None:
        handler = RingBufferHandler(capacity=4)
        assert isinstance(handler, logging.Handler)


class TestRingBufferHandlerEmit:
    """``emit`` is synchronous, captures formatted message + creation timestamp."""

    def test_records_appear_in_snapshot_in_order(self) -> None:
        handler = RingBufferHandler(capacity=8)
        for i in range(5):
            handler.emit(_make_record(f"line-{i}"))
        snap = handler.snapshot()
        assert [msg for _, msg in snap] == [f"line-{i}" for i in range(5)]

    def test_oldest_entries_evicted_at_capacity(self) -> None:
        handler = RingBufferHandler(capacity=3)
        for i in range(10):
            handler.emit(_make_record(f"r{i}"))
        snap = handler.snapshot()
        assert [msg for _, msg in snap] == ["r7", "r8", "r9"]

    def test_timestamp_matches_record_created(self) -> None:
        handler = RingBufferHandler(capacity=4)
        record = _make_record("ts-check")
        record.created = 1234567890.5
        handler.emit(record)
        ts, _ = handler.snapshot()[0]
        assert ts == 1234567890.5

    def test_emit_does_not_raise_on_format_failure(self) -> None:
        handler = RingBufferHandler(capacity=4)
        record = _make_record("bad")
        # Force ``format()`` to raise; emit must swallow + handleError.
        with patch.object(handler, "format", side_effect=RuntimeError("fmt boom")):
            handler.emit(record)
        # Buffer remains empty — the failed record was dropped, not partially appended.
        assert handler.snapshot() == []

    def test_uses_attached_formatter(self) -> None:
        handler = RingBufferHandler(capacity=4)
        handler.setFormatter(logging.Formatter("FORMATTED:%(message)s"))
        handler.emit(_make_record("payload"))
        assert handler.snapshot()[0][1] == "FORMATTED:payload"


class TestRingBufferHandlerSnapshot:
    """``snapshot`` returns an independent copy under the dump lock."""

    def test_snapshot_is_independent_copy(self) -> None:
        handler = RingBufferHandler(capacity=4)
        handler.emit(_make_record("a"))
        first = handler.snapshot()
        handler.emit(_make_record("b"))
        second = handler.snapshot()
        assert len(first) == 1
        assert len(second) == 2

    def test_snapshot_yields_oldest_first(self) -> None:
        handler = RingBufferHandler(capacity=4)
        for label in ("alpha", "beta", "gamma"):
            handler.emit(_make_record(label))
        msgs = [msg for _, msg in handler.snapshot()]
        assert msgs == ["alpha", "beta", "gamma"]


class TestRingBufferHandlerDumpToFile:
    """``dump_to_file`` writes JSONL with ``{ts, msg}`` per line."""

    def test_dump_creates_parent_directory(self, tmp_path: Path) -> None:
        handler = RingBufferHandler(capacity=4)
        handler.emit(_make_record("hi"))
        target = tmp_path / "nested" / "deeper" / "dump.jsonl"
        assert not target.parent.exists()
        handler.dump_to_file(target)
        assert target.exists()

    def test_dump_writes_one_record_per_line(self, tmp_path: Path) -> None:
        handler = RingBufferHandler(capacity=8)
        records = [
            (1.0, "first"),
            (2.0, "second"),
            (3.0, "third"),
        ]
        for ts, msg in records:
            r = _make_record(msg)
            r.created = ts
            handler.emit(r)
        target = tmp_path / "dump.jsonl"
        handler.dump_to_file(target)
        lines = target.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 3
        decoded = [json.loads(line) for line in lines]
        assert decoded == [
            {"ts": 1.0, "msg": "first"},
            {"ts": 2.0, "msg": "second"},
            {"ts": 3.0, "msg": "third"},
        ]

    def test_dump_overwrites_existing_file(self, tmp_path: Path) -> None:
        target = tmp_path / "dump.jsonl"
        target.write_text("STALE\nDATA\n", encoding="utf-8")
        handler = RingBufferHandler(capacity=4)
        handler.emit(_make_record("fresh"))
        handler.dump_to_file(target)
        contents = target.read_text(encoding="utf-8")
        assert "STALE" not in contents
        assert json.loads(contents.splitlines()[0])["msg"] == "fresh"

    def test_empty_buffer_writes_empty_file(self, tmp_path: Path) -> None:
        handler = RingBufferHandler(capacity=4)
        target = tmp_path / "empty.jsonl"
        handler.dump_to_file(target)
        assert target.exists()
        assert target.read_text(encoding="utf-8") == ""

    def test_dump_serialises_unicode_without_escaping(self, tmp_path: Path) -> None:
        handler = RingBufferHandler(capacity=4)
        handler.emit(_make_record("olá — ção"))
        target = tmp_path / "u.jsonl"
        handler.dump_to_file(target)
        line = target.read_text(encoding="utf-8").splitlines()[0]
        assert "olá — ção" in line  # ensure_ascii=False keeps the literal glyphs.


class TestInstallCrashHooksExcepthook:
    """``sys.excepthook`` chains: dump runs before delegating to the previous hook."""

    def test_excepthook_dumps_buffer_to_path(
        self,
        tmp_path: Path,
        _isolate_excepthook: None,
        _no_atexit_register: MagicMock,
    ) -> None:
        handler = RingBufferHandler(capacity=4)
        handler.emit(_make_record("crash-trace"))
        dump_path = tmp_path / "crash.jsonl"
        install_crash_hooks(handler, dump_path)
        # Simulate an uncaught exception.
        try:
            msg = "boom"
            raise RuntimeError(msg)
        except RuntimeError:
            sys.excepthook(*sys.exc_info())
        assert dump_path.exists()
        line = dump_path.read_text(encoding="utf-8").splitlines()[0]
        assert json.loads(line)["msg"] == "crash-trace"

    def test_excepthook_chains_to_previous(
        self,
        tmp_path: Path,
        _isolate_excepthook: None,
        _no_atexit_register: MagicMock,
    ) -> None:
        previous = MagicMock()
        sys.excepthook = previous
        handler = RingBufferHandler(capacity=4)
        install_crash_hooks(handler, tmp_path / "dump.jsonl")
        try:
            msg = "x"
            raise ValueError(msg)
        except ValueError:
            exc_info = sys.exc_info()
            sys.excepthook(*exc_info)
        previous.assert_called_once_with(exc_info[0], exc_info[1], exc_info[2])

    def test_excepthook_swallows_dump_failure(
        self,
        tmp_path: Path,
        _isolate_excepthook: None,
        _no_atexit_register: MagicMock,
    ) -> None:
        previous = MagicMock()
        sys.excepthook = previous
        handler = RingBufferHandler(capacity=4)
        install_crash_hooks(handler, tmp_path / "dump.jsonl")
        # Make dump_to_file raise; the excepthook must still chain to ``previous``.
        with patch.object(handler, "dump_to_file", side_effect=OSError("disk full")):
            try:
                msg = "y"
                raise KeyError(msg)
            except KeyError:
                exc_info = sys.exc_info()
                sys.excepthook(*exc_info)
        previous.assert_called_once()


class TestInstallCrashHooksAtExit:
    """``atexit.register`` is wired with a best-effort dumper."""

    def test_registers_atexit_callback(
        self,
        tmp_path: Path,
        _isolate_excepthook: None,
        _no_atexit_register: MagicMock,
    ) -> None:
        handler = RingBufferHandler(capacity=4)
        install_crash_hooks(handler, tmp_path / "dump.jsonl")
        assert _no_atexit_register.call_count == 1
        callback = _no_atexit_register.call_args.args[0]
        assert callable(callback)

    def test_atexit_callback_dumps_buffer(
        self,
        tmp_path: Path,
        _isolate_excepthook: None,
        _no_atexit_register: MagicMock,
    ) -> None:
        handler = RingBufferHandler(capacity=4)
        handler.emit(_make_record("exit-trace"))
        dump_path = tmp_path / "exit.jsonl"
        install_crash_hooks(handler, dump_path)
        callback = _no_atexit_register.call_args.args[0]
        callback()
        assert dump_path.exists()
        line = dump_path.read_text(encoding="utf-8").splitlines()[0]
        assert json.loads(line)["msg"] == "exit-trace"

    def test_atexit_callback_swallows_dump_failure(
        self,
        tmp_path: Path,
        _isolate_excepthook: None,
        _no_atexit_register: MagicMock,
    ) -> None:
        handler = RingBufferHandler(capacity=4)
        install_crash_hooks(handler, tmp_path / "dump.jsonl")
        callback = _no_atexit_register.call_args.args[0]
        with patch.object(handler, "dump_to_file", side_effect=OSError("nope")):
            # Must NOT raise — atexit failures would mask the real exit reason.
            callback()


class TestInstallCrashHooksAsyncio:
    """Asyncio loop integration — exception_handler dumps + delegates."""

    @pytest.mark.asyncio
    async def test_running_loop_gets_exception_handler(
        self,
        tmp_path: Path,
        _isolate_excepthook: None,
        _no_atexit_register: MagicMock,
    ) -> None:
        handler = RingBufferHandler(capacity=4)
        handler.emit(_make_record("loop-trace"))
        dump_path = tmp_path / "loop.jsonl"
        install_crash_hooks(handler, dump_path)
        loop = asyncio.get_running_loop()
        exc_handler = loop.get_exception_handler()
        assert exc_handler is not None
        # Trigger the handler synthetically — no real loop error needed.
        exc_handler(loop, {"message": "synthetic", "exception": RuntimeError("x")})
        assert dump_path.exists()
        line = dump_path.read_text(encoding="utf-8").splitlines()[0]
        assert json.loads(line)["msg"] == "loop-trace"

    def test_no_running_loop_does_not_raise(
        self,
        tmp_path: Path,
        _isolate_excepthook: None,
        _no_atexit_register: MagicMock,
    ) -> None:
        # Outside an asyncio context, install_crash_hooks must skip the
        # loop wiring silently.
        handler = RingBufferHandler(capacity=4)
        install_crash_hooks(handler, tmp_path / "dump.jsonl")
