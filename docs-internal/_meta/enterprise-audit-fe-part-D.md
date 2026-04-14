# Enterprise Audit FE — Part D (stores + hooks + lib + types)

Scoring legend: each criterion 0/1. Total /10. ENTERPRISE 8-10, DEVELOPED 5-7, NOT-ENT 0-4.

Columns: 1=Types 2=State 3=ErrBound 4=A11y 5=Perf 6=i18n 7=Responsive 8=Testing 9=API 10=Security.

## Summary

| Group     | Files | Avg | ENTERPRISE | DEVELOPED | NOT-ENT |
|-----------|------:|----:|-----------:|----------:|--------:|
| stores    |    13 | 8.5 |         10 |         3 |       0 |
| hooks     |     4 | 8.8 |          4 |         0 |       0 |
| lib       |     5 | 8.4 |          4 |         1 |       0 |
| types     |     1 |   9 |          1 |         0 |       0 |
| **TOTAL** |    23 | 8.6 |         19 |         4 |       0 |

Overall classification: **ENTERPRISE** (19/23 files). No NOT-ENT offenders.

## stores (13 files: 1 root + 12 slices)

| File                       | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | Tot | Class      |
|----------------------------|---|---|---|---|---|---|---|---|---|----|----:|------------|
| stores/dashboard.ts        | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1  |  10 | ENTERPRISE |
| slices/activity.ts         | 1 | 1 | 0 | 1 | 1 | 0 | 1 | 1 | 1 | 1  |   8 | ENTERPRISE |
| slices/auth.ts             | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1  |  10 | ENTERPRISE |
| slices/brain.ts            | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1  |  10 | ENTERPRISE |
| slices/chat.ts             | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1  |  10 | ENTERPRISE |
| slices/connection.ts       | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 0 | 1 | 1  |   9 | ENTERPRISE |
| slices/conversations.ts    | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1  |  10 | ENTERPRISE |
| slices/logs.ts             | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1  |  10 | ENTERPRISE |
| slices/onboarding.ts       | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1  |  10 | ENTERPRISE |
| slices/plugins.ts          | 1 | 1 | 0 | 1 | 1 | 0 | 1 | 1 | 1 | 1  |   8 | ENTERPRISE |
| slices/settings.ts         | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 0 | 1 | 1  |   9 | ENTERPRISE |
| slices/stats.ts            | 1 | 1 | 1 | 1 | 1 | 0 | 1 | 1 | 1 | 1  |   9 | ENTERPRISE |
| slices/status.ts           | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1  |  10 | ENTERPRISE |

Notes:
- **activity.ts**: error swallowed with hardcoded English "Failed to load timeline" (no i18n key). Same for plugins.ts ("Failed to load plugins").
- **stats.ts**: also hardcodes "Failed to fetch stats" fallback (used only when `err.message` absent).
- **logs.ts**: excellent ring-buffer implementation, correct MAX_LOGS trim-from-10% strategy avoids O(n) append pathology. Cost accumulation logic is deterministic + rounded.
- **plugins.ts**: optimistic update with proper rollback on failure — gold-standard pattern. The `as PluginStatus` casts are loose but safe.
- **connection.ts / settings.ts**: trivial slices without dedicated tests, but covered indirectly via `slices.test.ts` and integration.
- No direct mutations anywhere; every action uses immutable spread. StateCreator typed with DashboardState union.

## hooks (4 files)

| File                | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | Tot | Class      |
|---------------------|---|---|---|---|---|---|---|---|---|----|----:|------------|
| use-auth.ts         | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 0 | 1  |   9 | ENTERPRISE |
| use-mobile.ts       | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1  |  10 | ENTERPRISE |
| use-onboarding.ts   | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1  |  10 | ENTERPRISE |
| use-websocket.ts    | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 0  |   9 | ENTERPRISE |

Notes:
- **use-auth.ts**: uses raw `fetch()` against `/api/status` instead of `api.get()`. Self-inflicted duplication of auth-header logic; the centralized 401 handler in `api.ts` is skipped. Recommend `api.get<SystemStatus>("/api/status")` with explicit 401/403 catch. `ready` flag is essentially = `authenticated` which is misleading (returns false on error path until network resolves). Network-unreachable branch sets `authenticated=true` — risky UX: allows "optimistic access" with unverified token. Defensible but undocumented attack surface.
- **use-websocket.ts**: exemplary reconnect. Exponential backoff 1s→30s with reset on open, mountedRef guard, debounce map with per-key trailing-edge timers, proper cleanup (closes WS, clears all debounce timers). Plugin event dispatched correctly. **Security nit**: token passed as query-string param (`?token=...`) — inevitable given browser WS API, but this will appear in server access logs unless the backend filters it. Ensure backend strips `token` from access-log lines. Also, `WS_BASE` falls back to `ws://` on `window.location.host`, which on an HTTPS deployment would mix-content-fail rather than upgrade to `wss://` — should branch on `location.protocol`.
- **use-mobile.ts / use-onboarding.ts**: textbook. Onboarding is purely derived (memoized), no stored step state — matches "backend is source of truth" principle. Boundary tests cover 768 / 767 / exact-5 / exact-4 cases.

## lib (5 files)

| File         | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | Tot | Class      |
|--------------|---|---|---|---|---|---|---|---|---|----|----:|------------|
| api.ts       | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 0 | 0  |   8 | ENTERPRISE |
| constants.ts | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 0 | 1 | 1  |   9 | ENTERPRISE |
| format.ts    | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1  |  10 | ENTERPRISE |
| i18n.ts      | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 0 | 1 | 1  |   9 | ENTERPRISE |
| utils.ts     | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 0 | 1 | 1  |   9 | ENTERPRISE |

