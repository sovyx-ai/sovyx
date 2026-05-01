"""Tests for ``sovyx.voice.health._audio_service_win`` (Phase 6 / T6.22).

Pre-T6.22 the Windows ``audiosrv`` monitor (197 LOC) had ZERO direct
test coverage — every code path was only exercised transitively via
the ``VoiceCaptureWatchdog`` integration tests. This module pins:

* ``_query_audiosrv_state`` parser branches: RUNNING / STOPPED /
  STOP_PENDING / non-zero return / FileNotFoundError /
  SubprocessError / TimeoutExpired / output without STATE / STATE
  with too few parts.
* ``WindowsAudioServiceMonitor.__init__`` validation.
* ``start`` / ``stop`` lifecycle (idempotent, cancels in-flight
  task, no-op without start).
* ``_run`` transition logic: baseline-seed (no spurious UP on first
  poll), Running→Stopped emits DOWN, Stopped→Running emits UP,
  flaky polls (None) treated as no-change, on_event exceptions
  swallowed, on_event CancelledError propagates.
* ``build_windows_audio_service_monitor`` factory: Noop fallback
  when ``sc.exe`` is unavailable.
"""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from sovyx.voice.health._audio_service import NoopAudioServiceMonitor
from sovyx.voice.health._audio_service_win import (
    WindowsAudioServiceMonitor,
    _query_audiosrv_state,
    build_windows_audio_service_monitor,
)
from sovyx.voice.health.contract import AudioServiceEvent, AudioServiceEventKind

# ── _query_audiosrv_state parser ──────────────────────────────────────


class TestQueryAudiosrvState:
    """sc.exe parser — every branch returns either a state string or None."""

    def _patch_run(self, *, returncode: int, stdout: str) -> Any:  # noqa: ANN401
        completed = MagicMock(spec=subprocess.CompletedProcess)
        completed.returncode = returncode
        completed.stdout = stdout
        return patch(
            "sovyx.voice.health._audio_service_win.subprocess.run",
            return_value=completed,
        )

    def test_running_state_parsed(self) -> None:
        stdout = (
            "SERVICE_NAME: audiosrv\n"
            "        TYPE               : 10  WIN32_OWN_PROCESS\n"
            "        STATE              : 4  RUNNING\n"
            "        WIN32_EXIT_CODE    : 0  (0x0)\n"
        )
        with self._patch_run(returncode=0, stdout=stdout):
            assert _query_audiosrv_state() == "RUNNING"

    def test_stopped_state_parsed(self) -> None:
        stdout = "        STATE              : 1  STOPPED\n"
        with self._patch_run(returncode=0, stdout=stdout):
            assert _query_audiosrv_state() == "STOPPED"

    def test_stop_pending_state_parsed(self) -> None:
        stdout = "        STATE              : 3  STOP_PENDING\n"
        with self._patch_run(returncode=0, stdout=stdout):
            assert _query_audiosrv_state() == "STOP_PENDING"

    def test_non_zero_returncode_returns_none(self) -> None:
        with self._patch_run(returncode=1060, stdout=""):
            assert _query_audiosrv_state() is None

    def test_file_not_found_returns_none(self) -> None:
        # sc.exe absent (unlikely on Windows, common on Linux CI).
        with patch(
            "sovyx.voice.health._audio_service_win.subprocess.run",
            side_effect=FileNotFoundError("sc.exe"),
        ):
            assert _query_audiosrv_state() is None

    def test_subprocess_error_returns_none(self) -> None:
        with patch(
            "sovyx.voice.health._audio_service_win.subprocess.run",
            side_effect=subprocess.SubprocessError("crash"),
        ):
            assert _query_audiosrv_state() is None

    def test_timeout_expired_returns_none(self) -> None:
        # TimeoutExpired is a subclass of SubprocessError — guarded by
        # the same except clause.
        with patch(
            "sovyx.voice.health._audio_service_win.subprocess.run",
            side_effect=subprocess.TimeoutExpired("sc.exe", 3.0),
        ):
            assert _query_audiosrv_state() is None

    def test_os_error_returns_none(self) -> None:
        # Permission denied / handle exhausted / etc.
        with patch(
            "sovyx.voice.health._audio_service_win.subprocess.run",
            side_effect=OSError("EACCES"),
        ):
            assert _query_audiosrv_state() is None

    def test_no_state_line_returns_none(self) -> None:
        # Output exists but doesn't carry a STATE line (e.g. service
        # not registered on this host).
        stdout = "[SC] EnumQueryServicesStatus:OpenService FAILED\n"
        with self._patch_run(returncode=0, stdout=stdout):
            assert _query_audiosrv_state() is None

    def test_state_line_too_few_parts_returns_none(self) -> None:
        # Malformed STATE line — defensive against future sc output drift.
        stdout = "        STATE  :  RUNNING\n"  # 4 parts after split → has 4? Let's be explicit.
        # "STATE  :  RUNNING".split() → ["STATE", ":", "RUNNING"] (3 parts)
        with self._patch_run(returncode=0, stdout=stdout):
            assert _query_audiosrv_state() is None


