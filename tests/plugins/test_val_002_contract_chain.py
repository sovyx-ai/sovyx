"""VAL-002: PluginManager.execute() ↔ ToolExecutor ↔ ActPhase contract chain."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from sovyx.cognitive.act import ActPhase, ToolExecutor
from sovyx.llm.models import LLMResponse, ToolCall
from sovyx.plugins.manager import PluginManager
from sovyx.plugins.official.calculator import CalculatorPlugin


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


class TestCallIdPropagation:
    """call_id must flow from LLM → ToolExecutor → ToolResult → response."""

    @pytest.mark.anyio()
    async def test_call_id_preserved(self, tmp_path: Path) -> None:
        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        await mgr.load_single(CalculatorPlugin())
        executor = ToolExecutor(plugin_manager=mgr)

        calls = [
            ToolCall(
                id="call_abc123",
                function_name="calculator.calculate",
                arguments={"expression": "1+1"},
            ),
        ]
        results = await executor.execute(calls)

        assert results[0].call_id == "call_abc123"
        assert results[0].name == "calculator.calculate"

    @pytest.mark.anyio()
    async def test_call_id_on_error(self, tmp_path: Path) -> None:
        """Even on error, call_id must be preserved."""
        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        executor = ToolExecutor(plugin_manager=mgr)

        calls = [
            ToolCall(id="call_err", function_name="ghost.tool", arguments={}),
        ]
        results = await executor.execute(calls)

        assert results[0].call_id == "call_err"
        assert results[0].success is False

    @pytest.mark.anyio()
    async def test_multiple_calls_ids_preserved(self, tmp_path: Path) -> None:
        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        await mgr.load_single(CalculatorPlugin())
        executor = ToolExecutor(plugin_manager=mgr)

        calls = [
            ToolCall(
                id="id_1", function_name="calculator.calculate", arguments={"expression": "2+2"}
            ),
            ToolCall(
                id="id_2", function_name="calculator.calculate", arguments={"expression": "3+3"}
            ),
        ]
        results = await executor.execute(calls)

        assert results[0].call_id == "id_1"
        assert results[0].output == "4"
        assert results[1].call_id == "id_2"
        assert results[1].output == "6"


class TestToolResultFormat:
    """Tool results must be formatted correctly for LLM re-invocation."""

    @pytest.mark.anyio()
    async def test_react_injects_tool_message(self, tmp_path: Path) -> None:
        """ReAct loop injects tool result as 'tool' role message."""
        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        await mgr.load_single(CalculatorPlugin())
        executor = ToolExecutor(plugin_manager=mgr)

        mock_router = AsyncMock()
        mock_router.generate = AsyncMock(
            return_value=_response("The answer is 4."),
        )

        act = ActPhase(tool_executor=executor, llm_router=mock_router)
        perception = MagicMock()
        perception.source = "test"
        perception.text = "what is 2+2"
        perception.language = "en"

        initial = _response(
            content="",
            finish_reason="tool_use",
            tool_calls=[
                ToolCall(
                    id="tc1", function_name="calculator.calculate", arguments={"expression": "2+2"}
                ),
            ],
        )

        result = await act.process(
            initial,
            [{"role": "user", "content": "what is 2+2"}],
            perception,
        )

        # Verify the re-invocation messages
        call_args = mock_router.generate.call_args
        messages = call_args[1].get("messages", call_args[0][0] if call_args[0] else [])
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        assert len(tool_msgs) == 1
        assert "4" in tool_msgs[0]["content"]

    @pytest.mark.anyio()
    async def test_error_result_in_tool_message(self, tmp_path: Path) -> None:
        """Plugin errors are included in tool message (not swallowed)."""
        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        await mgr.load_single(CalculatorPlugin())
        executor = ToolExecutor(plugin_manager=mgr)

        mock_router = AsyncMock()
        mock_router.generate = AsyncMock(
            return_value=_response("I couldn't calculate that."),
        )

        act = ActPhase(tool_executor=executor, llm_router=mock_router)
        perception = MagicMock()
        perception.source = "test"
        perception.text = "eval import os"
        perception.language = "en"

        initial = _response(
            content="",
            finish_reason="tool_use",
            tool_calls=[
                ToolCall(
                    id="tc1",
                    function_name="calculator.calculate",
                    arguments={"expression": "import os"},
                ),
            ],
        )

        await act.process(initial, [{"role": "user", "content": "test"}], perception)

        call_args = mock_router.generate.call_args
        messages = call_args[1].get("messages", call_args[0][0] if call_args[0] else [])
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        assert len(tool_msgs) == 1
        assert "Error" in tool_msgs[0]["content"]


class TestNoStackTraceLeaks:
    """Plugin errors must not leak internal stack traces to users."""

    @pytest.mark.anyio()
    async def test_exception_message_only(self, tmp_path: Path) -> None:
        from sovyx.plugins.sdk import ISovyxPlugin
        from sovyx.plugins.sdk import tool as tool_dec

        class LeakyPlugin(ISovyxPlugin):
            @property
            def name(self) -> str:
                return "leaky"

            @property
            def version(self) -> str:
                return "1.0.0"

            @property
            def description(self) -> str:
                return "test"

            @tool_dec(description="Leaks")
            async def leak(self) -> str:
                # This would normally produce a long traceback
                raise ValueError("secret_internal_detail: /root/.sovyx/config.yaml")

        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        await mgr.load_single(LeakyPlugin())

        result = await mgr.execute("leaky.leak", {})

        # The error message is included (this is expected — it's for the LLM)
        # But the TRACEBACK should not be in the output
        assert "Traceback" not in result.output
        assert result.success is False


class TestReActDepthRespected:
    """ReAct loop must not exceed max_depth."""

    @pytest.mark.anyio()
    async def test_max_depth_3(self, tmp_path: Path) -> None:
        mgr = PluginManager(data_dir=tmp_path, discover_entry_points=False)
        await mgr.load_single(CalculatorPlugin())
        executor = ToolExecutor(plugin_manager=mgr, max_depth=3)

        call_count = 0

        async def mock_generate(**kwargs):
            nonlocal call_count
            call_count += 1
            # Always return a tool_call (infinite loop attempt)
            return _response(
                content="" if call_count < 5 else "done",
                finish_reason="tool_use" if call_count < 5 else "stop",
                tool_calls=[
                    ToolCall(
                        id=f"tc{call_count}",
                        function_name="calculator.calculate",
                        arguments={"expression": "1+1"},
                    ),
                ]
                if call_count < 5
                else None,
            )

        mock_router = AsyncMock()
        mock_router.generate = AsyncMock(side_effect=mock_generate)

        act = ActPhase(tool_executor=executor, llm_router=mock_router)
        perception = MagicMock()
        perception.source = "test"
        perception.text = "test"
        perception.language = "en"

        initial = _response(
            finish_reason="tool_use",
            tool_calls=[
                ToolCall(
                    id="tc0", function_name="calculator.calculate", arguments={"expression": "1+1"}
                )
            ],
        )

        result = await act.process(
            initial,
            [{"role": "user", "content": "test"}],
            perception,
        )

        # Max 3 re-invocations + initial = at most 3 calls to generate
        assert call_count <= 3
