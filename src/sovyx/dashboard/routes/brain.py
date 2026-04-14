"""Brain graph + search endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from sovyx.dashboard.routes._deps import verify_token

router = APIRouter(prefix="/api", dependencies=[Depends(verify_token)])


@router.get("/brain/graph")
async def get_brain_graph(
    request: Request,
    limit: int = Query(default=200, ge=0, le=1000),
) -> JSONResponse:
    """Brain knowledge graph (nodes + links for react-force-graph-2d)."""
    registry = getattr(request.app.state, "registry", None)
    if registry is not None:
        from sovyx.dashboard.brain import get_brain_graph as _get_graph

        graph = await _get_graph(registry, limit=limit)
        return JSONResponse(graph)
    return JSONResponse({"nodes": [], "links": []})


@router.get("/brain/search")
async def brain_search(
    request: Request,
    q: str = Query(default="", max_length=500),
    limit: int = Query(default=20, ge=1, le=100),
) -> JSONResponse:
    """Semantic search over brain concepts (hybrid FTS+vector)."""
    registry = getattr(request.app.state, "registry", None)
    if registry is not None:
        from sovyx.dashboard.brain import search_brain

        results = await search_brain(registry, q, limit=limit)
        return JSONResponse({"results": results, "query": q})
    return JSONResponse({"results": [], "query": q})
