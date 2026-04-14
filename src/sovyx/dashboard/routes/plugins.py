"""Plugin management endpoints."""

from __future__ import annotations

import contextlib

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.status import HTTP_503_SERVICE_UNAVAILABLE

from sovyx.dashboard.routes._deps import verify_token
from sovyx.observability.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/plugins", dependencies=[Depends(verify_token)])


@router.get("")
async def list_plugins(request: Request) -> JSONResponse:
    """List all plugins with status, health, and metadata."""
    from sovyx.dashboard.plugins import get_plugins_status
    from sovyx.plugins.manager import PluginManager

    plugin_manager: PluginManager | None = None
    registry = getattr(request.app.state, "registry", None)
    if registry is not None:
        with contextlib.suppress(Exception):
            plugin_manager = await registry.resolve(PluginManager)

    return JSONResponse(get_plugins_status(plugin_manager))


@router.get("/tools")
async def list_plugin_tools(request: Request) -> JSONResponse:
    """Flat list of all tools across active plugins."""
    from sovyx.dashboard.plugins import get_tools_list
    from sovyx.plugins.manager import PluginManager

    plugin_manager: PluginManager | None = None
    registry = getattr(request.app.state, "registry", None)
    if registry is not None:
        with contextlib.suppress(Exception):
            plugin_manager = await registry.resolve(PluginManager)

    return JSONResponse({"tools": get_tools_list(plugin_manager)})


@router.get("/{plugin_name}")
async def get_plugin_detail_route(request: Request, plugin_name: str) -> JSONResponse:
    """Detailed info for a specific plugin."""
    from sovyx.dashboard.plugins import get_plugin_detail
    from sovyx.plugins.manager import PluginManager

    plugin_manager: PluginManager | None = None
    registry = getattr(request.app.state, "registry", None)
    if registry is not None:
        with contextlib.suppress(Exception):
            plugin_manager = await registry.resolve(PluginManager)

    detail = get_plugin_detail(plugin_manager, plugin_name)
    if detail is None:
        raise HTTPException(status_code=404, detail="Plugin not found")
    return JSONResponse(detail)


@router.post("/{plugin_name}/enable")
async def enable_plugin_route(request: Request, plugin_name: str) -> JSONResponse:
    """Re-enable a disabled plugin."""
    from sovyx.plugins.manager import PluginError, PluginManager

    registry = getattr(request.app.state, "registry", None)
    if registry is None:
        return JSONResponse(
            {"ok": False, "error": "Engine not running"},
            status_code=HTTP_503_SERVICE_UNAVAILABLE,
        )

    try:
        plugin_manager: PluginManager = await registry.resolve(PluginManager)
    except Exception:  # noqa: BLE001
        return JSONResponse(
            {"ok": False, "error": "Plugin system not available"},
            status_code=HTTP_503_SERVICE_UNAVAILABLE,
        )

    try:
        plugin_manager.re_enable_plugin(plugin_name)
    except PluginError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    ws_manager = getattr(request.app.state, "ws_manager", None)
    if ws_manager is not None:
        await ws_manager.broadcast(
            {
                "type": "PluginStateChanged",
                "data": {
                    "plugin_name": plugin_name,
                    "from_state": "disabled",
                    "to_state": "active",
                },
            }
        )

    return JSONResponse({"ok": True, "plugin": plugin_name, "status": "active"})


@router.post("/{plugin_name}/disable")
async def disable_plugin_route(request: Request, plugin_name: str) -> JSONResponse:
    """Disable a loaded plugin (stops tools from being used)."""
    from sovyx.plugins.manager import PluginError, PluginManager

    registry = getattr(request.app.state, "registry", None)
    if registry is None:
        return JSONResponse(
            {"ok": False, "error": "Engine not running"},
            status_code=HTTP_503_SERVICE_UNAVAILABLE,
        )

    try:
        plugin_manager: PluginManager = await registry.resolve(PluginManager)
    except Exception:  # noqa: BLE001
        return JSONResponse(
            {"ok": False, "error": "Plugin system not available"},
            status_code=HTTP_503_SERVICE_UNAVAILABLE,
        )

    try:
        plugin_manager.disable_plugin(plugin_name)
    except PluginError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    ws_manager = getattr(request.app.state, "ws_manager", None)
    if ws_manager is not None:
        await ws_manager.broadcast(
            {
                "type": "PluginStateChanged",
                "data": {
                    "plugin_name": plugin_name,
                    "from_state": "active",
                    "to_state": "disabled",
                },
            }
        )

    return JSONResponse({"ok": True, "plugin": plugin_name, "status": "disabled"})


@router.post("/{plugin_name}/reload")
async def reload_plugin_route(request: Request, plugin_name: str) -> JSONResponse:
    """Reload a plugin (teardown + setup)."""
    from sovyx.plugins.manager import PluginError, PluginManager

    registry = getattr(request.app.state, "registry", None)
    if registry is None:
        return JSONResponse(
            {"ok": False, "error": "Engine not running"},
            status_code=HTTP_503_SERVICE_UNAVAILABLE,
        )

    try:
        plugin_manager: PluginManager = await registry.resolve(PluginManager)
    except Exception:  # noqa: BLE001
        return JSONResponse(
            {"ok": False, "error": "Plugin system not available"},
            status_code=HTTP_503_SERVICE_UNAVAILABLE,
        )

    try:
        await plugin_manager.reload(plugin_name)
    except PluginError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "plugin_reload_failed",
            plugin=plugin_name,
            error=str(exc),
        )
        return JSONResponse(
            {"ok": False, "error": f"Reload failed: {exc}"},
            status_code=500,
        )

    ws_manager = getattr(request.app.state, "ws_manager", None)
    if ws_manager is not None:
        await ws_manager.broadcast(
            {
                "type": "PluginStateChanged",
                "data": {
                    "plugin_name": plugin_name,
                    "from_state": "reloading",
                    "to_state": "active",
                },
            }
        )

    return JSONResponse({"ok": True, "plugin": plugin_name, "status": "reloaded"})
