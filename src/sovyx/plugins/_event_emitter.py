"""Plugin lifecycle event emitter — single sink for all 4 event types.

Replaces the four ~25-line `_emit_*` methods that used to live inside
``PluginManager`` and shared the same try/except/loop boilerplate. Logic
is identical: best-effort fire-and-forget on the EventBus, swallowing
any failure (logging side-channels must never crash plugin execution).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from sovyx.engine.events import EventBus
    from sovyx.plugins._manager_types import _PluginHealth


class PluginEventEmitter:
    """Best-effort fire-and-forget emitter for plugin lifecycle events.

    All emit methods:
    - return immediately if no event bus is wired,
    - schedule the emit on the running loop (no-op if no loop),
    - swallow any exception (event emission must never crash callers).
    """

    def __init__(self, event_bus: EventBus | None) -> None:
        self._event_bus = event_bus

    def _fire(self, event: object) -> None:
        if self._event_bus is None:
            return
        try:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return  # No event loop — drop silently.
            loop.create_task(self._event_bus.emit(event))
        except Exception:  # noqa: BLE001  # nosec B110
            # Event emission must never crash the manager.
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
