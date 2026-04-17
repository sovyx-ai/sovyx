"""Tests for GET /api/channels endpoint (DASH-09)."""

from __future__ import annotations

import secrets
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from starlette.testclient import TestClient

from sovyx.dashboard.server import create_app


@pytest.fixture(autouse=True)
def _clean_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect token file to tmp_path."""
    token_file = tmp_path / "token"
    monkeypatch.setattr("sovyx.dashboard.server.TOKEN_FILE", token_file)


@pytest.fixture()
def token(tmp_path: Path) -> str:
    """Generate a test token."""
    t = secrets.token_urlsafe(32)
    (tmp_path / "token").write_text(t)
    return t


@pytest.fixture()
def auth_headers(token: str) -> dict[str, str]:
    """Auth headers."""
    return {"Authorization": f"Bearer {token}"}


class TestChannelsEndpoint:
    """Tests for GET /api/channels."""

    def test_requires_auth(self, token: str) -> None:
        """Unauthenticated request returns 401."""
        app = create_app()
        client = TestClient(app)
        resp = client.get("/api/channels")
        assert resp.status_code == 401

    def test_returns_channels_without_registry(
        self,
        token: str,
        auth_headers: dict[str, str],
    ) -> None:
        """Without registry, dashboard disconnected and others not connected."""
        app = create_app()
        client = TestClient(app)
        resp = client.get("/api/channels", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "channels" in data
        channels = data["channels"]
        assert len(channels) >= 3  # dashboard, telegram, signal

        # Find dashboard
        dashboard = next(c for c in channels if c["type"] == "dashboard")
        assert dashboard["connected"] is False  # no registry

    def test_dashboard_connected_with_registry(
        self,
        token: str,
        auth_headers: dict[str, str],
    ) -> None:
        """With registry, dashboard shows connected."""
        from unittest.mock import AsyncMock

        app = create_app()
        mock_registry = MagicMock()
        # BridgeManager with no adapters
        mock_bridge = MagicMock()
        mock_bridge._adapters = {}
        mock_registry.resolve = AsyncMock(return_value=mock_bridge)
        app.state.registry = mock_registry
        client = TestClient(app)

        resp = client.get("/api/channels", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()

        dashboard = next(c for c in data["channels"] if c["type"] == "dashboard")
        assert dashboard["connected"] is True

    def test_telegram_connected_when_registered(
        self,
        token: str,
        auth_headers: dict[str, str],
    ) -> None:
        """Telegram shows connected when channel is in BridgeManager."""
        from unittest.mock import AsyncMock

        from sovyx.engine.types import ChannelType

        app = create_app()
        mock_registry = MagicMock()
        mock_bridge = MagicMock()
        mock_bridge._adapters = {ChannelType.TELEGRAM: MagicMock()}
        mock_registry.resolve = AsyncMock(return_value=mock_bridge)
        app.state.registry = mock_registry
        client = TestClient(app)

        resp = client.get("/api/channels", headers=auth_headers)
        data = resp.json()

        telegram = next(c for c in data["channels"] if c["type"] == "telegram")
        assert telegram["connected"] is True

    def test_channels_have_required_fields(
        self,
        token: str,
        auth_headers: dict[str, str],
    ) -> None:
        """Each channel has name, type, and connected fields."""
        from unittest.mock import AsyncMock

        app = create_app()
        mock_registry = MagicMock()
        mock_bridge = MagicMock()
        mock_bridge._adapters = {}
        mock_registry.resolve = AsyncMock(return_value=mock_bridge)
        app.state.registry = mock_registry
        client = TestClient(app)

        resp = client.get("/api/channels", headers=auth_headers)
        for ch in resp.json()["channels"]:
            assert "name" in ch
            assert "type" in ch
            assert "connected" in ch
            assert isinstance(ch["connected"], bool)

    def test_channel_types_unique(
        self,
        token: str,
        auth_headers: dict[str, str],
    ) -> None:
        """No duplicate channel types."""
        from unittest.mock import AsyncMock

        app = create_app()
        mock_registry = MagicMock()
        mock_bridge = MagicMock()
        mock_bridge._adapters = {}
        mock_registry.resolve = AsyncMock(return_value=mock_bridge)
        app.state.registry = mock_registry
        client = TestClient(app)

        resp = client.get("/api/channels", headers=auth_headers)
        types = [c["type"] for c in resp.json()["channels"]]
        assert len(types) == len(set(types))


class TestTelegramSetupEndpoint:
    """Tests for POST /api/channels/telegram/setup."""

    def test_requires_auth(self, token: str) -> None:
        """Unauthenticated request returns 401."""
        app = create_app()
        client = TestClient(app)
        resp = client.post("/api/channels/telegram/setup", json={"token": "x"})
        assert resp.status_code == 401

    def test_empty_token_returns_422(
        self,
        token: str,
        auth_headers: dict[str, str],
    ) -> None:
        """Empty or missing token returns 422."""
        app = create_app()
        client = TestClient(app)

        resp = client.post(
            "/api/channels/telegram/setup",
            json={"token": ""},
            headers=auth_headers,
        )
        assert resp.status_code == 422
        assert resp.json()["ok"] is False

    def test_missing_token_field_returns_422(
        self,
        token: str,
        auth_headers: dict[str, str],
    ) -> None:
        """Missing token field returns 422."""
        app = create_app()
        client = TestClient(app)

        resp = client.post(
            "/api/channels/telegram/setup",
            json={},
            headers=auth_headers,
        )
        assert resp.status_code == 422

    def test_invalid_json_returns_422(
        self,
        token: str,
        auth_headers: dict[str, str],
    ) -> None:
        """Non-JSON body returns 422."""
        app = create_app()
        client = TestClient(app)

        resp = client.post(
            "/api/channels/telegram/setup",
            content=b"not json",
            headers={**auth_headers, "Content-Type": "application/json"},
        )
        assert resp.status_code == 422

    def test_invalid_token_returns_400(
        self,
        token: str,
        auth_headers: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Invalid Telegram token returns 400 with error message."""
        import aiohttp

        async def _mock_getme(*_args: object, **_kwargs: object) -> None:  # noqa: ARG001
            """Simulate Telegram API rejecting the token."""

        # Patch aiohttp to return invalid token response
        class FakeResp:
            status = 401

            async def json(self) -> dict[str, object]:
                return {"ok": False, "description": "Unauthorized"}

            async def __aenter__(self) -> FakeResp:
                return self

            async def __aexit__(self, *_: object) -> None:
                pass

        class FakeSession:
            def get(self, *_args: object, **_kwargs: object) -> FakeResp:
                return FakeResp()

            async def __aenter__(self) -> FakeSession:
                return self

            async def __aexit__(self, *_: object) -> None:
                pass

        monkeypatch.setattr(aiohttp, "ClientSession", lambda: FakeSession())

        app = create_app()
        client = TestClient(app)

        resp = client.post(
            "/api/channels/telegram/setup",
            json={"token": "invalid:token"},
            headers=auth_headers,
        )
        assert resp.status_code == 400
        data = resp.json()
        assert data["ok"] is False
        assert "Unauthorized" in data["error"]

    def test_valid_token_saves_and_returns_bot_info(
        self,
        token: str,
        auth_headers: dict[str, str],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Valid token: validates, saves to channel.env, returns bot info."""
        import aiohttp

        class FakeResp:
            status = 200

            async def json(self) -> dict[str, object]:
                return {
                    "ok": True,
                    "result": {
                        "id": 123,
                        "is_bot": True,
                        "first_name": "TestBot",
                        "username": "test_sovyx_bot",
                    },
                }

            async def __aenter__(self) -> FakeResp:
                return self

            async def __aexit__(self, *_: object) -> None:
                pass

        class FakeSession:
            def get(self, *_args: object, **_kwargs: object) -> FakeResp:
                return FakeResp()

            async def __aenter__(self) -> FakeSession:
                return self

            async def __aexit__(self, *_: object) -> None:
                pass

        monkeypatch.setattr(aiohttp, "ClientSession", lambda: FakeSession())

        # Mock engine_config with tmp_path as data_dir
        mock_config = MagicMock()
        mock_config.data_dir = tmp_path

        app = create_app()
        app.state.engine_config = mock_config
        client = TestClient(app)

        with patch.object(Path, "chmod") as mock_chmod:
            resp = client.post(
                "/api/channels/telegram/setup",
                json={"token": "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"},
                headers=auth_headers,
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["bot_username"] == "test_sovyx_bot"
        assert data["bot_name"] == "TestBot"
        assert data["requires_restart"] is True

        # Verify token saved to channel.env
        env_path = tmp_path / "channel.env"
        assert env_path.exists()
        content = env_path.read_text()
        assert "SOVYX_TELEGRAM_TOKEN=123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11" in content
        # Verify code attempted owner-only perms (cross-platform via spy on Path.chmod)
        mock_chmod.assert_any_call(0o600)
