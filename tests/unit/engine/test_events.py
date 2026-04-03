"""Tests for sovyx.engine.events — typed events and event bus."""

from __future__ import annotations

import dataclasses

import pytest

from sovyx.engine.events import (
    ChannelConnected,
    ChannelDisconnected,
    ConceptCreated,
    ConsolidationCompleted,
    EngineStarted,
    EngineStopping,
    EpisodeEncoded,
    Event,
    EventBus,
    EventCategory,
    PerceptionReceived,
    ResponseSent,
    ServiceHealthChanged,
    ThinkCompleted,
)
from sovyx.observability.logging import get_correlation_id, set_correlation_id

ALL_EVENT_TYPES: list[type[Event]] = [
    EngineStarted,
    EngineStopping,
    ServiceHealthChanged,
    PerceptionReceived,
    ThinkCompleted,
    ResponseSent,
    ConceptCreated,
    EpisodeEncoded,
    ConsolidationCompleted,
    ChannelConnected,
    ChannelDisconnected,
]


class TestEventTypes:
    """All event types are frozen, have IDs, timestamps, and categories."""

    @pytest.mark.parametrize("event_cls", ALL_EVENT_TYPES)
    def test_instantiable_with_defaults(self, event_cls: type[Event]) -> None:
        event = event_cls()
        assert event.event_id != ""
        assert event.timestamp is not None
        assert event.correlation_id == ""

    @pytest.mark.parametrize("event_cls", ALL_EVENT_TYPES)
    def test_frozen(self, event_cls: type[Event]) -> None:
        event = event_cls()
        with pytest.raises(dataclasses.FrozenInstanceError):
            event.event_id = "new"  # type: ignore[misc]

    @pytest.mark.parametrize("event_cls", ALL_EVENT_TYPES)
    def test_has_category(self, event_cls: type[Event]) -> None:
        event = event_cls()
        cat = event.category
        assert isinstance(cat, EventCategory)

    @pytest.mark.parametrize("event_cls", ALL_EVENT_TYPES)
    def test_has_docstring(self, event_cls: type[Event]) -> None:
        assert event_cls.__doc__ is not None

    @pytest.mark.parametrize("event_cls", ALL_EVENT_TYPES)
    def test_unique_ids(self, event_cls: type[Event]) -> None:
        e1 = event_cls()
        e2 = event_cls()
        assert e1.event_id != e2.event_id

    def test_base_event_category_raises(self) -> None:
        event = Event()
        with pytest.raises(NotImplementedError):
            _ = event.category


class TestEventCategories:
    """Events have correct categories."""

    def test_engine_events(self) -> None:
        assert EngineStarted().category == EventCategory.ENGINE
        assert EngineStopping().category == EventCategory.ENGINE
        assert ServiceHealthChanged().category == EventCategory.ENGINE

    def test_cognitive_events(self) -> None:
        assert PerceptionReceived().category == EventCategory.COGNITIVE
        assert ThinkCompleted().category == EventCategory.COGNITIVE
        assert ResponseSent().category == EventCategory.COGNITIVE

    def test_brain_events(self) -> None:
        assert ConceptCreated().category == EventCategory.BRAIN
        assert EpisodeEncoded().category == EventCategory.BRAIN
        assert ConsolidationCompleted().category == EventCategory.BRAIN

    def test_bridge_events(self) -> None:
        assert ChannelConnected().category == EventCategory.BRIDGE
        assert ChannelDisconnected().category == EventCategory.BRIDGE


class TestEventFields:
    """Events have expected fields with defaults."""

    def test_engine_started_fields(self) -> None:
        e = EngineStarted(version="0.1.0", mind_count=2)
        assert e.version == "0.1.0"
        assert e.mind_count == 2

    def test_think_completed_fields(self) -> None:
        e = ThinkCompleted(
            mind_id="aria",
            response="Hello!",
            tokens_in=100,
            tokens_out=50,
            model="claude-sonnet",
            cost_usd=0.001,
            latency_ms=500,
        )
        assert e.tokens_in == 100
        assert e.cost_usd == 0.001

    def test_consolidation_completed_fields(self) -> None:
        e = ConsolidationCompleted(merged=3, pruned=10, strengthened=5, duration_s=1.5)
        assert e.merged == 3
        assert e.duration_s == 1.5

    def test_perception_received_fields(self) -> None:
        e = PerceptionReceived(source="telegram", content="hello", priority=5)
        assert e.source == "telegram"
        assert e.priority == 5

    def test_correlation_id_propagated(self) -> None:
        e = EngineStarted(correlation_id="req-123")
        assert e.correlation_id == "req-123"


