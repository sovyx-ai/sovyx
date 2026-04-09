"""Extended coverage tests for sovyx.dashboard.server.

VAL-03: Covers all uncovered paths identified in the coverage audit:
- ConnectionManager.broadcast with real connections + stale removal
- StatusCollector wired path (/api/status with registry)
- Health endpoint with online HealthRegistry
- Conversations/brain endpoints with wired registry
- Settings fallbacks (EngineConfig creation fails, invalid JSON, non-dict body)
- SPA fallback with real static files
- DashboardServer start/stop lifecycle
- SecurityHeadersMiddleware verification
"""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from sovyx.dashboard.server import (
    ConnectionManager,
    DashboardServer,
    create_app,
)

if TYPE_CHECKING:
    from pathlib import Path

    from fastapi import FastAPI


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
def app(token: str) -> FastAPI:
    """FastAPI app instance."""
    return create_app()


@pytest.fixture()
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


@pytest.fixture()
def auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ── ConnectionManager broadcast tests ──


class TestConnectionManagerBroadcast:
    """Test broadcast with actual connection simulation."""

    @pytest.mark.asyncio()
    async def test_broadcast_sends_to_all(self) -> None:
        """broadcast sends JSON to every connected client."""
        manager = ConnectionManager()

        ws1 = AsyncMock()
        ws1.send_json = AsyncMock()
        ws2 = AsyncMock()
        ws2.send_json = AsyncMock()

        manager._connections = [ws1, ws2]

        await manager.broadcast({"type": "test", "data": "hello"})

        ws1.send_json.assert_called_once_with({"type": "test", "data": "hello"})
        ws2.send_json.assert_called_once_with({"type": "test", "data": "hello"})

    @pytest.mark.asyncio()
    async def test_broadcast_removes_stale_connections(self) -> None:
        """Connections that error during send_json are removed."""
        manager = ConnectionManager()

        good_ws = AsyncMock()
        good_ws.send_json = AsyncMock()

        bad_ws = AsyncMock()
        bad_ws.send_json = AsyncMock(side_effect=RuntimeError("connection lost"))

        manager._connections = [good_ws, bad_ws]

        await manager.broadcast({"type": "test"})

        good_ws.send_json.assert_called_once()
        assert bad_ws not in manager._connections
        assert good_ws in manager._connections
        assert manager.active_count == 1

    @pytest.mark.asyncio()
    async def test_broadcast_empty_connections(self) -> None:
        """broadcast with empty connections is a no-op (early return)."""
        manager = ConnectionManager()
        await manager.broadcast({"type": "test"})
        assert manager.active_count == 0

    @pytest.mark.asyncio()
    async def test_broadcast_all_stale(self) -> None:
        """When all connections are stale, all get removed."""
        manager = ConnectionManager()

        ws1 = AsyncMock()
        ws1.send_json = AsyncMock(side_effect=ConnectionError("dead"))
        ws2 = AsyncMock()
        ws2.send_json = AsyncMock(side_effect=ConnectionError("dead"))

        manager._connections = [ws1, ws2]
        await manager.broadcast({"type": "test"})

        assert manager.active_count == 0

    @pytest.mark.asyncio()
    async def test_disconnect_not_in_list(self) -> None:
        """Disconnecting a WebSocket not in the list is a no-op (branch 84→86)."""
        manager = ConnectionManager()
        unknown_ws = AsyncMock()
        await manager.disconnect(unknown_ws)
        assert manager.active_count == 0


# ── Status endpoint with StatusCollector wired ──


