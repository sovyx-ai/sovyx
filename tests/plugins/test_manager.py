"""Tests for Sovyx Plugin Manager — discover, load, execute, lifecycle.

Coverage target: ≥95% on plugins/manager.py
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from sovyx.plugins import manager as _manager_mod  # anti-pattern #11
from sovyx.plugins.manager import (
    PluginDisabledError,
    PluginManager,
    _PluginHealth,
    _topological_sort,
)
from sovyx.plugins.permissions import Permission
from sovyx.plugins.sdk import ISovyxPlugin, tool

# ── Test Plugins ────────────────────────────────────────────────────


class FakeWeatherPlugin(ISovyxPlugin):
    @property
    def name(self) -> str:
        return "weather"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def description(self) -> str:
        return "Weather data."

    @property
    def permissions(self) -> list[Permission]:
        return [Permission.NETWORK_INTERNET]

    @tool(description="Get weather for a city")
    async def get_weather(self, city: str) -> str:
        return f"Sunny in {city}"


class FakeTimerPlugin(ISovyxPlugin):
    @property
    def name(self) -> str:
        return "timer"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def description(self) -> str:
        return "Set timers."

    @tool(description="Set a timer")
    async def set_timer(self, seconds: int) -> str:
        return f"Timer set for {seconds}s"


class SlowPlugin(ISovyxPlugin):
    @property
    def name(self) -> str:
        return "slow"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def description(self) -> str:
        return "Slow plugin."

    @tool(description="Slow operation")
    async def slow_op(self) -> str:
        await asyncio.sleep(0.5)  # cancelled by 0.01s manager timeout
        return "done"


class FailSetupPlugin(ISovyxPlugin):
    @property
    def name(self) -> str:
        return "fail-setup"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def description(self) -> str:
        return "Fails on setup."

    async def setup(self, ctx: object) -> None:
        msg = "Setup explosion"
        raise RuntimeError(msg)


class FailTeardownPlugin(ISovyxPlugin):
    @property
    def name(self) -> str:
        return "fail-teardown"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def description(self) -> str:
        return "Fails on teardown."

    async def teardown(self) -> None:
        msg = "Teardown explosion"
        raise RuntimeError(msg)


class ErrorToolPlugin(ISovyxPlugin):
    @property
    def name(self) -> str:
        return "error-tool"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def description(self) -> str:
        return "Tool that errors."

    @tool(description="Always fails")
    async def broken(self) -> str:
        msg = "tool broke"
        raise ValueError(msg)


# ── Topological Sort ────────────────────────────────────────────────


class TestTopologicalSort:
    """Tests for dependency resolution."""

    def test_no_deps(self) -> None:
        result = _topological_sort({"a": [], "b": [], "c": []})
        assert sorted(result) == ["a", "b", "c"]

    def test_simple_chain(self) -> None:
        result = _topological_sort({"c": ["b"], "b": ["a"], "a": []})
        assert result.index("a") < result.index("b") < result.index("c")

    def test_diamond(self) -> None:
        result = _topological_sort(
            {
                "d": ["b", "c"],
                "b": ["a"],
                "c": ["a"],
                "a": [],
            }
        )
        assert result.index("a") < result.index("b")
        assert result.index("a") < result.index("c")
        assert result.index("b") < result.index("d")

    def test_circular_dependency(self) -> None:
        with pytest.raises(Exception, match="Circular"):
            _topological_sort({"a": ["b"], "b": ["a"]})

    def test_unknown_dep_ignored(self) -> None:
        """Dependencies on unregistered plugins are ignored."""
        result = _topological_sort({"a": ["unknown"]})
        assert result == ["a"]

    def test_empty(self) -> None:
        result = _topological_sort({})
        assert result == []


# ── Plugin Manager: Loading ─────────────────────────────────────────


class TestPluginLoading:
    """Tests for plugin loading."""

    @pytest.mark.anyio()
    async def test_load_single(self, tmp_path: Path) -> None:
        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        await mgr.load_single(FakeWeatherPlugin())
        assert mgr.is_plugin_loaded("weather")
        assert mgr.plugin_count == 1

    @pytest.mark.anyio()
    async def test_load_creates_data_dir(self, tmp_path: Path) -> None:
        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        await mgr.load_single(FakeWeatherPlugin())
        assert (tmp_path / "weather").is_dir()

    @pytest.mark.anyio()
    async def test_load_duplicate_rejected(self, tmp_path: Path) -> None:
        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        await mgr.load_single(FakeWeatherPlugin())
        with pytest.raises(Exception, match="already loaded"):
            await mgr.load_single(FakeWeatherPlugin())

    @pytest.mark.anyio()
    async def test_load_all_registered(self, tmp_path: Path) -> None:
        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        mgr.register_class(FakeWeatherPlugin)
        mgr.register_class(FakeTimerPlugin)
        loaded = await mgr.load_all()
        assert "weather" in loaded
        assert "timer" in loaded

    @pytest.mark.anyio()
    async def test_disabled_plugin_skipped(self, tmp_path: Path) -> None:
        mgr = PluginManager(data_dir=tmp_path, disabled={"weather"}, discover_entry_points=False)
        mgr.register_class(FakeWeatherPlugin)
        mgr.register_class(FakeTimerPlugin)
        loaded = await mgr.load_all()
        assert "weather" not in loaded
        assert "timer" in loaded

    @pytest.mark.anyio()
    async def test_enabled_filter(self, tmp_path: Path) -> None:
        mgr = PluginManager(data_dir=tmp_path, enabled={"timer"}, discover_entry_points=False)
        mgr.register_class(FakeWeatherPlugin)
        mgr.register_class(FakeTimerPlugin)
        loaded = await mgr.load_all()
        assert "weather" not in loaded
        assert "timer" in loaded

    @pytest.mark.anyio()
    async def test_failed_setup_not_loaded(self, tmp_path: Path) -> None:
        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        mgr.register_class(FailSetupPlugin)
        loaded = await mgr.load_all()
        assert loaded == []
        assert mgr.plugin_count == 0


# ── Tool Execution ──────────────────────────────────────────────────


class TestToolExecution:
    """Tests for execute()."""

    @pytest.mark.anyio()
    async def test_execute_success(self, tmp_path: Path) -> None:
        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        await mgr.load_single(FakeWeatherPlugin())
        result = await mgr.execute("weather.get_weather", {"city": "Berlin"})
        assert result.success is True
        assert "Sunny in Berlin" in result.output

    @pytest.mark.anyio()
    async def test_execute_plugin_not_found(self, tmp_path: Path) -> None:
        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        with pytest.raises(Exception, match="not found"):
            await mgr.execute("nonexistent.tool", {})

    @pytest.mark.anyio()
    async def test_execute_tool_not_found(self, tmp_path: Path) -> None:
        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        await mgr.load_single(FakeWeatherPlugin())
        with pytest.raises(Exception, match="not found"):
            await mgr.execute("weather.nonexistent", {})

    @pytest.mark.anyio()
    async def test_execute_invalid_format(self, tmp_path: Path) -> None:
        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        with pytest.raises(Exception, match="Invalid tool name"):
            await mgr.execute("no-dot-name", {})

    @pytest.mark.anyio()
    async def test_execute_timeout(self, tmp_path: Path) -> None:
        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        await mgr.load_single(SlowPlugin())
        result = await mgr.execute("slow.slow_op", {}, timeout=0.01)
        assert result.success is False
        assert "timed out" in result.output

    @pytest.mark.anyio()
    async def test_execute_error_caught(self, tmp_path: Path) -> None:
        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        await mgr.load_single(ErrorToolPlugin())
        result = await mgr.execute("error-tool.broken", {})
        assert result.success is False
        assert "tool broke" in result.output


# ── Tool Definitions ────────────────────────────────────────────────


class TestToolDefinitions:
    """Tests for get_tool_definitions."""

    @pytest.mark.anyio()
    async def test_get_tools(self, tmp_path: Path) -> None:
        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        await mgr.load_single(FakeWeatherPlugin())
        await mgr.load_single(FakeTimerPlugin())
        tools = mgr.get_tool_definitions()
        names = [t.name for t in tools]
        assert "weather.get_weather" in names
        assert "timer.set_timer" in names

    @pytest.mark.anyio()
    async def test_tool_namespace(self, tmp_path: Path) -> None:
        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        await mgr.load_single(FakeWeatherPlugin())
        tools = mgr.get_tool_definitions()
        assert all(t.name.startswith("weather.") for t in tools)


# ── Lifecycle ───────────────────────────────────────────────────────


class TestLifecycle:
    """Tests for unload, reload, shutdown."""

    @pytest.mark.anyio()
    async def test_unload(self, tmp_path: Path) -> None:
        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        await mgr.load_single(FakeWeatherPlugin())
        await mgr.unload("weather")
        assert not mgr.is_plugin_loaded("weather")

    @pytest.mark.anyio()
    async def test_unload_not_found(self, tmp_path: Path) -> None:
        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        with pytest.raises(Exception, match="not found"):
            await mgr.unload("ghost")

    @pytest.mark.anyio()
    async def test_unload_teardown_error(self, tmp_path: Path) -> None:
        """Teardown error doesn't prevent unloading."""
        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        await mgr.load_single(FailTeardownPlugin())
        await mgr.unload("fail-teardown")
        assert not mgr.is_plugin_loaded("fail-teardown")

    @pytest.mark.anyio()
    async def test_shutdown(self, tmp_path: Path) -> None:
        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        await mgr.load_single(FakeWeatherPlugin())
        await mgr.load_single(FakeTimerPlugin())
        await mgr.shutdown()
        assert mgr.plugin_count == 0

    @pytest.mark.anyio()
    async def test_reload(self, tmp_path: Path) -> None:
        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        await mgr.load_single(FakeWeatherPlugin())
        await mgr.reload("weather")
        assert mgr.is_plugin_loaded("weather")
        tools = mgr.get_tool_definitions()
        assert len(tools) == 1

    @pytest.mark.anyio()
    async def test_reload_not_found(self, tmp_path: Path) -> None:
        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        with pytest.raises(Exception, match="not found"):
            await mgr.reload("ghost")


