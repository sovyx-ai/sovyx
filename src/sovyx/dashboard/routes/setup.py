"""Plugin setup wizard endpoints — schema, test-connection, configure, enable, disable."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from sovyx.dashboard.routes._deps import verify_token
from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from sovyx.plugins.manager import PluginManager

logger = get_logger(__name__)

router = APIRouter(prefix="/api/setup", dependencies=[Depends(verify_token)])


class ConfigureRequest(BaseModel):
    """Request body for plugin configuration."""

    config: dict[str, object]


# ── Helpers ──────────────────────────────────────────────────────────


async def _get_manager(request: Request) -> PluginManager | None:
    registry = getattr(request.app.state, "registry", None)
    if registry is None:
        return None
    from sovyx.plugins.manager import PluginManager  # noqa: PLC0415

    if not registry.is_registered(PluginManager):
        return None
    mgr: PluginManager = await registry.resolve(PluginManager)
    return mgr


# ── Schema ───────────────────────────────────────────────────────────


@router.get("/{plugin_name}/schema")
async def get_setup_schema(
    request: Request,
    plugin_name: str,
) -> JSONResponse:
    """Return the setup wizard schema for a plugin.

    Returns the ``setup_schema`` declared on the plugin class,
    or ``null`` if the plugin requires no configuration.
    """
    manager = await _get_manager(request)
    if manager is None:
        return JSONResponse({"error": "plugin system not available"}, status_code=503)

    loaded = manager.get_plugin(plugin_name)
    if loaded is None:
        return JSONResponse({"error": f"plugin '{plugin_name}' not found"}, status_code=404)

    schema = getattr(loaded.plugin, "setup_schema", None)
    config_schema = getattr(loaded.plugin, "config_schema", None)

    return JSONResponse(
        {
            "plugin": plugin_name,
            "setup_schema": schema,
            "config_schema": config_schema,
            "current_config": loaded.context.config if loaded.context else {},
        }
    )


# ── Test Connection ──────────────────────────────────────────────────


@router.post("/{plugin_name}/test-connection")
async def test_connection(
    request: Request,
    plugin_name: str,
    body: ConfigureRequest,
) -> JSONResponse:
    """Validate plugin configuration without persisting.

    Calls the plugin's ``test_connection(config)`` method. Returns
    the result without saving anything to mind.yaml.
    """
    manager = await _get_manager(request)
    if manager is None:
        return JSONResponse({"error": "plugin system not available"}, status_code=503)

    loaded = manager.get_plugin(plugin_name)
    if loaded is None:
        return JSONResponse({"error": f"plugin '{plugin_name}' not found"}, status_code=404)

    try:
        result = await loaded.plugin.test_connection(dict(body.config))
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "test_connection_failed",
            plugin=plugin_name,
            error=str(exc),
        )
        return JSONResponse(
            {
                "success": False,
                "message": f"Test failed: {exc}",
            }
        )

    return JSONResponse(
        {
            "success": result.success,
            "message": result.message,
            "details": result.details,
        }
    )


# ── Configure ────────────────────────────────────────────────────────


@router.post("/{plugin_name}/configure")
async def configure_plugin(
    request: Request,
    plugin_name: str,
    body: ConfigureRequest,
) -> JSONResponse:
    """Save plugin configuration to mind.yaml and reconfigure the plugin.

    Flow:
        1. Persist config to ``mind.yaml`` under ``plugins_config.<name>.config``.
        2. Call ``PluginManager.reconfigure()`` to teardown + re-setup.
        3. Broadcast ``PluginStateChanged`` over WebSocket.
    """
    manager = await _get_manager(request)
    if manager is None:
        return JSONResponse({"error": "plugin system not available"}, status_code=503)

    loaded = manager.get_plugin(plugin_name)
    if loaded is None:
        return JSONResponse({"error": f"plugin '{plugin_name}' not found"}, status_code=404)

    new_config = dict(body.config)

    # Persist to mind.yaml
    mind_yaml_path = getattr(request.app.state, "mind_yaml_path", None)
    if mind_yaml_path is not None:
        from pathlib import Path

        from sovyx.engine.config_editor import ConfigEditor

        editor = ConfigEditor()
        await editor.update_section(
            Path(mind_yaml_path),
            f"plugins_config.{plugin_name}.config",
            new_config,
        )

    # Reconfigure the running plugin
    try:
        await manager.reconfigure(plugin_name, new_config)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "plugin_reconfigure_failed",
            plugin=plugin_name,
            error=str(exc),
        )
        return JSONResponse(
            {"ok": False, "error": f"Reconfigure failed: {exc}"},
            status_code=500,
        )

    # Broadcast state change
    ws_manager = getattr(request.app.state, "ws_manager", None)
    if ws_manager is not None:
        await ws_manager.broadcast(
            {
                "type": "PluginStateChanged",
                "data": {
                    "plugin": plugin_name,
                    "action": "configured",
                },
            }
        )

    logger.info("plugin_configured_via_wizard", plugin=plugin_name)
    return JSONResponse({"ok": True, "plugin": plugin_name})


# ── Enable / Disable ────────────────────────────────────────────────


@router.post("/{plugin_name}/enable")
async def enable_plugin(
    request: Request,
    plugin_name: str,
) -> JSONResponse:
    """Re-enable a disabled plugin."""
    manager = await _get_manager(request)
    if manager is None:
        return JSONResponse({"error": "plugin system not available"}, status_code=503)

    try:
        manager.re_enable_plugin(plugin_name)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    ws_manager = getattr(request.app.state, "ws_manager", None)
    if ws_manager is not None:
        await ws_manager.broadcast(
            {
                "type": "PluginStateChanged",
                "data": {"plugin": plugin_name, "action": "enabled"},
            }
        )

    return JSONResponse({"ok": True, "plugin": plugin_name, "action": "enabled"})


@router.post("/{plugin_name}/disable")
async def disable_plugin(
    request: Request,
    plugin_name: str,
) -> JSONResponse:
    """Disable a loaded plugin."""
    manager = await _get_manager(request)
    if manager is None:
        return JSONResponse({"error": "plugin system not available"}, status_code=503)

    try:
        manager.disable_plugin(plugin_name)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    ws_manager = getattr(request.app.state, "ws_manager", None)
    if ws_manager is not None:
        await ws_manager.broadcast(
            {
                "type": "PluginStateChanged",
                "data": {"plugin": plugin_name, "action": "disabled"},
            }
        )

    return JSONResponse({"ok": True, "plugin": plugin_name, "action": "disabled"})
