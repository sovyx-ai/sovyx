"""Dashboard chat — direct conversation via HTTP.

POST /api/chat enables browser-based conversation without external channels.
Follows the same pipeline as BridgeManager: resolve person → track conversation →
build perception → submit to CogLoopGate → return response.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sovyx.cognitive.gate import CognitiveRequest
from sovyx.cognitive.perceive import Perception
from sovyx.engine.errors import CognitiveError
from sovyx.engine.types import (
    ChannelType,
    ConversationId,
    PerceptionType,
    generate_id,
)
from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from sovyx.engine.registry import ServiceRegistry

logger = get_logger(__name__)

# Track pending financial confirmations per conversation_id.
# Simple dict — acceptable for v0.6 (single-process, bounded by timeout).
_chat_pending_confirmations: dict[str, bool] = {}


async def _handle_chat_financial_callback(
    *,
    registry: ServiceRegistry,
    callback_data: str,
    conversation_id: str,
    mind_id: str,
) -> dict[str, Any]:
    """Resolve a financial confirmation callback from dashboard chat.

    Called when the chat message starts with ``fin_`` — this means the
    frontend sent a button callback_data instead of a user message.
    """
    is_confirm = callback_data.startswith(("fin_confirm:", "fin_confirm_all:"))
    is_cancel = callback_data.startswith(("fin_cancel:", "fin_cancel_all:"))

    if is_confirm:
        response_text = "✅ Financial action approved."
    elif is_cancel:
        response_text = "❌ Financial action cancelled."
    else:
        response_text = "⚠️ Unknown financial action."

    # Resolve in FinancialGate if available
    try:
        from sovyx.cognitive.financial_gate import FinancialGate

        fin_gate = await registry.resolve(FinancialGate)
        if is_confirm:
            pending = fin_gate.state.get_pending()
            if pending:
                fin_gate.state.confirm(pending.tool_call.id)
        elif is_cancel:
            fin_gate.state.cancel_all()
    except Exception:  # noqa: BLE001
        logger.debug("chat_financial_gate_not_available")

    # Clear pending state
    _chat_pending_confirmations.pop(conversation_id, None)

    logger.info(
        "dashboard_chat_financial_callback",
        callback=callback_data,
        action="confirm" if is_confirm else "cancel",
    )

    now = datetime.now(UTC)
    return {
        "response": response_text,
        "conversation_id": conversation_id,
        "mind_id": mind_id,
        "timestamp": now.isoformat(),
        "financial_resolved": True,
    }


# Fixed channel user ID for dashboard users — all dashboard sessions
# share the same person identity.  This simplifies v0.5 (single-user)
# while being extensible: v1.0 can add per-session user IDs.
_DASHBOARD_CHANNEL_USER_ID = "dashboard-user"

# Default timeout for cognitive loop submission (seconds).
_DEFAULT_TIMEOUT: float = 30.0
_MAX_MESSAGE_CHARS: int = 10_000  # Hard cap on chat input to bound LLM cost/context burn.


async def handle_chat_message(
    registry: ServiceRegistry,
    message: str,
    user_name: str = "Dashboard",
    conversation_id: str | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Process a chat message through the full cognitive pipeline.

    Mirrors BridgeManager.handle_inbound but returns the response directly
    instead of routing through a channel adapter.

    Args:
        registry: Service registry with PersonResolver, ConversationTracker,
            CogLoopGate registered.
        message: User message text.
        user_name: Display name for the dashboard user.
        conversation_id: Optional existing conversation to continue.
            If None, the ConversationTracker auto-creates one.
        timeout: Max wait for cognitive loop response.

    Returns:
        Dict with ``response``, ``conversation_id``, ``mind_id``, and
        ``timestamp`` keys.

    Raises:
        ValueError: If message is empty/whitespace-only or exceeds
            ``_MAX_MESSAGE_CHARS`` (10 000 characters).
        CognitiveError: If the cognitive loop times out or fails.
        ServiceNotRegisteredError: If required services are not registered.
    """
    # ── Validate ──
    stripped = message.strip()
    if not stripped:
        msg = "Message cannot be empty"
        raise ValueError(msg)
    if len(stripped) > _MAX_MESSAGE_CHARS:
        msg = f"Message too long ({len(stripped)} chars); limit is {_MAX_MESSAGE_CHARS:,}."
        raise ValueError(msg)

    # ── Resolve dependencies (lazy imports avoid circular deps) ──
    from sovyx.bridge.identity import PersonResolver
    from sovyx.bridge.manager import BridgeManager
    from sovyx.bridge.sessions import ConversationTracker
    from sovyx.cognitive.gate import CogLoopGate

    person_resolver = await registry.resolve(PersonResolver)
    conversation_tracker = await registry.resolve(ConversationTracker)
    gate = await registry.resolve(CogLoopGate)

    # v0.5: single-mind — get mind_id from BridgeManager
    bridge = await registry.resolve(BridgeManager)
    mind_id = bridge.mind_id

    # ── Resolve person (auto-create on first contact) ──
    person_id = await person_resolver.resolve(
        ChannelType.DASHBOARD,
        _DASHBOARD_CHANNEL_USER_ID,
        user_name,
    )

    # ── Get or create conversation ──
    if conversation_id is not None:
        conv_id = ConversationId(conversation_id)
        # Load history for the existing conversation
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

    # ── Build perception ──
    msg_id = generate_id()
    perception = Perception(
        id=msg_id,
        type=PerceptionType.USER_MESSAGE,
        source=ChannelType.DASHBOARD.value,
        content=stripped,
        person_id=person_id,
        metadata={
            "reply_to_message_id": msg_id,
            "chat_id": _DASHBOARD_CHANNEL_USER_ID,
        },
    )

    # ── Build cognitive request ──
    request = CognitiveRequest(
        perception=perception,
        mind_id=mind_id,
        conversation_id=conv_id,
        conversation_history=history,
        person_name=user_name,
    )

    # ── Financial callback shortcut ──
    # If the message is a callback_data from a button press, resolve it
    # without hitting the cognitive loop.
    if stripped.startswith("fin_"):
        return await _handle_chat_financial_callback(
            registry=registry,
            callback_data=stripped,
            conversation_id=str(conv_id),
            mind_id=str(mind_id),
        )

    # ── Submit to cognitive loop ──
    try:
        result = await gate.submit(request, timeout=timeout)
    except CognitiveError:
        logger.warning("dashboard_chat_gate_failed", exc_info=True)
        raise

    # ── Record turns ──
    await conversation_tracker.add_turn(conv_id, "user", stripped)

    response_text = ""
    buttons_payload: list[dict[str, str]] | None = None

    if result is not None and not result.filtered:
        response_text = result.response_text

        # ── Financial confirmation pending → include buttons ──
        if result.pending_confirmation and result.buttons:
            buttons_payload = []
            for row in result.buttons:
                for btn in row:
                    buttons_payload.append(
                        {
                            "text": getattr(btn, "text", str(btn)),
                            "callback_data": getattr(btn, "callback_data", ""),
                        }
                    )
            # Track pending for this conversation
            _chat_pending_confirmations[str(conv_id)] = True

        if response_text and not result.pending_confirmation:
            await conversation_tracker.add_turn(
                conv_id,
                "assistant",
                response_text,
            )

    # Handle error result gracefully
    if result is not None and result.error:
        response_text = result.response_text or "Something went wrong."
        logger.warning(
            "dashboard_chat_error_result",
            conversation_id=str(conv_id),
            response_preview=response_text[:100],
        )

    now = datetime.now(UTC)

    # Derive module/plugin tags surfaced to the chat UI.
    #
    # Tool names from the ReAct loop are already namespaced as
    # "plugin.tool" (enforced by PluginManager at sovyx/plugins/manager.py)
    # so the plugin name is the prefix before the first dot. Plugins are
    # deduplicated and sorted alphabetically for stable rendering, and
    # "brain" is always appended because the cognitive loop ran even
    # when tools did the heavy lifting (the LLM reasoned about which
    # tool to call, how to phrase the reply, etc.). Pending-confirmation,
    # degraded, and error paths all still carry tags — every response
    # the user sees should be traceable to the modules that produced it.
    plugin_tags: list[str] = []
    if result is not None and result.tool_calls_made:
        plugin_tags = sorted(
            {
                tc.function_name.split(".", 1)[0]
                for tc in result.tool_calls_made
                if tc.function_name
            }
        )
    tags: list[str] = [*plugin_tags, "brain"]

    resp: dict[str, Any] = {
        "response": response_text,
        "conversation_id": str(conv_id),
        "mind_id": str(mind_id),
        "timestamp": now.isoformat(),
        "tags": tags,
    }
    if buttons_payload:
        resp["buttons"] = buttons_payload
    if result is not None and result.pending_confirmation:
        resp["pending_confirmation"] = True
    return resp
