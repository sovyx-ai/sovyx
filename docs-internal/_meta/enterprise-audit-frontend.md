# Enterprise-Grade Audit — Sovyx Frontend (React Dashboard)

**Gerado em**: 2026-04-14 (FASE 2 de 4)
**Escopo**: 93 arquivos `.ts`/`.tsx` em `dashboard/src/` (sem arquivos de test)
**Método**: 4 agents paralelos, avaliando 10 critérios por arquivo, brutalmente honesto

**Detalhe por grupo**: `enterprise-audit-fe-part-{A,B,C,D}.md`.

---

## Score global do frontend

| Classificação | Arquivos | % |
|---|---:|---:|
| **ENTERPRISE** (8-10/10) | 69 | **74.2%** |
| **DEVELOPED-NOT-ENTERPRISE** (5-7/10) | 24 | 25.8% |
| **NOT-ENTERPRISE** (0-4/10) | 0 | 0% |
| **TOTAL** | **93** | 100% |
| **Score médio** | | **8.28/10** |

**Comparação com backend (FASE 1)**: Backend 78.2% ENT / 8.5 avg. Frontend 74.2% ENT / 8.28 avg. **Backend ligeiramente mais forte**. O drag do frontend vem de um grupo específico (components/dashboard/).

---

## Por grupo

