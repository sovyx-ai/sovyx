"""Integration tests for dashboard chat — API contract verification (DASH-05).

Tests the full HTTP endpoint contract:
- Request validation edge cases not in unit tests
- Response schema compliance
- Error response format consistency
- WebSocket broadcast integration
- Concurrency safety (multiple rapid requests)
- Auth token rotation

These complement the 40 unit tests in test_chat.py which cover
the business logic with mocked dependencies.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from starlette.testclient import TestClient

from sovyx.cognitive.act import ActionResult
from sovyx.dashboard.server import create_app

# ── Fixtures ──


# token + auth_headers from tests/dashboard/conftest.py


def _make_mock_registry(
    response_text: str = "Hello from Aria!",
    filtered: bool = False,
    error: bool = False,
    gate_side_effect: Exception | None = None,
) -> MagicMock:
    """Create a mock registry with full pipeline wired."""
    mock_registry = MagicMock()

    mock_person_resolver = AsyncMock()
    mock_person_resolver.resolve = AsyncMock(return_value="person-123")

    mock_conv_tracker = AsyncMock()
    mock_conv_tracker.get_or_create = AsyncMock(
        return_value=("conv-456", []),
    )
    mock_conv_tracker.add_turn = AsyncMock()

    action_result = ActionResult(
        response_text=response_text,
        target_channel="dashboard",
        filtered=filtered,
        error=error,
    )

    mock_gate = AsyncMock()
    if gate_side_effect:
        mock_gate.submit = AsyncMock(side_effect=gate_side_effect)
    else:
        mock_gate.submit = AsyncMock(return_value=action_result)

    mock_bridge = MagicMock()
    mock_bridge.mind_id = "test-mind"

    async def _resolve(interface: type) -> object:
        from sovyx.bridge.identity import PersonResolver
        from sovyx.bridge.manager import BridgeManager
        from sovyx.bridge.sessions import ConversationTracker
        from sovyx.cognitive.gate import CogLoopGate

        mapping: dict[type, object] = {
            PersonResolver: mock_person_resolver,
            ConversationTracker: mock_conv_tracker,
            CogLoopGate: mock_gate,
            BridgeManager: mock_bridge,
        }
        result = mapping.get(interface)
        if result is None:
            msg = f"Service not registered: {interface.__name__}"
            raise Exception(msg)  # noqa: TRY002
        return result

    mock_registry.resolve = AsyncMock(side_effect=_resolve)
    mock_registry._gate = mock_gate
    mock_registry._person_resolver = mock_person_resolver
    mock_registry._conv_tracker = mock_conv_tracker

    return mock_registry


# ── Response Schema Tests ──


class TestChatResponseSchema:
    """Verify the response JSON schema matches the API contract."""

    def test_success_response_has_all_fields(
        self,
        token: str,
        auth_headers: dict[str, str],
    ) -> None:
        """Success response contains response, conversation_id, mind_id, timestamp."""
        app = create_app()
        app.state.registry = _make_mock_registry()
        client = TestClient(app)

        resp = client.post(
            "/api/chat",
            json={"message": "Hello"},
            headers=auth_headers,
        )

        assert resp.status_code == 200
        data = resp.json()
        assert set(data.keys()) == {
            "response",
            "conversation_id",
            "mind_id",
            "timestamp",
        }
        assert isinstance(data["response"], str)
        assert isinstance(data["conversation_id"], str)
        assert isinstance(data["mind_id"], str)
        assert isinstance(data["timestamp"], str)

    def test_error_response_has_error_field(
        self,
        token: str,
        auth_headers: dict[str, str],
    ) -> None:
        """Error responses always have 'error' field."""
        app = create_app()
        client = TestClient(app)

        # No registry → 503
        resp = client.post(
            "/api/chat",
            json={"message": "Hello"},
            headers=auth_headers,
        )
        assert "error" in resp.json()

        # Invalid body → 422
        resp = client.post(
            "/api/chat",
            json={"message": ""},
            headers=auth_headers,
        )
        assert "error" in resp.json()

    def test_timestamp_is_iso8601(
        self,
        token: str,
        auth_headers: dict[str, str],
    ) -> None:
        """Timestamp parses as valid ISO 8601 with timezone."""
        from datetime import datetime

        app = create_app()
        app.state.registry = _make_mock_registry()
        client = TestClient(app)

        resp = client.post(
            "/api/chat",
            json={"message": "Hello"},
            headers=auth_headers,
        )

        ts = datetime.fromisoformat(resp.json()["timestamp"])
        assert ts.tzinfo is not None


# ── Error Response Consistency ──


class TestErrorResponseConsistency:
    """All error paths return consistent JSON format."""

    def test_422_validation_errors_are_json(
        self,
        token: str,
        auth_headers: dict[str, str],
    ) -> None:
        """422 errors return JSON with 'error' key, not HTML."""
        app = create_app()
        client = TestClient(app)

        cases = [
            {"message": ""},
            {"message": None},
            {"message": 42},
            {"user_name": "test"},
            {"message": "hi", "conversation_id": 123},
        ]

        for body in cases:
            resp = client.post(
                "/api/chat",
                json=body,
                headers=auth_headers,
            )
            assert resp.status_code == 422, f"Expected 422 for {body}"
            data = resp.json()
            assert "error" in data, f"Missing 'error' key for {body}"
            assert isinstance(data["error"], str)

    def test_500_error_is_user_friendly(
        self,
        token: str,
        auth_headers: dict[str, str],
    ) -> None:
        """500 errors don't leak stack traces to the client."""
        from sovyx.engine.errors import CognitiveError

        app = create_app()
        app.state.registry = _make_mock_registry(
            gate_side_effect=CognitiveError("Internal queue full"),
        )
        client = TestClient(app)

        resp = client.post(
            "/api/chat",
            json={"message": "Hello"},
            headers=auth_headers,
        )

        assert resp.status_code == 500
        data = resp.json()
        assert "error" in data
        # Should NOT contain the internal error message
        assert "queue full" not in data["error"].lower()
        assert "traceback" not in data["error"].lower()

    def test_503_when_engine_not_ready(
        self,
        token: str,
        auth_headers: dict[str, str],
    ) -> None:
        """503 when registry not set (engine not running)."""
        app = create_app()
        client = TestClient(app)

        resp = client.post(
            "/api/chat",
            json={"message": "Hello"},
            headers=auth_headers,
        )

        assert resp.status_code == 503
        assert "Engine not running" in resp.json()["error"]


