"""Direct dashboard chat endpoint — /api/chat."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from starlette.status import HTTP_503_SERVICE_UNAVAILABLE

from sovyx.dashboard.routes._deps import verify_token
from sovyx.observability.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api", dependencies=[Depends(verify_token)])


@router.post("/chat")
async def chat(request: Request) -> JSONResponse:
    """Send a message and get AI response — no external channel needed.

    Request body:
        message (str): User message text. Required.
        user_name (str): Display name. Default "Dashboard".
        conversation_id (str|null): Continue existing conversation.

    Returns:
        JSON with response, conversation_id, mind_id, timestamp.
    """
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        return JSONResponse(
            {"error": "Invalid JSON body"},
            status_code=422,
        )

    if not isinstance(body, dict):
        return JSONResponse(
            {"error": "Expected JSON object"},
            status_code=422,
        )

    message_text = body.get("message")
    if not message_text or not isinstance(message_text, str) or not message_text.strip():
        return JSONResponse(
            {"error": "Field 'message' is required and must be a non-empty string"},
            status_code=422,
        )

    user_name = body.get("user_name", "Dashboard")
    if not isinstance(user_name, str):
        user_name = "Dashboard"

    conversation_id = body.get("conversation_id")
    if conversation_id is not None and not isinstance(conversation_id, str):
        return JSONResponse(
            {"error": "Field 'conversation_id' must be a string or null"},
            status_code=422,
        )

    registry = getattr(request.app.state, "registry", None)
    if registry is None:
        return JSONResponse(
            {"error": "Engine not running — no registry available"},
            status_code=HTTP_503_SERVICE_UNAVAILABLE,
        )

    from sovyx.dashboard.chat import handle_chat_message
    from sovyx.dashboard.status import get_counters

    get_counters().record_message()

    try:
        result = await handle_chat_message(
            registry=registry,
            message=message_text,
            user_name=user_name,
            conversation_id=conversation_id,
        )
    except ValueError:
        logger.warning("dashboard_chat_validation_failed", exc_info=True)
        return JSONResponse(
            {"error": "Invalid message format."},
            status_code=422,
        )
    except Exception:
        logger.exception("dashboard_chat_failed")
        return JSONResponse(
            {"error": "Failed to process message. Please try again."},
            status_code=500,
        )

    # Count AI response as a message too (user expects total, not just inbound).
    get_counters().record_message()

    # Broadcast chat event to WebSocket clients for real-time updates.
    ws_manager = getattr(request.app.state, "ws_manager", None)
    if ws_manager is not None:
        await ws_manager.broadcast(
            {
                "type": "ChatMessage",
                "timestamp": datetime.now(UTC).isoformat(),
                "data": {
                    "conversation_id": result["conversation_id"],
                    "response_preview": result["response"][:200] if result["response"] else "",
                },
            }
        )

    return JSONResponse(result)
