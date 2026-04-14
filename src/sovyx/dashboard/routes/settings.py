"""Engine settings GET/PUT endpoints."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from sovyx.dashboard.routes._deps import verify_token

router = APIRouter(prefix="/api", dependencies=[Depends(verify_token)])


@router.get("/settings")
async def get_settings(request: Request) -> JSONResponse:
    """Current engine settings."""
    from sovyx.dashboard.settings import get_settings as _get_settings
    from sovyx.engine.config import EngineConfig

    config = getattr(request.app.state, "engine_config", None)
    if config is None:
        try:
            config = EngineConfig()
        except Exception:  # noqa: BLE001
            return JSONResponse({"log_level": "INFO", "data_dir": str(Path.home() / ".sovyx")})

    return JSONResponse(_get_settings(config))


@router.put("/settings")
async def update_settings(request: Request) -> JSONResponse:
    """Update mutable settings (e.g. log_level)."""
    from sovyx.dashboard.settings import apply_settings

    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        return JSONResponse(
            {"ok": False, "error": "Invalid JSON body"},
            status_code=422,
        )

    if not isinstance(body, dict):
        return JSONResponse(
            {"ok": False, "error": "Expected JSON object"},
            status_code=422,
        )

    config = getattr(request.app.state, "engine_config", None)
    if config is None:
        from sovyx.engine.config import EngineConfig

        try:
            config = EngineConfig()
            request.app.state.engine_config = config
        except Exception:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": "no config"}, status_code=500)

    config_path = getattr(request.app.state, "config_path", None)
    changes = apply_settings(config, body, config_path=config_path)

    return JSONResponse({"ok": True, "changes": changes})
