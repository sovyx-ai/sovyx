"""Tests for Dashboard plugin status endpoints (TASK-448)."""

from __future__ import annotations

from unittest.mock import MagicMock

from sovyx.dashboard.plugins import get_plugin_detail, get_plugins_status, get_tools_list


def _mock_plugin_manager(
    plugins: dict[str, dict] | None = None,
    disabled: set[str] | None = None,
) -> MagicMock:
    """Create a mock PluginManager with loaded plugins."""
    mgr = MagicMock()
    plugins = plugins or {}
    disabled = disabled or set()

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
            tools.append(tool)
        loaded.tools = tools
        loaded.manifest = p.get("manifest")
        return loaded

    mgr.get_plugin = get_plugin
    mgr.is_plugin_disabled = lambda name: name in disabled
    mgr.get_plugin_health = lambda name: {
        "consecutive_failures": 0,
        "disabled": name in disabled,
        "last_error": "",
        "active_tasks": 0,
    }

    return mgr


class TestGetPluginsStatus:
    """Tests for get_plugins_status."""

    def test_no_manager(self) -> None:
        result = get_plugins_status(None)
        assert result["available"] is False
        assert result["total"] == 0

    def test_empty_manager(self) -> None:
        mgr = _mock_plugin_manager()
        result = get_plugins_status(mgr)
        assert result["available"] is True
        assert result["total"] == 0

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

    def test_plugin_info_fields(self) -> None:
        mgr = _mock_plugin_manager(
            plugins={
                "weather": {
                    "version": "2.0.0",
                    "description": "Weather plugin",
                    "tools": [
                        {"name": "get_weather", "description": "Get weather"},
                    ],
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

    def test_disabled(self) -> None:
        mgr = _mock_plugin_manager(
            plugins={"bad": {"tools": []}},
            disabled={"bad"},
        )
        result = get_plugin_detail(mgr, "bad")
        assert result is not None
        assert result["status"] == "disabled"


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
        assert tools[0]["plugin"] == "calculator" or tools[0]["plugin"] == "weather"

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
