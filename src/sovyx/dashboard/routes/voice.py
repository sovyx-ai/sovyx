"""Voice status + models endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from starlette.status import HTTP_503_SERVICE_UNAVAILABLE

from sovyx.dashboard.routes._deps import verify_token

router = APIRouter(prefix="/api/voice", dependencies=[Depends(verify_token)])


@router.get("/status")
async def get_voice_status_endpoint(request: Request) -> JSONResponse:
    """Voice pipeline status — running state, models, hardware tier."""
    registry = getattr(request.app.state, "registry", None)
    if registry is None:
        return JSONResponse(
            {"error": "Engine not running"},
            status_code=HTTP_503_SERVICE_UNAVAILABLE,
        )

    from sovyx.dashboard.voice_status import get_voice_status

    status = await get_voice_status(registry)
    return JSONResponse(status)


@router.get("/models")
async def get_voice_models_endpoint(request: Request) -> JSONResponse:
    """Available voice models by hardware tier, with detected/active info."""
    registry = getattr(request.app.state, "registry", None)
    if registry is None:
        return JSONResponse(
            {"error": "Engine not running"},
            status_code=HTTP_503_SERVICE_UNAVAILABLE,
        )

    from sovyx.dashboard.voice_status import get_voice_models

    models = await get_voice_models(registry)
    return JSONResponse(models)
