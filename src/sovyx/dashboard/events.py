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
    from sovyx.engine.events import (
        ChannelConnected,
        ChannelDisconnected,
        ConceptCreated,
        ConsolidationCompleted,
        DreamCompleted,
        EngineStopping,
        EpisodeEncoded,
        Event,
        EventBus,
        PerceptionReceived,
        ResponseSent,
        ServiceHealthChanged,
        ThinkCompleted,
    )

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
            DreamCompleted,
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
            DreamCompleted,
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
    # xdist module reimport. See CLAUDE.md anti-pattern #8. Per-branch casts
    # restore attribute access typing without restoring isinstance.
    from typing import cast

    if name == "EngineStarted":
        base["data"] = {}
    elif name == "EngineStopping":
        ev = cast("EngineStopping", event)
        base["data"] = {"reason": ev.reason}
    elif name == "ServiceHealthChanged":
        ev_shc = cast("ServiceHealthChanged", event)
        base["data"] = {
            "service": ev_shc.service,
            "status": ev_shc.status,
        }
    elif name == "PerceptionReceived":
        ev_pr = cast("PerceptionReceived", event)
        base["data"] = {
            "source": ev_pr.source,
            "person_id": ev_pr.person_id,
        }
    elif name == "ThinkCompleted":
        ev_tc = cast("ThinkCompleted", event)
        base["data"] = {
            "tokens_in": ev_tc.tokens_in,
            "tokens_out": ev_tc.tokens_out,
            "model": ev_tc.model,
            "cost_usd": round(ev_tc.cost_usd, 6),
            "latency_ms": ev_tc.latency_ms,
        }
    elif name == "ResponseSent":
        ev_rs = cast("ResponseSent", event)
        base["data"] = {
            "channel": ev_rs.channel,
            "latency_ms": ev_rs.latency_ms,
        }
    elif name == "ConceptCreated":
        ev_cc = cast("ConceptCreated", event)
        base["data"] = {
            "concept_id": ev_cc.concept_id,
            "title": ev_cc.title,
            "source": ev_cc.source,
        }
    elif name == "EpisodeEncoded":
        ev_ee = cast("EpisodeEncoded", event)
        base["data"] = {
            "episode_id": ev_ee.episode_id,
            "importance": ev_ee.importance,
        }
    elif name == "ConsolidationCompleted":
        ev_cons = cast("ConsolidationCompleted", event)
        base["data"] = {
            "merged": ev_cons.merged,
            "pruned": ev_cons.pruned,
            "strengthened": ev_cons.strengthened,
            "duration_s": round(ev_cons.duration_s, 2),
        }
    elif name == "DreamCompleted":
        ev_dream = cast("DreamCompleted", event)
        base["data"] = {
            "patterns_found": ev_dream.patterns_found,
            "concepts_derived": ev_dream.concepts_derived,
            "relations_strengthened": ev_dream.relations_strengthened,
            "episodes_analyzed": ev_dream.episodes_analyzed,
            "duration_s": round(ev_dream.duration_s, 2),
        }
    elif name == "ChannelConnected":
        ev_chc = cast("ChannelConnected", event)
        base["data"] = {
            "channel_type": ev_chc.channel_type,
        }
    elif name == "ChannelDisconnected":
        ev_chd = cast("ChannelDisconnected", event)
        base["data"] = {
            "channel_type": ev_chd.channel_type,
            "reason": ev_chd.reason,
        }
    else:
        base["data"] = {}

    return base