# ── Constructor validation ────────────────────────────────────────────


class TestConstructor:
    def test_default_query_factory_used_when_none(self) -> None:
        # Default ``query`` is the module-level ``_query_audiosrv_state``.
        monitor = WindowsAudioServiceMonitor()
        # Internal access — guards against a refactor that drops the
        # default-wire-up.
        assert monitor._query is _query_audiosrv_state  # noqa: SLF001

    def test_explicit_query_takes_precedence(self) -> None:
        fake = lambda: "RUNNING"  # noqa: E731 — terse test stub
        monitor = WindowsAudioServiceMonitor(query=fake)
        assert monitor._query is fake  # noqa: SLF001

    def test_zero_interval_rejected(self) -> None:
        with pytest.raises(ValueError, match="poll_interval_s must be"):
            WindowsAudioServiceMonitor(poll_interval_s=0.0)

    def test_negative_interval_rejected(self) -> None:
        with pytest.raises(ValueError, match="poll_interval_s must be"):
            WindowsAudioServiceMonitor(poll_interval_s=-1.0)


# ── Lifecycle (start / stop) ──────────────────────────────────────────


async def _noop_handler(_event: AudioServiceEvent) -> None:
    return None


class TestLifecycle:
    @pytest.mark.asyncio()
    async def test_start_launches_task(self) -> None:
        monitor = WindowsAudioServiceMonitor(
            poll_interval_s=10.0,  # long; we cancel via stop().
            query=lambda: "RUNNING",
        )
        await monitor.start(_noop_handler)
        assert monitor._task is not None  # noqa: SLF001
        assert not monitor._task.done()  # noqa: SLF001
        await monitor.stop()

    @pytest.mark.asyncio()
    async def test_start_idempotent(self) -> None:
        monitor = WindowsAudioServiceMonitor(
            poll_interval_s=10.0,
            query=lambda: "RUNNING",
        )
        await monitor.start(_noop_handler)
        first_task = monitor._task  # noqa: SLF001
        await monitor.start(_noop_handler)  # second call: no-op
        assert monitor._task is first_task  # noqa: SLF001
        await monitor.stop()

    @pytest.mark.asyncio()
    async def test_stop_cancels_in_flight_task(self) -> None:
        monitor = WindowsAudioServiceMonitor(
            poll_interval_s=100.0,  # task will sleep for ages.
            query=lambda: "RUNNING",
        )
        await monitor.start(_noop_handler)
        task = monitor._task  # noqa: SLF001
        await monitor.stop()
        assert task is not None
        assert task.done()
        assert monitor._task is None  # noqa: SLF001

    @pytest.mark.asyncio()
    async def test_stop_without_start_is_noop(self) -> None:
        monitor = WindowsAudioServiceMonitor(query=lambda: "RUNNING")
        # Should not raise.
        await monitor.stop()
        assert monitor._task is None  # noqa: SLF001


