"""Direct dashboard chat endpoint — /api/chat and /api/chat/stream (SSE)."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from starlette.responses import StreamingResponse
from starlette.status import HTTP_503_SERVICE_UNAVAILABLE

from sovyx.dashboard.routes._deps import verify_token
from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from sovyx.cognitive.act import ActionResult

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
    except Exception:  # noqa: BLE001
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


@router.post("/chat/stream")
async def chat_stream(request: Request) -> StreamingResponse:
    """SSE streaming chat — tokens arrive as they are generated.

    Same request body as /api/chat. Returns text/event-stream with:
      event: phase   — cognitive phase transitions
      event: token   — individual text tokens
      event: done    — final metadata (response, conversation_id, tags, cost)
      event: error   — error message
    """
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        return _sse_error("Invalid JSON body")

    if not isinstance(body, dict):
        return _sse_error("Expected JSON object")

    message_text = body.get("message")
    if not message_text or not isinstance(message_text, str) or not message_text.strip():
        return _sse_error("Field 'message' is required")

    user_name = body.get("user_name", "Dashboard")
    if not isinstance(user_name, str):
        user_name = "Dashboard"

    conversation_id = body.get("conversation_id")
    registry = getattr(request.app.state, "registry", None)
    if registry is None:
        return _sse_error("Engine not running")

    async def generate() -> asyncio.AsyncIterator[str]:  # type: ignore[name-defined]
        from sovyx.bridge.identity import PersonResolver
        from sovyx.bridge.manager import BridgeManager
        from sovyx.bridge.sessions import ConversationTracker
        from sovyx.cognitive.gate import CognitiveRequest
        from sovyx.cognitive.loop import CognitiveLoop
        from sovyx.cognitive.perceive import Perception
        from sovyx.dashboard.status import get_counters
        from sovyx.engine.types import (
            ChannelType,
            ConversationId,
            PerceptionType,
            generate_id,
        )

        get_counters().record_message()

        try:
            person_resolver = await registry.resolve(PersonResolver)
            conversation_tracker = await registry.resolve(ConversationTracker)
            cogloop = await registry.resolve(CognitiveLoop)
            bridge = await registry.resolve(BridgeManager)
            mind_id = bridge.mind_id

            person_id = await person_resolver.resolve(
                ChannelType.DASHBOARD,
                "dashboard-user",
                user_name,
            )

            if conversation_id:
                conv_id = ConversationId(conversation_id)
                _, history = await conversation_tracker.get_or_create(
                    mind_id,
                    person_id,
                    ChannelType.DASHBOARD,
                )
            else:
                conv_id, history = await conversation_tracker.get_or_create(
                    mind_id,
                    person_id,
                    ChannelType.DASHBOARD,
                )

            stripped = message_text.strip()
            msg_id = generate_id()
            perception = Perception(
                id=msg_id,
                type=PerceptionType.USER_MESSAGE,
                source=ChannelType.DASHBOARD.value,
                content=stripped,
                person_id=person_id,
                metadata={"reply_to_message_id": msg_id, "chat_id": "dashboard-user"},
            )
            cog_request = CognitiveRequest(
                perception=perception,
                mind_id=mind_id,
                conversation_id=conv_id,
                conversation_history=history,
                person_name=user_name,
            )

            content_parts: list[str] = []
            start_time = datetime.now(UTC)

            async def on_token(text: str) -> None:
                content_parts.append(text)

            async def on_phase(phase: str, detail: str) -> None:
                pass  # Collected below via wrapper

            phase_events: list[tuple[str, str]] = []

            async def phase_callback(phase: str, detail: str) -> None:
                phase_events.append((phase, detail))

            # We need to yield phase events AND token events interleaved.
            # Strategy: run streaming in a background task, collect events
            # in queues, yield from the main generator.
            event_queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()

            async def token_callback(text: str) -> None:
                content_parts.append(text)
                await event_queue.put(("token", json.dumps({"text": text})))

            async def phase_cb(phase: str, detail: str) -> None:
                await event_queue.put(("phase", json.dumps({"phase": phase, "detail": detail})))

            async def run_loop() -> ActionResult:
                return await cogloop.process_request_streaming(  # type: ignore[no-any-return]
                    cog_request,
                    on_text_chunk=token_callback,
                    on_phase=phase_cb,
                )

            task = asyncio.create_task(run_loop())

            # Drain the event queue until the task completes
            while not task.done():
                try:
                    evt_type, evt_data = await asyncio.wait_for(
                        event_queue.get(),
                        timeout=0.1,
                    )
                    yield f"event: {evt_type}\ndata: {evt_data}\n\n"
                except TimeoutError:
                    continue

            # Drain remaining events
            while not event_queue.empty():
                evt_type, evt_data = event_queue.get_nowait()
                yield f"event: {evt_type}\ndata: {evt_data}\n\n"

            result = await task

            # Record turns
            await conversation_tracker.add_turn(conv_id, "user", stripped)
            response_text = result.response_text or ""

            if result.filtered:
                response_text = "I can't respond to that request."

            persist_tags: list[str] = []
            if result.tool_calls_made:
                persist_tags = sorted(
                    {
                        tc.function_name.split(".", 1)[0]
                        for tc in result.tool_calls_made
                        if tc.function_name
                    }
                )
            persist_tags.append("brain")

            if response_text:
                await conversation_tracker.add_turn(
                    conv_id,
                    "assistant",
                    response_text,
                    metadata={"tags": persist_tags},
                )

            get_counters().record_message()
            elapsed = int((datetime.now(UTC) - start_time).total_seconds() * 1000)

            llm_meta: dict[str, object] = {}
            if hasattr(result, "metadata") and result.metadata:
                for k in ("model", "tokens_in", "tokens_out", "cost_usd", "provider"):
                    if k in result.metadata:
                        llm_meta[k] = result.metadata[k]

            done_data = {
                "response": response_text,
                "conversation_id": str(conv_id),
                "mind_id": str(mind_id),
                "timestamp": datetime.now(UTC).isoformat(),
                "tags": persist_tags,
                "latency_ms": elapsed,
                **llm_meta,
            }
            yield f"event: done\ndata: {json.dumps(done_data)}\n\n"

        except Exception as exc:  # noqa: BLE001
            logger.exception("chat_stream_failed")
            yield f"event: error\ndata: {json.dumps({'error': str(exc)})}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


def _sse_error(message: str) -> StreamingResponse:
    """Return a single SSE error event."""

    async def gen() -> asyncio.AsyncIterator[str]:  # type: ignore[name-defined]
        yield f"event: error\ndata: {json.dumps({'error': message})}\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        status_code=200,
    )
