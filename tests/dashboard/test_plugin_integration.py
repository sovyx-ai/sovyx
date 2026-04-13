"""Integration test: FastAPI + real PluginManager + all plugin endpoints.

TASK-469 — Wires a real PluginManager into the FastAPI app and hits
every endpoint through TestClient. This is the ultimate E2E validation.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from sovyx.dashboard.server import create_app
from sovyx.engine.registry import ServiceRegistry
from sovyx.plugins.context import PluginContext
from sovyx.plugins.manager import LoadedPlugin, PluginManager
from sovyx.plugins.manifest import PluginManifest
from sovyx.plugins.permissions import PermissionEnforcer
from sovyx.plugins.sdk import ISovyxPlugin, ToolDefinition

# ── Test Plugin ──


class WeatherPlugin(ISovyxPlugin):
    """Minimal test plugin for integration tests."""

    name = "weather"
    version = "1.0.0"
    description = "Weather data for cities"

    def get_tools(self) -> list[ToolDefinition]:
        return [
            ToolDefinition(
                name="get_weather",
                description="Get current weather for a city",
                parameters={
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                },
                handler=self._get_weather,
            ),
        ]

    async def _get_weather(self, city: str = "London") -> str:
        return f"Sunny in {city}"


# ── Fixtures ──


@pytest.fixture()
def app_with_plugins() -> tuple[TestClient, PluginManager]:
    """Create a test app with a real PluginManager + loaded plugin."""
    app = create_app()
    token = app.state.auth_token  # read from app instance, not module global

    # Create real PluginManager and load a plugin directly
    mgr = PluginManager()
    plugin = WeatherPlugin()
    manifest = PluginManifest(
        name="weather",
        version="1.0.0",
        description="Weather data for cities",
        author="Test Author",
        permissions=["brain:read", "network:internet"],
        category="weather",
        tags=["api"],
    )

    # Build minimal context and loaded plugin
    import logging
    from pathlib import Path

    ctx = PluginContext(
        plugin_name="weather",
        plugin_version="1.0.0",
        data_dir=Path("/tmp/sovyx-test-plugins/weather"),
        config={},
        logger=logging.getLogger("test.weather"),
    )
    enforcer = PermissionEnforcer("weather", {"brain:read", "network:internet"})
    loaded = LoadedPlugin(
        plugin=plugin,
        tools=plugin.get_tools(),
        context=ctx,
        enforcer=enforcer,
        manifest=manifest,
    )
    mgr._plugins["weather"] = loaded
    mgr._record_success("weather")

    # Wire into the app via registry
    registry = ServiceRegistry()
    registry.register_instance(PluginManager, mgr)
    app.state.registry = registry

    client = TestClient(app)
    client.headers = {"Authorization": f"Bearer {token}"}  # type: ignore[assignment]

    return client, mgr


# ── Tests ──


class TestPluginIntegrationE2E:
    """Full end-to-end integration test through HTTP endpoints."""

    def test_list_plugins(
        self,
        app_with_plugins: tuple[TestClient, PluginManager],
    ) -> None:
        """GET /api/plugins returns real plugin data."""
        client, _ = app_with_plugins
        resp = client.get("/api/plugins")
        assert resp.status_code == 200

        data = resp.json()
        assert data["available"] is True
        assert data["total"] == 1
        assert data["active"] == 1
        assert data["disabled"] == 0
        assert data["total_tools"] == 1

        plugin = data["plugins"][0]
        assert plugin["name"] == "weather"
        assert plugin["version"] == "1.0.0"
        assert plugin["status"] == "active"
        assert plugin["tools_count"] == 1
        assert plugin["category"] == "weather"
        assert len(plugin["permissions"]) == 2

    def test_plugin_detail(
        self,
        app_with_plugins: tuple[TestClient, PluginManager],
    ) -> None:
        """GET /api/plugins/:name returns full detail."""
        client, _ = app_with_plugins
        resp = client.get("/api/plugins/weather")
        assert resp.status_code == 200

        data = resp.json()
        assert data["name"] == "weather"
        assert data["status"] == "active"
        assert len(data["tools"]) == 1
        assert data["tools"][0]["name"] == "get_weather"
        assert "parameters" in data["tools"][0]
        assert data["manifest"]["author"] == "Test Author"
        assert data["manifest"]["category"] == "weather"

    def test_plugin_detail_not_found(
        self,
        app_with_plugins: tuple[TestClient, PluginManager],
    ) -> None:
        """GET /api/plugins/ghost → 404."""
        client, _ = app_with_plugins
        resp = client.get("/api/plugins/ghost")
        assert resp.status_code == 404

    def test_tools_list(
        self,
        app_with_plugins: tuple[TestClient, PluginManager],
    ) -> None:
        """GET /api/plugins/tools returns flat tool list."""
        client, _ = app_with_plugins
        resp = client.get("/api/plugins/tools")
        assert resp.status_code == 200

        tools = resp.json()["tools"]
        assert len(tools) == 1
        assert tools[0]["plugin"] == "weather"
        assert tools[0]["name"] == "get_weather"

    def test_disable_plugin(
        self,
        app_with_plugins: tuple[TestClient, PluginManager],
    ) -> None:
        """POST disable → 200, then GET shows disabled."""
        client, mgr = app_with_plugins

        # Disable
        resp = client.post("/api/plugins/weather/disable")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert resp.json()["status"] == "disabled"

        # Verify via GET
        resp = client.get("/api/plugins")
        plugin = resp.json()["plugins"][0]
        assert plugin["status"] == "disabled"
        assert resp.json()["disabled"] == 1
        assert resp.json()["active"] == 0

    def test_enable_plugin(
        self,
        app_with_plugins: tuple[TestClient, PluginManager],
    ) -> None:
        """POST disable then enable → back to active."""
        client, mgr = app_with_plugins

        # Disable first
        client.post("/api/plugins/weather/disable")

        # Enable
        resp = client.post("/api/plugins/weather/enable")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert resp.json()["status"] == "active"

        # Verify
        resp = client.get("/api/plugins")
        assert resp.json()["plugins"][0]["status"] == "active"
        assert resp.json()["active"] == 1

    def test_disable_not_found(
        self,
        app_with_plugins: tuple[TestClient, PluginManager],
    ) -> None:
        """POST disable unknown plugin → 404."""
        client, _ = app_with_plugins
        resp = client.post("/api/plugins/ghost/disable")
        assert resp.status_code == 404

    def test_enable_not_found(
        self,
        app_with_plugins: tuple[TestClient, PluginManager],
    ) -> None:
        """POST enable unknown plugin → 404."""
        client, _ = app_with_plugins
        resp = client.post("/api/plugins/ghost/enable")
        assert resp.status_code == 404

    def test_tools_excluded_when_disabled(
        self,
        app_with_plugins: tuple[TestClient, PluginManager],
    ) -> None:
        """Disabled plugin tools don't appear in /api/plugins/tools."""
        client, _ = app_with_plugins

        # Disable
        client.post("/api/plugins/weather/disable")

        # Tools should be empty
        resp = client.get("/api/plugins/tools")
        assert resp.json()["tools"] == []

    def test_auth_required_all_endpoints(
        self,
        app_with_plugins: tuple[TestClient, PluginManager],
    ) -> None:
        """All plugin endpoints require auth."""
        client, _ = app_with_plugins
        # Remove auth header
        no_auth = TestClient(client.app)  # type: ignore[arg-type]

        endpoints = [
            ("GET", "/api/plugins"),
            ("GET", "/api/plugins/tools"),
            ("GET", "/api/plugins/weather"),
            ("POST", "/api/plugins/weather/enable"),
            ("POST", "/api/plugins/weather/disable"),
            ("POST", "/api/plugins/weather/reload"),
        ]

        for method, path in endpoints:
            resp = getattr(no_auth, method.lower())(path)
            assert resp.status_code == 401, f"{method} {path} should be 401 without auth"


class TestPluginIntegrationReload:
    """Reload endpoint tests (separate class — may need real async)."""

    def test_reload_plugin(
        self,
        app_with_plugins: tuple[TestClient, PluginManager],
    ) -> None:
        """POST reload → 200 (plugin reloads successfully)."""
        client, _ = app_with_plugins
        resp = client.post("/api/plugins/weather/reload")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_reload_not_found(
        self,
        app_with_plugins: tuple[TestClient, PluginManager],
    ) -> None:
        """POST reload unknown → 404."""
        client, _ = app_with_plugins
        resp = client.post("/api/plugins/ghost/reload")
        assert resp.status_code == 404
