# Module: dashboard

## What it does

The Sovyx dashboard is a React SPA served by the daemon, backed by a FastAPI application that exposes REST endpoints, a WebSocket event stream, and a Prometheus metrics endpoint. The backend lives in `src/sovyx/dashboard/` and the frontend in the `dashboard/` submodule. Auth is a single bearer token — generated on first start and stored in `~/.sovyx/token` with `0600` permissions — used both for REST (`Authorization: Bearer …`) and for the WebSocket (`/ws?token=…`).

## Key components

### Backend (`src/sovyx/dashboard/`)

| Name | Responsibility |
|---|---|
| `create_app(token=..., registry=...)` | FastAPI app factory. Mounts routers, middleware, and SPA fallback. |
| `ConnectionManager` | Tracks WebSocket clients and broadcasts JSON messages. |
| `DashboardEventBridge` | Subscribes to the internal `EventBus` and forwards events as WS payloads. |
| `StatusCollector` | Aggregates health, cost, latency, and counters into a single snapshot. |
| `DailyStatsRecorder` | Persists daily aggregates (cost, tokens, messages). |
| `RateLimitMiddleware` | Sliding-window rate limit per IP/route for `/api/chat` and `/api/import`. |
| `RequestIdMiddleware` / `SecurityHeadersMiddleware` | `X-Request-Id` correlation and CSP-style response headers. |

REST routers live in one file per domain: `brain.py`, `conversations.py`, `chat.py`, `activity.py`, `logs.py`, `plugins.py`, `voice_status.py`, `config.py`, `settings.py`, `daily_stats.py`, `export_import.py`.

### Frontend (`dashboard/src/`)

| Area | Contents |
|---|---|
| `main.tsx` / `App.tsx` / `router.tsx` | Entry point (imports `./lib/i18n` before the app), providers, React Router v7. |
| `pages/` | 12 route pages. |
| `components/` | Layout, chat, settings, and shadcn/ui v4 primitives. |
| `stores/dashboard.ts` + `stores/slices/` | Zustand root store composed from slices. |
| `hooks/` | `useAuth`, `useWebSocket` (300 ms debounce, exponential reconnect), `useMobile`, `useOnboarding`. |
| `lib/api.ts` | Centralized fetch client — attaches the bearer token and normalizes errors. |
| `lib/i18n.ts` | `i18next` setup with namespaces. |
| `types/api.ts` | ~20 TypeScript interfaces that mirror backend schemas. |

## Creating the app

```python
# src/sovyx/dashboard/server.py
TOKEN_FILE = Path.home() / ".sovyx" / "token"


def _ensure_token() -> str:
    """Read or generate the dashboard auth token."""
    if TOKEN_FILE.exists():
        token = TOKEN_FILE.read_text().strip()
        if token:
            return token
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    TOKEN_FILE.write_text(token)
    TOKEN_FILE.chmod(0o600)
    return token


app = create_app(
    token=_ensure_token(),
    registry=service_registry,   # resolves EngineConfig, EventBus, BrainService, etc.
)
```

Tests should pass a fixed token to `create_app` instead of patching globals:

```python
from fastapi.testclient import TestClient
from sovyx.dashboard.server import create_app

TOKEN = "test-token"
app = create_app(token=TOKEN)
client = TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"})
```

## REST endpoints (32)

| Group | Endpoints |
|---|---|
| Health / status | `/api/status`, `/api/health`, `/api/stats/history` |
| Conversations | `/api/conversations`, `/api/conversations/{id}` |
| Brain | `/api/brain/graph`, `/api/brain/search` |
| Logs | `/api/logs` |
| Activity | `/api/activity/timeline` |
| Settings / config | `/api/settings`, `/api/config` |
| Voice | `/api/voice/status`, `/api/voice/models` |
| Plugins | `/api/plugins`, `/api/plugins/{name}`, `/api/plugins/tools`, `/api/plugins/{name}/{enable|disable|reload}` |
| Channels | `/api/channels`, `/api/channels/telegram/setup` |
| Chat | `/api/chat` |
| Data | `/api/export`, `/api/import` |
| Safety | `/api/safety/{stats,status,history,rules}` |
| Providers | `/api/providers` |
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
| `ChannelConnected` / `ChannelDisconnected` | Channel state change. |
| `PluginStateChanged` | Plugin enabled/disabled/reloaded or auto-disabled. |

## Pages

| Page | Route |
|---|---|
| Overview | `/` |
| Conversations | `/conversations` |
| Brain | `/brain` |
| Logs | `/logs` |
| Settings | `/settings` (10 tabs) |
| Plugins | `/plugins` |
| Chat | `/chat` |
| About | `/about` |
| NotFound | `/not-found` |
| Voice | `/voice` |
| Emotions | `/emotions` |
| Productivity | `/productivity` |

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
  host: 127.0.0.1
  port: 4242
  token_file: ~/.sovyx/token
  cors_origins: ["http://localhost:5173"]
  rate_limit:
    chat: "60/minute"
    import: "5/minute"
```

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
