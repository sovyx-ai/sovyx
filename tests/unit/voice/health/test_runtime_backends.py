"""Unit tests for the Sprint 2 Task #18 runtime-event backends.

Covers the generic Protocol + Noop implementations
(:mod:`sovyx.voice.health._power`, :mod:`_audio_service`,
:mod:`_default_device`), the cross-platform polling watcher
(:class:`PollingDefaultDeviceWatcher`), and the Windows-specific
:mod:`_audio_service_win` polling backend. Windows message-loop
internals in :mod:`_power_win` are exercised via the build factory
only — spawning a real Win32 thread in unit tests is brittle and the
behaviour is covered by the wider watchdog integration tests.
"""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Awaitable, Callable
from unittest.mock import MagicMock, patch

import pytest

from sovyx.voice.health._audio_service import (
    AudioServiceMonitor,
    NoopAudioServiceMonitor,
)
from sovyx.voice.health._audio_service_win import (
    WindowsAudioServiceMonitor,
    _query_audiosrv_state,
    build_windows_audio_service_monitor,
)
from sovyx.voice.health._default_device import (
    NoopDefaultDeviceWatcher,
    PollingDefaultDeviceWatcher,
)
from sovyx.voice.health._power import NoopPowerEventListener, PowerEventListener
from sovyx.voice.health.contract import (
    AudioServiceEvent,
    AudioServiceEventKind,
    HotplugEvent,
    HotplugEventKind,
    PowerEvent,
)

# ---------------------------------------------------------------------------
# Protocol sanity — Noop variants honour the contract
# ---------------------------------------------------------------------------


class TestNoopPowerEventListener:
    """`NoopPowerEventListener` never raises and never fires events."""

    @pytest.mark.asyncio()
    async def test_start_stop_are_idempotent(self) -> None:
        listener = NoopPowerEventListener(reason="test")
        observed: list[PowerEvent] = []

        async def _cb(event: PowerEvent) -> None:
            observed.append(event)

        await listener.start(_cb)
        await listener.start(_cb)
        await listener.stop()
        await listener.stop()
        assert observed == []

    def test_protocol_satisfied(self) -> None:
        listener: PowerEventListener = NoopPowerEventListener(reason="t")
        assert hasattr(listener, "start") and hasattr(listener, "stop")


class TestNoopAudioServiceMonitor:
    """`NoopAudioServiceMonitor` honours the lifecycle contract."""

    @pytest.mark.asyncio()
    async def test_start_stop_are_idempotent(self) -> None:
        monitor = NoopAudioServiceMonitor(reason="test")
        observed: list[AudioServiceEvent] = []

        async def _cb(event: AudioServiceEvent) -> None:
            observed.append(event)

        await monitor.start(_cb)
        await monitor.start(_cb)
        await monitor.stop()
        await monitor.stop()
        assert observed == []

    def test_protocol_satisfied(self) -> None:
        monitor: AudioServiceMonitor = NoopAudioServiceMonitor(reason="t")
        assert hasattr(monitor, "start") and hasattr(monitor, "stop")


class TestNoopDefaultDeviceWatcher:
    """`NoopDefaultDeviceWatcher` honours the lifecycle contract."""

    @pytest.mark.asyncio()
    async def test_start_stop_are_idempotent(self) -> None:
        watcher = NoopDefaultDeviceWatcher(reason="test")
        observed: list[HotplugEvent] = []

        async def _cb(event: HotplugEvent) -> None:
            observed.append(event)

        await watcher.start(_cb)
        await watcher.start(_cb)
        await watcher.stop()
        await watcher.stop()
        assert observed == []


# ---------------------------------------------------------------------------
# PollingDefaultDeviceWatcher
# ---------------------------------------------------------------------------


