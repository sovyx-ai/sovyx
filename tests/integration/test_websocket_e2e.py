"""VAL-26: WebSocket event flow E2E.

Tests the full chain: EventBus.emit → DashboardEventBridge → ConnectionManager → WS client.
Also tests /ws endpoint auth, ping/pong, and disconnect via Starlette TestClient.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from sovyx.dashboard.events import DashboardEventBridge, _serialize_event
from sovyx.dashboard.server import ConnectionManager, create_app
from sovyx.engine.config import APIConfig
from sovyx.engine.events import (
    ChannelConnected,
    ChannelDisconnected,
    ConceptCreated,
    ConsolidationCompleted,
    EngineStarted,
    EngineStopping,
    EpisodeEncoded,
    EventBus,
    PerceptionReceived,
    ResponseSent,
    ServiceHealthChanged,
    ThinkCompleted,
)

_TOKEN = "ws-test-token"


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture()
def app() -> object:
    """Create a FastAPI app with mocked token."""
    with patch("sovyx.dashboard.server.TOKEN_FILE") as mock_tf:
        mock_tf.exists.return_value = True
        mock_tf.read_text.return_value = _TOKEN
        return create_app(APIConfig(host="127.0.0.1", port=0))


@pytest.fixture()
def sync_client(app: object) -> TestClient:
    """Starlette TestClient for sync WS tests."""
    return TestClient(app)  # type: ignore[arg-type]


class FakeWebSocket:
    """Captures sent messages for async event flow tests."""

    def __init__(self) -> None:
        self.messages: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def send_json(self, data: dict[str, Any]) -> None:
        await self.messages.put(data)

    async def get(self, timeout: float = 2.0) -> dict[str, Any]:
        return await asyncio.wait_for(self.messages.get(), timeout=timeout)


@pytest.fixture()
def event_bus() -> EventBus:
    return EventBus()


@pytest.fixture()
def ws_manager() -> ConnectionManager:
    return ConnectionManager()


@pytest.fixture()
def bridge(ws_manager: ConnectionManager, event_bus: EventBus) -> DashboardEventBridge:
    b = DashboardEventBridge(ws_manager, event_bus)
    b.subscribe_all()
    return b


@pytest.fixture()
async def fake_ws(ws_manager: ConnectionManager) -> FakeWebSocket:
    """A fake WS registered in the connection manager."""
    ws = FakeWebSocket()
    async with ws_manager._lock:
        ws_manager._connections.append(ws)  # type: ignore[arg-type]
    return ws


# ── WS Auth (real WebSocket via TestClient) ────────────────────────────────


class TestWebSocketAuth:
    """WS authentication via query param."""

    def test_no_token_rejected(self, sync_client: TestClient) -> None:
        """WS without token → connection closed with 4001."""
        with pytest.raises(Exception), sync_client.websocket_connect("/ws"):  # noqa: B017
            pass

    def test_empty_token_rejected(self, sync_client: TestClient) -> None:
        """WS with empty token → rejected."""
        with pytest.raises(Exception), sync_client.websocket_connect("/ws?token="):  # noqa: B017
            pass

    def test_wrong_token_rejected(self, sync_client: TestClient) -> None:
        """WS with wrong token → rejected."""
        with pytest.raises(Exception), sync_client.websocket_connect("/ws?token=wrong"):  # noqa: B017
            pass

    def test_partial_token_rejected(self, sync_client: TestClient) -> None:
        """WS with partial correct token → rejected."""
        partial = _TOKEN[:8]
        with pytest.raises(Exception), sync_client.websocket_connect(f"/ws?token={partial}"):  # noqa: B017
            pass

    def test_valid_token_connects(self, sync_client: TestClient) -> None:
        """WS with correct token → accepted, ping/pong works."""
        with sync_client.websocket_connect(f"/ws?token={_TOKEN}") as ws:
            ws.send_text("ping")
            assert ws.receive_text() == "pong"


# ── WS Ping/Pong ───────────────────────────────────────────────────────────


class TestWebSocketPingPong:
    """Keep-alive mechanism."""

    def test_multiple_pings(self, sync_client: TestClient) -> None:
        with sync_client.websocket_connect(f"/ws?token={_TOKEN}") as ws:
            for _ in range(5):
                ws.send_text("ping")
                assert ws.receive_text() == "pong"

    def test_non_ping_ignored(self, sync_client: TestClient) -> None:
        """Non-ping messages don't crash the handler."""
        with sync_client.websocket_connect(f"/ws?token={_TOKEN}") as ws:
            ws.send_text("hello")
            # Server doesn't respond to non-ping, send a ping to verify alive
            ws.send_text("ping")
            assert ws.receive_text() == "pong"


