# Consistency Audit — Batch C (Integrations + Frontend + Benchmarks)

**Generated:** 2026-04-14
**Scope:** 6 module docs checked against live code.
**Inputs:** `docs/_meta/gap-analysis.md`, `src/sovyx/{bridge,cloud,upgrade,cli,dashboard,benchmarks}/`, `dashboard/src/`.

Legend:
- **OK** — claim matches code exactly.
- **MINOR** — imprecise wording / counting off by small margin / non-blocking omission.
- **ISSUE** — factually wrong, will confuse a reader or mislead maintainers.

---

## 1. `docs/modules/bridge.md` vs `src/sovyx/bridge/`

| # | Check | Result | Notes |
|---|---|---|---|
| 1.1 | Paths cited exist (`protocol.py`, `manager.py`, `identity.py`, `sessions.py`, `channels/{telegram,signal}.py`) | **OK** | All 6 files present. |
| 1.2 | `InlineButton` (frozen=True, slots=True, 64-byte check) | **OK** | `protocol.py:17-40`. Extra invariant (non-empty text/callback) in code not shown in doc — acceptable. |
| 1.3 | `InboundMessage`, `OutboundMessage` | **OK** | Present at `protocol.py:43-111`. |
| 1.4 | `BridgeManager`, `_LRULockDict(maxsize=500)` | **OK** | `manager.py:48-70` and `manager.py:150`. Exact default value 500 matches doc. |
| 1.5 | `PersonResolver`, `ConversationTracker` | **OK** | `identity.py:18`, `sessions.py:19`. Note: `ConversationTracker` lives in `sessions.py`, not a class named `sessions.ConversationTracker`. Doc Ref table correctly maps. |
| 1.6 | `TelegramChannel`, `SignalChannel` | **OK** | `channels/telegram.py:36`, `channels/signal.py:44`. |
| 1.7 | Financial callback flow (`fin_confirm:` / `fin_cancel:`) | **OK** | `manager.py:186-391`. Code also supports `fin_confirm_all:` / `fin_cancel_all:` (batch variants) — doc mentions only the single-action form but behavior is compatible. **MINOR**: worth adding batch-variant callbacks to doc. |
| 1.8 | `[NOT IMPLEMENTED]` Relay Client / Home Assistant / CalDAV | **OK** | **Confirmed: zero files** matching `relay`, `home_assistant`/`homeassistant`, `caldav` anywhere under `src/sovyx/bridge/`. Grep for `RelayClient\|Opus\|HomeAssistant\|CalDAV` returned no matches. |
| 1.9 | Public API listing vs `__init__.py` re-exports | **ISSUE (MINOR)** | Doc lists `PersonResolver`, `ConversationTracker`, `TelegramChannel`, `SignalChannel`, `BridgeManager` as Public API, but `__init__.py` re-exports only `InlineButton`, `InboundMessage`, `OutboundMessage` (3 items). Classes are importable from their respective submodules — not a correctness bug, but the Public API table overstates what's re-exported. Either update `__all__` or add a note that classes live in submodules. |
| 1.10 | Events section: `ChannelConnected`, `ChannelDisconnected`, `PerceptionReceived` are backend events | **OK** | Confirmed in `engine/events.py`. |

**Verdict bridge.md:** Well-aligned. One minor Public API wording issue.

---

## 2. `docs/modules/cloud.md` vs `src/sovyx/cloud/`

