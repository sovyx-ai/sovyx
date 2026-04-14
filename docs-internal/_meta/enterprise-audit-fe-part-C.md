# Enterprise Audit FE — Part C (ui + layout + auth + chat + settings + common)

Scope: 32 frontend files across `dashboard/src/components/ui`, `components/layout`, `components/auth`, `components/chat`, `components/settings`, and `components/` (common).
Scoring: 10 criteria, 0 or 1 each. ENTERPRISE >= 8 - DEVELOPED 5-7 - NOT-ENT <= 4.

Criterion applicability: UI primitives pass criterion 6 (i18n) trivially when they have no own user-visible text; they are unopinionated and receive `children`/props from consumers. Criterion 8 (testing) is applied pragmatically to primitives: thin Radix/Base-UI wrappers with near-zero logic are scored on the presence of integration coverage via pages/components — flagged when a primitive is both non-trivial (e.g. `sidebar`, `chart`, `command`) AND untested.

## Summary

| Group            | Files | Avg  | ENTERPRISE | DEVELOPED | NOT-ENT |
|------------------|------:|-----:|-----------:|----------:|--------:|
| components/ui    |    17 | 8.59 |         14 |         3 |       0 |
| components/layout|     4 | 9.25 |          4 |         0 |       0 |
| components/auth  |     1 | 8.00 |          1 |         0 |       0 |
| components/chat  |     2 | 9.50 |          2 |         0 |       0 |
| components/settings|   2 | 8.00 |          2 |         0 |       0 |
| components (common)| 6   | 9.50 |          6 |         0 |       0 |
| **TOTAL**        |**32** | 8.84 |        **29** |     **3** |   **0** |

---

## components/ui (17 files)

### ui/badge.tsx — 9/10 — ENTERPRISE
Typed via `useRender.ComponentProps<"span"> & VariantProps`, no own strings, receives children.
Failed:
- **#8 [TESTING]**: no dedicated test; covered indirectly.

### ui/button.tsx — 9/10 — ENTERPRISE
Strong CVA typing `ButtonPrimitive.Props & VariantProps<typeof buttonVariants>`. Focus-visible ring, disabled pointer-events guard. Fully typed, no text.
Failed:
- **#8 [TESTING]**: no dedicated test.

### ui/card.tsx — 9/10 — ENTERPRISE
Typed div wrappers. No colour on `CardTitle` heading level — it's a `<div>` not `<h*>`. Mildly weak for semantics but consistent with shadcn.
Failed:
- **#4 [A11Y]**: `CardTitle` is a `<div>`, not a heading element — breaks assistive tech heading outline when used without an explicit wrapper.

### ui/chart.tsx — 7/10 — DEVELOPED
Strict typed context via `ChartContextProps` with `useChart()` guard. `ChartStyle` uses `dangerouslySetInnerHTML` for theme CSS injection — documented as safe (id sanitized, config is dev-defined).
Failed:
- **#4 [A11Y]**: no `role="img"` / `aria-label` on `ChartContainer`; screen-readers see only raw SVG guts.
- **#5 [PERF]**: `ChartStyle` recomputes CSS string on every render (no `useMemo`); cheap but wasteful when config is stable.
- **#8 [TESTING]**: no dedicated test for the non-trivial `getPayloadConfigFromPayload`.

### ui/command.tsx — 7/10 — DEVELOPED
Wrapper around `cmdk` + `Dialog`. Accessibility inherited from cmdk; `CommandDialog` defaults its own `title`/`description` to English literals ("Command Palette", "Search for a command to run...").
Failed:
- **#6 [I18N]**: hardcoded English defaults for `title`/`description` in `CommandDialog` — exposed as props so callers can override, but the defaults leak if a consumer forgets. Consumers in this codebase always override, but the default violates the i18n policy for user-visible strings.
- **#8 [TESTING]**: no dedicated test.
- **#4 [A11Y]**: relies on consumer passing meaningful title; `CheckIcon` appended unconditionally has no `aria-hidden`.

### ui/dialog.tsx — 9/10 — ENTERPRISE
Base-UI primitive bindings. `sr-only` "Close" label on the close button (keyboard-reachable). `showCloseButton` is a prop.
Failed:
- **#6 [I18N]**: `<span className="sr-only">Close</span>` hardcoded — SR text is user-visible for AT users.

