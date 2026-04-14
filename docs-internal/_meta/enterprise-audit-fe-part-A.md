# Enterprise Audit FE — Part A (root + pages)

Scope: 15 files — `App.tsx`, `main.tsx`, `router.tsx`, `pages/*.tsx` (12).
Criteria: 10 binary gates (type-safety, state, error-boundary, a11y, perf, i18n,
responsive, tests, api-layer, security). Score 8-10 = ENTERPRISE, 5-7 =
DEVELOPED-NOT-ENTERPRISE, 0-4 = NOT-ENTERPRISE.

## Summary

| Group           | Files | Avg  | ENTERPRISE | DEVELOPED | NOT-ENT |
| --------------- | ----- | ---- | ---------- | --------- | ------- |
| root (3)        | 3     | 9.3  | 3          | 0         | 0       |
| pages-core (5)  | 5     | 8.4  | 4          | 1         | 0       |
| pages-stub (3)  | 3     | 8.3  | 3          | 0         | 0       |
| pages-heavy (4) | 4     | 7.5  | 2          | 2         | 0       |
| **TOTAL (15)**  | 15    | 8.27 | 12         | 3         | 0       |

Legend by group (pages):
- pages-core = overview, conversations, chat, brain, logs
- pages-stub = emotions, productivity, not-found
- pages-heavy = settings, plugins, voice, about

---

## root (App.tsx, main.tsx, router.tsx)

### File: dashboard/src/App.tsx — Score: 10/10 — ENTERPRISE-GRADE
All 10 criteria pass. TokenEntryModal pattern + RouterProvider delegates lazy
+ error boundary responsibility correctly. Has no page-level business logic.
Implicitly typed FC, no `any`.

### File: dashboard/src/main.tsx — Score: 10/10 — ENTERPRISE-GRADE
StrictMode wrapping `<App />`; i18n imported before App (line 3). Uses non-null
assertion on `#root` which is acceptable — Vite template contract.

### File: dashboard/src/router.tsx — Score: 8/10 — ENTERPRISE-GRADE
Lazy loading for all routes, `<ErrorBoundary>` wraps every `<Suspense>` in
`PageWrapper`, skeleton fallbacks per route. 
Minor failures:
- (8) **TESTING**: no `router.test.tsx` — route config untested, including that
  every path renders without throwing or that the ErrorBoundary catches.
- (6) **I18N**: skeleton fallback accepted (non-textual), but the default
  fallback is a bare Skeleton with no `aria-label` for screen readers.

---

## pages-core (5)

### File: dashboard/src/pages/overview.tsx — Score: 9/10 — ENTERPRISE-GRADE
Failed:
- (8) **TESTING**: `overview.test.tsx` (99 lines) — thin: only checks heading
  presence + "150" string + "/database/i"; no interaction tests for
  onboarding transitions, timeout banner, `window.location.reload` click, or
  `refreshStatus`/`refreshHealth` dynamic import.

### File: dashboard/src/pages/conversations.tsx — Score: 9/10 — ENTERPRISE-GRADE
Failed:
- (5) **PERFORMANCE**: conversation list is rendered as a flat `.map(...)` inside
  `ScrollArea` — no virtualization. With `limit=50` pagination it's tolerable,
  but "Load More" accumulates in-memory without virt → 100+ convs will lag.
  Line: `filtered.map((conv) => ( <ConversationRow ... />))`.

### File: dashboard/src/pages/chat.tsx — Score: 9/10 — ENTERPRISE-GRADE
All security/XSS guarantees hold (ReactMarkdown with no `rehype-raw`, so HTML
is escaped; links get `rel="noopener noreferrer"`; images use `referrerPolicy`).
Failed:
- (5) **PERFORMANCE**: message thread renders via `.map` with no virtualization
  (`messages.map((msg) => <div key={msg.id}>...`); a 500-message chat will
  re-render the full list on any `input` state change (MarkdownContent is
  memoized, but the bubble wrappers are not; `useCallback`s depend on `input`).
  Line 207: `{messages.map((msg) => ( <div key={msg.id} ...`.