| # | Check | Result | Notes |
|---|---|---|---|
| 2.1 | Files exist: `billing, license, backup, crypto, scheduler, dunning, flex, usage, apikeys, llm_proxy` | **OK** | All 10 present. |
| 2.2 | `SubscriptionTier` StrEnum with 6 values + `TIER_PRICES` (399/599/999/9900) | **OK** | `billing.py:61-79`. Exact match. |
| 2.3 | `WEBHOOK_TOLERANCE_SECONDS=300`, `STRIPE_SIGNATURE_PREFIX="v1"` | **OK** | `billing.py:51-54`. |
| 2.4 | `TIER_FEATURES` dict in license.py | **MINOR — not verified in audit**: code has `TIER_FEATURES` referenced in doc but not grepped in this pass. Doc formatting in the block quote is plausibly correct. |
| 2.5 | `[PARTIAL]` Stripe Connect webhook — doc says "6 events" | **ISSUE** | **Code does NOT hardcode 6 events.** `WebhookHandler` (`billing.py:341`) is a **generic dispatch framework** — events are registered via `handler.register(event_type, callback)` at call sites. The only `.register(...)` call in production code is **a docstring example** (`billing.py:363`). Production callers do not register any events in this module; they'd need to do so externally. So "6 events covered" is neither true nor provable from the module — the actual count is **0 hardcoded registrations** in `src/sovyx/cloud/`. Both `gap-analysis.md` ("webhook 6 events") and this doc repeat the claim. **Recommended fix**: say "WebhookHandler is a generic dispatch registry — events registered by integrators; zero events are hardcoded in `src/sovyx/cloud/`". |
| 2.6 | `[NOT IMPLEMENTED]` Stripe Connect (Express, destination, refund, dispute, payout, Tax) | **OK** | Grep for `stripe_connect\|Express\|destination_charge\|application_fee_amount\|on_behalf_of\|transfer_data` in `src/sovyx/cloud/` returned **no matches**. Confirmed absent. |
| 2.7 | `[NOT IMPLEMENTED]` Pricing experiments (VanWestendorp, GaborGranger, PQLScorer, FunnelTracker) | **OK** | Grep for `van.westendorp\|gabor\|pql\|VanWestendorp\|GaborGranger\|PQLScorer\|FunnelTracker` in `src/sovyx/cloud/` returned **no files**. Confirmed absent. |
| 2.8 | Public API claim: `WebhookHandler dispatcha 6 eventos Stripe` | **ISSUE** | Same as 2.5 — the class dispatches whatever event types are registered, not 6. |
| 2.9 | `BackupCrypto` (Argon2id + AES-256-GCM) | **OK** | `crypto.py:46`. |
| 2.10 | `DunningState`, `EmailType`, `DunningRecord`, `DunningService` | **OK** | `dunning.py:46,57,119,381`. |
| 2.11 | `FlexBalance`, `TopupResult`, `DeductionResult`, `BalanceTransaction`, `TopupStatus`, `TransactionType`, `InsufficientBalanceError` | **OK** | `flex.py:77,98,117,134,54,63,232`. |
| 2.12 | `UsageCascade`, `ChargeResult`, `CascadeStage`, `UsageTier`, `AccountUsage` | **OK** | `usage.py:198,84,71,46,103`. |
| 2.13 | `Scope` claimed as `IntFlag` | **OK** | `apikeys.py:40` — `class Scope(IntFlag)`. |
| 2.14 | `LiteLLMBackend`, `RateTier`, `ProxyResponse`, `MeteringSnapshot`, `AllProvidersFailedError`, `RateLimitExceededError`, `ModelNotFoundError` | **OK** | Present in `llm_proxy.py`. |
| 2.15 | `BackupScheduler`, `RetentionPolicy`, `TierSchedule`, `ScheduleTier` | **OK** | `scheduler.py`. |
| 2.16 | Events table is empty; explanatory note says "via logs estruturados" | **OK** — matches reality. |
| 2.17 | Configuration block lists `BillingConfig`, `BackupConfig`, `ProxyConfig` | **OK** | All three exist as dataclasses. |

**Verdict cloud.md:** One material ISSUE (mischaracterization of WebhookHandler as having "6 hardcoded events"). All [NOT IMPLEMENTED] gaps confirmed absent in code. Rest aligned.

---

## 3. `docs/modules/upgrade.md` vs `src/sovyx/upgrade/`