### ui/input.tsx — 9/10 — ENTERPRISE
Passthrough typed via `React.ComponentProps<"input">`, destructures `type` so it isn't forced. Proper `aria-invalid` styling.
Failed:
- **#8 [TESTING]**: no dedicated test.

### ui/input-group.tsx — 8/10 — ENTERPRISE
Composes Input/Textarea/Button with CVA. `onClick` handler on `InputGroupAddon` focuses nearest input when clicking padding — reasonable UX but `closest("button")` check could miss `role="button"` custom elements.
Failed:
- **#4 [A11Y]**: custom click-to-focus may interfere with assistive focus expectations in nested interactives.
- **#8 [TESTING]**: no dedicated test.

### ui/label.tsx — 9/10 — ENTERPRISE
Trivial typed `<label>`. No text.
Failed:
- **#8 [TESTING]**: no dedicated test.

### ui/scroll-area.tsx — 9/10 — ENTERPRISE
Thin Base-UI wrapper. Focus ring on viewport.
Failed:
- **#8 [TESTING]**: no dedicated test.

### ui/separator.tsx — 10/10 — ENTERPRISE
Minimal and fully typed; tests trivially covered via layout integration.

### ui/sheet.tsx — 9/10 — ENTERPRISE
Matches Dialog pattern, side variants `"top"|"right"|"bottom"|"left"`. Backdrop + portal + close button with `sr-only` label.
Failed:
- **#6 [I18N]**: `<span className="sr-only">Close</span>` hardcoded.

### ui/sidebar.tsx — 7/10 — DEVELOPED
722 lines of substantive logic: context, cookie-persisted state, keyboard shortcut (Ctrl/Cmd+B), mobile Sheet variant, collapsible modes, tooltip integration. Rich CVA for menu button. Properly memoized `contextValue` and `setOpen`.
Failed:
- **#6 [I18N]**: hardcoded English `aria-label="Toggle Sidebar"` on `SidebarRail`, `"Sidebar"` / `"Displays the mobile sidebar."` on the mobile Sheet header. Should accept props or consume i18n.
- **#8 [TESTING]**: no dedicated test despite substantial logic (cookie write, keyboard listener).
- **#10 [SECURITY]**: `document.cookie = ... sidebar_state=...; path=/` written without `SameSite` or `Secure` flags — low severity (no sensitive data) but not best practice for enterprise.

### ui/skeleton.tsx — 10/10 — ENTERPRISE
Three-line passthrough.

### ui/sonner.tsx — 8/10 — ENTERPRISE
Wraps `sonner` with themed icons. `theme="dark"` is hardcoded — acceptable given app is dark-only, but not responsive to future theme switching.
Failed:
- **#7 [RESPONSIVE]**: `theme="dark"` hardcoded prevents a future light-theme toggle.
- **#8 [TESTING]**: no dedicated test.

### ui/textarea.tsx — 9/10 — ENTERPRISE
Matching Input pattern. `field-sizing-content` for auto-grow.
Failed:
- **#8 [TESTING]**: no dedicated test.

### ui/tooltip.tsx — 9/10 — ENTERPRISE
Base-UI tooltip with portal, positioner, arrow. `delay={0}` on Provider is aggressive (no hover intent) — accessibility-neutral but may cause flicker.
Failed:
- **#8 [TESTING]**: no dedicated test.

---

## components/layout (4 files)

### layout/app-layout.tsx — 10/10 — ENTERPRISE
Skip-nav link, `role="banner"`/`role="main"`, i18n `document.title` sync (WCAG 2.4.2), WebSocket lifecycle at layout scope, aria-labels on icon-only buttons, `aria-hidden` on decorative icons. Reactive responsive px/sm:px. Comprehensive.

### layout/app-sidebar.tsx — 9/10 — ENTERPRISE
Full i18n, typed `NavItem[]`, `aria-label="Main navigation"` on `<Sidebar>`. Nav links `isActive` bound to `useLocation`. Minor use of `useDashboardStore` in what is a layout shell — acceptable (nav needs connection state), not a UI primitive.
Failed:
- **#6 [I18N]**: `aria-label="Main navigation"` on Sidebar root hardcoded; trivial but a real SR string.

