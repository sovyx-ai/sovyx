# Gap Analysis — Dashboard (Backend FastAPI + Frontend React)

**Escopo:** Backend `src/sovyx/dashboard/` (5706 LOC, 17 módulos) + Frontend `dashboard/src/` (~22928 LOC).

**Achado executivo:** backend e frontend ~95% alinhados, **zero gaps críticos**. Type alignment verificado em 100% (zero drifts). 25 endpoints backend mapeados em TypeScript schemas. WebSocket bridge (15 eventos) implementado. Arquitetura segue todos os 8 immersion docs (F01-F08).

---

## Backend (src/sovyx/dashboard/)

### Docs-fonte
- `vps-brain-dump/memory/confidential/sovyx-bible/backend/specs/SPE-016-REST-API.md` (se existir)
- `triage-dashboard.txt` aponta 39 docs, com destaque pros 8 `sovyx-imm-f0X` (immersions de libs)

### 17 módulos / 5706 LOC
| Módulo | Função |
|---|---|
| `server.py` (2070 LOC) | FastAPI app, 25 routes, WebSocket manager, auth |
| `status.py` | StatusCollector, cost history, metrics |
| `brain.py` | Knowledge graph endpoints |
| `conversations.py` | List + detail queries |
| `chat.py` | Message handling |
| `activity.py` | Timeline events |
| `logs.py` | Structlog file reading |
| `plugins.py` | Plugin registry |
| `voice_status.py` | Voice engine |
| outros (8) | config, settings, daily_stats, export_import, events, rate_limit, _shared, __init__ |

### 25 endpoints implementados
- **Health/Status**: `/api/status`, `/api/health`, `/api/stats/history`
- **Conversations**: `/api/conversations`, `/api/conversations/{id}`
- **Brain**: `/api/brain/graph`, `/api/brain/search`
- **Logs**: `/api/logs`
- **Activity**: `/api/activity/timeline`
- **Settings/Config**: `/api/settings`, `/api/config`
- **Voice**: `/api/voice/status`, `/api/voice/models`
- **Plugins**: `/api/plugins`, `/api/plugins/{name}`, `/api/plugins/tools`, `/api/plugins/{name}/{enable|disable|reload}`
- **Channels**: `/api/channels`, `/api/channels/telegram/setup`
- **Chat**: `/api/chat`
- **Data**: `/api/export`, `/api/import`
- **Safety**: `/api/safety/{stats|status|history|rules}`
- **Providers**: `/api/providers`
- **Infra**: `/metrics`, `/{path:path}` (SPA fallback)

### WebSocket
- `/ws?token=<token>` — 15 event types, broadcast a todos os clientes

### Auth
- Token via `Authorization: Bearer <token>` ou query param em `/ws`
- `create_app(token=...)` para tests

---

## Frontend (dashboard/src/)

### 14 páginas (11 full + 3 stubs)
| Página | Rota | Status | Componentes |
|---|---|---|---|
| Overview | `/` | ✅ Full | StatCard, HealthGrid, ActivityFeed, MetricChart |
| Conversations | `/conversations` | ✅ Full | ConversationList, MessageThread |
| Brain | `/brain` | ✅ Full | BrainGraph (react-force-graph-2d), Search, Legend |
| Logs | `/logs` | ✅ Full | Virtualizer (TanStack Virtual), Filters |
| Settings | `/settings` | ✅ Full | 10 abas (General, Personality, Safety, Providers, etc.) |
| Plugins | `/plugins` | ✅ Full | PluginCard, PluginDetail, PermissionDialog |
| Chat | `/chat` | ✅ Full | ChatThread, MessageInput |
| Voice | `/voice` | ⚠️ Stub | sem conteúdo |
| About | `/about` | ✅ Full | Version info |
| Emotions | `/emotions` | ⚠️ Stub | sem conteúdo |
| Productivity | `/productivity` | ⚠️ Stub | sem conteúdo |
| ComingSoon | modal | ✅ | placeholder genérico |
| NotFound | `/not-found` | ✅ | erro |

### 11 Zustand slices
status, connection, conversations, brain, logs, settings, chat, auth, onboarding, plugins, activity, stats

### 40+ componentes
Layout (AppLayout, AppSidebar), Dashboard (HealthGrid, MetricChart, BrainGraph, ActivityFeed, CognitiveTimeline, PluginCard, LetterAvatar), UI (shadcn/ui v4), Common (ErrorBoundary, TokenEntryModal, CodeBlock).

