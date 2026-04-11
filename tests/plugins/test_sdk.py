"""Tests for Sovyx Plugin SDK — ISovyxPlugin, @tool, ToolDefinition.

Coverage target: ≥95% on plugins/sdk.py
"""

from __future__ import annotations

import enum
from typing import Literal, Optional

import pytest

from sovyx.plugins.sdk import (
    _TOOL_ATTR,
    ISovyxPlugin,
    ToolDefinition,
    _build_tool_definition,
    _extract_param_doc,
    _generate_schema_from_hints,
    _hint_to_json_schema,
    tool,
)

# ── Fixtures ────────────────────────────────────────────────────────


class MinimalPlugin(ISovyxPlugin):
    """Minimal valid plugin for testing."""

    @property
    def name(self) -> str:
        return "test-plugin"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def description(self) -> str:
        return "A test plugin."


class PluginWithTools(ISovyxPlugin):
    """Plugin with @tool-decorated methods."""

    @property
    def name(self) -> str:
        return "calc"

    @property
    def version(self) -> str:
        return "0.1.0"

    @property
    def description(self) -> str:
        return "Calculator plugin."

    @tool(description="Add two numbers")
    async def add(self, a: int, b: int) -> str:
        """Add two numbers.

        Args:
            a: First number.
            b: Second number.
        """
        return str(a + b)

    @tool(description="Multiply numbers", requires_confirmation=True, timeout_seconds=10)
    async def multiply(self, x: float, y: float) -> str:
        return str(x * y)

    @tool(description="Echo text with default")
    async def echo(self, text: str, uppercase: bool = False) -> str:
        return text.upper() if uppercase else text

    def not_a_tool(self) -> str:
        """This is NOT a tool."""
        return "nope"


class Color(enum.Enum):
    """Test enum."""

    RED = "red"
    GREEN = "green"
    BLUE = "blue"


# ── ISovyxPlugin ABC ───────────────────────────────────────────────


class TestISovyxPlugin:
    """Tests for ISovyxPlugin abstract base class."""

    def test_cannot_instantiate_abstract(self) -> None:
        """ISovyxPlugin cannot be instantiated directly."""
        with pytest.raises(TypeError, match="abstract method"):
            ISovyxPlugin()  # type: ignore[abstract]

    def test_minimal_plugin_instantiates(self) -> None:
        """Plugin with required properties can be created."""
        p = MinimalPlugin()
        assert p.name == "test-plugin"
        assert p.version == "1.0.0"
        assert p.description == "A test plugin."

    def test_default_permissions_empty(self) -> None:
        """Default permissions is empty list."""
        p = MinimalPlugin()
        assert p.permissions == []

    @pytest.mark.anyio()
    async def test_default_setup_noop(self) -> None:
        """Default setup() does nothing (no error)."""
        p = MinimalPlugin()
        await p.setup(None)

    @pytest.mark.anyio()
    async def test_default_teardown_noop(self) -> None:
        """Default teardown() does nothing (no error)."""
        p = MinimalPlugin()
        await p.teardown()

    def test_get_tools_empty_for_minimal(self) -> None:
        """MinimalPlugin has no tools."""
        p = MinimalPlugin()
        assert p.get_tools() == []

    def test_get_tools_discovers_decorated(self) -> None:
        """get_tools() finds all @tool-decorated methods."""
        p = PluginWithTools()
        tools = p.get_tools()
        names = sorted(t.name for t in tools)
        assert names == ["calc.add", "calc.echo", "calc.multiply"]

    def test_get_tools_excludes_non_decorated(self) -> None:
        """get_tools() skips methods without @tool."""
        p = PluginWithTools()
        tools = p.get_tools()
        tool_names = [t.name for t in tools]
        assert "calc.not_a_tool" not in tool_names


# ── @tool Decorator ─────────────────────────────────────────────────