### layout/breadcrumb.tsx — 9/10 — ENTERPRISE
Semantic `<nav aria-label="Breadcrumb">`, `aria-current="page"` on leaf, typed route map. Path normalization.
Failed:
- **#6 [I18N]**: literal `"Sovyx"` brand string and `aria-label="Breadcrumb"` not run through i18n — defensible for brand/landmark naming conventions but flagged for strictness.

### layout/page-transition.tsx — 9/10 — ENTERPRISE
Tiny, single-purpose, typed, CSS-only animation. Tested.
Failed:
- **#4 [A11Y]**: no `prefers-reduced-motion` opt-out honored at the component level (must be enforced via CSS tokens — not verifiable from this file).

---

## components/auth (1 file)

### auth/token-entry-modal.tsx — 8/10 — ENTERPRISE
Typed state machine (`ValidationState`), i18n, autofocus, Enter-to-submit, closing prevented without token. Uses `setToken` from api lib (not raw localStorage). Success/error UI with loader.
Failed:
- **#9 [API LAYER]**: uses raw `fetch(${BASE_URL}/api/status, {...})` directly instead of the central `api` client. This duplicates auth-header logic and bypasses the error-normalization layer used elsewhere.
- **#10 [SECURITY]**: token stored via `setToken` (which likely uses `localStorage`) — XSS recovery surface. Not a bug in this file but worth calling out: no silent token masking on reveal, relies on `type="password"`.

---

## components/chat (2 files — index.ts skipped)

### chat/code-block.tsx — 10/10 — ENTERPRISE
Pre-highlighted ReactNode children (not raw strings), `ref.textContent` for copy (no string concat). i18n with safe fallbacks. `aria-label` on copy button, `type="button"`, timed feedback. Testing file present.

### chat/markdown-content.tsx — 9/10 — ENTERPRISE
Security-critical file. Scored carefully:
- NO `dangerouslySetInnerHTML` — uses `react-markdown` which escapes by default.
- `rehype-highlight` + `remark-gfm` — no `rehype-raw` (would enable embedded HTML).
- External links: `target="_blank" rel="noopener noreferrer"` present.
- Images: `referrerPolicy="no-referrer"`, `loading="lazy"`, constrained size.
- Memoized with `React.memo`.
- Integration test present.

Failed:
- **#6 [I18N]**: `alt || "Image shared in conversation"` is a hardcoded English fallback served to AT users for missing alt text. Should use `useTranslation`.

This is a clean enterprise markdown renderer. XSS-safe by construction.

---

## components/settings (2 files)

### settings/export-import.tsx — 8/10 — ENTERPRISE
GDPR-aware data export/import, confirmation dialog before destructive import, file-type validation, toast feedback, i18n'd throughout. Binary blob download handling is correct (`URL.createObjectURL` + `revokeObjectURL`).
Failed:
- **#9 [API LAYER]**: uses raw `fetch(${BASE_URL}/api/export)` and `/api/import`; justifiable for `Content-Disposition` header + `multipart/form-data` that the central client may not expose, but the `api` client should be extended rather than bypassed.
- **#10 [SECURITY]**: `localStorage.getItem("sovyx_token")` read directly rather than via auth helper — duplicates the token-read path, widening the XSS attack surface.

### settings/provider-config.tsx — 8/10 — ENTERPRISE
Uses `api.get`/`api.put` (not raw fetch), `AbortController` cleanup, proper `DOMException` abort check, `useMemo` for derived state, i18n with fallbacks. Tests present.
Failed:
- **#4 [A11Y]**: provider picker uses native `<button>` but no `aria-pressed` to signal selected state; relies on colour/border only.
- **#6 [I18N]**: literal `"Ollama"` in button text bypasses i18n (acceptable — it's a proper noun — but flagged for strictness).

---

## components (common) (6 files)

### coming-soon.tsx — 10/10 — ENTERPRISE
Typed props (`LucideIcon`, `titleKey`, `descriptionKey`), full i18n by key. Accessible semantic `<h1>`, centered layout. Test present.

