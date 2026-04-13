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
    """Convert an Engine event to a dashboard-friendly JSON payload.

    Uses type(event).__name__ dispatch instead of isinstance() to avoid
    class identity mismatches when modules are re-imported in different
    namespaces (e.g. pytest-xdist forked workers in CI).
    """
    event_name = type(event).__name__

    base: dict[str, Any] = {
        "type": event_name,
        "timestamp": event.timestamp.isoformat(),
        "correlation_id": event.correlation_id,
    }

    if event_name == "EngineStarted":
        base["data"] = {}
    elif event_name == "EngineStopping":
        base["data"] = {"reason": event.reason}  # type: ignore[attr-defined]
    elif event_name == "ServiceHealthChanged":
        base["data"] = {
            "service": event.service,  # type: ignore[attr-defined]
            "status": event.status,  # type: ignore[attr-defined]
        }
    elif event_name == "PerceptionReceived":
        base["data"] = {
            "source": event.source,  # type: ignore[attr-defined]
            "person_id": event.person_id,  # type: ignore[attr-defined]
        }
    elif event_name == "ThinkCompleted":
        base["data"] = {
            "tokens_in": event.tokens_in,  # type: ignore[attr-defined]
            "tokens_out": event.tokens_out,  # type: ignore[attr-defined]
            "model": event.model,  # type: ignore[attr-defined]
            "cost_usd": round(event.cost_usd, 6),  # type: ignore[attr-defined]
            "latency_ms": event.latency_ms,  # type: ignore[attr-defined]
        }
    elif event_name == "ResponseSent":
        base["data"] = {
            "channel": event.channel,  # type: ignore[attr-defined]
            "latency_ms": event.latency_ms,  # type: ignore[attr-defined]
        }
    elif event_name == "ConceptCreated":
        base["data"] = {
            "concept_id": event.concept_id,  # type: ignore[attr-defined]
            "title": event.title,  # type: ignore[attr-defined]
            "source": event.source,  # type: ignore[attr-defined]
        }
    elif event_name == "EpisodeEncoded":
        base["data"] = {
            "episode_id": event.episode_id,  # type: ignore[attr-defined]
            "importance": event.importance,  # type: ignore[attr-defined]
        }
    elif event_name == "ConsolidationCompleted":
        base["data"] = {
            "merged": event.merged,  # type: ignore[attr-defined]
            "pruned": event.pruned,  # type: ignore[attr-defined]
            "strengthened": event.strengthened,  # type: ignore[attr-defined]
            "duration_s": round(event.duration_s, 2),  # type: ignore[attr-defined]
        }
    elif event_name == "ChannelConnected":
        base["data"] = {
            "channel_type": event.channel_type,  # type: ignore[attr-defined]
        }
    elif event_name == "ChannelDisconnected":
        base["data"] = {
            "channel_type": event.channel_type,  # type: ignore[attr-defined]
            "reason": event.reason,  # type: ignore[attr-defined]
        }
    else:
        base["data"] = {}

    return base
