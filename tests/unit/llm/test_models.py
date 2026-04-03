"""Tests for sovyx.llm.models — LLM response models."""

from __future__ import annotations

from sovyx.llm.models import LLMResponse, ToolCall, ToolResult


class TestLLMResponse:
    """LLMResponse dataclass."""

    def test_basic(self) -> None:
        r = LLMResponse(
            content="Hello",
            model="claude-sonnet-4-20250514",
            tokens_in=10,
            tokens_out=5,
            latency_ms=200,
            cost_usd=0.001,
            finish_reason="stop",
            provider="anthropic",
        )
        assert r.content == "Hello"
        assert r.tool_calls is None

    def test_with_tool_calls(self) -> None:
        tc = ToolCall(id="tc1", function_name="search", arguments={"q": "test"})
        r = LLMResponse(
            content="",
            model="gpt-4o",
            tokens_in=10,
            tokens_out=5,
            latency_ms=100,
            cost_usd=0.0,
            finish_reason="tool_use",
            provider="openai",
            tool_calls=[tc],
        )
        assert r.tool_calls is not None
        assert len(r.tool_calls) == 1


class TestToolResult:
    """ToolResult dataclass."""

    def test_basic(self) -> None:
        tr = ToolResult(call_id="tc1", name="search", output="found", success=True)
        assert tr.success is True