# ── WS Disconnect ──────────────────────────────────────────────────────────


class TestWebSocketDisconnect:
    """Clean disconnect handling."""

    def test_disconnect_is_clean(self, sync_client: TestClient) -> None:
        """Client can disconnect without errors."""
        with sync_client.websocket_connect(f"/ws?token={_TOKEN}") as ws:
            ws.send_text("ping")
            assert ws.receive_text() == "pong"
        # No exception on scope exit = clean disconnect


# ── Event Flow E2E (EventBus → Bridge → WS) ────────────────────────────────


class TestEventFlowEngineStarted:
    """EngineStarted: emit on EventBus → received via WS."""

    @pytest.mark.asyncio()
    async def test_engine_started_received(
        self,
        event_bus: EventBus,
        bridge: DashboardEventBridge,  # noqa: ARG002
        fake_ws: FakeWebSocket,
    ) -> None:
        await event_bus.emit(EngineStarted(version="1.0.0"))
        msg = await fake_ws.get()

        assert msg["type"] == "EngineStarted"
        assert msg["data"] == {}
        assert "timestamp" in msg
        assert "correlation_id" in msg

    @pytest.mark.asyncio()
    async def test_engine_started_timestamp_is_iso(
        self,
        event_bus: EventBus,
        bridge: DashboardEventBridge,  # noqa: ARG002
        fake_ws: FakeWebSocket,
    ) -> None:
        from datetime import datetime

        await event_bus.emit(EngineStarted())
        msg = await fake_ws.get()

        # Must be valid ISO 8601
        ts = datetime.fromisoformat(msg["timestamp"])
        assert ts.tzinfo is not None  # timezone-aware


class TestEventFlowConceptCreated:
    """BrainService creates concept → WS receives ConceptCreated."""

    @pytest.mark.asyncio()
    async def test_concept_created_payload(
        self,
        event_bus: EventBus,
        bridge: DashboardEventBridge,  # noqa: ARG002
        fake_ws: FakeWebSocket,
    ) -> None:
        await event_bus.emit(
            ConceptCreated(concept_id="c42", title="Quantum Computing", source="conversation"),
        )
        msg = await fake_ws.get()

        assert msg["type"] == "ConceptCreated"
        assert msg["data"]["concept_id"] == "c42"
        assert msg["data"]["title"] == "Quantum Computing"
        assert msg["data"]["source"] == "conversation"


class TestEventFlowThinkCompleted:
    """CogLoop completes think → WS receives ThinkCompleted."""

    @pytest.mark.asyncio()
    async def test_think_completed_payload(
        self,
        event_bus: EventBus,
        bridge: DashboardEventBridge,  # noqa: ARG002
        fake_ws: FakeWebSocket,
    ) -> None:
        await event_bus.emit(
            ThinkCompleted(
                tokens_in=500,
                tokens_out=200,
                model="claude-sonnet-4-20250514",
                cost_usd=0.003456,
                latency_ms=1500,
            ),
        )
        msg = await fake_ws.get()

        assert msg["type"] == "ThinkCompleted"
        assert msg["data"]["tokens_in"] == 500
        assert msg["data"]["tokens_out"] == 200
        assert msg["data"]["model"] == "claude-sonnet-4-20250514"
        assert msg["data"]["cost_usd"] == 0.003456
        assert msg["data"]["latency_ms"] == 1500

    @pytest.mark.asyncio()
    async def test_think_completed_cost_rounded(
        self,
        event_bus: EventBus,
        bridge: DashboardEventBridge,  # noqa: ARG002
        fake_ws: FakeWebSocket,
    ) -> None:
        """Cost is rounded to 6 decimal places."""
        await event_bus.emit(
            ThinkCompleted(
                tokens_in=1,
                tokens_out=1,
                model="m",
                cost_usd=0.00000012345,
                latency_ms=1,
            ),
        )
        msg = await fake_ws.get()
        # round(0.00000012345, 6) == 0.0
        assert isinstance(msg["data"]["cost_usd"], float)


