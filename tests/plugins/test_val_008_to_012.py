"""VAL-008 to VAL-012: Marketplace, Distribution, Isolation, Chaos, Dashboard."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from sovyx.dashboard.plugins import get_plugin_detail, get_plugins_status, get_tools_list
from sovyx.plugins.events import (
    PluginAutoDisabled,
    PluginLoaded,
    PluginToolExecuted,
)
from sovyx.plugins.manager import PluginManager
from sovyx.plugins.manifest import PluginManifest
from sovyx.plugins.official.calculator import CalculatorPlugin
from sovyx.plugins.sdk import ISovyxPlugin
from sovyx.plugins.sdk import tool as tool_dec

# ═══════════════════════════════════════════════════════════════════
# VAL-008: Manifest marketplace-grade validation
# ═══════════════════════════════════════════════════════════════════


class TestManifestMarketplace:
    """Manifest supports marketplace metadata without breaking existing plugins."""

    def test_backward_compatible(self) -> None:
        m = PluginManifest(name="old-plugin", version="1.0.0", description="test")
        assert m.pricing == "free"
        assert m.price_usd is None
        assert m.category == ""
        assert m.tags == []
        assert m.trial_days == 0

    def test_marketplace_fields(self) -> None:
        m = PluginManifest(
            name="premium",
            version="2.0.0",
            description="Premium plugin",
            category="finance",
            tags=["trading", "stocks"],
            pricing="paid",
            price_usd=9.99,
            trial_days=14,
            icon_url="https://example.com/icon.png",
            screenshots=["https://example.com/s1.png"],
        )
        assert m.pricing == "paid"
        assert m.price_usd == 9.99
        assert m.trial_days == 14
        assert m.category == "finance"
        assert len(m.tags) == 2

    def test_name_validation(self) -> None:
        with pytest.raises(Exception):
            PluginManifest(name="UPPERCASE", version="1.0", description="x")
        with pytest.raises(Exception):
            PluginManifest(name="has space", version="1.0", description="x")
        with pytest.raises(Exception):
            PluginManifest(name="bad--name", version="1.0", description="x")

    def test_version_validation(self) -> None:
        with pytest.raises(Exception):
            PluginManifest(name="test", version="1", description="x")
        # Valid
        PluginManifest(name="test", version="1.0", description="x")
        PluginManifest(name="test", version="1.0.0", description="x")


# ═══════════════════════════════════════════════════════════════════
# VAL-010: Cross-plugin isolation
# ═══════════════════════════════════════════════════════════════════


class TestPluginIsolation:
    """Each plugin is isolated from others."""

    @pytest.mark.anyio()
    async def test_separate_data_dirs(self, tmp_path: Path) -> None:
        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)

        class PluginA(ISovyxPlugin):
            @property
            def name(self) -> str:
                return "plugin-a"

            @property
            def version(self) -> str:
                return "1.0.0"

            @property
            def description(self) -> str:
                return "A"

        class PluginB(ISovyxPlugin):
            @property
            def name(self) -> str:
                return "plugin-b"

            @property
            def version(self) -> str:
                return "1.0.0"

            @property
            def description(self) -> str:
                return "B"

        await mgr.load_single(PluginA())
        await mgr.load_single(PluginB())

        ctx_a = mgr.get_plugin("plugin-a")
        ctx_b = mgr.get_plugin("plugin-b")
        assert ctx_a is not None
        assert ctx_b is not None
        assert ctx_a.context.data_dir != ctx_b.context.data_dir
        assert "plugin-a" in str(ctx_a.context.data_dir)
        assert "plugin-b" in str(ctx_b.context.data_dir)

    @pytest.mark.anyio()
    async def test_separate_loggers(self, tmp_path: Path) -> None:
        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        await mgr.load_single(CalculatorPlugin())

        loaded = mgr.get_plugin("calculator")
        assert loaded is not None
        assert "calculator" in loaded.context.logger.name


# ═══════════════════════════════════════════════════════════════════
# VAL-011: Chaos testing
# ═══════════════════════════════════════════════════════════════════


class TestChaos:
    """Plugins that misbehave must not crash the engine."""

    @pytest.mark.anyio()
    async def test_infinite_loop_timeout(self, tmp_path: Path) -> None:
        class LoopPlugin(ISovyxPlugin):
            @property
            def name(self) -> str:
                return "looper"

            @property
            def version(self) -> str:
                return "1.0.0"

            @property
            def description(self) -> str:
                return "loops"

            @tool_dec(description="Loops forever")
            async def loop(self) -> str:
                while True:
                    await asyncio.sleep(0.001)
                return "done"  # unreachable

        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        await mgr.load_single(LoopPlugin())
        result = await mgr.execute("looper.loop", {}, timeout=0.1)
        assert result.success is False
        assert "timed out" in result.output.lower()

    @pytest.mark.anyio()
    async def test_non_string_return(self, tmp_path: Path) -> None:
        class BadReturn(ISovyxPlugin):
            @property
            def name(self) -> str:
                return "bad-return"

            @property
            def version(self) -> str:
                return "1.0.0"

            @property
            def description(self) -> str:
                return "returns wrong type"

            @tool_dec(description="Returns int")
            async def calc(self) -> str:
                return 42  # type: ignore[return-value]

        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        await mgr.load_single(BadReturn())
        result = await mgr.execute("bad-return.calc", {})
        # str(42) = "42" — handled gracefully
        assert result.success is True
        assert result.output == "42"

    @pytest.mark.anyio()
    async def test_100_concurrent_calls(self, tmp_path: Path) -> None:
        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        await mgr.load_single(CalculatorPlugin())

        async def calc(i: int) -> str:
            r = await mgr.execute("calculator.calculate", {"expression": f"{i}+{i}"})
            return r.output

        results = await asyncio.gather(*[calc(i) for i in range(100)])
        assert len(results) == 100
        assert results[50] == "100"
        health = mgr.get_plugin_health("calculator")
        assert health["active_tasks"] == 0


# ═══════════════════════════════════════════════════════════════════
# VAL-012: Dashboard + events consistency
# ═══════════════════════════════════════════════════════════════════


class TestDashboardConsistency:
    """Dashboard reflects correct plugin state."""

    @pytest.mark.anyio()
    async def test_disabled_shown_correctly(self, tmp_path: Path) -> None:
        class FailPlugin(ISovyxPlugin):
            @property
            def name(self) -> str:
                return "fail-dash"

            @property
            def version(self) -> str:
                return "1.0.0"

            @property
            def description(self) -> str:
                return "fails"

            @tool_dec(description="fails")
            async def fail(self) -> str:
                msg = "boom"
                raise RuntimeError(msg)

        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        await mgr.load_single(CalculatorPlugin())
        await mgr.load_single(FailPlugin())

        # Before disable
        status = get_plugins_status(mgr)
        assert status["total"] == 2
        assert status["active"] == 2
        assert status["disabled"] == 0

        # Disable
        for _ in range(5):
            await mgr.execute("fail-dash.fail", {})

        status = get_plugins_status(mgr)
        assert status["active"] == 1
        assert status["disabled"] == 1

        # Tools list excludes disabled
        tools = get_tools_list(mgr)
        tool_names = {t["name"] for t in tools}
        assert "calculator.calculate" in tool_names
        assert "fail-dash.fail" not in tool_names

    @pytest.mark.anyio()
    async def test_health_resets_on_re_enable(self, tmp_path: Path) -> None:
        class FailPlugin2(ISovyxPlugin):
            @property
            def name(self) -> str:
                return "fail2"

            @property
            def version(self) -> str:
                return "1.0.0"

            @property
            def description(self) -> str:
                return "fails"

            @tool_dec(description="fails")
            async def fail(self) -> str:
                msg = "boom"
                raise RuntimeError(msg)

        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        await mgr.load_single(FailPlugin2())

        for _ in range(5):
            await mgr.execute("fail2.fail", {})

        detail = get_plugin_detail(mgr, "fail2")
        assert detail is not None
        assert detail["status"] == "disabled"

        mgr.re_enable_plugin("fail2")

        detail = get_plugin_detail(mgr, "fail2")
        assert detail is not None
        assert detail["status"] == "active"
        assert detail["health"]["consecutive_failures"] == 0

    def test_zero_plugins(self) -> None:
        status = get_plugins_status(None)
        assert status["available"] is False
        assert status["total"] == 0
        assert get_tools_list(None) == []


class TestEventEmission:
    """Events emitted correctly and never crash engine."""

    def test_events_are_frozen(self) -> None:
        evt = PluginLoaded(plugin_name="test", plugin_version="1.0", tools_count=2)
        with pytest.raises(AttributeError):
            evt.plugin_name = "mutated"  # type: ignore[misc]

    def test_auto_disabled_event_fields(self) -> None:
        evt = PluginAutoDisabled(
            plugin_name="bad",
            consecutive_failures=5,
            last_error="boom",
        )
        assert evt.plugin_name == "bad"
        assert evt.consecutive_failures == 5

    def test_tool_executed_event(self) -> None:
        evt = PluginToolExecuted(
            plugin_name="calc",
            tool_name="calculate",
            success=True,
            duration_ms=10,
            error_message="",
        )
        assert evt.success is True
