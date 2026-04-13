"""Integration tests — full plugin pipeline (TASK-449).

Tests the complete flow: PluginManager → ToolDefinition → LLMRouter → ActPhase → response.
No mocks on internal components — only external APIs are mocked.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from sovyx.cognitive.act import ActPhase, ToolExecutor
from sovyx.llm.models import LLMResponse, ToolCall
from sovyx.llm.router import LLMRouter
from sovyx.plugins.manager import PluginManager
from sovyx.plugins.official.calculator import CalculatorPlugin
from sovyx.plugins.testing import MockBrainAccess

# ── Helpers ─────────────────────────────────────────────────────────


def _perception(text: str = "test") -> MagicMock:
    p = MagicMock()
    p.source = "test-channel"
    p.text = text
    p.language = "en"
    return p


def _response(
    content: str = "",
    tool_calls: list[ToolCall] | None = None,
    finish_reason: str = "stop",
) -> LLMResponse:
    return LLMResponse(
        content=content,
        model="test",
        tokens_in=10,
        tokens_out=5,
        latency_ms=50,
        cost_usd=0.001,
        finish_reason=finish_reason,
        provider="test",
        tool_calls=tool_calls,
    )


# ── Full Pipeline Tests ────────────────────────────────────────────


class TestPluginToToolDefinition:
    """PluginManager produces tool definitions compatible with LLM providers."""

    @pytest.mark.anyio()
    async def test_calculator_definitions(self, tmp_path: Path) -> None:
        """Calculator plugin produces valid tool definitions."""
        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        await mgr.load_single(CalculatorPlugin())

        defs = mgr.get_tool_definitions()
        assert len(defs) >= 2  # calculate + percentage + interest (inherited)
        names = [d.name for d in defs]
        assert "calculator.calculate" in names
        calc_def = next(d for d in defs if d.name == "calculator.calculate")
        assert "expression" in str(calc_def.parameters)

    @pytest.mark.anyio()
    async def test_definitions_to_dicts(self, tmp_path: Path) -> None:
        """ToolDefinition → dict conversion for LLM."""
        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        await mgr.load_single(CalculatorPlugin())

        defs = mgr.get_tool_definitions()
        dicts = LLMRouter.tool_definitions_to_dicts(defs)

        assert len(dicts) >= 2
        dict_names = [d["name"] for d in dicts]
        assert "calculator.calculate" in dict_names
        assert isinstance(dicts[0]["description"], str)


class TestToolCallToExecution:
    """ToolExecutor dispatches tool calls to PluginManager."""

    @pytest.mark.anyio()
    async def test_calculator_execution(self, tmp_path: Path) -> None:
        """Calculator tool call is dispatched and returns correct result."""
        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        await mgr.load_single(CalculatorPlugin())

        executor = ToolExecutor(plugin_manager=mgr)
        calls = [
            ToolCall(
                id="tc1",
                function_name="calculator.calculate",
                arguments={"expression": "2 + 3 * 4"},
            ),
        ]
        results = await executor.execute(calls)

        assert len(results) == 1
        assert results[0].success is True
        assert results[0].output == "14"
        assert results[0].call_id == "tc1"

    @pytest.mark.anyio()
    async def test_multiple_tool_calls(self, tmp_path: Path) -> None:
        """Multiple tool calls executed independently."""
        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        await mgr.load_single(CalculatorPlugin())

        executor = ToolExecutor(plugin_manager=mgr)
        calls = [
            ToolCall(
                id="tc1", function_name="calculator.calculate", arguments={"expression": "10 / 3"}
            ),
            ToolCall(
                id="tc2", function_name="calculator.calculate", arguments={"expression": "2 ** 10"}
            ),
        ]
        results = await executor.execute(calls)

        assert len(results) == 2
        assert all(r.success for r in results)
        assert results[1].output == "1024"


class TestFullReActLoop:
    """Complete ReAct loop: LLM → tool_call → execute → re-invoke → final response."""

    @pytest.mark.anyio()
    async def test_calculator_react(self, tmp_path: Path) -> None:
        """Full ReAct: LLM asks for calculation → plugin executes → LLM responds."""
        # Setup real PluginManager
        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        await mgr.load_single(CalculatorPlugin())

        executor = ToolExecutor(plugin_manager=mgr)

        # Mock LLM router: first call returns tool_call, second returns text
        mock_router = AsyncMock()
        mock_router.generate = AsyncMock(
            return_value=_response("2 + 3 * 4 = 14"),
        )

        act = ActPhase(tool_executor=executor, llm_router=mock_router)

        # Initial LLM response with tool_call
        initial = _response(
            content="Let me calculate that.",
            finish_reason="tool_use",
            tool_calls=[
                ToolCall(
                    id="tc1",
                    function_name="calculator.calculate",
                    arguments={"expression": "2 + 3 * 4"},
                ),
            ],
        )

        result = await act.process(
            initial,
            [{"role": "user", "content": "what is 2 + 3 * 4?"}],
            _perception("what is 2 + 3 * 4?"),
        )

        assert result.response_text == "2 + 3 * 4 = 14"
        assert len(result.tool_calls_made) == 1
        assert result.tool_calls_made[0].function_name == "calculator.calculate"

        # Verify LLM was re-invoked with tool results
        call_args = mock_router.generate.call_args
        reinvoke_messages = call_args[1].get("messages", call_args[0][0] if call_args[0] else [])
        tool_msg = [m for m in reinvoke_messages if m["role"] == "tool"]
        assert len(tool_msg) == 1
        assert "14" in tool_msg[0]["content"]


class TestErrorBoundaryIntegration:
    """Error boundary prevents plugin crashes from crashing the engine."""

    @pytest.mark.anyio()
    async def test_invalid_expression_handled(self, tmp_path: Path) -> None:
        """Invalid expression returns error result, not crash."""
        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        await mgr.load_single(CalculatorPlugin())

        executor = ToolExecutor(plugin_manager=mgr)
        calls = [
            ToolCall(
                id="tc1",
                function_name="calculator.calculate",
                arguments={"expression": "import os"},
            ),
        ]
        results = await executor.execute(calls)

        assert len(results) == 1
        assert results[0].success is True  # Plugin returned error string, not crashed
        assert "Error" in results[0].output

    @pytest.mark.anyio()
    async def test_nonexistent_plugin_handled(self, tmp_path: Path) -> None:
        """Tool call to nonexistent plugin returns error, not crash."""
        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        executor = ToolExecutor(plugin_manager=mgr)

        calls = [
            ToolCall(id="tc1", function_name="ghost.tool", arguments={}),
        ]
        results = await executor.execute(calls)

        assert len(results) == 1
        assert results[0].success is False
        assert "not found" in results[0].output.lower()


class TestAutoDisableIntegration:
    """Auto-disable after consecutive failures."""

    @pytest.mark.anyio()
    async def test_auto_disable_after_failures(self, tmp_path: Path) -> None:
        """Plugin auto-disabled after 5 consecutive failures."""
        from sovyx.plugins.sdk import ISovyxPlugin
        from sovyx.plugins.sdk import tool as tool_dec

        class AlwaysFailPlugin(ISovyxPlugin):
            @property
            def name(self) -> str:
                return "always-fail"

            @property
            def version(self) -> str:
                return "1.0.0"

            @property
            def description(self) -> str:
                return "Always fails."

            @tool_dec(description="Always fails")
            async def fail(self) -> str:
                msg = "intentional failure"
                raise RuntimeError(msg)

        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        await mgr.load_single(AlwaysFailPlugin())

        # 5 failures
        for _ in range(5):
            await mgr.execute("always-fail.fail", {})

        # Should be disabled now
        assert mgr.is_plugin_disabled("always-fail")

        # 6th call should raise PluginDisabledError
        with pytest.raises(Exception, match="disabled") as exc_info:
            await mgr.execute("always-fail.fail", {})
        assert type(exc_info.value).__name__ == "PluginDisabledError"

        # Re-enable works
        mgr.re_enable_plugin("always-fail")
        assert not mgr.is_plugin_disabled("always-fail")


class TestMockContextIntegration:
    """MockPluginContext works with real plugins."""

    @pytest.mark.anyio()
    async def test_knowledge_with_mock_brain(self) -> None:
        """Knowledge plugin works with MockBrainAccess."""
        from sovyx.plugins.official.knowledge import KnowledgePlugin

        brain = MockBrainAccess()
        brain.seed(
            [
                {"name": "dark-mode", "content": "User prefers dark mode"},
            ]
        )

        plugin = KnowledgePlugin(brain=brain)

        # Search
        result = await plugin.search("dark mode")
        assert "dark-mode" in result

        # Remember
        result = await plugin.remember("New fact", name="fact-1")
        assert "Remembered" in result
        brain.assert_learned("fact-1")

        # Recall
        result = await plugin.recall_about("dark")
        assert "dark-mode" in result

    @pytest.mark.anyio()
    async def test_calculator_no_context_needed(self) -> None:
        """Calculator plugin works standalone (no context needed)."""
        plugin = CalculatorPlugin()
        result = await plugin.calculate("pi * 2")
        assert result.startswith("6.28")
