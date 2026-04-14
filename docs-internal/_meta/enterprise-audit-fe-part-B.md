# Enterprise Audit FE — Part B (components/dashboard)

## Summary

| Group | Files | Avg | ENTERPRISE | DEVELOPED | NOT-ENT |
|---|---|---|---|---|---|
| components/dashboard | 23 | 7.2 | 9 | 14 | 0 |

Legend (per file scoring): T=TypeSafety, S=State, E=Errors, A=A11y, P=Perf, I=i18n, R=Responsive, Te=Tests, Ap=ApiLayer, Se=Security.

---

## components/dashboard (23 files)

### File: activity-feed.tsx — 9/10 — ENTERPRISE
T1 S1 E1 A1 P0 I1 R1 Te1 Ap1 Se1
- Failed P: line 171 `const reversed = [...events].reverse();` runs on every render without `useMemo`; list can grow large and `reversed` is allocated per render. Keys also use array index fallback (`key={`${event.timestamp}-${i}`}`) mixing index with timestamp — unstable if identical timestamps appear. Component is not `React.memo`-wrapped despite being a hot path.

### File: brain-graph.tsx — 8/10 — ENTERPRISE
T0 S1 E1 A0 P1 I1 R1 Te1 Ap1 Se1
- Failed T: double `as unknown as { x: number }` cast appears in `nodeCanvasObject` (lines 72-73) and `nodePointerAreaPaint` (lines 179-180). Should extend `BrainNode` with `x?: number; y?: number` from force-graph types instead of bypassing the type system.
- Failed A: canvas-based graph has NO keyboard navigation, no `aria-label`, no text alternative, no fallback list for screen readers. `react-force-graph-2d` root is not wrapped in a labelled landmark. Click-only interaction (`onNodeClick`) is inaccessible for keyboard users.
- Note P passes: callbacks memoized via `useCallback`, hoveredNode drives local redraws only, `zoomToFit` debounced via `setTimeout`. But no `ResizeObserver` — width/height are props from parent, so resize handling is delegated (acceptable).

### File: category-legend.tsx — 7/10 — DEVELOPED
T1 S1 E1 A1 P0 I1 R1 Te0 Ap1 Se1
- Failed P: exports two sibling components (`CategoryLegend`, `RelationLegend`) but no `.test.tsx`. Not memoized despite being stable pure-props components rendered alongside the graph.
- Failed Te: no test file for either component.

### File: channel-badge.tsx — 6/10 — DEVELOPED
T1 S1 E1 A0 P0 I0 R1 Te0 Ap1 Se1
- Failed A: `<span>` with `title` only — no `role` or `aria-label`. Icon is an emoji prefix inside the label text (not hidden from SR), so screen readers read "airplane emoji Telegram".
- Failed I: **hardcoded labels** (`label: "Telegram"`, `"Discord"`, `"Signal"`, `"CLI"`, `"API"`) never pass through `useTranslation()` (lines 4-9). Also the fallback `label: channel` is raw backend string.
- Failed Te: no `channel-badge.test.tsx`.
- Failed P: no `React.memo`; used inline in rows.

