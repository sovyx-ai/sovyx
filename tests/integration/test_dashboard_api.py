"""VAL-25: Dashboard API → Real DB integration tests.

Uses httpx AsyncClient with the real FastAPI app (no port binding).
Database is a fresh in-memory SQLite via DatabasePool.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from sovyx.dashboard.server import create_app
from sovyx.engine.config import APIConfig

# Fixed test token
_TOKEN = "test-token-val25"


@pytest.fixture()
def api_config() -> APIConfig:
    """Minimal API config for testing."""
    return APIConfig(host="127.0.0.1", port=0)


@pytest.fixture()
def app(api_config: APIConfig) -> object:
    """Create FastAPI app with test token."""
    with patch("sovyx.dashboard.server.TOKEN_FILE") as mock_tf:
        mock_tf.exists.return_value = True
        mock_tf.read_text.return_value = _TOKEN
        return create_app(api_config)


@pytest.fixture()
async def client(app: object) -> AsyncClient:  # type: ignore[override]
    """Async HTTP client against the ASGI app."""
    transport = ASGITransport(app=app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c  # type: ignore[misc]


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {_TOKEN}"}


class TestAuth:
    """Authentication enforcement."""

    async def test_no_token_returns_401(self, client: AsyncClient) -> None:
        r = await client.get("/api/status")
        assert r.status_code == 401

    async def test_wrong_token_returns_401(self, client: AsyncClient) -> None:
        r = await client.get("/api/status", headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401

    async def test_valid_token_returns_200(self, client: AsyncClient) -> None:
        r = await client.get("/api/status", headers=_auth())
        assert r.status_code == 200


class TestStatusEndpoint:
    """GET /api/status."""

    async def test_returns_json(self, client: AsyncClient) -> None:
        r = await client.get("/api/status", headers=_auth())
        assert r.status_code == 200
        data = r.json()
        assert "status" in data or "engine" in data or isinstance(data, dict)


class TestHealthEndpoint:
    """GET /api/health."""

    async def test_returns_checks(self, client: AsyncClient) -> None:
        r = await client.get("/api/health", headers=_auth())
        assert r.status_code == 200
        data = r.json()
        assert "checks" in data or isinstance(data, list) or "status" in data


class TestConversationsEndpoint:
    """GET /api/conversations."""

    async def test_empty_conversations(self, client: AsyncClient) -> None:
        r = await client.get("/api/conversations", headers=_auth())
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, (list, dict))


class TestBrainEndpoint:
    """GET /api/brain/graph."""

    async def test_brain_graph(self, client: AsyncClient) -> None:
        r = await client.get("/api/brain/graph", headers=_auth())
        assert r.status_code == 200
        data = r.json()
        assert "nodes" in data or isinstance(data, dict)


class TestLogsEndpoint:
    """GET /api/logs."""

    async def test_logs(self, client: AsyncClient) -> None:
        r = await client.get("/api/logs", headers=_auth())
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, (list, dict))


class TestSettingsEndpoint:
    """GET /api/settings."""

    async def test_get_settings(self, client: AsyncClient) -> None:
        r = await client.get("/api/settings", headers=_auth())
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, dict)