| Grupo | Files | Avg | ENTERPRISE | DEVELOPED | NOT-ENT |
|---|---:|---:|---:|---:|---:|
| **root** (App/main/router) | 3 | 9.3 | 2 | 1 | 0 |
| **pages/** | 12 | 8.0 | 10 | 2 | 0 |
| **components/dashboard/** | 23 | **7.2** | 9 | 14 | 0 |
| **components/ui/** | 17 | 8.6 | 14 | 3 | 0 |
| **components/layout/** | 4 | 9.25 | 4 | 0 | 0 |
| **components/auth/** | 1 | 8.0 | 1 | 0 | 0 |
| **components/chat/** | 2 | 9.5 | 2 | 0 | 0 |
| **components/settings/** | 2 | 8.0 | 2 | 0 | 0 |
| **components/common** (6 sparse) | 6 | 9.5 | 6 | 0 | 0 |
| **stores/** (1 root + 12 slices) | 13 | 9.5 | 10 | 3 | 0 |
| **hooks/** | 4 | 9.5 | 4 | 0 | 0 |
| **lib/** | 5 | 9.0 | 5 | 0 | 0 |
| **types/api.ts** | 1 | 9.0 | 1 | 0 | 0 |

**Grupo mais forte**: `components/chat/`, `components/common/`, `hooks/`, `stores/` (9.5 avg cada).
**Grupo mais fraco**: **`components/dashboard/` (7.2 avg)** — 14/23 arquivos não chegam em ENTERPRISE. Single point de dívida concentrada.

---

## Top 5 achados críticos

### 1. `components/dashboard/` carrega a dívida técnica do FE

14 de 23 arquivos são DEVELOPED-NOT-ENTERPRISE. Padrões sistêmicos:
- **Zero `React.memo`** exceto em `status-dot.tsx` — cognitive-timeline, log-row, chat-bubble, plugin-card re-renderizam em cascata
- **Sem virtualização** em `chat-thread.tsx` (linha 46) e `cognitive-timeline.tsx` (linha 341) — travam em 1k+ items
- **`log-row.tsx` linha 56**: `onClick` em `<div>` sem `role`/`tabIndex`/`onKeyDown` — teclado não consegue expandir ERROR logs
- **Hardcoded English**: `channel-badge.tsx`, `chat-bubble.tsx` (nenhum `useTranslation` no arquivo inteiro), `letter-avatar.tsx` (`aria-label="Sovyx Mind"`)
- **Missing tests em 14 arquivos**, incluindo o critical security gate `permission-dialog` e o mais complexo `plugin-detail`

### 2. Raw `fetch()` em 3 lugares fura a camada API

- `hooks/use-auth.ts`: `fetch()` direto em vez de `api.get()` — duplica lógica de auth + bypassa 401 handler centralizado. **Plus**: silenciosamente seta `authenticated=true` em network error (security posture estranho)
- `components/auth/token-entry-modal.tsx`: raw fetch
- `components/settings/export-import.tsx`: raw fetch

Camada API (`lib/api.ts`) é sólida no coração, mas esses 3 pontos furam o contrato — drift de auth/error handling é iminente.

### 3. Type safety runtime ausente

- `types/api.ts` é **compile-time only** — zero validação de schema em runtime (sem zod/io-ts)
- **Maior risco latente**: backend muda schema, FE segue compilando mas quebra silencioso em produção
- `stores/slices/plugins.ts`: `as PluginStatus` casts no WS handler aceitam valores desconhecidos sem narrowing

### 4. Token em `localStorage` + WS token em query param

- `api.ts` guarda token em `localStorage` — XSS-exposed (qualquer script injetado lê)
- WebSocket passa token como query param — fica em access logs de proxy reverso e ISPs
- Fix: token em `sessionStorage` + memory (preferível), WS token via subprotocol ou first-message após upgrade

### 5. API layer incompleto

`lib/api.ts` falta:
- **Default timeout / AbortController** — request pendurado fica forever
- **Retry com backoff** em erros transientes (429, 503, ECONNRESET)
- **Verbo PATCH** (só GET/POST/PUT/DELETE)
- **Query-string helper tipado** — URLs construídas à mão em vários calls
- **WS URL protocol hardcoded `ws://`** — quebra em HTTPS deploy; deve ramificar em `location.protocol === 'https:' ? 'wss:' : 'ws:'`

---

## Top issues sistêmicos (consolidado)

### Acessibilidade (a11y)

- Hardcoded `aria-label` em inglês em vários pontos (`plugins.tsx` linhas 85/124, `plugin-card.tsx` 181, `letter-avatar.tsx`, `chat-bubble.tsx` inteiro sem i18n)
- Shadcn `ui/dialog.tsx`, `ui/sheet.tsx`, `ui/sidebar.tsx`, `ui/command.tsx` têm defaults `sr-only` em inglês (aceitável em lib, mas override pouco usado nos consumers)
- `brain-graph.tsx`: canvas sem alternativa de teclado (force-graph-2d inacessível)
- `log-row.tsx`: interactive `<div>` sem `role="button"` + `tabIndex={0}` + `onKeyDown`
- `settings.tsx` linha 456: tooltip `group-hover:block` — só mouse, sem teclado
- **Emoji-as-semantics** em risk dots (🟢🟡🔴), category icons, channel icons — lidos literalmente por screen readers

### Estado e lifecycle

- `voice.tsx` tem `useState` local que deveria estar em Zustand store (inconsistente com padrão geral)
- `voice.tsx` define 10+ voice-type interfaces inline (linhas 33-93) em vez de em `types/api.ts`
- `stores/slices/plugins.ts` — optimistic update + rollback bem feito (referência)
- `use-websocket.ts` — exponential backoff 1s→30s com reset-on-open, mountedRef guard, per-key trailing-edge debounce map, cleanup completo (referência)

### Performance

- **Nenhum dos hot paths está memoizado**: log-row (virtualized list), chat-bubble (thread), plugin-card (grid), cognitive-timeline (events list)
- Apenas `status-dot.tsx` usa `React.memo` — referência
- `chat-thread.tsx` e `cognitive-timeline.tsx` — listas não-virtualizadas (logs é a única página que faz right)

### Security

- **XSS posture está OK**: `MarkdownContent` usa `react-markdown` sem `rehype-raw`, escapa HTML, links externos têm `rel="noopener noreferrer"`, imgs têm `referrerPolicy="no-referrer"`
- **Única `dangerouslySetInnerHTML`** do codebase: `ui/chart.tsx` pra CSS-variable theming (id sanitizado, config dev-authored — aceitável mas anotado)
- `settings.tsx` linha 608: `window.prompt()` pra input de guardrail — UX + security smell (prompt bloqueia event loop, não pode ser styled, não pode ser testado)
- `ui/sidebar.tsx` escreve cookie sem `SameSite`/`Secure` attributes
- **Unbounded `JSON.stringify` em DOM** em `plugin-detail.tsx` (manifest + tool params) e `log-row.tsx` (extraFields) — sem size clamp nem secret redaction (token/key numa config é renderizado cru)

### Testing

- **plugins.tsx (363 LOC) sem test page-level** — gap de prioridade máxima
- **ui/sidebar.tsx (722 LOC) sem test dedicado**
- **ui/chart.tsx**, **ui/command.tsx** sem test
- **command-palette.tsx** sem test (Cmd+K keyboard handler crítico)
- **14 arquivos em components/dashboard/ sem tests**
- Testes das pages Overview/Brain/Conversations são **render-only, não interactive** — claim de ≥95% coverage é aspiracional
- **router.tsx sem test** — lazy loading + ErrorBoundary wiring untested

### Duplicação

- `nameToHue` duplicado em `plugin-detail.tsx` (inline linha 324 + helper linha 692) + `plugin-card.tsx`
- Auth header logic duplicada: `api.ts` + `use-auth.ts` + `token-entry-modal.tsx` + `export-import.tsx`

### ErrorBoundary

- `ErrorBoundary` existe mas falta `componentDidCatch` telemetry hook — crashes em produção ficam sem report
- Sem per-section error boundaries em `settings.tsx`, `plugins.tsx`, `chat.tsx` — um crash num sub-component derruba a página toda
- `router.tsx` tem boundary de nível route mas sem reporter

---

## Pontos fortes (worth calling out)

- **`lib/api.ts`** (o coração): typed, auth header injection, error normalization. Precisa só de timeout + retry + queries pra ser exemplar.
- **`use-websocket.ts`**: exponential backoff + debounce + cleanup — **referência de implementação**.
- **`use-onboarding.ts`**: fully derived/memoized from store, zero state duplicado.
- **`stores/slices/plugins.ts`**: optimistic update + rollback bem feito, zero `any`, proper typing.
- **`types/api.ts`**: completo, bem documentado, espelha 14 WS event types do backend.
- **`App.tsx` + `main.tsx`**: 10/10 cada — i18n import presente, strict mode, error boundary root.
- **`components/chat/markdown-content.tsx`**: XSS-hardened — no `rehype-raw`, escapes HTML, safe link/image overrides.
- **`components/dashboard/status-dot.tsx`**: 10/10, único com `React.memo` — **referência de perf**.
- **`stores/`**: avg 9.5, zero `any` em 23 arquivos, Zustand slices corretamente tipados com `StateCreator`.
- **Zero `any`** em todo o codebase frontend auditado.

---

## Roadmap de hardening FE (prioridade)

### P0 — security & reliability

1. **Token storage**: migrar de `localStorage` pra `sessionStorage` + memory (ou HTTP-only cookie se possível)
2. **WS auth**: substituir query param por subprotocol (`Sec-WebSocket-Protocol`) ou first-message
3. **WS URL protocol**: ramificar em `location.protocol` — senão quebra em HTTPS
4. **`JSON.stringify` render clamp**: adicionar size limit + secret redaction em `plugin-detail.tsx` e `log-row.tsx`
5. **Remover `window.prompt()`** em `settings.tsx` linha 608 — substituir por modal proper

### P1 — type safety + API layer

6. **Runtime validation**: adicionar `zod` schemas em `types/api.ts` + validar em `lib/api.ts` antes de retornar. Detecta backend drift em staging.
7. **`api.ts` hardening**: default 30s timeout + AbortController, retry com backoff (429/503/ECONNRESET), PATCH, query-string helper tipado
8. **Eliminar raw `fetch()`**: migrar `use-auth.ts`, `token-entry-modal.tsx`, `export-import.tsx` pra `api.get/post`
9. **`use-auth.ts` security posture**: não setar `authenticated=true` em network error — fail-closed

### P2 — performance

10. **`React.memo` em hot paths**: log-row, chat-bubble, plugin-card, cognitive-timeline-event (5 arquivos)
11. **Virtualizar**: `chat-thread.tsx` + `cognitive-timeline.tsx` (usar `@tanstack/react-virtual` já no projeto)
12. **Extrair `voice.tsx` types** pra `types/api.ts` + migrar state pra Zustand slice

### P3 — acessibilidade

13. **i18n sweep** em aria-labels hardcoded: `plugins.tsx`, `plugin-card.tsx`, `letter-avatar.tsx`, `chat-bubble.tsx`, `channel-badge.tsx`
14. **`log-row.tsx`**: adicionar `role="button"` + `tabIndex={0}` + `onKeyDown` pra expand
15. **Tooltips em `settings.tsx`**: substituir `group-hover` por Radix Tooltip (teclado-acessível)
16. **Emoji-as-semantics**: adicionar `aria-label` textual nos risk/status emojis, esconder emoji com `aria-hidden="true"`
17. **`brain-graph.tsx`**: adicionar listagem acessível dos nodes/relações (fallback pra force-graph)

### P4 — testing

18. **plugins.tsx page-level test** (prioridade máxima — 363 LOC untested)
19. **permission-dialog** interaction test — security gate
20. **command-palette.tsx** Cmd+K test
21. **router.tsx** lazy + ErrorBoundary test
22. **settings.tsx** slider/preset/save interaction tests (atualmente shallow)
23. **ui/sidebar.tsx** (722 LOC), **ui/chart.tsx**, **ui/command.tsx** tests

### P5 — structural

24. **ErrorBoundary `componentDidCatch` telemetry hook** — Sentry/PostHog integration
25. **Per-section error boundaries** em settings.tsx, plugins.tsx, chat.tsx
26. **Consolidar `nameToHue`** em `lib/format.ts`
27. **Consolidar auth header logic** em `lib/api.ts` (DRY)
28. **`ui/sidebar.tsx` cookie**: adicionar `SameSite=Lax` + `Secure` em HTTPS

---

## Output files

- `docs-internal/_meta/enterprise-audit-fe-part-A.md` — root + pages (210 linhas)
- `docs-internal/_meta/enterprise-audit-fe-part-B.md` — components/dashboard (156 linhas)
- `docs-internal/_meta/enterprise-audit-fe-part-C.md` — ui/layout/auth/chat/settings/common (224 linhas)
- `docs-internal/_meta/enterprise-audit-fe-part-D.md` — stores/hooks/lib/types (105 linhas)
- `docs-internal/_meta/enterprise-audit-frontend.md` — este (consolidado)
