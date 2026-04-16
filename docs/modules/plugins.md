# Module: plugins

## What it does

`sovyx.plugins` is the extension system: plugins are Python packages that expose **tools** (LLM-callable functions) to the Mind via function calling. The module discovers plugins, validates their manifest, runs a static AST scan plus a runtime import guard, enforces a capability-based permission model, and injects a sandboxed filesystem and HTTP client. Seven official plugins ship in the tree: `calculator`, `financial_math`, `knowledge`, `weather`, `web_intelligence`, `home_assistant`, and `caldav` (plus model helpers `_caldav_models` and `_ha_models`).

## Create a plugin in 10 lines

```python
from sovyx.plugins.sdk import ISovyxPlugin, tool


class HelloPlugin(ISovyxPlugin):
    @property
    def name(self) -> str:         return "hello"
    @property
    def version(self) -> str:      return "1.0.0"
    @property
    def description(self) -> str:  return "Say hi."

    @tool(description="Greet a person by name.")
    async def greet(self, name: str) -> str:
        return f"Hello, {name}!"
```

`@tool` inspects the method's type hints to build a JSON Schema for `parameters`. The tool is registered as `hello.greet` and the LLM can call it via normal function calling (OpenAI and Anthropic formats are both emitted by `ToolDefinition.to_openai_schema()` / `to_anthropic_schema()`).

## Key components

| Name | Responsibility |
|---|---|
| `ISovyxPlugin` | ABC every plugin implements (`name`, `version`, `description`, lifecycle hooks). |
| `@tool(...)` | Decorator that marks a method as LLM-callable and carries metadata. |
| `ToolDefinition` | Resolved tool (name, description, JSON Schema, timeout, handler). |
| `PluginManager` | Discovery, dependency resolution, load/unload, health, auto-disable. |
| `PluginManifest` | Pydantic model for `plugin.yaml` — validates on install and load. |
| `Permission` | 13-value `StrEnum` of capabilities requested by a plugin. |
| `PermissionEnforcer` | Runtime check — undeclared capability → `PermissionDeniedError`. |
| `PluginSecurityScanner` | Install-time AST scan for blocked imports, calls, and attributes. |
| `ImportGuard` | `sys.meta_path` hook that blocks dynamic imports during plugin execution. |
| `SandboxedFsAccess` | Filesystem handle scoped to the plugin's `data_dir` with size caps. |
| `SandboxedHttpClient` | `httpx`-based client with rate limiting and a domain allowlist. |
| `PluginContext` | Injects approved accesses (brain, events, fs, http, scheduler, vault). |

## Manifest

Each plugin has a `plugin.yaml` next to its package:

```yaml
name: weather
version: 1.0.0
description: Weather data via Open-Meteo (free, no API key).
author: sovyx
license: MIT

permissions:
  - network:internet

network:
  allowed_domains:
    - geocoding-api.open-meteo.com
    - api.open-meteo.com

tools:
  - name: get_weather
    description: Get current weather for a city.
  - name: get_forecast
    description: Get a multi-day forecast for a city.

depends: []
events:
  emits: []
  subscribes: []
```

`name` is constrained to `^[a-z][a-z0-9\-]*$`. `permissions` must be a subset of the `Permission` enum. `allowed_domains` is used by `SandboxedHttpClient` to whitelist outbound HTTP.

## Permissions

```python
# src/sovyx/plugins/permissions.py
class Permission(enum.StrEnum):
    BRAIN_READ = "brain:read"
    BRAIN_WRITE = "brain:write"
    EVENT_SUBSCRIBE = "event:subscribe"
    EVENT_EMIT = "event:emit"
    NETWORK_LOCAL = "network:local"
    NETWORK_INTERNET = "network:internet"
    FS_READ = "fs:read"
    FS_WRITE = "fs:write"
    SCHEDULER_READ = "scheduler:read"
    SCHEDULER_WRITE = "scheduler:write"
    VAULT_READ = "vault:read"
    VAULT_WRITE = "vault:write"
    PROACTIVE = "proactive"
```