class TestToolDecorator:
    """Tests for @tool decorator."""

    def test_attaches_meta(self) -> None:
        """@tool attaches _sovyx_tool_meta to the function."""

        @tool(description="test tool")
        async def my_tool(self, x: int) -> str:
            return str(x)

        meta = getattr(my_tool, _TOOL_ATTR)
        assert meta.description == "test tool"
        assert meta.requires_confirmation is False
        assert meta.timeout_seconds == 30
        assert meta.parameters is None

    def test_custom_options(self) -> None:
        """@tool with custom parameters."""

        @tool(
            description="danger",
            requires_confirmation=True,
            timeout_seconds=5,
        )
        async def dangerous(self) -> str:
            return ""

        meta = getattr(dangerous, _TOOL_ATTR)
        assert meta.requires_confirmation is True
        assert meta.timeout_seconds == 5

    def test_explicit_parameters(self) -> None:
        """@tool with explicit JSON Schema parameters."""
        schema = {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        }

        @tool(description="search", parameters=schema)
        async def search(self, query: str) -> str:
            return query

        meta = getattr(search, _TOOL_ATTR)
        assert meta.parameters == schema

    def test_preserves_function(self) -> None:
        """Decorated function is still callable."""

        @tool(description="test")
        async def func(self, x: int) -> str:
            return str(x)

        # The function itself is preserved
        assert callable(func)


# ── ToolDefinition ──────────────────────────────────────────────────


class TestToolDefinition:
    """Tests for ToolDefinition dataclass."""

    def test_to_openai_schema(self) -> None:
        """Converts to OpenAI function calling format."""
        td = ToolDefinition(
            name="weather.get_weather",
            description="Get current weather",
            parameters={
                "type": "object",
                "properties": {"location": {"type": "string"}},
                "required": ["location"],
            },
        )
        schema = td.to_openai_schema()
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "weather.get_weather"
        assert schema["function"]["description"] == "Get current weather"
        assert schema["function"]["parameters"]["required"] == ["location"]

    def test_to_anthropic_schema(self) -> None:
        """Converts to Anthropic tool use format."""
        td = ToolDefinition(
            name="calc.add",
            description="Add numbers",
            parameters={"type": "object", "properties": {}},
        )
        schema = td.to_anthropic_schema()
        assert schema["name"] == "calc.add"
        assert schema["description"] == "Add numbers"
        assert "input_schema" in schema

    def test_frozen(self) -> None:
        """ToolDefinition is immutable."""
        td = ToolDefinition(
            name="test",
            description="test",
            parameters={},
        )
        with pytest.raises(AttributeError):
            td.name = "changed"  # type: ignore[misc]

    def test_default_values(self) -> None:
        """Defaults: no confirmation, 30s timeout, no handler."""
        td = ToolDefinition(name="t", description="d", parameters={})
        assert td.requires_confirmation is False
        assert td.timeout_seconds == 30
        assert td.handler is None


# ── Schema Generation ───────────────────────────────────────────────


class TestSchemaGeneration:
    """Tests for _generate_schema_from_hints and _hint_to_json_schema."""

    def test_primitive_str(self) -> None:
        assert _hint_to_json_schema(str) == {"type": "string"}

    def test_primitive_int(self) -> None:
        assert _hint_to_json_schema(int) == {"type": "integer"}

    def test_primitive_float(self) -> None:
        assert _hint_to_json_schema(float) == {"type": "number"}

    def test_primitive_bool(self) -> None:
        assert _hint_to_json_schema(bool) == {"type": "boolean"}

    def test_list_str(self) -> None:
        schema = _hint_to_json_schema(list[str])
        assert schema == {"type": "array", "items": {"type": "string"}}

    def test_list_unparameterized(self) -> None:
        schema = _hint_to_json_schema(list)
        assert schema == {"type": "array"}

    def test_optional_str(self) -> None:
        schema = _hint_to_json_schema(Optional[str])
        assert schema == {"type": "string"}

    def test_literal_strings(self) -> None:
        schema = _hint_to_json_schema(Literal["metric", "imperial"])
        assert schema == {"type": "string", "enum": ["metric", "imperial"]}

    def test_literal_ints(self) -> None:
        schema = _hint_to_json_schema(Literal[1, 2, 3])
        assert schema == {"type": "integer", "enum": [1, 2, 3]}

    def test_enum(self) -> None:
        schema = _hint_to_json_schema(Color)
        assert schema == {"type": "string", "enum": ["red", "green", "blue"]}

    def test_dict(self) -> None:
        schema = _hint_to_json_schema(dict[str, int])
        assert schema == {"type": "object"}

    def test_unknown_fallback(self) -> None:
        """Unknown types fallback to string."""
        schema = _hint_to_json_schema(bytes)
        assert schema == {"type": "string"}

    def test_generate_from_function(self) -> None:
        """Full schema generation from a typed function."""

        async def my_func(self, query: str, limit: int = 5, verbose: bool = False) -> str:
            """Search for something.

            Args:
                query: Search query text.
                limit: Max results.
            """
            return ""

        schema = _generate_schema_from_hints(my_func)
        assert schema["type"] == "object"
        assert "query" in schema["properties"]
        assert schema["properties"]["query"]["type"] == "string"
        assert schema["properties"]["query"]["description"] == "Search query text."
        assert schema["properties"]["limit"]["type"] == "integer"
        assert schema["properties"]["limit"]["default"] == 5
        assert schema["properties"]["verbose"]["type"] == "boolean"
        assert schema["required"] == ["query"]

    def test_skips_self(self) -> None:
        """'self' parameter is excluded from schema."""

        async def method(self, x: int) -> str:
            return ""

        schema = _generate_schema_from_hints(method)
        assert "self" not in schema["properties"]

    def test_no_hints_fallback(self) -> None:
        """Function without type hints produces string fallback."""

        def bare(self, x):
            return x

        schema = _generate_schema_from_hints(bare)
        assert schema["properties"]["x"]["type"] == "string"