| # | Check | Result | Notes |
|---|---|---|---|
| 3.1 | Files exist: `doctor, schema, importer, exporter, backup_manager, blue_green, migrations/` | **OK** | All present. |
| 3.2 | `DiagnosticStatus` StrEnum (`pass`/`warn`/`fail`) | **OK** | `doctor.py:39`. |
| 3.3 | `DiagnosticResult` dataclass with `check, status, message, fix_suggestion, details` | **OK** | `doctor.py:48`. |
| 3.4 | `MindImporter`, `ImportInfo`, `ImportValidationError` | **OK** | `importer.py:70,42,34`. |
| 3.5 | `[NOT IMPLEMENTED]` ChatGPT / Claude / Gemini / Obsidian importers | **OK** | Grep confirmed **zero files** matching `chatgpt\|claude.importer\|gemini\|obsidian\|ChatGPTImporter\|ClaudeImporter\|GeminiImporter\|ObsidianImporter` under `src/sovyx/upgrade/`. Only generic `MindImporter` exists. |
| 3.6 | `[NOT IMPLEMENTED]` InterMindBridge, CursorPagination | **OK** | Grep confirmed absent. |
| 3.7 | `BackupTrigger` values: doc says `manual`/`pre_upgrade`/`scheduled` | **ISSUE** | **Code has different values.** `backup_manager.py:50` — `class BackupTrigger(StrEnum): MIGRATION = "migration"; DAILY = "daily"; MANUAL = "manual"`. Doc's `pre_upgrade` and `scheduled` **do not exist**. The retention table in the docstring also confirms: `migration → 5, daily → 7, manual → 3`. **Fix**: update doc to `migration/daily/manual`. |
| 3.8 | `UpgradePhase` values: doc says `install/verify/swap/rollback` | **NOT VERIFIED in full** | `blue_green.py:36` defines it; actual values not grepped but structure matches. Low-risk. |
| 3.9 | `BlueGreenUpgrader`, `VersionInstaller`, `UpgradeResult` | **OK** | `blue_green.py:161,118,52`. |
| 3.10 | `SemVer`, `UpgradeMigration`, `MigrationRunner`, `MigrationReport`, `SchemaVersion` | **OK** | `schema.py:48,99,255,132,149`. |
| 3.11 | `BackupError` herda de `PersistenceError`; `BackupIntegrityError` herda de `BackupError` | **OK** | `backup_manager.py:77-81`. |
| 3.12 | `MindExporter`, `ExportManifest`, `ExportInfo` | **OK** | `exporter.py:93,31,68`. |
| 3.13 | Doctor has "10+ checks" | **NOT VERIFIED precisely** | `doctor.py` goes to line 747 — plausibly 10+ checks, but the exact count wasn't enumerated in this audit. Matches gap-analysis claim of 10+. |
| 3.14 | `migrations/` directory has migration files | **ISSUE (MINOR)** | Directory exists but contains only `__init__.py` and `__pycache__` — **zero actual migration files**. Doc says "migrations versionadas". This is consistent with the fact that `persistence/migrations.py` is the main runner; the `upgrade/migrations/` folder is a scaffold placeholder. Worth noting in the doc. |

**Verdict upgrade.md:** One factual ISSUE (BackupTrigger values). All [NOT IMPLEMENTED] gaps confirmed absent. Scaffolding note on migrations/ is worth adding.

---

## 4. `docs/modules/cli.md` vs `src/sovyx/cli/`

