# Plugins

Sovyx supports a full plugin system to extend Minds with custom tools the LLM can call
during conversation. Plugins run in a sandboxed environment with explicit permissions.

## Architecture

Plugins hook into the **Act** phase of the cognitive loop (Perceive → Attend → Think → **Act** → Reflect). When the LLM generates a tool call, the PluginManager dispatches it to the appropriate handler via a ReAct loop (max 3 iterations).

```
LLM Response → tool_call → PluginManager → Plugin.method() → ToolResult → LLM
```

## Plugin Interface

Plugins implement `ISovyxPlugin` and decorate tools with `@tool`:

```python
from sovyx.plugins.sdk import ISovyxPlugin, tool

class WeatherPlugin(ISovyxPlugin):
    @property
    def name(self) -> str:
        return "weather"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def description(self) -> str:
        return "Weather forecasts via Open-Meteo."

    @tool(description="Get current weather for a city.")
    async def get_weather(self, city: str) -> str:
        # Your logic here
        return f"Weather in {city}: 22°C, sunny"
```

## Configuration

Configure plugins in `mind.yaml`:

```yaml
plugins:
  disabled:
    - dangerous-plugin

  plugins_config:
    weather:
      enabled: true
      config:
        default_units: celsius
      permissions:
        - network:internet
```

## Permissions

Plugins run in a capability-based sandbox. They must declare what they need:

| Permission | Description |
|-----------|-------------|
| `network:internet` | HTTP requests to allowed domains |
| `brain:read` | Read from Mind's memory |
| `brain:write` | Write to Mind's memory |
| `fs:read` | Read files in plugin data dir |
| `fs:write` | Write files in plugin data dir |
| `events:emit` | Emit events on the EventBus |
| `events:subscribe` | Subscribe to events |

Undeclared permissions are denied at runtime with `PermissionDeniedError`.

## Built-in Plugins

| Plugin | Tools | Description |
|--------|-------|-------------|
| `calculator` | `calculate` | Safe math via AST (no eval) |
| `weather` | `get_weather`, `get_forecast`, `will_it_rain` | Open-Meteo (free, no API key) |
| `knowledge` | `remember`, `search`, `forget`, `recall_about`, `what_do_you_know` | Brain interface |

## Dashboard Management

The **Plugins** page in the dashboard (`/plugins`) provides a full management UI:

- **Grid view** with search, status filters, and category filters
- **Plugin detail panel** — permissions, tools, metadata, configuration
- **Enable/disable/remove** with confirmation dialogs
- **Permission approval** — security-first flow for activating plugins
- **Real-time sync** via WebSocket events

## Safety

### Error Boundary
- Plugin crashes never crash the engine
- `asyncio.wait_for` enforces timeouts (default 30s)
- All exceptions caught and returned as error results

### Auto-Disable
- 5 consecutive failures → plugin auto-disabled
- `PluginAutoDisabled` event emitted
- Re-enable via CLI (`sovyx plugin enable <name>`) or dashboard

### Security Scanner
- AST scanner flags `eval()`, `exec()`, `__import__`, `subprocess`
- Runtime `ImportGuard` restricts imports (PEP 451)
- Network requests limited to declared domains

## CLI

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

## Further Reading

- [Plugin Developer Guide](plugin-developer-guide.md) — full SDK reference, testing harness, distribution
- [API Reference](api.md) — REST endpoints including `/api/plugins`
