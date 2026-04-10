"""Sovyx BridgeManager — communication ↔ cognitive integration.

Pipeline: InboundMessage → PersonResolver → ConversationTracker →
          Perception → CogLoopGate → ActionResult → OutboundMessage → Channel
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from typing import TYPE_CHECKING, Generic, Protocol, TypeVar

from sovyx.bridge.protocol import InboundMessage, OutboundMessage
from sovyx.cognitive.gate import CognitiveRequest
from sovyx.cognitive.perceive import Perception
from sovyx.engine.errors import CognitiveError
from sovyx.engine.types import PerceptionType
from sovyx.observability.logging import get_logger
from sovyx.observability.metrics import get_metrics

if TYPE_CHECKING:
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


class _LRULockDict(Generic[_K]):
    """Bounded dict of asyncio.Lock instances with LRU eviction.

    Prevents unbounded memory growth when conversation IDs are
    generated per-session (e.g. one per chat message over months).
    When *maxsize* is reached, the least-recently-used lock is evicted.
    """

    def __init__(self, maxsize: int = 500) -> None:
        self._maxsize = maxsize
        self._locks: OrderedDict[_K, asyncio.Lock] = OrderedDict()

    def setdefault(self, key: _K, default: asyncio.Lock) -> asyncio.Lock:
        """Get or insert a lock, promoting to most-recently-used."""
        if key in self._locks:
            self._locks.move_to_end(key)
            return self._locks[key]
        # Evict oldest if at capacity
        while len(self._locks) >= self._maxsize:
            self._locks.popitem(last=False)
        self._locks[key] = default
        return default

    def __len__(self) -> int:
        return len(self._locks)


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
    ) -> None: ...


class BridgeManager:
    """Manage communication channels and route messages.

    Pipeline (v13 audit fix — per-conversation lock):
    1. Resolve person + conversation (get conv_id)
    2. Lock per conversation (serialize same-user messages)
    3. Inside lock: reload history, build request, submit, record turns
    4. Send response via channel adapter
    """

    def __init__(
        self,
        event_bus: EventBus,
        cog_loop_gate: CogLoopGate,
        person_resolver: PersonResolver,
        conversation_tracker: ConversationTracker,
        mind_id: MindId,
    ) -> None:
        self._events = event_bus
        self._gate = cog_loop_gate
        self._resolver = person_resolver
        self._tracker = conversation_tracker
        self._mind_id = mind_id
        self._adapters: dict[ChannelType, ChannelAdapter] = {}
        self._conv_locks: _LRULockDict[ConversationId] = _LRULockDict(maxsize=500)

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
        for adapter in self._adapters.values():
            await adapter.stop()
        logger.info("bridge_stopped")

    async def handle_inbound(self, message: InboundMessage) -> None:
        """Process inbound message through the full pipeline.

        NEVER raises — all errors handled internally.
        """
        get_metrics().messages_received.add(
            1,
            {"channel": message.channel_type.value},
        )

        # Update dashboard counters (queryable, non-OTel)
        from sovyx.dashboard.status import get_counters

        get_counters().record_message()
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

                # Record assistant turn + send response
                await self._tracker.add_turn(conv_id, "assistant", result.response_text)
                get_counters().record_message()  # count AI response too
                outbound = OutboundMessage(
                    channel_type=message.channel_type,
                    target=message.chat_id,
                    text=result.response_text,
                    reply_to=result.reply_to,
                )
                await self._send_response(outbound)

        except Exception:
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
            except Exception:
                logger.warning("error_response_also_failed", exc_info=True)

    async def _send_response(self, outbound: OutboundMessage) -> None:
        """Find correct adapter and send."""
        adapter = self._get_adapter(outbound.channel_type)
        if adapter is None:
            logger.error(
                "no_adapter_for_channel",
                channel=outbound.channel_type.value,
            )
            return
        try:
            await adapter.send(
                outbound.target,
                outbound.text,
                reply_to=outbound.reply_to,
            )
        except Exception:
            logger.exception(
                "send_response_failed",
                channel=outbound.channel_type.value,
            )

    def _get_adapter(self, channel_type: ChannelType) -> ChannelAdapter | None:
        """Get adapter by channel type."""
        return self._adapters.get(channel_type)
