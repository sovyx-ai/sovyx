"""Unified activity timeline endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from sovyx.dashboard.routes._deps import verify_token

router = APIRouter(prefix="/api", dependencies=[Depends(verify_token)])


@router.get("/activity/timeline")
async def get_activity_timeline(
    request: Request,
    hours: int = Query(default=24, ge=1, le=168),
    limit: int = Query(default=100, ge=1, le=500),
) -> JSONResponse:
    """Unified cognitive activity timeline from persistent storage."""
    registry = getattr(request.app.state, "registry", None)
    if registry is not None:
        from sovyx.dashboard.activity import get_activity_timeline as _get_timeline

        timeline = await _get_timeline(registry, hours=hours, limit=limit)
        return JSONResponse(timeline)
    empty_meta = {"hours": hours, "limit": limit, "total_before_limit": 0, "cutoff": ""}
    return JSONResponse({"entries": [], "meta": empty_meta})
