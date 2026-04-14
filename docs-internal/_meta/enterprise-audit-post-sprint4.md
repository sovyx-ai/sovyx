# Enterprise-Grade Audit — Post-Sprint 4 Delta

**Gerado em**: 2026-04-14 (pós-Sprints 1–4)
**Baseline**: `enterprise-audit-final.md` (258 arquivos · 76.7 % ENT · 8.42/10 avg)
**Método**: `audit_coverage.py` (doc-coverage mecânico) + delta qualitativo mapeando commits de Sprints 1–4 contra os issues catalogados.

---

## 1. Doc-coverage (mecânico — audit_coverage.py)

| Categoria | Total | Documentado | % |
|---|---:|---:|---:|
| Classes | 517 | 203 | 39.3 % |
| Funções top-level | 196 | 19 | 9.7 % |
| Métodos públicos | 968 | 148 | 15.3 % |
| **TOTAL** | **1 681** | **370** | **22.0 %** |

O script mede apenas presença de símbolo em `docs/`. Cobertura caiu artificialmente porque `docs/` foi reescrito pra público durante Sprint-prep; os docs detalhados ficaram em `docs-internal/`. Esse número **não reflete qualidade de código** — é um proxy de superfície pública documentada.

---

## 2. Score enterprise — delta estimado

A pontuação 0–10 original é LLM-judged por arquivo (10 critérios × 258 arquivos). Não é reexecutável num único turn. O delta abaixo vem de mapear cada commit Sprint 1–4 contra os issues catalogados no baseline.

### Backend (165 arquivos)

| Módulo | Score antes | Score estimado | Delta | O que mudou |
|---|---:|---:|---:|---|
| cli | 7.5 | **8.2** | +0.7 | path-traversal fix (S1), BLE001 sweep (S2) |
| brain | 7.8 | **8.5** | +0.7 | hardcoded constants → `EngineConfig.tuning.brain` (S3), `service.py`/`embedding.py` god files quebrados (S2) |
| cognitive | 8.0 | **8.6** | +0.6 | `safety_patterns.py`, `safety_classifier.py`, `reflect.py` god files quebrados (S2), constants → config (S3), BLE001 swept |
| voice | 8.09 | **8.7** | +0.6 | Wyoming auth (S1), sync ONNX → `asyncio.to_thread()` (S3), `pipeline.py` split em subpackage (S2), `voice` constants → config (S3) |
| plugins | 8.4 | **8.8** | +0.4 | Official plugins → `SandboxedHttpClient` (S1), AST scanner gaps (S1), `manager.py` helpers extraídos (S2) |
| llm | 8.4 | **8.6** | +0.2 | Google header fix (S1); pricing-unification **ainda não foi feito** |
| dashboard (BE) | 8.6 | **9.0** | +0.4 | `server.py` 2134 LOC → `routes/` subpackage (S2), size cap import (S1), chat max length (S1) |
| engine | 8.6 | **8.7** | +0.1 | LRULockDict promovido p/ `engine/_lock_dict.py` (S3) |
| observability | 9.1 | **9.2** | +0.1 | alerts lock + BatchSpanProcessor foram sinalizados S3 mas não todos feitos |
| cloud | 9.2 | **9.3** | +0.1 | `defaultdict(asyncio.Lock)` → `LRULockDict` em `flex.py` + `usage.py` (S3) |
| persistence | 9.2 | 9.2 | 0 | `_read_index` race ainda aberto |
| upgrade | 9.1 | 9.1 | 0 | Importers continuam ausentes (Sprint 8) |
| bridge | 9.0 | 9.0 | 0 | Relay/HA/CalDAV fora do escopo |
| mind | 9.25 | 9.25 | 0 | Emotional baseline (Sprint 5) |
| context | 9.5 | 9.5 | 0 | já referência |
| benchmarks | 9.3 | 9.3 | 0 | já referência |

