"""Tests for Dashboard plugin status endpoints (TASK-448 + TASK-451)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from sovyx.dashboard.plugins import (
    get_plugin_detail,
    get_plugins_status,
    get_tools_list,
)


def _make_manifest(
    *,
    name: str = "test-plugin",
    permissions: list[str] | None = None,
    category: str = "",
    tags: list[str] | None = None,
    icon_url: str = "",
    pricing: str = "free",
) -> MagicMock:
    """Create a mock PluginManifest."""
    m = MagicMock()
    m.name = name
    m.version = "1.0.0"
    m.description = f"{name} plugin"
    m.author = "test-author"
    m.license = "MIT"
    m.homepage = "https://example.com"
    m.min_sovyx_version = "0.7.0"
    m.permissions = permissions or []
    m.network.allowed_domains = []
    m.depends = []
    m.optional_depends = []
    m.events.emits = []
    m.events.subscribes = []
    m.tools = []
    m.config_schema = {}
    m.category = category
    m.tags = tags or []
    m.icon_url = icon_url
    m.screenshots = []
    m.pricing = pricing
    m.price_usd = None
    m.trial_days = 0
    return m


def _mock_plugin_manager(
    plugins: dict[str, dict] | None = None,
    disabled: set[str] | None = None,
    health_overrides: dict[str, dict] | None = None,
) -> MagicMock:
    """Create a mock PluginManager with loaded plugins."""
    mgr = MagicMock()
    plugins = plugins or {}
    disabled = disabled or set()
    health_overrides = health_overrides or {}

    mgr.loaded_plugins = list(plugins.keys())
    mgr.plugin_count = len(plugins)

    def get_plugin(name: str) -> MagicMock | None:
        if name not in plugins:
            return None
        p = plugins[name]
        loaded = MagicMock()
        loaded.plugin.version = p.get("version", "1.0.0")
        loaded.plugin.description = p.get("description", f"{name} plugin")

        tools = []
        for t in p.get("tools", []):
            tool = MagicMock()
            tool.name = t["name"]
            tool.description = t.get("description", "")
            tool.parameters = t.get("parameters", {})
            tool.requires_confirmation = t.get("requires_confirmation", False)
            tool.timeout_seconds = t.get("timeout_seconds", 30.0)
            tools.append(tool)
        loaded.tools = tools
        loaded.manifest = p.get("manifest")
        return loaded

    mgr.get_plugin = get_plugin
    mgr.is_plugin_disabled = lambda name: name in disabled

    def get_health(name: str) -> dict:
        if name in health_overrides:
            return health_overrides[name]
        return {
            "consecutive_failures": 0,
            "disabled": name in disabled,
            "last_error": "",
            "active_tasks": 0,
        }

    mgr.get_plugin_health = get_health

    return mgr


# ── get_plugins_status ──


class TestGetPluginsStatus:
    """Tests for get_plugins_status."""

    def test_no_manager(self) -> None:
        result = get_plugins_status(None)
        assert result["available"] is False
        assert result["total"] == 0
        assert result["total_tools"] == 0
        assert result["error"] == 0

    def test_empty_manager(self) -> None:
        mgr = _mock_plugin_manager()
        result = get_plugins_status(mgr)
        assert result["available"] is True
        assert result["total"] == 0
        assert result["total_tools"] == 0

    def test_with_plugins(self) -> None:
        mgr = _mock_plugin_manager(
            plugins={
                "weather": {
                    "version": "1.0.0",
                    "tools": [
                        {"name": "get_weather", "description": "Get weather"},
                    ],
                },
                "calculator": {
                    "version": "1.0.0",
                    "tools": [
                        {"name": "calculate", "description": "Calculate"},
                    ],
                },
            },
        )
        result = get_plugins_status(mgr)
        assert result["total"] == 2
        assert result["active"] == 2
        assert result["disabled"] == 0
        assert result["total_tools"] == 2
        assert len(result["plugins"]) == 2

    def test_with_disabled(self) -> None:
        mgr = _mock_plugin_manager(
            plugins={
                "weather": {"tools": []},
                "bad": {"tools": []},
            },
            disabled={"bad"},
        )
        result = get_plugins_status(mgr)
        assert result["active"] == 1
        assert result["disabled"] == 1
        bad = [p for p in result["plugins"] if p["name"] == "bad"][0]
        assert bad["status"] == "disabled"

    def test_error_status(self) -> None:
        """Plugin with consecutive failures (not disabled) shows error."""
        mgr = _mock_plugin_manager(
            plugins={
                "failing": {"tools": [{"name": "t1", "description": ""}]},
            },
            health_overrides={
                "failing": {
                    "consecutive_failures": 3,
                    "disabled": False,
                    "last_error": "timeout",
                    "active_tasks": 0,
                },
            },
        )
        result = get_plugins_status(mgr)
        assert result["error"] == 1
        assert result["active"] == 0
        p = result["plugins"][0]
        assert p["status"] == "error"

    def test_plugin_info_fields(self) -> None:
        manifest = _make_manifest(
            name="weather",
            permissions=["brain:read"],
            category="productivity",
            tags=["weather", "api"],
            icon_url="https://example.com/icon.png",
            pricing="free",
        )
        mgr = _mock_plugin_manager(
            plugins={
                "weather": {
                    "version": "2.0.0",
                    "description": "Weather plugin",
                    "tools": [
                        {"name": "get_weather", "description": "Get weather"},
                    ],
                    "manifest": manifest,
                },
            },
        )
        result = get_plugins_status(mgr)
        plugin = result["plugins"][0]
        assert plugin["name"] == "weather"
        assert plugin["version"] == "2.0.0"
        assert plugin["description"] == "Weather plugin"
        assert plugin["tools_count"] == 1
        assert plugin["status"] == "active"
        assert plugin["health"]["consecutive_failures"] == 0
        # Manifest fields
        assert plugin["category"] == "productivity"
        assert plugin["tags"] == ["weather", "api"]
        assert plugin["icon_url"] == "https://example.com/icon.png"
        assert plugin["pricing"] == "free"
        # Permissions with risk info
        assert len(plugin["permissions"]) == 1
        assert plugin["permissions"][0]["permission"] == "brain:read"
        assert plugin["permissions"][0]["risk"] == "low"

    def test_plugin_no_manifest(self) -> None:
        """Plugins without manifest still get default fields."""
        mgr = _mock_plugin_manager(
            plugins={"basic": {"tools": []}},
        )
        result = get_plugins_status(mgr)
        p = result["plugins"][0]
        assert p["category"] == ""
        assert p["tags"] == []
        assert p["permissions"] == []
        assert p["pricing"] == "free"

    def test_get_plugin_none(self) -> None:
        """get_plugin returning None is handled."""
        mgr = MagicMock()
        mgr.loaded_plugins = ["ghost"]
        mgr.get_plugin.return_value = None
        mgr.get_plugin_health.return_value = {
            "consecutive_failures": 0,
            "disabled": False,
            "last_error": "",
            "active_tasks": 0,
        }
        mgr.is_plugin_disabled.return_value = False
        result = get_plugins_status(mgr)
        assert result["total"] == 0


# ── get_plugin_detail ──


class TestGetPluginDetail:
    """Tests for get_plugin_detail."""

    def test_no_manager(self) -> None:
        assert get_plugin_detail(None, "weather") is None

    def test_not_found(self) -> None:
        mgr = _mock_plugin_manager()
        assert get_plugin_detail(mgr, "ghost") is None

    def test_found(self) -> None:
        mgr = _mock_plugin_manager(
            plugins={
                "weather": {
                    "version": "1.0.0",
                    "tools": [
                        {
                            "name": "get_weather",
                            "description": "Get weather",
                            "parameters": {"type": "object"},
                        },
                    ],
                },
            },
        )
        result = get_plugin_detail(mgr, "weather")
        assert result is not None
        assert result["name"] == "weather"
        assert len(result["tools"]) == 1
        assert result["tools"][0]["parameters"] == {"type": "object"}
        assert result["tools"][0]["requires_confirmation"] is False
        assert result["tools"][0]["timeout_seconds"] == 30.0

    def test_disabled(self) -> None:
        mgr = _mock_plugin_manager(
            plugins={"bad": {"tools": []}},
            disabled={"bad"},
        )
        result = get_plugin_detail(mgr, "bad")
        assert result is not None
        assert result["status"] == "disabled"

    def test_error_status(self) -> None:
        mgr = _mock_plugin_manager(
            plugins={"failing": {"tools": []}},
            health_overrides={
                "failing": {
                    "consecutive_failures": 2,
                    "disabled": False,
                    "last_error": "crash",
                    "active_tasks": 1,
                },
            },
        )
        result = get_plugin_detail(mgr, "failing")
        assert result is not None
        assert result["status"] == "error"
        assert result["health"]["last_error"] == "crash"
        assert result["health"]["active_tasks"] == 1

    def test_with_manifest(self) -> None:
        manifest = _make_manifest(
            name="weather",
            permissions=["brain:read", "network:internet"],
            category="weather",
        )
        mgr = _mock_plugin_manager(
            plugins={
                "weather": {
                    "version": "1.0.0",
                    "tools": [],
                    "manifest": manifest,
                },
            },
        )
        result = get_plugin_detail(mgr, "weather")
        assert result is not None
        # Full manifest serialized
        assert result["manifest"]["name"] == "weather"
        assert result["manifest"]["author"] == "test-author"
        assert result["manifest"]["license"] == "MIT"
        assert result["manifest"]["homepage"] == "https://example.com"
        assert result["manifest"]["category"] == "weather"
        assert result["manifest"]["pricing"] == "free"
        # Permissions with risk info
        assert len(result["permissions"]) == 2
        perms_by_name = {p["permission"]: p for p in result["permissions"]}
        assert perms_by_name["brain:read"]["risk"] == "low"
        assert perms_by_name["network:internet"]["risk"] == "high"

    def test_no_manifest(self) -> None:
        mgr = _mock_plugin_manager(
            plugins={"basic": {"tools": []}},
        )
        result = get_plugin_detail(mgr, "basic")
        assert result is not None
        assert result["manifest"] == {}
        assert result["permissions"] == []


# ── get_tools_list ──


class TestGetToolsList:
    """Tests for get_tools_list."""

    def test_no_manager(self) -> None:
        assert get_tools_list(None) == []

    def test_with_tools(self) -> None:
        mgr = _mock_plugin_manager(
            plugins={
                "weather": {
                    "tools": [{"name": "get_weather", "description": "Get weather"}],
                },
                "calculator": {
                    "tools": [{"name": "calculate", "description": "Calculate"}],
                },
            },
        )
        tools = get_tools_list(mgr)
        assert len(tools) == 2

    def test_disabled_excluded(self) -> None:
        mgr = _mock_plugin_manager(
            plugins={
                "weather": {
                    "tools": [{"name": "get_weather", "description": "Get weather"}],
                },
                "bad": {
                    "tools": [{"name": "bad_tool", "description": "Bad"}],
                },
            },
            disabled={"bad"},
        )
        tools = get_tools_list(mgr)
        assert len(tools) == 1
        assert tools[0]["plugin"] == "weather"

    def test_empty(self) -> None:
        mgr = _mock_plugin_manager()
        assert get_tools_list(mgr) == []

    def test_get_plugin_none_skipped(self) -> None:
        """Plugins where get_plugin returns None are skipped."""
        mgr = MagicMock()
        mgr.loaded_plugins = ["ghost"]
        mgr.is_plugin_disabled.return_value = False
        mgr.get_plugin.return_value = None
        assert get_tools_list(mgr) == []


# ── _serialize_manifest ──


class TestSerializeManifest:
    """Tests for _serialize_manifest."""

    def test_none(self) -> None:
        from sovyx.dashboard.plugins import _serialize_manifest

        assert _serialize_manifest(None) == {}

    def test_full_manifest(self) -> None:
        from sovyx.dashboard.plugins import _serialize_manifest

        manifest = _make_manifest(
            name="test",
            permissions=["brain:read"],
            category="finance",
            tags=["money", "api"],
            icon_url="https://icon.png",
            pricing="paid",
        )
        result = _serialize_manifest(manifest)
        assert result["name"] == "test"
        assert result["category"] == "finance"
        assert result["tags"] == ["money", "api"]
        assert result["icon_url"] == "https://icon.png"
        assert result["pricing"] == "paid"
        assert result["permissions"] == ["brain:read"]
        assert result["author"] == "test-author"


# ── _permission_info ──


class TestPermissionInfo:
    """Tests for _permission_info."""

    def test_known_permission(self) -> None:
        from sovyx.dashboard.plugins import _permission_info

        info = _permission_info("brain:read")
        assert info["permission"] == "brain:read"
        assert info["risk"] == "low"
        assert "memory" in info["description"].lower()

    def test_high_risk_permission(self) -> None:
        from sovyx.dashboard.plugins import _permission_info

        info = _permission_info("network:internet")
        assert info["risk"] == "high"

    def test_unknown_permission(self) -> None:
        from sovyx.dashboard.plugins import _permission_info

        info = _permission_info("unknown:fake")
        assert info["risk"] == "medium"
        assert info["permission"] == "unknown:fake"


# ── PluginManager.disable_plugin ──


class TestPluginManagerDisable:
    """Tests for PluginManager.disable_plugin method."""

    def test_disable_plugin(self) -> None:
        from sovyx.plugins.manager import PluginManager

        mgr = PluginManager()
        # Create a minimal loaded plugin for the test
        mock_loaded = MagicMock()
        mock_loaded.plugin.name = "test"
        mgr._plugins["test"] = mock_loaded
        # _PluginHealth is private, init via _record_success
        mgr._record_success("test")

        assert not mgr.is_plugin_disabled("test")
        mgr.disable_plugin("test")
        assert mgr.is_plugin_disabled("test")

    def test_disable_then_reenable(self) -> None:
        from sovyx.plugins.manager import PluginManager

        mgr = PluginManager()
        mock_loaded = MagicMock()
        mock_loaded.plugin.name = "test"
        mgr._plugins["test"] = mock_loaded
        mgr._record_success("test")

        mgr.disable_plugin("test")
        assert mgr.is_plugin_disabled("test")

        mgr.re_enable_plugin("test")
        assert not mgr.is_plugin_disabled("test")

    def test_disable_not_found(self) -> None:
        from sovyx.plugins.manager import PluginError, PluginManager

        mgr = PluginManager()
        with pytest.raises(PluginError, match="not found"):
            mgr.disable_plugin("ghost")

    def test_disable_plugin_no_prior_health(self) -> None:
        """disable_plugin works even if no health record exists yet."""
        from sovyx.plugins.manager import PluginManager

        mgr = PluginManager()
        mock_loaded = MagicMock()
        mock_loaded.plugin.name = "new"
        mgr._plugins["new"] = mock_loaded
        # No _record_success — no _health entry

        mgr.disable_plugin("new")
        assert mgr.is_plugin_disabled("new")


# ── Server routes (integration) ──


class TestPluginRoutes:
    """Integration tests for plugin API routes in FastAPI."""

    @pytest.fixture()
    def client(self) -> object:
        """Create a test client with the dashboard app."""
        from starlette.testclient import TestClient

        from sovyx.dashboard.server import create_app

        app = create_app()
        # Get the token for auth
        token = app.state.auth_token
        client = TestClient(app)
        client.headers = {"Authorization": f"Bearer {token}"}  # type: ignore[assignment]
        return client

    def test_list_plugins_no_registry(self, client: object) -> None:
        """GET /api/plugins without registry returns unavailable."""
        from starlette.testclient import TestClient

        assert isinstance(client, TestClient)
        resp = client.get("/api/plugins")
        assert resp.status_code == 200
        data = resp.json()
        assert data["available"] is False

    def test_list_plugin_tools_no_registry(self, client: object) -> None:
        """GET /api/plugins/tools without registry returns empty."""
        from starlette.testclient import TestClient

        assert isinstance(client, TestClient)
        resp = client.get("/api/plugins/tools")
        assert resp.status_code == 200
        assert resp.json()["tools"] == []

    def test_get_plugin_detail_not_found(self, client: object) -> None:
        """GET /api/plugins/:name returns 404 when no registry."""
        from starlette.testclient import TestClient

        assert isinstance(client, TestClient)
        resp = client.get("/api/plugins/ghost")
        assert resp.status_code == 404

    def test_enable_no_registry(self, client: object) -> None:
        """POST enable returns 503 without registry."""
        from starlette.testclient import TestClient

        assert isinstance(client, TestClient)
        resp = client.post("/api/plugins/test/enable")
        assert resp.status_code == 503

    def test_disable_no_registry(self, client: object) -> None:
        """POST disable returns 503 without registry."""
        from starlette.testclient import TestClient

        assert isinstance(client, TestClient)
        resp = client.post("/api/plugins/test/disable")
        assert resp.status_code == 503

    def test_reload_no_registry(self, client: object) -> None:
        """POST reload returns 503 without registry."""
        from starlette.testclient import TestClient

        assert isinstance(client, TestClient)
        resp = client.post("/api/plugins/test/reload")
        assert resp.status_code == 503

    def test_auth_required(self) -> None:
        """Plugin routes require authentication."""
        from starlette.testclient import TestClient

        from sovyx.dashboard.server import create_app

        app = create_app()
        client = TestClient(app)
        # No auth header
        resp = client.get("/api/plugins")
        assert resp.status_code == 401
