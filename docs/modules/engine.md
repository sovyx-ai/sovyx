# Module: engine

## What it does

The `sovyx.engine` package is the kernel of Sovyx: it orchestrates the daemon lifecycle, wires every subsystem through a lightweight DI container, and publishes typed events that the rest of the system subscribes to. All runtime wiring flows through a layered bootstrap defined here.

## Key classes

| Name | Responsibility |
|---|---|
| `EngineConfig` | Pydantic-settings config (prefix `SOVYX_`, `__` for nesting). Resolves `data_dir` and `log_file`. |
| `ServiceRegistry` | Lightweight DI container (~150 LOC). Singletons, instances, reverse-order shutdown. |
| `EventBus` | Async in-process event bus with error isolation and correlation-id propagation. |
| `LifecycleManager` | Startup/shutdown orchestration. Owns the `PidLock` to prevent double-start. |
| `HealthChecker` | 10 checks consumed by `sovyx doctor` (SQLite, sqlite-vec, embedding, LLM, disk, RSS, loop lag). |
| `DegradationManager` | Per-component HEALTHY/DEGRADED/CRITICAL states with documented fallbacks. |
| `DaemonRPCServer` | Local JSON-RPC 2.0 over Unix socket / named pipe. Used by CLI and dashboard. |

## Bootstrap layers

`bootstrap()` initializes services in a fixed order. Shutdown walks `_init_order` in reverse and calls `.shutdown()` (sync or async) on anything that defines it.

- **Layer 0** — `EngineConfig` + `setup_logging` + channel env.
- **Layer 1** — `EventBus` then `DatabaseManager` (pools + migrations) then `MindManager`.
- **Layer 2 (per mind)** — Brain (repos + embedding + spreading + retrieval), PersonalityEngine, ContextAssembler, LLMRouter, CognitiveLoop, BridgeManager, channels.

## DI example

```python
# src/sovyx/engine/registry.py
registry = ServiceRegistry()

registry.register_instance(EventBus, event_bus)
registry.register_singleton(BrainService, lambda: build_brain(...))

brain = await registry.resolve(BrainService)  # lazy, cached after first call
event_bus = await registry.resolve(EventBus)  # returns the pre-built instance

await registry.shutdown_all()  # reverse init order
```

Registry keys use `f"{module}.{qualname}"` so lookups survive module reimports under pytest-xdist.

## EventBus example

```python
from sovyx.engine.events import EventBus, ThinkCompleted

bus = EventBus()

async def on_think(event: ThinkCompleted) -> None:
    print(event.model, event.tokens_in, event.cost_usd)

bus.subscribe(ThinkCompleted, on_think)
await bus.emit(ThinkCompleted(model="claude-sonnet-4", tokens_in=120, ...))
```

Handler exceptions are logged but do not abort the dispatch — remaining handlers still run.

## Events

All events are frozen dataclasses with `event_id`, `timestamp`, and `correlation_id`.

| Event | Emitted when |
|---|---|
| `EngineStarted` | Engine finishes startup. |
| `EngineStopping` | Engine begins shutdown. |
| `ServiceHealthChanged` | A service health state changes. |
| `PerceptionReceived` | A new perception enters the cognitive loop. |
| `ThinkCompleted` | Think phase finishes an LLM call. |
| `ResponseSent` | A response is delivered by a channel. |
| `ConceptCreated` | A concept is stored in memory. |
| `EpisodeEncoded` | An episode is encoded in memory. |
| `ConceptContradicted` | New content contradicts an existing concept. |
| `ConceptForgotten` | A concept is removed from the brain. |
| `ConsolidationCompleted` | A consolidation cycle finishes. |
| `ChannelConnected` | A communication channel connects. |
| `ChannelDisconnected` | A communication channel disconnects. |

## Errors

| Exception | When raised |
|---|---|
| `SovyxError` | Base class for all Sovyx errors. |
| `EngineError` / `BootstrapError` / `ShutdownError` | Kernel and lifecycle failures. |
| `ServiceNotRegisteredError` | Resolved service is missing from the registry. |
| `ConfigError` / `ConfigNotFoundError` / `ConfigValidationError` | Config load/validation failures. |
| `HealthCheckError` | One or more health checks failed. |

## Health checks

`HealthChecker` exposes ten checks used by `sovyx doctor`:

1. SQLite writable
2. sqlite-vec extension loaded
3. Embedding model loaded
4. EventBus functional
5. Brain accessible
6. At least one LLM provider reachable
7. Telegram connected (if configured)
8. Disk space > 100 MB
9. RSS < 85% of total RAM
10. Event loop lag < 100 ms

The dashboard `/api/health` endpoint uses a separate `HealthRegistry` in `sovyx.observability.health`.

## Degradation fallbacks

`DegradationManager` tracks each component and activates a known fallback when it trips:

- `sqlite-vec` missing — retrieval falls back to FTS5-only.
- All LLM providers down — template response.
- Telegram disconnect — exponential backoff reconnect.
- Disk < 100 MB — read-only warning.
- OOM risk — trigger consolidation prune.

## Configuration

```yaml
# Env equivalent: SOVYX_LOG__LEVEL=DEBUG
log:
  level: INFO
  console_format: pretty     # file handler always writes JSON

database:
  pool_size: 5
  wal: true

telemetry:
  enabled: false

socket:
  path: ~/.local/share/sovyx/sovyx.sock   # Windows: named pipe
```

`EngineConfig` resolves `LoggingConfig.log_file` to `data_dir/logs/sovyx.log` when unset — never hardcode log paths.

## Roadmap

- Pluggable event broker adapter (current bus is in-process only).
- Multi-mind manager beyond the current single-mind default.
- Health check plugin hooks for custom checks.

## See also

- `../architecture.md` — end-to-end request flow
- `cognitive.md` — the loop wired on top of the registry
- `brain.md` — memory services bootstrapped in Layer 2
- `llm.md` — router invoked by `ThinkPhase`
