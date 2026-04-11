"""VAL-001: Tool name sanitization roundtrip — all providers + edge cases."""

from __future__ import annotations

import re

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from sovyx.llm.providers._shared import (
    _sanitize_tool_name,
    _unsanitize_tool_name,
    format_tools_anthropic,
    format_tools_google,
    format_tools_openai,
    parse_tool_calls_anthropic,
    parse_tool_calls_google,
    parse_tool_calls_openai,
)

OPENAI_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")

# Valid plugin names: [a-z][a-z0-9-]*, no --, no leading/trailing -
PLUGIN_NAME = st.from_regex(r"[a-z][a-z0-9]{0,10}", fullmatch=True)
# Valid tool names: Python identifiers (no hyphens)
TOOL_NAME = st.from_regex(r"[a-z][a-z0-9_]{0,15}", fullmatch=True)


class TestSanitizeRoundtrip:
    """Sanitize → unsanitize must always recover the original name."""

    @pytest.mark.parametrize(
        "name",
        [
            "calculator.calculate",
            "my-plugin.my_tool",
            "my_plugin.do_thing",
            "plugin2.tool3",
            "weather.get_weather",
            "weather.will_it_rain",
            "a.b",
        ],
    )
    def test_roundtrip_known_names(self, name: str) -> None:
        sanitized = _sanitize_tool_name(name)
        restored = _unsanitize_tool_name(sanitized)
        assert restored == name

    @pytest.mark.parametrize(
        "name",
        [
            "calc__v2.add",
            "my___plugin.tool",
            "plugin_with_underscores.my_tool_name",
        ],
    )
    def test_roundtrip_names_with_underscores(self, name: str) -> None:
        """Names containing underscores must survive roundtrip."""
        sanitized = _sanitize_tool_name(name)
        restored = _unsanitize_tool_name(sanitized)
        assert restored == name

    @given(plugin=PLUGIN_NAME, tool=TOOL_NAME)
    @settings(max_examples=200)
    def test_roundtrip_fuzz(self, plugin: str, tool: str) -> None:
        """Fuzz: any valid plugin.tool name must roundtrip correctly."""
        name = f"{plugin}.{tool}"
        sanitized = _sanitize_tool_name(name)
        restored = _unsanitize_tool_name(sanitized)
        assert restored == name, f"Roundtrip failed: {name} → {sanitized} → {restored}"


class TestOpenAICompliance:
    """Sanitized names must match OpenAI's ^[a-zA-Z0-9_-]+$ pattern."""

    @given(plugin=PLUGIN_NAME, tool=TOOL_NAME)
    @settings(max_examples=200)
    def test_pattern_compliance_fuzz(self, plugin: str, tool: str) -> None:
        name = f"{plugin}.{tool}"
        sanitized = _sanitize_tool_name(name)
        assert OPENAI_PATTERN.match(sanitized), f"'{sanitized}' does not match OpenAI pattern"

    def test_no_dots_in_sanitized(self) -> None:
        name = "weather.get_weather"
        sanitized = _sanitize_tool_name(name)
        assert "." not in sanitized


class TestProviderRoundtrip:
    """Full format → parse roundtrip for each provider."""

    TOOL = {
        "name": "weather.get_weather",
        "description": "Get weather",
        "parameters": {"type": "object"},
    }

    def test_openai_roundtrip(self) -> None:
        formatted = format_tools_openai([self.TOOL])
        fn_name = formatted[0]["function"]["name"]
        # Simulate LLM returning same name
        llm_response = [
            {"id": "tc1", "function": {"name": fn_name, "arguments": '{"city": "SP"}'}}
        ]
        parsed = parse_tool_calls_openai(llm_response)
        assert parsed[0]["function_name"] == "weather.get_weather"

    def test_anthropic_roundtrip(self) -> None:
        formatted = format_tools_anthropic([self.TOOL])
        fn_name = formatted[0]["name"]
        llm_response = [
            {"type": "tool_use", "id": "tc1", "name": fn_name, "input": {"city": "SP"}}
        ]
        parsed = parse_tool_calls_anthropic(llm_response)
        assert parsed[0]["function_name"] == "weather.get_weather"

    def test_google_roundtrip(self) -> None:
        formatted = format_tools_google([self.TOOL])
        fn_name = formatted[0]["functionDeclarations"][0]["name"]
        llm_response = [{"functionCall": {"name": fn_name, "args": {"city": "SP"}}}]
        parsed = parse_tool_calls_google(llm_response)
        assert parsed[0]["function_name"] == "weather.get_weather"

    def test_ollama_uses_openai_format(self) -> None:
        """Ollama uses same format as OpenAI."""
        formatted = format_tools_openai([self.TOOL])
        assert formatted[0]["type"] == "function"
        fn_name = formatted[0]["function"]["name"]
        assert "." not in fn_name


class TestEdgeCases:
    """Edge cases that could break the system."""

    def test_empty_name(self) -> None:
        assert _sanitize_tool_name("") == ""
        assert _unsanitize_tool_name("") == ""

    def test_no_dot(self) -> None:
        """Name without dot passes through unchanged."""
        assert _sanitize_tool_name("nodot") == "nodot"
        assert _unsanitize_tool_name("nodot") == "nodot"

    def test_only_sanitizes_first_dot(self) -> None:
        """If somehow a name has multiple dots, only first is replaced."""
        # This shouldn't happen (plugin.tool has exactly 1 dot)
        # but defensive handling is important
        sanitized = _sanitize_tool_name("a.b.c")
        assert sanitized == "a--b.c"
        restored = _unsanitize_tool_name(sanitized)
        assert restored == "a.b.c"