### File: dashboard/src/pages/brain.tsx — Score: 10/10 — ENTERPRISE-GRADE
Graph is capped to `limit=200` server-side + `useMemo` on nodes/links/counts;
ForceGraph canvas handles its own perf. AbortController on fetch, debounced
search (300ms), ResizeObserver for responsive dims, a11y labels on search.

### File: dashboard/src/pages/logs.tsx — Score: 10/10 — ENTERPRISE-GRADE
Virtualized with `@tanstack/react-virtual`, 500-item server cap, visibility-
aware polling, auto-follow with manual-scroll break. Typed LogEntry, no `any`.
LogRow colocated component, test has 79 lines with interaction (filter
buttons, limit param, fetch error).

---

## pages-stub (3)

### File: dashboard/src/pages/emotions.tsx — Score: 10/10 — ENTERPRISE-GRADE
Thin wrapper over `<ComingSoon>` — scored accordingly. i18n keys, no state,
no network. Test exists (16 lines, appropriate for stub).

### File: dashboard/src/pages/productivity.tsx — Score: 10/10 — ENTERPRISE-GRADE
Same as emotions — `<ComingSoon>` wrapper. Legitimate 16-line test.

### File: dashboard/src/pages/not-found.tsx — Score: 9/10 — ENTERPRISE-GRADE
Clean. Tests: 2 cases (404 heading + link to /). 
Failed:
- (4) **ACCESSIBILITY**: the giant "S" brand logo (line 11) is a `<div>` with
  no `aria-hidden="true"` or `role="img"` — screen readers will announce the
  letter "S" before the 404 heading. Minor, but in an enterprise a11y audit
  this is a common axe-core catch.

---

## pages-heavy (4)

### File: dashboard/src/pages/settings.tsx — Score: 6/10 — DEVELOPED-NOT-ENTERPRISE
832 lines, multi-tab form (Personality, OCEAN, Safety, General, Engine Info,
Provider, LLM/Brain). Well-typed from `types/api.ts`. Real concerns:
- (3) **ERROR BOUNDARIES**: the page itself has NO local error boundary for
  sub-sections (ProviderConfig, ExportImportSection imported as components).
  A throw in `ProviderConfig` blows the whole settings page (route-level
  ErrorBoundary catches it, but the granularity is wrong).
- (4) **ACCESSIBILITY**: "tooltip" pattern uses `group-hover:block` only —
  keyboard users cannot trigger the tooltip (`cursor-help` + hover class).
  Line 454-459: `<div className="group relative"> <InfoIcon ... /> <div
  className="... hidden ... group-hover:block">`.
- (5) **PERFORMANCE**: 832-line single component, no memoization of
  `getEffectiveTone` / `getPersonalityValue` / `getOceanValue`; every slider
  drag re-runs all three on every trait. Minor for 6 sliders but indicative.
- (8) **TESTING**: test file (224 lines) covers engine display + section
  rendering, but lacks interaction tests for slider drag, tone preset apply,
  guardrail add/remove, child-safe toggle cascade, PUT payload shape, save
  success/failure toasts. Claim "≥95% coverage" in CLAUDE.md not credible
  for this file.
- (10) **SECURITY**: line 608 `const rule = prompt(...)` — uses native
  `window.prompt()` for user input which then feeds into `updateSafety(
  "guardrails", custom)`. No sanitization, no length cap, no escape. Stored
  into mind config and later rendered via `{g.rule}` (React escapes, so no
  XSS), but UX-wise this is a security smell + mobile-hostile.
Pass: type-safety, state (Zustand slices), i18n (all via t()), responsive,
api-layer (api.get/put exclusively).

### File: dashboard/src/pages/plugins.tsx — Score: 5/10 — DEVELOPED-NOT-ENTERPRISE
Failed:
- (6) **I18N**: two hardcoded English strings in `aria-label`:
  - Line 85: `aria-label="Plugin filter"`
  - Line 124: `aria-label="Sort plugins"`
- (8) **TESTING**: **no `plugins.test.tsx` file exists**. This is a 363-line
  page with filter/sort/search/detail-panel logic and it is completely
  untested at the page level.
- (4) **ACCESSIBILITY**: role="tablist" without matching `role="tabpanel"` +
  `aria-controls` — ARIA tabs pattern incomplete.