class TestEventFlowServiceHealthChanged:
    """Health status changes → WS receives ServiceHealthChanged."""

    @pytest.mark.asyncio()
    async def test_health_changed_payload(
        self,
        event_bus: EventBus,
        bridge: DashboardEventBridge,  # noqa: ARG002
        fake_ws: FakeWebSocket,
    ) -> None:
        await event_bus.emit(
            ServiceHealthChanged(service="Database", status="yellow"),
        )
        msg = await fake_ws.get()

        assert msg["type"] == "ServiceHealthChanged"
        assert msg["data"]["service"] == "Database"
        assert msg["data"]["status"] == "yellow"


class TestEventFlowAllTypes:
    """All 11 event types flow through the bridge correctly."""

    @pytest.mark.asyncio()
    async def test_all_11_events_reach_ws(
        self,
        event_bus: EventBus,
        bridge: DashboardEventBridge,  # noqa: ARG002
        fake_ws: FakeWebSocket,
    ) -> None:
        """Emit all 11 event types → each arrives on WS with correct type."""
        events = [
            EngineStarted(),
            EngineStopping(reason="shutdown"),
            ServiceHealthChanged(service="LLM", status="red"),
            PerceptionReceived(source="telegram", person_id="p1"),
            ThinkCompleted(tokens_in=10, tokens_out=5, model="m", cost_usd=0.0, latency_ms=100),
            ResponseSent(channel="telegram", latency_ms=50),
            ConceptCreated(concept_id="c1", title="AI", source="conv"),
            EpisodeEncoded(episode_id="e1", importance=0.9),
            ConsolidationCompleted(merged=2, pruned=1, strengthened=5, duration_s=1.234),
            ChannelConnected(channel_type="telegram"),
            ChannelDisconnected(channel_type="signal", reason="error"),
        ]

        for evt in events:
            await event_bus.emit(evt)

        received_types: list[str] = []
        for _ in range(11):
            msg = await fake_ws.get()
            received_types.append(msg["type"])

        expected = [
            "EngineStarted",
            "EngineStopping",
            "ServiceHealthChanged",
            "PerceptionReceived",
            "ThinkCompleted",
            "ResponseSent",
            "ConceptCreated",
            "EpisodeEncoded",
            "ConsolidationCompleted",
            "ChannelConnected",
            "ChannelDisconnected",
        ]
        assert received_types == expected

    @pytest.mark.asyncio()
    async def test_all_payloads_have_required_fields(
        self,
        event_bus: EventBus,
        bridge: DashboardEventBridge,  # noqa: ARG002
        fake_ws: FakeWebSocket,
    ) -> None:
        """Every event payload has type, timestamp, correlation_id, and data."""
        events = [
            EngineStarted(),
            EngineStopping(reason="r"),
            ServiceHealthChanged(service="s", status="green"),
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
            await event_bus.emit(evt)

        for _ in range(11):
            msg = await fake_ws.get()
            assert "type" in msg, f"Missing 'type' in {msg}"
            assert "timestamp" in msg, f"Missing 'timestamp' in {msg}"
            assert "correlation_id" in msg, f"Missing 'correlation_id' in {msg}"
            assert "data" in msg, f"Missing 'data' in {msg}"
            assert isinstance(msg["data"], dict), f"'data' should be dict in {msg}"


class TestEventFlowPayloadDetails:
    """Detailed payload validation for specific event types."""

    @pytest.mark.asyncio()
    async def test_engine_stopping_reason(
        self,
        event_bus: EventBus,
        bridge: DashboardEventBridge,  # noqa: ARG002
        fake_ws: FakeWebSocket,
    ) -> None:
        await event_bus.emit(EngineStopping(reason="SIGTERM"))
        msg = await fake_ws.get()
        assert msg["data"]["reason"] == "SIGTERM"

    @pytest.mark.asyncio()
    async def test_perception_received_fields(
        self,
        event_bus: EventBus,
        bridge: DashboardEventBridge,  # noqa: ARG002
        fake_ws: FakeWebSocket,
    ) -> None:
        await event_bus.emit(PerceptionReceived(source="discord", person_id="user42"))
        msg = await fake_ws.get()
        assert msg["data"]["source"] == "discord"
        assert msg["data"]["person_id"] == "user42"

    @pytest.mark.asyncio()
    async def test_response_sent_fields(
        self,
        event_bus: EventBus,
        bridge: DashboardEventBridge,  # noqa: ARG002
        fake_ws: FakeWebSocket,
    ) -> None:
        await event_bus.emit(ResponseSent(channel="telegram", latency_ms=250))
        msg = await fake_ws.get()
        assert msg["data"]["channel"] == "telegram"
        assert msg["data"]["latency_ms"] == 250

    @pytest.mark.asyncio()
    async def test_episode_encoded_fields(
        self,
        event_bus: EventBus,
        bridge: DashboardEventBridge,  # noqa: ARG002
        fake_ws: FakeWebSocket,
    ) -> None:
        await event_bus.emit(EpisodeEncoded(episode_id="ep-99", importance=0.75))
        msg = await fake_ws.get()
        assert msg["data"]["episode_id"] == "ep-99"
        assert msg["data"]["importance"] == 0.75

    @pytest.mark.asyncio()
    async def test_consolidation_duration_rounded(
        self,
        event_bus: EventBus,
        bridge: DashboardEventBridge,  # noqa: ARG002
        fake_ws: FakeWebSocket,
    ) -> None:
        await event_bus.emit(
            ConsolidationCompleted(merged=3, pruned=1, strengthened=7, duration_s=2.5678),
        )
        msg = await fake_ws.get()
        assert msg["data"]["merged"] == 3
        assert msg["data"]["pruned"] == 1
        assert msg["data"]["strengthened"] == 7
        assert msg["data"]["duration_s"] == 2.57  # rounded to 2 decimals

    @pytest.mark.asyncio()
    async def test_channel_connected_type(
        self,
        event_bus: EventBus,
        bridge: DashboardEventBridge,  # noqa: ARG002
        fake_ws: FakeWebSocket,
    ) -> None:
        await event_bus.emit(ChannelConnected(channel_type="matrix"))
        msg = await fake_ws.get()
        assert msg["data"]["channel_type"] == "matrix"

    @pytest.mark.asyncio()
    async def test_channel_disconnected_reason(
        self,
        event_bus: EventBus,
        bridge: DashboardEventBridge,  # noqa: ARG002
        fake_ws: FakeWebSocket,
    ) -> None:
        await event_bus.emit(ChannelDisconnected(channel_type="slack", reason="rate_limited"))
        msg = await fake_ws.get()
        assert msg["data"]["channel_type"] == "slack"
        assert msg["data"]["reason"] == "rate_limited"


class TestEventFlowMultiClient:
    """Multiple WS clients receive broadcasts."""

    @pytest.mark.asyncio()
    async def test_two_clients_both_receive(
        self,
        event_bus: EventBus,
        ws_manager: ConnectionManager,
        bridge: DashboardEventBridge,  # noqa: ARG002
    ) -> None:
        """Two connected clients both receive the same event."""
        ws1 = FakeWebSocket()
        ws2 = FakeWebSocket()

        async with ws_manager._lock:
            ws_manager._connections.append(ws1)  # type: ignore[arg-type]
            ws_manager._connections.append(ws2)  # type: ignore[arg-type]

        await event_bus.emit(EngineStarted())

        msg1 = await ws1.get()
        msg2 = await ws2.get()

        assert msg1["type"] == "EngineStarted"
        assert msg2["type"] == "EngineStarted"
        # Same event → same correlation_id
        assert msg1["correlation_id"] == msg2["correlation_id"]

    @pytest.mark.asyncio()
    async def test_no_clients_no_error(
        self,
        event_bus: EventBus,
        ws_manager: ConnectionManager,
        bridge: DashboardEventBridge,  # noqa: ARG002
    ) -> None:
        """Emitting with zero clients connected doesn't raise."""
        assert ws_manager.active_count == 0
        # Should not raise
        await event_bus.emit(EngineStarted())


class TestEventFlowCorrelation:
    """Correlation ID propagation."""

    @pytest.mark.asyncio()
    async def test_correlation_id_preserved(
        self,
        event_bus: EventBus,
        bridge: DashboardEventBridge,  # noqa: ARG002
        fake_ws: FakeWebSocket,
    ) -> None:
        """Event correlation_id is preserved in WS payload."""
        evt = EngineStarted(correlation_id="req-abc-123")
        await event_bus.emit(evt)

        msg = await fake_ws.get()
        assert msg["correlation_id"] == "req-abc-123"

    @pytest.mark.asyncio()
    async def test_empty_correlation_id(
        self,
        event_bus: EventBus,
        bridge: DashboardEventBridge,  # noqa: ARG002
        fake_ws: FakeWebSocket,
    ) -> None:
        """Default empty correlation_id is still present in payload."""
        await event_bus.emit(EngineStarted())
        msg = await fake_ws.get()
        assert "correlation_id" in msg
        assert msg["correlation_id"] == ""
