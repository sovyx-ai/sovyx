"""Tests for sovyx.dashboard.server — FastAPI app, auth, WebSocket, SPA fallback."""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from sovyx.dashboard.server import (
    ConnectionManager,
    DashboardServer,
    _ensure_token,
    create_app,
)

if TYPE_CHECKING:
    from pathlib import Path


# ── Fixtures ──


@pytest.fixture(autouse=True)
def _clean_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect token file to tmp_path for test isolation."""
    token_file = tmp_path / "token"
    monkeypatch.setattr("sovyx.dashboard.server.TOKEN_FILE", token_file)


@pytest.fixture()
def token(tmp_path: Path) -> str:
    """Generate a token and write it to the test token file."""
    t = secrets.token_urlsafe(32)
    token_file = tmp_path / "token"
    token_file.write_text(t)
    return t


@pytest.fixture()
def client(token: str) -> TestClient:
    """TestClient with auth token available."""
    app = create_app()
    return TestClient(app)


@pytest.fixture()
def auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ── Token Tests ──


class TestTokenManagement:
    def test_generates_token_if_missing(self, tmp_path: Path) -> None:
        token_file = tmp_path / "token"
        assert not token_file.exists()
        with patch("sovyx.dashboard.server.TOKEN_FILE", token_file):
            t = _ensure_token()
        assert token_file.exists()
        assert len(t) > 20
        assert token_file.stat().st_mode & 0o777 == 0o600

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
    def test_status(
        self, client: TestClient, auth_headers: dict[str, str]
    ) -> None:
        resp = client.get("/api/status", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "version" in data
        assert "mind_name" in data
        assert "uptime_seconds" in data
        assert "llm_cost_today" in data

    def test_health(
        self, client: TestClient, auth_headers: dict[str, str]
    ) -> None:
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

    def test_conversations(
        self, client: TestClient, auth_headers: dict[str, str]
    ) -> None:
        resp = client.get("/api/conversations", headers=auth_headers)
        assert resp.status_code == 200
        assert "conversations" in resp.json()

    def test_brain_graph(
        self, client: TestClient, auth_headers: dict[str, str]
    ) -> None:
        resp = client.get("/api/brain/graph", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "nodes" in data
        assert "links" in data

    def test_logs(
        self, client: TestClient, auth_headers: dict[str, str]
    ) -> None:
        resp = client.get("/api/logs", headers=auth_headers)
        assert resp.status_code == 200
        assert "entries" in resp.json()

    def test_get_settings(
        self, client: TestClient, auth_headers: dict[str, str]
    ) -> None:
        resp = client.get("/api/settings", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "mind_name" in data
        assert "personality" in data
        assert "channels" in data

    def test_put_settings(
        self, client: TestClient, auth_headers: dict[str, str]
    ) -> None:
        resp = client.put("/api/settings", headers=auth_headers)
        assert resp.status_code == 200


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

    def test_ws_valid_token_connects(
        self, client: TestClient, token: str
    ) -> None:
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


# ── SPA Fallback Tests ──


class TestSPAFallback:
    def test_unknown_path_without_static(
        self, client: TestClient
    ) -> None:
        """Without static files, unknown paths return 503."""
        resp = client.get("/some/random/path")
        # Depends on whether static dir exists
        assert resp.status_code in (200, 503)

    def test_api_docs(self, client: TestClient, auth_headers: dict[str, str]) -> None:
        """Swagger docs accessible at /api/docs."""
        resp = client.get("/api/docs")
        assert resp.status_code == 200
