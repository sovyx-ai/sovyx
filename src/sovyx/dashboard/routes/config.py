"""Mind configuration (personality, OCEAN, safety) GET/PUT endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from starlette.status import HTTP_503_SERVICE_UNAVAILABLE

from sovyx.dashboard.routes._deps import verify_token

router = APIRouter(prefix="/api", dependencies=[Depends(verify_token)])


@router.get("/config")
async def get_config(request: Request) -> JSONResponse:
    """Current mind configuration (personality, OCEAN, safety, brain, LLM)."""
    from sovyx.dashboard.config import get_config as _get_config

    mind_config = getattr(request.app.state, "mind_config", None)
    if mind_config is None:
        return JSONResponse(
            {"error": "No mind configuration loaded"},
            status_code=HTTP_503_SERVICE_UNAVAILABLE,
        )

    return JSONResponse(_get_config(mind_config))


@router.put("/config")
async def update_config(request: Request) -> JSONResponse:
    """Update mutable mind config (personality, OCEAN, safety, name, language, timezone)."""
    from sovyx.dashboard.config import apply_config

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

    mind_config = getattr(request.app.state, "mind_config", None)
    if mind_config is None:
        return JSONResponse(
            {"ok": False, "error": "No mind configuration loaded"},
            status_code=HTTP_503_SERVICE_UNAVAILABLE,
        )

    mind_yaml_path = getattr(request.app.state, "mind_yaml_path", None)
    changes = apply_config(mind_config, body, mind_yaml_path=mind_yaml_path)

    if changes:
        from sovyx.observability.audit import emit_config_change, parse_change_summary

        request_id = getattr(request.state, "request_id", None)
        for field_path, summary in changes.items():
            old, new = parse_change_summary(summary)
            emit_config_change(
                field_path,
                old_value_summary=old,
                new_value_summary=new,
                actor="user",
                request_id=request_id,
                source="dashboard",
            )

    ws_manager = getattr(request.app.state, "ws_manager", None)
    if changes and ws_manager is not None:
        await ws_manager.broadcast(
            {
                "type": "ConfigUpdated",
                "data": {"changes": changes},
            }
        )

        # Safety-specific event for targeted UI updates.
        safety_changes = {k: v for k, v in changes.items() if k.startswith("safety.")}
        if safety_changes:
            await ws_manager.broadcast(
                {
                    "type": "SafetyConfigUpdated",
                    "data": {"changes": safety_changes},
                }
            )

    return JSONResponse({"ok": True, "changes": changes})
