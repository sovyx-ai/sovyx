"""Conversation list + detail endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from sovyx.dashboard.routes._deps import verify_token

router = APIRouter(prefix="/api", dependencies=[Depends(verify_token)])


@router.get("/conversations")
async def get_conversations(
    request: Request,
    limit: int = Query(default=50, ge=0, le=500),
    offset: int = Query(default=0, ge=0),
) -> JSONResponse:
    """List conversations ordered by most recent activity."""
    registry = getattr(request.app.state, "registry", None)
    if registry is not None:
        from sovyx.dashboard.conversations import list_conversations

        convos = await list_conversations(registry, limit=limit, offset=offset)
        return JSONResponse({"conversations": convos})
    return JSONResponse({"conversations": []})


@router.get("/conversations/{conversation_id}")
async def get_conversation_detail(
    request: Request,
    conversation_id: str,
    limit: int = Query(default=100, ge=0, le=1000),
) -> JSONResponse:
    """Get messages for a specific conversation."""
    registry = getattr(request.app.state, "registry", None)
    if registry is not None:
        from sovyx.dashboard.conversations import get_conversation_messages

        messages = await get_conversation_messages(
            registry,
            conversation_id,
            limit=limit,
        )
        return JSONResponse({"conversation_id": conversation_id, "messages": messages})
    return JSONResponse({"conversation_id": conversation_id, "messages": []})