# ── Properties ──────────────────────────────────────────────────────


class TestProperties:
    """Tests for manager properties."""

    @pytest.mark.anyio()
    async def test_loaded_plugins(self, tmp_path: Path) -> None:
        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        await mgr.load_single(FakeWeatherPlugin())
        assert mgr.loaded_plugins == ["weather"]

    @pytest.mark.anyio()
    async def test_get_plugin(self, tmp_path: Path) -> None:
        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        await mgr.load_single(FakeWeatherPlugin())
        loaded = mgr.get_plugin("weather")
        assert loaded is not None
        assert loaded.plugin.name == "weather"

    @pytest.mark.anyio()
    async def test_get_plugin_not_found(self, tmp_path: Path) -> None:
        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        assert mgr.get_plugin("ghost") is None


# ── Edge Cases ──────────────────────────────────────────────────────


class TestEdgeCases:
    """Tests for uncovered edge cases."""

    @pytest.mark.anyio()
    async def test_instantiation_failure_skipped(self, tmp_path: Path) -> None:
        """Plugin class that fails to instantiate is skipped."""

        class BadPlugin(ISovyxPlugin):
            def __init__(self) -> None:
                msg = "cannot create"
                raise RuntimeError(msg)

            @property
            def name(self) -> str:
                return "bad"

            @property
            def version(self) -> str:
                return "1.0.0"

            @property
            def description(self) -> str:
                return "Bad."

        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        mgr.register_class(BadPlugin)
        loaded = await mgr.load_all()
        assert loaded == []

    @pytest.mark.anyio()
    async def test_load_with_brain_access(self, tmp_path: Path) -> None:
        """Plugin with brain permissions gets BrainAccess."""
        mock_brain = MagicMock()
        mgr = PluginManager(
            brain=mock_brain,
            data_dir=tmp_path,
            granted_permissions={"weather": {"brain:read", "brain:write"}},
            discover_entry_points=False,
            mind_id="test-mind",  # type: ignore[arg-type]
        )
        await mgr.load_single(FakeWeatherPlugin())
        loaded = mgr.get_plugin("weather")
        assert loaded is not None
        assert loaded.context.brain is not None

    @pytest.mark.anyio()
    async def test_load_brain_access_inherits_plugin_manager_mind_id(self, tmp_path: Path) -> None:
        """MISSION-plugin-mind-scope-2026-05-13 T1 regression — the
        plugin's BrainAccess MUST be scoped to the PluginManager's
        configured mind_id (Option F load-time resolution), NOT the
        legacy ``"default"`` sentinel. Pre-fix, every plugin queried
        the phantom default mind regardless of the operator's real
        mind. This test pins the closure.
        """
        mock_brain = MagicMock()
        mgr = PluginManager(
            brain=mock_brain,
            data_dir=tmp_path,
            granted_permissions={"weather": {"brain:read", "brain:write"}},
            discover_entry_points=False,
            mind_id="jonny",  # type: ignore[arg-type]
        )
        await mgr.load_single(FakeWeatherPlugin())
        loaded = mgr.get_plugin("weather")
        assert loaded is not None
        assert loaded.context.brain is not None
        # BrainAccess._mind_id is a private attribute; access via the
        # closure path is the canonical way to verify the binding
        # without coupling to public API surface (no method exposes the
        # mind_id today). After the refactor this MUST be 'jonny',
        # never 'default'.
        assert loaded.context.brain._mind_id == "jonny"

    def test_construction_rejects_brain_without_mind_id(self, tmp_path: Path) -> None:
        """MISSION-plugin-mind-scope-2026-05-13 D-T0-3 fail-loud
        contract: ``PluginManager(brain=X)`` without ``mind_id`` raises
        ``ValueError`` at construction time. Operators cannot construct
        a brain-enabled manager without committing to a mind scope —
        eliminates the pre-fix silent ``"default"`` sentinel.
        """
        with pytest.raises(ValueError, match="requires mind_id when brain"):
            PluginManager(
                brain=MagicMock(),
                data_dir=tmp_path,
                discover_entry_points=False,
            )

    def test_construction_no_brain_no_mind_id_ok(self, tmp_path: Path) -> None:
        """Counterpart to the fail-loud contract: ``brain=None`` does
        NOT require ``mind_id``. Test fixtures that bootstrap a manager
        without brain wiring (no BrainAccess will be granted) continue
        to work without supplying a mind."""
        # No exception raised.
        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        assert mgr is not None

    @pytest.mark.anyio()
    async def test_load_with_event_bus(self, tmp_path: Path) -> None:
        """Plugin with event permissions gets EventBusAccess."""
        mock_bus = MagicMock()
        mgr = PluginManager(
            event_bus=mock_bus,
            data_dir=tmp_path,
            granted_permissions={"weather": {"event:subscribe", "event:emit"}},
            discover_entry_points=False,
        )
        await mgr.load_single(FakeWeatherPlugin())
        loaded = mgr.get_plugin("weather")
        assert loaded is not None
        assert loaded.context.event_bus is not None

    @pytest.mark.anyio()
    async def test_load_with_plugin_config(self, tmp_path: Path) -> None:
        """Per-plugin config passed through."""
        mgr = PluginManager(
            data_dir=tmp_path,
            plugin_config={"weather": {"api_key": "test123"}},
            discover_entry_points=False,
        )
        await mgr.load_single(FakeWeatherPlugin())
        loaded = mgr.get_plugin("weather")
        assert loaded is not None
        assert loaded.context.config == {"api_key": "test123"}

    @pytest.mark.anyio()
    async def test_execute_no_handler(self, tmp_path: Path) -> None:
        """Tool with None handler raises PluginError."""
        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        await mgr.load_single(FakeWeatherPlugin())
        # Manually set handler to None
        loaded = mgr.get_plugin("weather")
        assert loaded is not None
        import dataclasses as dc

        loaded.tools = [dc.replace(loaded.tools[0], handler=None)]
        with pytest.raises(Exception, match="no handler"):
            await mgr.execute("weather.get_weather", {"city": "X"})

    @pytest.mark.anyio()
    async def test_shutdown_with_teardown_error(self, tmp_path: Path) -> None:
        """Shutdown proceeds even if individual teardown fails."""
        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        await mgr.load_single(FailTeardownPlugin())
        await mgr.load_single(FakeWeatherPlugin())
        await mgr.shutdown()
        assert mgr.plugin_count == 0

    @pytest.mark.anyio()
    async def test_reload_with_event_cleanup(self, tmp_path: Path) -> None:
        """Reload cleans up event subscriptions."""
        mock_bus = MagicMock()
        mgr = PluginManager(
            event_bus=mock_bus,
            data_dir=tmp_path,
            granted_permissions={"weather": {"event:subscribe", "event:emit"}},
            discover_entry_points=False,
        )
        await mgr.load_single(FakeWeatherPlugin())
        await mgr.reload("weather")
        assert mgr.is_plugin_loaded("weather")

    @pytest.mark.anyio()
    async def test_default_data_dir(self) -> None:
        """Without data_dir, uses ~/.sovyx/plugins/."""
        mgr = PluginManager(discover_entry_points=False)
        await mgr.load_single(FakeWeatherPlugin())
        assert mgr.is_plugin_loaded("weather")
        # Cleanup
        await mgr.shutdown()

    @pytest.mark.anyio()
    async def test_entry_points_discovery(self, tmp_path: Path) -> None:
        """Entry points discovery finds first-party plugins.

        v0.32.0 Phase C M1 — entry-point auto-discovery is restricted
        to first-party packages (``dist.name == 'sovyx'``) by default.
        Third-party-allowlist coverage lives in
        ``test_entry_point_allowlist.py``.
        """
        from unittest.mock import MagicMock as MM
        from unittest.mock import patch

        mock_ep = MM()
        mock_ep.name = "weather"
        mock_ep.dist = MM()
        mock_ep.dist.name = "sovyx"  # first-party
        mock_ep.load.return_value = FakeWeatherPlugin

        with patch.object(_manager_mod, "entry_points", return_value=[mock_ep], create=True):
            # Need to patch inside the method
            mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
            # Manually test discovery
            with patch("importlib.metadata.entry_points", return_value=[mock_ep]):
                plugins = mgr._discover_entry_points()
            assert len(plugins) == 1
            assert plugins[0] is FakeWeatherPlugin

    @pytest.mark.anyio()
    async def test_entry_points_failure(self, tmp_path: Path) -> None:
        """Failed entry point load is skipped."""
        from unittest.mock import MagicMock as MM
        from unittest.mock import patch

        mock_ep = MM()
        mock_ep.name = "broken"
        mock_ep.dist = MM()
        mock_ep.dist.name = "sovyx"  # first-party so we reach load()
        mock_ep.load.side_effect = ImportError("missing")

        with patch("importlib.metadata.entry_points", return_value=[mock_ep]):
            mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
            plugins = mgr._discover_entry_points()
        assert plugins == []

    @pytest.mark.anyio()
    async def test_entry_points_import_error(self, tmp_path: Path) -> None:
        """entry_points itself failing is handled."""
        from unittest.mock import patch

        with patch("importlib.metadata.entry_points", side_effect=Exception("boom")):
            mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
            plugins = mgr._discover_entry_points()
        assert plugins == []

    @pytest.mark.anyio()
    async def test_unload_with_event_cleanup(self, tmp_path: Path) -> None:
        """Unload cleans up event bus subscriptions."""
        mock_bus = MagicMock()
        mgr = PluginManager(
            event_bus=mock_bus,
            data_dir=tmp_path,
            granted_permissions={"weather": {"event:subscribe"}},
            discover_entry_points=False,
        )
        await mgr.load_single(FakeWeatherPlugin())
        await mgr.unload("weather")
        assert not mgr.is_plugin_loaded("weather")

    @pytest.mark.anyio()
    async def test_permission_denied_in_execute(self, tmp_path: Path) -> None:
        """PermissionDeniedError in tool returns error result."""

        class PermPlugin(ISovyxPlugin):
            @property
            def name(self) -> str:
                return "perm-test"

            @property
            def version(self) -> str:
                return "1.0.0"

            @property
            def description(self) -> str:
                return "Permission test."

            @tool(description="Raise perm error")
            async def denied_op(self) -> str:
                from sovyx.plugins.permissions import PermissionDeniedError as PDE

                raise PDE("perm-test", "nope")

        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        await mgr.load_single(PermPlugin())
        result = await mgr.execute("perm-test.denied_op", {})
        assert result.success is False
        assert "Permission denied" in result.output