# ── Transition logic ──────────────────────────────────────────────────


class _StateSequence:
    """Programmable state-query stand-in returning queued values per call.

    Post-exhaustion behaviour: returns the LAST queued value (stable
    repeat). Pre-fix the helper returned the literal string "RUNNING"
    after the sequence drained, which made
    ``test_running_to_stopped_emits_down`` flaky on slower CI cells:
    ``_drive_polls`` waits until ``query.calls >= expected_calls`` and
    then awaits ``monitor.stop()``, but the monitor loop can issue ONE
    extra poll between those points. With the legacy "RUNNING" fallback
    a second event fires (STOPPED→RUNNING transition emits UP), and
    the test asserts ``len(capture.events) == 1`` fails with 2 ==
    1. Repeating the LAST queued state keeps post-exhaustion polls
    transition-free regardless of the host OS scheduler.
    """

    def __init__(self, sequence: list[str | None]) -> None:
        self._sequence = list(sequence)
        # Pre-seed the repeat-on-exhaustion value to "RUNNING" so an
        # empty sequence still has a deterministic baseline (matches
        # the legacy default for tests that pass [] to the helper).
        self._last: str | None = "RUNNING"
        self.calls = 0

    def __call__(self) -> str | None:
        self.calls += 1
        if not self._sequence:
            return self._last
        self._last = self._sequence.pop(0)
        return self._last


class _EventCapture:
    def __init__(self) -> None:
        self.events: list[AudioServiceEvent] = []
        self.exceptions: list[BaseException] = []

    async def __call__(self, event: AudioServiceEvent) -> None:
        self.events.append(event)

    @staticmethod
    def make_raising(
        exc: BaseException,
    ) -> Callable[[AudioServiceEvent], Awaitable[None]]:
        async def _handler(_event: AudioServiceEvent) -> None:
            raise exc

        return _handler


async def _drive_polls(
    monitor: WindowsAudioServiceMonitor,
    handler: Callable[[AudioServiceEvent], Awaitable[None]],
    *,
    expected_calls: int,
    query: _StateSequence,
) -> None:
    """Run the monitor until ``query`` has been called ``expected_calls`` times.

    The monitor's ``_run`` sleeps for ``poll_interval_s`` between
    polls; tests use a tiny interval so the loop iterates quickly.
    """
    await monitor.start(handler)
    # Spin until enough polls land OR a 2 s deadline hits (would
    # indicate a regression — should never trigger in healthy runs).
    deadline = asyncio.get_event_loop().time() + 2.0
    while query.calls < expected_calls:
        if asyncio.get_event_loop().time() > deadline:
            await monitor.stop()
            msg = f"Monitor did not poll {expected_calls} times in 2 s (observed {query.calls})"
            raise AssertionError(msg)
        await asyncio.sleep(0.005)
    await monitor.stop()


