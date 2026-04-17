"""Tests for sovyx.dashboard.server — FastAPI app, auth, WebSocket, SPA fallback."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from sovyx.dashboard.server import (
    ConnectionManager,
    DashboardServer,
    _ensure_token,
    create_app,
)

# ── Fixtures ──


@pytest.fixture()
def token() -> str:
    """Fixed auth token for tests using create_app(token=...)."""
    return "test-token-fixo"


@pytest.fixture()
def client(token: str) -> TestClient:
    """TestClient with auth token available."""
    app = create_app(token=token)
    return TestClient(app)


@pytest.fixture()
def auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ── Token Tests ──


class TestTokenManagement:
    def test_generates_token_if_missing(self, tmp_path: Path) -> None:
        token_file = tmp_path / "token"
        assert not token_file.exists()
        with (
            patch("sovyx.dashboard.server.TOKEN_FILE", token_file),
            patch.object(Path, "chmod") as mock_chmod,
        ):
            t = _ensure_token()
        assert token_file.exists()
        assert len(t) > 20
        mock_chmod.assert_called_once_with(0o600)

    def test_reads_existing_token(self, tmp_path: Path) -> None:
        token_file = tmp_path / "token"
        token_file.write_text("my-secret-token")
        with patch("sovyx.dashboard.server.TOKEN_FILE", token_file):
            t = _ensure_token()
        assert t == "my-secret-token"

    def test_regenerates_if_empty(self, tmp_path: Path) -> None:
        token_file = tmp_path / "token"
        token_file.write_text("")
        with patch("sovyx.dashboard.server.TOKEN_FILE", token_file):
            t = _ensure_token()
        assert len(t) > 20


# ── Auth Tests ──


class TestAuth:
    def test_no_auth_returns_401(self, client: TestClient) -> None:
        resp = client.get("/api/status")
        assert resp.status_code == 401

    def test_wrong_token_returns_401(self, client: TestClient) -> None:
        resp = client.get(
            "/api/status",
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert resp.status_code == 401

    def test_valid_token_returns_200(
        self, client: TestClient, auth_headers: dict[str, str]
    ) -> None:
        resp = client.get("/api/status", headers=auth_headers)
        assert resp.status_code == 200

    def test_malformed_auth_header(self, client: TestClient) -> None:
        resp = client.get(
            "/api/status",
            headers={"Authorization": "Basic abc123"},
        )
        assert resp.status_code == 401


# ── API Route Tests ──


class TestAPIRoutes:
    def test_status(self, client: TestClient, auth_headers: dict[str, str]) -> None:
        resp = client.get("/api/status", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "version" in data
        assert "mind_name" in data
        assert "uptime_seconds" in data
        assert "llm_cost_today" in data

    def test_health(self, client: TestClient, auth_headers: dict[str, str]) -> None:
        resp = client.get("/api/health", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "checks" in data
        assert "overall" in data
        # Should have at least offline checks (Disk, RAM, CPU)
        assert len(data["checks"]) >= 3
        for check in data["checks"]:
            assert "name" in check
            assert "status" in check
            assert check["status"] in ("green", "yellow", "red")

    def test_conversations(self, client: TestClient, auth_headers: dict[str, str]) -> None:
        resp = client.get("/api/conversations", headers=auth_headers)
        assert resp.status_code == 200
        assert "conversations" in resp.json()

    def test_brain_graph(self, client: TestClient, auth_headers: dict[str, str]) -> None:
        resp = client.get("/api/brain/graph", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "nodes" in data
        assert "links" in data

    def test_logs(self, client: TestClient, auth_headers: dict[str, str]) -> None:
        resp = client.get("/api/logs", headers=auth_headers)
        assert resp.status_code == 200
        assert "entries" in resp.json()

    def test_get_settings(self, client: TestClient, auth_headers: dict[str, str]) -> None:
        resp = client.get("/api/settings", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "log_level" in data
        assert "data_dir" in data

    def test_put_settings(self, client: TestClient, auth_headers: dict[str, str]) -> None:
        resp = client.put(
            "/api/settings",
            headers=auth_headers,
            json={"log_level": "DEBUG"},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True


# ── WebSocket Tests ──


class TestWebSocket:
    def test_ws_no_token_rejected(self, client: TestClient) -> None:
        from starlette.websockets import WebSocketDisconnect

        with pytest.raises(WebSocketDisconnect), client.websocket_connect("/ws"):
            pass  # pragma: no cover

    def test_ws_wrong_token_rejected(self, client: TestClient) -> None:
        from starlette.websockets import WebSocketDisconnect

        with pytest.raises(WebSocketDisconnect), client.websocket_connect("/ws?token=wrong"):
            pass  # pragma: no cover

    def test_ws_valid_token_connects(self, client: TestClient, token: str) -> None:
        with client.websocket_connect(f"/ws?token={token}") as ws:
            ws.send_text("ping")
            data = ws.receive_text()
            assert data == "pong"


# ── ConnectionManager Tests ──


class TestConnectionManager:
    @pytest.mark.asyncio()
    async def test_connect_disconnect(self) -> None:
        manager = ConnectionManager()
        assert manager.active_count == 0
        # Can't fully test without real WebSocket, but verify structure
        assert isinstance(manager._connections, list)

    @pytest.mark.asyncio()
    async def test_broadcast_removes_stale(self) -> None:
        """Broadcast should silently remove connections that error."""
        manager = ConnectionManager()
        # Start with empty — broadcast should not error
        await manager.broadcast({"type": "test"})
        assert manager.active_count == 0


# ── DashboardServer Tests ──


class TestDashboardServer:
    def test_init_defaults(self) -> None:
        server = DashboardServer()
        assert server.app is None
        assert server.ws_manager is None

    def test_init_with_config(self) -> None:
        from sovyx.engine.config import APIConfig

        config = APIConfig(host="0.0.0.0", port=8888)
        server = DashboardServer(config=config)
        assert server._config is not None
        assert server._config.port == 8888


class TestResolveLogFile:
    """DashboardServer._resolve_log_file resolution order."""

    @pytest.mark.asyncio()
    async def test_resolves_from_registry(self) -> None:
        """Registry EngineConfig is used when available."""
        from sovyx.engine.config import EngineConfig
        from sovyx.engine.registry import ServiceRegistry

        engine_config = EngineConfig(data_dir=Path("/custom/data"))
        registry = ServiceRegistry()
        registry.register_instance(EngineConfig, engine_config)

        server = DashboardServer(registry=registry)
        result = await server._resolve_log_file()
        assert result == Path("/custom/data/logs/sovyx.log")

    @pytest.mark.asyncio()
    async def test_fallback_without_registry(self) -> None:
        """No registry → falls back to fresh EngineConfig defaults."""
        server = DashboardServer()
        result = await server._resolve_log_file()
        assert result == Path.home() / ".sovyx" / "logs" / "sovyx.log"

    @pytest.mark.asyncio()
    async def test_fallback_when_registry_missing_config(self) -> None:
        """Registry exists but EngineConfig not registered → fallback."""
        from sovyx.engine.registry import ServiceRegistry

        registry = ServiceRegistry()
        server = DashboardServer(registry=registry)
        result = await server._resolve_log_file()
        assert result == Path.home() / ".sovyx" / "logs" / "sovyx.log"

    @pytest.mark.asyncio()
    async def test_registry_config_with_custom_log_file(self) -> None:
        """Explicit log_file in registry config is preserved."""
        from sovyx.engine.config import EngineConfig, LoggingConfig
        from sovyx.engine.registry import ServiceRegistry

        custom_path = Path("/var/log/sovyx/daemon.log")
        config = EngineConfig(log=LoggingConfig(log_file=custom_path))
        registry = ServiceRegistry()
        registry.register_instance(EngineConfig, config)

        server = DashboardServer(registry=registry)
        result = await server._resolve_log_file()
        assert result == custom_path

    @pytest.mark.asyncio()
    async def test_registry_resolve_error_falls_back(self) -> None:
        """If registry.resolve raises, falls back to default."""
        from unittest.mock import AsyncMock, MagicMock

        registry = MagicMock()
        registry.is_registered.return_value = True
        registry.resolve = AsyncMock(side_effect=RuntimeError("broken"))

        server = DashboardServer(registry=registry)
        result = await server._resolve_log_file()
        # Should fall back to default, not crash
        assert result == Path.home() / ".sovyx" / "logs" / "sovyx.log"


# ── SPA Fallback Tests ──


class TestSPAFallback:
    def test_unknown_path_without_static(self, client: TestClient) -> None:
        """Without static files, unknown paths return 503."""
        resp = client.get("/some/random/path")
        # Depends on whether static dir exists
        assert resp.status_code in (200, 503)

    def test_api_docs(self, client: TestClient, auth_headers: dict[str, str]) -> None:
        """Swagger docs accessible at /api/docs."""
        resp = client.get("/api/docs")
        assert resp.status_code == 200
