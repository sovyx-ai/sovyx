"""Sovyx BridgeManager — communication ↔ cognitive integration.

Pipeline: InboundMessage → PersonResolver → ConversationTracker →
          Perception → CogLoopGate → ActionResult → OutboundMessage → Channel

Financial confirmation flow:
    When ActPhase returns ``pending_confirmation=True``, BridgeManager:
    1. Sends the confirmation message WITH inline buttons
    2. Records the pending state (message_id, chat_id, channel)
    3. On next inbound with matching callback_data (``fin_confirm:*`` / ``fin_cancel:*``):
       a. Resolves the confirmation via FinancialGate
       b. Edits the original message to show result (removes buttons)
       c. Does NOT submit to cognitive loop
"""

from __future__ import annotations

import asyncio
import dataclasses
from typing import TYPE_CHECKING, Protocol, TypeVar

from sovyx.bridge.protocol import InboundMessage, OutboundMessage
from sovyx.cognitive.gate import CognitiveRequest
from sovyx.cognitive.perceive import Perception
from sovyx.engine.errors import CognitiveError
from sovyx.engine.types import PerceptionType
from sovyx.observability.logging import get_logger
from sovyx.observability.metrics import get_metrics
from sovyx.observability.saga import async_saga_scope
from sovyx.observability.tasks import spawn

if TYPE_CHECKING:
    from sovyx.cognitive.financial_gate import FinancialGate
    from sovyx.cognitive.gate import CogLoopGate
    from sovyx.engine.events import EventBus
    from sovyx.engine.protocols import ChannelAdapter
    from sovyx.engine.types import (
        ChannelType,
        ConversationId,
        MindId,
        PersonId,
    )

logger = get_logger(__name__)

_K = TypeVar("_K")


# `_LRULockDict` was promoted to ``sovyx.engine._lock_dict.LRULockDict``
# so cloud/flex.py and cloud/usage.py can share the implementation. The
# leading-underscore alias is preserved here to avoid breaking any test
# that imported it from this module.
from sovyx.engine._lock_dict import LRULockDict as _LRULockDict  # noqa: E402, F401


class PersonResolver(Protocol):
    """Resolve channel user to PersonId."""

    async def resolve(
        self,
        channel_type: ChannelType,
        channel_user_id: str,
        display_name: str,
    ) -> PersonId: ...


class ConversationTracker(Protocol):
    """Track conversations per person."""

    async def get_or_create(
        self,
        mind_id: MindId,
        person_id: PersonId,
        channel_type: ChannelType,
    ) -> tuple[ConversationId, list[dict[str, str]]]: ...

    async def add_turn(
        self,
        conversation_id: ConversationId,
        role: str,
        content: str,
        *,
        metadata: dict[str, object] | None = None,
    ) -> None: ...


@dataclasses.dataclass(slots=True)
class _PendingConfirmationCtx:
    """Tracks a financial confirmation awaiting user response.

    Stored per chat_id so that callback queries from the same chat
    can be matched without hitting the cognitive loop.
    """

    message_id: str  # Sent message with buttons
    chat_id: str
    channel_type: ChannelType
    tool_call_ids: list[str]
    is_batch: bool = False