# ── Error Boundary & Auto-Disable (TASK-433) ───────────────────────


class TestErrorBoundary:
    """Tests for plugin error boundary, failure tracking, auto-disable."""

    @pytest.mark.anyio()
    async def test_failure_count_increments(self, tmp_path: Path) -> None:
        """Each failed execution increments consecutive failure count."""
        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        await mgr.load_single(ErrorToolPlugin())

        await mgr.execute("error-tool.broken", {})
        health = mgr.get_plugin_health("error-tool")
        assert health["consecutive_failures"] == 1

        await mgr.execute("error-tool.broken", {})
        health = mgr.get_plugin_health("error-tool")
        assert health["consecutive_failures"] == 2

    @pytest.mark.anyio()
    async def test_success_resets_failure_count(self, tmp_path: Path) -> None:
        """Successful execution resets consecutive failure count to 0."""
        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)

        class FlipPlugin(ISovyxPlugin):
            call_count: int = 0

            @property
            def name(self) -> str:
                return "flip"

            @property
            def version(self) -> str:
                return "1.0.0"

            @property
            def description(self) -> str:
                return "Flip between fail/success."

            @tool(description="Maybe fails")
            async def maybe_fail(self) -> str:
                FlipPlugin.call_count += 1
                if FlipPlugin.call_count <= 3:  # noqa: PLR2004
                    msg = "fail"
                    raise RuntimeError(msg)
                return "ok"

        FlipPlugin.call_count = 0
        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        await mgr.load_single(FlipPlugin())

        # 3 failures
        for _ in range(3):
            await mgr.execute("flip.maybe_fail", {})
        health = mgr.get_plugin_health("flip")
        assert health["consecutive_failures"] == 3

        # 1 success resets
        result = await mgr.execute("flip.maybe_fail", {})
        assert result.success is True
        health = mgr.get_plugin_health("flip")
        assert health["consecutive_failures"] == 0

    @pytest.mark.anyio()
    async def test_auto_disable_after_threshold(self, tmp_path: Path) -> None:
        """Plugin auto-disabled after 5 consecutive failures."""
        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        await mgr.load_single(ErrorToolPlugin())

        for _ in range(5):
            await mgr.execute("error-tool.broken", {})

        assert mgr.is_plugin_disabled("error-tool") is True
        health = mgr.get_plugin_health("error-tool")
        assert health["disabled"] is True
        assert health["consecutive_failures"] == 5

    @pytest.mark.anyio()
    async def test_disabled_plugin_raises(self, tmp_path: Path) -> None:
        """Executing tool on disabled plugin raises PluginDisabledError."""
        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        await mgr.load_single(ErrorToolPlugin())

        for _ in range(5):
            await mgr.execute("error-tool.broken", {})

        with pytest.raises(PluginDisabledError, match="disabled"):
            await mgr.execute("error-tool.broken", {})

    @pytest.mark.anyio()
    async def test_re_enable_plugin(self, tmp_path: Path) -> None:
        """Re-enabling a disabled plugin resets health and allows execution."""
        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        await mgr.load_single(ErrorToolPlugin())

        for _ in range(5):
            await mgr.execute("error-tool.broken", {})
        assert mgr.is_plugin_disabled("error-tool")

        mgr.re_enable_plugin("error-tool")
        assert not mgr.is_plugin_disabled("error-tool")
        health = mgr.get_plugin_health("error-tool")
        assert health["consecutive_failures"] == 0
        assert health["last_error"] == ""

    @pytest.mark.anyio()
    async def test_re_enable_not_found(self, tmp_path: Path) -> None:
        """Re-enabling nonexistent plugin raises PluginError."""
        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        with pytest.raises(Exception, match="not found"):
            mgr.re_enable_plugin("ghost")

    @pytest.mark.anyio()
    async def test_timeout_counts_as_failure(self, tmp_path: Path) -> None:
        """Timeout counts toward consecutive failure count."""
        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        await mgr.load_single(SlowPlugin())

        await mgr.execute("slow.slow_op", {}, timeout=0.01)
        health = mgr.get_plugin_health("slow")
        assert health["consecutive_failures"] == 1

    @pytest.mark.anyio()
    async def test_permission_denied_not_counted(self, tmp_path: Path) -> None:
        """PermissionDeniedError does NOT count as plugin failure."""

        class PermFailPlugin(ISovyxPlugin):
            @property
            def name(self) -> str:
                return "perm-fail"

            @property
            def version(self) -> str:
                return "1.0.0"

            @property
            def description(self) -> str:
                return "Permission deny test."

            @tool(description="Denied")
            async def denied(self) -> str:
                from sovyx.plugins.permissions import PermissionDeniedError as PDE

                raise PDE("perm-fail", "nope")

        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        await mgr.load_single(PermFailPlugin())
        await mgr.execute("perm-fail.denied", {})
        health = mgr.get_plugin_health("perm-fail")
        assert health["consecutive_failures"] == 0

    @pytest.mark.anyio()
    async def test_not_disabled_before_threshold(self, tmp_path: Path) -> None:
        """Plugin NOT disabled after fewer than threshold failures."""
        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        await mgr.load_single(ErrorToolPlugin())

        for _ in range(4):
            await mgr.execute("error-tool.broken", {})

        assert mgr.is_plugin_disabled("error-tool") is False

    @pytest.mark.anyio()
    async def test_health_unknown_plugin(self, tmp_path: Path) -> None:
        """get_plugin_health returns defaults for unknown plugin."""
        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        health = mgr.get_plugin_health("unknown")
        assert health["consecutive_failures"] == 0
        assert health["disabled"] is False

    @pytest.mark.anyio()
    async def test_is_disabled_unknown_plugin(self, tmp_path: Path) -> None:
        """is_plugin_disabled returns False for unknown plugin."""
        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        assert mgr.is_plugin_disabled("unknown") is False

    @pytest.mark.anyio()
    async def test_unload_cleans_health(self, tmp_path: Path) -> None:
        """Unloading a plugin removes its health tracking."""
        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        await mgr.load_single(ErrorToolPlugin())
        await mgr.execute("error-tool.broken", {})
        assert mgr.get_plugin_health("error-tool")["consecutive_failures"] == 1

        await mgr.unload("error-tool")
        health = mgr.get_plugin_health("error-tool")
        assert health["consecutive_failures"] == 0

    @pytest.mark.anyio()
    async def test_last_error_tracked(self, tmp_path: Path) -> None:
        """Last error message is stored in health."""
        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        await mgr.load_single(ErrorToolPlugin())
        await mgr.execute("error-tool.broken", {})
        health = mgr.get_plugin_health("error-tool")
        assert "tool broke" in health["last_error"]


