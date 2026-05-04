"""Plugin lifecycle event emitter — single sink for all 4 event types.

Replaces the four ~25-line `_emit_*` methods that used to live inside
``PluginManager`` and shared the same try/except/loop boilerplate. Logic
is identical: best-effort fire-and-forget on the EventBus, swallowing
any failure (logging side-channels must never crash plugin execution).

Saga/cause propagation: ``spawn(coro)`` copies the current
:mod:`contextvars` context (PEP 567), so any ``saga_id`` bound at the
fire site flows into the spawned task automatically. ``EventBus.emit``
then re-binds ``saga_id`` + ``cause_id=event.event_id`` before each
handler dispatch (see Phase 2 Task 2.3 in
``observability/saga.py`` + ``engine/events.py``). The wrapper
:meth:`PluginEventEmitter._emit_with_logging` makes that flow explicit
by emitting a ``plugin.event.scheduled`` record carrying the saga/cause
ids visible at fire time — useful when reconstructing why a handler
ran from logs alone.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger
from sovyx.observability.saga import current_event_id, current_saga_id
from sovyx.observability.tasks import mark_consumed, spawn

if TYPE_CHECKING:  # pragma: no cover
    from sovyx.engine.events import Event, EventBus
    from sovyx.plugins._manager_types import _PluginHealth


logger = get_logger(__name__)


class PluginEventEmitter:
    """Best-effort fire-and-forget emitter for plugin lifecycle events.

    All emit methods:
    - return immediately if no event bus is wired,
    - schedule the emit on the running loop (no-op if no loop),
    - swallow any exception (event emission must never crash callers).
    """

    def __init__(self, event_bus: EventBus | None) -> None:
        self._event_bus = event_bus

    def _fire(self, event: Event) -> None:
        if self._event_bus is None:
            return
        try:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                return  # No event loop — drop silently.
            # The spawned task inherits this frame's contextvars (PEP
            # 567), so saga_id / event_id bound here remain visible
            # inside _emit_with_logging and downstream EventBus.emit.
            #
            # Plugin lifecycle emission is intentional fire-and-forget:
            # the manager doesn't await the EventBus dispatch and never
            # observes the result. ``mark_consumed`` honours the
            # ``observability.tasks`` contract that any successful task
            # whose result is never observed should be flagged as a
            # leak via ``task.orphaned``. Without this call every
            # plugin lifecycle event surfaces as a 30-second-delayed
            # WARNING in operator logs (~12 false positives observed
            # in a 4-minute session under v0.30.7).
            task = spawn(self._emit_with_logging(event), name="plugin-event-emit")
            mark_consumed(task)
        except Exception:  # noqa: BLE001  # nosec B110
            # Event emission must never crash the manager.
            return

    async def _emit_with_logging(self, event: Event) -> None:
        """Emit *event* on the bus after recording a scheduled-marker log.

        The marker carries the ``saga_id`` / ``cause_id`` that were
        active at fire time so a log-only reconstruction can connect
        the plugin operation that triggered the emit to the handlers
        that fired downstream. Failures inside ``EventBus.emit`` are
        swallowed — observability must never crash plugin execution.
        """
        assert self._event_bus is not None  # noqa: S101 — _fire guarded.
        logger.debug(
            "plugin.event.scheduled",
            **{
                "plugin.event.type": type(event).__name__,
                "plugin.event.id": event.event_id,
                "saga.id_at_fire": current_saga_id() or "",
                "cause.id_at_fire": current_event_id() or "",
            },
        )
        try:
            await self._event_bus.emit(event)
        except Exception:  # noqa: BLE001  # nosec B110
            return

    def tool_executed(
        self,
        *,
        plugin_name: str,
        tool_name: str,
        success: bool,
        duration_ms: int,
        error_msg: str,
    ) -> None:
        """Emit ``PluginToolExecuted``."""
        if self._event_bus is None:
            return
        try:
            from sovyx.plugins.events import PluginToolExecuted
        except Exception:  # noqa: BLE001  # nosec B110
            return
        self._fire(
            PluginToolExecuted(
                plugin_name=plugin_name,
                tool_name=tool_name,
                success=success,
                duration_ms=duration_ms,
                error_message=error_msg,
            ),
        )

    def loaded(
        self,
        *,
        plugin_name: str,
        version: str,
        tools_count: int,
    ) -> None:
        """Emit ``PluginLoaded``."""
        if self._event_bus is None:
            return
        try:
            from sovyx.plugins.events import PluginLoaded
        except Exception:  # noqa: BLE001  # nosec B110
            return
        self._fire(
            PluginLoaded(
                plugin_name=plugin_name,
                plugin_version=version,
                tools_count=tools_count,
            ),
        )

    def unloaded(self, *, plugin_name: str, reason: str) -> None:
        """Emit ``PluginUnloaded``."""
        if self._event_bus is None:
            return
        try:
            from sovyx.plugins.events import PluginUnloaded
        except Exception:  # noqa: BLE001  # nosec B110
            return
        self._fire(
            PluginUnloaded(plugin_name=plugin_name, reason=reason),
        )

    def auto_disabled(self, *, plugin_name: str, health: _PluginHealth) -> None:
        """Emit ``PluginAutoDisabled``."""
        if self._event_bus is None:
            return
        try:
            from sovyx.plugins.events import PluginAutoDisabled
        except Exception:  # noqa: BLE001  # nosec B110
            return
        self._fire(
            PluginAutoDisabled(
                plugin_name=plugin_name,
                consecutive_failures=health.consecutive_failures,
                last_error=health.last_error,
            ),
        )
