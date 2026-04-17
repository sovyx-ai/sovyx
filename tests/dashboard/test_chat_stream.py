"""Tests for POST /api/chat/stream SSE endpoint."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.testclient import TestClient

from sovyx.dashboard.server import create_app

_TOKEN = "test-token-stream"


@pytest.fixture()
def app():
    application = create_app(token=_TOKEN)
    registry = MagicMock()
    registry.is_registered.return_value = True
    registry.resolve = AsyncMock()
    application.state.registry = registry
    application.state.mind_config = MagicMock()
    application.state.mind_config.configure_mock(name="test")
    application.state.mind_yaml_path = None
    application.state.ws_manager = MagicMock()
    application.state.ws_manager.broadcast = AsyncMock()
    return application


@pytest.fixture()
def client(app) -> TestClient:
    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})


class TestStreamValidation:
    """POST /api/chat/stream input validation."""

    def test_missing_message_returns_sse_error(self, client: TestClient) -> None:
        resp = client.post(
            "/api/chat/stream",
            json={},
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
        assert resp.status_code == 200  # noqa: PLR2004
        assert "text/event-stream" in resp.headers["content-type"]
        assert "error" in resp.text

    def test_empty_message_returns_sse_error(self, client: TestClient) -> None:
        resp = client.post(
            "/api/chat/stream",
            json={"message": ""},
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
        assert resp.status_code == 200  # noqa: PLR2004
        assert "error" in resp.text

    def test_no_registry_returns_sse_error(self) -> None:
        application = create_app(token=_TOKEN)
        application.state.registry = None
        c = TestClient(application, headers={"Authorization": f"Bearer {_TOKEN}"})
        resp = c.post("/api/chat/stream", json={"message": "hello"})
        assert "error" in resp.text

    def test_no_auth_401(self) -> None:
        application = create_app(token=_TOKEN)
        c = TestClient(application)
        resp = c.post("/api/chat/stream", json={"message": "hello"})
        assert resp.status_code == 401  # noqa: PLR2004


class TestStreamEndpoint:
    """POST /api/chat/stream returns SSE events."""

    def test_content_type_is_event_stream(self, client: TestClient) -> None:
        resp = client.post(
            "/api/chat/stream",
            json={"message": "hello"},
        )
        assert "text/event-stream" in resp.headers.get("content-type", "")

    def test_invalid_json_returns_error(self, client: TestClient) -> None:
        resp = client.post(
            "/api/chat/stream",
            content=b"not json",
            headers={
                "Authorization": f"Bearer {_TOKEN}",
                "Content-Type": "application/json",
            },
        )
        assert "error" in resp.text


class TestBatchEndpointMetadata:
    """POST /api/chat returns LLM metadata (model, tokens, cost)."""

    def test_response_shape_unchanged(self, client: TestClient) -> None:
        from unittest.mock import patch

        with patch(
            "sovyx.dashboard.chat.handle_chat_message",
            new_callable=AsyncMock,
        ) as mock_handle:
            mock_handle.return_value = {
                "response": "Hello!",
                "conversation_id": "conv-1",
                "mind_id": "test",
                "timestamp": "2026-04-16T00:00:00Z",
                "tags": ["brain"],
            }
            resp = client.post("/api/chat", json={"message": "hi"})

        assert resp.status_code == 200  # noqa: PLR2004
        data = resp.json()
        assert "response" in data
        assert "tags" in data
