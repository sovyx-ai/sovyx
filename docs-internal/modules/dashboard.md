# Módulo: dashboard

## Objetivo

Painel web oficial do Sovyx, distribuído como **SPA React servida pelo daemon**. Cobre dois subsistemas que são documentados em conjunto por estarem em co-projeto: **backend FastAPI** (`src/sovyx/dashboard/`, 17 módulos, 5706 LOC, 32 endpoints REST, 12 tipos de evento WebSocket) e **frontend React** (`dashboard/src/`, 12 páginas, 12 Zustand slices, 40+ componentes, ~23 kLOC).

**Estado atual: zero gaps críticos.** Type alignment backend ↔ frontend em 100%, com todos os 8 docs de immersion F01-F08 aplicados no código.

## Responsabilidades

### Backend (`src/sovyx/dashboard/`)

- **FastAPI app factory** — `create_app(token=..., registry=...)` monta todas as rotas.
- **Auth por Bearer token** — `Authorization: Bearer <token>` em REST, query param em `/ws?token=...`.
- **Token management** — gera ou lê `~/.sovyx/token` (chmod 0600).
- **ConnectionManager WebSocket** — broadcast assíncrono para todos os clientes conectados.
- **DashboardEventBridge** — subscribe no `EventBus` interno e traduz eventos para payloads JSON dashboard-friendly.
- **StatusCollector** — agrega métricas (custo, latência, health) para snapshot unificado.
- **Rate limiting** — limitador simples para evitar abuso do `/api/chat` e `/api/import`.
- **SPA fallback** — `/{path:path}` retorna `index.html` para client-side routing.

### Frontend (`dashboard/src/`)

- **Router** React Router v7 com rotas aninhadas.
- **Auth flow** — `TokenEntryModal` ao primeiro acesso, `useAuth` persiste em localStorage.
- **State global** — 12 Zustand slices composadas em `dashboard.ts`.
- **API client** — `lib/api.ts` centraliza fetch + auth headers + error handling.
- **WebSocket reativo** — `useWebSocket` com debounce 300 ms, reconnect exponencial.
- **i18n** — `i18next` com namespaces; `main.tsx` importa `./lib/i18n` antes do App.
- **Tema** — OKLCH tokens via shadcn/ui v4, dark mode, system-sync.

## Arquitetura

```
src/sovyx/dashboard/ (Backend — 17 módulos / 5706 LOC)
  ├── server.py          FastAPI app factory + ConnectionManager + auth + SPA fallback
  ├── events.py          DashboardEventBridge (EventBus → WebSocket)
  ├── status.py          StatusCollector + cost history
  ├── brain.py           knowledge graph endpoints
  ├── conversations.py   list + detail
  ├── chat.py            POST /api/chat
  ├── activity.py        timeline events
  ├── logs.py            structlog file tail/search
  ├── plugins.py         plugin registry endpoints
  ├── voice_status.py    voice engine status
  ├── config.py          config read/write
  ├── settings.py        get_settings / update_settings
  ├── daily_stats.py     stats aggregation
  ├── export_import.py   data portability
  ├── rate_limit.py      RateLimiter
  └── _shared.py         deps comuns

dashboard/src/ (Frontend — ~23 kLOC)
  ├── main.tsx           entry point; importa ./lib/i18n ANTES do App
  ├── App.tsx            top-level providers
  ├── router.tsx         React Router v7
  ├── pages/             12 páginas (9 full + 3 stubs: Voice, Emotions, Productivity)
  ├── components/        layout, dashboard, auth, chat, settings, ui (shadcn v4)
  ├── stores/
  │     ├── dashboard.ts root store
  │     └── slices/      12 slices
  ├── hooks/             useAuth, useWebSocket, useMobile, useOnboarding
  ├── lib/               api.ts, i18n.ts, format.ts, constants.ts, utils.ts
  ├── locales/           translations
  └── types/api.ts       20+ schemas espelhando backend
```

## Endpoints REST (32)