# ── Resource Monitoring (TASK-433) ──────────────────────────────────


class TestResourceMonitoring:
    """Tests for active task tracking per plugin."""

    @pytest.mark.anyio()
    async def test_active_tasks_zero_after_execution(self, tmp_path: Path) -> None:
        """Active tasks returns to 0 after execution completes."""
        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        await mgr.load_single(FakeWeatherPlugin())
        await mgr.execute("weather.get_weather", {"city": "Berlin"})
        health = mgr.get_plugin_health("weather")
        assert health["active_tasks"] == 0

    @pytest.mark.anyio()
    async def test_active_tasks_zero_after_failure(self, tmp_path: Path) -> None:
        """Active tasks returns to 0 even after failure."""
        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        await mgr.load_single(ErrorToolPlugin())
        await mgr.execute("error-tool.broken", {})
        health = mgr.get_plugin_health("error-tool")
        assert health["active_tasks"] == 0

    @pytest.mark.anyio()
    async def test_active_tasks_zero_after_timeout(self, tmp_path: Path) -> None:
        """Active tasks returns to 0 after timeout."""
        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        await mgr.load_single(SlowPlugin())
        await mgr.execute("slow.slow_op", {}, timeout=0.01)
        health = mgr.get_plugin_health("slow")
        assert health["active_tasks"] == 0

    @pytest.mark.anyio()
    async def test_active_tasks_during_execution(self, tmp_path: Path) -> None:
        """Active tasks incremented during execution."""

        observed_active: list[int] = []

        class ObservablePlugin(ISovyxPlugin):
            mgr_ref: PluginManager | None = None

            @property
            def name(self) -> str:
                return "observable"

            @property
            def version(self) -> str:
                return "1.0.0"

            @property
            def description(self) -> str:
                return "Observable."

            @tool(description="Observe active")
            async def observe(self) -> str:
                if ObservablePlugin.mgr_ref:
                    h = ObservablePlugin.mgr_ref.get_plugin_health("observable")
                    observed_active.append(h["active_tasks"])
                return "observed"

        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        ObservablePlugin.mgr_ref = mgr
        await mgr.load_single(ObservablePlugin())
        await mgr.execute("observable.observe", {})

        assert len(observed_active) == 1
        assert observed_active[0] == 1  # Was 1 during execution


