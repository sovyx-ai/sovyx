"""Sovyx event system — typed events and async event bus.

All events are frozen dataclasses (immutable). The EventBus dispatches
events to registered handlers with error isolation and correlation ID
propagation.
"""

from __future__ import annotations

import dataclasses
from collections import defaultdict
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from enum import Enum, auto
from uuid import uuid4

from sovyx.observability.logging import get_logger, set_correlation_id

logger = get_logger(__name__)


# ── Event Categories ────────────────────────────────────────────────────────


class EventCategory(Enum):
    """Categories for event classification."""

    ENGINE = auto()
    COGNITIVE = auto()
    BRAIN = auto()
    VOICE = auto()
    BRIDGE = auto()
    PLUGIN = auto()
    SECURITY = auto()


# ── Base Event ──────────────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class Event:
    """Base for all system events.

    Events are immutable (frozen dataclass). Each has a unique ID,
    UTC timestamp, and optional correlation_id for request tracing.
    """

    event_id: str = dataclasses.field(default_factory=lambda: str(uuid4()))
    timestamp: datetime = dataclasses.field(default_factory=lambda: datetime.now(UTC))
    correlation_id: str = ""

    @property
    def category(self) -> EventCategory:
        """Event category for classification. Must be overridden."""
        raise NotImplementedError


# ── Engine Events ───────────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class EngineStarted(Event):
    """Emitted when the engine finishes startup."""

    version: str = ""
    mind_count: int = 0

    @property
    def category(self) -> EventCategory:
        """Event category."""
        return EventCategory.ENGINE


@dataclasses.dataclass(frozen=True)
class EngineStopping(Event):
    """Emitted when the engine begins shutdown."""

    reason: str = ""

    @property
    def category(self) -> EventCategory:
        """Event category."""
        return EventCategory.ENGINE


@dataclasses.dataclass(frozen=True)
class ServiceHealthChanged(Event):
    """Emitted when a service health status changes."""

    service: str = ""
    status: str = ""
    details: str = ""

    @property
    def category(self) -> EventCategory:
        """Event category."""
        return EventCategory.ENGINE


# ── Cognitive Events ────────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class PerceptionReceived(Event):
    """Emitted when a new perception enters the cognitive loop."""

    source: str = ""
    content: str = ""
    person_id: str = ""
    channel_id: str = ""
    priority: int = 10

    @property
    def category(self) -> EventCategory:
        """Event category."""
        return EventCategory.COGNITIVE


@dataclasses.dataclass(frozen=True)
class ThinkCompleted(Event):
    """Emitted when the think phase completes an LLM call."""

    mind_id: str = ""
    response: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    model: str = ""
    cost_usd: float = 0.0
    latency_ms: int = 0

    @property
    def category(self) -> EventCategory:
        """Event category."""
        return EventCategory.COGNITIVE


@dataclasses.dataclass(frozen=True)
class ResponseSent(Event):
    """Emitted when a response is delivered through a channel."""

    mind_id: str = ""
    channel: str = ""
    message_id: str = ""
    latency_ms: int = 0

    @property
    def category(self) -> EventCategory:
        """Event category."""
        return EventCategory.COGNITIVE


# ── Brain Events ────────────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class ConceptCreated(Event):
    """Emitted when a new concept is stored in brain memory."""

    concept_id: str = ""
    title: str = ""
    source: str = ""

    @property
    def category(self) -> EventCategory:
        """Event category."""
        return EventCategory.BRAIN


@dataclasses.dataclass(frozen=True)
class EpisodeEncoded(Event):
    """Emitted when an episode is encoded into brain memory."""

    episode_id: str = ""
    conversation_id: str = ""
    importance: float = 0.0

    @property
    def category(self) -> EventCategory:
        """Event category."""
        return EventCategory.BRAIN


@dataclasses.dataclass(frozen=True)
class ConsolidationCompleted(Event):
    """Emitted when a memory consolidation cycle completes."""

    merged: int = 0
    pruned: int = 0
    strengthened: int = 0
    duration_s: float = 0.0

    @property
    def category(self) -> EventCategory:
        """Event category."""
        return EventCategory.BRAIN


# ── Bridge Events ───────────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class ChannelConnected(Event):
    """Emitted when a communication channel connects."""

    channel_type: str = ""
    channel_id: str = ""

    @property
    def category(self) -> EventCategory:
        """Event category."""
        return EventCategory.BRIDGE


@dataclasses.dataclass(frozen=True)
class ChannelDisconnected(Event):
    """Emitted when a communication channel disconnects."""

    channel_type: str = ""
    reason: str = ""

    @property
    def category(self) -> EventCategory:
        """Event category."""
        return EventCategory.BRIDGE


# ── Event Bus ───────────────────────────────────────────────────────────────

EventHandler = Callable[[Event], Awaitable[None]]


class EventBus:
    """Async in-process event bus.

    Characteristics:
        - Typed: handlers registered per event type
        - Async: handlers are coroutines
        - Error-isolated: handler exception is logged, other handlers continue
        - Ordered: handlers fire in registration order
        - Correlation: propagates event correlation_id to handler context

    Not thread-safe (asyncio single-threaded by design).
    Not persistent (in-memory only).
    """

    def __init__(self) -> None:
        self._handlers: dict[type[Event], list[EventHandler]] = defaultdict(list)

    def subscribe(self, event_type: type[Event], handler: EventHandler) -> None:
        """Register a handler for an event type.

        Args:
            event_type: The event class to listen for.
            handler: Async callable to invoke when event is emitted.
        """
        self._handlers[event_type].append(handler)

    def unsubscribe(self, event_type: type[Event], handler: EventHandler) -> None:
        """Remove a handler for an event type.

        Args:
            event_type: The event class.
            handler: The handler to remove.
        """
        handlers = self._handlers.get(event_type)
        if handlers and handler in handlers:
            handlers.remove(handler)

    async def emit(self, event: Event) -> None:
        """Emit an event to all subscribed handlers.

        Handlers are called in registration order. If a handler raises
        an exception, it is logged and the remaining handlers continue.

        Propagates the event's correlation_id into the handler context.

        Args:
            event: The event to emit.
        """
        handlers = self._handlers.get(type(event), [])
        if not handlers:
            return

        # Propagate correlation_id
        if event.correlation_id:
            set_correlation_id(event.correlation_id)

        for handler in handlers:
            try:
                await handler(event)
            except Exception:
                logger.error(
                    "event_handler_error",
                    event_type=type(event).__name__,
                    event_id=event.event_id,
                    handler=getattr(handler, "__name__", str(handler)),
                    exc_info=True,
                )

    @property
    def handler_count(self) -> int:
        """Total number of registered handlers across all event types."""
        return sum(len(h) for h in self._handlers.values())

    def clear(self) -> None:
        """Remove all registered handlers."""
        self._handlers.clear()
