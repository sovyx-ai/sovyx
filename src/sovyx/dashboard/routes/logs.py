"""Structured log query endpoints — FTS5-backed search + saga reconstruction.

Replaces the linear file-scan endpoints from Phase 2 with FTS5
queries when :class:`sovyx.observability.fts_index.FTSIndexer` is
available on the application state. The legacy file-scan helpers
(``query_logs`` / ``query_saga``) are kept as fall-backs so the
dashboard keeps working in deployments where the FTS sidecar has
not been wired yet (Task 10.2 introduces the routes; the bootstrap
wireup arrives in a later task).

Endpoints
---------
    * ``GET /api/logs`` — legacy filter (kept for current dashboard).
    * ``GET /api/logs/search`` — FTS5 MATCH query with structured
      filters; returns snippets.
    * ``GET /api/logs/sagas/{saga_id}`` — every entry tagged with
      *saga_id*, chronological.
    * ``GET /api/logs/sagas/{saga_id}/story`` — localized narrative
      rendered from the saga (introduced in Task 8.3).
    * ``GET /api/logs/sagas/{saga_id}/causality`` — list of
      cause→event edges for graph rendering.
    * ``GET /api/logs/anomalies`` — recent ``anomaly.*`` events.
    * ``WS /api/logs/stream`` — real-time tail with filters.

All routes share the ``verify_token`` dependency.

Aligned with IMPL-OBSERVABILITY-001 §16 (Phase 10, Task 10.2).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from typing import Literal

from fastapi import APIRouter, Depends, Path, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from sovyx.dashboard.routes._deps import verify_token

router = APIRouter(prefix="/api", dependencies=[Depends(verify_token)])

# A WebSocket router separately because Depends() on WS routes was
# only properly wired in newer FastAPI versions; we re-apply
# verify_token manually inside the handler instead.
ws_router = APIRouter(prefix="/api")


# ── Legacy file-scan endpoints (Phase 2 contract) ──────────────────


@router.get("/logs")
async def get_logs(
    request: Request,
    level: str | None = None,
    module: str | None = None,
    search: str | None = None,
    after: str | None = None,
    limit: int = Query(default=100, ge=0, le=1000),
) -> JSONResponse:
    """Legacy filter — returns rows from the JSONL file scan.

    Unchanged so the current dashboard log viewer keeps working
    while Task 10.4 swaps the front-end to ``/api/logs/search``.
    Use ``after`` (ISO-8601) for incremental polling.
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


# ── FTS5 search ────────────────────────────────────────────────────


@router.get("/logs/search")
async def search_logs(
    request: Request,
    q: str = Query(default=""),
    level: str | None = None,
    logger_name: str | None = Query(default=None, alias="logger"),
    saga_id: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
) -> JSONResponse:
    """Full-text search backed by the FTS5 sidecar.

    Returns 503 when the indexer is not wired into application
    state, so the dashboard can fall back to the legacy
    ``GET /api/logs`` endpoint without ambiguity.
    """
    indexer = getattr(request.app.state, "fts_indexer", None)
    if indexer is None:
        return JSONResponse(
            {"error": "fts_indexer not configured", "fallback": "/api/logs"},
            status_code=503,
        )

    since_unix = _iso_to_unix(since)
    until_unix = _iso_to_unix(until)

    rows = await indexer.search(
        q,
        level=level,
        logger_name=logger_name,
        saga_id=saga_id,
        since_unix=since_unix,
        until_unix=until_unix,
        limit=limit,
    )
    return JSONResponse(
        {
            "query": q,
            "filters": {
                "level": level,
                "logger": logger_name,
                "saga_id": saga_id,
                "since": since,
                "until": until,
            },
            "count": len(rows),
            "entries": rows,
        }
    )


# ── Saga endpoints ─────────────────────────────────────────────────


@router.get("/logs/sagas/{saga_id}")
async def get_saga(
    request: Request,
    saga_id: str = Path(min_length=1, max_length=64),
    limit: int = Query(default=2000, ge=1, le=10000),
) -> JSONResponse:
    """Return every log entry that carries *saga_id*, chronological.

    Prefers the FTS sidecar when available (indexed lookup, sub-ms);
    falls back to the file-scan helper from Phase 2 so the route
    keeps working before the indexer is wired into bootstrap.
    """
    indexer = getattr(request.app.state, "fts_indexer", None)
    if indexer is not None:
        rows = await indexer.search(
            "",
            saga_id=saga_id,
            limit=limit,
        )
        rows.sort(key=lambda r: r.get("timestamp") or "")
        return JSONResponse({"saga_id": saga_id, "entries": rows})

    from sovyx.dashboard.logs import query_saga

    log_file = getattr(request.app.state, "log_file", None)
    entries = query_saga(log_file, saga_id, limit=limit)
    return JSONResponse({"saga_id": saga_id, "entries": entries})


@router.get("/logs/sagas/{saga_id}/story")
async def get_saga_story(
    request: Request,
    saga_id: str = Path(min_length=1, max_length=64),
    locale: Literal["pt-BR", "en-US"] = Query(default="pt-BR"),
) -> JSONResponse:
    """Render a saga as a human-readable storyline (pt-BR or en-US).

    Delegates to :func:`sovyx.observability.narrative.build_user_journey`,
    which streams the structured log file, filters entries by
    ``saga_id``, sorts chronologically, and renders each known event
    via a localized template.
    """
    from pathlib import Path as _Path

    from sovyx.observability.narrative import build_user_journey

    log_file = getattr(request.app.state, "log_file", None)
    if log_file is None:
        return JSONResponse(
            {"saga_id": saga_id, "story": "(log file not configured)", "locale": locale}
        )
    story = build_user_journey(saga_id, _Path(str(log_file)), locale=locale)
    return JSONResponse({"saga_id": saga_id, "story": story, "locale": locale})