# ── Event Emission (TASK-433) ───────────────────────────────────────


async def _drain_plugin_events() -> None:
    """Wait for all in-flight ``plugin-event-emit`` tasks to settle.

    Plugin lifecycle events are emitted via ``spawn()`` (fire-and-forget)
    for saga/cause contextvar propagation — the call returns before the
    event reaches the bus. Tests that assert on ``mock_bus.emit`` must
    drain these background tasks first, otherwise the AsyncMock hasn't
    recorded the call yet.
    """
    loop = asyncio.get_running_loop()
    for _ in range(10):
        pending = [t for t in asyncio.all_tasks(loop) if t.get_name() == "plugin-event-emit"]
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)


class TestEventEmission:
    """Tests for PluginToolExecuted and PluginAutoDisabled events."""

    @pytest.mark.anyio()
    async def test_tool_executed_event_on_success(self, tmp_path: Path) -> None:
        """PluginToolExecuted emitted on successful execution."""
        mock_bus = AsyncMock()
        mgr = PluginManager(event_bus=mock_bus, data_dir=tmp_path, discover_entry_points=False)
        await mgr.load_single(FakeWeatherPlugin())
        await mgr.execute("weather.get_weather", {"city": "NYC"})
        await _drain_plugin_events()

        # Find PluginToolExecuted in emit calls
        from sovyx.plugins.events import PluginToolExecuted

        emitted = [
            call.args[0]
            for call in mock_bus.emit.call_args_list
            if isinstance(call.args[0], PluginToolExecuted)
        ]
        assert len(emitted) == 1
        assert emitted[0].plugin_name == "weather"
        assert emitted[0].tool_name == "weather.get_weather"
        assert emitted[0].success is True
        assert emitted[0].duration_ms >= 0
        assert emitted[0].error_message == ""

    @pytest.mark.anyio()
    async def test_tool_executed_event_on_failure(self, tmp_path: Path) -> None:
        """PluginToolExecuted emitted with success=False on error."""
        mock_bus = AsyncMock()
        mgr = PluginManager(event_bus=mock_bus, data_dir=tmp_path, discover_entry_points=False)
        await mgr.load_single(ErrorToolPlugin())
        await mgr.execute("error-tool.broken", {})
        await _drain_plugin_events()

        from sovyx.plugins.events import PluginToolExecuted

        emitted = [
            call.args[0]
            for call in mock_bus.emit.call_args_list
            if isinstance(call.args[0], PluginToolExecuted)
        ]
        assert len(emitted) == 1
        assert emitted[0].success is False
        assert "tool broke" in emitted[0].error_message

    @pytest.mark.anyio()
    async def test_auto_disabled_event(self, tmp_path: Path) -> None:
        """PluginAutoDisabled emitted when plugin reaches failure threshold."""
        mock_bus = AsyncMock()
        mgr = PluginManager(event_bus=mock_bus, data_dir=tmp_path, discover_entry_points=False)
        await mgr.load_single(ErrorToolPlugin())

        for _ in range(5):
            await mgr.execute("error-tool.broken", {})
        await _drain_plugin_events()

        from sovyx.plugins.events import PluginAutoDisabled

        emitted = [
            call.args[0]
            for call in mock_bus.emit.call_args_list
            if isinstance(call.args[0], PluginAutoDisabled)
        ]
        assert len(emitted) == 1
        assert emitted[0].plugin_name == "error-tool"
        assert emitted[0].consecutive_failures == 5
        assert "tool broke" in emitted[0].last_error

    @pytest.mark.anyio()
    async def test_plugin_loaded_event(self, tmp_path: Path) -> None:
        """PluginLoaded emitted when plugin is loaded."""
        mock_bus = AsyncMock()
        mgr = PluginManager(event_bus=mock_bus, data_dir=tmp_path, discover_entry_points=False)
        await mgr.load_single(FakeWeatherPlugin())
        await _drain_plugin_events()

        from sovyx.plugins.events import PluginLoaded

        emitted = [
            call.args[0]
            for call in mock_bus.emit.call_args_list
            if isinstance(call.args[0], PluginLoaded)
        ]
        assert len(emitted) == 1
        assert emitted[0].plugin_name == "weather"
        assert emitted[0].plugin_version == "1.0.0"
        assert emitted[0].tools_count == 1

    @pytest.mark.anyio()
    async def test_plugin_unloaded_event(self, tmp_path: Path) -> None:
        """PluginUnloaded emitted when plugin is unloaded."""
        mock_bus = AsyncMock()
        mgr = PluginManager(event_bus=mock_bus, data_dir=tmp_path, discover_entry_points=False)
        await mgr.load_single(FakeWeatherPlugin())
        await _drain_plugin_events()
        mock_bus.emit.reset_mock()

        await mgr.unload("weather")
        await _drain_plugin_events()

        from sovyx.plugins.events import PluginUnloaded

        emitted = [
            call.args[0]
            for call in mock_bus.emit.call_args_list
            if isinstance(call.args[0], PluginUnloaded)
        ]
        assert len(emitted) == 1
        assert emitted[0].plugin_name == "weather"
        assert emitted[0].reason == "explicit"

    @pytest.mark.anyio()
    async def test_no_event_without_bus(self, tmp_path: Path) -> None:
        """No crash when event_bus is None."""
        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        await mgr.load_single(FakeWeatherPlugin())
        # Should not crash
        await mgr.execute("weather.get_weather", {"city": "X"})

    @pytest.mark.anyio()
    async def test_tool_executed_event_on_timeout(self, tmp_path: Path) -> None:
        """PluginToolExecuted emitted on timeout."""
        mock_bus = AsyncMock()
        mgr = PluginManager(event_bus=mock_bus, data_dir=tmp_path, discover_entry_points=False)
        await mgr.load_single(SlowPlugin())
        await mgr.execute("slow.slow_op", {}, timeout=0.01)
        await _drain_plugin_events()

        from sovyx.plugins.events import PluginToolExecuted

        emitted = [
            call.args[0]
            for call in mock_bus.emit.call_args_list
            if isinstance(call.args[0], PluginToolExecuted)
        ]
        assert len(emitted) == 1
        assert emitted[0].success is False
        assert "timed out" in emitted[0].error_message


