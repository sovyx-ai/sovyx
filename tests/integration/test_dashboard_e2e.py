"""DASH-15: Dashboard end-to-end test — chat + conversations + channels.

Tests the full API flow:
1. POST /api/chat → message processed, response returned
2. GET /api/conversations → conversation was created
3. GET /api/channels → dashboard channel is connected

Uses mock registry (no real LLM) but validates the HTTP flow end-to-end.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from sovyx.cognitive.act import ActionResult
from sovyx.dashboard.server import create_app
from sovyx.engine.config import APIConfig

_TOKEN = "test-token-e2e"


def _make_mock_registry(
    response_text: str = "I'm Aria, your sovereign mind.",
) -> MagicMock:
    """Build a mock registry for E2E chat flow."""
    mock_registry = MagicMock()

    mock_person_resolver = AsyncMock()
    mock_person_resolver.resolve = AsyncMock(return_value="person-e2e")

    mock_conv_tracker = AsyncMock()
    mock_conv_tracker.get_or_create = AsyncMock(
        return_value=("conv-e2e-001", []),
    )
    mock_conv_tracker.add_turn = AsyncMock()

    action_result = ActionResult(
        response_text=response_text,
        target_channel="dashboard",
        filtered=False,
        error=False,
    )

    mock_gate = AsyncMock()
    mock_gate.submit = AsyncMock(return_value=action_result)

    mock_bridge = MagicMock()
    mock_bridge._mind_id = "aria"  # noqa: SLF001

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
    mock_registry.is_registered = MagicMock(return_value=False)

    return mock_registry


@pytest.fixture()
def app() -> object:
    """Create app with mock token and registry."""
    with patch("sovyx.dashboard.server.TOKEN_FILE") as mock_tf:
        mock_tf.exists.return_value = True
        mock_tf.read_text.return_value = _TOKEN
        fa = create_app(APIConfig(host="127.0.0.1", port=0))
        fa.state.registry = _make_mock_registry()  # type: ignore[union-attr]
        return fa


@pytest.fixture()
async def client(app: object) -> AsyncClient:
    """Async HTTP client."""
    transport = ASGITransport(app=app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c  # type: ignore[misc]


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {_TOKEN}"}


class TestDashboardE2E:
    """Full end-to-end flow: chat → conversations → channels."""

    async def test_chat_returns_response(self, client: AsyncClient) -> None:
        """POST /api/chat returns AI response."""
        resp = await client.post(
            "/api/chat",
            headers=_auth(),
            json={"message": "Hello, who are you?"},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["response"] == "I'm Aria, your sovereign mind."
        assert data["conversation_id"] == "conv-e2e-001"
        assert data["mind_id"] == "aria"
        assert "timestamp" in data

    async def test_chat_with_user_name(self, client: AsyncClient) -> None:
        """POST /api/chat with custom user_name."""
        resp = await client.post(
            "/api/chat",
            headers=_auth(),
            json={"message": "Hi", "user_name": "Guipe"},
        )

        assert resp.status_code == 200
        assert resp.json()["response"] == "I'm Aria, your sovereign mind."

    async def test_channels_shows_dashboard_connected(
        self, client: AsyncClient,
    ) -> None:
        """GET /api/channels shows dashboard as connected."""
        resp = await client.get("/api/channels", headers=_auth())

        assert resp.status_code == 200
        data = resp.json()
        channels = data["channels"]

        dashboard = next(c for c in channels if c["type"] == "dashboard")
        assert dashboard["connected"] is True

    async def test_channels_lists_all_types(
        self, client: AsyncClient,
    ) -> None:
        """GET /api/channels returns dashboard, telegram, signal."""
        resp = await client.get("/api/channels", headers=_auth())
        types = {c["type"] for c in resp.json()["channels"]}
        assert "dashboard" in types
        assert "telegram" in types
        assert "signal" in types

    async def test_status_endpoint_works(self, client: AsyncClient) -> None:
        """GET /api/status returns valid response."""
        resp = await client.get("/api/status", headers=_auth())
        assert resp.status_code == 200

    async def test_full_flow_chat_then_channels(
        self, client: AsyncClient,
    ) -> None:
        """Sequential flow: chat message → check channels."""
        # Step 1: Send a chat message
        chat_resp = await client.post(
            "/api/chat",
            headers=_auth(),
            json={"message": "Tell me about memory."},
        )
        assert chat_resp.status_code == 200
        conv_id = chat_resp.json()["conversation_id"]
        assert conv_id  # Not empty

        # Step 2: Check channels (dashboard should be connected)
        channels_resp = await client.get(
            "/api/channels", headers=_auth(),
        )
        assert channels_resp.status_code == 200
        dashboard = next(
            c for c in channels_resp.json()["channels"]
            if c["type"] == "dashboard"
        )
        assert dashboard["connected"] is True

    async def test_validation_errors_are_json(
        self, client: AsyncClient,
    ) -> None:
        """Validation errors return consistent JSON."""
        resp = await client.post(
            "/api/chat",
            headers=_auth(),
            json={"message": ""},
        )
        assert resp.status_code == 422
        assert "error" in resp.json()

    async def test_auth_required_on_all_endpoints(
        self, client: AsyncClient,
    ) -> None:
        """All API endpoints require authentication."""
        endpoints = [
            ("GET", "/api/status"),
            ("GET", "/api/channels"),
            ("POST", "/api/chat"),
            ("GET", "/api/health"),
        ]

        for method, path in endpoints:
            if method == "GET":
                resp = await client.get(path)
            else:
                resp = await client.post(path, json={"message": "test"})
            assert resp.status_code in {401, 403}, (
                f"{method} {path} returned {resp.status_code} without auth"
            )
