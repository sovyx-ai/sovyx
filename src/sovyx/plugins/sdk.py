"""Sovyx Plugin SDK — ISovyxPlugin ABC, @tool decorator, ToolDefinition.

This module defines the CONTRACT that every Sovyx plugin implements.
A plugin is a collection of tools that the Mind can invoke via LLM
function calling. No intent parsing — the LLM decides when to call tools.

Spec: SPE-008 §2 (Plugin Interface), §2.2 (@tool decorator)
"""

from __future__ import annotations

import dataclasses
import enum
import inspect
import typing
from abc import ABC, abstractmethod
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints

from sovyx.observability.logging import get_logger

logger = get_logger(__name__)

if typing.TYPE_CHECKING:
    from collections.abc import Callable

    from sovyx.plugins.permissions import Permission


# ── Tool Definition ─────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class ToolDefinition:
    """Schema for an LLM-callable tool.

    Used by Context Assembly (SPE-006) to build the tools= parameter
    for LLM providers. The LLM sees name + description + parameters
    and decides when to call the tool.

    Attributes:
        name: Fully-qualified tool name (e.g., "weather.get_weather").
              Set by PluginManager after loading; @tool sets the method name.
        description: What this tool does — shown to the LLM.
        parameters: JSON Schema for function arguments.
        requires_confirmation: If True, user must approve before execution.
        timeout_seconds: Max execution time before kill.
        handler: Bound method reference (set at runtime).
    """

    name: str
    description: str
    parameters: dict[str, Any]
    requires_confirmation: bool = False
    timeout_seconds: int = 30
    handler: Callable[..., Any] | None = dataclasses.field(default=None, repr=False, compare=False)

    def to_openai_schema(self) -> dict[str, Any]:
        """Convert to OpenAI function calling format.

        Returns:
            Dict matching OpenAI's tool schema:
            {"type": "function", "function": {"name", "description", "parameters"}}
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def to_anthropic_schema(self) -> dict[str, Any]:
        """Convert to Anthropic tool use format.

        Returns:
            Dict matching Anthropic's tool schema:
            {"name", "description", "input_schema"}
        """
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
        }


# ── @tool Decorator ─────────────────────────────────────────────────


_TOOL_ATTR = "_sovyx_tool_meta"


@dataclasses.dataclass(frozen=True)
class _ToolMeta:
    """Internal metadata attached by @tool decorator.

    Stored on the function object. PluginManager reads this to build
    ToolDefinition instances at load time.
    """

    description: str
    parameters: dict[str, Any] | None
    requires_confirmation: bool
    timeout_seconds: int


def tool(
    description: str,
    *,
    parameters: dict[str, Any] | None = None,
    requires_confirmation: bool = False,
    timeout_seconds: int = 30,
) -> Callable[..., Any]:
    """Decorator to expose a plugin method as an LLM-callable tool.

    Usage::

        class MyPlugin(ISovyxPlugin):
            @tool(description="Calculate a math expression")
            async def calculate(self, expression: str) -> str:
                ...

    The decorator auto-generates JSON Schema from type hints if
    ``parameters`` is not provided. Supports: str, int, float, bool,
    list[T], Optional[T], Literal["a", "b"], Enum types.

    Args:
        description: What this tool does (shown to LLM in system prompt).
        parameters: Explicit JSON Schema override. Auto-generated if None.
        requires_confirmation: If True, user must confirm before execution.
        timeout_seconds: Max execution time (default 30s).

    Returns:
        Decorated function with ``_sovyx_tool_meta`` attribute.
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        meta = _ToolMeta(
            description=description,
            parameters=parameters,
            requires_confirmation=requires_confirmation,
            timeout_seconds=timeout_seconds,
        )
        setattr(func, _TOOL_ATTR, meta)
        return func

    return decorator