# ── PluginHealth dataclass (TASK-433) ───────────────────────────────


class TestPluginHealth:
    """Tests for _PluginHealth internals."""

    def test_defaults(self) -> None:
        """Health starts with sane defaults."""
        h = _PluginHealth()
        assert h.consecutive_failures == 0
        assert h.disabled is False
        assert h.last_error == ""
        assert h.active_tasks == 0

    def test_mutation(self) -> None:
        """Health fields are mutable."""
        h = _PluginHealth()
        h.consecutive_failures = 3
        h.disabled = True
        h.last_error = "boom"
        h.active_tasks = 2
        assert h.consecutive_failures == 3
        assert h.disabled is True


# ── v0.32.0 Phase C C2 — unload/reload-while-active race ───────────


class _TrackedSlowPlugin(ISovyxPlugin):
    """Plugin whose tool sleeps for *delay* seconds; teardown_called flag."""

    teardown_called: bool = False
    teardown_observed_active: int = -1

    @property
    def name(self) -> str:
        return "tracked-slow"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def description(self) -> str:
        return "Slow plugin that records teardown observation."

    @tool(description="Slow tool")
    async def slow_op(self, delay: float = 0.05) -> str:
        await asyncio.sleep(delay)
        return "done"

    async def teardown(self) -> None:
        # The whole point of C2: by the time teardown runs, no in-flight
        # tasks must remain. Capture the count for the test to assert on.
        type(self).teardown_called = True