class TestTransitions:
    @pytest.mark.asyncio()
    async def test_first_running_seeds_baseline_no_event(self) -> None:
        # The very first observed state must NOT fire an event — otherwise
        # every daemon boot would emit a spurious UP at startup.
        query = _StateSequence(["RUNNING"])
        capture = _EventCapture()
        monitor = WindowsAudioServiceMonitor(
            poll_interval_s=0.001,
            query=query,
        )
        await _drive_polls(monitor, capture, expected_calls=1, query=query)
        assert capture.events == []

    @pytest.mark.asyncio()
    async def test_running_to_stopped_emits_down(self) -> None:
        query = _StateSequence(["RUNNING", "STOPPED"])
        capture = _EventCapture()
        monitor = WindowsAudioServiceMonitor(
            poll_interval_s=0.001,
            query=query,
        )
        await _drive_polls(monitor, capture, expected_calls=2, query=query)
        # Filter to events emitted by the loop (capture.events list).
        assert len(capture.events) == 1
        assert capture.events[0].kind is AudioServiceEventKind.DOWN

    @pytest.mark.asyncio()
    async def test_stopped_to_running_emits_up(self) -> None:
        query = _StateSequence(["STOPPED", "RUNNING"])
        capture = _EventCapture()
        monitor = WindowsAudioServiceMonitor(
            poll_interval_s=0.001,
            query=query,
        )
        await _drive_polls(monitor, capture, expected_calls=2, query=query)
        assert len(capture.events) == 1
        assert capture.events[0].kind is AudioServiceEventKind.UP

    @pytest.mark.asyncio()
    async def test_query_none_does_not_flip_state(self) -> None:
        # Flaky poll (None) sandwiched between Running readings must
        # NOT flip the state — operators see no spurious DOWN.
        query = _StateSequence(["RUNNING", None, "RUNNING"])
        capture = _EventCapture()
        monitor = WindowsAudioServiceMonitor(
            poll_interval_s=0.001,
            query=query,
        )
        await _drive_polls(monitor, capture, expected_calls=3, query=query)
        assert capture.events == []

    @pytest.mark.asyncio()
    async def test_full_flap_running_down_up_emits_two_events(self) -> None:
        query = _StateSequence(["RUNNING", "STOPPED", "RUNNING"])
        capture = _EventCapture()
        monitor = WindowsAudioServiceMonitor(
            poll_interval_s=0.001,
            query=query,
        )
        await _drive_polls(monitor, capture, expected_calls=3, query=query)
        assert [e.kind for e in capture.events] == [
            AudioServiceEventKind.DOWN,
            AudioServiceEventKind.UP,
        ]

    @pytest.mark.asyncio()
    async def test_handler_exception_swallowed(self) -> None:
        # The watchdog's downstream contract says that handler errors
        # must NOT kill the polling loop. Exception is logged via
        # voice_audio_service_dispatch_failed but the loop keeps polling.
        query = _StateSequence(["RUNNING", "STOPPED", "RUNNING"])
        handler = _EventCapture.make_raising(RuntimeError("downstream blew up"))
        monitor = WindowsAudioServiceMonitor(
            poll_interval_s=0.001,
            query=query,
        )
        await _drive_polls(monitor, handler, expected_calls=3, query=query)
        # The loop kept running — we observed all 3 polls without a crash.

    @pytest.mark.asyncio()
    async def test_handler_cancelled_propagates(self) -> None:
        # CancelledError is propagated (loop terminates) but the
        # ``stop()`` path swallows the exception cleanly. Regression
        # guard against accidentally turning CancelledError into a
        # warning log + continue (would defeat shutdown).
        query = _StateSequence(["RUNNING", "STOPPED"])

        async def _handler(_event: AudioServiceEvent) -> None:
            raise asyncio.CancelledError

        monitor = WindowsAudioServiceMonitor(
            poll_interval_s=0.001,
            query=query,
        )
        await monitor.start(_handler)
        # Wait for the task to end (cancel propagation).
        deadline = asyncio.get_event_loop().time() + 2.0
        while monitor._task is not None and not monitor._task.done():  # noqa: SLF001
            if asyncio.get_event_loop().time() > deadline:
                await monitor.stop()
                msg = "Cancellation did not propagate within 2 s"
                raise AssertionError(msg)
            await asyncio.sleep(0.01)
        await monitor.stop()


# ── Factory ───────────────────────────────────────────────────────────


class TestFactory:
    def test_returns_noop_when_sc_unavailable(self) -> None:
        with patch(
            "sovyx.voice.health._audio_service_win._query_audiosrv_state",
            return_value=None,
        ):
            monitor = build_windows_audio_service_monitor()
        assert isinstance(monitor, NoopAudioServiceMonitor)

    def test_returns_real_monitor_when_query_succeeds(self) -> None:
        with patch(
            "sovyx.voice.health._audio_service_win._query_audiosrv_state",
            return_value="RUNNING",
        ):
            monitor = build_windows_audio_service_monitor()
        assert isinstance(monitor, WindowsAudioServiceMonitor)
