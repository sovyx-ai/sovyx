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

# Fixed channel user ID for dashboard users — all dashboard sessions
# share the same person identity.  This simplifies v0.5 (single-user)
# while being extensible: v1.0 can add per-session user IDs.
_DASHBOARD_CHANNEL_USER_ID = "dashboard-user"

# Default timeout for cognitive loop submission (seconds).
_DEFAULT_TIMEOUT: float = 30.0


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
        ValueError: If message is empty or whitespace-only.
        CognitiveError: If the cognitive loop times out or fails.
        ServiceNotRegisteredError: If required services are not registered.
    """
    # ── Validate ──
    stripped = message.strip()
    if not stripped:
        msg = "Message cannot be empty"
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
    mind_id = bridge._mind_id  # noqa: SLF001

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

    # ── Submit to cognitive loop ──
    try:
        result = await gate.submit(request, timeout=timeout)
    except CognitiveError:
        logger.warning("dashboard_chat_gate_failed", exc_info=True)
        raise

    # ── Record turns ──
    await conversation_tracker.add_turn(conv_id, "user", stripped)

    response_text = ""
    if result is not None and not result.filtered:
        response_text = result.response_text
        if response_text:
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

    return {
        "response": response_text,
        "conversation_id": str(conv_id),
        "mind_id": str(mind_id),
        "timestamp": now.isoformat(),
    }
