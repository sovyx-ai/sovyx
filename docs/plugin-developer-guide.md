# Sovyx Plugin Developer Guide

Build tools for AI Minds with persistent memory. A Sovyx plugin is a Python class with `@tool` methods — the LLM decides when to call them based on conversation context.

**What makes Sovyx plugins different:** your code can read and write to the Mind's brain. Search memories, store new knowledge, participate in the cognitive loop. No other AI platform offers this.

> **New here?** Jump to [Quick Start](#quick-start-5-minutes) and have your first plugin running in under 5 minutes. Then come back and read the rest.

---

## Table of Contents

- [Quick Start (5 minutes)](#quick-start-5-minutes)
- [How Plugins Work](#how-plugins-work)
- [Architecture Overview](#architecture-overview)
- [Writing a Plugin](#writing-a-plugin)
- [Brain Access — The Differentiator](#brain-access--the-differentiator)
- [The @tool Decorator](#the-tool-decorator)
- [Plugin Manifest (plugin.yaml)](#plugin-manifest-pluginyaml)
- [Permissions](#permissions)
- [Testing](#testing)
- [Configuration](#configuration)
- [Safety and Security](#safety-and-security)
- [Hot Reload](#hot-reload)
- [Distribution](#distribution)
- [Marketplace](#marketplace)
- [CLI Reference](#cli-reference)
- [Official Plugins as Reference](#official-plugins-as-reference)
- [Patterns and Best Practices](#patterns-and-best-practices)
- [Troubleshooting](#troubleshooting)
- [FAQ](#faq)
- [Resources](#resources)

---

## Quick Start (5 minutes)

Three ways to start. Pick one.

### Option A: Use the template repo (recommended)

```bash
# Clone the starter template — comes with tests, CI, and best practices
git clone https://github.com/sovyx-ai/sovyx-plugin-template.git my-plugin
cd my-plugin
pip install -e ".[dev]"
pytest tests/ -v  # 10 tests pass out of the box
```

### Option B: Scaffold from CLI

```bash
pip install sovyx
sovyx plugin create my-plugin
cd my-plugin
```

This generates:
```
my-plugin/
├── src/my_plugin/
│   ├── __init__.py          # Re-exports your plugin class
│   └── plugin.py            # ISovyxPlugin subclass with @tool methods
├── tests/
│   └── test_plugin.py       # Tests using MockPluginContext
├── plugin.yaml              # Manifest (permissions, metadata)
├── pyproject.toml           # Build config + entry point
└── README.md
```

### Option C: From scratch (minimal)

```bash
pip install sovyx
```

Create `plugin.py`:

```python
from sovyx.plugins.sdk import ISovyxPlugin, tool


class MyPlugin(ISovyxPlugin):
    """A minimal Sovyx plugin."""

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
        """Greet a person by name.

        Args:
            name: The person's name.
        """
        return f"Hello, {name}!"
```

Install and verify:

```bash
sovyx plugin install .
sovyx plugin list          # Shows: my-plugin v1.0.0 (1 tool)
sovyx plugin info my-plugin  # Shows: greet — "Say hello to someone."
```

### Verify it works

Start Sovyx and ask it to greet someone:

```
You: Say hi to Alice
Mind: Hello, Alice!  (via my-plugin.greet)
```

The LLM saw your tool's description, decided it was relevant, called `greet(name="Alice")`, and returned the result. You didn't write any routing logic.

---

## How Plugins Work

### The ReAct Loop

```
User message
    ↓
LLM sees available tools (from all loaded plugins)
    ↓
LLM decides: call a tool? or respond directly?
    ↓                              ↓
Call tool                    Respond to user
    ↓
PluginManager dispatches → YourPlugin.your_tool(**params)
    ↓
ToolResult injected back into LLM context
    ↓
LLM responds (or calls another tool — up to 3 iterations per turn)
```

**Key insight:** your plugin does not handle routing, parsing, or intent detection. The LLM handles all of that. You write the tool, describe what it does, and the LLM figures out when to use it. The description is everything — write it like you're explaining the tool to a smart colleague.

### Plugin Lifecycle

| Stage | What happens | Your code |
|-------|-------------|-----------|
| **1. Discovery** | Sovyx finds your plugin via pip `entry_points` or directory scan | Nothing — automatic |
| **2. Instantiation** | `__init__()` called (no arguments) | Keep it lightweight |
| **3. Initialization** | `setup(ctx)` called with `PluginContext` (brain, events, HTTP, filesystem) | Open connections, load data |
| **4. Active** | Tools available to the LLM via function calling | Handle tool calls |
| **5. Shutdown** | `teardown()` called on engine stop or plugin reload | Close connections, flush buffers |

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────┐
│                      Your Plugin                         │
│                                                          │
│  @tool methods  ←→  ISovyxPlugin ABC  ←→  PluginContext │
└──────────────────────────┬──────────────────────────────┘
                           │
              ┌────────────▼────────────┐
              │    Plugin SDK Layer      │
              │  Permissions · Sandbox   │
              │  Timeout · Error Boundary│
              └────────────┬────────────┘
                           │
         ┌─────────────────┼─────────────────┐
         ▼                 ▼                  ▼
   ┌──────────┐    ┌──────────────┐   ┌──────────┐
   │  Brain    │    │  EventBus    │   │  HTTP    │
   │ Engine    │    │  (pub/sub)   │   │ Client   │
   │           │    │              │   │ (sandboxed)│
   │ search()  │    │ emit()       │   │          │
   │ remember()│    │ subscribe()  │   │ Domain   │
   │ recall()  │    │              │   │ whitelist│
   │ forget()  │    │              │   │          │
   └──────────┘    └──────────────┘   └──────────┘
        │
        ▼
   ┌──────────────────────────┐
   │     Memory Graph          │
   │  Spreading Activation     │
   │  Concept Linking          │
   │  Semantic Search          │
   │  Persistent Storage       │
   └──────────────────────────┘
```

Every arrow goes through the permission layer. If your `plugin.yaml` doesn't declare `brain:read`, the SDK raises `PermissionDenied` before the call reaches the brain.

---

## Writing a Plugin

### The ISovyxPlugin Base Class

Every plugin extends `ISovyxPlugin` and implements three required properties:

```python
from sovyx.plugins.sdk import ISovyxPlugin, tool


class MyPlugin(ISovyxPlugin):
    @property
    def name(self) -> str:
        """Unique identifier. Lowercase, hyphens allowed. No spaces.

        Convention: match your package name.
        Examples: "weather", "habit-tracker", "financial-math"
        """
        return "my-plugin"

    @property
    def version(self) -> str:
        """SemVer version string. Must match plugin.yaml."""
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
        """Called once after loading. Use this to initialize resources.

        The ctx object provides:
          - ctx.brain — BrainAccess (if brain:read or brain:write permitted)
          - ctx.events — EventBus (if events:emit or events:subscribe permitted)
          - ctx.http — sandboxed HTTP client (if network:internet permitted)
          - ctx.fs — filesystem access (if fs:read or fs:write permitted)
        """
        self._session = aiohttp.ClientSession()
        self._cache: dict[str, str] = {}

    async def teardown(self) -> None:
        """Called before unloading. Release all resources.

        This runs on engine shutdown, plugin disable, or hot-reload.
        If you open it in setup(), close it in teardown().
        """
        await self._session.close()
```

### Complete Plugin Example

Here's a real-world plugin that tracks habits using the Mind's brain:

```python
"""Habit Tracker — track daily habits with streak counting.

Uses brain:read to find existing habit entries and brain:write
to store new ones. The Mind remembers your habits across all
conversations, forever.
"""

from sovyx.plugins.sdk import ISovyxPlugin, tool


class HabitTracker(ISovyxPlugin):
    @property
    def name(self) -> str:
        return "habit-tracker"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def description(self) -> str:
        return "Track daily habits with streak counting and memory."

    @tool(description="Log a habit completion for today. Tracks streaks automatically.")
    async def log_habit(self, habit: str) -> str:
        """Log that you completed a habit today.

        Args:
            habit: The habit name (e.g., "exercise", "read", "meditate").
        """
        # Search the Mind's memories for this habit
        existing = await self.brain.search(f"habit:{habit}", limit=100)
        streak = len(existing) + 1

        # Write a new memory — persists forever
        await self.brain.remember(
            f"habit:{habit}:day-{streak}",
            f"Completed {habit} — day {streak}",
        )

        return f"✓ {habit} — streak: {streak} days"

    @tool(description="Check your current streak for a habit.")
    async def check_streak(self, habit: str) -> str:
        """Look up how many days in a row you've done a habit.

        Args:
            habit: The habit name to check.
        """
        entries = await self.brain.search(f"habit:{habit}", limit=1000)
        if not entries:
            return f"No records found for '{habit}'. Start today!"
        return f"{habit}: {len(entries)} day streak"

    @tool(description="List all tracked habits and their streaks.")
    async def list_habits(self) -> str:
        """Show all habits the Mind has tracked."""
        all_habits = await self.brain.search("habit:", limit=1000)
        if not all_habits:
            return "No habits tracked yet."

        # Group by habit name
        habits: dict[str, int] = {}
        for entry in all_habits:
            name = str(entry.get("name", ""))
            # Extract habit name from "habit:exercise:day-5"
            parts = name.split(":")
            if len(parts) >= 2:
                habit_name = parts[1]
                habits[habit_name] = habits.get(habit_name, 0) + 1

        lines = [f"- {h}: {count}d streak" for h, count in sorted(habits.items())]
        return "Your habits:\n" + "\n".join(lines)
```

---

## Brain Access — The Differentiator

This is what separates Sovyx from every other AI platform. Your plugin has direct access to the Mind's persistent memory — a knowledge graph with spreading activation, semantic linking, and concept relationships.

### Reading Memories

```python
@tool(description="Find notes about a topic.")
async def find_notes(self, query: str) -> str:
    """Search the Mind's memory using spreading activation.

    Args:
        query: What to search for. Can be a keyword, phrase, or concept.
    """
    results = await self.brain.search(query, limit=5)
    if not results:
        return "No memories found."
    return "\n".join(
        f"- {r['name']}: {r['content']}" for r in results
    )
```

**How `brain.search()` works:**

`brain.search()` doesn't just match keywords. It uses **spreading activation** — a model from cognitive science where activating one concept "spreads" energy to related concepts. When you search for "morning routine", the brain might surface:

- "coffee-preference" (linked through "morning")
- "gym-schedule" (linked through "routine")
- "wake-up-time" (directly related)
- "melatonin-dose" (linked through "sleep → morning")

This means your plugin gets contextually relevant results, not just string matches.

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `query` | `str` | required | Search query — concepts, keywords, or natural language |
| `limit` | `int` | `5` | Maximum results to return |

**Returns:** `list[dict]` — each dict has `name`, `content`, `category`, `confidence`, `metadata`.

### Writing Memories

```python
@tool(description="Remember something important.")
async def save_note(self, title: str, content: str) -> str:
    """Store a new memory that persists across all conversations.

    Args:
        title: Short identifier for this memory (used in search).
        content: The information to remember.
    """
    await self.brain.remember(title, content)
    return f"Remembered: {title}"
```

**What happens when you call `brain.remember()`:**

1. The memory is stored permanently in the knowledge graph
2. The brain automatically finds related existing concepts and creates links
3. The memory becomes searchable by all plugins and the Mind itself
4. It appears in the brain dashboard for the user to see
5. It participates in spreading activation — future searches can discover it through related concepts

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `name` | `str` | required | Short identifier (e.g., "meeting-notes-apr-12") |
| `content` | `str` | required | The information to store |
| `category` | `str` | `"fact"` | Category: `"fact"`, `"preference"`, `"event"`, `"skill"` |
| `metadata` | `dict` | `None` | Optional structured data (JSON-serializable) |

### Other Brain Operations

```python
# Recall a specific memory by name
memory = await self.brain.recall("meeting-notes-apr-12")

# Forget a memory (soft-delete — can be recovered)
await self.brain.forget("outdated-info")
```

### Permissions Required

Brain access requires explicit permissions in `plugin.yaml`:

```yaml
permissions:
  - brain:read     # For brain.search() and brain.recall()
  - brain:write    # For brain.remember() and brain.forget()
```

Without these, calls raise `PermissionDenied` at runtime. This is intentional — the user controls which plugins can read or modify their Mind.

---

## The @tool Decorator

The `@tool` decorator is the core of the plugin SDK. It transforms a method into an LLM-callable function.

### Basic Usage

```python
@tool(description="Convert Celsius to Fahrenheit.")
async def celsius_to_fahrenheit(self, celsius: float) -> str:
    """Convert a temperature from Celsius to Fahrenheit.

    Args:
        celsius: Temperature in Celsius.
    """
    fahrenheit = (celsius * 9 / 5) + 32
    return f"{celsius}°C = {fahrenheit:.1f}°F"
```

### Rules

| Rule | Why |
|------|-----|
| Must be `async` | All tool methods are awaited by the engine |
| Must return `str` | The result is injected into LLM context as text |
| Must have type hints | Auto-generates JSON Schema for LLM function calling |
| Must have `description=` | The LLM reads this to decide when to call the tool |

### Automatic JSON Schema Generation

The SDK auto-generates JSON Schema from your type hints. The LLM receives this schema to understand what parameters your tool accepts.

| Python Type | JSON Schema | Example |
|------------|-------------|---------|
| `str` | `{"type": "string"}` | `city: str` |
| `int` | `{"type": "integer"}` | `count: int` |
| `float` | `{"type": "number"}` | `amount: float` |
| `bool` | `{"type": "boolean"}` | `verbose: bool` |
| `list[str]` | `{"type": "array", "items": {"type": "string"}}` | `tags: list[str]` |
| `Optional[str]` | `{"type": "string"}` (not required) | `note: str \| None = None` |
| `Literal["a", "b"]` | `{"type": "string", "enum": ["a", "b"]}` | `mode: Literal["get", "set"]` |
| `Enum` | `{"type": "string", "enum": [...]}` | `color: Color` |
| `dict` | `{"type": "object"}` | `options: dict` |

**Docstring parameters are extracted too.** If you use Google-style docstrings with `Args:`, the SDK extracts parameter descriptions and includes them in the schema. The LLM sees these descriptions and makes better decisions about what to pass.

```python
@tool(description="Search the web for a query.")
async def search(self, query: str, max_results: int = 5) -> str:
    """Search the web and return results.

    Args:
        query: The search query.
        max_results: Maximum number of results to return (1-10).
    """
    ...
```

Generates this schema (sent to the LLM):

```json
{
  "type": "object",
  "properties": {
    "query": {
      "type": "string",
      "description": "The search query."
    },
    "max_results": {
      "type": "integer",
      "description": "Maximum number of results to return (1-10).",
      "default": 5
    }
  },
  "required": ["query"]
}
```

### Advanced Options

```python
@tool(
    description="Delete all user data permanently.",
    requires_confirmation=True,   # User must approve before execution
    timeout_seconds=60,           # Override default 30s timeout
)
async def delete_all_data(self, confirm: bool) -> str:
    """Permanently delete all data. Cannot be undone.

    Args:
        confirm: Must be True to proceed.
    """
    if not confirm:
        return "Deletion cancelled."
    # ... perform deletion
    return "All data deleted."
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `description` | `str` | required | What this tool does (shown to LLM) |
| `parameters` | `dict` | `None` | Explicit JSON Schema override (auto-generated if None) |
| `requires_confirmation` | `bool` | `False` | User must approve before execution |
| `timeout_seconds` | `int` | `30` | Max execution time before the engine kills the call |

### The Multi-Mode Pattern

For plugins with many related operations, use a `mode` parameter instead of separate tools. This reduces tool clutter in the LLM's context window. This is the pattern used by the official Financial Math plugin (8 operations, 1 tool):

```python
from typing import Literal

@tool(description="Financial calculations: compound interest, TVM, IRR, amortization, percentage change, and currency conversion.")
async def calculate(
    self,
    mode: Literal["compound", "tvm", "irr", "amortize", "percentage", "convert"],
    principal: float | None = None,
    rate: float | None = None,
    periods: int | None = None,
    # ... other optional params
) -> str:
    """Perform a financial calculation.

    Args:
        mode: Which calculation to perform.
        principal: Starting amount (for compound, tvm, amortize).
        rate: Interest rate as decimal (0.05 = 5%).
        periods: Number of periods.
    """
    if mode == "compound":
        return self._compound(principal, rate, periods)
    elif mode == "tvm":
        return self._tvm(principal, rate, periods)
    # ...
```

**When to use multi-mode:** When you have 4+ related operations that share parameters. The LLM handles routing between modes based on the user's natural language.

**When to use separate tools:** When operations are unrelated or have completely different parameter sets.

---

## Plugin Manifest (plugin.yaml)

Every plugin has a `plugin.yaml` that declares metadata and permissions. This file is the source of truth for what your plugin is and what it's allowed to do.

### Complete Example

```yaml
# Required
name: habit-tracker
version: 1.0.0
description: Track daily habits with streak counting and memory.

# Recommended
author: Your Name <you@example.com>
license: MIT
repository: https://github.com/you/sovyx-plugin-habit-tracker
min_sovyx_version: 0.7.0

# Permissions — only declare what you need
permissions:
  - brain:read         # Search existing memories
  - brain:write        # Store new habit entries
  - network:internet   # Only if you call external APIs

# Network sandbox — required if network:internet is declared
network:
  allowed_domains:
    - api.example.com
    - api.backup.com

# Tool metadata (for marketplace display)
tools:
  - name: log_habit
    description: Log a habit completion for today.
  - name: check_streak
    description: Check your current streak for a habit.
  - name: list_habits
    description: List all tracked habits and their streaks.
```

### Fields Reference

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Must exactly match `ISovyxPlugin.name`. Lowercase, hyphens allowed. |
| `version` | Yes | SemVer. Must exactly match `ISovyxPlugin.version`. Mismatch = load failure. |
| `description` | Yes | One-line description for dashboard and marketplace. |
| `author` | Recommended | Your name or org. Shown in marketplace. |
| `license` | Recommended | SPDX identifier (MIT, Apache-2.0, AGPL-3.0, etc.). |
| `repository` | Recommended | Source code URL. Shown in marketplace. |
| `min_sovyx_version` | Recommended | Minimum compatible Sovyx version. Older engines skip your plugin. |
| `permissions` | If needed | List of required permissions. See [Permissions](#permissions). |
| `network.allowed_domains` | If network used | Whitelist for HTTP requests. Required with `network:internet`. |
| `tools` | Recommended | Tool metadata for marketplace listing. |

---

## Permissions

Plugins run in a permission sandbox. Every capability must be declared in `plugin.yaml` and approved by the user.

### Available Permissions

| Permission | Grants | Example use case |
|-----------|--------|-----------------|
| `network:internet` | HTTP requests to `allowed_domains` only | API calls, web scraping |
| `brain:read` | `brain.search()`, `brain.recall()` | Search user's memories |
| `brain:write` | `brain.remember()`, `brain.forget()` | Store/remove memories |
| `fs:read` | Read files in the plugin's data directory | Load cached data |
| `fs:write` | Write files in the plugin's data directory | Save state between restarts |
| `events:emit` | Emit events on the EventBus | Notify other plugins |
| `events:subscribe` | Subscribe to engine events | React to conversations |

### How Permissions Work

1. You declare permissions in `plugin.yaml`
2. On `sovyx plugin install`, the user sees the permission list and approves
3. At runtime, the SDK enforces permissions before every operation
4. **Undeclared permissions are denied** — calling `brain.search()` without `brain:read` raises `PermissionDenied`

### Principle of Least Privilege

Only request what you need. A plugin that tracks habits needs `brain:read` + `brain:write`. It does NOT need `network:internet` or `fs:write`. Users trust plugins that request fewer permissions.

```yaml
# Good — minimal permissions
permissions:
  - brain:read
  - brain:write

# Bad — over-requesting (why does a habit tracker need network?)
permissions:
  - brain:read
  - brain:write
  - network:internet
  - fs:write
  - events:emit
```

---

## Testing

The SDK provides a complete mock harness so you can test without a running Sovyx engine. Every mock is designed to behave identically to the real implementation.

### Basic Tool Test

```python
import pytest
from my_plugin.plugin import MyPlugin


@pytest.fixture()
def plugin() -> MyPlugin:
    return MyPlugin()


@pytest.mark.anyio()
async def test_greet(plugin: MyPlugin) -> None:
    result = await plugin.greet("World")
    assert result == "Hello, World!"
    assert isinstance(result, str)  # Tools must return str
```

### Testing Brain Access

```python
from sovyx.plugins.testing import MockPluginContext


@pytest.mark.anyio()
async def test_find_notes() -> None:
    # Create a mock context with seeded brain data
    ctx = MockPluginContext("my-plugin")
    ctx.brain.seed([
        {"name": "meeting-notes", "content": "Discussed Q2 roadmap with team"},
        {"name": "coffee-pref", "content": "Oat milk, no sugar"},
        {"name": "gym-schedule", "content": "Mon/Wed/Fri at 7am"},
    ])

    # Initialize plugin with mock brain
    plugin = MyPlugin(brain=ctx.brain)
    result = await plugin.find_notes("meeting")

    # Verify results
    assert "Q2 roadmap" in result
    assert "coffee" not in result  # Shouldn't match

    # Verify the search was called correctly
    ctx.brain.assert_searched("meeting")
```

### Testing Memory Writes

```python
@pytest.mark.anyio()
async def test_save_note() -> None:
    ctx = MockPluginContext("my-plugin")
    plugin = MyPlugin(brain=ctx.brain)

    result = await plugin.save_note("project-idea", "Build a Sovyx plugin for Todoist")

    assert "Remembered" in result

    # Verify what was written to the brain
    assert len(ctx.brain.learned) == 1
    assert ctx.brain.learned[0]["name"] == "project-idea"
    assert "Todoist" in ctx.brain.learned[0]["content"]
```

### Testing HTTP Calls

```python
from sovyx.plugins.testing import MockPluginContext


@pytest.mark.anyio()
async def test_api_call() -> None:
    ctx = MockPluginContext("my-plugin")

    # Pre-configure mock HTTP responses
    ctx.http.mock_response(
        url="https://api.example.com/data",
        json={"temperature": 22, "unit": "celsius"},
    )

    plugin = MyPlugin(http=ctx.http)
    result = await plugin.get_weather("London")

    assert "22" in result
    ctx.http.assert_called("https://api.example.com/data")
```

### Available Mocks

| Mock | What it simulates | Key methods |
|------|-------------------|-------------|
| `MockBrainAccess` | Brain engine | `seed()`, `search()`, `learn()`, `assert_searched()` |
| `MockEventBus` | Event system | `emit()`, `subscribe()`, `assert_emitted()` |
| `MockHttpClient` | Sandboxed HTTP | `mock_response()`, `assert_called()` |
| `MockFsAccess` | Plugin filesystem | In-memory `read()`, `write()`, `exists()` |
| `MockPluginContext` | All of the above | `.brain`, `.events`, `.http`, `.fs` |

### Tool Discovery Tests

Every plugin should verify its tools are properly registered:

```python
def test_tools_discovered(plugin: MyPlugin) -> None:
    """Verify all expected tools are discoverable."""
    tools = plugin.get_tools()
    names = [t.name for t in tools]
    assert "my-plugin.greet" in names


def test_all_tools_have_descriptions(plugin: MyPlugin) -> None:
    """Every tool must have a non-empty description."""
    for t in plugin.get_tools():
        assert len(t.description) > 0, f"{t.name} missing description"


def test_all_tools_have_valid_schemas(plugin: MyPlugin) -> None:
    """Every tool must have a valid JSON Schema for parameters."""
    for t in plugin.get_tools():
        assert t.parameters.get("type") == "object"
        assert "properties" in t.parameters
```

### Running Tests

```bash
# Run all tests
pytest tests/ -v

# Run with coverage
pytest tests/ --cov=my_plugin --cov-report=term-missing

# Run the full quality gate (what the marketplace CI runs)
sovyx plugin validate .   # ruff + mypy + pytest + schema validation
```

### Recommended Test Structure

```
tests/
├── test_plugin.py       # Tool discovery + basic behavior
├── test_brain.py        # Brain access tests (search, remember)
├── test_api.py          # External API integration tests
├── test_edge_cases.py   # Error handling, empty inputs, limits
└── conftest.py          # Shared fixtures (plugin, mock context)
```

---

## Configuration

Users configure plugins in their `~/.sovyx/mind.yaml`:

```yaml
plugins:
  plugins_config:
    my-plugin:
      enabled: true
      config:
        api_key: "sk-abc123"
        timeout: 30
        max_results: 10
      permissions:
        - network:internet
        - brain:read
```

### Config Schema Validation

Define a schema in your plugin to validate user-provided config:

```python
class MyPlugin(ISovyxPlugin):
    config_schema = {
        "required": ["api_key"],
        "properties": {
            "api_key": {
                "type": "string",
                "description": "API key for the external service.",
            },
            "timeout": {
                "type": "integer",
                "default": 30,
                "minimum": 5,
                "maximum": 120,
                "description": "Request timeout in seconds.",
            },
            "max_results": {
                "type": "integer",
                "default": 10,
                "minimum": 1,
                "maximum": 100,
                "description": "Maximum results per query.",
            },
        },
    }
```

If a user's config doesn't match the schema (missing required fields, wrong types), Sovyx shows a clear validation error on startup instead of silently failing.

---

## Safety and Security

### Error Boundary

Plugin crashes **never** crash the engine. Every tool call runs inside:

```python
# Inside the engine (you don't write this — it happens automatically)
try:
    async with asyncio.timeout(tool.timeout_seconds):
        result = await tool.handler(**params)
except TimeoutError:
    result = ToolResult(error="Tool timed out")
except Exception as e:
    result = ToolResult(error=f"Tool failed: {e}")
```

If your plugin raises an exception, the engine catches it and returns an error result to the LLM. The LLM decides how to respond (usually "Sorry, that tool failed — let me try another way.").

### Auto-Disable

If a plugin fails **5 consecutive times**, it's automatically disabled:

- `PluginAutoDisabled` event emitted on the EventBus
- Tool removed from LLM context (won't be called again)
- Dashboard shows the plugin as disabled with error count
- Re-enable via `sovyx plugin enable <name>` or the dashboard

This protects the user experience — a broken plugin doesn't spam errors forever.

### Security Scanner

Before loading, Sovyx scans your plugin's AST (Abstract Syntax Tree) for dangerous patterns:

| Blocked Pattern | Why | Alternative |
|----------------|-----|-------------|
| `eval()` | Arbitrary code execution | Use `ast.literal_eval()` for safe parsing |
| `exec()` | Arbitrary code execution | Restructure logic to avoid dynamic execution |
| `__import__()` | Import guard bypass | Use normal `import` statements |
| `subprocess.*` | Shell command execution | Not available — use the SDK's sandboxed APIs |
| `os.system()` | Shell command execution | Not available |
| `pickle.loads()` | Deserialization attack | Use `json.loads()` |

The scanner runs automatically on plugin install and load. If your plugin uses any blocked pattern, it won't load and you'll see a clear error message explaining why.

### Network Sandbox

HTTP requests are restricted to domains declared in `plugin.yaml`:

```yaml
network:
  allowed_domains:
    - api.example.com
    - cdn.example.com
```

Requests to any other domain raise `NetworkAccessDenied`. This prevents plugins from:
- Exfiltrating user data to unauthorized servers
- Making unexpected API calls
- Participating in DDoS or spam

---

## Hot Reload

During development, plugins auto-reload when you save a file:

```bash
# Start Sovyx with hot-reload enabled
sovyx start --dev
```

### How it works

1. File watcher (via `watchdog`) monitors your plugin directory
2. On file change → 200ms debounce (avoids rapid reloads on save-all)
3. Old plugin version: `teardown()` called → module cache cleared
4. New plugin version: module loaded → `setup(ctx)` called
5. If reload fails: 3 retries with backoff, then falls back to old version

### Requirements

```bash
pip install watchdog  # Required for hot-reload
```

### Tips

- Keep `setup()` and `teardown()` fast — they run on every reload
- Use `teardown()` to close connections — leaked resources accumulate on reloads
- If your plugin reads config from `plugin.yaml`, that file is also watched

---

## Distribution

### Via pip (recommended)

Add an entry point to `pyproject.toml`:

```toml
[project]
name = "sovyx-plugin-my-plugin"
version = "1.0.0"

[project.entry-points."sovyx.plugins"]
my-plugin = "my_plugin:MyPlugin"
```

Users install with:

```bash
pip install sovyx-plugin-my-plugin
# Auto-discovered on next 'sovyx start'
```

**Naming convention:** `sovyx-plugin-<name>` for the package, `<name>` for the entry point.

### Via git

```bash
sovyx plugin install git+https://github.com/you/sovyx-plugin-example.git
```

### Local development

```bash
sovyx plugin install ./my-plugin           # Install from local directory
sovyx plugin install -e ./my-plugin        # Editable install (for development)
```

---

## Marketplace

The Sovyx Marketplace is where plugins reach users. Publish free or premium plugins — we handle distribution, payments, and discovery.

### Revenue Share

| Your Revenue | Sovyx Cut | Notes |
|-------------|-----------|-------|
| **85%** | 15% | More generous than Apple (70/30). On par with Shopify and JetBrains. |

We handle Stripe, invoicing, tax reporting, and refunds. You build, you earn.

### Pricing Options

- **Free** — open source, community goodwill, portfolio piece
- **One-time purchase** — $5+ (user buys once, uses forever)
- **Subscription** — $1–$49/mo (recurring revenue)

### Publishing

```bash
# Run quality gates (required for marketplace)
sovyx plugin validate .

# Submit for review
sovyx plugin publish .
```

Review process:
1. **Automated CI** — ruff, mypy, pytest, security scan, schema validation
2. **Human review** — code quality, permission justification, description accuracy
3. **Published** — live in the marketplace within 48 hours

### What you get as a developer

- **Developer dashboard** — real-time analytics (installs, active users, ratings, earnings)
- **Featured placement** — quality plugins get "Plugin of the Week" spotlight
- **Co-marketing** — we promote great plugins on our channels
- **"Sovyx Builder" badge** — social proof for your profile
- **Direct channel** — Discord access to the core team for SDK questions

---

## CLI Reference

```
Plugin Management:
  sovyx plugin list                  List installed plugins and their status
  sovyx plugin info <name>           Show plugin details (tools, permissions, health)
  sovyx plugin install <source>      Install from local path, pip, or git URL
  sovyx plugin install -e <path>     Editable install (for development)
  sovyx plugin remove <name>         Uninstall a plugin
  sovyx plugin enable <name>         Enable a disabled plugin
  sovyx plugin disable <name>        Disable a plugin (tools removed from LLM)

Development:
  sovyx plugin create <name>         Scaffold a new plugin project
  sovyx plugin validate <dir>        Run quality gates (ruff + mypy + pytest)
  sovyx plugin test <dir>            Run tests only

Distribution:
  sovyx plugin publish <dir>         Submit to the marketplace
```

---

## Official Plugins as Reference

Three production-grade plugins ship with Sovyx. Study them — they demonstrate every pattern in this guide.

### 1. Knowledge — The Brain Interface

**Path:** `src/sovyx/plugins/official/knowledge.py`
**Stats:** 922 LOC · 5 tools · 120 tests

The plugin that talks directly to the Mind's brain. Study this for:
- Full `brain:read` + `brain:write` usage patterns
- Semantic deduplication (avoid storing duplicate memories)
- Conflict resolution (when new info contradicts old)
- Auto-relations between concepts
- Confidence tracking on memories

```python
# Example: how Knowledge handles search with spreading activation
results = await self.brain.search(query, limit=limit)
# Results come back with confidence scores and relation paths
# The plugin formats them for the LLM, including HOW concepts are related
```

### 2. Web Intelligence — External APIs Done Right

**Path:** `src/sovyx/plugins/official/web_intelligence.py`
**Stats:** 1,962 LOC · 6 tools · 242 tests

Web search, news, page extraction. Study this for:
- Intent classification (query → search mode routing)
- Multiple external API integrations with fallbacks
- Result ranking and filtering
- Graceful degradation when services are unavailable
- Rate limiting and caching

### 3. Financial Math — The SDK Showcase

**Path:** `src/sovyx/plugins/official/financial_math.py`
**Stats:** 2,019 LOC · 8 tools · 308 tests

This is the reference implementation. Every best practice is demonstrated:
- **Multi-mode pattern** — 8 operations through 1 `@tool` with a `mode` parameter
- **Decimal-first precision** — all math via `Decimal(str(value))`, banker's rounding
- **Structured JSON output** — consistent `{"ok", "action", "mode", "result", "message"}` schema
- **Input validation** — `_require()`, `_validate_value()`, bounds checking before any computation
- **Safety limits** — max periods, max cashflows, overflow protection
- **Zero external dependencies** — Newton-Raphson for IRR, pure Python Decimal math
- **Property-based tests** — Hypothesis for invariant testing (e.g., "amortization total always equals principal + interest")

See the [Financial Math API Reference](./financial-math-plugin.md) for complete documentation.

---

## Patterns and Best Practices

### Return structured data

The LLM processes your tool's return value. Structured output helps it extract and present information:

```python
import json

# ✅ Good — structured, parseable, consistent
return json.dumps({
    "ok": True,
    "temperature": 22,
    "unit": "celsius",
    "city": city,
    "conditions": "partly cloudy",
})

# ✅ Also good — human-readable for simple results
return f"Weather in {city}: 22°C, partly cloudy"

# ❌ Bad — ambiguous, hard for LLM to parse
return str(data)

# ❌ Bad — raw exception string
return f"Error: {e}"
```

**Recommendation:** Use JSON for complex results (multiple fields, nested data). Use plain text for simple results (one value, yes/no, confirmations).

### Validate inputs early

Catch bad inputs before doing any work:

```python
@tool(description="Transfer funds between accounts.")
async def transfer(self, from_acct: str, to_acct: str, amount: float) -> str:
    """Transfer money between accounts.

    Args:
        from_acct: Source account ID.
        to_acct: Destination account ID.
        amount: Amount to transfer (must be positive).
    """
    # Validate BEFORE doing anything
    if amount <= 0:
        return json.dumps({"ok": False, "error": "Amount must be positive"})
    if amount > 1_000_000:
        return json.dumps({"ok": False, "error": "Amount exceeds limit ($1M)"})
    if from_acct == to_acct:
        return json.dumps({"ok": False, "error": "Cannot transfer to same account"})

    # Now proceed safely
    result = await self._execute_transfer(from_acct, to_acct, amount)
    return json.dumps({"ok": True, "transfer_id": result.id, "amount": amount})
```

### Handle errors gracefully

Never let exceptions escape without a useful message:

```python
@tool(description="Fetch current stock price.")
async def stock_price(self, symbol: str) -> str:
    """Get the current price for a stock symbol.

    Args:
        symbol: Stock ticker (e.g., AAPL, GOOGL).
    """
    try:
        price = await self._fetch_price(symbol.upper())
        return json.dumps({
            "ok": True,
            "symbol": symbol.upper(),
            "price": float(price),
            "currency": "USD",
        })
    except aiohttp.ClientError:
        return json.dumps({
            "ok": False,
            "error": f"Could not reach price API for {symbol}",
        })
    except KeyError:
        return json.dumps({
            "ok": False,
            "error": f"Unknown symbol: {symbol}",
        })
```

### Write tool descriptions for the LLM

The description is what the LLM reads to decide when to call your tool. Be specific and unambiguous:

```python
# ✅ Good — the LLM knows exactly when to use this
@tool(description="Convert an amount from one currency to another using live exchange rates. Supports all major currencies (USD, EUR, GBP, JPY, etc.).")

# ❌ Bad — too vague, LLM might call it for unrelated queries
@tool(description="Do currency stuff.")

# ❌ Bad — too long, wastes context window
@tool(description="This tool converts currencies. It takes a source currency and target currency and an amount and then looks up the exchange rate from an API and multiplies the amount by the rate and returns the result in the target currency. It supports USD EUR GBP JPY CNY and many more.")
```

**Rule of thumb:** One sentence. What it does + key constraints. Under 100 characters if possible.

### Use `requires_confirmation` for destructive actions

```python
# Safe — read-only, no side effects
@tool(description="Check account balance.")
async def balance(self, account: str) -> str: ...

# Dangerous — modifies data, needs confirmation
@tool(description="Delete all memories about a topic.", requires_confirmation=True)
async def forget_topic(self, topic: str) -> str: ...
```

---

## Troubleshooting

### Plugin not loading

| Symptom | Cause | Fix |
|---------|-------|-----|
| Not in `sovyx plugin list` | Entry point missing | Check `pyproject.toml` `[project.entry-points."sovyx.plugins"]` |
| "Version mismatch" error | `plugin.yaml` version ≠ code version | Make both match exactly |
| "Security violation" error | Blocked AST pattern detected | Remove `eval()`, `exec()`, `subprocess` calls |
| "Permission denied" on load | `plugin.yaml` missing | Create `plugin.yaml` with required fields |

### Tool not being called

| Symptom | Cause | Fix |
|---------|-------|-----|
| LLM ignores your tool | Bad description | Rewrite — be specific about what it does and when |
| LLM calls wrong tool | Ambiguous description | Differentiate from other loaded plugins' tools |
| "PermissionDenied" at runtime | Missing permission | Add the permission to `plugin.yaml` |
| Tool returns error | Unhandled exception | Add try/except, return error as JSON |

### Testing issues

| Symptom | Cause | Fix |
|---------|-------|-----|
| `MockBrainAccess` returns empty | No seed data | Call `ctx.brain.seed([...])` before testing |
| `anyio` error | Missing pytest mark | Add `@pytest.mark.anyio()` to async tests |
| Import error | Wrong package structure | Check `__init__.py` re-exports your plugin class |

---

## FAQ

**Q: Can I use non-async functions as tools?**
A: No. All `@tool` methods must be `async`. If your logic is synchronous, wrap it: `result = await asyncio.to_thread(sync_function, args)`.

**Q: How many tools can a plugin have?**
A: No hard limit, but fewer is better. Each tool takes space in the LLM's context window. The Financial Math plugin has 8 operations but exposes them as 1 tool with a `mode` parameter.

**Q: Can my plugin talk to other plugins?**
A: Not directly. Use the EventBus (`events:emit` + `events:subscribe`) for inter-plugin communication.

**Q: How do I store state between tool calls?**
A: Three options: (1) Use `self._cache` for session-lifetime state, (2) Use `brain.remember()` for permanent state, (3) Use `fs:write` for file-based state.

**Q: Can I use external packages?**
A: Yes. Add them to your `pyproject.toml` dependencies. They're installed normally via pip. Just avoid blocked patterns (`subprocess`, `eval`, etc.).

**Q: What Python versions are supported?**
A: Python 3.11+. We use modern typing features (`str | None`, `list[str]`) extensively.

**Q: How do I debug a tool call?**
A: Add logging: `import logging; logger = logging.getLogger(__name__)`. Sovyx captures plugin logs and shows them in the dashboard Logs page.

**Q: Is there a size limit for tool return values?**
A: No hard limit, but the LLM has a context window. Keep returns under 4KB for best results. For large data, summarize and offer to show more.

---

## Resources

- **[Plugin Template](https://github.com/sovyx-ai/sovyx-plugin-template)** — clone and start building
- **[ISovyxPlugin source](https://github.com/sovyx-ai/sovyx/blob/main/src/sovyx/plugins/sdk.py)** — the contract (read this to understand every method)
- **[Testing Harness source](https://github.com/sovyx-ai/sovyx/blob/main/src/sovyx/plugins/testing.py)** — mock internals
- **[Official Plugins](https://github.com/sovyx-ai/sovyx/tree/main/src/sovyx/plugins/official)** — reference implementations
- **[Financial Math API Reference](./financial-math-plugin.md)** — complete API documentation for the most complex plugin
- **[sovyx.ai/developers](https://sovyx.ai/developers)** — overview and showcase

---

*Built something? Open a PR to add it to the [community plugins list](https://github.com/sovyx-ai/sovyx/discussions/categories/plugins), or [submit it to the marketplace](https://sovyx.ai/developers#marketplace).*

[![Built for Sovyx](https://sovyx.ai/badge.svg)](https://sovyx.ai/developers)