### command-palette.tsx — 9/10 — ENTERPRISE
Cmd+K / Ctrl+K keyboard shortcut with cleanup, typed `CommandAction`, i18n throughout. Nav + actions split, keywords for fuzzy match, `useCallback` for `run`. Uses `useNavigate` and store action.
Failed:
- **#8 [TESTING]**: no dedicated `command-palette.test.tsx`. This is a user-facing, shortcut-bound feature with non-trivial `useEffect` keyboard handling — the absence of tests is a gap.

### empty-state.tsx — 10/10 — ENTERPRISE
Accepts i18n strings as props (`title`, `description`, `action.label`), optional animation slot, proper button composition. Tested.

### empty-state-animations.tsx — 10/10 — ENTERPRISE
Pure CSS/SVG animations, `aria-hidden="true"` on every decorative surface, typed `className` props. Tested.

### error-boundary.tsx — 9/10 — ENTERPRISE
Real class-component `componentDidCatch`-family boundary via `getDerivedStateFromError`, retry button, i18n via `i18n.t()` (correct — class components can't use hooks), custom fallback prop, production-vs-dev messaging sidestepped via `error.message`. Tested.
Failed:
- **#3 [ERROR BOUNDARIES]**: no `componentDidCatch(error, errorInfo)` — no logging/telemetry hook. A real enterprise boundary reports to a logger/Sentry/server. Retry UI alone is not enough.

### skeletons.tsx — 10/10 — ENTERPRISE
Five page-specific skeletons composed from `Skeleton` primitive, token-driven, responsive grids. No own strings, no logic.

---

## Top issues across C

1. **Missing tests on substantive primitives and features** — `ui/sidebar.tsx` (722 lines of state/cookie/keyboard logic), `ui/chart.tsx` (context + style injection), `ui/command.tsx`, AND `command-palette.tsx` (cmd+K handler) lack dedicated tests. Integration coverage masks regressions in isolated logic like cookie serialization, keyboard-shortcut collisions, and `getPayloadConfigFromPayload`.

2. **Raw fetch in feature components bypasses the `api` client** — `auth/token-entry-modal.tsx` and `settings/export-import.tsx` reach for `fetch(${BASE_URL}...)` directly. This duplicates auth header logic, skips error normalization, and scatters the XSS surface (`localStorage.getItem("sovyx_token")` read at call site). Extend `api` to expose blob/multipart helpers and migrate.

3. **Hardcoded English sr-only / aria-label strings in UI primitives** — `ui/dialog.tsx` and `ui/sheet.tsx` ship `<span className="sr-only">Close</span>`, `ui/sidebar.tsx` ships `aria-label="Toggle Sidebar"` and `"Displays the mobile sidebar."`, `ui/command.tsx` defaults `title="Command Palette"`. These are real user-visible strings to screen readers and must route through i18n (accept labels as props with typed keys, or use a shared AT-string namespace).

4. **ErrorBoundary has no telemetry hook** — missing `componentDidCatch(error, errorInfo)` means React render crashes are silently discarded once the user clicks retry. For enterprise, this must at least log to the `api` backend and/or console via a structured path.

5. **Minor a11y gaps in charts/cards** — `CardTitle` is a `<div>` breaking heading outline; `ChartContainer` has no `role="img"`/`aria-label` and exposes raw SVG to AT; provider-config pickers lack `aria-pressed`. Small individually, cumulative impact on AT users.

6. **Cookie hygiene in `ui/sidebar.tsx`** — `document.cookie = ... sidebar_state=...; path=/` lacks `SameSite=Lax` and `Secure`; low-severity but not enterprise-hygienic.

Overall verdict: group C is strong — 29/32 ENTERPRISE, 0 NOT-ENT, average 8.84. The three DEVELOPED files (`ui/chart.tsx`, `ui/command.tsx`, `ui/sidebar.tsx`) are the non-trivial primitives, and their main gap is test coverage plus i18n'd defaults — not architectural defects. Security posture on the markdown/code-block pipeline is correctly hardened (no raw HTML, proper link/image sanitization, memoized parsing).