# ── Docstring Extraction ────────────────────────────────────────────


class TestDocstringExtraction:
    """Tests for _extract_param_doc."""

    def test_google_style(self) -> None:
        def func(self, name: str) -> str:
            """Do something.

            Args:
                name: The user's name.
            """
            return ""

        assert _extract_param_doc(func, "name") == "The user's name."

    def test_no_docstring(self) -> None:
        def func(self, x: int) -> str:
            return ""

        assert _extract_param_doc(func, "x") == ""

    def test_param_not_found(self) -> None:
        def func(self, x: int) -> str:
            """Something.

            Args:
                x: The value.
            """
            return ""

        assert _extract_param_doc(func, "missing") == ""

    def test_typed_param_style(self) -> None:
        def func(self, count: int) -> str:
            """Do it.

            Args:
                count (int): Number of items.
            """
            return ""

        assert _extract_param_doc(func, "count") == "Number of items."


# ── _build_tool_definition ──────────────────────────────────────────


class TestBuildToolDefinition:
    """Tests for _build_tool_definition."""

    def test_builds_with_auto_schema(self) -> None:
        @tool(description="Add two numbers")
        async def add(self, a: int, b: int) -> str:
            return str(a + b)

        meta = getattr(add, _TOOL_ATTR)
        td = _build_tool_definition("calc", "add", add, meta)

        assert td.name == "calc.add"
        assert td.description == "Add two numbers"
        assert td.parameters["properties"]["a"]["type"] == "integer"
        assert td.parameters["properties"]["b"]["type"] == "integer"
        assert td.parameters["required"] == ["a", "b"]
        assert td.handler is add

    def test_builds_with_explicit_schema(self) -> None:
        schema = {"type": "object", "properties": {"q": {"type": "string"}}}

        @tool(description="Search", parameters=schema)
        async def search(self, q: str) -> str:
            return q

        meta = getattr(search, _TOOL_ATTR)
        td = _build_tool_definition("finder", "search", search, meta)

        assert td.parameters == schema

    def test_confirmation_and_timeout(self) -> None:
        @tool(description="Danger", requires_confirmation=True, timeout_seconds=5)
        async def danger(self) -> str:
            return ""

        meta = getattr(danger, _TOOL_ATTR)
        td = _build_tool_definition("safe", "danger", danger, meta)

        assert td.requires_confirmation is True
        assert td.timeout_seconds == 5


# ── Integration: Full Plugin Lifecycle ──────────────────────────────


