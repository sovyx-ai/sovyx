"""Structured JSON logs query endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from sovyx.dashboard.routes._deps import verify_token

router = APIRouter(prefix="/api", dependencies=[Depends(verify_token)])


@router.get("/logs")
async def get_logs(
    request: Request,
    level: str | None = None,
    module: str | None = None,
    search: str | None = None,
    after: str | None = None,
    limit: int = Query(default=100, ge=0, le=1000),
) -> JSONResponse:
    """Query structured JSON logs with filters.

    Use ``after`` (ISO-8601 timestamp) for incremental polling: only
    entries newer than the given timestamp are returned.
    """
    from sovyx.dashboard.logs import query_logs

    log_file = getattr(request.app.state, "log_file", None)
    entries = query_logs(
        log_file,
        level=level,
        module=module,
        search=search,
        after=after,
        limit=limit,
    )
    return JSONResponse({"entries": entries})
