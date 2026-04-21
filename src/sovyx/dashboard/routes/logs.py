"""Structured JSON logs query endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Path, Query, Request
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


@router.get("/logs/sagas/{saga_id}")
async def get_saga(
    request: Request,
    saga_id: str = Path(min_length=1, max_length=64),
    limit: int = Query(default=2000, ge=1, le=10000),
) -> JSONResponse:
    """Return every log entry that carries the given ``saga_id``.

    File-scan implementation (Phase 2 stop-gap). The FTS5 index in
    Phase 10 will replace the linear scan with an indexed lookup,
    but the response shape stays the same so dashboard callers don't
    need to change.

    Entries are returned chronologically (oldest first) so the saga
    reads as a story top-to-bottom. Returns an empty ``entries``
    array when the saga is unknown — distinguishing "no such saga"
    from "saga not yet emitted" is left to the caller (a fresh
    saga's first entry might be in the BackgroundLogWriter queue).
    """
    from sovyx.dashboard.logs import query_saga

    log_file = getattr(request.app.state, "log_file", None)
    entries = query_saga(log_file, saga_id, limit=limit)
    return JSONResponse({"saga_id": saga_id, "entries": entries})