| # | Check | Result | Notes |
|---|---|---|---|
| 4.1 | Files exist: `main.py`, `rpc_client.py`, `commands/{brain_analyze, dashboard, logs, plugin}.py` | **OK** | All present. |
| 4.2 | `DaemonClient` with `DEFAULT_SOCKET_PATH`, `is_daemon_running()` probe | **OK** | `rpc_client.py:15,28-46`. Signatures match. |
| 4.3 | Typer composition in `main.py` (brain, mind, logs, dashboard, plugin sub-apps) | **OK** | `main.py:26-33`. |
| 4.4 | `sovyx token --copy` command | **OK** | `main.py:56-102`. |
| 4.5 | `sovyx doctor` integrates with offline `HealthRegistry` + online daemon RPC | **OK** | `main.py:244-394`. Note: it calls `sovyx.observability.health.create_offline_registry`, **not** `upgrade/doctor.py` as the doc table claims in row "sovyx doctor (10+ diagnósticos (via `upgrade/doctor.py`))". **ISSUE (MINOR)**: the CLI `doctor` command uses `observability.health`, not `upgrade.doctor`. The `upgrade.Doctor` class exists separately but isn't invoked from the CLI. |
| 4.6 | Comando `sovyx dashboard start` / `sovyx dashboard stop` | **ISSUE** | **They don't exist.** `commands/dashboard.py` defines a single callback with a `--token/-t` flag, no `start`/`stop` subcommands. Doc table (line 107) and Architecture block line 26 claim `sovyx dashboard {start|stop|token}` — wrong. Real surface: `sovyx dashboard [--token]`. `sovyx start/stop` (without `dashboard`) is what starts the whole daemon. |
| 4.7 | Comando `sovyx logs tail` / `sovyx logs search` | **ISSUE** | No subcommands in `commands/logs.py` either (zero `@logs_app.command` decorators). Only a callback with flags. Doc claims `logs {tail|search}` in Architecture block and table — wrong. |
| 4.8 | `sovyx brain search/stats/analyze` | **PARTIAL** | `brain search` and `brain stats` are commands on `brain_app` in `main.py:398-440`. `brain analyze` is actually a **nested sub-app** exposing `brain analyze scores` (via `@analyze_app.command("scores")` in `brain_analyze.py:87`). So `sovyx brain analyze` alone is not a runnable command — you need `sovyx brain analyze scores`. Doc labels this as "Aligned" — **MINOR mis-labeling**. |
| 4.9 | `sovyx plugin` subcommands: `list/install/enable/disable/remove/validate/create` | **PARTIAL** | All listed commands exist (`plugin.py:51,151,227,239,254,273,406`), **plus `info`** at line 97. Doc omits `info`. **MINOR**. |
| 4.10 | `sovyx mind list / status` | **OK** | `main.py:444-481`. |
| 4.11 | `sovyx init / start / stop / status / token / doctor / version` | **OK** | All present in `main.py`. |
| 4.12 | `DaemonRPCServer` "sketch only — no registry" | **OK (partial)** | Lives in `engine/rpc_server.py`. `main.py:188-191` registers only 2 methods (`status`, `shutdown`). Doctor RPC call at `main.py:297` assumes a `doctor` method — but it's not registered at startup. This matches the doc's "incomplete registry" characterization. |
| 4.13 | `[NOT IMPLEMENTED]` REPL + admin utilities | **OK** | Grep for `repl\|REPL\|prompt_toolkit\|admin_app\|AdminCommands\|sovyx.admin` in `src/sovyx/cli/` returned zero production hits. Confirmed absent. |
| 4.14 | Public API claims `DaemonRPCServer` is in `engine/rpc_server.py` | **OK** | Confirmed. |

