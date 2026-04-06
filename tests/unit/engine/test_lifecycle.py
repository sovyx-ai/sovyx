"""Tests for sovyx.engine.lifecycle — PidLock + LifecycleManager."""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from sovyx.engine.errors import EngineError
from sovyx.engine.lifecycle import LifecycleManager, PidLock
from sovyx.engine.registry import ServiceRegistry

if TYPE_CHECKING:
    from pathlib import Path


class TestPidLock:
    """PID file management."""

    def test_acquire_creates_pid_file(self, tmp_path: Path) -> None:
        pid_path = tmp_path / "sovyx.pid"
        lock = PidLock(pid_path)
        lock.acquire()
        assert pid_path.exists()
        assert pid_path.read_text().strip() == str(os.getpid())

    def test_release_removes_pid_file(self, tmp_path: Path) -> None:
        pid_path = tmp_path / "sovyx.pid"
        lock = PidLock(pid_path)
        lock.acquire()
        lock.release()
        assert not pid_path.exists()

    def test_acquire_detects_running_instance(self, tmp_path: Path) -> None:
        pid_path = tmp_path / "sovyx.pid"
        # Write our own PID (we're alive)
        pid_path.write_text(str(os.getpid()))

        lock = PidLock(pid_path)
        with pytest.raises(EngineError, match="already running"):
            lock.acquire()

    def test_acquire_removes_stale_pid(self, tmp_path: Path) -> None:
        pid_path = tmp_path / "sovyx.pid"
        # Write a fake dead PID
        pid_path.write_text("999999999")

        lock = PidLock(pid_path)
        lock.acquire()  # Should succeed — stale PID removed
        assert pid_path.read_text().strip() == str(os.getpid())

    def test_acquire_handles_corrupt_pid(self, tmp_path: Path) -> None:
        pid_path = tmp_path / "sovyx.pid"
        pid_path.write_text("not-a-number")

        lock = PidLock(pid_path)
        lock.acquire()  # Should succeed — corrupt file removed
        assert pid_path.exists()

    def test_release_missing_file_no_crash(self, tmp_path: Path) -> None:
        pid_path = tmp_path / "sovyx.pid"
        lock = PidLock(pid_path)
        lock.release()  # Should not crash

    def test_is_process_alive_current(self) -> None:
        assert PidLock._is_process_alive(os.getpid()) is True

    def test_is_process_alive_dead(self) -> None:
        assert PidLock._is_process_alive(999999999) is False

    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        pid_path = tmp_path / "subdir" / "sovyx.pid"
        lock = PidLock(pid_path)
        lock.acquire()
        assert pid_path.exists()


class TestLifecycleManager:
    """Lifecycle start/stop."""

    async def test_start_and_stop(self, tmp_path: Path) -> None:
        registry = ServiceRegistry()
        event_bus = AsyncMock()
        event_bus.emit = AsyncMock()
        pid_path = tmp_path / "sovyx.pid"

        mgr = LifecycleManager(registry, event_bus, pid_path)
        await mgr.start()

        assert pid_path.exists()
        assert mgr._running is True
        event_bus.emit.assert_called_once()

        await mgr.stop()
        assert not pid_path.exists()
        assert mgr._running is False

    async def test_stop_idempotent(self, tmp_path: Path) -> None:
        registry = ServiceRegistry()
        event_bus = AsyncMock()
        event_bus.emit = AsyncMock()
        pid_path = tmp_path / "sovyx.pid"

        mgr = LifecycleManager(registry, event_bus, pid_path)
        await mgr.start()
        await mgr.stop()
        await mgr.stop()  # Second stop should be no-op

    async def test_shutdown_calls_services(self, tmp_path: Path) -> None:
        """Services stopped in reverse order."""
        from sovyx.bridge.manager import BridgeManager
        from sovyx.cognitive.gate import CogLoopGate
        from sovyx.cognitive.loop import CognitiveLoop
        from sovyx.llm.router import LLMRouter
        from sovyx.persistence.manager import DatabaseManager

        registry = ServiceRegistry()
        order: list[str] = []

        for cls, name in [
            (CognitiveLoop, "loop"),
            (CogLoopGate, "gate"),
            (BridgeManager, "bridge"),
            (LLMRouter, "router"),
            (DatabaseManager, "db"),
        ]:
            mock = AsyncMock()

            async def make_stop(n: str = name) -> None:
                order.append(n)

            mock.stop = AsyncMock(side_effect=make_stop)
            mock.start = AsyncMock()
            registry.register_instance(cls, mock)

        event_bus = AsyncMock()
        event_bus.emit = AsyncMock()
        pid_path = tmp_path / "sovyx.pid"

        mgr = LifecycleManager(registry, event_bus, pid_path)
        await mgr.start()
        await mgr.stop()

        # Shutdown order: bridge → gate → loop → router → db
        assert order == ["bridge", "gate", "loop", "router", "db"]

    async def test_sd_notify_no_socket(self, tmp_path: Path) -> None:
        """sd_notify is silent when NOTIFY_SOCKET is not set."""
        registry = ServiceRegistry()
        event_bus = AsyncMock()
        event_bus.emit = AsyncMock()
        pid_path = tmp_path / "sovyx.pid"

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NOTIFY_SOCKET", None)
            mgr = LifecycleManager(registry, event_bus, pid_path)
            await mgr.start()
            await mgr.stop()

    async def test_run_forever_responds_to_event(self, tmp_path: Path) -> None:
        """run_forever unblocks when shutdown event is set."""
        registry = ServiceRegistry()
        event_bus = AsyncMock()
        event_bus.emit = AsyncMock()
        pid_path = tmp_path / "sovyx.pid"

        mgr = LifecycleManager(registry, event_bus, pid_path)
        with patch.object(mgr, "_start_dashboard", new_callable=AsyncMock):
            await mgr.start()

        # Set shutdown event after a delay
        async def trigger() -> None:
            await asyncio.sleep(0.05)
            mgr._shutdown_event.set()

        asyncio.create_task(trigger())
        await asyncio.wait_for(mgr.run_forever(), timeout=2.0)
        assert not pid_path.exists()

    async def test_engine_events_emitted(self, tmp_path: Path) -> None:
        """EngineStarted and EngineStopping events emitted."""
        from sovyx.engine.events import EngineStarted, EngineStopping

        registry = ServiceRegistry()
        event_bus = AsyncMock()
        event_bus.emit = AsyncMock()
        pid_path = tmp_path / "sovyx.pid"

        mgr = LifecycleManager(registry, event_bus, pid_path)
        await mgr.start()
        await mgr.stop()

        calls = event_bus.emit.call_args_list
        event_types = [type(c[0][0]) for c in calls]
        assert EngineStarted in event_types
        assert EngineStopping in event_types