| Permission | Access granted |
|---|---|
| `brain:read` / `brain:write` | Search and read, or create and update, concepts and episodes. |
| `event:subscribe` / `event:emit` | Receive from or publish to the `EventBus`. |
| `network:local` | HTTP to RFC1918 / loopback addresses only. |
| `network:internet` | HTTP to the domains listed in `network.allowed_domains`. |
| `fs:read` / `fs:write` | Read or write inside the plugin's own `data_dir`. |
| `scheduler:read` / `scheduler:write` | Inspect or create reminders and timers. |
| `vault:read` / `vault:write` | Read or write user credentials from the vault. |
| `proactive` | Send a message through a channel without a prior perception. |

If a plugin calls an access object it did not declare (e.g. a tool reaches `context.http` without `network:internet`), `PermissionEnforcer` raises `PermissionDeniedError` and the tool call fails.

## Sandbox

The sandbox is layered. Every plugin passes through each layer:

1. **Manifest validation** (`manifest.py`) — schema, version, permission names.
2. **Static AST scan** (`security.py`, install time) — rejects plugins that import or reference disallowed names.
3. **Runtime import guard** (`security.py`) — a `sys.meta_path` hook intercepts `__import__` and `importlib` inside the plugin.
4. **Permission enforcement** (`permissions.py`) — checked on every access.
5. **Sandboxed filesystem + HTTP** (`sandbox_fs.py`, `sandbox_http.py`).

```python
# src/sovyx/plugins/security.py — blocked-by-default
class PluginSecurityScanner:
    BLOCKED_IMPORTS: frozenset[str] = frozenset({
        "os", "subprocess", "shutil", "sys", "importlib",
        "ctypes", "pickle", "marshal", "code", "codeop",
        "compileall", "multiprocessing", "threading",
        "signal", "resource", "socket",
    })
```

```python
# src/sovyx/plugins/sandbox_fs.py — hard limits
_MAX_FILE_BYTES = 50 * 1024 * 1024     # 50 MB per file
_MAX_TOTAL_BYTES = 500 * 1024 * 1024   # 500 MB per plugin

class SandboxedFsAccess:
    """Filesystem scoped to data_dir. Symlinks are resolved BEFORE the path check."""
    async def write(self, path: str, data: str | bytes) -> None: ...
    async def read(self, path: str) -> bytes: ...
```

`SandboxedHttpClient` applies a domain allowlist, a per-plugin rate limit, and a hard timeout. Local-network access requires the `network:local` permission explicitly.

## Lifecycle

```
discovered → loaded → running → disabled
                  ↘              ↑
                    auto-disabled (5 consecutive failures)
```

```python
# src/sovyx/plugins/manager.py
_DEFAULT_TOOL_TIMEOUT_S = 30.0
_MAX_CONSECUTIVE_FAILURES = 5


@dataclasses.dataclass
class _PluginHealth:
    consecutive_failures: int = 0
    disabled: bool = False
    last_error: str = ""
    active_tasks: int = 0
```

The manager runs tools with `asyncio.wait_for(tool(), timeout=...)`. Five consecutive failures flip the plugin to `disabled` and raise `PluginAutoDisabledError`. Administrators can re-enable via the `plugin enable` CLI command once the cause is fixed.

## CLI

```bash
sovyx plugin install ./my-plugin      # validate + copy to data_dir/plugins
sovyx plugin list                     # show discovered plugins with state
sovyx plugin info my-plugin           # manifest, permissions, tools, risk levels
sovyx plugin enable my-plugin
sovyx plugin disable my-plugin
sovyx plugin validate ./my-plugin     # AST scan without installing
sovyx plugin remove my-plugin
sovyx plugin create my-plugin         # scaffold a new plugin skeleton
```

Hot reload happens automatically when the plugin file changes — `PluginFileWatcher` picks up edits inside `~/.sovyx/plugins/` and re-loads the plugin in place.

