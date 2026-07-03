# Module: dashboard

## What it does

The Sovyx dashboard is a React SPA served by the daemon, backed by a FastAPI application that exposes REST endpoints, a WebSocket event stream, and a Prometheus metrics endpoint. The backend lives in `src/sovyx/dashboard/` and the frontend in `dashboard/` (part of the main repo, not a submodule). Auth is a single bearer token — generated on first start and stored in `~/.sovyx/token` with `0600` permissions — used both for REST (`Authorization: Bearer …`) and for the WebSocket (`/ws?token=…`).

## Key components

### Backend (`src/sovyx/dashboard/`)

| Name | Responsibility |
|---|---|
| `create_app(config=None, *, token=None)` | FastAPI app factory. Mounts routers, middleware, and SPA fallback. |
| `DashboardServer(config, registry)` | Uvicorn lifecycle wrapper — calls `create_app` and wires the `ServiceRegistry` onto `app.state`. |
| `ConnectionManager` | Tracks WebSocket clients and broadcasts JSON messages. |
| `DashboardEventBridge` | Subscribes to the internal `EventBus` and forwards events as WS payloads. |
| `StatusCollector` | Aggregates health, cost, latency, and counters into a single snapshot. |
| `DailyStatsRecorder` | Persists daily aggregates (cost, tokens, messages). |
| `RateLimitMiddleware` | Sliding-window rate limit per IP/route on every `/api/*` path. |
| `RequestIdMiddleware` / `SecurityHeadersMiddleware` | `X-Request-Id` correlation and CSP-style response headers. |

REST routers live in one file per domain under `src/sovyx/dashboard/routes/` — 33 `APIRouter` modules, grouped roughly as: core (`status`, `chat`, `conversations`, `conversation_import`, `data`, `logs`, `activity`, `emotions`, `brain`, `mind`, `settings`, `config`, `setup`, `onboarding`, `safety`, `channels`, `plugins`, `telemetry`, `websocket`), LLM (`providers`, `llm_health`), engine/observability (`engine_degraded`, `engine_resources`, `observability`), and voice (`voice`, `voice_test`, `voice_health`, `voice_calibration`, `voice_kb`, `voice_kb_contribute`, `voice_platform_diagnostics`, `voice_training`, `voice_wizard`), plus a shared `_deps.py` for the `verify_token` dependency. `server.py` wires the routers and middleware, and also defines the `/assets` static mount and the `/{path:path}` SPA-fallback endpoint itself (integrity-gated — see [dashboard-distribution-integrity](./dashboard-distribution-integrity.md)).

### Frontend (`dashboard/src/`)

| Area | Contents |
|---|---|
| `main.tsx` / `App.tsx` / `router.tsx` | Entry point (imports `./lib/i18n` before the app), providers, React Router v7. |
| `pages/` | 18 route pages. |
| `components/` | Layout, chat, settings, and shadcn/ui v4 primitives. |
| `stores/dashboard.ts` + `stores/slices/` | Zustand root store composed from slices. |
| `hooks/` | `useAuth`, `useWebSocket` (300 ms debounce, exponential reconnect), `useMobile`, `useOnboarding`. |
| `lib/api.ts` | Centralized fetch client — attaches the bearer token and normalizes errors. |
| `lib/i18n.ts` | `i18next` setup with namespaces. |
| `types/api.ts` | ~20 TypeScript interfaces that mirror backend schemas. |

## Creating the app

`create_app(config=None, *, token=None)` builds the FastAPI app; when `token` is `None` it reads or generates `TOKEN_FILE` (`~/.sovyx/token`, a module constant in `server.py`). In production the app is created by `DashboardServer`, which wires the `ServiceRegistry` onto `app.state` — there is no module-level `app`:

```python
# src/sovyx/dashboard/server.py (DashboardServer.start, simplified)
server = DashboardServer(config=api_config, registry=service_registry)
await server.start()
# start() does:
#   self._app = create_app(self._config)
#   self._app.state.registry = self._registry          # resolves EngineConfig, EventBus, ...
#   self._app.state.status_collector = StatusCollector(self._registry)
#   self._app.state.health_registry = ...              # shared engine HealthRegistry
```

Tests should pass a fixed token to `create_app` instead of patching globals:

```python
from fastapi.testclient import TestClient
from sovyx.dashboard.server import create_app

TOKEN = "test-token"
app = create_app(token=TOKEN)
client = TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"})
```

## REST endpoints

The canonical list lives in [`api-reference.md`](../api-reference.md). Grouped here by router:

| Router | Endpoints |
|---|---|
| `status` | `/api/status`, `/api/health`, `/api/stats/history` |
| `conversations` / `conversation_import` | `/api/conversations`, `/api/conversations/{id}`, `/api/import/conversations`, `/api/import/{job_id}/progress` |
| `brain` | `/api/brain/graph`, `/api/brain/search`, `/api/brain/search/vector` |
| `logs` | `/api/logs`, `/api/logs/stream` |
| `activity` | `/api/activity/timeline` |
| `emotions` | `/api/emotions/*` |
| `settings` / `config` | `/api/settings`, `/api/config` |
| `voice` + `voice_*` submodules | `/api/voice/*` (incl. `/api/voice/test/*`, `/api/voice/health/*`, `/api/voice/calibration/*`, `/api/voice/kb/*`, `/api/voice/wizard/*`, `/api/voice/training/*`, `/api/voice/platform-diagnostics/*`, `WS /api/voice/test/input`) |
| `mind` | `/api/mind/*` (forget, retention, wake-word toggle, …) |
| `engine_degraded` / `engine_resources` | `/api/engine/*` (degraded store + ack, resource cohorts, snapshots) |
| `llm_health` | `/api/llm/health`, `/api/llm/test-connection` |
| `observability` | `/api/observability/*` |
| `plugins` | `/api/plugins`, `/api/plugins/{name}`, `/api/plugins/tools`, `/api/plugins/{name}/{enable|disable|reload}` |
| `channels` | `/api/channels`, `/api/channels/telegram/setup` |
| `chat` | `POST /api/chat`, `POST /api/chat/stream` |
| `data` | `/api/export`, `/api/import` |
| `safety` | `/api/safety/{stats,status,history,rules}` |
| `providers` | `/api/providers` |
| `telemetry` | `/api/telemetry/*` |
| `onboarding` / `setup` | `/api/onboarding/*`, `/api/setup/*` |
| `websocket` | `WS /ws?token=...` |
| Infra | `/metrics` (Prometheus), `/{path:path}` (SPA fallback → `index.html`) |

## WebSocket events (12)

Connect to `/ws?token=<token>`. The `DashboardEventBridge` subscribes to the internal `EventBus` and rewrites events into `{type, timestamp, correlation_id, data}` payloads.

| Event | Source trigger |
|---|---|
| `EngineStarted` | Daemon finished bootstrap. |
| `EngineStopping` | Daemon shutdown begins. |
| `ServiceHealthChanged` | Health of a service transitions (green/yellow/red). |
| `PerceptionReceived` | New inbound message entered the cognitive loop. |
| `ThinkCompleted` | LLM Think phase finished — tokens in/out, model, cost. |
| `ResponseSent` | Channel delivered a response. |
| `ConceptCreated` | New concept stored in the Brain. |
| `EpisodeEncoded` | Episode encoded into memory. |
| `ConsolidationCompleted` | Consolidation pass finished (merged, pruned, strengthened). |
| `DreamCompleted` | Nightly dream cycle finished (patterns, concepts, relations). |
| `ChannelConnected` / `ChannelDisconnected` | Channel state change. |

## Pages

18 route pages under `dashboard/src/pages/`:

| Page | Route |
|---|---|
| Overview | `/` |
| Onboarding | `/onboarding` |
| Chat | `/chat` |
| Conversations | `/conversations` |
| Brain | `/brain` |
| Emotions | `/emotions` |
| Productivity | `/productivity` |
| Logs | `/logs` |
| Settings | `/settings` (also `/settings/providers`, `/settings/voice`) |
| Plugins | `/plugins` |
| About | `/about` |
| Voice | `/voice` |
| Voice Health | `/voice/health` |
| Voice Platform Diagnostics | `/voice/platform-diagnostics` |
| Engine Resources | `/engine/resources` |
| Heap Snapshot | `/engine/resources/heap-snapshot/:ts` |
| Thread Snapshot | `/engine/resources/thread-snapshot/:ts` |
| NotFound | `*` (catch-all) |

Logs uses `@tanstack/react-virtual` for the log viewer, Brain uses `react-force-graph-2d` for the knowledge graph, and charts are drawn with `recharts`. A Cmd+K command palette is provided via `cmdk`.

## Type alignment

The frontend types in `dashboard/src/types/api.ts` mirror backend schemas.

```ts
export type HealthStatus = "green" | "yellow" | "red";

export interface HealthCheck {
  name: string;
  status: HealthStatus;
  message: string;
  latency_ms?: number;
}

export interface LogEntry {
  timestamp: string;
  level: string;
  logger: string;
  event: string;
}
```

The backend normalizes structlog output before sending: `ts → timestamp`, `severity → level`, `message → event`, `module → logger`.

## Configuration

```yaml
api:
  enabled: true
  host: 127.0.0.1
  port: 7777
  cors_origins: ["http://localhost:7777"]
```

The token file path is not configurable — it is the module constant `TOKEN_FILE = ~/.sovyx/token` in `server.py`. Rate limits are also hardcoded in `rate_limit.py` (60 s sliding window): per-endpoint `/api/chat` 20, `/api/import` 10, `/api/export` 5 requests/window; every other `/api/*` path defaults to 120 (GET) or 30 (POST/PUT/PATCH/DELETE).

## Roadmap

- Fill the `Voice`, `Emotions`, and `Productivity` pages — endpoints exist, UI is a placeholder.
- Complete the Providers and Safety forms in Settings.
- Telegram OAuth flow in `/api/channels/telegram/setup`.
- Toast-level error handling for plugin enable/disable/reload buttons.
- i18n namespace consistency across every page.

## See also

- Source: `src/sovyx/dashboard/server.py`, `src/sovyx/dashboard/events.py`, `src/sovyx/dashboard/status.py`, router files.
- Frontend: `dashboard/src/main.tsx`, `dashboard/src/stores/dashboard.ts`, `dashboard/src/lib/api.ts`, `dashboard/src/types/api.ts`.
- Related modules: [`engine`](./engine.md) for the `EventBus` and `ServiceRegistry`, [`plugins`](./plugins.md) for the `/api/plugins` surface, [`voice`](./voice.md) for `/api/voice/status`.