@router.get("/logs/sagas/{saga_id}/causality")
async def get_saga_causality(
    request: Request,
    saga_id: str = Path(min_length=1, max_length=64),
    limit: int = Query(default=2000, ge=1, le=10000),
) -> JSONResponse:
    """Return the cause→event edges of *saga_id* for graph rendering.

    Each edge is ``{"id": <span_id>, "event": <event_name>,
    "cause_id": <parent>, "timestamp": <iso>}``. The frontend
    layouts the DAG with ``cause_id`` as the parent pointer; root
    nodes have ``cause_id=None``. Returns ``edges=[]`` for unknown
    sagas — distinguishing "no such saga" from "saga not yet
    indexed" is the caller's job.
    """
    rows = await _gather_saga_rows(request, saga_id, limit)
    edges: list[dict[str, object]] = []
    for row in rows:
        envelope = _maybe_parse_content(row)
        edges.append(
            {
                "id": envelope.get("span_id"),
                "event": row.get("event") or envelope.get("event"),
                "cause_id": envelope.get("cause_id"),
                "logger": row.get("logger"),
                "timestamp": row.get("timestamp"),
                "level": row.get("level"),
            }
        )
    return JSONResponse({"saga_id": saga_id, "edges": edges})


# ── Anomalies ──────────────────────────────────────────────────────


@router.get("/logs/anomalies")
async def get_anomalies(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    since: str | None = None,
) -> JSONResponse:
    """Return recent ``anomaly.*`` events emitted by the AnomalyDetector.

    Anomaly events use the prefix ``anomaly.`` (first_occurrence,
    latency_spike, error_rate_spike, memory_growth). Search via FTS
    when available; fall back to a file scan otherwise.
    """
    since_unix = _iso_to_unix(since)

    indexer = getattr(request.app.state, "fts_indexer", None)
    if indexer is not None:
        rows = await indexer.search(
            "anomaly*",
            since_unix=since_unix,
            limit=limit,
        )
        return JSONResponse({"count": len(rows), "entries": rows})

    from sovyx.dashboard.logs import query_logs

    log_file = getattr(request.app.state, "log_file", None)
    entries = query_logs(
        log_file,
        search="anomaly.",
        after=since,
        limit=limit,
    )
    return JSONResponse({"count": len(entries), "entries": entries})


# ── WebSocket tail ─────────────────────────────────────────────────


@ws_router.websocket("/logs/stream")
async def stream_logs(websocket: WebSocket) -> None:
    """Real-time log tail with optional filters.

    Query params (forwarded as filters):
        ``level``, ``logger``, ``saga_id``, ``q``.

    Auth is enforced by reading ``token`` from the query string
    (FastAPI's ``Depends(verify_token)`` does not flow into
    WebSocket routes reliably across versions). The token must
    match ``request.app.state.auth_token`` set by
    :func:`sovyx.dashboard.server.create_app`.
    """
    expected = getattr(websocket.app.state, "auth_token", None)
    provided = websocket.query_params.get("token")
    if expected is not None and provided != expected:
        await websocket.close(code=4401)
        return

    await websocket.accept()

    indexer = getattr(websocket.app.state, "fts_indexer", None)
    if indexer is None:
        await websocket.send_json({"type": "error", "message": "fts_indexer not configured"})
        await websocket.close(code=4503)
        return

    level = websocket.query_params.get("level")
    logger_name = websocket.query_params.get("logger")
    saga_id = websocket.query_params.get("saga_id")
    q = websocket.query_params.get("q") or ""

    cursor_unix = time.time()
    poll_interval = 0.5

    try:
        while True:
            rows = await indexer.search(
                q,
                level=level,
                logger_name=logger_name,
                saga_id=saga_id,
                since_unix=cursor_unix,
                limit=200,
            )
            if rows:
                rows.sort(key=lambda r: r.get("timestamp") or "")
                await websocket.send_json({"type": "batch", "entries": rows})
                last = rows[-1].get("timestamp")
                last_unix = _iso_to_unix(last)
                if last_unix is not None:
                    cursor_unix = last_unix + 0.001
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=poll_interval)
            except asyncio.TimeoutError:
                continue
    except WebSocketDisconnect:
        return
    except Exception:  # noqa: BLE001 — never let a stream error blow up the loop.
        with contextlib.suppress(Exception):
            await websocket.close(code=1011)


# ── Helpers ────────────────────────────────────────────────────────


def _iso_to_unix(value: str | None) -> float | None:
    """Parse an ISO-8601 timestamp into a unix epoch float, or None."""
    if not value:
        return None
    try:
        from datetime import datetime  # noqa: PLC0415 — single-call.

        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


async def _gather_saga_rows(request: Request, saga_id: str, limit: int) -> list[dict[str, object]]:
    """Return saga rows via FTS when available, else legacy file scan."""
    indexer = getattr(request.app.state, "fts_indexer", None)
    if indexer is not None:
        rows = await indexer.search("", saga_id=saga_id, limit=limit)
        rows.sort(key=lambda r: r.get("timestamp") or "")
        return rows

    from sovyx.dashboard.logs import query_saga

    log_file = getattr(request.app.state, "log_file", None)
    return query_saga(log_file, saga_id, limit=limit)


def _maybe_parse_content(row: dict[str, object]) -> dict[str, object]:
    """Return the embedded envelope (parsed ``content`` JSON), or an empty dict.

    FTS rows ship the original JSON line as ``content``; legacy
    file-scan rows don't carry that field. We tolerate both shapes
    so callers don't need to branch on the data source.
    """
    content = row.get("content")
    if not isinstance(content, str):
        return {}
    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}