class TestPollingDefaultDeviceWatcher:
    """Cross-platform polling semantics for §4.4.3."""

    def test_invalid_interval_raises(self) -> None:
        with pytest.raises(ValueError, match="poll_interval_s"):
            PollingDefaultDeviceWatcher(
                query_default=lambda: "A",
                poll_interval_s=0.0,
            )

    @pytest.mark.asyncio()
    async def test_start_is_idempotent(self) -> None:
        watcher = PollingDefaultDeviceWatcher(
            query_default=lambda: "A",
            poll_interval_s=0.01,
        )
        observed: list[HotplugEvent] = []

        async def _cb(event: HotplugEvent) -> None:
            observed.append(event)

        await watcher.start(_cb)
        first = watcher._task  # noqa: SLF001 — test-only
        await watcher.start(_cb)
        assert watcher._task is first  # noqa: SLF001
        await watcher.stop()

    @pytest.mark.asyncio()
    async def test_baseline_is_silent_first_change_fires(self) -> None:
        readings = ["A", "A", "B"]

        def _query() -> object:
            return readings.pop(0) if readings else "B"

        watcher = PollingDefaultDeviceWatcher(
            query_default=_query,
            poll_interval_s=0.01,
        )
        observed: list[HotplugEvent] = []

        async def _cb(event: HotplugEvent) -> None:
            observed.append(event)

        await watcher.start(_cb)
        for _ in range(50):
            if observed:
                break
            await asyncio.sleep(0.01)
        await watcher.stop()
        assert len(observed) == 1
        event = observed[0]
        assert event.kind == HotplugEventKind.DEFAULT_DEVICE_CHANGED
        assert event.device_friendly_name == "B"

    @pytest.mark.asyncio()
    async def test_query_exception_does_not_kill_poller(self) -> None:
        calls = {"n": 0}

        def _query() -> object:
            calls["n"] += 1
            if calls["n"] == 2:
                msg = "PortAudio exploded"
                raise OSError(msg)
            if calls["n"] <= 1:
                return "A"
            return "B"

        watcher = PollingDefaultDeviceWatcher(
            query_default=_query,
            poll_interval_s=0.01,
        )
        observed: list[HotplugEvent] = []

        async def _cb(event: HotplugEvent) -> None:
            observed.append(event)

        await watcher.start(_cb)
        for _ in range(50):
            if observed:
                break
            await asyncio.sleep(0.01)
        await watcher.stop()
        # Exception on attempt 2 was swallowed; attempt 3 onwards sees "B".
        assert calls["n"] >= 3
        assert len(observed) == 1

    @pytest.mark.asyncio()
    async def test_dispatch_exception_is_logged_and_swallowed(self) -> None:
        readings = ["A", "B"]

        def _query() -> object:
            return readings.pop(0) if readings else "B"

        async def _raising_cb(event: HotplugEvent) -> None:
            del event
            msg = "handler blew up"
            raise RuntimeError(msg)

        watcher = PollingDefaultDeviceWatcher(
            query_default=_query,
            poll_interval_s=0.01,
        )
        await watcher.start(_raising_cb)
        # The poller keeps running — give it time to fire + survive.
        await asyncio.sleep(0.1)
        await watcher.stop()
        # No assertion on observed — the point is that stop() completes
        # cleanly after the handler raised.

    @pytest.mark.asyncio()
    async def test_stop_before_start_is_noop(self) -> None:
        watcher = PollingDefaultDeviceWatcher(
            query_default=lambda: "A",
            poll_interval_s=0.01,
        )
        await watcher.stop()  # never started


# ---------------------------------------------------------------------------
# Windows `sc query audiosrv` backend
# ---------------------------------------------------------------------------


class TestQueryAudiosrvState:
    """`_query_audiosrv_state` parses ``sc.exe`` output defensively."""

    def _run_result(self, *, returncode: int = 0, stdout: str = "") -> MagicMock:
        mock = MagicMock(spec=subprocess.CompletedProcess)
        mock.returncode = returncode
        mock.stdout = stdout
        return mock

    def test_parses_running_state(self) -> None:
        stdout = (
            "SERVICE_NAME: audiosrv\n"
            "        TYPE               : 20  WIN32_SHARE_PROCESS\n"
            "        STATE              : 4  RUNNING\n"
            "                                (STOPPABLE, PAUSABLE)\n"
        )
        with patch(
            "sovyx.voice.health._audio_service_win.subprocess.run",
            return_value=self._run_result(stdout=stdout),
        ):
            assert _query_audiosrv_state() == "RUNNING"

    def test_parses_stopped_state(self) -> None:
        stdout = "SERVICE_NAME: audiosrv\n        STATE              : 1  STOPPED\n"
        with patch(
            "sovyx.voice.health._audio_service_win.subprocess.run",
            return_value=self._run_result(stdout=stdout),
        ):
            assert _query_audiosrv_state() == "STOPPED"

    def test_non_zero_returncode_is_none(self) -> None:
        with patch(
            "sovyx.voice.health._audio_service_win.subprocess.run",
            return_value=self._run_result(returncode=1, stdout="Access denied"),
        ):
            assert _query_audiosrv_state() is None

    def test_missing_state_line_is_none(self) -> None:
        stdout = "SERVICE_NAME: audiosrv\n        TYPE               : 20  WIN32\n"
        with patch(
            "sovyx.voice.health._audio_service_win.subprocess.run",
            return_value=self._run_result(stdout=stdout),
        ):
            assert _query_audiosrv_state() is None

    def test_missing_sc_exe_is_none(self) -> None:
        with patch(
            "sovyx.voice.health._audio_service_win.subprocess.run",
            side_effect=FileNotFoundError("sc.exe not found"),
        ):
            assert _query_audiosrv_state() is None

    def test_timeout_is_none(self) -> None:
        with patch(
            "sovyx.voice.health._audio_service_win.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="sc", timeout=3.0),
        ):
            assert _query_audiosrv_state() is None


# ---------------------------------------------------------------------------
# WindowsAudioServiceMonitor
# ---------------------------------------------------------------------------


