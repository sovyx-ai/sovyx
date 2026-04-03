"""Tests for sovyx.cognitive.act — ActPhase + ToolExecutor."""

from __future__ import annotations

from unittest.mock import AsyncMock

from sovyx.cognitive.act import ActionResult, ActPhase, ToolExecutor
from sovyx.cognitive.perceive import Perception
from sovyx.engine.types import PerceptionType
from sovyx.llm.models import LLMResponse, ToolCall


def _perception(content: str = "Hello") -> Perception:
    return Perception(
        id="p1",
        type=PerceptionType.USER_MESSAGE,
        source="telegram",
        content=content,
        metadata={"reply_to": "msg123"},
    )


def _response(
    content: str = "Hi!",
    finish_reason: str = "stop",
    tool_calls: list[ToolCall] | None = None,
) -> LLMResponse:
    return LLMResponse(
        content=content,
        model="test",
        tokens_in=10,
        tokens_out=5,
        latency_ms=100,
        cost_usd=0.001,
        finish_reason=finish_reason,
        provider="test",
        tool_calls=tool_calls,
    )


class TestToolExecutor:
    """ToolExecutor v0.1."""

    async def test_no_tools_returns_errors(self) -> None:
        executor = ToolExecutor()
        calls = [ToolCall(id="tc1", function_name="search", arguments={"q": "test"})]
        results = await executor.execute(calls)
        assert len(results) == 1
        assert results[0].success is False
        assert "no tools" in results[0].output

    async def test_empty_calls(self) -> None:
        executor = ToolExecutor()
        results = await executor.execute([])
        assert results == []

    def test_register_tool(self) -> None:
        executor = ToolExecutor()
        executor.register_tool("test", lambda: None)
        assert "test" in executor._tools


class TestActPhase:
    """ActPhase processing."""

    async def test_text_response(self) -> None:
        phase = ActPhase(ToolExecutor(), AsyncMock())
        result = await phase.process(_response(), [], _perception())
        assert isinstance(result, ActionResult)
        assert result.response_text == "Hi!"
        assert result.target_channel == "telegram"

    async def test_reply_to_from_metadata(self) -> None:
        phase = ActPhase(ToolExecutor(), AsyncMock())
        result = await phase.process(_response(), [], _perception())
        assert result.reply_to == "msg123"

    async def test_degraded_response(self) -> None:
        phase = ActPhase(ToolExecutor(), AsyncMock())
        result = await phase.process(
            _response(content="Degraded", finish_reason="error"), [], _perception()
        )
        assert result.degraded is True

    async def test_tool_calls_handled(self) -> None:
        phase = ActPhase(ToolExecutor(), AsyncMock())
        calls = [ToolCall(id="tc1", function_name="search", arguments={})]
        result = await phase.process(_response(tool_calls=calls), [], _perception())
        assert len(result.tool_calls_made) == 1

    async def test_no_reply_to_if_missing(self) -> None:
        p = Perception(
            id="p1",
            type=PerceptionType.USER_MESSAGE,
            source="cli",
            content="Hi",
        )
        phase = ActPhase(ToolExecutor(), AsyncMock())
        result = await phase.process(_response(), [], p)
        assert result.reply_to is None