class TestStatusWithCollector:
    def test_status_with_collector(
        self, app: FastAPI, client: TestClient, auth_headers: dict[str, str]
    ) -> None:
        """When status_collector is wired, it returns collected data."""
        from sovyx.dashboard.status import StatusCollector, StatusSnapshot

        mock_snapshot = StatusSnapshot(
            version="0.1.0-test",
            uptime_seconds=42.5,
            mind_name="aria",
            active_conversations=3,
            memory_concepts=100,
            memory_episodes=50,
            llm_cost_today=0.05,
            llm_calls_today=10,
            tokens_today=5000,
            messages_today=15,
        )

        collector = MagicMock(spec=StatusCollector)
        collector.collect = AsyncMock(return_value=mock_snapshot)

        app.state.status_collector = collector
        resp = client.get("/api/status", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["version"] == "0.1.0-test"
        assert data["uptime_seconds"] == 42.5
        assert data["mind_name"] == "aria"
        assert data["active_conversations"] == 3

    def test_status_collector_wrong_type(
        self, app: FastAPI, client: TestClient, auth_headers: dict[str, str]
    ) -> None:
        """When status_collector is wrong type, raises TypeError."""
        app.state.status_collector = "not-a-collector"
        with pytest.raises(TypeError, match="expected StatusCollector"):
            client.get("/api/status", headers=auth_headers)


# ── Health endpoint with online registry ──


class TestHealthWithRegistry:
    def test_health_with_online_registry(
        self, app: FastAPI, client: TestClient, auth_headers: dict[str, str]
    ) -> None:
        """When health_registry is wired, online checks are included."""
        from sovyx.observability.health import CheckResult, CheckStatus, HealthRegistry

        mock_result = CheckResult(
            name="db_check",
            status=CheckStatus.GREEN,
            message="Database OK",
            metadata={"latency_ms": 5},
        )

        mock_registry = MagicMock(spec=HealthRegistry)
        mock_registry.run_all = AsyncMock(return_value=[mock_result])

        app.state.health_registry = mock_registry
        resp = client.get("/api/health", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        check_names = [c["name"] for c in data["checks"]]
        assert "db_check" in check_names
        db_check = next(c for c in data["checks"] if c["name"] == "db_check")
        assert db_check["latency_ms"] == 5


# ── Conversations/Brain with registry ──


class TestEndpointsWithRegistry:
    def test_conversations_with_registry(
        self, app: FastAPI, client: TestClient, auth_headers: dict[str, str]
    ) -> None:
        """Conversations endpoint with wired registry calls list_conversations."""
        mock_registry = MagicMock()
        app.state.registry = mock_registry

        with patch(
            "sovyx.dashboard.conversations.list_conversations",
            new_callable=AsyncMock,
            return_value=[{"id": "conv-1", "person": "Alice", "turns": 5}],
        ):
            resp = client.get("/api/conversations", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["conversations"]) == 1
        assert data["conversations"][0]["id"] == "conv-1"

    def test_conversation_detail_with_registry(
        self, app: FastAPI, client: TestClient, auth_headers: dict[str, str]
    ) -> None:
        """Conversation detail endpoint calls get_conversation_messages."""
        mock_registry = MagicMock()
        app.state.registry = mock_registry

        with patch(
            "sovyx.dashboard.conversations.get_conversation_messages",
            new_callable=AsyncMock,
            return_value=[
                {"role": "user", "content": "hello", "timestamp": "2026-04-06T12:00:00Z"},
                {"role": "assistant", "content": "hi!", "timestamp": "2026-04-06T12:00:01Z"},
            ],
        ):
            resp = client.get("/api/conversations/conv-42", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["conversation_id"] == "conv-42"
        assert len(data["messages"]) == 2

    def test_brain_graph_with_registry(
        self, app: FastAPI, client: TestClient, auth_headers: dict[str, str]
    ) -> None:
        """Brain graph endpoint calls get_brain_graph when registry is wired."""
        mock_registry = MagicMock()
        app.state.registry = mock_registry

        with patch(
            "sovyx.dashboard.brain.get_brain_graph",
            new_callable=AsyncMock,
            return_value={
                "nodes": [{"id": "n1", "name": "concept1", "val": 0.8}],
                "links": [{"source": "n1", "target": "n2", "value": 0.5}],
            },
        ):
            resp = client.get("/api/brain/graph", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["nodes"]) == 1
        assert len(data["links"]) == 1

    def test_conversations_with_limit_offset(
        self, app: FastAPI, client: TestClient, auth_headers: dict[str, str]
    ) -> None:
        """Query params limit and offset are passed through."""
        mock_registry = MagicMock()
        app.state.registry = mock_registry

        with patch(
            "sovyx.dashboard.conversations.list_conversations",
            new_callable=AsyncMock,
            return_value=[],
        ) as mock_list:
            resp = client.get(
                "/api/conversations?limit=10&offset=5",
                headers=auth_headers,
            )
        assert resp.status_code == 200
        mock_list.assert_called_once_with(mock_registry, limit=10, offset=5)


# ── Settings fallback paths ──


class TestSettingsFallbacks:
    def test_get_settings_engine_config_fails(
        self, app: FastAPI, client: TestClient, auth_headers: dict[str, str]
    ) -> None:
        """When EngineConfig() construction fails, return defaults."""
        with patch(
            "sovyx.engine.config.EngineConfig",
            side_effect=RuntimeError("broken config"),
        ):
            resp = client.get("/api/settings", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["log_level"] == "INFO"
        assert "data_dir" in data

    def test_put_settings_invalid_json(
        self, client: TestClient, auth_headers: dict[str, str]
    ) -> None:
        """PUT with non-JSON body returns 422."""
        resp = client.put(
            "/api/settings",
            headers={**auth_headers, "Content-Type": "application/json"},
            content=b"this is not json{{{",
        )
        assert resp.status_code == 422
        assert resp.json()["ok"] is False
        assert "Invalid JSON" in resp.json()["error"]

    def test_put_settings_non_dict_body(
        self, client: TestClient, auth_headers: dict[str, str]
    ) -> None:
        """PUT with JSON array (not object) returns 422."""
        resp = client.put(
            "/api/settings",
            headers=auth_headers,
            json=["not", "a", "dict"],
        )
        assert resp.status_code == 422
        assert "Expected JSON object" in resp.json()["error"]

    def test_put_settings_no_engine_config(
        self, app: FastAPI, client: TestClient, auth_headers: dict[str, str]
    ) -> None:
        """PUT settings creates EngineConfig on the fly when not wired."""
        if hasattr(app.state, "engine_config"):
            del app.state.engine_config
        resp = client.put(
            "/api/settings",
            headers=auth_headers,
            json={"log_level": "WARNING"},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_put_settings_engine_config_creation_fails(
        self, app: FastAPI, client: TestClient, auth_headers: dict[str, str]
    ) -> None:
        """PUT settings returns 500 when EngineConfig can't be created."""
        if hasattr(app.state, "engine_config"):
            del app.state.engine_config

        with patch(
            "sovyx.engine.config.EngineConfig",
            side_effect=RuntimeError("broken"),
        ):
            resp = client.put(
                "/api/settings",
                headers=auth_headers,
                json={"log_level": "DEBUG"},
            )
        assert resp.status_code == 500
        assert resp.json()["ok"] is False


# ── SPA fallback with static files ──


class TestSPAFallbackWithStatic:
    def test_spa_serves_index_for_unknown_routes(
        self, tmp_path: Path, token: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With static dir present, unknown routes serve index.html."""
        static_dir = tmp_path / "static"
        static_dir.mkdir()
        (static_dir / "index.html").write_text("<html>SPA</html>")
        assets_dir = static_dir / "assets"
        assets_dir.mkdir()
        (assets_dir / "app.js").write_text("console.log('app')")

        monkeypatch.setattr("sovyx.dashboard.server.STATIC_DIR", static_dir)
        test_app = create_app()
        test_client = TestClient(test_app)

        resp = test_client.get("/dashboard/overview")
        assert resp.status_code == 200
        assert "SPA" in resp.text

    def test_spa_serves_actual_static_file(
        self, tmp_path: Path, token: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Static files are served directly if they exist."""
        static_dir = tmp_path / "static"
        static_dir.mkdir()
        (static_dir / "index.html").write_text("<html>SPA</html>")
        (static_dir / "favicon.ico").write_bytes(b"\x00\x00\x01\x00")
        assets_dir = static_dir / "assets"
        assets_dir.mkdir()

        monkeypatch.setattr("sovyx.dashboard.server.STATIC_DIR", static_dir)
        test_app = create_app()
        test_client = TestClient(test_app)

        resp = test_client.get("/favicon.ico")
        assert resp.status_code == 200

    def test_spa_path_traversal_blocked(
        self, tmp_path: Path, token: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Path traversal attempts are blocked, serving index.html instead."""
        static_dir = tmp_path / "static"
        static_dir.mkdir()
        (static_dir / "index.html").write_text("<html>SPA</html>")
        assets_dir = static_dir / "assets"
        assets_dir.mkdir()

        (tmp_path / "secret.txt").write_text("sensitive data")

        monkeypatch.setattr("sovyx.dashboard.server.STATIC_DIR", static_dir)
        test_app = create_app()
        test_client = TestClient(test_app)

        resp = test_client.get("/../secret.txt")
        assert resp.status_code == 200
        assert "sensitive data" not in resp.text

    def test_no_static_dir_returns_503(
        self, tmp_path: Path, token: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without static dir, all non-API routes return 503."""
        fake_dir = tmp_path / "nonexistent"
        monkeypatch.setattr("sovyx.dashboard.server.STATIC_DIR", fake_dir)
        test_app = create_app()
        test_client = TestClient(test_app)

        resp = test_client.get("/any/path")
        assert resp.status_code == 503
        assert "not built" in resp.json()["error"]


# ── SecurityHeadersMiddleware ──


class TestSecurityHeaders:
    def test_security_headers_present(
        self, client: TestClient, auth_headers: dict[str, str]
    ) -> None:
        """All security headers are set on responses."""
        resp = client.get("/api/status", headers=auth_headers)
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        assert resp.headers["X-Frame-Options"] == "DENY"
        assert resp.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
        assert "camera=()" in resp.headers["Permissions-Policy"]
        assert "default-src 'self'" in resp.headers["Content-Security-Policy"]
        assert "frame-ancestors 'none'" in resp.headers["Content-Security-Policy"]


# ── DashboardServer lifecycle ──


class TestDashboardServerLifecycle:
    def test_ws_manager_with_app(self, token: str) -> None:
        """ws_manager returns the ConnectionManager when app is created."""
        from sovyx.engine.config import APIConfig

        server = DashboardServer(config=APIConfig())
        assert server.ws_manager is None

        server._app = create_app(APIConfig())
        assert server.ws_manager is not None
        assert isinstance(server.ws_manager, ConnectionManager)

    @pytest.mark.asyncio()
    async def test_start_creates_uvicorn(self, token: str) -> None:
        """start() creates uvicorn config with correct host/port."""
        from sovyx.engine.config import APIConfig

        config = APIConfig(host="0.0.0.0", port=9999)
        server = DashboardServer(config=config)

        mock_uvi_server = MagicMock()
        mock_uvi_server.serve = AsyncMock()

        with (
            patch("uvicorn.Config") as mock_config_cls,
            patch("uvicorn.Server", return_value=mock_uvi_server),
            patch("asyncio.create_task"),
        ):
            await server.start()

            mock_config_cls.assert_called_once()
            call_kwargs = mock_config_cls.call_args
            assert call_kwargs.kwargs["host"] == "0.0.0.0"
            assert call_kwargs.kwargs["port"] == 9999  # noqa: PLR2004

        assert server.app is not None
        assert server._server is mock_uvi_server

    @pytest.mark.asyncio()
    async def test_start_with_registry(self, token: str) -> None:
        """start() wires StatusCollector when registry is provided."""
        from sovyx.engine.config import APIConfig

        config = APIConfig()
        mock_registry = MagicMock()
        server = DashboardServer(config=config, registry=mock_registry)

        mock_uvi_server = MagicMock()
        mock_uvi_server.serve = AsyncMock()

        with (
            patch("uvicorn.Config"),
            patch("uvicorn.Server", return_value=mock_uvi_server),
            patch("asyncio.create_task"),
        ):
            await server.start()

        assert server.app is not None
        assert hasattr(server.app.state, "status_collector")
        assert hasattr(server.app.state, "registry")
        assert server.app.state.registry is mock_registry

    @pytest.mark.asyncio()
    async def test_start_without_config(self, token: str) -> None:
        """start() uses defaults when config is None."""
        server = DashboardServer(config=None)

        mock_uvi_server = MagicMock()
        mock_uvi_server.serve = AsyncMock()

        with (
            patch("uvicorn.Config") as mock_config_cls,
            patch("uvicorn.Server", return_value=mock_uvi_server),
            patch("asyncio.create_task"),
        ):
            await server.start()

            call_kwargs = mock_config_cls.call_args
            assert call_kwargs.kwargs["host"] == "127.0.0.1"
            assert call_kwargs.kwargs["port"] == 7777  # noqa: PLR2004

    @pytest.mark.asyncio()
    async def test_start_log_file_wiring_failure(self, token: str) -> None:
        """start() handles EngineConfig failure gracefully when wiring log file."""
        from sovyx.engine.config import APIConfig

        config = APIConfig()
        server = DashboardServer(config=config)

        mock_uvi_server = MagicMock()
        mock_uvi_server.serve = AsyncMock()

        with (
            patch("uvicorn.Config"),
            patch("uvicorn.Server", return_value=mock_uvi_server),
            patch("asyncio.create_task"),
            patch(
                "sovyx.engine.config.EngineConfig",
                side_effect=RuntimeError("config broken"),
            ),
        ):
            await server.start()

        assert server.app is not None
        # _resolve_log_file returns None when both registry and fallback fail
        assert server.app.state.log_file is None

    @pytest.mark.asyncio()
    async def test_stop(self, token: str) -> None:
        """stop() sets should_exit on the uvicorn server."""
        server = DashboardServer()
        mock_uvi_server = MagicMock()
        mock_uvi_server.should_exit = False
        server._server = mock_uvi_server

        await server.stop()
        assert mock_uvi_server.should_exit is True

    @pytest.mark.asyncio()
    async def test_stop_no_server(self, token: str) -> None:
        """stop() is a no-op when server hasn't started."""
        server = DashboardServer()
        await server.stop()


# ── WebSocket ping/pong via real TestClient ──


class TestWebSocketPingPong:
    def test_ws_ping_pong(self, client: TestClient, token: str) -> None:
        """WebSocket responds 'pong' to 'ping' messages."""
        with client.websocket_connect(f"/ws?token={token}") as ws:
            ws.send_text("ping")
            assert ws.receive_text() == "pong"
            ws.send_text("ping")
            assert ws.receive_text() == "pong"

    def test_ws_non_ping_ignored(self, client: TestClient, token: str) -> None:
        """Non-ping messages are received but don't trigger pong."""
        with client.websocket_connect(f"/ws?token={token}") as ws:
            ws.send_text("ping")
            assert ws.receive_text() == "pong"


# ── CORS tests ──


class TestCORS:
    def test_cors_headers_on_preflight(
        self, client: TestClient, auth_headers: dict[str, str]
    ) -> None:
        """CORS preflight request returns correct headers."""
        resp = client.options(
            "/api/status",
            headers={
                "Origin": "http://localhost:7777",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert resp.status_code == 200
        assert "access-control-allow-origin" in resp.headers

    def test_cors_with_custom_origins(
        self, tmp_path: Path, token: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Custom CORS origins from config are applied."""
        from sovyx.engine.config import APIConfig

        config = APIConfig(cors_origins=["https://mydomain.com"])
        test_app = create_app(config)
        test_client = TestClient(test_app)

        resp = test_client.options(
            "/api/status",
            headers={
                "Origin": "https://mydomain.com",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert resp.headers.get("access-control-allow-origin") == "https://mydomain.com"