- (10) **SECURITY**: no direct XSS risk, but plugin `description` is passed to
  `lowercase().includes` and rendered inside PluginCard without audit here
  (would need to check PluginCard — out of scope).
- (3) **ERROR BOUNDARIES**: no local boundary around PluginDetailPanel (side
  panel crash kills page).
Pass: type-safety (PluginInfo from api types, no `any`), state (Zustand slice
`plugins`), perf (useMemo on filter+sort), responsive (sm: lg: grid),
api-layer (fetchPlugins is a store action, no raw fetch).

### File: dashboard/src/pages/voice.tsx — Score: 7/10 — DEVELOPED-NOT-ENTERPRISE
Failed:
- (2) **STATE MANAGEMENT**: `status`, `models`, `loading`, `error` are all
  local `useState` — voice status is shared data that should live in the
  Zustand store like other system state (status, plugins, etc.). Inconsistent
  with the rest of the dashboard.
- (9) **API LAYER**: **types defined in-file** (PipelineStatus, STTStatus,
  TTSStatus, VADStatus, WakeWordStatus, WyomingStatus, HardwareStatus,
  VoiceStatus, ModelSelection, VoiceModels — lines 33-93). These belong in
  `types/api.ts` per convention. Fetches go through `api.get`, so api-layer
  itself is fine, but the type-location violation is flagged under API-LAYER.
- (3) **ERROR BOUNDARIES**: no local boundary. Route-level only.
Pass: type-safety (no `any`), error-state UI, a11y (`aria-label` on refresh),
perf (single fetch, table unvirtualized but finite tiers), i18n (all t()),
responsive (sm: lg:), testing (241-line test with fixtures + matrix assertions),
security (refreshButton, no innerHTML).

### File: dashboard/src/pages/about.tsx — Score: 10/10 — ENTERPRISE-GRADE
Full i18n, all external links have `rel="noopener noreferrer"` + `target=
"_blank"` (line 97-98), read-only page, Zustand for `status`, tests cover
content + link targets. Comment header claims "Zero hardcoded English" and
verified.

---

## Top issues across A

1. **Missing `plugins.test.tsx`** — a 363-line interactive page has zero page-
   level tests. Highest-priority gap in the fe audit.
2. **Hardcoded aria-labels in `plugins.tsx`** (lines 85, 124) — breaks the
   "all user-visible + a11y strings via i18n" convention.
3. **`voice.tsx` state not in Zustand store + types inline** — inconsistent
   with the rest of the dashboard (conversations, brain, logs, plugins all use
   the store + `types/api.ts`).
4. **`chat.tsx` + `conversations.tsx` message lists not virtualized** — will
   degrade past ~200 items. Logs is the only page that got virt right.
5. **`settings.tsx` uses native `window.prompt()`** for guardrail input (line
   608) — UX smell, mobile-hostile, no validation. Should be a `<Dialog>`.
6. **`settings.tsx` tooltips hover-only** (lines 454-459) — keyboard users
   cannot read tooltip content. `focus-within:block` would fix it.
7. **No per-section error boundaries** in `settings.tsx` and `plugins.tsx` —
   a single crash in `<ProviderConfig>` or `<PluginDetailPanel>` flips the
   whole route to the fallback UI.
8. **Overview + Brain + Conversations tests are shallow** — mostly
   render-assertions, no interaction (clicks, filter changes, load-more).
   Claim of ≥95% coverage in `CLAUDE.md` is aspirational, not reality for
   these pages.
9. **`not-found.tsx` brand letter `<div>S</div>` lacks `aria-hidden`** —
   screen-reader chatter. Trivial fix.
10. **`router.tsx` untested** — critical infrastructure (lazy loading + error
    boundary wiring) has no direct tests.

Overall: the **root + stub pages are enterprise-grade**. The pages that actually
house non-trivial UX (`settings`, `plugins`, `voice`, and to a lesser extent
`chat`/`conversations`) show the typical "developed-not-enterprise" signature:
working features, correct types, correct i18n on body text, but test depth,
accessibility edge cases, per-section error isolation, and state-placement
consistency all miss the bar.
