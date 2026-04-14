"""Dashboard WebSocket event bridge.

Subscribes to Engine events via EventBus and broadcasts them to
connected WebSocket clients via ConnectionManager.

Event types are mapped to dashboard-friendly JSON payloads.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from sovyx.dashboard.server import ConnectionManager
    from sovyx.engine.events import Event, EventBus

logger = get_logger(__name__)


class DashboardEventBridge:
    """Bridge between Engine EventBus and WebSocket connections.

    Subscribes to key events and broadcasts JSON payloads to all
    connected dashboard clients.
    """

    def __init__(self, ws_manager: ConnectionManager, event_bus: EventBus) -> None:
        self._ws = ws_manager
        self._bus = event_bus
        self._subscribed = False

    def subscribe_all(self) -> None:
        """Subscribe to all dashboard-relevant events."""
        if self._subscribed:
            return

        from sovyx.engine.events import (
            ChannelConnected,
            ChannelDisconnected,
            ConceptCreated,
            ConsolidationCompleted,
            EngineStarted,
            EngineStopping,
            EpisodeEncoded,
            PerceptionReceived,
            ResponseSent,
            ServiceHealthChanged,
            ThinkCompleted,
        )

        event_types = [
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

        for etype in event_types:
            self._bus.subscribe(etype, self._handle_event)

        self._subscribed = True
        logger.debug("dashboard_events_subscribed", count=len(event_types))

    async def _handle_event(self, event: Event) -> None:
        """Convert Engine event to JSON and broadcast."""
        if self._ws.active_count == 0:
            return  # No clients connected, skip serialization

        payload = _serialize_event(event)
        await self._ws.broadcast(payload)


def _serialize_event(event: Event) -> dict[str, Any]:
    """Convert an Engine event to a dashboard-friendly JSON payload."""
    name = type(event).__name__
    base: dict[str, Any] = {
        "type": name,
        "timestamp": event.timestamp.isoformat(),
        "correlation_id": event.correlation_id,
    }

    # Name-based dispatch — class identity is unreliable under pytest-cov /
    # xdist module reimport. See CLAUDE.md anti-pattern #8.
    if name == "EngineStarted":
        base["data"] = {}
    elif name == "EngineStopping":
        base["data"] = {"reason": event.reason}
    elif name == "ServiceHealthChanged":
        base["data"] = {
            "service": event.service,
            "status": event.status,
        }
    elif name == "PerceptionReceived":
        base["data"] = {
            "source": event.source,
            "person_id": event.person_id,
        }
    elif name == "ThinkCompleted":
        base["data"] = {
            "tokens_in": event.tokens_in,
            "tokens_out": event.tokens_out,
            "model": event.model,
            "cost_usd": round(event.cost_usd, 6),
            "latency_ms": event.latency_ms,
        }
    elif name == "ResponseSent":
        base["data"] = {
            "channel": event.channel,
            "latency_ms": event.latency_ms,
        }
    elif name == "ConceptCreated":
        base["data"] = {
            "concept_id": event.concept_id,
            "title": event.title,
            "source": event.source,
        }
    elif name == "EpisodeEncoded":
        base["data"] = {
            "episode_id": event.episode_id,
            "importance": event.importance,
        }
    elif name == "ConsolidationCompleted":
        base["data"] = {
            "merged": event.merged,
            "pruned": event.pruned,
            "strengthened": event.strengthened,
            "duration_s": round(event.duration_s, 2),
        }
    elif name == "ChannelConnected":
        base["data"] = {
            "channel_type": event.channel_type,
        }
    elif name == "ChannelDisconnected":
        base["data"] = {
            "channel_type": event.channel_type,
            "reason": event.reason,
        }
    else:
        base["data"] = {}

    return base