**Backend estimado**: **~88 % ENT · 8.82/10** (antes: 78.2 % · 8.5/10).

### Frontend (93 arquivos)

| Grupo | Score antes | Score estimado | Delta | O que mudou |
|---|---:|---:|---:|---|
| **components/dashboard** | **7.2** | **8.6** | **+1.4** | `React.memo` nos 5 hot paths (S4A), 13 novos `*.test.tsx` (S4C) — 14 arquivos DEVELOPED caem pra ~4 |
| components/ui | 8.6 | 8.9 | +0.3 | chart, command, sidebar agora com teste (S4C) |
| components/auth | 8.0 | 8.8 | +0.8 | `fetch()` bruto em `token-entry-modal.tsx` migrado pra `apiFetch` (S4D) |
| components/settings | 8.0 | 8.7 | +0.7 | `fetch()` bruto + leitura de `localStorage` em `export-import.tsx` eliminados (S4D) |
| lib | 9.0 | 9.4 | +0.4 | `ApiOptions.schema` opt-in + `apiFetch` helper (S4B/S4D); **ainda falta timeout/retry/PATCH** |
| types | 9.0 | 9.7 | +0.7 | novo `src/types/schemas.ts` com zod schemas (S4B) |
| stores | 9.5 | 9.5 | 0 | mantido; passou a validar via schema no boundary |
| pages | 8.0 | 8.2 | +0.2 | logs/brain/chat/conversations agora validam schema; **plugins.tsx page-level test ainda falta** |
| hooks | 9.5 | 9.5 | 0 | use-auth já fail-closed; use-websocket também valida schemas |
| root (App/main/router) | 9.3 | 9.3 | 0 | `router.tsx` lazy + boundary test **não foi feito** |
| common | 9.5 | 9.5 | 0 | — |
| chat | 9.5 | 9.5 | 0 | — |
| layout | 9.25 | 9.25 | 0 | — |

**Frontend estimado**: **~89 % ENT · 8.9/10** (antes: 74.2 % · 8.28/10).

### Combinado

| Camada | Arquivos | ENT% (antes → agora) | Avg (antes → agora) |
|---|---:|---:|---:|
| Backend | 165 | 78.2 % → **~88 %** | 8.5 → **~8.82** |
| Frontend | 93 | 74.2 % → **~89 %** | 8.28 → **~8.9** |
| **Combinado** | **258** | **76.7 % → ~88 %** | **8.42 → ~8.85** |

**Movimento**: ~30 arquivos DEVELOPED migraram para ENTERPRISE. Nenhum arquivo regrediu.

---

## 3. O que ainda está < 8/10 (ou não-documentado) após Sprint 4

Itens fora do escopo dos 4 sprints executados, organizados por bloqueador:

### Backend — ainda abertos

- **`persistence/pool.py` `_read_index` race** — lock nunca foi adicionado (Sprint 3 item 6, não feito).
- **`observability/alerts.py` lock em `_metrics`/`_states`** (Sprint 3 item 5, não feito).
- **`observability/tracing.py` BatchSpanProcessor** (Sprint 3 item 9, não feito).
- **Pricing table duplicada em 5 providers** — unificação em `llm/pricing.py` (Sprint 3 item 8, não feito).
- **`cloud/backup.py` boto3 sync em `async def`** — `aioboto3` / `to_thread` wrap (Sprint 3 item 2, não feito).
- **Emotional 2D → 3D PAD migration** (Sprint 5).
- **CONSOLIDATE phase** no cognitive loop (Sprint 5).
- **DREAM phase** nightly (Sprint 5).
- **Streaming LLM → speculative TTS** (Sprint 5).
- **BYOK token isolation** multi-tenant (Sprint 5).
- **13 features comerciais** (Relay, Stripe Connect, 4 importers, Speaker Rec, HA, CalDAV, Pricing experiments, SMFExporter completo, InterMindBridge, CursorPagination).

### Frontend — ainda abertos