| Grupo | Endpoints |
|---|---|
| Health/Status | `/api/status`, `/api/health`, `/api/stats/history` |
| Conversations | `/api/conversations`, `/api/conversations/{id}` |
| Brain | `/api/brain/graph`, `/api/brain/search` |
| Logs | `/api/logs` |
| Activity | `/api/activity/timeline` |
| Settings/Config | `/api/settings`, `/api/config` |
| Voice | `/api/voice/status`, `/api/voice/models` |
| Plugins | `/api/plugins`, `/api/plugins/{name}`, `/api/plugins/tools`, `/api/plugins/{name}/{enable|disable|reload}` |
| Channels | `/api/channels`, `/api/channels/telegram/setup` |
| Chat | `/api/chat` |
| Data | `/api/export`, `/api/import` |
| Safety | `/api/safety/{stats,status,history,rules}` |
| Providers | `/api/providers` |
| Infra | `/metrics` (Prometheus), `/{path:path}` (SPA fallback) |

## WebSocket (12 eventos)

Endpoint: `/ws?token=<token>`. Broadcast para todos os clientes.

`ChannelConnected`, `ChannelDisconnected`, `ConceptCreated`, `ConsolidationCompleted`, `EngineStopping`, `EpisodeEncoded`, `PerceptionReceived`, `ResponseSent`, `ServiceHealthChanged`, `ThinkCompleted`, … (ver `dashboard/events.py` para lista completa).

## Páginas frontend (12)

| Página | Rota | Status |
|---|---|---|
| Overview | `/` | Full |
| Conversations | `/conversations` | Full |
| Brain | `/brain` | Full |
| Logs | `/logs` | Full |
| Settings | `/settings` | Full (10 tabs) |
| Plugins | `/plugins` | Full |
| Chat | `/chat` | Full |
| About | `/about` | Full |
| NotFound | `/not-found` | Full |
| Voice | `/voice` | Stub |
| Emotions | `/emotions` | Stub |
| Productivity | `/productivity` | Stub |

*(`ComingSoon` é component/modal reutilizado, não página roteada — não conta nas 12.)*

## Código real (exemplos curtos)

**`src/sovyx/dashboard/server.py`** — token management:

```python
TOKEN_FILE = Path.home() / ".sovyx" / "token"

def _ensure_token() -> str:
    if TOKEN_FILE.exists():
        token = TOKEN_FILE.read_text().strip()
        if token:
            return token
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    TOKEN_FILE.write_text(token)
    TOKEN_FILE.chmod(0o600)
    return token
```

**`src/sovyx/dashboard/events.py`** — bridge assíncrono:

```python
class DashboardEventBridge:
    """Bridge entre Engine EventBus e WebSocket clients."""

    def __init__(self, ws_manager: ConnectionManager, event_bus: EventBus) -> None:
        self._ws = ws_manager
        self._bus = event_bus
        self._subscribed = False
```

**`dashboard/src/main.tsx`** — entry point com i18n (já presente):

```tsx
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./lib/i18n";   // Initialize i18n BEFORE App
import "./index.css";
import App from "./App.tsx";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
```

**`dashboard/src/types/api.ts`** — tipos espelhando backend:

```ts
export type HealthStatus = "green" | "yellow" | "red";

export interface HealthCheck {
  name: string;
  status: HealthStatus;
  message: string;
  latency_ms?: number;
}

export interface HealthResponse {
  overall: HealthStatus;
  checks: HealthCheck[];
}
```

## Type alignment (100%, zero drifts)

| Frontend type | Backend source |
|---|---|
| `HealthStatus` | `CheckStatus` enum |
| `SystemStatus` | `StatusSnapshot.to_dict()` |
| `Conversation[]` | `list_conversations()` |
| `Message` | `get_conversation_messages()` |
| `BrainNode` (7 categorias) | `_get_concepts()` |
| `BrainLink` (7 relation types) | `_get_relations()` |
| `LogEntry` | structlog JSON |
| `WsEvent` (12 types) | `DashboardEventBridge` |
| `Settings` | `get_settings()` |
| `SafetyConfig` | `get_config()` |
| `ChatResponse` | `handle_chat_message()` |

## Immersion docs F01-F08 aplicados

| Doc | Lib / Tópico | Implementação |
|---|---|---|
| F01 | shadcn/ui v4 | Compound components, `data-slot`, Forms+Zod, OKLCH tokens |
| F02 | recharts | `MetricChart` AreaChart, time axis, dark theme, real-time append |
| F03 | @tanstack/react-virtual | `LogsPage` virtualizer |
| F04 | i18next | Config, namespaces, 40+ components — **import presente em `main.tsx`** |
| F05 | react-force-graph-2d | Canvas, 7-color nodes, click/hover/zoom, search highlight |
| F06 | framer-motion | `AnimatePresence`, layout anim, `useReducedMotion` |
| F07 | cmdk | Command palette (Cmd+K), keyboard nav, quick actions |
| F08 | Patterns | Letter avatar (hash→OKLCH), Zustand v5 slices, Sonner toasts, React Router v7 |

