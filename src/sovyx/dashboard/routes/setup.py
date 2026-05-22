"""Plugin setup wizard endpoints — schema, test-connection, configure, enable, disable."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from sovyx.dashboard.routes._deps import verify_token
from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from sovyx.plugins.manager import PluginManager

logger = get_logger(__name__)

router = APIRouter(prefix="/api/setup", dependencies=[Depends(verify_token)])


class ConfigureRequest(BaseModel):
    """Request body for plugin configuration."""

    config: dict[str, object]


class SetupSchemaResponse(BaseModel):
    """Response of `GET /api/setup/{plugin_name}/schema` (Mission C C.4).

    Schema shape varies by plugin; typed as opaque dict via
    extra="allow"."""

    model_config = ConfigDict(extra="allow")
    schema_: dict[str, object] | None = None
    error: str | None = None


class SetupTestConnectionResponse(BaseModel):
    """Response of `POST /api/setup/{plugin_name}/test-connection`
    (Mission C C.4). Mirrors the LLM test-connection shape."""

    model_config = ConfigDict(extra="allow")
    ok: bool
    message: str | None = None
    latency_ms: float | None = None
    error: str | None = None


class SetupConfigureResponse(BaseModel):
    """Response of `POST /api/setup/{plugin_name}/configure` (Mission C C.4)."""

    model_config = ConfigDict(extra="allow")
    ok: bool
    plugin: str | None = None
    error: str | None = None


class SetupActionResponse(BaseModel):
    """Shared response of `POST /api/setup/{plugin_name}/{enable,disable}`
    (Mission C C.4)."""

    model_config = ConfigDict(extra="allow")
    ok: bool
    plugin: str | None = None
    status: str | None = None
    error: str | None = None


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


@router.get("/{plugin_name}/schema", response_model=SetupSchemaResponse)
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


@router.post("/{plugin_name}/test-connection", response_model=SetupTestConnectionResponse)
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


@router.post("/{plugin_name}/configure", response_model=SetupConfigureResponse)
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

    # Persist to mind.yaml (Phase 3.A Layer B — per-request resolution).
    from sovyx.dashboard._shared import resolve_mind_yaml_path_for_request

    _, mind_yaml_path, _ = await resolve_mind_yaml_path_for_request(request)
    if mind_yaml_path is not None:
        from sovyx.engine.config_editor import ConfigEditor

        editor = ConfigEditor()
        await editor.update_section(
            mind_yaml_path,
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


@router.post("/{plugin_name}/enable", response_model=SetupActionResponse)
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


@router.post("/{plugin_name}/disable", response_model=SetupActionResponse)
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