class TestUnloadInFlightRace:
    """v0.32.0 Phase C C2 — unload/reload waits for in-flight tasks."""

    @pytest.mark.anyio()
    async def test_unload_waits_for_active_tasks(self, tmp_path: Path) -> None:
        """Long-running tool tasks complete naturally before teardown.

        C2 contract: ``unload`` MUST drain in-flight tool tasks before
        running ``teardown()`` + dropping the registry entry.
        """
        _TrackedSlowPlugin.teardown_called = False
        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        await mgr.load_single(_TrackedSlowPlugin())

        # Kick off two long-running tool tasks.
        task1 = asyncio.create_task(
            mgr.execute("tracked-slow.slow_op", {"delay": 0.1}, timeout=5.0)
        )
        task2 = asyncio.create_task(
            mgr.execute("tracked-slow.slow_op", {"delay": 0.1}, timeout=5.0)
        )
        # Yield so the tasks register themselves into _in_flight_tasks.
        await asyncio.sleep(0.01)
        assert len(mgr._in_flight_tasks["tracked-slow"]) == 2  # noqa: SLF001

        # Unload with generous timeout — both tasks should finish naturally.
        await mgr.unload("tracked-slow", timeout=5.0)

        # Both tool tasks completed successfully (drained, not cancelled).
        result1 = await task1
        result2 = await task2
        assert result1.success is True
        assert result2.success is True
        # Plugin gone from registry.
        assert not mgr.is_plugin_loaded("tracked-slow")
        assert _TrackedSlowPlugin.teardown_called is True

    @pytest.mark.anyio()
    async def test_unload_force_cancels_after_timeout(self, tmp_path: Path) -> None:
        """Tasks still running past the unload timeout get cancelled.

        C2 contract: after the cooperative wait expires, ``unload``
        force-cancels via ``task.cancel()`` so it cannot stall on a
        misbehaving plugin.
        """
        _TrackedSlowPlugin.teardown_called = False
        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        await mgr.load_single(_TrackedSlowPlugin())

        # Start a tool that runs longer than the unload timeout.
        task = asyncio.create_task(
            mgr.execute("tracked-slow.slow_op", {"delay": 5.0}, timeout=10.0)
        )
        await asyncio.sleep(0.01)  # let it register

        # Unload with a tight timeout — task gets cancelled.
        start = time.monotonic()
        await mgr.unload("tracked-slow", timeout=0.05)
        elapsed = time.monotonic() - start
        # Drain + grace = ~0.05 + 1.0 max; never the 5 s of the slow op.
        assert elapsed < 3.0, f"unload took {elapsed}s — expected <3s"

        # Task was cancelled (or completed via the cancel grace window).
        assert task.done()
        # Plugin tore down + registry dropped despite the in-flight task.
        assert not mgr.is_plugin_loaded("tracked-slow")
        assert _TrackedSlowPlugin.teardown_called is True
        # In-flight bucket cleared.
        assert "tracked-slow" not in mgr._in_flight_tasks  # noqa: SLF001

    @pytest.mark.anyio()
    async def test_reload_waits_for_active_tasks(self, tmp_path: Path) -> None:
        """Reload uses the same drain protocol as unload.

        C2 contract: ``reload`` calls ``_drain_in_flight`` BEFORE
        ``plugin.teardown()`` so a tool mid-execution doesn't observe
        its globals get torn out from under it.
        """
        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        await mgr.load_single(_TrackedSlowPlugin())

        # Start one long-running tool.
        task = asyncio.create_task(
            mgr.execute("tracked-slow.slow_op", {"delay": 0.1}, timeout=5.0)
        )
        await asyncio.sleep(0.01)
        assert len(mgr._in_flight_tasks["tracked-slow"]) == 1  # noqa: SLF001

        # Reload — should drain the in-flight task first.
        await mgr.reload("tracked-slow", timeout=5.0)

        # Tool completed naturally (not cancelled).
        result = await task
        assert result.success is True
        # Plugin still loaded post-reload.
        assert mgr.is_plugin_loaded("tracked-slow")

    @pytest.mark.anyio()
    async def test_unload_with_no_in_flight_tasks_is_fast(self, tmp_path: Path) -> None:
        """Drain is a no-op when no tasks are in-flight."""
        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        await mgr.load_single(FakeWeatherPlugin())

        start = time.monotonic()
        await mgr.unload("weather", timeout=10.0)
        elapsed = time.monotonic() - start
        # No tasks → no waiting; should be near-instant.
        assert elapsed < 0.5
        assert not mgr.is_plugin_loaded("weather")
