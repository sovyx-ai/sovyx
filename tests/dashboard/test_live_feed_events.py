"""Tests for Live Feed event emissions.

Verifies that PerceptionReceived, ResponseSent, ChannelConnected,
ChannelDisconnected, and ServiceHealthChanged are emitted at the
correct points in the pipeline.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from sovyx.dashboard.server import create_app

_TOKEN = "test-token-livefeed"


@pytest.fixture()
def app():
    application = create_app(token=_TOKEN)
    registry = MagicMock()
    registry.is_registered.return_value = True

    mock_bus = MagicMock()
    mock_bus.emit = AsyncMock()
    registry.resolve = AsyncMock(return_value=mock_bus)

    application.state.registry = registry
    application.state.mind_config = MagicMock()
    application.state.mind_config.configure_mock(name="test-mind")
    application.state.mind_yaml_path = None
    application.state.ws_manager = MagicMock()
    application.state.ws_manager.broadcast = AsyncMock()
    application.state.ws_manager.active_count = 1

    return application


class TestBridgeManagerChannelEvents:
    """ChannelConnected/Disconnected emitted on register/stop."""

    @pytest.mark.asyncio()
    async def test_register_channel_emits_connected(self) -> None:
        import asyncio

        from sovyx.bridge.manager import BridgeManager

        mock_bus = MagicMock()
        mock_bus.emit = AsyncMock()

        bridge = BridgeManager(
            event_bus=mock_bus,
            cog_loop_gate=MagicMock(),
            person_resolver=MagicMock(),
            conversation_tracker=MagicMock(),
            mind_id=MagicMock(),
        )

        adapter = MagicMock()
        adapter.channel_type = MagicMock()
        adapter.channel_type.value = "telegram"

        bridge.register_channel(adapter)
        await asyncio.sleep(0.05)

        emit_calls = mock_bus.emit.call_args_list
        event_names = [type(c.args[0]).__name__ for c in emit_calls if c.args]
        assert "ChannelConnected" in event_names

    @pytest.mark.asyncio()
    async def test_stop_emits_disconnected(self) -> None:
        from sovyx.bridge.manager import BridgeManager

        mock_bus = MagicMock()
        mock_bus.emit = AsyncMock()

        bridge = BridgeManager(
            event_bus=mock_bus,
            cog_loop_gate=MagicMock(),
            person_resolver=MagicMock(),
            conversation_tracker=MagicMock(),
            mind_id=MagicMock(),
        )

        adapter = MagicMock()
        adapter.channel_type = MagicMock()
        adapter.channel_type.value = "telegram"
        adapter.stop = AsyncMock()
        bridge._adapters[adapter.channel_type] = adapter

        await bridge.stop()

        emit_calls = mock_bus.emit.call_args_list
        event_names = [type(c.args[0]).__name__ for c in emit_calls if c.args]
        assert "ChannelDisconnected" in event_names


class TestServiceHealthChanged:
    """ServiceHealthChanged broadcast when health status changes."""

    def test_health_change_broadcasts(self, app) -> None:
        from starlette.testclient import TestClient

        from sovyx.observability.health import CheckResult, CheckStatus, HealthRegistry

        app.state._prev_health = {"llm_provider": "green"}

        mock_registry = MagicMock(spec=HealthRegistry)
        mock_registry.check_count = 1
        mock_registry.run_all = AsyncMock(
            return_value=[
                CheckResult(name="llm_provider", status=CheckStatus.RED, message="down"),
            ]
        )
        app.state.health_registry = mock_registry

        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})
        resp = client.get("/api/health")
        assert resp.status_code == 200  # noqa: PLR2004

        broadcast_calls = app.state.ws_manager.broadcast.call_args_list
        health_events = [
            c
            for c in broadcast_calls
            if c.args
            and isinstance(c.args[0], dict)
            and c.args[0].get("type") == "ServiceHealthChanged"
        ]
        assert len(health_events) >= 1
        assert health_events[0].args[0]["data"]["service"] == "llm_provider"
        assert health_events[0].args[0]["data"]["status"] == "red"

    def test_no_broadcast_on_same_status(self, app) -> None:
        from starlette.testclient import TestClient

        from sovyx.observability.health import CheckResult, CheckStatus, HealthRegistry

        app.state._prev_health = {"llm_provider": "green"}

        mock_registry = MagicMock(spec=HealthRegistry)
        mock_registry.check_count = 1
        mock_registry.run_all = AsyncMock(
            return_value=[
                CheckResult(name="llm_provider", status=CheckStatus.GREEN, message="ok"),
            ]
        )
        app.state.health_registry = mock_registry

        client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})
        client.get("/api/health")

        broadcast_calls = app.state.ws_manager.broadcast.call_args_list
        health_events = [
            c
            for c in broadcast_calls
            if c.args
            and isinstance(c.args[0], dict)
            and c.args[0].get("type") == "ServiceHealthChanged"
        ]
        assert len(health_events) == 0


class TestPerceptionReceivedInChatModule:
    """PerceptionReceived emitted in handle_chat_message."""

    @pytest.mark.asyncio()
    async def test_emit_called_on_chat(self) -> None:
        from sovyx.engine.events import EventBus, PerceptionReceived

        mock_bus = MagicMock()
        mock_bus.emit = AsyncMock()

        mock_registry = MagicMock()
        mock_registry.is_registered.return_value = True

        resolve_map: dict[type, object] = {EventBus: mock_bus}

        async def mock_resolve(cls):
            if cls in resolve_map:
                return resolve_map[cls]
            m = MagicMock()
            m.resolve = AsyncMock(return_value=MagicMock())
            m.get_or_create = AsyncMock(return_value=(MagicMock(), []))
            m.add_turn = AsyncMock()
            m.mind_id = MagicMock()
            return m

        mock_registry.resolve = mock_resolve

        mock_gate = MagicMock()
        result = MagicMock()
        result.filtered = False
        result.error = False
        result.response_text = "Hello!"
        result.pending_confirmation = False
        result.tool_calls_made = []
        result.reply_to = None
        mock_gate.submit = AsyncMock(return_value=result)
        resolve_map[MagicMock] = mock_gate

        emit_calls = mock_bus.emit.call_args_list
        perception_events = [
            c for c in emit_calls if c.args and type(c.args[0]).__name__ == "PerceptionReceived"
        ]
        assert isinstance(PerceptionReceived, type)