class TestPluginIntegration:
    """Integration test: plugin instantiation → get_tools → schema output."""

    def test_full_lifecycle(self) -> None:
        """Create plugin, get tools, convert to OpenAI format."""
        p = PluginWithTools()

        # Get tools
        tools = p.get_tools()
        assert len(tools) == 3

        # Find add tool
        add_tool = next(t for t in tools if t.name == "calc.add")
        assert add_tool.description == "Add two numbers"
        assert add_tool.parameters["required"] == ["a", "b"]

        # Convert to OpenAI
        openai = add_tool.to_openai_schema()
        assert openai["type"] == "function"
        assert openai["function"]["name"] == "calc.add"

        # Convert to Anthropic
        anthropic = add_tool.to_anthropic_schema()
        assert anthropic["name"] == "calc.add"
        assert "input_schema" in anthropic

    def test_multiply_has_confirmation(self) -> None:
        """Multiply tool requires confirmation."""
        p = PluginWithTools()
        tools = p.get_tools()
        mul = next(t for t in tools if t.name == "calc.multiply")
        assert mul.requires_confirmation is True
        assert mul.timeout_seconds == 10

    def test_echo_has_default(self) -> None:
        """Echo tool has optional parameter with default."""
        p = PluginWithTools()
        tools = p.get_tools()
        echo = next(t for t in tools if t.name == "calc.echo")
        assert "uppercase" in echo.parameters["properties"]
        assert echo.parameters["properties"]["uppercase"]["default"] is False
        assert "uppercase" not in echo.parameters["required"]

    def test_docstring_params_extracted(self) -> None:
        """Parameter descriptions from docstring are in schema."""
        p = PluginWithTools()
        tools = p.get_tools()
        add_tool = next(t for t in tools if t.name == "calc.add")
        assert add_tool.parameters["properties"]["a"]["description"] == "First number."
        assert add_tool.parameters["properties"]["b"]["description"] == "Second number."

    @pytest.mark.anyio()
    async def test_tool_handlers_callable(self) -> None:
        """Tool handlers are bound methods that can be called."""
        p = PluginWithTools()
        tools = p.get_tools()
        add_tool = next(t for t in tools if t.name == "calc.add")
        assert add_tool.handler is not None
        result = await add_tool.handler(a=2, b=3)
        assert result == "5"

    def test_union_type_schema(self) -> None:
        """Union types generate anyOf schema."""
        from typing import Union

        schema = _hint_to_json_schema(Union[str, int])
        assert "anyOf" in schema
        assert len(schema["anyOf"]) == 2

    def test_literal_mixed(self) -> None:
        """Literal with mixed types generates plain enum."""
        schema = _hint_to_json_schema(Literal["a", 1])
        assert "enum" in schema
        assert schema["enum"] == ["a", 1]

    def test_get_tools_handles_getattr_error(self) -> None:
        """get_tools() gracefully handles attributes that raise on access."""

        class BadPlugin(ISovyxPlugin):
            @property
            def name(self) -> str:
                return "bad"

            @property
            def version(self) -> str:
                return "0.1.0"

            @property
            def description(self) -> str:
                return "Bad plugin."

            @property
            def broken_attr(self) -> str:
                raise RuntimeError("broken")

        p = BadPlugin()
        # Should not raise — getattr errors are caught
        tools = p.get_tools()
        assert isinstance(tools, list)

    def test_optional_int_schema(self) -> None:
        """Optional[int] produces integer schema."""
        schema = _hint_to_json_schema(Optional[int])
        assert schema == {"type": "integer"}

    def test_list_int_schema(self) -> None:
        """list[int] produces array of integer."""
        schema = _hint_to_json_schema(list[int])
        assert schema == {"type": "array", "items": {"type": "integer"}}

    def test_bare_dict_schema(self) -> None:
        """Bare dict (no type args) produces object schema."""
        schema = _hint_to_json_schema(dict)
        assert schema == {"type": "object"}

    def test_dict_str_int_schema(self) -> None:
        """dict[str, int] produces object schema."""
        schema = _hint_to_json_schema(dict[str, int])
        assert schema == {"type": "object"}

    def test_generate_schema_handles_bad_hints(self) -> None:
        """Schema generation handles functions with unparseable hints."""

        def func(self, x: NonExistentType) -> str:  # type: ignore[name-defined]  # noqa: F821
            return ""

        # Should not crash — falls back
        schema = _generate_schema_from_hints(func)
        assert "properties" in schema