### File: channel-status.tsx — 7/10 — DEVELOPED
T1 S1 E1 A1 P0 I1 R1 Te1 Ap1 Se0
- Failed P: `handleConnect` `useCallback` dependency array (line 72) omits `t` — stale closure risk when language changes mid-flow. Also the `SETUP_COMPONENTS` map stores `SignalSetup` which does not accept `onDone`, but is typed as `React.FC<{ onDone: () => void }>` — type is erased by ignoring the prop, technically unsafe.
- Failed Se: `placeholder="123456:ABC-DEF..."` in the token input is fine, but the token is passed via `api.post` — good. However no CSRF/origin check visible on the Telegram setup endpoint call-site (that's backend concern).

### File: chat-bubble.tsx — 7/10 — DEVELOPED
T1 S1 E1 A0 P0 I0 R1 Te0 Ap1 Se1
- Failed A: no `role="article"` or `aria-label` indicating author. The bubble conveys authorship via CSS direction only — SR users cannot distinguish user from AI.
- Failed I: **no `useTranslation`** used at all. The component renders user content, but the `MindAvatar` aria label "Sovyx Mind" (in letter-avatar.tsx) is also hardcoded English.
- Failed Te: no `chat-bubble.test.tsx`.
- Failed P: `React.memo` would be valuable — bubbles re-render on every message-list update; no memoization. Also every bubble pays the cost of `MarkdownContent` full parse.
- Note Se passes: markdown path via `MarkdownContent` (react-markdown, sanitized by default; `markdown-integration.test.tsx` includes an XSS test).

### File: chat-thread.tsx — 5/10 — DEVELOPED
T1 S1 E1 A0 P0 I1 R1 Te0 Ap1 Se0
- Failed A: the scrolling log has no `role="log"` / `aria-live`. Loading spinner has no `aria-label` / `role="status"`.
- Failed P: **no virtualization** — `messages.map` renders every message (line 46). At 1k+ messages each with markdown parsing this will stall. `bottomRef.scrollIntoView({ behavior: "smooth" })` on every `messages.length` change is fine but combined with no memoization of `ChatBubble` causes full-tree reconcile on every new message.
- Failed Te: no `chat-thread.test.tsx`.
- Failed Se: user-supplied `participantName` is forwarded to `ChatBubble` → `LetterAvatar` where it is rendered as text content. Not executed as HTML so practically safe, but there is no length clamp — a pathological long name is not truncated at the thread level.

### File: cognitive-timeline.tsx — 6/10 — DEVELOPED
T0 S1 E1 A1 P0 I1 R1 Te1 Ap1 Se0
- Failed T: `entrySummary` uses `(d.channel as string)`, `(d.names as string[])`, `(d.importance as number)` etc. (lines 106-133) — multiple `as` casts on untyped `TimelineEntry["data"]` Record. `t` parameter typed ad-hoc as `(key: string, opts?: Record<string, unknown>) => string` instead of `TFunction`.
- Failed P: **no virtualization** for the timeline — `groupEntries` returns a `Map` and every entry is rendered (line 341). Groups use `idx` as part of key (`${entry.timestamp}-${idx}`), unstable for identical timestamps. At hundreds of entries the ScrollArea holds the full DOM.
- Failed Se: `entrySummary` default branch `JSON.stringify(d).slice(0, 60)` stringifies arbitrary backend data; while not HTML it can leak internal fields to the UI. Also `(d.preview as string) ?? t("timeline.message")` renders raw backend text without length clamp on the row path (relies on `truncate` class only).
- Note Ap: data flows via `useDashboardStore().fetchTimeline` → centralized store layer (good).

### File: health-grid.tsx — 9/10 — ENTERPRISE
T1 S1 E1 A1 P0 I1 R1 Te0 Ap1 Se1
- Failed P: no memoization; `overallStatus` + `.filter` recompute every render on the full checks array. Acceptable at n=10 but structurally non-enterprise.
- Failed Te: no `health-grid.test.tsx`.

### File: letter-avatar.tsx — 7/10 — DEVELOPED
T1 S1 E1 A1 P0 I0 R1 Te0 Ap1 Se1
- Failed I: `MindAvatar` hardcodes `aria-label="Sovyx Mind"` (line 79); `LetterAvatar` renders the raw first char of `name` without i18n and without aria label (screen reader reads the letter).
- Failed P: `hashString` runs on every render; should memoize. Both components not wrapped in `React.memo` despite being rendered once per chat bubble.
- Failed Te: no test.

### File: log-row.tsx — 6/10 — DEVELOPED
T1 S1 E1 A0 P0 I0 R1 Te0 Ap1 Se1
- Failed A: the row has `onClick` on a `<div>` (line 56) with **no `role="button"`, no `tabIndex`, no keyboard handler**. In a virtual list this means ERROR/WARNING entries cannot be expanded via keyboard at all. Critical a11y failure for a log-viewer used under "logs" page.
- Failed I: no `useTranslation` — logs are inherently data, but the level badges (DEBUG/INFO/WARNING/ERROR/CRITICAL) are rendered verbatim. Defensible; marginal fail.
- Failed P: **not wrapped in `React.memo`** despite being used in `@tanstack/react-virtual` hot path (per docstring). The whole point of a virtualized list is that items are pure; `LogRow` re-renders every time the parent re-renders unless memoized. Combined with `useState(expanded)` the state resets on unmount/remount during scroll — check parent's `measureElement` behavior.
- Failed Te: no `log-row.test.tsx` despite being a critical virtualized hot-path component.

### File: metric-chart.tsx — 8/10 — ENTERPRISE
T1 S1 E1 A1 P0 I1 R1 Te0 Ap1 Se1
- Failed P: `chartConfig` is recreated on every render (line 55); should be `useMemo`. No `React.memo` wrapper.
- Failed Te: no `metric-chart.test.tsx`.

### File: mind-alive-card.tsx — 9/10 — ENTERPRISE
T1 S1 E1 A1 P0 I1 R1 Te1 Ap1 Se1
- Failed P: no `React.memo`; re-renders whenever any store slice changes upstream since `useDashboardStore((s) => s.status)` re-subscribes the full object.

### File: neural-mesh.tsx — 9/10 — ENTERPRISE
T1 S1 E1 A1 P1 I1 R1 Te0 Ap1 Se1
- Failed Te: no test file (pure-CSS decorative component — low priority but still missing).
- Note: correctly declared `aria-hidden="true"` and CSS-only animations with documented RGBA rationale.

### File: permission-dialog.tsx — 8/10 — ENTERPRISE
T1 S1 E1 A1 P0 I1 R1 Te0 Ap1 Se1
- Failed P: `sorted = [...permissions].sort(...)` runs on every render (line 185); should `useMemo`.
- Failed Te: no `permission-dialog.test.tsx` — **critical**: this is the security-consent gate and has zero interaction coverage (approve/close/sort by risk).
- Note A: dialog uses Radix-based primitives so focus trap + Escape close are inherited. High-risk warnings render emoji `⚠️` inside text nodes — not announced as severity to SR.

### File: plugin-badges.tsx — 6/10 — DEVELOPED
T1 S1 E1 A0 P0 I0 R1 Te0 Ap1 Se1
- Failed A: `BadgeBase` uses `<span>` with `title` only. `CategoryBadge` renders decorative emoji as child text (not aria-hidden), so SR reads "money bag emoji finance".
- Failed I: `CATEGORY_ICONS` maps keys to emoji (finance: "💰", etc.); the category string itself is rendered unwrapped (`<span>{category}</span>`) without translation.
- Failed P: no memoization on any of the 4 exported badges — they are primitive props-only components and ideal `React.memo` candidates (high call volume in card/detail).
- Failed Te: no test.

### File: plugin-card.tsx — 5/10 — DEVELOPED
T1 S1 E1 A0 P0 I0 R1 Te0 Ap1 Se1
- Failed T: `STATUS_STYLES[status as keyof typeof STATUS_STYLES]` (line 67) — narrows a `string` via `as keyof`. Also `RISK_DOTS` stores emojis rendered as fallback label, breaking semantic typing.
- Failed A: `aria-label` on the main button (line 272) is OK, but the kebab-menu `button` has `aria-label="Plugin actions"` **hardcoded in English** (line 181). The menu itself has no `role="menu"` / `role="menuitem"`, no arrow-key navigation, no `aria-expanded` tied to `open`.
- Failed I: `aria-label="Plugin actions"` (line 181) hardcoded. Risk dots are emoji (`🟢🟡🔴`) read verbatim by SR.
- Failed P: component is non-memoized; `QuickActions` sets up `document.addEventListener("mousedown"/"keydown")` per instance — in a list of 20 plugins this is 40 document listeners. Should lift to parent or use single delegated handler. `highestRisk(plugin.permissions)` recomputes every render.
- Failed Te: no `plugin-card.test.tsx` despite complex state (menu open, action loading, toast paths).

### File: plugin-detail.tsx — 4/10 — DEVELOPED
T0 S1 E1 A0 P0 I0 R1 Te0 Ap1 Se0
- Failed T: raw hsl interpolation using `nameToHue(detail.name)` inline at line 324 (manually templated CSS) — bypasses `LetterAvatar`. `detail.status as keyof typeof STATUS_CONFIG` (line 303). Also **the helper `nameToHue` is duplicated** (line 692) inside the same file — dead code or shadowing risk.
- Failed A: `Section` collapsible button (line 91) has no `aria-expanded` / `aria-controls` binding the disclosure state to the region below. `ToolItem` expand button similarly lacks `aria-expanded`. The Confirm dialog's close button is a generic `<button>` not using the Dialog close semantics.
- Failed I: toast keys `t("actions.enabled")` etc. are fine, but the helper-level string building for status (`t(\`status.${detail.status}\`)` etc.) depends on the raw backend enum — OK. But the status-row *order* and layout "Status + Actions Row" don't name-label the region for SR. `setActionError(msg)` text is translated. Marginal pass overall — still, confirmation dialog title/desc keys `actions.${confirmAction}ConfirmTitle` rely on dynamic keys (i18n tooling lint-bypass risk).
- Failed P: renders a giant `<pre>` with `JSON.stringify(manifest, null, 2)` (line 628) and `JSON.stringify(tool.parameters, null, 2)` (line 178) per tool — unbounded, not lazy-loaded, not clamped. Many `useState` toggles without memoization; every section re-renders when any action runs. `sorted` / `highestRisk` recompute per render.
- Failed Te: **no `plugin-detail.test.tsx`** despite being the most complex component in this batch (10+ states, confirm flow, permission dialog, toast feedback).
- Failed Se: **duplicate `nameToHue` function** is a code smell but not exploitable. However `manifest.homepage` link uses `target="_blank" rel="noopener noreferrer"` correctly (line 411). The raw `<pre>` JSON dump of `manifest` surfaces arbitrary backend data to the DOM — no escape needed (text node) but no redaction of secrets if they ever leak into manifest fields.

### File: stat-card.tsx — 9/10 — ENTERPRISE
T1 S1 E1 A1 P0 I1 R1 Te1 Ap1 Se1
- Failed P: no `React.memo`; used in overview grid where parent re-renders on every WS event. Also `<Shimmer>` recreates `backgroundImage` inline-style object per render.

### File: status-dot.tsx — 10/10 — ENTERPRISE
T1 S1 E1 A1 P1 I1 R1 Te1 Ap1 Se1
- Pure, tested, accessible (role=status, aria-label from i18n), reduced-motion respected. Gold-standard leaf component.

### File: usage-card.tsx — 9/10 — ENTERPRISE
T1 S1 E1 A1 P0 I1 R1 Te1 Ap1 Se1
- Failed P: `fillGaps(statsHistory)` and `costData` recompute on every render (lines 111-112); should `useMemo(() => fillGaps(statsHistory), [statsHistory])`. Instantiating a `new Date` inside a `for` loop with mutation is fine but allocation-heavy on re-render.

### File: welcome-banner.tsx — 8/10 — ENTERPRISE
T1 S1 E1 A1 P0 I1 R1 Te1 Ap1 Se1
- Failed P: inline template-literal className strings mean Tailwind JIT recomputes style on every render; `WelcomeStep` is declared **inside** the module as a named function but not memoized. Acceptable at 3 steps; structurally non-enterprise at scale.
- Note A: `aria-current="step"` / `role="progressbar"` / `aria-valuenow` on the progress bar are correctly wired.

---

## Top issues across Part B

1. **React.memo hygiene is absent.** Only `status-dot` passes P cleanly. Every card/row/bubble/badge is un-memoized even where used in hot paths (chat-thread, log-row, plugin list, overview grid). Three fixes dominate: wrap leaf presentational components in `React.memo`, `useMemo` expensive derivations (`fillGaps`, `sorted`, `reversed`, `highestRisk`, `overallStatus`, `groupEntries`), and `useCallback` stable handlers.
2. **Virtualization missing where it matters.** `chat-thread.tsx` and `cognitive-timeline.tsx` render every item directly; at 1k+ items they will stall. `log-row.tsx` *is* the virtual-list item but isn't memoized and isn't keyboard-operable — defeats the purpose.
3. **Keyboard accessibility on click-only divs.** `log-row.tsx` (line 56) attaches `onClick` to a `<div>` with no role/tabIndex/keyDown — blocks keyboard users from expanding ERROR entries. `brain-graph` canvas has no keyboard alternative. `plugin-card` kebab menu has no `aria-expanded`/menu semantics. Anti-pattern #4 per CLAUDE.md criteria.
4. **Hardcoded English strings.** `channel-badge` (Telegram/Discord/Signal/CLI/API labels), `chat-bubble` (no i18n at all), `letter-avatar` ("Sovyx Mind"), `plugin-card` ("Plugin actions"), `plugin-badges` (category names unwrapped), emoji-as-label patterns across badges. i18n coverage is partial.
5. **Test coverage is sparse.** 23 components, only 9 test files; critical gaps: `log-row`, `plugin-detail` (most complex), `plugin-card`, `permission-dialog` (security-consent gate), `chat-bubble`/`chat-thread`, `health-grid`, `metric-chart`, all 4 badges in `plugin-badges`. `brain-graph.test.tsx` tests pure functions extracted from the component — not the component itself.
6. **Type-system escape hatches.** `brain-graph` uses `as unknown as { x: number }`; `cognitive-timeline` casts `d.channel/names/importance`; `plugin-card` and `plugin-detail` use `as keyof typeof`. All fixable with proper node/entry typing.
7. **Duplicate code.** `plugin-detail.tsx` redeclares `nameToHue` (line 324 inline usage + line 692 helper) — same hash also duplicated in `plugin-card.tsx`. A single `@/lib/color-hash` util would dedupe.
8. **Security: unbounded `JSON.stringify` dumps to DOM.** `plugin-detail.tsx` dumps raw manifest & tool parameters into `<pre>` blocks — no size clamp, no secret redaction. `log-row.tsx` does the same for `extraFields`. Text-node context makes it non-exploitable, but a hostile backend or leaked credential field would render verbatim.
9. **Emoji-as-semantics.** Plugin risk dots (🟢🟡🔴), category icons (💰🌤️), channel icons (✈️💬🔒) are all screen-reader-visible text. Should be wrapped in `aria-hidden="true"` or replaced with icon components + `aria-label`.
10. **`StatusDot`, `MindAliveCard`, `UsageCard`, `StatCard`, `NeuralMesh`, `HealthGrid`, `WelcomeBanner`, `MetricChart`, `PermissionDialog`, `ActivityFeed` form the enterprise core.** Everything plugin-related (`plugin-card`, `plugin-detail`, `plugin-badges`) plus the chat pair (`chat-bubble`, `chat-thread`) and `log-row` need a concerted pass focused on memoization, keyboard a11y, and tests before they clear the enterprise bar.