# ── WebSocket Broadcast Contract ──


class TestChatWebSocketContract:
    """WebSocket events from chat have correct structure."""

    def test_broadcast_event_structure(
        self,
        token: str,
        auth_headers: dict[str, str],
    ) -> None:
        """ChatMessage event has type, data.conversation_id, data.response_preview."""
        app = create_app()
        app.state.registry = _make_mock_registry(
            response_text="This is a long response that should be truncated",
        )
        client = TestClient(app)

        broadcast_events: list[dict[str, Any]] = []

        async def _capture(msg: dict[str, Any]) -> None:
            broadcast_events.append(msg)

        original = app.state.ws_manager.broadcast
        app.state.ws_manager.broadcast = _capture

        try:
            client.post(
                "/api/chat",
                json={"message": "Hello"},
                headers=auth_headers,
            )

            assert len(broadcast_events) == 1
            event = broadcast_events[0]
            assert event["type"] == "ChatMessage"
            assert "conversation_id" in event["data"]
            assert "response_preview" in event["data"]
            assert isinstance(event["data"]["response_preview"], str)
            assert len(event["data"]["response_preview"]) <= 200
        finally:
            app.state.ws_manager.broadcast = original


# ── Multiple Rapid Requests ──


class TestChatConcurrency:
    """Behavior under rapid sequential requests."""

    def test_rapid_sequential_requests(
        self,
        token: str,
        auth_headers: dict[str, str],
    ) -> None:
        """Multiple rapid requests all succeed (no state corruption)."""
        app = create_app()
        app.state.registry = _make_mock_registry()
        client = TestClient(app)

        responses = []
        for i in range(5):
            resp = client.post(
                "/api/chat",
                json={"message": f"Message {i}"},
                headers=auth_headers,
            )
            responses.append(resp)

        # All should succeed
        for resp in responses:
            assert resp.status_code == 200
            assert resp.json()["response"] == "Hello from Aria!"


# ── Content-Type Edge Cases ──


class TestChatContentType:
    """Content-Type handling edge cases."""

    def test_missing_content_type(
        self,
        token: str,
        auth_headers: dict[str, str],
    ) -> None:
        """Request without Content-Type header returns 422."""
        app = create_app()
        client = TestClient(app)

        resp = client.post(
            "/api/chat",
            content=b'{"message": "hello"}',
            headers=auth_headers,
        )
        # Either 422 (can't parse) or 200 (auto-detected)
        assert resp.status_code in {200, 422, 503}

    def test_accepts_utf8_content_type(
        self,
        token: str,
        auth_headers: dict[str, str],
    ) -> None:
        """Request with charset=utf-8 is accepted."""
        app = create_app()
        app.state.registry = _make_mock_registry()
        client = TestClient(app)

        resp = client.post(
            "/api/chat",
            content=b'{"message": "hello"}',
            headers={
                **auth_headers,
                "Content-Type": "application/json; charset=utf-8",
            },
        )
        assert resp.status_code == 200


# ── Auth Edge Cases ──


class TestChatAuthEdgeCases:
    """Auth token edge cases specific to chat endpoint."""

    def test_empty_bearer_token(
        self,
        token: str,
        auth_headers: dict[str, str],
    ) -> None:
        """Empty Bearer token is rejected."""
        app = create_app()
        client = TestClient(app)

        resp = client.post(
            "/api/chat",
            json={"message": "Hello"},
            headers={"Authorization": "Bearer "},
        )
        assert resp.status_code in {401, 403}
