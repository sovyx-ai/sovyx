"""Contract tests: backend plugin responses vs frontend TypeScript types.

TASK-466 — Validates every field the frontend expects exists in backend
responses with correct types. If backend changes a field, this test fails
and signals frontend types need updating.

Frontend types (source of truth):
  dashboard/src/types/api.ts → PluginInfo, PluginDetail, PluginManifestData,
  PluginPermission, PluginHealth, PluginsResponse, PluginToolsResponse
"""

from __future__ import annotations

from unittest.mock import MagicMock

from sovyx.dashboard.plugins import (
    get_plugin_detail,
    get_plugins_status,
    get_tools_list,
)
from sovyx.plugins.manifest import PluginManifest
from sovyx.plugins.sdk import ToolDefinition

# ── Fixtures ──


def _make_loaded_plugin(
    *,
    name: str = "weather",
    version: str = "1.0.0",
    description: str = "Weather plugin",
    with_manifest: bool = True,
    with_tools: bool = True,
    disabled: bool = False,
    failures: int = 0,
) -> tuple[MagicMock, MagicMock]:
    """Create a mock PluginManager + LoadedPlugin with realistic data."""
    mgr = MagicMock()
    mgr.loaded_plugins = [name]

    loaded = MagicMock()
    loaded.plugin.version = version
    loaded.plugin.description = description

    # Tools
    if with_tools:
        tool = ToolDefinition(
            name="get_weather",
            description="Get weather for a city",
            parameters={
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
            handler=lambda x: x,
            requires_confirmation=True,
            timeout_seconds=15.0,
        )
        loaded.tools = [tool]
    else:
        loaded.tools = []

    # Manifest
    if with_manifest:
        loaded.manifest = PluginManifest(
            name=name,
            version=version,
            description=description,
            author="Test Author",
            license="MIT",
            homepage="https://example.com/weather",
            permissions=["brain:read", "network:internet"],
            category="weather",
            tags=["api", "weather", "data"],
            icon_url="https://example.com/icon.png",
            pricing="freemium",
            price_usd=4.99,
            trial_days=14,
        )
    else:
        loaded.manifest = None

    mgr.get_plugin.return_value = loaded
    mgr.is_plugin_disabled.return_value = disabled
    mgr.get_plugin_health.return_value = {
        "consecutive_failures": failures,
        "disabled": disabled,
        "last_error": "timeout after 30s" if failures > 0 else "",
        "active_tasks": 1 if not disabled else 0,
    }

    return mgr, loaded


# ── PluginsResponse contract ──


class TestPluginsResponseContract:
    """GET /api/plugins response must match frontend PluginsResponse type."""

    def test_response_top_level_fields(self) -> None:
        """PluginsResponse: available, plugins, total, active, disabled, error, total_tools."""
        mgr, _ = _make_loaded_plugin()
        result = get_plugins_status(mgr)

        assert isinstance(result["available"], bool)
        assert isinstance(result["plugins"], list)
        assert isinstance(result["total"], int)
        assert isinstance(result["active"], int)
        assert isinstance(result["disabled"], int)
        assert isinstance(result["error"], int)
        assert isinstance(result["total_tools"], int)

    def test_unavailable_response(self) -> None:
        """When engine is off → available=False, all counts=0."""
        result = get_plugins_status(None)
        assert result["available"] is False
        assert result["total"] == 0
        assert result["error"] == 0
        assert result["total_tools"] == 0


# ── PluginInfo contract ──


class TestPluginInfoContract:
    """Each plugin in plugins[] must match frontend PluginInfo type."""

    def test_all_12_fields_present(self) -> None:
        """PluginInfo has exactly the 12 fields frontend expects."""
        mgr, _ = _make_loaded_plugin()
        result = get_plugins_status(mgr)
        p = result["plugins"][0]

        expected_fields = {
            "name",
            "version",
            "description",
            "status",
            "tools_count",
            "tools",
            "permissions",
            "health",
            "category",
            "tags",
            "icon_url",
            "pricing",
        }
        assert set(p.keys()) == expected_fields

    def test_field_types(self) -> None:
        """All PluginInfo fields have correct Python types."""
        mgr, _ = _make_loaded_plugin()
        p = get_plugins_status(mgr)["plugins"][0]

        assert isinstance(p["name"], str)
        assert isinstance(p["version"], str)
        assert isinstance(p["description"], str)
        assert isinstance(p["status"], str)
        assert p["status"] in ("active", "disabled", "error")
        assert isinstance(p["tools_count"], int)
        assert isinstance(p["tools"], list)
        assert isinstance(p["permissions"], list)
        assert isinstance(p["health"], dict)
        assert isinstance(p["category"], str)
        assert isinstance(p["tags"], list)
        assert isinstance(p["icon_url"], str)
        assert isinstance(p["pricing"], str)

    def test_tool_summary_fields(self) -> None:
        """Each tool in PluginInfo.tools has name + description."""
        mgr, _ = _make_loaded_plugin()
        tool = get_plugins_status(mgr)["plugins"][0]["tools"][0]

        assert "name" in tool
        assert "description" in tool
        assert isinstance(tool["name"], str)
        assert isinstance(tool["description"], str)


# ── PluginPermission contract ──


class TestPluginPermissionContract:
    """PluginPermission: permission, risk, description."""

    def test_permission_fields(self) -> None:
        mgr, _ = _make_loaded_plugin()
        perms = get_plugins_status(mgr)["plugins"][0]["permissions"]

        assert len(perms) == 2
        for perm in perms:
            assert "permission" in perm
            assert "risk" in perm
            assert "description" in perm
            assert isinstance(perm["permission"], str)
            assert perm["risk"] in ("low", "medium", "high")
            assert isinstance(perm["description"], str)
            assert len(perm["description"]) > 0

    def test_risk_values_correct(self) -> None:
        """brain:read=low, network:internet=high."""
        mgr, _ = _make_loaded_plugin()
        perms = get_plugins_status(mgr)["plugins"][0]["permissions"]
        by_name = {p["permission"]: p for p in perms}

        assert by_name["brain:read"]["risk"] == "low"
        assert by_name["network:internet"]["risk"] == "high"


# ── PluginHealth contract ──


class TestPluginHealthContract:
    """PluginHealth: consecutive_failures, disabled, last_error, active_tasks."""

    def test_health_fields_and_types(self) -> None:
        mgr, _ = _make_loaded_plugin(failures=3)
        health = get_plugins_status(mgr)["plugins"][0]["health"]

        assert isinstance(health["consecutive_failures"], int)
        assert isinstance(health["disabled"], bool)
        assert isinstance(health["last_error"], str)
        assert isinstance(health["active_tasks"], int)

    def test_healthy_plugin(self) -> None:
        mgr, _ = _make_loaded_plugin(failures=0)
        health = get_plugins_status(mgr)["plugins"][0]["health"]

        assert health["consecutive_failures"] == 0
        assert health["disabled"] is False
        assert health["last_error"] == ""

    def test_failing_plugin(self) -> None:
        mgr, _ = _make_loaded_plugin(failures=3)
        health = get_plugins_status(mgr)["plugins"][0]["health"]

        assert health["consecutive_failures"] == 3
        assert health["last_error"] == "timeout after 30s"


# ── PluginDetail contract ──


class TestPluginDetailContract:
    """GET /api/plugins/:name must match frontend PluginDetail type."""

    def test_all_fields_present(self) -> None:
        """PluginDetail: name, version, description, status, tools, permissions, health, manifest."""
        mgr, _ = _make_loaded_plugin()
        detail = get_plugin_detail(mgr, "weather")
        assert detail is not None

        expected = {
            "name",
            "version",
            "description",
            "status",
            "tools",
            "permissions",
            "health",
            "manifest",
        }
        assert set(detail.keys()) == expected

    def test_tool_detail_fields(self) -> None:
        """PluginToolDetail: name, description, parameters, requires_confirmation, timeout_seconds."""
        mgr, _ = _make_loaded_plugin()
        detail = get_plugin_detail(mgr, "weather")
        assert detail is not None
        tool = detail["tools"][0]

        assert isinstance(tool["name"], str)
        assert isinstance(tool["description"], str)
        assert isinstance(tool["parameters"], dict)
        assert isinstance(tool["requires_confirmation"], bool)
        assert isinstance(tool["timeout_seconds"], (int, float))

    def test_tool_values(self) -> None:
        mgr, _ = _make_loaded_plugin()
        tool = get_plugin_detail(mgr, "weather")["tools"][0]  # type: ignore[index]

        assert tool["name"] == "get_weather"
        assert tool["requires_confirmation"] is True
        assert tool["timeout_seconds"] == 15.0
        assert "city" in str(tool["parameters"])


# ── PluginManifestData contract ──


class TestManifestContract:
    """Serialized manifest must match frontend PluginManifestData type (21 fields)."""

    def test_all_21_fields_present(self) -> None:
        mgr, _ = _make_loaded_plugin(with_manifest=True)
        detail = get_plugin_detail(mgr, "weather")
        assert detail is not None
        m = detail["manifest"]

        expected_fields = {
            "name",
            "version",
            "description",
            "author",
            "license",
            "homepage",
            "min_sovyx_version",
            "permissions",
            "network",
            "depends",
            "optional_depends",
            "events",
            "tools",
            "config_schema",
            "category",
            "tags",
            "icon_url",
            "screenshots",
            "pricing",
            "price_usd",
            "trial_days",
        }
        assert set(m.keys()) == expected_fields

    def test_manifest_field_types(self) -> None:
        mgr, _ = _make_loaded_plugin(with_manifest=True)
        m = get_plugin_detail(mgr, "weather")["manifest"]  # type: ignore[index]

        assert isinstance(m["name"], str)
        assert isinstance(m["version"], str)
        assert isinstance(m["author"], str)
        assert isinstance(m["license"], str)
        assert isinstance(m["homepage"], str)
        assert isinstance(m["permissions"], list)
        assert isinstance(m["network"], dict)
        assert isinstance(m["network"]["allowed_domains"], list)
        assert isinstance(m["depends"], list)
        assert isinstance(m["optional_depends"], list)
        assert isinstance(m["events"], dict)
        assert isinstance(m["events"]["emits"], list)
        assert isinstance(m["events"]["subscribes"], list)
        assert isinstance(m["tools"], list)
        assert isinstance(m["config_schema"], dict)
        assert isinstance(m["category"], str)
        assert isinstance(m["tags"], list)
        assert isinstance(m["icon_url"], str)
        assert isinstance(m["screenshots"], list)
        assert isinstance(m["pricing"], str)
        # price_usd can be None or float
        assert m["price_usd"] is None or isinstance(m["price_usd"], (int, float))
        assert isinstance(m["trial_days"], int)

    def test_manifest_values(self) -> None:
        mgr, _ = _make_loaded_plugin(with_manifest=True)
        m = get_plugin_detail(mgr, "weather")["manifest"]  # type: ignore[index]

        assert m["author"] == "Test Author"
        assert m["license"] == "MIT"
        assert m["homepage"] == "https://example.com/weather"
        assert m["category"] == "weather"
        assert m["tags"] == ["api", "weather", "data"]
        assert m["pricing"] == "freemium"
        assert m["price_usd"] == 4.99
        assert m["trial_days"] == 14

    def test_no_manifest_returns_empty_dict(self) -> None:
        """Plugin without manifest → manifest={} (not None)."""
        mgr, _ = _make_loaded_plugin(with_manifest=False)
        detail = get_plugin_detail(mgr, "weather")
        assert detail is not None
        assert detail["manifest"] == {}


# ── Edge cases ──


class TestEdgeCases:
    """Edge cases that could break frontend rendering."""

    def test_no_tools(self) -> None:
        mgr, _ = _make_loaded_plugin(with_tools=False)
        p = get_plugins_status(mgr)["plugins"][0]
        assert p["tools_count"] == 0
        assert p["tools"] == []

    def test_disabled_status(self) -> None:
        mgr, _ = _make_loaded_plugin(disabled=True)
        p = get_plugins_status(mgr)["plugins"][0]
        assert p["status"] == "disabled"

    def test_error_status(self) -> None:
        """Plugin with failures (not disabled) → status='error'."""
        mgr, _ = _make_loaded_plugin(failures=3)
        p = get_plugins_status(mgr)["plugins"][0]
        assert p["status"] == "error"

    def test_active_status(self) -> None:
        mgr, _ = _make_loaded_plugin(failures=0, disabled=False)
        p = get_plugins_status(mgr)["plugins"][0]
        assert p["status"] == "active"

    def test_no_manifest_defaults(self) -> None:
        """Plugin without manifest → empty category, tags, permissions."""
        mgr, _ = _make_loaded_plugin(with_manifest=False)
        p = get_plugins_status(mgr)["plugins"][0]
        assert p["category"] == ""
        assert p["tags"] == []
        assert p["permissions"] == []
        assert p["pricing"] == "free"
        assert p["icon_url"] == ""

    def test_plugin_not_found(self) -> None:
        mgr, _ = _make_loaded_plugin(name="weather")
        # Override get_plugin to return None for unknown names
        real_loaded = mgr.get_plugin.return_value
        mgr.get_plugin.side_effect = (
            lambda n: real_loaded if n == "weather" else None
        )
        assert get_plugin_detail(mgr, "nonexistent") is None

    def test_tools_list_excludes_disabled(self) -> None:
        mgr, _ = _make_loaded_plugin(disabled=True, with_tools=True)
        tools = get_tools_list(mgr)
        assert tools == []

    def test_tools_list_fields(self) -> None:
        """PluginToolsResponse: tools[] has plugin, name, description."""
        mgr, _ = _make_loaded_plugin()
        tools = get_tools_list(mgr)
        assert len(tools) == 1
        t = tools[0]
        assert "plugin" in t
        assert "name" in t
        assert "description" in t
        assert t["plugin"] == "weather"
