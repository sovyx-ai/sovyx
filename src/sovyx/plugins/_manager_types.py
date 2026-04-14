"""Plugin manager support types — extracted from manager.py.

Moved here so the manager module can stay focused on the orchestration
class itself. Public API preserved via re-exports in ``manager.py``.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from sovyx.plugins.context import PluginContext
    from sovyx.plugins.manifest import PluginManifest
    from sovyx.plugins.permissions import PermissionEnforcer
    from sovyx.plugins.sdk import ISovyxPlugin, ToolDefinition
    from sovyx.plugins.security import ImportGuard


class PluginError(Exception):
    """Raised when a plugin operation fails."""


class PluginDisabledError(PluginError):
    """Raised when executing a tool on an auto-disabled plugin."""


@dataclasses.dataclass
class _PluginHealth:
    """Per-plugin health tracking."""

    consecutive_failures: int = 0
    disabled: bool = False
    last_error: str = ""
    active_tasks: int = 0


@dataclasses.dataclass
class LoadedPlugin:
    """A plugin that has been loaded and initialized."""

    plugin: ISovyxPlugin
    tools: list[ToolDefinition]
    context: PluginContext
    enforcer: PermissionEnforcer
    manifest: PluginManifest | None = None
    guard: ImportGuard | None = None
