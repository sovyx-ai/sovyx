"""Tests for LLM provider tool calling (TASK-436).

Tests shared formatters, parsers, and per-provider tool integration.
"""

from __future__ import annotations

from typing import Any

import pytest

from sovyx.llm.models import ToolCall
from sovyx.llm.providers._shared import (
    format_tools_anthropic,
    format_tools_google,
    format_tools_openai,
    parse_tool_calls_anthropic,
    parse_tool_calls_google,
    parse_tool_calls_openai,
)

# ── Sample Tool Definition ──────────────────────────────────────────

SAMPLE_TOOLS: list[dict[str, Any]] = [
    {
        "name": "weather.get_weather",
        "description": "Get weather for a city",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
    {
        "name": "timer.set_timer",
        "description": "Set a timer",
        "parameters": {
            "type": "object",
            "properties": {"seconds": {"type": "integer"}},
        },
    },
]


# ── Format Tests ────────────────────────────────────────────────────


class TestFormatToolsOpenAI:
    """Tests for OpenAI/Ollama tool format conversion."""

    def test_basic(self) -> None:
        result = format_tools_openai(SAMPLE_TOOLS)
        assert len(result) == 2
        assert result[0]["type"] == "function"
        assert result[0]["function"]["name"] == "weather.get_weather"
        assert result[0]["function"]["description"] == "Get weather for a city"
        assert result[0]["function"]["parameters"]["required"] == ["city"]

    def test_empty(self) -> None:
        assert format_tools_openai([]) == []

    def test_minimal_tool(self) -> None:
        result = format_tools_openai([{"name": "x", "description": "y"}])
        assert result[0]["function"]["parameters"] == {}


class TestFormatToolsAnthropic:
    """Tests for Anthropic tool format conversion."""

    def test_basic(self) -> None:
        result = format_tools_anthropic(SAMPLE_TOOLS)
        assert len(result) == 2
        assert result[0]["name"] == "weather.get_weather"
        assert result[0]["description"] == "Get weather for a city"
        assert result[0]["input_schema"]["type"] == "object"
        # No "type": "function" wrapper
        assert "type" not in result[0] or result[0].get("type") != "function"

    def test_empty(self) -> None:
        assert format_tools_anthropic([]) == []


class TestFormatToolsGoogle:
    """Tests for Google Gemini tool format conversion."""

    def test_basic(self) -> None:
        result = format_tools_google(SAMPLE_TOOLS)
        assert len(result) == 1  # Wrapped in single object
        declarations = result[0]["functionDeclarations"]
        assert len(declarations) == 2
        assert declarations[0]["name"] == "weather.get_weather"

    def test_empty(self) -> None:
        result = format_tools_google([])
        assert result == [{"functionDeclarations": []}]


# ── Parse Tests ─────────────────────────────────────────────────────


class TestParseToolCallsOpenAI:
    """Tests for OpenAI tool_calls parsing."""

    def test_single_call(self) -> None:
        raw = [
            {
                "id": "call_123",
                "function": {
                    "name": "weather.get_weather",
                    "arguments": '{"city": "Berlin"}',
                },
            }
        ]
        result = parse_tool_calls_openai(raw)
        assert len(result) == 1
        assert result[0]["id"] == "call_123"
        assert result[0]["function_name"] == "weather.get_weather"
        assert result[0]["arguments"] == {"city": "Berlin"}

    def test_multiple_calls(self) -> None:
        raw = [
            {"id": "c1", "function": {"name": "a", "arguments": "{}"}},
            {"id": "c2", "function": {"name": "b", "arguments": '{"x": 1}'}},
        ]
        result = parse_tool_calls_openai(raw)
        assert len(result) == 2

    def test_invalid_json_arguments(self) -> None:
        raw = [{"id": "c1", "function": {"name": "a", "arguments": "not json"}}]
        result = parse_tool_calls_openai(raw)
        assert result[0]["arguments"] == {}

    def test_dict_arguments(self) -> None:
        """Arguments already parsed as dict (Ollama sometimes does this)."""
        raw = [{"id": "c1", "function": {"name": "a", "arguments": {"key": "val"}}}]
        result = parse_tool_calls_openai(raw)
        assert result[0]["arguments"] == {"key": "val"}

    def test_empty(self) -> None:
        assert parse_tool_calls_openai([]) == []


class TestParseToolCallsAnthropic:
    """Tests for Anthropic tool_use block parsing."""

    def test_single_tool_use(self) -> None:
        blocks = [
            {"type": "text", "text": "Let me check the weather."},
            {
                "type": "tool_use",
                "id": "toolu_01",
                "name": "weather.get_weather",
                "input": {"city": "Tokyo"},
            },
        ]
        result = parse_tool_calls_anthropic(blocks)
        assert len(result) == 1
        assert result[0]["id"] == "toolu_01"
        assert result[0]["function_name"] == "weather.get_weather"
        assert result[0]["arguments"] == {"city": "Tokyo"}

    def test_multiple_tool_uses(self) -> None:
        blocks = [
            {"type": "tool_use", "id": "t1", "name": "a", "input": {}},
            {"type": "tool_use", "id": "t2", "name": "b", "input": {"x": 1}},
        ]
        result = parse_tool_calls_anthropic(blocks)
        assert len(result) == 2

    def test_no_tool_use(self) -> None:
        blocks = [{"type": "text", "text": "Just text."}]
        result = parse_tool_calls_anthropic(blocks)
        assert result == []

    def test_empty(self) -> None:
        assert parse_tool_calls_anthropic([]) == []


class TestParseToolCallsGoogle:
    """Tests for Google Gemini functionCall parsing."""

    def test_single_call(self) -> None:
        parts = [
            {"functionCall": {"name": "weather.get_weather", "args": {"city": "Paris"}}}
        ]
        result = parse_tool_calls_google(parts)
        assert len(result) == 1
        assert result[0]["id"] == "gemini-0"
        assert result[0]["function_name"] == "weather.get_weather"
        assert result[0]["arguments"] == {"city": "Paris"}

    def test_mixed_parts(self) -> None:
        parts = [
            {"text": "Let me check."},
            {"functionCall": {"name": "a", "args": {}}},
        ]
        result = parse_tool_calls_google(parts)
        assert len(result) == 1
        assert result[0]["id"] == "gemini-1"

    def test_no_function_calls(self) -> None:
        parts = [{"text": "Hello"}]
        assert parse_tool_calls_google(parts) == []

    def test_empty(self) -> None:
        assert parse_tool_calls_google([]) == []


# ── Provider Integration Tests ──────────────────────────────────────


class TestAnthropicToolCalling:
    """Integration tests for Anthropic provider with tools."""

    @pytest.mark.anyio()
    async def test_tool_use_response(self) -> None:
        """Simulate Anthropic returning tool_use blocks."""
        import httpx
        from unittest.mock import AsyncMock, patch
        import json

        from sovyx.llm.providers.anthropic import AnthropicProvider

        provider = AnthropicProvider(api_key="test-key")

        mock_response = httpx.Response(
            200,
            json={
                "content": [
                    {"type": "text", "text": "Checking weather."},
                    {
                        "type": "tool_use",
                        "id": "toolu_01",
                        "name": "weather.get_weather",
                        "input": {"city": "Berlin"},
                    },
                ],
                "model": "claude-sonnet-4-20250514",
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 100, "output_tokens": 50},
            },
            request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
        )

        with patch.object(provider._client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response
            result = await provider.generate(
                messages=[{"role": "user", "content": "weather in Berlin"}],
                tools=SAMPLE_TOOLS,
            )

        assert result.tool_calls is not None
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].function_name == "weather.get_weather"
        assert result.tool_calls[0].arguments == {"city": "Berlin"}
        assert result.finish_reason == "tool_use"
        assert result.content == "Checking weather."

    @pytest.mark.anyio()
    async def test_tools_in_payload(self) -> None:
        """Verify tools are formatted and sent in the API payload."""
        import httpx
        from unittest.mock import AsyncMock, patch

        from sovyx.llm.providers.anthropic import AnthropicProvider

        provider = AnthropicProvider(api_key="test-key")

        mock_response = httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "hi"}],
                "model": "claude-sonnet-4-20250514",
                "stop_reason": "stop",
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
            request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
        )

        with patch.object(provider._client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response
            await provider.generate(
                messages=[{"role": "user", "content": "hi"}],
                tools=SAMPLE_TOOLS,
            )

        payload = mock_post.call_args[1]["json"]
        assert "tools" in payload
        assert payload["tools"][0]["input_schema"]["type"] == "object"

    @pytest.mark.anyio()
    async def test_no_tools_no_payload_key(self) -> None:
        """Without tools, no tools key in payload."""
        import httpx
        from unittest.mock import AsyncMock, patch

        from sovyx.llm.providers.anthropic import AnthropicProvider

        provider = AnthropicProvider(api_key="test-key")

        mock_response = httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "hi"}],
                "model": "claude-sonnet-4-20250514",
                "stop_reason": "stop",
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
            request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
        )

        with patch.object(provider._client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response
            await provider.generate(
                messages=[{"role": "user", "content": "hi"}],
            )

        payload = mock_post.call_args[1]["json"]
        assert "tools" not in payload


class TestOpenAIToolCalling:
    """Integration tests for OpenAI provider with tools."""

    @pytest.mark.anyio()
    async def test_tool_calls_response(self) -> None:
        import httpx
        from unittest.mock import AsyncMock, patch

        from sovyx.llm.providers.openai import OpenAIProvider

        provider = OpenAIProvider(api_key="test-key")

        mock_response = httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_abc",
                                    "type": "function",
                                    "function": {
                                        "name": "weather.get_weather",
                                        "arguments": '{"city": "NYC"}',
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "model": "gpt-4o",
                "usage": {"prompt_tokens": 80, "completion_tokens": 20},
            },
            request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions"),
        )

        with patch.object(provider._client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response
            result = await provider.generate(
                messages=[{"role": "user", "content": "weather NYC"}],
                tools=SAMPLE_TOOLS,
            )

        assert result.tool_calls is not None
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].id == "call_abc"
        assert result.tool_calls[0].arguments == {"city": "NYC"}

    @pytest.mark.anyio()
    async def test_tools_in_payload(self) -> None:
        import httpx
        from unittest.mock import AsyncMock, patch

        from sovyx.llm.providers.openai import OpenAIProvider

        provider = OpenAIProvider(api_key="test-key")

        mock_response = httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "model": "gpt-4o",
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
            request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions"),
        )

        with patch.object(provider._client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response
            await provider.generate(
                messages=[{"role": "user", "content": "hi"}],
                tools=SAMPLE_TOOLS,
            )

        payload = mock_post.call_args[1]["json"]
        assert "tools" in payload
        assert payload["tools"][0]["type"] == "function"


class TestGoogleToolCalling:
    """Integration tests for Google provider with tools."""

    @pytest.mark.anyio()
    async def test_function_call_response(self) -> None:
        import httpx
        from unittest.mock import AsyncMock, patch

        from sovyx.llm.providers.google import GoogleProvider

        provider = GoogleProvider(api_key="test-key")

        mock_response = httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "functionCall": {
                                        "name": "weather.get_weather",
                                        "args": {"city": "London"},
                                    }
                                }
                            ],
                        },
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {"promptTokenCount": 50, "candidatesTokenCount": 10},
            },
            request=httpx.Request("POST", "https://generativelanguage.googleapis.com/test"),
        )

        with patch.object(provider._client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response
            result = await provider.generate(
                messages=[{"role": "user", "content": "weather London"}],
                tools=SAMPLE_TOOLS,
            )

        assert result.tool_calls is not None
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].function_name == "weather.get_weather"
        assert result.tool_calls[0].arguments == {"city": "London"}

    @pytest.mark.anyio()
    async def test_tools_in_payload(self) -> None:
        import httpx
        from unittest.mock import AsyncMock, patch

        from sovyx.llm.providers.google import GoogleProvider

        provider = GoogleProvider(api_key="test-key")

        mock_response = httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {"parts": [{"text": "ok"}]},
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5},
            },
            request=httpx.Request("POST", "https://generativelanguage.googleapis.com/test"),
        )

        with patch.object(provider._client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response
            await provider.generate(
                messages=[{"role": "user", "content": "hi"}],
                tools=SAMPLE_TOOLS,
            )

        payload = mock_post.call_args[1]["json"]
        assert "tools" in payload
        assert "functionDeclarations" in payload["tools"][0]


class TestOllamaToolCalling:
    """Integration tests for Ollama provider with tools."""

    @pytest.mark.anyio()
    async def test_tool_calls_response(self) -> None:
        import httpx
        from unittest.mock import AsyncMock, patch

        from sovyx.llm.providers.ollama import OllamaProvider

        provider = OllamaProvider()

        mock_response = httpx.Response(
            200,
            json={
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "oll_1",
                            "function": {
                                "name": "weather.get_weather",
                                "arguments": {"city": "SP"},
                            },
                        }
                    ],
                },
                "model": "llama3.2:1b",
                "prompt_eval_count": 30,
                "eval_count": 10,
            },
            request=httpx.Request("POST", "http://localhost:11434/api/chat"),
        )

        with patch.object(provider._client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response
            result = await provider.generate(
                messages=[{"role": "user", "content": "weather SP"}],
                tools=SAMPLE_TOOLS,
            )

        assert result.tool_calls is not None
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].arguments == {"city": "SP"}
        assert result.finish_reason == "tool_calls"
