# Sovyx Architecture Bible

> Read this when you need to understand a subsystem in depth.
> For quick reference and conventions, see `CLAUDE.md` in the repo root.

## Table of Contents
1. [System Overview](#system-overview)
2. [Engine](#engine)
3. [Cognitive Loop](#cognitive-loop)
4. [Brain](#brain)
5. [Bridge](#bridge)
6. [Dashboard](#dashboard)
7. [Observability](#observability)
8. [Persistence](#persistence)
9. [LLM Router](#llm-router)
10. [CLI](#cli)
11. [Architectural Decisions](#architectural-decisions)
12. [Bug Post-Mortems](#bug-post-mortems)
13. [Version History](#version-history)

---

## System Overview

```
User Message
    │
    ▼
┌─────────┐     ┌──────────┐     ┌───────────────┐
│ Channel  │────▶│  Bridge  │────▶│ CogLoopGate   │
│ (Telegram│     │ Manager  │     │ (queue+worker) │
│  Signal) │     └──────────┘     └───────┬───────┘
└─────────┘                               │
                                          ▼
                              ┌───────────────────┐
                              │  Cognitive Loop    │
                              │                    │
                              │ Perceive → Attend  │
                              │ → Think → Act      │
                              │ → Reflect          │
                              └─────────┬──────────┘
                                        │
                         ┌──────────────┼──────────────┐
                         ▼              ▼              ▼
                   ┌──────────┐  ┌───────────┐  ┌──────────┐
                   │  Brain   │  │    LLM    │  │ Context  │
                   │ Service  │  │  Router   │  │ Assembler│
                   └──────────┘  └───────────┘  └──────────┘
                         │
              ┌──────────┼──────────┐
              ▼          ▼          ▼
        ┌──────────┐ ┌────────┐ ┌──────────┐
        │ Concepts │ │Episodes│ │Relations │
        └──────────┘ └────────┘ └──────────┘
                         │
                         ▼
                   ┌──────────┐
                   │  SQLite  │
                   │ (aiosql) │
                   └──────────┘
```

The dashboard is a separate FastAPI server that connects to engine services via the `ServiceRegistry`.

---

## Engine

**Files:** `src/sovyx/engine/`

### Config (`config.py`)
- `EngineConfig` — root config (pydantic-settings, `SOVYX_*` env vars)
- `LoggingConfig` — `console_format` ("text"|"json"), `log_file` (resolved by EngineConfig)
- `DatabaseConfig`, `HardwareConfig`, `LLMDefaultsConfig`, `TelemetryConfig`, `APIConfig`, `RelayConfig`, `SocketConfig`
- `load_engine_config()` — YAML + env + overrides merge. Handles legacy `format` → `console_format` migration.

### Bootstrap (`bootstrap.py`)
- `bootstrap()` — async function that wires ALL services in dependency order
- Order: EventBus → DatabaseManager → Brain → Personality → Context → LLM → Cognitive → PersonResolver → ConversationTracker → BridgeManager
- On partial failure: cleanup in reverse order
- Calls `setup_logging(engine_config.log)` for file+console handlers

### Lifecycle (`lifecycle.py`)
- `LifecycleManager` — manages daemon start/stop, signal handling, dashboard startup
- Starts dashboard via `DashboardServer` if API is enabled
- RPC server for CLI communication (`sovyx status`, `sovyx stop`)

### Registry (`registry.py`)
- `ServiceRegistry` — async dependency injection container
- `register_instance(type, instance)` / `resolve(type)` / `is_registered(type)`
- Single source of truth for all service instances

### Events (`events.py`)
- `EventBus` — pub/sub for engine events
- Events: `EngineStarted`, `ServiceHealthChanged`, `ConceptCreated`, `ThinkCompleted`, etc.
- Dashboard subscribes via `DashboardEventBridge`

**⚠️ Trap:** `engine/events.py` is imported by `observability/alerts.py`. The `observability/__init__.py` uses `__getattr__` lazy loading to break the circular import cycle. Never add eager imports to `observability/__init__.py`.

---

## Cognitive Loop

**Files:** `src/sovyx/cognitive/`

The cognitive loop processes each inbound message through 5 phases:

1. **Perceive** (`perceive.py`) — Extract intent, entities, sentiment from raw message
2. **Attend** (`attend.py`) — Retrieve relevant memories (concepts, episodes) from brain
3. **Think** (`think.py`) — Assemble context + call LLM for response generation
4. **Act** (`act.py`) — Execute any tool calls, format final response
5. **Reflect** (`reflect.py`) — Post-process: create concepts, encode episodes, update relations

### State Machine (`state.py`)
- `CognitiveStateMachine` — tracks loop state per request
- States: IDLE → PERCEIVING → ATTENDING → THINKING → ACTING → REFLECTING → IDLE

### Gate (`gate.py`)
- `CogLoopGate` — async queue + worker pool
- Serializes requests per conversation (prevents race conditions)
- Configurable concurrency

---

## Brain

**Files:** `src/sovyx/brain/`

### Service (`service.py`)
- `BrainService` — unified API for all brain operations
- Orchestrates: ConceptRepository, EpisodeRepository, RelationRepository, EmbeddingEngine, SpreadingActivation, HebbianLearning, EbbinghausDecay, HybridRetrieval, WorkingMemory

### Embedding (`embedding.py`)
- `EmbeddingEngine` — ONNX Runtime with e5-small-v2 model (384 dimensions)
- `ModelDownloader` — enterprise-grade with:
  - Retry-After header parsing (RFC 7231)
  - Decorrelated jitter backoff (AWS-style), max 60s
  - Mirror URL fallback (HuggingFace → GitHub Releases)
  - Download cooldown (15min `.failed` marker)
  - `HF_TOKEN` / `HUGGING_FACE_HUB_TOKEN` auth
  - `SOVYX_MODEL_DIR` for custom path

### Scoring (`scoring.py`)
- `ImportanceScorer` — multi-factor importance scoring
- Factors: frequency, recency, emotional valence, connection density, user reinforcement

### Retrieval (`retrieval.py`)
- `HybridRetrieval` — combines embedding similarity + spreading activation + recency decay

### Consolidation (`consolidation.py`)
- Background process: merge similar concepts, strengthen/weaken relations
- Ebbinghaus decay for forgetting curve

**⚠️ Trap:** Embedding model files are downloaded on first use. Tests mock the downloader — never let tests hit the real HuggingFace API.

---

## Bridge

**Files:** `src/sovyx/bridge/`

### Manager (`manager.py`)
- `BridgeManager` — routes messages between channels and cognitive loop
- Pipeline: InboundMessage → PersonResolver → ConversationTracker → Perception → CogLoopGate → ActionResult → OutboundMessage → Channel

### Channels (`channels/`)
- `telegram.py` — Telegram bot integration
- `signal.py` — Signal messenger integration
- Each channel implements `ChannelProtocol`

### Sessions (`sessions.py`)
- `ConversationTracker` — manages conversation state, history, context window

---

## Dashboard

**Files:** `src/sovyx/dashboard/` (backend) + `dashboard/` (frontend)

### Backend

#### Server (`server.py`)
- `DashboardServer` — FastAPI + uvicorn, manages lifecycle
- `create_app()` — registers all API routes
- Token-based auth (Bearer token in `~/.sovyx/token`)
- WebSocket endpoint for real-time events

#### API Endpoints
| Endpoint | Module | Description |
|----------|--------|-------------|
| `GET /api/status` | `status.py` | System metrics snapshot |
| `GET /api/health` | `status.py` | Health checks (green/yellow/red) |
| `GET /api/logs?level=&module=&search=&after=&limit=` | `logs.py` | Query JSON log file |
| `GET /api/brain/graph` | `brain.py` | Knowledge graph nodes + edges |
| `GET /api/conversations` | `conversations.py` | Conversation list |
| `GET /api/settings` | `settings.py` | Current engine config |
| `PUT /api/settings` | `settings.py` | Update mutable settings |
| `WS /ws?token=` | `server.py` | Real-time event stream |

#### Logs (`logs.py`)
- `query_logs()` — reads JSON log file, applies filters, normalizes schema
- Schema normalization: `ts→timestamp`, `severity→level`, `message→event`, `module→logger`
- Rotation resilience: retry + `.1` backup fallback
- `after` parameter for incremental polling

#### Events (`events.py`)
- `DashboardEventBridge` — subscribes to EventBus, broadcasts to WebSocket clients
- Maps engine events to dashboard-friendly JSON payloads

### Frontend

#### Key Files
- `src/types/api.ts` — TypeScript types mirroring backend (LogEntry, WsEvent, SystemStatus, etc.)
- `src/stores/dashboard.ts` — Zustand store with slices (logs, events, status, health, brain, conversations)
- `src/hooks/use-websocket.ts` — WebSocket auto-reconnect + debounced API refreshes
- `src/lib/api.ts` — Centralized API client with auth + error handling

#### LogEntry Schema (frontend expectation)
```typescript
interface LogEntry {
  timestamp: string;
  level: "DEBUG" | "INFO" | "WARNING" | "ERROR" | "CRITICAL";
  logger: string;
  event: string;
  [key: string]: unknown;
}
```

#### WS Events vs Logs
- WS events → `recentEvents` store (activity feed)
- Logs → `logs` store (fed exclusively by `/api/logs` polling every 5s)
- They are SEPARATE data sources. Never mix.

**⚠️ Trap:** Dashboard is a git submodule. Commit dashboard changes in `dashboard/` first, then `git add dashboard` in the main repo.

---

## Observability

**Files:** `src/sovyx/observability/`

### Logging (`logging.py`)
- `setup_logging(config)` — configures structlog + stdlib handlers
  - Thread-safe (threading.Lock)
  - Idempotent (can call multiple times safely)
  - Console: `config.console_format` renderer ("text" or "json")
  - File: ALWAYS JSON (RotatingFileHandler, 10MB, 3 backups)
  - Suppresses httpx/httpcore/urllib3/hpack to WARNING
- `get_logger(name)` — returns structlog BoundLogger
- `bind_request_context()` / `clear_request_context()` — async context vars for request tracing
- `SecretMasker` — processor that redacts sensitive field values

### Health (`health.py`)
- Health check system with green/yellow/red status
- Checks: database connectivity, brain service, LLM availability, disk space

### Alerts (`alerts.py`)
- Alert rules based on health status changes and SLO violations

---

## Persistence

**Files:** `src/sovyx/persistence/`

### Manager (`manager.py`)
- `DatabaseManager` — manages SQLite pools for system.db + per-mind brain.db/conversations.db
- WAL mode, MMAP, connection pooling

### Schemas (`schemas/`)
- SQL schema definitions for concepts, episodes, relations, conversations

### Default Paths
- `~/.sovyx/system.db` — global system database
- `~/.sovyx/{mind_name}/brain.db` — per-mind knowledge graph
- `~/.sovyx/{mind_name}/conversations.db` — per-mind conversation history

---

## LLM Router

**Files:** `src/sovyx/llm/`

### Router (`router.py`)
- Multi-provider routing: Anthropic > OpenAI > Google > Ollama
- Auto-detection from env vars (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, etc.)
- Fallback chain on failure

### Circuit Breaker (`circuit.py`)
- Per-provider circuit breaker (open/half-open/closed)
- Prevents hammering a down provider

### Cost Tracking (`cost.py`)
- Token-level cost calculation per provider/model

---

## CLI

**Files:** `src/sovyx/cli/`

### Commands
- `sovyx init [name]` — creates `~/.sovyx/`, system.yaml, mind.yaml, logs/
- `sovyx start [-f]` — starts daemon (foreground mode with -f)
- `sovyx stop` — stops daemon via RPC
- `sovyx status` — shows daemon status
- `sovyx logs [--level] [--search] [--follow]` — reads JSON log file
- `sovyx token` — shows dashboard auth token

### RPC Client (`rpc_client.py`)
- Unix socket communication with running daemon
- Protocol in `engine/rpc_protocol.py`

---

## Architectural Decisions

### ADR-001: structlog over stdlib logging
- **Decision:** structlog with JSON file output + colored console
- **Rationale:** Structured fields for dashboard queries, context vars for request tracing
- **Consequence:** All logging via `get_logger()`, never raw `logging`

### ADR-002: SQLite over PostgreSQL
- **Decision:** aiosqlite with WAL mode, connection pooling
- **Rationale:** Zero-config, embedded, sufficient for single-user companion
- **Consequence:** No concurrent write scaling, but WAL handles read concurrency

### ADR-003: ONNX Runtime for embeddings
- **Decision:** Local e5-small-v2 model via ONNX Runtime
- **Rationale:** No API dependency, fast inference, works offline, 384-dim vectors
- **Consequence:** ~90MB model download on first use, enterprise downloader handles rate limits

### ADR-004: Pydantic Settings for config
- **Decision:** `pydantic-settings` with env prefix SOVYX_
- **Rationale:** Type-safe config, env var override, YAML merge
- **Consequence:** Nested env vars use `__` delimiter (e.g., `SOVYX_LOG__LEVEL`)

### ADR-005: Dashboard as git submodule
- **Decision:** React dashboard in separate directory, committed as submodule
- **Rationale:** Independent build pipeline, separate CI jobs
- **Consequence:** Must commit dashboard changes before main repo changes

### ADR-006: console_format vs file format (v0.5.24)
- **Decision:** Console format is configurable ("text"/"json"), file is ALWAYS JSON
- **Rationale:** v0.5.22 bug where `format: "json"` default made console unreadable
- **Consequence:** `LoggingConfig.console_format` (not `format`), backward compat migration

### ADR-007: Log file path relative to data_dir (v0.5.24)
- **Decision:** `log_file` resolved by EngineConfig model_validator from `data_dir`
- **Rationale:** `SOVYX_DATA_DIR=/custom` must put logs at `/custom/logs/sovyx.log`
- **Consequence:** `LoggingConfig.log_file` defaults to None, resolved at EngineConfig level

---

## Bug Post-Mortems

### PM-001: Circular Import (v0.5.18)
- **Symptom:** `pip install sovyx && python -c "import sovyx"` → ImportError
- **Chain:** `engine.events` → `observability.logging` → `observability.__init__` → `observability.alerts` → `engine.events`
- **Root cause:** Eager imports in `observability/__init__.py`
- **Fix:** `__getattr__` lazy loading in `__init__.py`
- **Bonus fix:** Removed 2 test files that injected `sys.modules` stubs (test_consolidation_batching, test_scoring_pipeline)
- **Lesson:** Never add eager imports to `observability/__init__.py`

### PM-002: Console JSON Dump (v0.5.22–v0.5.23)
- **Symptom:** `sovyx start -f` showed raw JSON instead of colored console logs
- **Chain:** `LoggingConfig.format` default was `"json"` → `setup_logging()` used JSON renderer for console
- **Root cause:** Field name `format` was ambiguous — controlled only console but name suggested all output
- **Fix (v0.5.24):** Renamed to `console_format`, default `"text"`. File handler always JSON.
- **Lesson:** Config field names must be unambiguous. Band-aid fixes (manual override in bootstrap) indicate the real problem isn't solved.

### PM-003: Dashboard Empty Logs (v0.5.22)
- **Symptom:** Dashboard logs page showed "0 entries" with daemon running
- **Chain:** `LoggingConfig.log_file` was `None` by default → no file handler → no JSON log file → API returned `[]`
- **Root cause:** `setup_logging()` was never called in bootstrap, and `log_file` had no sensible default
- **Fix (v0.5.24):** `log_file` resolved from `data_dir` via model_validator, `setup_logging()` called in bootstrap
- **Lesson:** End-to-end tests would have caught this — unit tests passed because each component worked in isolation

---

## Version History

| Version | Date | Changes |
|---------|------|---------|
| v0.5.24 | 2026-04-09 | **Logs hardening** — 11 tasks: console_format rename, log_file relative to data_dir, registry resolution, idempotent setup, httpx suppression, sovyx init logs dir, incremental polling, WS/logs separation, rotation safety, schema normalization, e2e tests |
| v0.5.23 | 2026-04-09 | Console format band-aid (superseded by v0.5.24) |
| v0.5.22 | 2026-04-09 | Log file default + setup_logging in bootstrap (caused console regression) |
| v0.5.21 | 2026-04-09 | Dashboard: removed bouncing dots from empty conversations |
| v0.5.20 | 2026-04-09 | Dashboard: static empty state for conversation select panel |
| v0.5.19 | 2026-04-09 | Enterprise model downloader (Retry-After, mirrors, jitter, cooldown) |
| v0.5.18 | 2026-04-09 | Fixed circular import in observability/__init__ (lazy loading) |
| v0.5.17 | 2026-04-08 | Scoring refinement mission complete (15/15 tasks) |
| v0.5.16 | 2026-04-07 | Dynamic importance mission complete (18/18 tasks) |
| v0.5.15 | 2026-04-06 | Brain semantic enrichment mission complete (10/10 tasks) |
