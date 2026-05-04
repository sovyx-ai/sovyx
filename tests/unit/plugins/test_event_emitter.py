"""Unit tests for :mod:`sovyx.plugins._event_emitter`.

Regression coverage for the v0.30.8 fix that stops every plugin
lifecycle emit from surfacing as ``task.orphaned`` 30 s later.

Background: ``_fire`` schedules ``_emit_with_logging`` via
:func:`sovyx.observability.tasks.spawn` for saga / cause contextvar
propagation. The orphan watcher in ``observability.tasks`` flags any
successful task whose result is never marked consumed — and plugin
lifecycle emission is *intentional* fire-and-forget. Until this fix
the manager never called :func:`mark_consumed`, so every successful
emit surfaced as a delayed WARNING. The fix calls ``mark_consumed``
right after ``spawn`` to honour the documented contract at
``observability/tasks.py:23-25``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from sovyx.plugins import _event_emitter as mod
from sovyx.plugins._event_emitter import PluginEventEmitter
from sovyx.plugins.events import (
    PluginAutoDisabled,
    PluginLoaded,
    PluginToolExecuted,
    PluginUnloaded,
)


class _FakeHealth:
    """Stub matching the slice of ``_PluginHealth`` ``auto_disabled`` reads."""

    consecutive_failures = 3
    last_error = "boom"


class TestFireMarksTaskConsumed:
    """``_fire`` calls :func:`mark_consumed` on the spawned task.

    This is the core contract: fire-and-forget emission MUST NOT
    surface as a ``task.orphaned`` warning. We verify the contract by
    spying on ``mark_consumed`` from the module's import namespace
    (``sovyx.plugins._event_emitter.mark_consumed``).
    """

    @pytest.mark.asyncio()
    async def test_tool_executed_marks_consumed(self) -> None:
        bus = AsyncMock()
        emitter = PluginEventEmitter(event_bus=bus)
        with patch.object(mod, "mark_consumed") as spy:
            emitter.tool_executed(
                plugin_name="weather",
                tool_name="get_weather",
                success=True,
                duration_ms=12,
                error_msg="",
            )
            # Drain the spawned task so the emit completes.
            await _drain_plugin_event_tasks()
        assert spy.call_count == 1
        marked = spy.call_args.args[0]
        assert isinstance(marked, asyncio.Task)
        assert marked.done()

    @pytest.mark.asyncio()
    async def test_loaded_marks_consumed(self) -> None:
        bus = AsyncMock()
        emitter = PluginEventEmitter(event_bus=bus)
        with patch.object(mod, "mark_consumed") as spy:
            emitter.loaded(plugin_name="weather", version="1.0", tools_count=2)
            await _drain_plugin_event_tasks()
        assert spy.call_count == 1

    @pytest.mark.asyncio()
    async def test_unloaded_marks_consumed(self) -> None:
        bus = AsyncMock()
        emitter = PluginEventEmitter(event_bus=bus)
        with patch.object(mod, "mark_consumed") as spy:
            emitter.unloaded(plugin_name="weather", reason="manual")
            await _drain_plugin_event_tasks()
        assert spy.call_count == 1

    @pytest.mark.asyncio()
    async def test_auto_disabled_marks_consumed(self) -> None:
        bus = AsyncMock()
        emitter = PluginEventEmitter(event_bus=bus)
        with patch.object(mod, "mark_consumed") as spy:
            emitter.auto_disabled(plugin_name="weather", health=_FakeHealth())
            await _drain_plugin_event_tasks()
        assert spy.call_count == 1


class TestFireRemainsRobust:
    """Pre-existing fire-and-forget semantics survive the fix."""

    @pytest.mark.asyncio()
    async def test_no_event_bus_drops_silently(self) -> None:
        emitter = PluginEventEmitter(event_bus=None)
        with patch.object(mod, "mark_consumed") as spy:
            emitter.tool_executed(
                plugin_name="weather",
                tool_name="get_weather",
                success=True,
                duration_ms=12,
                error_msg="",
            )
        assert spy.call_count == 0

    def test_no_running_loop_drops_silently(self) -> None:
        bus = AsyncMock()
        emitter = PluginEventEmitter(event_bus=bus)
        # Calling outside an asyncio loop must NOT raise + must NOT
        # try to mark anything consumed.
        with patch.object(mod, "mark_consumed") as spy:
            emitter.loaded(plugin_name="weather", version="1.0", tools_count=2)
        assert spy.call_count == 0
        bus.emit.assert_not_called()

    @pytest.mark.asyncio()
    async def test_event_payload_reaches_bus(self) -> None:
        bus = AsyncMock()
        emitter = PluginEventEmitter(event_bus=bus)
        emitter.tool_executed(
            plugin_name="weather",
            tool_name="get_weather",
            success=False,
            duration_ms=99,
            error_msg="upstream 500",
        )
        await _drain_plugin_event_tasks()
        assert bus.emit.await_count == 1
        sent = bus.emit.await_args.args[0]
        assert isinstance(sent, PluginToolExecuted)
        assert sent.plugin_name == "weather"
        assert sent.success is False
        assert sent.error_message == "upstream 500"

    @pytest.mark.asyncio()
    async def test_unloaded_payload_reaches_bus(self) -> None:
        bus = AsyncMock()
        emitter = PluginEventEmitter(event_bus=bus)
        emitter.unloaded(plugin_name="weather", reason="manual")
        await _drain_plugin_event_tasks()
        sent = bus.emit.await_args.args[0]
        assert isinstance(sent, PluginUnloaded)
        assert sent.reason == "manual"

    @pytest.mark.asyncio()
    async def test_loaded_payload_reaches_bus(self) -> None:
        bus = AsyncMock()
        emitter = PluginEventEmitter(event_bus=bus)
        emitter.loaded(plugin_name="weather", version="1.0", tools_count=2)
        await _drain_plugin_event_tasks()
        sent = bus.emit.await_args.args[0]
        assert isinstance(sent, PluginLoaded)
        assert sent.tools_count == 2  # noqa: PLR2004

    @pytest.mark.asyncio()
    async def test_auto_disabled_payload_reaches_bus(self) -> None:
        bus = AsyncMock()
        emitter = PluginEventEmitter(event_bus=bus)
        emitter.auto_disabled(plugin_name="weather", health=_FakeHealth())
        await _drain_plugin_event_tasks()
        sent = bus.emit.await_args.args[0]
        assert isinstance(sent, PluginAutoDisabled)
        assert sent.consecutive_failures == _FakeHealth.consecutive_failures


async def _drain_plugin_event_tasks() -> None:
    """Wait for the in-flight ``plugin-event-emit`` task to settle.

    Mirrors ``tests/plugins/test_manager.py::_drain_plugin_events`` so
    test assertions on the event bus and consumption flag observe the
    final state of the spawned task.
    """
    loop = asyncio.get_running_loop()
    for _ in range(10):
        pending = [t for t in asyncio.all_tasks(loop) if t.get_name() == "plugin-event-emit"]
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)