def _build_tool_definition(
    plugin_name: str,
    method_name: str,
    method: Callable[..., Any],
    meta: _ToolMeta,
) -> ToolDefinition:
    """Build a ToolDefinition from a decorated method.

    Called by ISovyxPlugin.get_tools() for each @tool-decorated method.
    Auto-generates JSON Schema from type hints if parameters not explicit.

    Args:
        plugin_name: Plugin name for namespacing.
        method_name: Method name (becomes tool name).
        method: The bound/unbound method.
        meta: Metadata from @tool decorator.

    Returns:
        Complete ToolDefinition ready for LLM.
    """
    if meta.parameters is not None:
        params = meta.parameters
    else:
        params = _generate_schema_from_hints(method)

    return ToolDefinition(
        name=f"{plugin_name}.{method_name}",
        description=meta.description,
        parameters=params,
        requires_confirmation=meta.requires_confirmation,
        timeout_seconds=meta.timeout_seconds,
        handler=method,
    )


def _generate_schema_from_hints(func: Callable[..., Any]) -> dict[str, Any]:
    """Generate JSON Schema from Python type hints.

    Supports: str, int, float, bool, list[T], Optional[T],
    Literal["a", "b"], Enum types, Union types.

    Skips 'self' and 'return' from the schema.

    Args:
        func: Function with type annotations.

    Returns:
        JSON Schema dict with "type", "properties", "required".
    """
    try:
        hints = get_type_hints(func, include_extras=True)
    except (NameError, TypeError, AttributeError):
        # NameError: unresolved forward reference (e.g. `"SomeClass"`
        # pointing at a missing import). TypeError: malformed annotation
        # at runtime. AttributeError: lookup into a namespace that no
        # longer has the referenced name. All three mean the plugin
        # author has a broken annotation — log it so the mistake is
        # visible rather than silently degrading to an empty schema.
        logger.debug(
            "plugin_tool_type_hints_unresolvable",
            func=getattr(func, "__qualname__", repr(func)),
            exc_info=True,
        )
        hints = {}

    sig = inspect.signature(func)
    properties: dict[str, Any] = {}
    required: list[str] = []

    for param_name, param in sig.parameters.items():
        if param_name == "self":
            continue

        hint = hints.get(param_name, str)
        prop = _hint_to_json_schema(hint)

        # Extract description from docstring if available
        doc_desc = _extract_param_doc(func, param_name)
        if doc_desc:
            prop["description"] = doc_desc

        if param.default is inspect.Parameter.empty:
            required.append(param_name)
        elif param.default is not None:
            prop["default"] = param.default

        properties[param_name] = prop

    return {
        "type": "object",
        "properties": properties,
        "required": required,
    }


def _hint_to_json_schema(hint: object) -> dict[str, Any]:
    """Convert a Python type hint to JSON Schema.

    Args:
        hint: A Python type annotation.

    Returns:
        JSON Schema property dict.
    """
    origin = get_origin(hint)
    args = get_args(hint)

    # Optional[X] → Union[X, None]
    if origin is Union:
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            # Optional[X]
            schema = _hint_to_json_schema(non_none[0])
            return schema
        # Union[X, Y] → anyOf (rare in tool params)
        return {"anyOf": [_hint_to_json_schema(a) for a in non_none]}

    # Literal["a", "b"]
    if origin is Literal:
        values = list(args)
        if all(isinstance(v, str) for v in values):
            return {"type": "string", "enum": values}
        if all(isinstance(v, int) for v in values):
            return {"type": "integer", "enum": values}
        return {"enum": values}

    # list[X]
    if origin is list:
        if args:
            return {"type": "array", "items": _hint_to_json_schema(args[0])}
        return {"type": "array"}

    # dict[str, X]
    if origin is dict:
        return {"type": "object"}

    # Enum subclass
    if isinstance(hint, type) and issubclass(hint, enum.Enum):
        values = [e.value for e in hint]
        return {"type": "string", "enum": values}

    # Bare container types (no type args)
    if hint is list:
        return {"type": "array"}
    if hint is dict:
        return {"type": "object"}

    # Primitives
    type_map: dict[type, str] = {
        str: "string",
        int: "integer",
        float: "number",
        bool: "boolean",
    }

    if isinstance(hint, type) and hint in type_map:
        return {"type": type_map[hint]}

    # Fallback
    return {"type": "string"}


