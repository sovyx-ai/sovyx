"""Dashboard plugin status endpoints.

Provides data functions for /api/plugins/* REST endpoints — plugin
management and status monitoring in the dashboard.

Each function takes a PluginManager and returns serializable dicts.
The FastAPI routes in server.py call these functions.

Ref: SPE-008 Appendix D.2, TASK-451
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from sovyx.plugins.manager import PluginManager
    from sovyx.plugins.manifest import PluginManifest

logger = get_logger(__name__)


def _serialize_manifest(
    manifest: PluginManifest | None,
) -> dict[str, Any]:
    """Serialize a PluginManifest to a JSON-safe dict.

    Returns an empty dict when manifest is None.
    """
    if manifest is None:
        return {}

    return {
        "name": manifest.name,
        "version": manifest.version,
        "description": manifest.description,
        "author": manifest.author,
        "license": manifest.license,
        "homepage": manifest.homepage,
        "min_sovyx_version": manifest.min_sovyx_version,
        "permissions": manifest.permissions,
        "network": {
            "allowed_domains": manifest.network.allowed_domains,
        },
        "depends": [{"name": d.name, "version": d.version} for d in manifest.depends],
        "optional_depends": [
            {"name": d.name, "version": d.version} for d in manifest.optional_depends
        ],
        "events": {
            "emits": [
                {"name": e.name, "description": e.description} for e in manifest.events.emits
            ],
            "subscribes": manifest.events.subscribes,
        },
        "tools": [{"name": t.name, "description": t.description} for t in manifest.tools],
        "config_schema": dict(manifest.config_schema),
        "category": manifest.category,
        "tags": list(manifest.tags),
        "icon_url": manifest.icon_url,
        "screenshots": list(manifest.screenshots),
        "pricing": manifest.pricing,
        "price_usd": manifest.price_usd,
        "trial_days": manifest.trial_days,
    }


def _permission_info(perm_str: str) -> dict[str, str]:
    """Get risk level and description for a permission string."""
    try:
        from sovyx.plugins.permissions import (
            Permission,
            get_description,
            get_risk,
        )

        perm = Permission(perm_str)
        return {
            "permission": perm_str,
            "risk": get_risk(perm),
            "description": get_description(perm),
        }
    except (ValueError, KeyError):
        return {
            "permission": perm_str,
            "risk": "medium",
            "description": perm_str,
        }


def get_plugins_status(
    plugin_manager: PluginManager | None,
) -> dict[str, Any]:
    """Get status of all plugins.

    Returns:
        Dict with plugins list, counts, health, and total tools.
    """
    if plugin_manager is None:
        return {
            "available": False,
            "plugins": [],
            "total": 0,
            "active": 0,
            "disabled": 0,
            "error": 0,
            "total_tools": 0,
        }

    plugins: list[dict[str, Any]] = []
    disabled_count = 0
    error_count = 0
    total_tools = 0

    for name in plugin_manager.loaded_plugins:
        loaded = plugin_manager.get_plugin(name)
        if loaded is None:
            continue

        health = plugin_manager.get_plugin_health(name)
        is_disabled = plugin_manager.is_plugin_disabled(name)
        is_error = (
            health["consecutive_failures"] > 0  # type: ignore[operator]
            and not is_disabled
        )

        if is_disabled:
            disabled_count += 1
        if is_error:
            error_count += 1

        status = "disabled" if is_disabled else ("error" if is_error else "active")
        tools_count = len(loaded.tools)
        total_tools += tools_count

        # Permission risk from manifest
        permissions: list[dict[str, str]] = []
        if loaded.manifest is not None:
            permissions = [_permission_info(p) for p in loaded.manifest.permissions]

        # Marketplace metadata from manifest
        category = ""
        tags: list[str] = []
        icon_url = ""
        pricing = "free"
        if loaded.manifest is not None:
            category = loaded.manifest.category
            tags = list(loaded.manifest.tags)
            icon_url = loaded.manifest.icon_url
            pricing = loaded.manifest.pricing

        plugin_info: dict[str, Any] = {
            "name": name,
            "version": loaded.plugin.version,
            "description": loaded.plugin.description,
            "status": status,
            "tools_count": tools_count,
            "tools": [
                {
                    "name": t.name,
                    "description": t.description,
                }
                for t in loaded.tools
            ],
            "permissions": permissions,
            "health": {
                "consecutive_failures": health["consecutive_failures"],
                "disabled": health["disabled"],
                "last_error": health["last_error"],
                "active_tasks": health["active_tasks"],
            },
            "category": category,
            "tags": tags,
            "icon_url": icon_url,
            "pricing": pricing,
        }
        plugins.append(plugin_info)

    return {
        "available": True,
        "plugins": plugins,
        "total": len(plugins),
        "active": len(plugins) - disabled_count - error_count,
        "disabled": disabled_count,
        "error": error_count,
        "total_tools": total_tools,
    }


def get_plugin_detail(
    plugin_manager: PluginManager | None,
    plugin_name: str,
) -> dict[str, Any] | None:
    """Get detailed info for a specific plugin.

    Returns:
        Plugin detail dict with tools, permissions, manifest, or None.
    """
    if plugin_manager is None:
        return None

    loaded = plugin_manager.get_plugin(plugin_name)
    if loaded is None:
        return None

    health = plugin_manager.get_plugin_health(plugin_name)
    is_disabled = plugin_manager.is_plugin_disabled(plugin_name)
    is_error = (
        health["consecutive_failures"] > 0  # type: ignore[operator]
        and not is_disabled
    )
    status = "disabled" if is_disabled else ("error" if is_error else "active")

    # Full permission info
    permissions: list[dict[str, str]] = []
    if loaded.manifest is not None:
        permissions = [_permission_info(p) for p in loaded.manifest.permissions]

    return {
        "name": plugin_name,
        "version": loaded.plugin.version,
        "description": loaded.plugin.description,
        "status": status,
        "tools": [
            {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
                "requires_confirmation": t.requires_confirmation,
                "timeout_seconds": t.timeout_seconds,
            }
            for t in loaded.tools
        ],
        "permissions": permissions,
        "health": {
            "consecutive_failures": health["consecutive_failures"],
            "disabled": health["disabled"],
            "last_error": health["last_error"],
            "active_tasks": health["active_tasks"],
        },
        "manifest": _serialize_manifest(loaded.manifest),
    }


def get_tools_list(
    plugin_manager: PluginManager | None,
) -> list[dict[str, Any]]:
    """Get flat list of all available tools across active plugins.

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