class BridgeManager:
    """Manage communication channels and route messages.

    Pipeline (v13 audit fix — per-conversation lock):
    1. Resolve person + conversation (get conv_id)
    2. Lock per conversation (serialize same-user messages)
    3. Inside lock: reload history, build request, submit, record turns
    4. Send response via channel adapter

    Financial confirmation:
    - Tracks pending confirmations per chat_id
    - Intercepts callback_data starting with ``fin_`` before cognitive loop
    - Edits original button message with confirmation result
    """

    def __init__(
        self,
        event_bus: EventBus,
        cog_loop_gate: CogLoopGate,
        person_resolver: PersonResolver,
        conversation_tracker: ConversationTracker,
        mind_id: MindId,
        financial_gate: FinancialGate | None = None,
    ) -> None:
        self._events = event_bus
        self._gate = cog_loop_gate
        self._resolver = person_resolver
        self._tracker = conversation_tracker
        self._mind_id = mind_id
        self._financial_gate = financial_gate
        self._adapters: dict[ChannelType, ChannelAdapter] = {}
        self._conv_locks: _LRULockDict[ConversationId] = _LRULockDict(maxsize=500)
        self._pending_confirmations: dict[str, _PendingConfirmationCtx] = {}

    @property
    def mind_id(self) -> MindId:
        """Public accessor for the active mind identifier."""
        return self._mind_id

    def register_channel(self, adapter: ChannelAdapter) -> None:
        """Register a channel adapter."""
        self._adapters[adapter.channel_type] = adapter
        logger.info(
            "channel_registered",
            channel=adapter.channel_type.value,
        )

        from sovyx.engine.events import ChannelConnected

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            spawn(
                self._events.emit(ChannelConnected(channel_type=adapter.channel_type.value)),
                name="bridge-channel-connected-emit",
            )

    async def start(self) -> None:
        """Start all registered channel adapters."""
        for adapter in self._adapters.values():
            await adapter.start()
        logger.info(
            "bridge_started",
            channels=len(self._adapters),
        )

    async def stop(self) -> None:
        """Stop all registered channel adapters."""
        from sovyx.engine.events import ChannelDisconnected

        for adapter in self._adapters.values():
            await adapter.stop()
            await self._events.emit(
                ChannelDisconnected(
                    channel_type=adapter.channel_type.value,
                    reason="shutdown",
                )
            )
        logger.info("bridge_stopped")

    async def handle_inbound(self, message: InboundMessage) -> None:
        """Process inbound message through the full pipeline.

        NEVER raises — all errors handled internally.

        Opens a ``bridge_message`` saga for the duration so every log
        emitted downstream (cognitive loop, brain, channel send) carries
        the same ``saga_id`` and the originating ``channel_id`` /
        ``channel_user_id`` — making it possible to reconstruct the full
        causal chain of a single inbound message from the logs.
        """
        async with async_saga_scope(
            "bridge_message",
            kind="bridge",
            binds={
                "channel_id": message.channel_type.value,
                "channel_user_id": message.channel_user_id,
            },
        ):
            await self._handle_inbound_inner(message)

    async def _handle_inbound_inner(self, message: InboundMessage) -> None:
        """Inner pipeline body — runs inside the ``bridge_message`` saga."""
        # ── Financial callback interception (before cognitive loop) ──
        callback = message.callback_data
        if callback and callback.startswith("fin_"):
            await self._handle_financial_callback(message)
            return

        get_metrics().messages_received.add(
            1,
            {"channel": message.channel_type.value},
        )

        # Update dashboard counters (queryable, non-OTel)
        from sovyx.dashboard.status import get_counters

        get_counters().record_message()

        # Emit PerceptionReceived for Live Feed
        from sovyx.engine.events import PerceptionReceived

        await self._events.emit(
            PerceptionReceived(
                source=message.channel_type.value,
                person_id="",
            )
        )

        try:
            # 1. Resolve person
            person_id = await self._resolver.resolve(
                message.channel_type,
                message.channel_user_id,
                message.display_name,
            )

            # 2. Get conversation (for conv_id and lock)
            conv_id, _ = await self._tracker.get_or_create(
                self._mind_id,
                person_id,
                message.channel_type,
            )

            # 3. Per-conversation lock (v13 fix)
            lock = self._conv_locks.setdefault(conv_id, asyncio.Lock())
            async with lock:
                # Re-load history inside lock (fresh)
                _, history = await self._tracker.get_or_create(
                    self._mind_id,
                    person_id,
                    message.channel_type,
                )

                # Build perception
                perception = Perception(
                    id=message.channel_message_id,
                    type=PerceptionType.USER_MESSAGE,
                    source=message.channel_type.value,
                    content=message.text,
                    person_id=person_id,
                    metadata={
                        "reply_to_message_id": message.channel_message_id,
                        "chat_id": message.chat_id,
                    },
                )

                # Build cognitive request
                request = CognitiveRequest(
                    perception=perception,
                    mind_id=self._mind_id,
                    conversation_id=conv_id,
                    conversation_history=history,
                    person_name=message.display_name or None,
                )

                # Submit to cognitive loop
                try:
                    result = await self._gate.submit(request, timeout=30.0)
                except CognitiveError:
                    logger.warning(
                        "gate_submit_failed",
                        exc_info=True,
                    )
                    result = None

                # Record user turn (ALWAYS)
                await self._tracker.add_turn(conv_id, "user", message.text)

                # If filtered or failed, don't send response
                if result is None or result.error:
                    fallback = result.response_text if result else "Something went wrong."
                    outbound = OutboundMessage(
                        channel_type=message.channel_type,
                        target=message.chat_id,
                        text=fallback,
                        reply_to=message.channel_message_id,
                    )
                    await self._send_response(outbound)
                    get_counters().record_message()  # count fallback response
                    return

                if result.filtered:
                    return

                # ── Financial confirmation pending → send with buttons ──
                if result.pending_confirmation:
                    outbound = OutboundMessage(
                        channel_type=message.channel_type,
                        target=message.chat_id,
                        text=result.response_text,
                        reply_to=message.channel_message_id,
                        buttons=result.buttons,
                    )
                    sent_id = await self._send_response(outbound)
                    if sent_id and result.confirmation_details:
                        # Track pending so callback can resolve it
                        details = result.confirmation_details
                        raw_ids = details.get("tool_call_ids")
                        if isinstance(raw_ids, list):
                            tc_ids = [str(i) for i in raw_ids]
                        elif "tool_call_id" in details:
                            tc_ids = [str(details["tool_call_id"])]
                        else:
                            tc_ids = []
                        self._pending_confirmations[message.chat_id] = _PendingConfirmationCtx(
                            message_id=sent_id,
                            chat_id=message.chat_id,
                            channel_type=message.channel_type,
                            tool_call_ids=tc_ids,
                            is_batch="count" in details,
                        )
                    get_counters().record_message()
                    return

                # Record assistant turn + send response
                bridge_tags: list[str] = []
                if result.tool_calls_made:
                    bridge_tags = sorted(
                        {
                            tc.function_name.split(".", 1)[0]
                            for tc in result.tool_calls_made
                            if tc.function_name
                        }
                    )
                bridge_tags.append("brain")
                await self._tracker.add_turn(
                    conv_id,
                    "assistant",
                    result.response_text,
                    metadata={"tags": bridge_tags},
                )
                get_counters().record_message()  # count AI response too
                outbound = OutboundMessage(
                    channel_type=message.channel_type,
                    target=message.chat_id,
                    text=result.response_text,
                    reply_to=result.reply_to,
                )
                await self._send_response(outbound)

                # Emit ResponseSent for Live Feed
                from sovyx.engine.events import ResponseSent

                await self._events.emit(
                    ResponseSent(
                        mind_id=str(self._mind_id),
                        channel=message.channel_type.value,
                    )
                )

        except Exception:  # noqa: BLE001
            logger.exception("handle_inbound_failed")
            # Best-effort error response so user doesn't get silence
            try:
                error_out = OutboundMessage(
                    channel_type=message.channel_type,
                    target=message.chat_id,
                    text="Something went wrong processing your message. Please try again.",
                    reply_to=message.channel_message_id,
                )
                await self._send_response(error_out)
                get_counters().record_message()  # count crash-path error response
            except Exception:  # noqa: BLE001 — error-response path must not itself raise
                logger.warning("error_response_also_failed", exc_info=True)

    async def _handle_financial_callback(self, message: InboundMessage) -> None:
        """Handle a financial confirmation/cancellation callback.

        Resolves the pending confirmation, edits the original message
        to remove buttons and show the result, and does NOT submit
        to the cognitive loop.
        """
        callback = message.callback_data
        if not callback:
            return

        chat_id = message.chat_id
        ctx = self._pending_confirmations.pop(chat_id, None)

        # Determine action
        is_confirm = callback.startswith(("fin_confirm:", "fin_confirm_all:"))
        is_cancel = callback.startswith(("fin_cancel:", "fin_cancel_all:"))

        if not is_confirm and not is_cancel:
            logger.warning("financial_callback_unknown", callback=callback)
            return

        # Resolve in FinancialGate state
        if self._financial_gate:
            if is_confirm:
                # Confirm all pending tool calls
                pending = self._financial_gate.state.get_pending()
                if pending:
                    self._financial_gate.state.confirm(pending.tool_call.id)
            elif is_cancel:
                self._financial_gate.state.cancel_all()

        # Build response text
        if is_confirm:
            response_text = "✅ Financial action approved."
            logger.info("financial_callback_approved", chat_id=chat_id)
        else:
            response_text = "❌ Financial action cancelled."
            logger.info("financial_callback_cancelled", chat_id=chat_id)

        # Edit original message to remove buttons + show result
        if ctx:
            edit_outbound = OutboundMessage(
                channel_type=message.channel_type,
                target=chat_id,
                text=response_text,
                edit_message_id=ctx.message_id,
                buttons=None,  # Remove buttons
            )
            await self._send_response(edit_outbound)
        else:
            # No context found — send as new message
            outbound = OutboundMessage(
                channel_type=message.channel_type,
                target=chat_id,
                text=response_text,
            )
            await self._send_response(outbound)

    async def _send_response(self, outbound: OutboundMessage) -> str | None:
        """Find correct adapter and send. Returns message ID or None."""
        adapter = self._get_adapter(outbound.channel_type)
        if adapter is None:
            logger.error(
                "no_adapter_for_channel",
                channel=outbound.channel_type.value,
            )
            return None
        try:
            if outbound.edit_message_id:
                await adapter.edit(
                    outbound.edit_message_id,
                    outbound.text,
                    buttons=outbound.buttons,
                    target=outbound.target,
                )
                return outbound.edit_message_id
            return await adapter.send(
                outbound.target,
                outbound.text,
                reply_to=outbound.reply_to,
                buttons=outbound.buttons,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "send_response_failed",
                channel=outbound.channel_type.value,
            )
            return None

    def _get_adapter(self, channel_type: ChannelType) -> ChannelAdapter | None:
        """Get adapter by channel type."""
        return self._adapters.get(channel_type)
