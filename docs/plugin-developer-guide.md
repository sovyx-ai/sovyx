# Sovyx Plugin Developer Guide

Build custom tools for Sovyx Minds. Plugins extend the LLM's capabilities with
real-world actions — API calls, calculations, database queries, and more.

## Quick Start

```bash
# Scaffold a new plugin
sovyx plugin create my-weather

# Edit your plugin
cd my-weather
# → plugin.py: Add @tool methods
# → plugin.yaml: Declare permissions

# Validate quality gates
sovyx plugin validate .

# Install and test
sovyx plugin install .
```

## Architecture

```
User → LLM → tool_call → PluginManager → YourPlugin.your_tool()
                                ↓
                          ToolResult → LLM → Final Response
```

The ReAct loop runs up to 3 iterations:
1. LLM decides to call a tool
2. PluginManager dispatches to your plugin
3. Result injected back into LLM context
4. LLM responds (or calls another tool)

## Plugin Structure

```
my-plugin/
├── __init__.py          # Re-export your plugin class
├── plugin.py            # ISovyxPlugin subclass with @tool methods
├── plugin.yaml          # Manifest (permissions, metadata)
├── tests/
│   └── test_my_plugin.py
├── pyproject.toml       # For pip install
└── README.md
```

## Writing a Plugin

### 1. Subclass ISovyxPlugin

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

    @tool(description="Calculate the answer to life.")
    async def compute(self, question: str) -> str:
        return f"The answer to '{question}' is 42."
```

### 2. The @tool Decorator

Every public tool must be decorated with `@tool(description="...")`:

```python
@tool(description="Get weather for a city.")
async def get_weather(self, city: str, units: str = "celsius") -> str:
    # Your logic here
    return f"Weather in {city}: 22°C"
```

- Must be `async`
- Must return `str`
- Parameters become the tool's JSON Schema
- Description is what the LLM sees

### 3. Plugin Manifest (plugin.yaml)

```yaml
name: my-plugin
version: 1.0.0
description: What my plugin does.
author: Your Name
license: MIT
min_sovyx_version: 0.7.0

permissions:
  - network:internet    # HTTP access to the internet
  - brain:read          # Read from Mind's memory

network:
  allowed_domains:
    - api.example.com   # Whitelist specific domains

tools:
  - name: compute
    description: Calculate the answer to life.
```

### 4. Permissions

Plugins run in a sandbox. They must declare what they need:

| Permission | Description |
|-----------|-------------|
| `network:internet` | HTTP requests to allowed domains |
| `brain:read` | Read from Mind's memory |
| `brain:write` | Write to Mind's memory |
| `fs:read` | Read files in plugin data dir |
| `fs:write` | Write files in plugin data dir |
| `events:emit` | Emit events on the EventBus |
| `events:subscribe` | Subscribe to events |

Undeclared permissions are denied at runtime.

## Testing Your Plugin

### Using the Testing Harness

```python
from sovyx.plugins.testing import MockPluginContext, MockBrainAccess

# Create mock context
ctx = MockPluginContext("my-plugin")

# Seed test data
ctx.brain.seed([
    {"name": "user-pref", "content": "dark mode"}
])

# Test your plugin
plugin = MyPlugin(brain=ctx.brain)
result = await plugin.search("dark mode")
assert "user-pref" in result

# Assert operations happened
ctx.brain.assert_searched("dark mode")
```

### Available Mocks

| Mock | Purpose |
|------|---------|
| `MockBrainAccess` | Seed data, track searches/learns |
| `MockEventBus` | Track emitted events |
| `MockHttpClient` | Pre-configure HTTP responses |
| `MockFsAccess` | In-memory filesystem |
| `MockPluginContext` | All of the above bundled |

### Running Tests

```bash
# Run your plugin's tests
pytest tests/ -v

# Validate quality gates
sovyx plugin validate .
```

## Configuration

Users configure plugins in `mind.yaml`:

```yaml
plugins:
  disabled:
    - dangerous-plugin

  plugins_config:
    my-plugin:
      enabled: true
      config:
        api_key: "abc123"
        timeout: 30
      permissions:
        - network:internet
```

Access config in your plugin:
```python
class MyPlugin(ISovyxPlugin):
    config_schema = {
        "required": ["api_key"],
        "properties": {
            "api_key": {"type": "string"},
            "timeout": {"type": "integer"},
        },
    }
```

## Safety Features

### Error Boundary
- Plugin crashes NEVER crash the engine
- `asyncio.wait_for` enforces timeouts (default 30s)
- All exceptions caught and returned as error results

### Auto-Disable
- 5 consecutive failures → plugin auto-disabled
- `PluginAutoDisabled` event emitted
- Re-enable via `sovyx plugin enable <name>` or API

### Security Scanning
- AST scanner flags `eval()`, `exec()`, `__import__`, `subprocess`
- Import guard restricts runtime imports
- Network requests limited to declared domains

## CLI Reference

```bash
sovyx plugin list              # List installed plugins
sovyx plugin info <name>       # Detailed plugin info
sovyx plugin install <source>  # Install (local/pip/git)
sovyx plugin create <name>     # Scaffold new plugin
sovyx plugin validate <dir>    # Run quality gates
sovyx plugin enable <name>     # Enable a plugin
sovyx plugin disable <name>    # Disable a plugin
sovyx plugin remove <name>     # Remove a plugin
```

## Built-in Plugins

| Plugin | Tools | Description |
|--------|-------|-------------|
| `calculator` | `calculate` | Safe math via AST (no eval) |
| `weather` | `get_weather`, `get_forecast`, `will_it_rain` | Open-Meteo (free, no key) |
| `knowledge` | `remember`, `search`, `forget`, `recall_about`, `what_do_you_know` | Brain interface |

## Hot Reload (Dev Mode)

During development, enable hot-reload to auto-reload plugins on file changes:

```python
from sovyx.plugins.hot_reload import PluginFileWatcher

watcher = PluginFileWatcher(plugin_manager, [Path("./my-plugin")])
watcher.start()
# Edit plugin files → auto-reloaded
watcher.stop()
```

Requires `pip install watchdog`.

## Distribution

### Via pip

```toml
# pyproject.toml
[project.entry-points."sovyx.plugins"]
my-plugin = "my_plugin:MyPlugin"
```

```bash
pip install sovyx-plugin-my-plugin
# Auto-discovered on next 'sovyx start'
```

### Via git

```bash
sovyx plugin install git+https://github.com/you/sovyx-plugin-example.git
```

### Local

```bash
sovyx plugin install ./my-plugin
```
