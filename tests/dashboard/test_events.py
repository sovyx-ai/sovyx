"""Tests for sovyx.dashboard.events — WebSocket event bridge."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, PropertyMock

import pytest

from sovyx.dashboard.events import DashboardEventBridge, _serialize_event
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
    PerceptionReceived,
    ResponseSent,
    ServiceHealthChanged,
    ThinkCompleted,
)


class TestSerializeEvent:
    def test_engine_started(self) -> None:
        event = EngineStarted()
        result = _serialize_event(event)
        assert result["type"] == "EngineStarted"
        assert "timestamp" in result
        assert result["data"] == {}

    def test_engine_stopping(self) -> None:
        result = _serialize_event(EngineStopping(reason="shutdown"))
        assert result["data"]["reason"] == "shutdown"

    def test_service_health_changed(self) -> None:
        result = _serialize_event(ServiceHealthChanged(service="Database", status="green"))
        assert result["data"]["service"] == "Database"
        assert result["data"]["status"] == "green"

    def test_perception_received(self) -> None:
        result = _serialize_event(PerceptionReceived(source="telegram", person_id="p1"))
        assert result["data"]["source"] == "telegram"
        assert result["data"]["person_id"] == "p1"

    def test_think_completed(self) -> None:
        event = ThinkCompleted(
            tokens_in=100,
            tokens_out=50,
            model="gpt-4o",
            cost_usd=0.005,
            latency_ms=1200,
        )
        result = _serialize_event(event)
        assert result["data"]["tokens_in"] == 100
        assert result["data"]["tokens_out"] == 50
        assert result["data"]["model"] == "gpt-4o"
        assert result["data"]["cost_usd"] == 0.005
        assert result["data"]["latency_ms"] == 1200

    def test_response_sent(self) -> None:
        result = _serialize_event(ResponseSent(channel="telegram", latency_ms=200))
        assert result["data"]["channel"] == "telegram"

    def test_concept_created(self) -> None:
        event = ConceptCreated(concept_id="c1", title="Python", source="conversation")
        result = _serialize_event(event)
        assert result["data"]["concept_id"] == "c1"
        assert result["data"]["title"] == "Python"

    def test_episode_encoded(self) -> None:
        result = _serialize_event(EpisodeEncoded(episode_id="e1", importance=0.8))
        assert result["data"]["episode_id"] == "e1"
        assert result["data"]["importance"] == 0.8

    def test_consolidation_completed(self) -> None:
        result = _serialize_event(
            ConsolidationCompleted(merged=5, pruned=3, strengthened=10, duration_s=2.567),
        )
        assert result["data"]["merged"] == 5
        assert result["data"]["pruned"] == 3
        assert result["data"]["duration_s"] == 2.57

    def test_channel_connected(self) -> None:
        result = _serialize_event(ChannelConnected(channel_type="telegram"))
        assert result["data"]["channel_type"] == "telegram"

    def test_channel_disconnected(self) -> None:
        result = _serialize_event(ChannelDisconnected(channel_type="signal", reason="timeout"))
        assert result["data"]["reason"] == "timeout"

    def test_unknown_event_type_returns_empty_data(self) -> None:
        """Cover the else branch: unknown event type → data = {}."""
        # Create a bare Event (base class, not one of the known subclasses)
        event = Event()
        result = _serialize_event(event)
        assert result["type"] == "Event"
        assert result["data"] == {}
        assert "timestamp" in result
        assert "correlation_id" in result

    def test_all_events_have_correlation_id(self) -> None:
        """Every serialized event must have a correlation_id."""
        events = [
            EngineStarted(),
            EngineStopping(reason="test"),
            ServiceHealthChanged(service="X", status="green"),
            PerceptionReceived(source="s", person_id="p"),
            ThinkCompleted(tokens_in=1, tokens_out=1, model="m", cost_usd=0.0, latency_ms=1),
            ResponseSent(channel="c", latency_ms=1),
            ConceptCreated(concept_id="c", title="t", source="s"),
            EpisodeEncoded(episode_id="e", importance=0.5),
            ConsolidationCompleted(merged=0, pruned=0, strengthened=0, duration_s=0.0),
            ChannelConnected(channel_type="t"),
            ChannelDisconnected(channel_type="t", reason="r"),
        ]
        for evt in events:
            result = _serialize_event(evt)
            assert result["correlation_id"] is not None


class TestDashboardEventBridge:
    def test_subscribe_all(self) -> None:
        ws = MagicMock()
        bus = EventBus()
        bridge = DashboardEventBridge(ws, bus)

        bridge.subscribe_all()

        # Should have 11 event types subscribed
        total_handlers = sum(len(h) for h in bus._handlers.values())
        assert total_handlers == 11

    def test_subscribe_idempotent(self) -> None:
        ws = MagicMock()
        bus = EventBus()
        bridge = DashboardEventBridge(ws, bus)

        bridge.subscribe_all()
        bridge.subscribe_all()

        total_handlers = sum(len(h) for h in bus._handlers.values())
        assert total_handlers == 11  # Not 22

    @pytest.mark.asyncio()
    async def test_handle_event_broadcasts(self) -> None:
        ws = MagicMock()
        type(ws).active_count = PropertyMock(return_value=2)
        ws.broadcast = AsyncMock()

        bus = EventBus()
        bridge = DashboardEventBridge(ws, bus)
        bridge.subscribe_all()

        await bus.emit(EngineStarted())

        ws.broadcast.assert_called_once()
        payload = ws.broadcast.call_args[0][0]
        assert payload["type"] == "EngineStarted"

    @pytest.mark.asyncio()
    async def test_skip_broadcast_no_clients(self) -> None:
        ws = MagicMock()
        type(ws).active_count = PropertyMock(return_value=0)
        ws.broadcast = AsyncMock()

        bus = EventBus()
        bridge = DashboardEventBridge(ws, bus)
        bridge.subscribe_all()

        await bus.emit(EngineStarted())

        ws.broadcast.assert_not_called()

    @pytest.mark.asyncio()
    async def test_broadcasts_all_11_event_types(self) -> None:
        """Every known event type should be broadcast when clients are connected."""
        ws = MagicMock()
        type(ws).active_count = PropertyMock(return_value=1)
        ws.broadcast = AsyncMock()

        bus = EventBus()
        bridge = DashboardEventBridge(ws, bus)
        bridge.subscribe_all()

        events = [
            EngineStarted(),
            EngineStopping(reason="test"),
            ServiceHealthChanged(service="X", status="green"),
            PerceptionReceived(source="s", person_id="p"),
            ThinkCompleted(tokens_in=1, tokens_out=1, model="m", cost_usd=0.0, latency_ms=1),
            ResponseSent(channel="c", latency_ms=1),
            ConceptCreated(concept_id="c", title="t", source="s"),
            EpisodeEncoded(episode_id="e", importance=0.5),
            ConsolidationCompleted(merged=0, pruned=0, strengthened=0, duration_s=0.0),
            ChannelConnected(channel_type="t"),
            ChannelDisconnected(channel_type="t", reason="r"),
        ]

        for evt in events:
            await bus.emit(evt)

        assert ws.broadcast.call_count == 11