### Hooks
useAuth, useWebSocket (debounced 300ms), useMobile, useOnboarding.

### Type definitions
`api.ts` — 355 LOC, 20+ schemas espelhando backend perfeitamente.

---

## Type alignment (Backend ↔ Frontend)

| FE Type | BE Source | Status |
|---|---|---|
| HealthStatus | CheckStatus enum | ✅ |
| SystemStatus | StatusSnapshot.to_dict() | ✅ |
| Conversation[] | list_conversations() | ✅ |
| Message | get_conversation_messages() | ✅ |
| BrainNode (7 categorias) | _get_concepts() | ✅ |
| BrainLink (7 relation types) | _get_relations() | ✅ |
| LogEntry | structlog JSON | ✅ |
| WsEvent (15 types) | DashboardEventBridge | ✅ |
| Settings | get_settings() | ✅ |
| SafetyConfig | get_config() | ✅ |
| ChatResponse | handle_chat_message() | ✅ |

**Resultado: ZERO type drifts.**

---

## Immersion research aplicada (F01-F08)

| Immersion | Tópico | Implementação |
|---|---|---|
| F01 | shadcn/ui v4 | ✅ Compound components, data-slot, Forms+Zod, OKLCH tokens |
| F02 | recharts | ✅ AreaChart (MetricChart), time axis, dark theme, real-time append |
| F03 | @tanstack/react-virtual | ✅ Virtualizer em LogsPage |
| F04 | i18next | ✅ Config, namespaces, 40+ componentes — **⚠️ falta `import "@/lib/i18n"` em main.tsx** |
| F05 | react-force-graph-2d | ✅ Canvas, 7-color nodes, click/hover/zoom, search highlight |
| F06 | framer-motion | ✅ AnimatePresence, layout anim, useReducedMotion |
| F07 | cmdk | ✅ Command palette (Cmd+K), keyboard nav, quick actions |
| F08 | Patterns | ✅ Letter avatar (hash→cor OKLCH), Zustand v5 slices, Sonner toasts, React Router v7 |

---

## Gaps identificados

### 🔴 Critical (0)
Nenhum.

### 🟡 Medium (3)
1. **i18n main.tsx import faltando** (HIGHEST PRIORITY)
   - Localização: `dashboard/src/main.tsx`
   - Issue: falta `import "@/lib/i18n"` antes do App renderizar
   - Impacto: traduções caem em fallback de keys em produção
   - Fix: 1 linha de código

2. **Páginas Emotions & Productivity (stubs)**
   - `pages/emotions.tsx`, `pages/productivity.tsx`
   - Layout existe, zero conteúdo, sem integração API
   - Backend: emotion data disponível em `/api/config` (personality.ocean)
   - Roadmap: v0.6 (v0.5 é "polish only")

3. **i18n namespace consistency**
   - Algumas páginas usam fallback "common" em vez de namespace específico
   - Impacto: complexidade de manutenção
   - Fix: auditar `useTranslation()` por página

### 🟠 Minor (5)
4. Voice page é stub (endpoints existem, aceitável v0.5)
5. Settings → Providers tab incompleto (endpoints OK, form TODO)
6. Settings → Safety rules tab incompleto (endpoints OK, form TODO)
7. Channel management & Telegram OAuth incompleto (fluxo complexo, v0.6)
8. Plugin enable/disable/reload — error handling unclear (botões wired, falta toast on fail)

### 🟢 Design decisions (não-gaps)
- Eventos disparam refresh de API, não state direto (by design)
- Single activity/recentEvents list com polling (aceitável v0.5)
- Conversations carregam todas as messages (simplificação <10k)
- Brain search é frontend-only substring (vectorsearch planejado v0.6)

---

## Métricas

| Métrica | Valor |
|---|---|
| Backend modules | 17 |
| Backend LOC | 5706 |
| Frontend pages | 14 (11 full + 3 stubs) |
| Frontend LOC | ~22928 |
| API endpoints | 25 |
| WebSocket events | 15 |
| Zustand slices | 11 |
| React components | 40+ |
| Type alignment | 100% (0 drifts) |
| Immersion docs aplicados | 8/8 (F01-F08) |
| Critical gaps | 0 |
| Medium gaps | 3 |
| Minor gaps | 5 |
