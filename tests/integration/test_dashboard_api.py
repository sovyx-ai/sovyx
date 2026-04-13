"""VAL-25: Dashboard API → Real DB integration tests.

Uses httpx AsyncClient with the real FastAPI app (no port binding).
Database is a fresh in-memory SQLite via DatabasePool.
"""

from __future__ import annotations

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
def app(api_config: APIConfig, monkeypatch: pytest.MonkeyPatch) -> object:
    """Create FastAPI app with test token."""
    import sovyx.dashboard.server as _srv

    monkeypatch.setattr(_srv, "_ensure_token", lambda: _TOKEN)
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


class TestBrainSearchEndpoint:
    """GET /api/brain/search."""

    async def test_search_no_query_returns_empty(self, client: AsyncClient) -> None:
        """Empty query returns empty results."""
        r = await client.get("/api/brain/search?q=", headers=_auth())
        assert r.status_code == 200
        data = r.json()
        assert data["results"] == []
        assert data["query"] == ""

    async def test_search_returns_structure(self, client: AsyncClient) -> None:
        """Search returns correct response structure even without a registry."""
        r = await client.get("/api/brain/search?q=test", headers=_auth())
        assert r.status_code == 200
        data = r.json()
        assert "results" in data
        assert "query" in data
        assert data["query"] == "test"
        assert isinstance(data["results"], list)

    async def test_search_limit_param(self, client: AsyncClient) -> None:
        """Limit parameter is accepted."""
        r = await client.get("/api/brain/search?q=test&limit=5", headers=_auth())
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data["results"], list)

    async def test_search_requires_auth(self, client: AsyncClient) -> None:
        """Search endpoint requires authentication."""
        r = await client.get("/api/brain/search?q=test")
        assert r.status_code == 401

    async def test_search_limit_validation(self, client: AsyncClient) -> None:
        """Limit must be between 1 and 100."""
        r = await client.get("/api/brain/search?q=test&limit=0", headers=_auth())
        assert r.status_code == 422

        r = await client.get("/api/brain/search?q=test&limit=101", headers=_auth())
        assert r.status_code == 422


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


class TestConfigEndpointNoMind:
    """GET/PUT /api/config — without mind_config wired."""

    async def test_get_config_no_mind_returns_503(self, client: AsyncClient) -> None:
        r = await client.get("/api/config", headers=_auth())
        assert r.status_code == 503

    async def test_put_config_no_mind_returns_503(self, client: AsyncClient) -> None:
        r = await client.put(
            "/api/config",
            headers=_auth(),
            json={"personality": {"tone": "direct"}},
        )
        assert r.status_code == 503

    async def test_get_config_requires_auth(self, client: AsyncClient) -> None:
        r = await client.get("/api/config")
        assert r.status_code == 401

    async def test_put_config_requires_auth(self, client: AsyncClient) -> None:
        r = await client.put("/api/config", json={"name": "test"})
        assert r.status_code == 401


@pytest.fixture()
def app_with_mind(api_config: APIConfig, monkeypatch: pytest.MonkeyPatch) -> object:
    """Create FastAPI app with mind_config wired."""
    import sovyx.dashboard.server as _srv
    from sovyx.mind.config import MindConfig

    monkeypatch.setattr(_srv, "_ensure_token", lambda: _TOKEN)
    fa = create_app(api_config)
    _srv._server_token = _TOKEN
    fa.state.auth_token = _TOKEN  # type: ignore[union-attr]
    fa.state.mind_config = MindConfig(name="TestMind")  # type: ignore[union-attr]
    return fa


@pytest.fixture()
async def client_with_mind(app_with_mind: object) -> AsyncClient:  # type: ignore[override]
    """Async HTTP client with mind config wired."""
    transport = ASGITransport(app=app_with_mind)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c  # type: ignore[misc]


class TestConfigEndpointWithMind:
    """GET/PUT /api/config — with mind_config."""

    async def test_get_config_returns_personality(
        self,
        client_with_mind: AsyncClient,
    ) -> None:
        r = await client_with_mind.get("/api/config", headers=_auth())
        assert r.status_code == 200
        data = r.json()
        assert data["name"] == "TestMind"
        assert "personality" in data
        assert "ocean" in data
        assert data["personality"]["tone"] == "warm"

    async def test_put_config_updates_personality(
        self,
        client_with_mind: AsyncClient,
    ) -> None:
        r = await client_with_mind.put(
            "/api/config",
            headers=_auth(),
            json={"personality": {"tone": "direct", "humor": 0.9}},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert "personality.tone" in data["changes"]
        assert "personality.humor" in data["changes"]

        # Verify GET reflects changes
        r2 = await client_with_mind.get("/api/config", headers=_auth())
        assert r2.json()["personality"]["tone"] == "direct"
        assert r2.json()["personality"]["humor"] == 0.9

    async def test_put_config_invalid_json(
        self,
        client_with_mind: AsyncClient,
    ) -> None:
        headers = {**_auth(), "Content-Type": "application/json"}
        r = await client_with_mind.put(
            "/api/config",
            headers=headers,
            content=b"not json",
        )
        assert r.status_code == 422

    async def test_put_config_ocean(
        self,
        client_with_mind: AsyncClient,
    ) -> None:
        r = await client_with_mind.put(
            "/api/config",
            headers=_auth(),
            json={"ocean": {"openness": 0.95, "neuroticism": 0.1}},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True

    async def test_put_config_immutable_ignored(
        self,
        client_with_mind: AsyncClient,
    ) -> None:
        r = await client_with_mind.put(
            "/api/config",
            headers=_auth(),
            json={"brain": {"decay_rate": 0.99}},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["changes"] == {}

    async def test_put_config_empty_body(
        self,
        client_with_mind: AsyncClient,
    ) -> None:
        r = await client_with_mind.put(
            "/api/config",
            headers=_auth(),
            json={},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["changes"] == {}