def _extract_param_doc(func: Callable[..., Any], param_name: str) -> str:
    """Extract parameter description from function docstring.

    Supports Google-style and numpy-style docstrings:
        Args:
            param_name: Description here.

    Args:
        func: Function with docstring.
        param_name: Parameter to extract description for.

    Returns:
        Description string, or empty string if not found.
    """
    doc = inspect.getdoc(func)
    if not doc:
        return ""

    # Simple Google-style: "param_name: Description"
    for line in doc.split("\n"):
        stripped = line.strip()
        if stripped.startswith(f"{param_name}:"):
            return stripped[len(param_name) + 1 :].strip()
        # Also match "param_name (type): Description"
        if stripped.startswith(f"{param_name} ("):
            idx = stripped.find("): ")
            if idx != -1:
                return stripped[idx + 3 :].strip()

    return ""


# ── ISovyxPlugin ABC ───────────────────────────────────────────────


class ISovyxPlugin(ABC):
    """Abstract base class for all Sovyx plugins.

    A plugin is a collection of tools that the Mind can use via LLM
    function calling. Implement this class to create a plugin.

    Lifecycle:
        1. Plugin discovered (pip entry_points / directory scan)
        2. Plugin instantiated (``__init__`` called — no args)
        3. Plugin initialized (``setup(ctx)`` called with PluginContext)
        4. Plugin active (tools available to CogLoop via LLM)
        5. Plugin shutdown (``teardown()`` called)

    Minimal example::

        class WeatherPlugin(ISovyxPlugin):
            @property
            def name(self) -> str:
                return "weather"

            @property
            def version(self) -> str:
                return "1.0.0"

            @property
            def description(self) -> str:
                return "Get current weather and forecasts."

            @tool(description="Get current weather for a location")
            async def get_weather(self, location: str) -> str:
                ...

    Spec: SPE-008 §2.1
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique plugin identifier (lowercase, hyphens, no spaces).

        Convention: ``sovyx-plugin-{name}`` for pip packages.
        The name is used for namespacing tools (``{name}.{tool_name}``).
        """
        ...

    @property
    @abstractmethod
    def version(self) -> str:
        """SemVer version string (e.g., ``"1.0.0"``)."""
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        """One-line description for marketplace and dashboard."""
        ...

    @property
    def permissions(self) -> list[Permission]:
        """Permissions this plugin requires.

        Permissions are DECLARED in the plugin and APPROVED by the user.
        Only declared permissions are available via PluginContext.

        Override to declare permissions::

            @property
            def permissions(self) -> list[Permission]:
                return [Permission.BRAIN_READ, Permission.NETWORK_INTERNET]

        Returns:
            List of required permissions. Empty = no special permissions.
        """
        return []

    async def setup(self, ctx: object) -> None:  # noqa: B027
        """Called after plugin is loaded. Initialize resources here.

        The ``ctx`` argument is a ``PluginContext`` providing sandboxed
        access to brain, event bus, HTTP client, filesystem, and config.
        Only services matching declared permissions are available.

        Not abstract — override only if your plugin needs initialization.

        Args:
            ctx: PluginContext instance.
        """

    async def teardown(self) -> None:  # noqa: B027
        """Called before plugin is unloaded. Cleanup resources.

        Release HTTP sessions, cancel background tasks, flush data.
        Called even if ``setup()`` raised an exception.

        Not abstract — override only if your plugin needs cleanup.
        """

    def get_tools(self) -> list[ToolDefinition]:
        """Return all tools this plugin provides.

        Default implementation discovers methods decorated with ``@tool``.
        Override for dynamic tool registration.

        Returns:
            List of ToolDefinition instances.
        """
        tools: list[ToolDefinition] = []
        for attr_name in dir(self):
            try:
                method = getattr(self, attr_name)
            except Exception:  # noqa: BLE001  # nosec B112
                continue
            meta = getattr(method, _TOOL_ATTR, None)
            if meta is not None:
                td = _build_tool_definition(
                    plugin_name=self.name,
                    method_name=attr_name,
                    method=method,
                    meta=meta,
                )
                tools.append(td)
        return tools
