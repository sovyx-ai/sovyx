"""Tests for GET /api/channels endpoint (DASH-09)."""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from starlette.testclient import TestClient

from sovyx.dashboard.server import create_app

if TYPE_CHECKING:
    from pathlib import Path


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
        app = create_app()
        mock_registry = MagicMock()
        mock_registry.is_registered.return_value = False
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
        """Telegram shows connected when TelegramChannel is registered."""
        from sovyx.bridge.channels.telegram import TelegramChannel

        app = create_app()
        mock_registry = MagicMock()

        def _is_registered(cls: type) -> bool:
            return cls is TelegramChannel

        mock_registry.is_registered.side_effect = _is_registered
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
        app = create_app()
        mock_registry = MagicMock()
        mock_registry.is_registered.return_value = False
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
        app = create_app()
        mock_registry = MagicMock()
        mock_registry.is_registered.return_value = False
        app.state.registry = mock_registry
        client = TestClient(app)

        resp = client.get("/api/channels", headers=auth_headers)
        types = [c["type"] for c in resp.json()["channels"]]
        assert len(types) == len(set(types))