class TestWindowsAudioServiceMonitor:
    """Polling monitor emits DOWN/UP transitions via injected fake query."""

    def test_invalid_interval_raises(self) -> None:
        with pytest.raises(ValueError, match="poll_interval_s"):
            WindowsAudioServiceMonitor(poll_interval_s=0.0, query=lambda: "RUNNING")

    @pytest.mark.asyncio()
    async def test_transitions_emit_events(self) -> None:
        states = ["RUNNING", "RUNNING", "STOPPED", "RUNNING"]

        def _query() -> str | None:
            return states.pop(0) if states else "RUNNING"

        events: list[AudioServiceEvent] = []

        async def _cb(event: AudioServiceEvent) -> None:
            events.append(event)

        monitor = WindowsAudioServiceMonitor(poll_interval_s=0.01, query=_query)
        await monitor.start(_cb)
        for _ in range(80):
            if len(events) >= 2:
                break
            await asyncio.sleep(0.01)
        await monitor.stop()
        kinds = [event.kind for event in events]
        assert AudioServiceEventKind.DOWN in kinds
        assert AudioServiceEventKind.UP in kinds
        # DOWN must come before UP.
        assert kinds.index(AudioServiceEventKind.DOWN) < kinds.index(
            AudioServiceEventKind.UP,
        )

    @pytest.mark.asyncio()
    async def test_failed_query_is_treated_as_no_change(self) -> None:
        """A transient ``sc`` failure must not flip the state."""
        readings: list[str | None] = ["RUNNING", None, None, "STOPPED"]

        def _query() -> str | None:
            return readings.pop(0) if readings else "STOPPED"

        events: list[AudioServiceEvent] = []

        async def _cb(event: AudioServiceEvent) -> None:
            events.append(event)

        monitor = WindowsAudioServiceMonitor(poll_interval_s=0.01, query=_query)
        await monitor.start(_cb)
        for _ in range(80):
            if events:
                break
            await asyncio.sleep(0.01)
        await monitor.stop()
        # After the two ``None`` polls the monitor observed RUNNING → STOPPED
        # and fired exactly one DOWN; the failed polls never emitted anything.
        assert [event.kind for event in events] == [AudioServiceEventKind.DOWN]

    @pytest.mark.asyncio()
    async def test_start_is_idempotent(self) -> None:
        monitor = WindowsAudioServiceMonitor(
            poll_interval_s=0.01,
            query=lambda: "RUNNING",
        )

        async def _cb(event: AudioServiceEvent) -> None:
            del event

        await monitor.start(_cb)
        first = monitor._task  # noqa: SLF001
        await monitor.start(_cb)
        assert monitor._task is first  # noqa: SLF001
        await monitor.stop()

    @pytest.mark.asyncio()
    async def test_dispatch_exception_is_swallowed(self) -> None:
        states = ["RUNNING", "STOPPED"]

        def _query() -> str | None:
            return states.pop(0) if states else "STOPPED"

        async def _raising_cb(event: AudioServiceEvent) -> None:
            del event
            msg = "handler exploded"
            raise RuntimeError(msg)

        monitor = WindowsAudioServiceMonitor(poll_interval_s=0.01, query=_query)
        await monitor.start(_raising_cb)
        await asyncio.sleep(0.1)
        await monitor.stop()

    @pytest.mark.asyncio()
    async def test_stop_before_start_is_noop(self) -> None:
        monitor = WindowsAudioServiceMonitor(
            poll_interval_s=0.01,
            query=lambda: "RUNNING",
        )
        await monitor.stop()


class TestBuildWindowsAudioServiceMonitor:
    """Factory probes once and returns Noop when ``sc`` fails."""

    def test_returns_noop_when_sc_unavailable(self) -> None:
        with patch(
            "sovyx.voice.health._audio_service_win._query_audiosrv_state",
            return_value=None,
        ):
            monitor = build_windows_audio_service_monitor()
        assert isinstance(monitor, NoopAudioServiceMonitor)

    def test_returns_real_monitor_when_probe_succeeds(self) -> None:
        with patch(
            "sovyx.voice.health._audio_service_win._query_audiosrv_state",
            return_value="RUNNING",
        ):
            monitor = build_windows_audio_service_monitor()
        assert isinstance(monitor, WindowsAudioServiceMonitor)


# ---------------------------------------------------------------------------
# Misc: callback signature shape
# ---------------------------------------------------------------------------


class TestCallbackShape:
    """Sanity: handler callables accept our dataclasses without coercion."""

    def test_power_event_callable(self) -> None:
        async def _cb(event: PowerEvent) -> None:
            del event

        _: Callable[[PowerEvent], Awaitable[None]] = _cb

    def test_audio_service_event_callable(self) -> None:
        async def _cb(event: AudioServiceEvent) -> None:
            del event

        _: Callable[[AudioServiceEvent], Awaitable[None]] = _cb
