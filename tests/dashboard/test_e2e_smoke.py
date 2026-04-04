"""PREP-10: End-to-end smoke test — frontend build served via FastAPI.

Validates the full pipeline:
1. Vite build outputs exist in src/sovyx/dashboard/static/
2. FastAPI serves index.html at /
3. Static assets served at /assets/*
4. SPA fallback works (unknown path → index.html)
5. API routes accessible with auth
6. WebSocket connects and responds to ping
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from sovyx.dashboard import STATIC_DIR
from sovyx.dashboard.server import create_app

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _setup_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("smoke-test-token")
    monkeypatch.setattr("sovyx.dashboard.server.TOKEN_FILE", token_file)


@pytest.fixture()
def client() -> TestClient:
    return TestClient(create_app())


AUTH = {"Authorization": "Bearer smoke-test-token"}


class TestBuildOutput:
    """Verify Vite build produced the expected files."""

    def test_static_dir_exists(self) -> None:
        assert STATIC_DIR.exists(), f"STATIC_DIR not found: {STATIC_DIR}"

    def test_index_html_exists(self) -> None:
        index = STATIC_DIR / "index.html"
        assert index.exists(), "index.html not found in static dir"

    def test_assets_dir_exists(self) -> None:
        assets = STATIC_DIR / "assets"
        assert assets.exists(), "assets/ dir not found"

    def test_has_js_bundle(self) -> None:
        assets = STATIC_DIR / "assets"
        js_files = list(assets.glob("*.js"))
        assert len(js_files) >= 1, "No JS bundles found"

    def test_has_css_bundle(self) -> None:
        assets = STATIC_DIR / "assets"
        css_files = list(assets.glob("*.css"))
        assert len(css_files) >= 1, "No CSS bundles found"

    def test_index_html_references_assets(self) -> None:
        html = (STATIC_DIR / "index.html").read_text()
        assert "/assets/" in html, "index.html doesn't reference /assets/"
        assert 'type="module"' in html, "index.html missing module script"


class TestStaticServing:
    """Verify FastAPI serves the built frontend."""

    def test_root_serves_index_html(self, client: TestClient) -> None:
        resp = client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")
        assert "Sovyx Dashboard" in resp.text

    def test_assets_css_served(self, client: TestClient) -> None:
        assets = STATIC_DIR / "assets"
        css_files = list(assets.glob("*.css"))
        assert css_files, "No CSS files to test"
        css_name = css_files[0].name
        resp = client.get(f"/assets/{css_name}")
        assert resp.status_code == 200

    def test_assets_js_served(self, client: TestClient) -> None:
        assets = STATIC_DIR / "assets"
        js_files = [f for f in assets.glob("*.js") if "index-" in f.name]
        assert js_files, "No JS files to test"
        js_name = js_files[0].name
        resp = client.get(f"/assets/{js_name}")
        assert resp.status_code == 200

    def test_spa_fallback_returns_index(self, client: TestClient) -> None:
        """Unknown paths should return index.html (React Router handles routing)."""
        resp = client.get("/conversations")
        assert resp.status_code == 200
        assert "Sovyx Dashboard" in resp.text

    def test_spa_fallback_deep_path(self, client: TestClient) -> None:
        resp = client.get("/brain/some/deep/path")
        assert resp.status_code == 200
        assert "Sovyx Dashboard" in resp.text


class TestAPISmoke:
    """Quick smoke test of all API endpoints."""

    def test_status(self, client: TestClient) -> None:
        resp = client.get("/api/status", headers=AUTH)
        assert resp.status_code == 200
        data = resp.json()
        assert data["version"] == "0.1.0"

    def test_health(self, client: TestClient) -> None:
        resp = client.get("/api/health", headers=AUTH)
        assert resp.status_code == 200
        data = resp.json()
        assert "overall" in data
        assert len(data["checks"]) >= 3

    def test_conversations(self, client: TestClient) -> None:
        resp = client.get("/api/conversations", headers=AUTH)
        assert resp.status_code == 200

    def test_brain_graph(self, client: TestClient) -> None:
        resp = client.get("/api/brain/graph", headers=AUTH)
        assert resp.status_code == 200

    def test_logs(self, client: TestClient) -> None:
        resp = client.get("/api/logs", headers=AUTH)
        assert resp.status_code == 200

    def test_settings_get(self, client: TestClient) -> None:
        resp = client.get("/api/settings", headers=AUTH)
        assert resp.status_code == 200

    def test_settings_put(self, client: TestClient) -> None:
        resp = client.put("/api/settings", headers=AUTH)
        assert resp.status_code == 200

    def test_docs(self, client: TestClient) -> None:
        resp = client.get("/api/docs")
        assert resp.status_code == 200

    def test_auth_required(self, client: TestClient) -> None:
        resp = client.get("/api/status")
        assert resp.status_code == 401


class TestWebSocketSmoke:
    """WebSocket connectivity smoke test."""

    def test_ws_connect_ping_pong(self, client: TestClient) -> None:
        with client.websocket_connect("/ws?token=smoke-test-token") as ws:
            ws.send_text("ping")
            assert ws.receive_text() == "pong"

    def test_ws_rejects_bad_token(self, client: TestClient) -> None:
        from starlette.websockets import WebSocketDisconnect

        with pytest.raises(WebSocketDisconnect), client.websocket_connect("/ws?token=bad"):
            pass  # pragma: no cover


class TestFullPipeline:
    """Integration: verify the entire flow works together."""

    def test_html_loads_then_api_works(self, client: TestClient) -> None:
        """Simulate: browser loads HTML → JS fetches /api/status."""
        # 1. Load the page
        html_resp = client.get("/")
        assert html_resp.status_code == 200
        assert "Sovyx Dashboard" in html_resp.text

        # 2. JS would fetch status
        api_resp = client.get("/api/status", headers=AUTH)
        assert api_resp.status_code == 200
        assert "mind_name" in api_resp.json()

        # 3. JS would open WebSocket
        with client.websocket_connect("/ws?token=smoke-test-token") as ws:
            ws.send_text("ping")
            assert ws.receive_text() == "pong"

    def test_navigate_spa_routes(self, client: TestClient) -> None:
        """All SPA routes should return index.html."""
        for path in ["/", "/conversations", "/brain", "/logs", "/settings"]:
            resp = client.get(path)
            assert resp.status_code == 200, f"Failed for {path}"
            assert "Sovyx Dashboard" in resp.text, f"No title for {path}"