## Specs-fonte

- **SPE-016-REST-API** (se existir) — contrato dos 32 endpoints.
- **sovyx-imm-f01…f08** — 8 docs de immersion (shadcn, recharts, tanstack-virtual, i18next, force-graph-2d, framer-motion, cmdk, patterns).

## Status de implementação

| Item | Status |
|---|---|
| 32 endpoints REST | Aligned |
| 12 tipos de evento WebSocket | Aligned |
| `create_app(token=...)` (pattern recomendado para tests) | Aligned |
| ConnectionManager broadcast | Aligned |
| DashboardEventBridge (EventBus → WS) | Aligned |
| SPA fallback `/{path:path}` | Aligned |
| Rate limiting (`/api/chat`, `/api/import`) | Aligned |
| 9 pages full + 3 stubs | Aligned (stubs intencionais v0.5) |
| 12 Zustand slices | Aligned |
| `useWebSocket` com debounce 300 ms | Aligned |
| Type alignment BE ↔ FE | Aligned (100%, zero drifts) |
| Immersion docs F01-F08 aplicados | Aligned |
| `import "./lib/i18n"` em `main.tsx` | Aligned (presente — gap-analysis-D pode estar desatualizado) |

## Divergências

**Nenhuma crítica.** As 3 páginas stub (Voice, Emotions, Productivity) são intencionais: os endpoints backend existem, o layout está roteado, mas o conteúdo é placeholder com marker "v0.6 planned". Isso não caracteriza divergência, apenas roadmap explícito.

**Observação sobre gap-D** — `analysis-D-dashboard.md` (linha 128-131) lista "`import '@/lib/i18n'` missing em `main.tsx`" como gap medium. **Inspeção direta (2026-04-14) mostra o import PRESENTE** em `dashboard/src/main.tsx:3`. Gap provavelmente foi resolvido após a análise. Esta doc reflete o estado atual do código.

**Gaps minor não-bloqueantes (v0.6):**

- Voice page stub (endpoints OK).
- Settings → Providers/Safety tabs com forms incompletos.
- Channel management + Telegram OAuth flow.
- Plugin enable/disable/reload — error handling: botões wired, falta toast em fail paths.
- i18n namespace consistency — algumas pages usam fallback `"common"` em vez do namespace específico.

## Dependências

### Backend
- `fastapi`, `starlette`, `uvicorn` — server stack.
- `sovyx.engine.config.APIConfig`, `sovyx.engine.registry.ServiceRegistry`.
- `sovyx.engine.events.EventBus` — subscribe via `DashboardEventBridge`.
- `sovyx.observability.health.HealthRegistry` — `/api/health`.
- `sovyx.observability.metrics` — `/metrics` Prometheus export.

### Frontend
- React 19, TypeScript, Vite, Tailwind CSS v4, Zustand v5.
- `@tanstack/react-virtual`, `react-force-graph-2d`, `recharts`, `framer-motion`, `cmdk`, `sonner`.
- `i18next`, `react-i18next`.
- `react-router` v7.

## Testes

