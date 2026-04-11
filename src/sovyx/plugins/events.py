"""Sovyx Plugin Events — events emitted by the plugin system.

These events are emitted on the engine EventBus during plugin
lifecycle transitions and tool executions. Dashboard subscribes
for real-time plugin monitoring.

Spec: SPE-008 §10 (Plugin Lifecycle)
"""

from __future__ import annotations

import dataclasses

from sovyx.engine.events import Event, EventCategory


@dataclasses.dataclass(frozen=True)
class PluginStateChanged(Event):
    """Emitted when a plugin transitions between lifecycle states."""

    plugin_name: str = ""
    from_state: str = ""
    to_state: str = ""
    error_message: str = ""

    @property
    def category(self) -> EventCategory:
        """Plugin events category."""
        return EventCategory.PLUGIN


@dataclasses.dataclass(frozen=True)
class PluginLoaded(Event):
    """Emitted when a plugin is successfully loaded and ready.

    Dashboard uses this for real-time plugin status updates.
    """

    plugin_name: str = ""
    plugin_version: str = ""
    tools_count: int = 0

    @property
    def category(self) -> EventCategory:
        """Plugin events category."""
        return EventCategory.PLUGIN


@dataclasses.dataclass(frozen=True)
class PluginUnloaded(Event):
    """Emitted when a plugin is unloaded.

    Dashboard uses this for real-time plugin status updates.
    """

    plugin_name: str = ""
    reason: str = ""

    @property
    def category(self) -> EventCategory:
        """Plugin events category."""
        return EventCategory.PLUGIN


@dataclasses.dataclass(frozen=True)
class PluginToolExecuted(Event):
    """Emitted after a plugin tool is executed."""

    plugin_name: str = ""
    tool_name: str = ""
    success: bool = True
    duration_ms: int = 0
    error_message: str = ""

    @property
    def category(self) -> EventCategory:
        """Plugin events category."""
        return EventCategory.PLUGIN


@dataclasses.dataclass(frozen=True)
class PluginAutoDisabled(Event):
    """Emitted when a plugin is auto-disabled after consecutive failures."""

    plugin_name: str = ""
    consecutive_failures: int = 0
    last_error: str = ""

    @property
    def category(self) -> EventCategory:
        """Plugin events category."""
        return EventCategory.PLUGIN