Notes on `lib/api.ts` (the critical file):
- **No timeout / AbortSignal default**: caller must provide its own AbortController. For long-stalled requests (backend hang, network blackhole) the fetch never rejects.
- **No retry / backoff**: every caller gets a single shot. Acceptable trade-off (most callers tolerate failure + WS refresh), but undocumented.
- **No token-refresh flow**: on 401, token is wiped and modal opens. Fine for static-token model; no refresh-token semantics.
- **Lazy import of store inside error path** (`await import("@/stores/dashboard")`) — correctly avoids the circular dependency but creates an async gap between clearing the token and showing the modal. On 401 burst, multiple imports in flight. Tolerable since `localStorage.removeItem` is idempotent, but could be extracted to a callback injected at bootstrap.
- **Security concern**: token in `localStorage` is vulnerable to XSS exfiltration. No CSP enforced at this layer. Acceptable for self-hosted single-tenant dashboard, but should be called out in SECURITY.md. Score dropped on #10.
- **API layer coverage**: no `patch` verb, no multipart/FormData helper, no query-string builder (callers concatenate manually — see `activity.ts` `?hours=${hours}&limit=${limit}`). This invites XSS/injection when params come from user input; today they don't, but it's a latent risk. Score dropped on #9.
- **utils.ts / constants.ts / i18n.ts**: no sibling tests, but utility is single-line or config. Acceptable.

## types (1 file)

| File         | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | Tot | Class      |
|--------------|---|---|---|---|---|---|---|---|---|----|----:|------------|
| types/api.ts | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 0  |   9 | ENTERPRISE |

Notes:
- Zero `any`. Well-documented, sections delimited. `WsEvent<T>` generic is correct. `LogEntry` index-signature `[key: string]: unknown` matches structlog freeform pairs.
- Completeness: mirrors 14 WS event types, health, status, conversation, brain, logs, timeline, stats, chat, settings, mind config, plugins. Internally consistent.
- Minor drift risk: `MindConfigUpdateResponse.changes: Record<string, string>` — backend may emit nested diffs; if so, this becomes a lie in silent ways. No runtime validation (no zod / io-ts) to catch drift. Score dropped on #10 (contract safety).
- `PluginDetail.manifest` union `PluginManifestData | Record<string, never>` is awkward; `PluginManifestData | null` would be cleaner.

## Top issues across D

1. **No runtime schema validation (highest-severity latent risk)**. `types/api.ts` is compile-time only. Any backend contract drift (renamed field, type change) fails silently in the browser until a specific code path touches it. Add zod (or typia) at `api.request()` — parse response against a schema per endpoint; throw ApiError("schema_mismatch") on divergence. This closes the biggest gap between backend and frontend and eliminates entire classes of "the dashboard renders undefined" bugs.
2. **`use-auth.ts` bypasses `api.ts`**. Raw fetch duplicates header wiring and skips the centralized 401-handler. Migrate to `api.get<SystemStatus>("/api/status")` and catch `ApiError` with `status === 401 || status === 403`. Also: the network-unreachable branch silently `setAuthenticated(true)` — document explicitly or gate behind a feature flag; a silently-accepted unverified token is unusual security posture.
3. **Token lifecycle / XSS exposure**. `sovyx_token` in `localStorage` is readable by any script. For a self-hosted single-tenant tool this is accepted, but call it out in security docs and enforce a strict CSP (`script-src 'self'`). The WS token-in-query-string needs backend log-scrubbing to avoid leakage.
4. **Missing fetch timeout + retry**. No default AbortController timeout in `api.request()`. A hung backend freezes affected UI indefinitely. Add a default 15s AbortController (overridable). Retry policy is out-of-scope but at least one retry on `TypeError` (network failure) would help flaky Wi-Fi.
5. **Hardcoded error strings in slices** (`activity.ts`, `plugins.ts`, `stats.ts`). User-visible fallback messages bypass i18n. Either funnel through `common:errors.*` keys or standardize on a sentinel (`"error.timeline.load"`) that components translate.
6. **WS URL protocol**. `VITE_WS_URL ?? "ws://" + host` — on HTTPS this breaks. Derive from `window.location.protocol === "https:" ? "wss://" : "ws://"`.
7. **`api.ts` missing PATCH verb and no typed query-string helper**. Inline string concat invites injection once any query param becomes user-controlled.
8. **Lazy store import inside `api.ts`** couples the API layer to a specific store module. A callback-style `onUnauthorized` injected from bootstrap would decouple and make `api.ts` reusable.
9. **`plugins.ts`: `as PluginStatus` string casts** in WS handler could silently accept an unknown state value. Gate with `in ("active"|"disabled"|"error")` narrowing.
10. **Defense-in-depth test coverage**. `connection.ts` and `settings.ts` lack dedicated test files (covered indirectly). Tests exist for all behavior but auditor visibility suffers. Add 3-assertion stubs for completeness.

## Verdict

Part D is **ENTERPRISE** (19/23 ≥ 8). The Zustand slice architecture, WS hook with per-key debounce, and type completeness are textbook. The remaining defects cluster in two areas: (a) no runtime schema validation between backend and frontend, (b) `api.ts` minimalism (no timeout, no typed query-string, lazy store import). Both are addressable in a focused day of work. None are structural.