class TestEventBus:
    """Async event bus functionality."""

    @pytest.fixture
    def bus(self) -> EventBus:
        return EventBus()

    async def test_emit_no_subscribers(self, bus: EventBus) -> None:
        """Emit without subscribers does not fail."""
        await bus.emit(EngineStarted(version="0.1.0"))

    async def test_subscribe_and_emit(self, bus: EventBus) -> None:
        """Subscribe → emit → handler called."""
        received: list[Event] = []

        async def handler(event: Event) -> None:
            received.append(event)

        bus.subscribe(EngineStarted, handler)
        event = EngineStarted(version="0.1.0")
        await bus.emit(event)

        assert len(received) == 1
        assert received[0] is event

    async def test_multiple_handlers_ordered(self, bus: EventBus) -> None:
        """Multiple handlers are called in registration order."""
        order: list[str] = []

        async def handler_a(event: Event) -> None:
            order.append("a")

        async def handler_b(event: Event) -> None:
            order.append("b")

        async def handler_c(event: Event) -> None:
            order.append("c")

        bus.subscribe(EngineStarted, handler_a)
        bus.subscribe(EngineStarted, handler_b)
        bus.subscribe(EngineStarted, handler_c)

        await bus.emit(EngineStarted())
        assert order == ["a", "b", "c"]

    async def test_unsubscribe(self, bus: EventBus) -> None:
        """Unsubscribe removes the handler."""
        received: list[Event] = []

        async def handler(event: Event) -> None:
            received.append(event)

        bus.subscribe(EngineStarted, handler)
        bus.unsubscribe(EngineStarted, handler)
        await bus.emit(EngineStarted())

        assert len(received) == 0

    async def test_unsubscribe_nonexistent(self, bus: EventBus) -> None:
        """Unsubscribing a non-registered handler does not fail."""

        async def handler(event: Event) -> None:
            pass

        bus.unsubscribe(EngineStarted, handler)  # no error

    async def test_error_isolation(self, bus: EventBus) -> None:
        """Handler exception is caught; other handlers still run."""
        received: list[str] = []

        async def bad_handler(event: Event) -> None:
            msg = "boom"
            raise RuntimeError(msg)

        async def good_handler(event: Event) -> None:
            received.append("ok")

        bus.subscribe(EngineStarted, bad_handler)
        bus.subscribe(EngineStarted, good_handler)

        await bus.emit(EngineStarted())
        assert received == ["ok"]

    async def test_correlation_id_propagated(self, bus: EventBus) -> None:
        """Event correlation_id is set in handler context."""
        captured_cid: list[str] = []

        async def handler(event: Event) -> None:
            captured_cid.append(get_correlation_id())

        bus.subscribe(EngineStarted, handler)
        await bus.emit(EngineStarted(correlation_id="corr-456"))

        assert captured_cid == ["corr-456"]
        set_correlation_id("")  # cleanup

    async def test_handler_count(self, bus: EventBus) -> None:
        """handler_count returns total across all event types."""

        async def h(event: Event) -> None:
            pass

        assert bus.handler_count == 0
        bus.subscribe(EngineStarted, h)
        assert bus.handler_count == 1
        bus.subscribe(ConceptCreated, h)
        assert bus.handler_count == 2

    async def test_clear(self, bus: EventBus) -> None:
        """clear removes all handlers."""

        async def h(event: Event) -> None:
            pass

        bus.subscribe(EngineStarted, h)
        bus.subscribe(ConceptCreated, h)
        assert bus.handler_count == 2
        bus.clear()
        assert bus.handler_count == 0

    async def test_different_event_types_isolated(self, bus: EventBus) -> None:
        """Handlers for different event types don't cross-fire."""
        received: list[str] = []

        async def engine_handler(event: Event) -> None:
            received.append("engine")

        async def brain_handler(event: Event) -> None:
            received.append("brain")

        bus.subscribe(EngineStarted, engine_handler)
        bus.subscribe(ConceptCreated, brain_handler)

        await bus.emit(EngineStarted())
        assert received == ["engine"]

        await bus.emit(ConceptCreated())
        assert received == ["engine", "brain"]