### Backend
- `tests/dashboard/` — 32 endpoints, WebSocket handshake, auth (token OK / token inválido / token ausente), adversarial tests.
- **Regra crítica (CLAUDE.md anti-pattern #10):** usar `create_app(token="...")` para testes — **nunca** monkeypatch `_ensure_token` ou setar `_server_token` global. O parâmetro `token` bypassa filesystem e state global.

### Frontend
- `vitest` colocado ao lado de cada page/component (`*.test.tsx`).
- Testes cobrem Zustand slices, hooks, components; mocks de API via `vi.mock("@/lib/api")`.
- `npx tsc -b tsconfig.app.json` deve passar com zero erros.
- A11y tests em `a11y.test.ts` e `a11y-expanded.test.ts`.

## Public API reference

### Public API
| Classe | Descrição |
|---|---|
| `DashboardServer` | Gerencia o ciclo de vida do uvicorn (start/stop) integrado ao Engine. |
| `ConnectionManager` | Registra conexões WebSocket e faz broadcast assíncrono. |
| `DashboardEventBridge` | Subscribe no EventBus → serializa → broadcast para WS clients. |
| `StatusCollector` | Agrega health, custo, latência, counters num `StatusSnapshot`. |
| `StatusSnapshot` | Snapshot unificado do estado do sistema (serializável). |
| `DashboardCounters` | Contadores in-memory (msgs, errors, tokens) para o status. |
| `DailyStatsRecorder` | Persiste agregados diários (custo, tokens, mensagens). |
| `RateLimitMiddleware` | Middleware starlette — sliding window por IP/rota. |
| `RequestIdMiddleware` | Gera/propaga `X-Request-ID` para correlação. |
| `SecurityHeadersMiddleware` | Adiciona headers (CSP, X-Content-Type-Options, etc). |

*(o app FastAPI é criado por `create_app(token=..., registry=...)` — factory, não classe — e agrega 32 endpoints REST via `APIRouter`s nos módulos `brain.py`, `conversations.py`, `chat.py`, etc.)*

### Errors
| Exception | Quando é raised |
|---|---|

*(sem exceptions dedicadas — endpoints propagam `HTTPException`; rate limit retorna 429; auth retorna 401)*

### Events (WebSocket broadcasts — 12 tipos)
| Event | Payload / trigger |
|---|---|
| `EngineStarted` | Engine bootou — payload vazio. |
| `EngineStopping` | Engine iniciando shutdown — `reason`. |
| `ServiceHealthChanged` | Health de um serviço mudou — `service`, `status`. |
| `PerceptionReceived` | Nova InboundMessage — `source`, `person_id`. |
| `ThinkCompleted` | Think phase concluída — `tokens_in/out`, `model`, `cost_usd`, `latency_ms`. |
| `ResponseSent` | Resposta enviada — `channel`, `latency_ms`. |
| `ConceptCreated` | Novo concept na Brain — `concept_id`, `title`, `source`. |
| `EpisodeEncoded` | Episode encodado — `episode_id`, `importance`. |
| `ConsolidationCompleted` | Consolidação concluída — `merged`, `pruned`, `strengthened`, `duration_s`. |
| `ChannelConnected` | Canal conectou — `channel_type`. |
| `ChannelDisconnected` | Canal desconectou — `channel_type`, `reason`. |

Eventos viajam como `{type, timestamp, correlation_id, data}` via `ws.broadcast()`. São 12 tipos no total: 11 subscritos em `DashboardEventBridge._subscribed` (do event bus) + `PluginStateChanged` broadcast diretamente. No frontend, alguns stores derivam eventos agregados (`BrainUpdated`, `PluginsChanged`, `VoiceStateChanged`, `LogAppended`) via subscriptions; esses são *fan-out* frontend, não eventos adicionais no backend.

### Configuration
| Config | Campo/Finalidade |
|---|---|
| `APIConfig` | Host, port, token path — reutilizado de `engine/config.py` (não há `DashboardConfig` dedicado). |

## Referências

### Backend
- `src/sovyx/dashboard/server.py` — app factory, ConnectionManager, auth, SPA fallback.
- `src/sovyx/dashboard/events.py` — DashboardEventBridge.
- `src/sovyx/dashboard/status.py` — StatusCollector.
- `src/sovyx/dashboard/{brain,conversations,chat,activity,logs,plugins,voice_status,config,settings,daily_stats,export_import,rate_limit,_shared}.py` — endpoints.

### Frontend
- `dashboard/src/main.tsx` — entry point + i18n import.
- `dashboard/src/App.tsx` — root providers.
- `dashboard/src/router.tsx` — React Router v7.
- `dashboard/src/pages/*.tsx` — 12 páginas.
- `dashboard/src/stores/dashboard.ts` + `slices/*.ts`.
- `dashboard/src/hooks/{use-auth,use-websocket,use-mobile,use-onboarding}.ts`.
- `dashboard/src/lib/{api,i18n,format,constants,utils}.ts`.
- `dashboard/src/types/api.ts` — 20+ schemas.

### Specs / Immersion
- SPE-016-REST-API — contrato REST.
- `sovyx-imm-f01` … `sovyx-imm-f08` — immersion docs (8).
- `docs/_meta/gap-inputs/analysis-D-dashboard.md` — análise completa de type alignment.
- `docs/_meta/gap-analysis.md` §dashboard — zero critical gaps.
- `CLAUDE.md` §Anti-Patterns #7 (LogEntry fields), #10 (`create_app(token=...)`).
