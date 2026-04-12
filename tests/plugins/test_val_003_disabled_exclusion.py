"""VAL-003: Disabled plugins excluded from tools= sent to LLM."""

from __future__ import annotations

from pathlib import Path

import pytest

from sovyx.plugins.manager import PluginManager
from sovyx.plugins.official.calculator import CalculatorPlugin
from sovyx.plugins.sdk import ISovyxPlugin
from sovyx.plugins.sdk import tool as tool_dec


class _FailPlugin(ISovyxPlugin):
    @property
    def name(self) -> str:
        return "fail-plugin"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def description(self) -> str:
        return "Always fails"

    @tool_dec(description="Always fails")
    async def fail(self) -> str:
        msg = "boom"
        raise RuntimeError(msg)


class TestDisabledExcluded:
    """Disabled plugin tools must NOT appear in get_tool_definitions()."""

    @pytest.mark.anyio()
    async def test_disabled_tools_excluded(self, tmp_path: Path) -> None:
        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        await mgr.load_single(CalculatorPlugin())
        await mgr.load_single(_FailPlugin())

        # Both present before disable
        defs = mgr.get_tool_definitions()
        names = {d.name for d in defs}
        assert "calculator.calculate" in names
        assert "fail-plugin.fail" in names

        # Trigger auto-disable
        for _ in range(5):
            await mgr.execute("fail-plugin.fail", {})
        assert mgr.is_plugin_disabled("fail-plugin")

        # Disabled plugin excluded
        defs = mgr.get_tool_definitions()
        names = {d.name for d in defs}
        assert "calculator.calculate" in names
        assert "fail-plugin.fail" not in names

    @pytest.mark.anyio()
    async def test_re_enabled_tools_reappear(self, tmp_path: Path) -> None:
        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        await mgr.load_single(_FailPlugin())

        for _ in range(5):
            await mgr.execute("fail-plugin.fail", {})
        assert mgr.is_plugin_disabled("fail-plugin")
        assert len(mgr.get_tool_definitions()) == 0

        # Re-enable
        mgr.re_enable_plugin("fail-plugin")
        assert not mgr.is_plugin_disabled("fail-plugin")

        defs = mgr.get_tool_definitions()
        assert len(defs) == 1
        assert defs[0].name == "fail-plugin.fail"

    @pytest.mark.anyio()
    async def test_tool_count_updates_correctly(self, tmp_path: Path) -> None:
        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        await mgr.load_single(CalculatorPlugin())
        await mgr.load_single(_FailPlugin())

        assert len(mgr.get_tool_definitions()) == 3  # calc + percentage + fail-plugin

        for _ in range(5):
            await mgr.execute("fail-plugin.fail", {})

        assert len(mgr.get_tool_definitions()) == 2  # calc + percentage (fail disabled)

        mgr.re_enable_plugin("fail-plugin")
        assert len(mgr.get_tool_definitions()) == 3  # calc + percentage + fail-plugin
