"""Sovyx Plugin SDK — Extensions that let the Mind act in the world.

The Plugin SDK provides:
- ISovyxPlugin: Abstract base class for all plugins
- @tool: Decorator to expose methods as LLM-callable tools
- ToolDefinition: Schema for tool parameters
- Permission: Capability-based permission model

See SPE-008 for the full specification.
"""

from sovyx.plugins.context import BrainAccess, EventBusAccess, PluginContext
from sovyx.plugins.manager import PluginDisabledError, PluginError, PluginManager
from sovyx.plugins.permissions import Permission, PermissionDeniedError
from sovyx.plugins.sdk import ISovyxPlugin, ToolDefinition, tool
from sovyx.plugins.testing import MockPluginContext

__all__ = [
    "BrainAccess",
    "EventBusAccess",
    "ISovyxPlugin",
    "MockPluginContext",
    "Permission",
    "PermissionDeniedError",
    "PluginContext",
    "PluginDisabledError",
    "PluginError",
    "PluginManager",
    "ToolDefinition",
    "tool",
]