- **Virtualização `chat-thread.tsx` + `cognitive-timeline.tsx`** — `@tanstack/react-virtual` já no projeto, não foi aplicado nesses dois (Sprint 4 item 5, não feito).
- **`lib/api.ts` hardening** — default 30 s timeout + AbortController + retry backoff + PATCH + query-string helper tipado (Sprint 4 item 2, só zod foi feito).
- **Tests críticos ainda faltando**:
  - `pages/plugins.tsx` (363 LOC, sem test page-level)
  - `components/dashboard/command-palette` (Cmd+K handler)
  - `router.tsx` (lazy + ErrorBoundary wiring)
  - `pages/settings.tsx` slider/preset/save flows (hoje render-only)
- **ErrorBoundary `componentDidCatch` telemetry hook** (Sentry/PostHog).
- **Per-section error boundaries** em settings.tsx, plugins.tsx, chat.tsx.
- **i18n sweep** em aria-labels hardcoded (`plugins.tsx`, `plugin-card.tsx`, `letter-avatar.tsx`, `chat-bubble.tsx`, `channel-badge.tsx`).
- **`log-row.tsx`** `role="button"` + `tabIndex={0}` + `onKeyDown`.
- **`brain-graph.tsx`** listagem acessível fallback.
- **`ui/sidebar.tsx` cookie** `SameSite=Lax` + `Secure`.
- **`JSON.stringify` render clamp + secret redaction** em `plugin-detail.tsx` e `log-row.tsx`.

### 10 erros TS pré-existentes (não regredidos, mas continuam)
- `activity-feed.tsx:40` — Record completo de `WsEventType` (falta plugin events).
- `permission-dialog.tsx:37` — `lowCount` unused.
- `plugin-badges.tsx:197` — `styles` possivelmente undefined.
- `plugin-detail.tsx:46` — `PluginDetailType` import unused.
- `use-websocket.ts:268` — cast inseguro pra `PluginStateChangedEvent`.
- `plugins.tsx:256` — `isFiltering` unused.
- `settings.tsx:440,512` — acesso a `guardrails`/`pii_protection` que não existem em `SafetyConfig`.

---

## 4. Resumo — onde estávamos, onde estamos, o que falta pra 90 %

Entramos em 76.7 % ENT / 8.42 avg com 60 arquivos DEVELOPED, 9 security blockers P0 abertos e o `components/dashboard/` FE carregando 14/23 arquivos sub-enterprise. Sprints 1–4 executaram cirurgicamente contra essa lista: Sprint 1 fechou os 11 security P0, Sprint 2 quebrou 7 god files (`server.py`, `safety_patterns.py`, `safety_classifier.py`, `reflect.py`, `pipeline.py`, `manager.py`, `embedding.py`) e swept BLE001, Sprint 3 migrou ONNX sync p/ threads + hardcoded constants pra `EngineConfig.tuning` + `defaultdict(Lock)` pra LRU bounded, Sprint 4 levou o FE `components/dashboard/` de 7.2 pra ~8.6 com `React.memo` + 56 testes novos + zod runtime validation + `apiFetch` centralizado. Estimativa atual: **~88 % ENT / 8.85 avg** — ~30 arquivos migraram de DEVELOPED pra ENTERPRISE, zero regrediu. Pra cruzar 90 %, restam os ~8 itens específicos que ficaram fora do escopo dos 4 sprints: **`api.ts` hardening** (timeout/retry/PATCH), **virtualização chat-thread + cognitive-timeline**, **4 tests críticos** (plugins.tsx page-level, command-palette, router.tsx, settings interactions), **ErrorBoundary telemetry**, **JSON.stringify clamp + redaction**, **5 issues pendentes em observability/persistence/llm/cloud** (alerts lock, _read_index lock, pricing unification, boto3 async, BatchSpanProcessor), e **limpeza dos 10 erros TS pré-existentes**. Estimativa: 1.5–2 dias-dev concentrados para chegar em 90 %.