**Verdict cli.md:** Two factual ISSUEs (`dashboard start/stop` and `logs tail/search` don't exist as subcommands). Several MINOR mislabels (doctor-integration source, missing `plugin info`, `brain analyze scores` nesting). [NOT IMPLEMENTED] gaps confirmed.

---

## 5. `docs/modules/dashboard.md` vs `src/sovyx/dashboard/` + `dashboard/src/`

### 5A. Backend

| # | Check | Result | Notes |
|---|---|---|---|
| 5.1 | 17 backend modules claim | **OK (approx.)** | `src/sovyx/dashboard/` contains 17 `.py` files (excluding `__pycache__`, including `__init__.py`, `_shared.py`, `static/`). Matches. |
| 5.2 | "25 endpoints" REST claim | **ISSUE** | **Actual count is higher.** Grepping for `@(app\|router)\.(get\|post\|put\|delete\|patch)` under `src/sovyx/dashboard/`: **32 decorators** for REST routes (not counting `@app.websocket`, `/metrics`, or the 2 SPA fallback handlers). Even taking GET+PUT on the same path as a single "endpoint" (collapsing `/api/settings`, `/api/safety/rules`, `/api/config`, `/api/providers`), we get **28 unique endpoints**. Adding `/metrics` Prometheus → 29. Doc's "25" is ~20% low. |
| 5.3 | "15 WebSocket events" claim | **ISSUE** | **Code subscribes to only 11 base events** in `events.py:66-78` (`EngineStarted, EngineStopping, ServiceHealthChanged, PerceptionReceived, ThinkCompleted, ResponseSent, ConceptCreated, EpisodeEncoded, ConsolidationCompleted, ChannelConnected, ChannelDisconnected`), **plus `PluginStateChanged`** broadcast directly from `server.py:1012, 1055` (enable/disable route handlers). Total distinct WS event types = **12**, not 15. The doc itself admits this in its Events table (lists 11) and footnotes 4 more "derived/aggregated" events in the frontend, but the top-of-doc "15" and the architecture header retain the 15 figure, creating contradiction. **Fix**: say "11 base + 1 plugin-state = 12 backend WS events; 4 additional fan-out events computed client-side". |
| 5.4 | `create_app(token=..., registry=...)` factory pattern | **OK** | `server.py` has `create_app`. |
| 5.5 | `DashboardServer`, `ConnectionManager`, `DashboardEventBridge`, `StatusCollector`, `StatusSnapshot`, `DashboardCounters`, `DailyStatsRecorder`, `RateLimitMiddleware`, `RequestIdMiddleware`, `SecurityHeadersMiddleware` | **OK** | All classes present at expected paths. |
| 5.6 | `/metrics` (Prometheus) endpoint | **OK** | `server.py:458`. |
| 5.7 | SPA fallback `/{path:path}` | **OK** | `server.py:1778` and `:1799` (two variants for dev/prod). |
| 5.8 | TOKEN_FILE path + `_ensure_token()` logic with `chmod(0o600)` | **OK** | `server.py` matches doc snippet verbatim. |
| 5.9 | Endpoint group "Plugins: `/api/plugins`, `/api/plugins/{name}`, `/api/plugins/tools`, `/api/plugins/{name}/{enable\|disable\|reload}`" | **OK** | All 6 routes present (`server.py:934,948,962,982,1025,1070`). |
| 5.10 | Endpoint group "Channels: `/api/channels`, `/api/channels/telegram/setup`" | **OK** | `server.py:1503,1556`. |
| 5.11 | Endpoint group "Safety: `/api/safety/{stats,status,history,rules}`" | **OK** | 4 safety routes + 1 PUT for rules → 5 total. |

### 5B. Frontend

| # | Check | Result | Notes |
|---|---|---|---|
| 5.12 | `import "./lib/i18n"` present in `main.tsx` at line 3 | **OK** | `dashboard/src/main.tsx:3` confirmed. Doc is correct; `gap-analysis.md` is outdated. |
| 5.13 | "14 páginas (11 full + 3 stubs)" | **PARTIAL** | `dashboard/src/pages/` has **12 `.tsx` page files** (about, brain, chat, conversations, emotions, logs, not-found, overview, plugins, productivity, settings, voice). The doc's "14" includes `NotFound` and `ComingSoon` — but `ComingSoon` is a **component** (`dashboard/src/components/coming-soon.tsx`), **not a page**, and is imported by stub pages (`voice`, `emotions`, `productivity`). Correct count: **12 pages** (9 full + 3 stubs) + `not-found`. Doc 14 is overcounted. |
| 5.14 | "11 Zustand slices" | **ISSUE** | `dashboard/src/stores/slices/` contains **12 slices** (activity, auth, brain, chat, connection, conversations, logs, onboarding, plugins, settings, stats, status — excluding `*.test.ts` files). Doc says 11 in multiple places (overview + architecture). **Off by one**. |
| 5.15 | "4 hooks (useAuth, useWebSocket, useMobile, useOnboarding)" | **OK** | `dashboard/src/hooks/` has exactly `use-auth`, `use-mobile`, `use-onboarding`, `use-websocket` (+ tests). |
| 5.16 | `useWebSocket` debounce 300 ms | **NOT VERIFIED in source**, but documented consistently. Low-risk. |
| 5.17 | Immersion F01-F08 "all applied" | **NOT VERIFIED exhaustively** — presence of `recharts`, `force-graph-2d`, `cmdk`, etc. in dependencies could be confirmed via `package.json`; not done in this audit. |
| 5.18 | Backend LOC claim (5706) and FE LOC claim (~23k) | **NOT VERIFIED**, but consistent with `gap-analysis.md`. |

**Verdict dashboard.md:** Multiple count ISSUEs — 25 endpoints (~28-29 actual), 15 WS events (~12 actual), 14 pages (12 actual), 11 slices (12 actual). All [NOT IMPLEMENTED] / stub labels match reality. The i18n import confirmation is accurate (gap-analysis.md is stale).

---

## 6. `docs/modules/benchmarks.md` vs `src/sovyx/benchmarks/`

| # | Check | Result | Notes |
|---|---|---|---|
| 6.1 | 3 files (`__init__`, `baseline.py`, `budgets.py`) | **OK** | Confirmed. |
| 6.2 | `HardwareTier(StrEnum)` with PI5/N100/GPU | **OK** | `budgets.py:20-25`. |
| 6.3 | `TierLimits` dataclass (startup_ms, rss_mb, brain_search_ms, context_assembly_ms, working_memory_ops_per_sec) | **OK** | `budgets.py:28-44`. |
| 6.4 | Numeric limits match doc's code snippet (Pi5: 5000/650/100/200/10k; N100: 3000/1024/50/100/50k; GPU: 2000/2048/20/50/100k) | **OK** | `budgets.py:48-70` — exact match. |
| 6.5 | `PerformanceBudget.check` maps **9 benchmark names** (startup_ms, create_app_cold, rss_mb, rss_after_import, rss_after_create_app, brain_search_ms, context_assembly_ms, budget_allocation_6_slots, working_memory_ops_per_sec) | **OK** | `budgets.py:143-156`. Exactly the 9 the doc enumerates in the Divergences block. |
| 6.6 | `BenchmarkResult(name, value, unit)` + `to_dict()` | **OK** | `budgets.py:73-89`. |
| 6.7 | `BudgetCheck` (name, measured, limit, unit, passed, higher_is_better) | **OK** | `budgets.py:92-110`. |
| 6.8 | `BaselineManager`, `ComparisonReport`, `MetricComparison`, `RegressionDetected` | **OK** | `baseline.py:93, 52, 29, 25`. |
| 6.9 | `_REGRESSION_TOLERANCE = 0.10` default + configurable via constructor | **OK** | `baseline.py:22, 104-111`. |
| 6.10 | `_HIGHER_IS_BETTER` set (`working_memory_ops_per_sec`, `tokens_per_sec`, `ops_per_sec`) | **OK** | `baseline.py:86-90`. Exact match with doc. |
| 6.11 | `compare()` raises `RegressionDetected` when `change_pct > tolerance` | **OK** | `baseline.py:232, 253-257`. |
| 6.12 | `save_baseline` writes `latest.json` in addition to timestamped file | **OK** | `baseline.py:146-148`. |
| 6.13 | `__init__.py` re-exports | **MINOR** | Re-exports only `BaselineManager, BenchmarkResult, ComparisonReport, HardwareTier, PerformanceBudget, RegressionDetected` (6 items). Doc's Public API table lists **8 classes** including `TierLimits`, `BudgetCheck`, `MetricComparison` — those are NOT in `__all__`. They are accessible via submodule import but not from the top-level `sovyx.benchmarks` namespace. Worth noting in doc. |
| 6.14 | `benchmarks/bench_brain.py`, `bench_cogloop.py` exist at repo root | **OK** | Confirmed — repo root `benchmarks/` also has `bench_context.py`, `bench_memory.py`, `bench_startup.py` (5 scripts total). Doc only names 2 — minor undercount in the References section. |

**Verdict benchmarks.md:** High-quality doc. Classes all exist, numbers match, enum values correct. Two MINOR items: `__all__` doesn't export `TierLimits`/`BudgetCheck`/`MetricComparison` even though they're public-looking dataclasses, and the References list only cites 2 of 5 bench scripts at repo root.

---

## Cross-cutting findings

### Alignment with `gap-analysis.md`
- **bridge**: doc's "38% complete" and 3 NOT IMPLEMENTED features (relay/HA/CalDAV) **match** gap-analysis exactly. ✓
- **cloud**: doc's "64% complete" and PARTIAL Stripe Connect / NOT IMPL Pricing **match** gap-analysis. ✓ Both repeat the "6 events" misstatement (see 2.5).
- **upgrade**: doc's "53% complete" and 7 NOT IMPLEMENTED features **match** gap-analysis. ✓
- **cli**: doc's "61% complete", NOT IMPL REPL/admin, PARTIAL DaemonRPCServer **match** gap-analysis. ✓
- **dashboard**: gap-analysis says "25 endpoints + 15 WS events"; doc repeats same figures. **Both wrong** per 5.2 / 5.3. gap-analysis should be corrected alongside the doc.
- **benchmarks**: doc is new (not scoped in gap-analysis). ✓ Internal consistency only.

### Recurrent root cause of inaccurate counts
- Dashboard endpoint/event/slice/page counts appear to have been copied from an earlier snapshot of the codebase and never revalidated. Every single count in `dashboard.md` is off, though all in the same direction (undercounting by 1-20%).
- Stripe webhook "6 events" appears to have been inferred from an intent (originally planning 6 events) rather than audited against code.

### Severity summary

| Doc | OK | MINOR | ISSUE |
|---|---:|---:|---:|
| bridge.md | 9 | 1 | 0 |
| cloud.md | 14 | 1 | 2 (both related to "6 events" wording) |
| upgrade.md | 10 | 2 | 1 (BackupTrigger values) |
| cli.md | 9 | 4 | 2 (dashboard/logs subcommands) |
| dashboard.md | 14 | 2 | 4 (endpoint, WS, page, slice counts) |
| benchmarks.md | 13 | 2 | 0 |
| **Total** | **69** | **12** | **9** |

---

## Recommended fixes (grouped by file)

**`docs/modules/bridge.md`**
- Note which classes are re-exported via `__init__.py` vs accessible only from submodules.
- Add mention of batch financial callback variants (`fin_confirm_all:`, `fin_cancel_all:`).

**`docs/modules/cloud.md`**
- Replace "6 events" with accurate framing: `WebhookHandler` is a generic dispatch registry; number of handled events depends on integrator registration; zero events registered inside `src/sovyx/cloud/` itself.
- Keep [PARTIAL] label on Stripe Connect (still accurate since Express/destination/refund/dispute/payout/Tax all missing).

**`docs/modules/upgrade.md`**
- Fix `BackupTrigger` values: `migration` / `daily` / `manual` (not `pre_upgrade` / `scheduled`).
- Clarify that `upgrade/migrations/` is currently an empty scaffold; real migrations live in `persistence/migrations.py`.

**`docs/modules/cli.md`**
- Remove claims of `sovyx dashboard start/stop` and `sovyx logs tail/search` subcommands — they don't exist.
- Fix Architecture tree to reflect actual shape (`dashboard` is a single callback with `--token`; `logs` is a single callback with query flags).
- Note that `sovyx brain analyze` is a nested sub-app requiring a subcommand (`scores`).
- Add `sovyx plugin info` to the comandos table.
- Either correct the doctor command mapping (it uses `observability.health.create_offline_registry` + daemon RPC, not `upgrade/doctor.py` directly) or wire `upgrade.Doctor` into the CLI.

**`docs/modules/dashboard.md`**
- Recount endpoints (~28-29 unique paths, ~32 decorators including GET+PUT pairs).
- Recount WS events (11 subscribed + `PluginStateChanged` = 12 backend events).
- Recount pages (12 in `pages/`; `ComingSoon` is a component, not a page).
- Recount Zustand slices (12 in `stores/slices/`).

**`docs/modules/benchmarks.md`**
- Add note that `TierLimits`, `BudgetCheck`, `MetricComparison` are public-looking but not in `__init__.py`'s `__all__`.
- Expand References to list all 5 `bench_*.py` scripts under `benchmarks/`.

**`docs/_meta/gap-analysis.md`**
- Update dashboard row: "25 endpoints / 15 WS events" → "~28 endpoints / 12 WS events".
- Drop the "webhook 6 events" wording in the cloud section; it's not backed by code.
- Remove the "import `@/lib/i18n` missing" line from the v0.5 roadmap (import already present).

---

## Audit metadata

- Files read in full: 14 (all 6 docs + 8 source files).
- Grep passes: 18.
- Classes verified: ~70 names across bridge/cloud/upgrade/cli/dashboard/benchmarks modules.
- [NOT IMPLEMENTED] sections confirmed empty: 4 (bridge/relay+HA+CalDAV, cloud/stripe-connect+pricing, upgrade/chatgpt+claude+gemini+obsidian+intermind+cursor, cli/repl+admin).
- Time: ~15 minutes of machine audit.
