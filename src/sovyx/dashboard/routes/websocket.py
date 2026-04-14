"""WebSocket /ws endpoint — real-time event stream."""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from sovyx.observability.logging import get_logger

logger = get_logger(__name__)

router = APIRouter()


@router.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    token: str | None = Query(default=None),
) -> None:
    """Real-time event stream.

    Auth via query param: ``/ws?token=<token>`` (WebSocket does not
    support the Authorization header easily).
    """
    expected = websocket.app.state.auth_token
    if not token or not secrets.compare_digest(token, expected):
        await websocket.close(code=4001, reason="Unauthorized")
        return

    ws_manager = websocket.app.state.ws_manager
    await ws_manager.connect(websocket)
    try:
        while True:
            # Keep connection alive, handle client pings.
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        logger.debug("ws_client_disconnected")
    finally:
        await ws_manager.disconnect(websocket)
