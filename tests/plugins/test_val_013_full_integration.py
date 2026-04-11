"""VAL-013: Full integration — real LLM mock → plugin → response pipeline."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from sovyx.cognitive.act import ActPhase, ToolExecutor
from sovyx.cognitive.think import ThinkPhase
from sovyx.context.assembler import AssembledContext
from sovyx.llm.models import LLMResponse, ToolCall
from sovyx.mind.config import MindConfig
from sovyx.plugins.manager import PluginManager
from sovyx.plugins.official.calculator import CalculatorPlugin
from sovyx.plugins.sdk import ISovyxPlugin
from sovyx.plugins.sdk import tool as tool_dec


def _perception(text: str = "test") -> MagicMock:
    p = MagicMock()
    p.source = "telegram"
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


class TestCalculatorConversation:
    """User asks 'what is 2+2' → LLM calls calculator → response."""

    @pytest.mark.anyio()
    async def test_calculator_flow(self, tmp_path: Path) -> None:
        mgr = PluginManager(data_dir=tmp_path)
        await mgr.load_single(CalculatorPlugin())
        executor = ToolExecutor(plugin_manager=mgr)

        mock_router = AsyncMock()
        mock_router.generate = AsyncMock(
            return_value=_response("2 + 2 = 4"),
        )

        act = ActPhase(tool_executor=executor, llm_router=mock_router)

        initial = _response(
            finish_reason="tool_use",
            tool_calls=[
                ToolCall(
                    id="tc1",
                    function_name="calculator.calculate",
                    arguments={"expression": "2 + 2"},
                ),
            ],
        )

        result = await act.process(
            initial,
            [{"role": "user", "content": "what is 2+2"}],
            _perception("what is 2+2"),
        )

        assert result.response_text == "2 + 2 = 4"
        assert len(result.tool_calls_made) == 1
        assert result.error is False


class TestToolChaining:
    """LLM chains 2 tools in sequence (ReAct depth 2)."""

    @pytest.mark.anyio()
    async def test_two_tool_calls_in_sequence(self, tmp_path: Path) -> None:
        mgr = PluginManager(data_dir=tmp_path)
        await mgr.load_single(CalculatorPlugin())
        executor = ToolExecutor(plugin_manager=mgr)

        call_count = 0

        async def mock_gen(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First re-invoke: call another tool
                return _response(
                    finish_reason="tool_use",
                    tool_calls=[
                        ToolCall(
                            id="tc2",
                            function_name="calculator.calculate",
                            arguments={"expression": "4 * 10"},
                        ),
                    ],
                )
            # Second re-invoke: final answer
            return _response("The final answer is 40.")

        mock_router = AsyncMock()
        mock_router.generate = AsyncMock(side_effect=mock_gen)

        act = ActPhase(tool_executor=executor, llm_router=mock_router)

        initial = _response(
            finish_reason="tool_use",
            tool_calls=[
                ToolCall(
                    id="tc1",
                    function_name="calculator.calculate",
                    arguments={"expression": "2 + 2"},
                ),
            ],
        )

        result = await act.process(
            initial,
            [{"role": "user", "content": "compute step by step"}],
            _perception("compute"),
        )

        assert result.response_text == "The final answer is 40."
        assert len(result.tool_calls_made) == 2
        assert call_count == 2


class TestDisabledPluginNoTools:
    """Disabled plugin → LLM has no tools → responds without tools."""

    @pytest.mark.anyio()
    async def test_no_tools_when_all_disabled(self, tmp_path: Path) -> None:
        class FailPlugin(ISovyxPlugin):
            @property
            def name(self) -> str:
                return "only-plugin"

            @property
            def version(self) -> str:
                return "1.0.0"

            @property
            def description(self) -> str:
                return "test"

            @tool_dec(description="fails")
            async def fail(self) -> str:
                msg = "boom"
                raise RuntimeError(msg)

        mgr = PluginManager(data_dir=tmp_path)
        await mgr.load_single(FailPlugin())

        # Verify tools exist
        assert len(mgr.get_tool_definitions()) == 1

        # Disable
        for _ in range(5):
            await mgr.execute("only-plugin.fail", {})

        # No tools
        assert len(mgr.get_tool_definitions()) == 0

        # ThinkPhase should pass tools=None
        assembler = AsyncMock()
        assembler.assemble = AsyncMock(
            return_value=AssembledContext(
                messages=[{"role": "user", "content": "test"}],
                tokens_used=10,
                token_budget=4096,
                sources=[],
                budget_breakdown={},
            ),
        )
        router = AsyncMock()
        router.generate = AsyncMock(
            return_value=_response("I can help with that."),
        )
        router.get_context_window = MagicMock(return_value=4096)

        think = ThinkPhase(
            context_assembler=assembler,
            llm_router=router,
            mind_config=MindConfig(name="test"),
            plugin_manager=mgr,
        )

        from sovyx.cognitive.perceive import Perception
        from sovyx.engine.types import MindId, PerceptionType

        perception = Perception(
            id="p1",
            type=PerceptionType.USER_MESSAGE,
            source="test",
            content="test",
            metadata={},
        )

        await think.process(perception, MindId("test"), [])

        call_kwargs = router.generate.call_args[1]
        assert call_kwargs.get("tools") is None


class TestThinkPassesToolsToLLM:
    """ThinkPhase passes plugin tools to router.generate()."""

    @pytest.mark.anyio()
    async def test_tools_in_generate_call(self, tmp_path: Path) -> None:
        mgr = PluginManager(data_dir=tmp_path)
        await mgr.load_single(CalculatorPlugin())

        assembler = AsyncMock()
        assembler.assemble = AsyncMock(
            return_value=AssembledContext(
                messages=[{"role": "user", "content": "2+2"}],
                tokens_used=10,
                token_budget=4096,
                sources=[],
                budget_breakdown={},
            ),
        )
        router = AsyncMock()
        router.generate = AsyncMock(
            return_value=_response("4"),
        )
        router.get_context_window = MagicMock(return_value=4096)

        think = ThinkPhase(
            context_assembler=assembler,
            llm_router=router,
            mind_config=MindConfig(name="test"),
            plugin_manager=mgr,
        )

        from sovyx.cognitive.perceive import Perception
        from sovyx.engine.types import MindId, PerceptionType

        perception = Perception(
            id="p1",
            type=PerceptionType.USER_MESSAGE,
            source="test",
            content="2+2",
            metadata={},
        )

        await think.process(perception, MindId("test"), [])

        call_kwargs = router.generate.call_args[1]
        assert call_kwargs["tools"] is not None
        assert len(call_kwargs["tools"]) == 1
        assert call_kwargs["tools"][0]["name"] == "calculator.calculate"
