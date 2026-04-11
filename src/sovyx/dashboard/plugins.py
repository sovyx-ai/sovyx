"""Dashboard plugin status endpoints.

Provides /api/plugins/* REST endpoints for plugin management
and status monitoring in the dashboard.

Ref: SPE-008 Appendix D.2
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from sovyx.plugins.manager import PluginManager

logger = get_logger(__name__)


def get_plugins_status(
    plugin_manager: PluginManager | None,
) -> dict[str, Any]:
    """Get status of all plugins.

    Returns:
        Dict with plugins list, counts, and health.
    """
    if plugin_manager is None:
        return {
            "available": False,
            "plugins": [],
            "total": 0,
            "active": 0,
            "disabled": 0,
        }

    plugins: list[dict[str, Any]] = []
    disabled_count = 0

    for name in plugin_manager.loaded_plugins:
        loaded = plugin_manager.get_plugin(name)
        if loaded is None:
            continue

        health = plugin_manager.get_plugin_health(name)
        is_disabled = plugin_manager.is_plugin_disabled(name)
        if is_disabled:
            disabled_count += 1

        plugin_info: dict[str, Any] = {
            "name": name,
            "version": loaded.plugin.version,
            "description": loaded.plugin.description,
            "tools_count": len(loaded.tools),
            "tools": [
                {
                    "name": t.name,
                    "description": t.description,
                }
                for t in loaded.tools
            ],
            "status": "disabled" if is_disabled else "active",
            "health": {
                "consecutive_failures": health["consecutive_failures"],
                "disabled": health["disabled"],
                "last_error": health["last_error"],
                "active_tasks": health["active_tasks"],
            },
        }
        plugins.append(plugin_info)

    return {
        "available": True,
        "plugins": plugins,
        "total": len(plugins),
        "active": len(plugins) - disabled_count,
        "disabled": disabled_count,
    }


def get_plugin_detail(
    plugin_manager: PluginManager | None,
    plugin_name: str,
) -> dict[str, Any] | None:
    """Get detailed info for a specific plugin.

    Returns:
        Plugin detail dict, or None if not found.
    """
    if plugin_manager is None:
        return None

    loaded = plugin_manager.get_plugin(plugin_name)
    if loaded is None:
        return None

    health = plugin_manager.get_plugin_health(plugin_name)
    is_disabled = plugin_manager.is_plugin_disabled(plugin_name)

    return {
        "name": plugin_name,
        "version": loaded.plugin.version,
        "description": loaded.plugin.description,
        "status": "disabled" if is_disabled else "active",
        "tools": [
            {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            }
            for t in loaded.tools
        ],
        "health": {
            "consecutive_failures": health["consecutive_failures"],
            "disabled": health["disabled"],
            "last_error": health["last_error"],
            "active_tasks": health["active_tasks"],
        },
        "manifest": {
            "has_manifest": loaded.manifest is not None,
        },
    }


def get_tools_list(
    plugin_manager: PluginManager | None,
) -> list[dict[str, Any]]:
    """Get flat list of all available tools across all plugins.

    Returns:
        List of tool dicts with plugin_name, name, description.
    """
    if plugin_manager is None:
        return []

    tools: list[dict[str, Any]] = []
    for name in plugin_manager.loaded_plugins:
        if plugin_manager.is_plugin_disabled(name):
            continue
        loaded = plugin_manager.get_plugin(name)
        if loaded is None:
            continue
        for t in loaded.tools:
            tools.append(
                {
                    "plugin": name,
                    "name": t.name,
                    "description": t.description,
                }
            )

    return tools