Install performs the AST scan. Any `SecurityFinding` aborts the install unless `--allow-unsafe` is passed explicitly.

## Official plugins

| Plugin | Tools | Permissions |
|---|---|---|
| `calculator` | `calculate` | none (pure) |
| `financial_math` | `calculate`, compound interest, NPV, IRR, amortization helpers | none (pure) |
| `knowledge` | `remember`, `search`, `recall`, `forget` | `brain:read`, `brain:write` |
| `weather` | `get_weather`, `get_forecast` | `network:internet` |
| `web_intelligence` | `fetch_url`, `extract_content`, `search`, `research`, `lookup`, `learn_from_web`, `recall_web` | `network:internet` |
| `home_assistant` (v0.11.8) | `list_lights`, `turn_on_light`, `turn_off_light`, `turn_on_switch`, `turn_off_switch`, `read_sensor`, `list_sensors`, `set_temperature` | `network:local` |
| `caldav` (v0.11.9) | `list_calendars`, `get_today`, `get_upcoming`, `get_event`, `find_free_slot`, `search_events` | `network:internet` |

`financial_math` uses `Decimal` end-to-end and is the recommended study target for tool design, input validation, and structured JSON output. `home_assistant` is the canonical example of a LAN-bound plugin (`allow_local=True` on `SandboxedHttpClient`) with a `requires_confirmation=True` tool (`set_temperature`). `caldav` is the canonical example of a plugin that speaks an HTTP-extension protocol (PROPFIND / REPORT) via the public `SandboxedHttpClient.request()` method, with `defusedxml` for XXE-safe parsing of server-controlled XML and `icalendar` + `python-dateutil` for RRULE expansion.

## Events

| Event | Payload |
|---|---|
| `PluginStateChanged` | `plugin_name`, `from_state`, `to_state`, `error_message`. |
| `PluginLoaded` | `plugin_name`, `plugin_version`, `tools_count`. |
| `PluginUnloaded` | `plugin_name`, `reason`. |
| `PluginToolExecuted` | `plugin_name`, `tool_name`, `success`, `duration_ms`, `error_message`. |
| `PluginAutoDisabled` | `plugin_name`, `consecutive_failures`, `last_error`. |

## Errors

| Exception | Raised when |
|---|---|
| `PluginError` | Base class for the plugin system. |
| `ManifestError` | `plugin.yaml` missing or invalid. |
| `PermissionDeniedError` | Runtime access without the matching `Permission`. |
| `PluginDisabledError` | Tool invoked on a disabled plugin. |
| `PluginAutoDisabledError` | Plugin hit `_MAX_CONSECUTIVE_FAILURES` (5). |
| `InvalidTransitionError` | Illegal lifecycle state transition. |

## Roadmap

- **Kernel-level isolation for marketplace plugins** — seccomp-BPF (Linux), mount/PID/user namespaces, Seatbelt profile on macOS, and a subprocess IPC protocol so plugins run out-of-process.
- **Zero-downtime rollback** — automatic revert to the previous version if a hot reload fails health checks.
- **Marketplace billing** — see [`cloud`](./cloud.md) roadmap for Stripe Connect.

## See also

- Source: `src/sovyx/plugins/sdk.py`, `manager.py`, `manifest.py`, `permissions.py`, `security.py`, `sandbox_fs.py`, `sandbox_http.py`, `context.py`, `lifecycle.py`, `hot_reload.py`.
- Official plugins: `src/sovyx/plugins/official/{calculator,financial_math,knowledge,weather,web_intelligence,home_assistant,caldav}.py`. Helper models: `_ha_models.py`, `_caldav_models.py`.
- Tests: `tests/plugins/`, `tests/unit/plugins/`, `tests/security/plugins/`.
- Related modules: [`engine`](./engine.md) for the `ServiceRegistry` that binds `PluginContext`, [`dashboard`](./dashboard.md) for the `/api/plugins` endpoints.
