# Sovyx Plugin Developer Guide

Build tools for AI Minds with persistent memory. A Sovyx plugin is a Python class with `@tool` methods — the LLM decides when to call them based on conversation context.

**What makes Sovyx plugins different:** your code can read and write to the Mind's brain. Search memories, store new knowledge, participate in the cognitive loop. No other AI platform offers this.

## Table of Contents

- [Quick Start (5 minutes)](#quick-start-5-minutes)
- [How Plugins Work](#how-plugins-work)
- [Writing a Plugin](#writing-a-plugin)
- [Brain Access](#brain-access)
- [The @tool Decorator](#the-tool-decorator)
- [Plugin Manifest](#plugin-manifest)
- [Permissions](#permissions)
- [Testing](#testing)
- [Configuration](#configuration)
- [Safety and Security](#safety-and-security)
- [Hot Reload](#hot-reload)
- [Distribution](#distribution)
- [CLI Reference](#cli-reference)
- [Official Plugins as Reference](#official-plugins-as-reference)
- [Patterns and Best Practices](#patterns-and-best-practices)

---

## Quick Start (5 minutes)

### Option A: Use the template repo

```bash
# Clone the template (GitHub "Use this template" also works)
git clone https://github.com/sovyx-ai/sovyx-plugin-template.git my-plugin
cd my-plugin
pip install -e ".[dev]"
pytest tests/ -v  # Should pass — 10 tests included
```

### Option B: Scaffold from CLI

```bash
sovyx plugin create my-plugin
cd my-plugin
```

### Option C: From scratch

```bash
pip install sovyx
```

Create `plugin.py`:

```python
from sovyx.plugins.sdk import ISovyxPlugin, tool

class MyPlugin(ISovyxPlugin):
    @property
    def name(self) -> str:
        return "my-plugin"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def description(self) -> str:
        return "What my plugin does."

    @tool(description="Say hello to someone.")
    async def greet(self, name: str) -> str:
        return f"Hello, {name}!"
```

Install and test:

```bash
sovyx plugin install .
sovyx plugin list  # Should show my-plugin
```

---

## How Plugins Work

```
User message → LLM → decides to call a tool → PluginManager dispatches
                                                        ↓
                                                  YourPlugin.your_tool()
                                                        ↓
                                                  ToolResult → LLM → response
```

The **ReAct loop** runs up to 3 iterations per turn:

1. User sends a message
2. LLM sees available tools (from all loaded plugins) and decides whether to call one
3. If it calls a tool, PluginManager finds the right plugin and invokes the method
4. The result is injected back into LLM context
5. LLM responds to the user (or calls another tool)

Your plugin does not handle routing, parsing, or intent detection. The LLM handles all of that. You write the tool, describe what it does, and the LLM figures out when to use it.

### Plugin Structure

```
my-plugin/
├── src/my_plugin/
│   ├── __init__.py          # Re-export your plugin class
│   └── plugin.py            # ISovyxPlugin subclass with @tool methods
├── tests/
│   └── test_plugin.py       # Tests using MockPluginContext
├── plugin.yaml              # Manifest (permissions, metadata)
├── pyproject.toml            # Build config + entry point
└── README.md
```

### Plugin Lifecycle

1. **Discovery** — Sovyx finds your plugin via pip entry points or directory scan
2. **Instantiation** — `__init__()` called (no arguments)
3. **Initialization** — `setup(ctx)` called with `PluginContext` (brain, events, HTTP, filesystem)
4. **Active** — tools available to the LLM via function calling
5. **Shutdown** — `teardown()` called (cleanup resources)

---

## Writing a Plugin

### The ISovyxPlugin Base Class

Every plugin extends `ISovyxPlugin` and implements three required properties:

```python
from sovyx.plugins.sdk import ISovyxPlugin, tool

class MyPlugin(ISovyxPlugin):
    @property
    def name(self) -> str:
        """Unique identifier. Lowercase, hyphens allowed. No spaces."""
        return "my-plugin"

    @property
    def version(self) -> str:
        """SemVer version string."""
        return "1.0.0"

    @property
    def description(self) -> str:
        """One-line description — shown in dashboard and marketplace."""
        return "What my plugin does."
```

### Optional Lifecycle Methods

```python
class MyPlugin(ISovyxPlugin):
    # ... required properties ...

    async def setup(self, ctx: object) -> None:
        """Called after loading. Initialize HTTP sessions, load data."""
        self._session = aiohttp.ClientSession()

    async def teardown(self) -> None:
        """Called before unloading. Release resources."""
        await self._session.close()
```

---

## Brain Access

This is Sovyx's differentiator. Your plugin can interact with the Mind's persistent memory.

### Reading Memories

```python
@tool(description="Find notes about a topic.")
async def find_notes(self, query: str) -> str:
    """Search the Mind's memory using spreading activation.

    Args:
        query: What to search for.
    """
    results = await self.brain.search(query, limit=5)
    if not results:
        return "No memories found."
    return "\n".join(
        f"- {r['name']}: {r['content']}" for r in results
    )
```

`brain.search()` uses spreading activation — it finds connections the Mind has made, not just keyword matches. A search for "morning routine" might surface memories about coffee preferences, gym schedule, and wake-up times if the Mind has linked them.

### Writing Memories

```python
@tool(description="Remember something important.")
async def save_note(self, title: str, content: str) -> str:
    """Store a new memory that persists across conversations.

    Args:
        title: Short name for this memory.
        content: The information to remember.
    """
    await self.brain.remember(title, content)
    return f"Remembered: {title}"
```

Memories written by your plugin:
- Persist across all conversations
- Get linked to related concepts automatically
- Can be recalled months later by the Mind or other plugins
- Are visible in the brain dashboard

### Permissions Required

Brain access requires explicit permissions in `plugin.yaml`:

```yaml
permissions:
  - brain:read     # For brain.search()
  - brain:write    # For brain.remember()
```

Without these, calls to `brain.search()` and `brain.remember()` raise `PermissionDenied` at runtime.

---

## The @tool Decorator

The `@tool` decorator exposes a method as an LLM-callable function.

### Basic Usage

```python
@tool(description="Convert Celsius to Fahrenheit.")
async def celsius_to_fahrenheit(self, celsius: float) -> str:
    fahrenheit = (celsius * 9/5) + 32
    return f"{celsius}°C = {fahrenheit}°F"
```

### Rules

- **Must be `async`** — all tool methods are awaited
- **Must return `str`** — the result is injected into LLM context as text
- **Parameters become JSON Schema** — type hints are auto-converted
- **Description is shown to the LLM** — write it clearly; the LLM uses it to decide when to call the tool

### Supported Type Hints

| Python Type | JSON Schema | Example |
|------------|-------------|---------|
| `str` | `"string"` | `city: str` |
| `int` | `"integer"` | `count: int` |
| `float` | `"number"` | `amount: float` |
| `bool` | `"boolean"` | `verbose: bool` |
| `list[str]` | `"array"` of `"string"` | `tags: list[str]` |
| `Optional[str]` | `"string"` (not required) | `note: str \| None = None` |
| `Literal["a", "b"]` | `"string"` with `enum` | `mode: Literal["get", "set"]` |

### Advanced Options

```python
@tool(
    description="Delete all user data.",
    requires_confirmation=True,  # User must approve before execution
    timeout_seconds=60,          # Override default 30s timeout
)
async def delete_data(self, confirm: bool) -> str:
    ...
```

### Multi-Mode Pattern

For plugins with many related operations, use a `mode` parameter instead of many separate tools. This is the pattern used by the Financial Math plugin (8 operations, 1 tool):

```python
@tool(description="Financial calculations: compound interest, TVM, IRR, etc.")
async def calculate(
    self,
    mode: Literal["compound", "tvm", "irr", "amortize", "percentage", "convert"],
    **kwargs,
) -> str:
    if mode == "compound":
        return self._compound(**kwargs)
    elif mode == "tvm":
        return self._tvm(**kwargs)
    # ...
```

This reduces tool clutter in the LLM's context and groups related functionality.

---

## Plugin Manifest

Every plugin has a `plugin.yaml` that declares metadata and permissions.

```yaml
name: my-plugin
version: 1.0.0
description: What my plugin does.
author: Your Name
license: MIT
min_sovyx_version: 0.7.0

permissions:
  - network:internet
  - brain:read

network:
  allowed_domains:
    - api.example.com
    - api.backup.com

tools:
  - name: my_tool
    description: What this tool does.
```

### Fields

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Must match `ISovyxPlugin.name` |
| `version` | Yes | SemVer — must match `ISovyxPlugin.version` |
| `description` | Yes | One-line description |
| `author` | No | Your name or organization |
| `license` | No | SPDX license identifier |
| `min_sovyx_version` | No | Minimum compatible Sovyx version |
| `permissions` | No | List of required permissions |
| `network.allowed_domains` | No | Whitelist for HTTP requests |
| `tools` | No | Tool metadata (for marketplace display) |

---

## Permissions

Plugins run in a sandbox. Every capability must be declared and approved.

| Permission | What it grants |
|-----------|----------------|
| `network:internet` | HTTP requests to `allowed_domains` only |
| `brain:read` | `brain.search()`, `brain.recall()` |
| `brain:write` | `brain.remember()`, `brain.forget()` |
| `fs:read` | Read files in the plugin's data directory |
| `fs:write` | Write files in the plugin's data directory |
| `events:emit` | Emit events on the EventBus |
| `events:subscribe` | Subscribe to engine events |

**Undeclared permissions are denied at runtime.** If your plugin calls `brain.search()` without `brain:read` in the manifest, it raises `PermissionDenied`.

---

## Testing

The SDK provides a complete mock harness. No running Sovyx engine needed.

### Basic Test

```python
import pytest
from sovyx_plugin_example.plugin import ExamplePlugin

@pytest.fixture()
def plugin() -> ExamplePlugin:
    return ExamplePlugin()

@pytest.mark.anyio()
async def test_greet(plugin: ExamplePlugin) -> None:
    result = await plugin.greet("World")
    assert result == "Hello, World!"
```

### Testing Brain Access

```python
from sovyx.plugins.testing import MockPluginContext

@pytest.mark.anyio()
async def test_find_notes() -> None:
    ctx = MockPluginContext("my-plugin")
    ctx.brain.seed([
        {"name": "meeting-notes", "content": "Discussed Q2 roadmap"},
        {"name": "coffee-pref", "content": "Oat milk, no sugar"},
    ])

    plugin = MyPlugin(brain=ctx.brain)
    result = await plugin.find_notes("meeting")

    assert "Q2 roadmap" in result
    ctx.brain.assert_searched("meeting")
```

### Available Mocks

| Mock | What it simulates |
|------|-------------------|
| `MockBrainAccess` | `brain.search()`, `brain.remember()` — seed data, track calls |
| `MockEventBus` | `events.emit()`, `events.subscribe()` — track emitted events |
| `MockHttpClient` | Pre-configure HTTP responses for testing API calls |
| `MockFsAccess` | In-memory filesystem for `fs:read`/`fs:write` |
| `MockPluginContext` | All of the above, bundled |

### Tool Discovery Test

Every plugin should verify its tools are discoverable:

```python
def test_tools_discovered(plugin: ExamplePlugin) -> None:
    tools = plugin.get_tools()
    names = [t.name for t in tools]
    assert "my-plugin.greet" in names

def test_all_tools_have_descriptions(plugin: ExamplePlugin) -> None:
    for t in plugin.get_tools():
        assert len(t.description) > 0, f"{t.name} missing description"
```

### Running Tests

```bash
pytest tests/ -v                    # Run all tests
sovyx plugin validate .             # Run quality gates (lint + type + test)
```

---

## Configuration

Users configure plugins in their `mind.yaml`:

```yaml
plugins:
  plugins_config:
    my-plugin:
      enabled: true
      config:
        api_key: "abc123"
        timeout: 30
      permissions:
        - network:internet
```

Access config values in your plugin using `config_schema`:

```python
class MyPlugin(ISovyxPlugin):
    config_schema = {
        "required": ["api_key"],
        "properties": {
            "api_key": {"type": "string"},
            "timeout": {"type": "integer", "default": 30},
        },
    }
```

Users who don't provide required config values see a validation error on startup.

---

## Safety and Security

### Error Boundary

Plugin crashes never crash the engine. Every tool call runs inside:

```python
async with asyncio.timeout(tool.timeout_seconds):
    result = await tool.handler(**params)
```

If your plugin raises an exception, the engine catches it and returns an error result to the LLM. The LLM decides how to respond ("Sorry, that tool failed.").

### Auto-Disable

If a plugin fails **5 consecutive times**, it's automatically disabled:

- `PluginAutoDisabled` event emitted
- Tool removed from LLM context
- Re-enable via `sovyx plugin enable <name>` or the dashboard

### Security Scanner

Before loading, Sovyx scans your plugin's AST for dangerous patterns:

| Blocked Pattern | Why |
|----------------|-----|
| `eval()` | Arbitrary code execution |
| `exec()` | Arbitrary code execution |
| `__import__()` | Import guard bypass |
| `subprocess.*` | Shell command execution |
| `os.system()` | Shell command execution |

The scanner runs automatically. If your plugin uses any of these, it won't load.

### Network Sandbox

HTTP requests are restricted to domains declared in `plugin.yaml`:

```yaml
network:
  allowed_domains:
    - api.example.com
```

Requests to any other domain raise `NetworkAccessDenied`.

---

## Hot Reload

During development, plugins auto-reload when you save a file:

```bash
# Start Sovyx with hot-reload enabled
sovyx start --dev
```

Under the hood, a file watcher monitors your plugin directory:
- File change detected → debounce (avoid rapid reloads)
- Old plugin unloaded → module cache cleared → new version loaded
- 3 retries on failure before giving up
- Requires `pip install watchdog`

---

## Distribution

### Via pip (recommended)

Add an entry point to `pyproject.toml`:

```toml
[project.entry-points."sovyx.plugins"]
my-plugin = "my_plugin:MyPlugin"
```

Then:

```bash
pip install sovyx-plugin-my-plugin
# Auto-discovered on next 'sovyx start'
```

### Via git

```bash
sovyx plugin install git+https://github.com/you/sovyx-plugin-example.git
```

### Local development

```bash
sovyx plugin install ./my-plugin
```

---

## CLI Reference

```
sovyx plugin list              List installed plugins
sovyx plugin info <name>       Show plugin details (tools, permissions, status)
sovyx plugin install <source>  Install from local path, pip, or git URL
sovyx plugin create <name>     Scaffold a new plugin project
sovyx plugin validate <dir>    Run quality gates (ruff + mypy + pytest)
sovyx plugin enable <name>     Enable a disabled plugin
sovyx plugin disable <name>    Disable a plugin (tools removed from LLM)
sovyx plugin remove <name>     Uninstall a plugin
```

---

## Official Plugins as Reference

Three enterprise-grade official plugins, ordered by complexity:

### 1. Knowledge (core — 922 LOC, 5 tools)

**File:** `src/sovyx/plugins/official/knowledge.py`

The brain interface plugin. Shows:
- Full `brain:read` + `brain:write` usage
- Semantic deduplication
- Conflict resolution
- Auto-relations between concepts
- Confidence tracking

### 2. Web Intelligence (advanced — 1,962 LOC, 6 tools)

**File:** `src/sovyx/plugins/official/web_intelligence.py`

Web search, news, page extraction. Shows:
- Intent classification (query → search mode)
- Multiple external API integrations
- Result ranking and filtering
- Graceful degradation when services are unavailable

### 3. Financial Math (enterprise — 2,019 LOC, 8 tools)

**File:** `src/sovyx/plugins/official/financial_math.py`

The SDK showcase. Demonstrates every best practice:
- **Multi-mode pattern** — one `@tool` with `mode` parameter instead of 8 separate tools
- **Decimal-first** — all math via `Decimal(str(value))`, banker's rounding
- **Structured JSON output** — consistent `{ok, action, mode, result, message}` schema
- **Input validation** — `_require()`, `_validate_value()`, bounds checking
- **Safety limits** — max periods, max cashflows, overflow protection
- **Zero dependencies** — Newton-Raphson for IRR, pure Decimal math
- **228 tests** — unit + Hypothesis property-based invariant testing

See the [Financial Math Plugin API Reference](./financial-math-plugin.md) for full documentation.

---

## Patterns and Best Practices

### Return structured data

The LLM processes your tool's return value. Structured output helps it extract information:

```python
# Good — structured, parseable
return json.dumps({"ok": True, "temperature": 22, "unit": "celsius", "city": city})

# Also good — human-readable for simple results
return f"Weather in {city}: 22°C, partly cloudy"

# Bad — ambiguous
return str(data)
```

### Validate inputs early

```python
@tool(description="Transfer funds between accounts.")
async def transfer(self, from_account: str, to_account: str, amount: float) -> str:
    if amount <= 0:
        return json.dumps({"ok": False, "error": "Amount must be positive"})
    if amount > 1_000_000:
        return json.dumps({"ok": False, "error": "Amount exceeds limit"})
    # ... proceed with transfer
```

### Handle errors gracefully

Never let exceptions escape without a useful message:

```python
@tool(description="Fetch stock price.")
async def stock_price(self, symbol: str) -> str:
    try:
        price = await self._fetch_price(symbol)
        return json.dumps({"ok": True, "symbol": symbol, "price": price})
    except aiohttp.ClientError:
        return json.dumps({"ok": False, "error": f"Failed to reach API for {symbol}"})
```

### Write tool descriptions for the LLM

The description is what the LLM reads to decide when to call your tool. Be specific:

```python
# Good — the LLM knows exactly when to use this
@tool(description="Convert an amount from one currency to another using live exchange rates.")

# Bad — too vague, LLM might call it for unrelated queries
@tool(description="Do currency stuff.")
```

### Test tool discovery

Your tests should verify that tools are visible and properly configured:

```python
def test_all_tools_have_parameters(plugin):
    for t in plugin.get_tools():
        assert "properties" in t.parameters
        assert t.parameters.get("type") == "object"
```

---

## Resources

- [Template Repo](https://github.com/sovyx-ai/sovyx-plugin-template) — clone and start building
- [ISovyxPlugin source](https://github.com/sovyx-ai/sovyx/blob/main/src/sovyx/plugins/sdk.py) — the contract
- [Testing Harness source](https://github.com/sovyx-ai/sovyx/blob/main/src/sovyx/plugins/testing.py) — mock internals
- [Official Plugins](https://github.com/sovyx-ai/sovyx/tree/main/src/sovyx/plugins/official) — reference implementations
- [sovyx.ai/developers](https://sovyx.ai/developers) — overview and showcase

[![Built for Sovyx](https://sovyx.ai/badge.svg)](https://sovyx.ai/developers)
