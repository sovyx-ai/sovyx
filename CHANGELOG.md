# Changelog

All notable changes to Sovyx will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.11.4] — 2026-04-15

New-user onboarding: import existing conversation history from other assistants so the mind already knows you on day one. Ships ChatGPT this release; Claude / Gemini follow the same shape in later releases.

### Added

- **ChatGPT conversation importer** (IMPL-SUP-015 first tranche). Parses a ChatGPT data-export `conversations.json`, walks the `mapping` tree from `current_node` up through parents to extract the mainline (forks from regeneration stay abandoned), and encodes each conversation as one `Episode` plus up to five extracted `Concept` rows. Architecture is **summary-first** (Option C in IMPL-SUP-015): one fast-model LLM call per conversation produces `{summary, concepts, emotional_valence/arousal, importance}`. Target cost ~$0.001-0.003 per conversation — $3 and ~20 minutes for a 1000-conversation import. A synchronous fallback path preserves the Episode even when the LLM router is missing or returns malformed JSON.
- **New subpackage `sovyx.upgrade.conv_import`** housing the import machinery: platform-neutral `RawConversation`/`RawMessage` dataclasses, a `ConversationImporter` Protocol, the `ChatGPTImporter`, `summarize_and_encode` encoder, `ImportProgressTracker` (async-lock-guarded, snapshot-returning), `source_hash` dedup helper. Follow-up platform parsers (Claude, Gemini) drop a sibling file and register in the endpoint's platform map; the HTTP surface and tracker stay unchanged.
- **New endpoints**: `POST /api/import/conversations` (multipart: `platform` + `file`) → `202 Accepted {job_id, conversations_total}` with a background `asyncio.Task` driving the encode loop; and `GET /api/import/{job_id}/progress` → live snapshot `{state, conversations_total/processed/skipped, episodes_created, concepts_learned, warnings, error, elapsed_ms}`. Same 100 MiB upload cap + Bearer-token auth as every other dashboard route.
- **Dedup at conversation level** via a new `conversation_imports` table keyed by `sha256(platform||conversation_id)`. Re-importing the same export is a no-op per conversation; verified by an end-to-end HTTP test. Backed by a new migration 005 on `brain.db`.
- Frontend types: `ConversationImportPlatform`, `ConversationImportState`, `StartConversationImportResponse`, `ConversationImportProgress` in `dashboard/src/types/api.ts` with mirrored zod schemas in `schemas.ts` — ready for a UI follow-up PR.
- Test fixture `tests/fixtures/chatgpt/sample_conversations.json` (3 synthetic conversations: linear, branched, multimodal) plus 54 new tests across parser / hash / tracker / summary-encoder / HTTP endpoints.

### Fixed

- `test_brain_schema.py` migration-count assertions and three test function names bumped for migration 005.

### Non-goals (explicit — roadmap candidates for later releases)

- Claude, Gemini, Obsidian importers — same Protocol + HTTP surface, follow-up PRs.
- Deep-import mode (per-turn REFLECT) — expensive; deferred.
- Attachments / multimodal asset extraction — v1 stringifies with a marker only.
- PII scrubbing on import — user's own data, explicit decision.
- WebSocket progress events — polling only for v1.
- Resuming interrupted imports — daemon restart means re-submit.
- Frontend UI for import — this release ships backend + types only; dashboard wiring lands in a follow-up.

## [0.11.3] — 2026-04-15

Quality pass: exhaustive bare-`except` audit + cleanup across the backend, plus a latent React render bug in the brain-graph accessibility fallback.

### Changed

- **BLE001 sweep across `src/sovyx/`** (4 commits). Ruff's `flake8-blind-except` rule is now enabled (`BLE` added to `[tool.ruff.lint] select`), so any new `except Exception:` fails CI. Net effect: **77 un-justified broad catches → 0**. Categorised cleanup:
  - **Batch 1** (`4d1833f`) — 49 legitimate boundaries explicitly annotated with `# noqa: BLE001 — <reason>`. Covers health-check runners (`engine/health.py` + `observability/health.py`), CLI command handlers (`cli/main.py`), boundary translation into domain exceptions (`engine/bootstrap.py`, `engine/rpc_server.py`, `cognitive/reflect/phase.py`, `bridge/manager.py`, `upgrade/blue_green.py`, `upgrade/schema.py`, `voice/pipeline/_orchestrator.py`), and background loops that must not die on single failures (`cognitive/loop.py`, `bridge/channels/{telegram,signal}.py`, `voice/wyoming.py`, `llm/router.py`).
  - **Batch 2** (`069d3eb`) — 9 silent-swallow sites narrowed to typed exception tuples with `exc_info=True` added where missing: `plugins/sdk.py` `get_type_hints`, `engine/bootstrap.py` YAML read/write, `brain/_model_downloader.py` retry loop, `llm/providers/ollama.py` ping/list-models, `voice/jarvis.py` filler synthesis, `cognitive/reflect/phase.py` novelty compute, `brain/contradiction.py` LLM detection, `cognitive/financial_gate.py` intent classification.
  - **Batch 3** (`4e696fe`) — brain + persistence + cost DB narrows: `brain/consolidation.py` centroid refresh + per-pair merge, `brain/embedding.py` ONNX model load, `brain/retrieval.py` vector/episode search, `brain/service.py` `_safe_record_access`, `persistence/pool.py` WAL checkpoint + extension load, `llm/cost.py` restore/persist/daily-flush.
  - **Batch 5** (`853c8d3`) — voice + bridge API narrows: `voice/pipeline/_orchestrator.py` STT transcribe + TTS synthesize (all 4 call sites), `voice/tts_kokoro.py` `list_voices`, `bridge/channels/telegram.py` `edit_message_text` (narrowed to `AiogramError`).
- Pre-existing `# noqa: BLE001` catches triaged in the earlier Sprint 2 sweep were spot-checked and left as-is — all 12 sampled were legitimate resilience boundaries with fallback + logging.
- `tests/**/*.py` added to BLE001 per-file-ignores: security fuzz (`tests/security/test_frontend_attack.py`) and stress loops (`tests/stress/ws_stress_test.py`) legitimately need broad catches to probe attack surfaces / keep harnesses alive.

### Fixed

- **React error #31 on `/brain`** (`c74aab9`). `react-force-graph-2d` (via d3-force) mutates link objects in place once the simulation starts — `link.source` and `link.target` are replaced with references to the node objects themselves. The screen-reader fallback table was rendering the raw mutated object as a `<td>` child, triggering "Objects are not valid as a React child". A silent correctness bug also lived in the same paths: `connectionCounts` was keying its Map by the mutated objects, so every concept silently showed "0" connections in the SR table. Introduced `linkEndpointId()` coercion helper applied at every leak site (memo, render keys, table cells); regression test constructs a link with fully mutated endpoints and asserts both symptoms.
- Tests that seeded typed exceptions (`LLMError`, `SearchError`) in `AsyncMock.side_effect` were updated to use the builtin/stdlib equivalents already present in the narrow tuples (`ValueError`, `sqlite3.OperationalError`). Internal-class seeding is covered by CLAUDE.md anti-pattern #8 — under pytest-cov's trace-based source rewriting, the test-side and production-side class objects can diverge, causing `except (..., SearchError, ...)` to miss. Seeding builtins avoids the class-identity drift while keeping the production narrow unchanged.

### Diagnostic improvements

- 15 `logger.*` call sites gained `exc_info=True`. Previously-silent degradation paths — TTS/STT failures, Ollama ping, YAML persist, cost-guard errors, model-download retry, filler synthesis, Kokoro voice listing — now emit full tracebacks at their existing log level, so real bugs can be told apart from expected fallback.
- `react_iteration` log line now carries `tools=[...]` and `plugins=[...]` fields alongside the per-iteration counts, completing the observability parity promised by the v0.11.2 module-tags feature.

## [0.11.2] — 2026-04-15

### Added

- **Module/plugin tags on every chat response.** Every assistant message now carries at least one visible tag (pill) indicating which modules produced the reply. Pure cognitive replies show `brain`; tool-backed replies show the plugin name(s) followed by `brain`. Tags are derived from the ReAct loop's `tool_calls_made` list (no new data plumbing — plugin names come from the existing namespaced `plugin.tool` format) and rendered above the assistant bubble via a new `MessageTags` React component with i18n labels and raw-name fallback for unknown plugins.
- `react_iteration` log call now includes `tools` and `plugins` fields for observability parity with the new wire-format contract.
- `ChatResponse.tags?: string[]` and matching zod schema on the frontend; `ChatMessage` extended with the same field for thread-level rendering.

## [0.11.1] — 2026-04-15

Sprint 6 — 90 % → 95 % enterprise polish. Thirteen focused items across accessibility, resilience, observability, and schema hygiene. All CI gates green.

### Fixed

- 10 pre-existing TypeScript errors (schema drift `SafetyConfig`).
- Pricing tables unified into single source (`llm/pricing.py`).
- `BatchSpanProcessor` replaces `SimpleSpanProcessor` (IMPL-015).
- Last raw `httpx` in plugins migrated to `SandboxedHttpClient`.
- OTel `setup_tracing` resilient to prior shutdown.

### Added

- Emotional baseline config (`EmotionalBaselineConfig` in `EngineConfig`).
- Per-section `ErrorBoundary`s with telemetry reporting.
- `brain-graph` screen-reader fallback table.
- `log-row` keyboard accessibility (role, tabIndex, onKeyDown).
- i18n aria-label sweep (9 hardcoded → `useTranslation`).
- `safeStringify` with secret redaction.
- Vector search documented as implemented.

### Security

- Sidebar cookie hardened (`SameSite=Strict`, `Secure`).

## [0.11.0] — 2026-04-14

The v0.11 line is an enterprise hardening pass across backend, frontend, and CI infrastructure. Five focused sprints: security P0, god-file splits, concurrency + config hardening, frontend hardening, and 90% polish.

### Security

- Wyoming voice server: bearer-token auth, rate limit, payload cap, read timeout.
- Plugins: every official plugin now routes HTTP through `SandboxedHttpClient`; raw `httpx` from plugin code is no longer permitted.
- AST scanner: blocks `builtins`, `tempfile`, `gc`, `inspect`, `mmap`, `pty`, plus the `().__class__.__base__.__subclasses__()` escape chain.
- CLI: `sovyx init --name` validated via regex; path traversal closed.
- Dashboard: import endpoint size cap (100 MB) + streaming parse; chat max-length 10 000 chars.
- LLM providers: Google API key moved from URL parameter to `x-goog-api-key` header.
- Frontend: token migrated from `localStorage` to `sessionStorage` + in-memory fallback; `window.prompt()` replaced with Radix Dialog; WS URL derived from `location.protocol`; `use-auth` now fail-closed on network errors.
- Frontend: `safeStringify` (size clamp + secret redaction) applied to `plugin-detail` manifest, tool parameters, and `log-row` extra fields.

### Added

- `engine/_lock_dict.LRULockDict` — bounded `asyncio.Lock` dict with LRU eviction; shared by `bridge/manager.py`, `cloud/flex.py`, `cloud/usage.py`.
- `EngineConfig.tuning.{safety,brain,voice}` — tuning knobs previously hardcoded now overridable via `SOVYX_TUNING__*` env variables.
- Frontend runtime response validation: `src/types/schemas.ts` holds zod schemas for 11 response shapes; `api.get/post/put/patch/delete` accept an optional `{ schema }` option that runs `safeParse` and logs mismatches.
- Frontend: `api.patch()`, `buildQuery()` helper, default 30 s timeout via composable `AbortController`, retry with exponential backoff + jitter on 408/429/502/503/504 for idempotent verbs.
- Frontend error telemetry: `POST /api/telemetry/frontend-error` endpoint (rate-limited 20 / 60 s, pydantic length caps) + `ErrorBoundary.componentDidCatch` hook.
- Virtualization on `chat-thread.tsx` and `cognitive-timeline.tsx` via TanStack Virtual.
- 56 new component tests across 13 `components/dashboard/` files + 3 `components/ui/` primitives.
- 5 new critical tests: `plugins.tsx` page-level, command palette Cmd+K, `router.tsx` lazy + ErrorBoundary, settings slider/preset/save interactions.
- `src/lib/safe-json.ts` with 9 tests — size clamp and secret-key redaction for DOM-rendered JSON.
- `persistence/pool._read_index_lock` — round-robin cursor now atomic under contention.
- `observability/alerts._state_lock` — evaluate() serialized; concurrent callers no longer double-fire `AlertFired`.

### Changed

- **God files split into subpackages** (public surface preserved via `__init__.py` re-exports):
  - `dashboard/server.py` (2 134 LOC) → `dashboard/routes/` (16 APIRouter modules).
  - `cognitive/safety_patterns.py` (1 165 LOC) → `cognitive/safety/patterns_{en,pt,es,child_safe}.py`.
  - `cognitive/safety_classifier.py` (704 LOC) → `cognitive/safety/_classifier_*`.
  - `cognitive/reflect.py` (1 021 LOC) → `cognitive/reflect/` (phase.py + 5 helpers).
  - `voice/pipeline.py` (840 LOC) → `voice/pipeline/` (orchestrator + state + output queue + barge-in + config).
  - `plugins/manager.py` (819 LOC) — `_event_emitter.py`, `_manager_types.py`, `_dependency.py` extracted.
  - `brain/service.py` (712 LOC) — `_novelty.py` + `_centroid.py` extracted.
  - `brain/embedding.py` (705 LOC) — `_model_downloader.py` extracted.
- ONNX inference (Piper, Kokoro, Silero, Moonshine, openWakeWord) now runs via `asyncio.to_thread()`; the event loop no longer stalls during synthesis or wake-word checks.
- `cloud/backup` boto3 calls (upload / list / batch-delete) in the scheduler wrapped in `asyncio.to_thread()` so backup cycles don't block the loop.
- BLE001 sweep: `except Exception:` turned into typed handlers with explicit `log + re-raise` where appropriate; blanket exception catches removed from cognitive/, plugins/, cloud/, cli/.
- Frontend hot paths memoized: `LogRow`, `ChatBubble`, `PluginCard`, `TimelineRow`, `ToolItem`, `LetterAvatar`, `PluginStatusDot`.
- `nameToHue` consolidated in `dashboard/src/lib/format.ts`; duplicate copies in `plugin-card` and `plugin-detail` removed.
- `apiFetch` helper centralizes Bearer-header injection; `token-entry-modal` and `settings/export-import` no longer call raw `fetch()`.

### Fixed

- `bridge/manager`: `defaultdict(asyncio.Lock)` replaced with `LRULockDict(maxsize=500)` — long-running daemons no longer leak locks.
- Hardcoded timeouts / thresholds across cognitive/, brain/, voice/ now route through `EngineConfig.tuning`.
- Dashboard `CommandDialog` (shadcn/ui) wasn't wrapping children in `<Command>` — caused cmdk internals to crash on render in tests; fixed.
- Dashboard tests for `chat-thread` / `cognitive-timeline` adapted to virtualized rendering (setup.ts now stubs `offsetWidth/Height` and fires ResizeObserver synchronously).

### Tests

- Backend: ~7 820 tests on Python 3.11 and 3.12 matrix.
- Dashboard: 767 vitest tests (was 676 pre-v0.11).
- Every quality gate green on `sovyx-4core` runners: `uv lock --check`, ruff, ruff format, mypy strict, bandit, pytest, vitest, `tsc -b`.

## [0.10.1] — 2026-04-13

### Fixed

- Plugin manager: handle `PluginStateChanged` serialization edge case when an auto-disabled plugin emits during teardown.
- Cognitive: `safety_classifier` cache eviction under high fan-in.

## [0.10.0] — 2026-04-13

### Added

- **Web Intelligence plugin** (6 tools — `search`, `fetch`, `research`, `lookup`, `learn_from_web`, `recall_web`). Three backends: DuckDuckGo (no key), SearXNG (self-hosted), Brave (API key). Intent-adaptive cache, source credibility tiers, SSRF protection, per-tool rate limits. 224 tests (200 unit + 24 Hypothesis).
- **Financial Math plugin** — 9 Decimal-native tools (`calculate`, `percentage`, `interest`, `tvm`, `amortization`, `portfolio`, `position_size`, `currency`). Banker's rounding, 28-digit precision, zero external deps. 228 tests.

### Changed

- `CalculatorPlugin` is now a backward-compatibility wrapper over `FinancialMathPlugin.calculate`.

## [0.9.0] — 2026-04-12

### Added — Knowledge plugin v2.0

- **Semantic deduplication** — cosine similarity ≥ 0.88 detects near-duplicates.
- **LLM-assisted conflict resolution** — classifies as SAME / EXTENDS / CONTRADICTS / UNRELATED.
- **Confidence reinforcement** — "established" status after 5+ confirmations.
- **Auto-relation creation** — new concepts linked to related existing concepts (similarity 0.65–0.87).
- **Episode-aware recall** — `recall_about()` enriches results with conversation history.
- **Person-scoped memory** — `remember(about_person="X")` and `search(about_person="X")`.
- **Real forget with cascade** — deletes concept + relations + embeddings + working memory; emits `ConceptForgotten`.
- **Structured JSON output** — all 5 tools return `{action, ok, message, ...}`.
- **Rate limiting** — sliding window: 30 writes/min, 60 reads/min.
- `BrainAccess` API: `classify_content`, `reinforce`, `create_relation`, `boost_importance`, `get_stats`, `get_top_concepts`, `forget_all`.

### Tests

- 659 plugin tests (unit + integration + contract + E2E).

## [0.8.2] — 2026-04-11

### Fixed

- ReAct loop: sanitize tool function names in re-invocation messages — OpenAI requires `^[a-zA-Z0-9_-]+$` but Sovyx uses dots (`calculator.calculate`). Now properly converts to `calculator--calculate` before sending back.

## [0.8.1] — 2026-04-11

### Fixed

- ReAct loop: tool re-invocation now includes `tool_calls` on assistant message and `tool_call_id` on tool results — fixes OpenAI 400 that caused raw fallback output.
- Plugin detail panel redesign: proper spacing, sections in cards, labeled action buttons, collapse animations.
- Plugin card polish: larger badges, readable text (10 → 11 px), health warnings in styled cards.
- Cognitive timeline: scrollbar no longer overlaps right-aligned timestamps.
- Metric chart: `YAxis` width increased (40 → 52) so cost labels aren't clipped.

## [0.8.0] — 2026-04-11

### Added — Plugin dashboard

- `/plugins` page with grid layout, search, filters by status / category, real-time stats.
- `PluginCard` hero card (glass morphism, status badges, tool / permission indicators).
- Plugin Detail slide-over panel — description, version, author, permissions, tools, config.
- Reusable badge system — tools count, permission levels, category tags, pricing.
- Enable / disable / remove flow with confirmation dialogs + double-click guard.
- Permission Approval Dialog: users explicitly review and approve each permission before activation.
- `/api/plugins` REST endpoints with enriched data.
- Zustand plugin slice with optimistic updates + WebSocket sync.
- Engine-state awareness: distinguishes "plugin engine off" from "no plugins installed".

### Testing

- 25 contract tests (backend ↔ frontend type parity).
- 12 E2E tests through real `PluginManager` + FastAPI.
- 20 vitest plugin-slice tests.

## [0.7.1] — 2026-04-11

### Fixed — Plugin SDK deep validation

- `ImportGuard` PEP 451 (CRITICAL): replaced deprecated `find_module` with `find_spec` — runtime import guard now actually runs on Python 3.12+.
- Tool name separator `__` → `--` (manifests block consecutive hyphens; Python methods can't have hyphens).
- Disabled plugins now filtered from `get_tool_definitions()`.
- Empty `enabled` set no longer falls through to "load everything" via `or None`.
- `ThinkPhase` tools=[] normalized to `None` so providers don't receive empty tools arrays.
- Entry-points group alignment: `sovyx.plugins` everywhere (was split `sovyx_plugins` / `sovyx.plugins`).

### Added

- Marketplace manifest fields (`category`, `tags`, `icon_url`, `screenshots`, `pricing`, `price_usd`, `trial_days`).
- `PluginManager` wired into bootstrap — `load_all()` on startup, cleanup on shutdown.
- 72 new validation tests (VAL-001 … VAL-014).

## [0.7.0] — 2026-04-11

### Added — Plugin SDK

- `sovyx.plugins.sdk`: `ISovyxPlugin` ABC, `@tool` decorator, `ToolDefinition` schema.
- `sovyx.plugins.manager`: load, unload, execute, lifecycle with auto-disable on 5 consecutive failures.
- `sovyx.plugins.permissions`: capability-based (`network:internet`, `brain:read`, `fs:write`, …).
- `sovyx.plugins.sandbox_http` / `sandbox_fs`: domain-whitelisted HTTP + scoped filesystem.
- `sovyx.plugins.security`: AST scanner blocks `eval`, `exec`, `subprocess`, `__import__`; runtime `ImportGuard`.
- `sovyx.plugins.events`: `PluginLoaded`, `PluginUnloaded`, `PluginAutoDisabled`, `PluginToolExecuted`, `PluginStateChanged`.
- Plugin config whitelist / blacklist model in `mind.yaml`.
- LLM tool integration across all 4 providers (Anthropic, OpenAI, Google, Ollama).
- ReAct loop in `ActPhase`: LLM → tool_call → `PluginManager.execute()` → result → LLM re-invoke (max 3 iterations).
- `sovyx plugin` CLI: `list`, `info`, `install` (local / pip / git), `enable`, `disable`, `remove`, `create`, `validate`.
- Hot reload via `watchdog` for dev mode.
- Built-in plugins: Calculator, Weather (Open-Meteo), Knowledge.
- Testing harness: `MockPluginContext`, `MockBrainAccess`, `MockEventBus`, `MockHttpClient`, `MockFsAccess`.
- Plugin Developer Guide (docs).

### Tests

- 504 new plugin tests, 97.61 % coverage across plugin modules.

## [0.6.0] — 2026-04-10

### Added

- Financial Gate v2: language-agnostic with inline buttons + LLM fallback.

## [0.5.x] — 2026-04-06 … 2026-04-10

### Added

- Safety guardrails: enterprise multilingual safety system.
- Enterprise audit tooling (13-task compliance suite).
- Dashboard chat (`POST /api/chat` + `ChannelType.DASHBOARD`).
- `sovyx token` CLI command + startup banner.
- Welcome banner, channel status card, request-ID middleware.
- Dashboard build step + attack testing suite (74 security tests).
- `publish.yml` workflow with OIDC trusted publishing.
- Voice pipeline: wake word, Silero VAD, Moonshine STT, Piper + Kokoro TTS, Wyoming protocol.
- Dashboard: brain viz, conversations, logs, settings, system status, WebSocket live updates.
- Cloud backup: zero-knowledge encryption (Argon2id + AES-256-GCM) to Cloudflare R2, Stripe billing.
- Signal channel via signal-cli-rest-api.
- Observability: SLO monitoring, Prometheus `/metrics`, structured logging, cost tracking.
- Zero-downtime upgrades: blue-green with automatic rollback, schema migrations.
- Performance benchmarks: hardware-tier budgets (Pi 5, N100, GPU).
- Security headers middleware, timing-safe token auth.

### Changed

- `__version__` derived from `importlib.metadata`.

## [0.1.0] — 2026-04-03

### Added

- Cognitive Loop (Perceive → Attend → Think → Act → Reflect).
- Brain system: concept / episode / relation storage in SQLite + `sqlite-vec`.
- Working memory with activation-based geometric decay.
- Spreading activation (multi-hop retrieval).
- Hebbian learning (co-occurrence strengthening).
- Ebbinghaus decay with rehearsal reinforcement.
- Hybrid retrieval: RRF fusion of FTS5 + vector KNN.
- Memory consolidation (scheduled decay + pruning).
- Personality engine (OCEAN model).
- Context assembly with Lost-in-Middle ordering (Liu et al. 2023).
- LLM router: multi-provider failover + circuit breaker (Anthropic, OpenAI, Ollama).
- Cost guard: per-conversation and daily USD budgets.
- Telegram channel (`aiogram` 3.x with exponential-backoff reconnect).
- Person resolver, conversation tracker (30-min timeout, 50-turn history).
- CLI (`init` / `start` / `stop` / `status` / `doctor` / `brain` / `mind`) with Typer + Rich.
- Daemon: JSON-RPC 2.0 over Unix socket.
- Lifecycle manager: PID lock, SIGTERM / SIGINT graceful shutdown, `sd_notify`.
- Health checker: 10 concurrent checks.
- Service registry, event bus, Docker multi-stage build, systemd unit file.

### Tests

- 1 138 tests, ≥ 95 % coverage, mypy strict, ruff, bandit — zero errors.
- Python 3.11 + 3.12 CI matrix.

[Unreleased]: https://github.com/sovyx-ai/sovyx/compare/v0.11.0...HEAD
[0.11.0]: https://github.com/sovyx-ai/sovyx/compare/v0.10.1...v0.11.0
[0.10.1]: https://github.com/sovyx-ai/sovyx/compare/v0.10.0...v0.10.1
[0.10.0]: https://github.com/sovyx-ai/sovyx/compare/v0.9.0...v0.10.0
[0.9.0]: https://github.com/sovyx-ai/sovyx/compare/v0.8.2...v0.9.0
[0.8.2]: https://github.com/sovyx-ai/sovyx/compare/v0.8.1...v0.8.2
[0.8.1]: https://github.com/sovyx-ai/sovyx/compare/v0.8.0...v0.8.1
[0.8.0]: https://github.com/sovyx-ai/sovyx/compare/v0.7.1...v0.8.0
[0.7.1]: https://github.com/sovyx-ai/sovyx/compare/v0.7.0...v0.7.1
[0.7.0]: https://github.com/sovyx-ai/sovyx/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/sovyx-ai/sovyx/compare/v0.5.40...v0.6.0
[0.1.0]: https://github.com/sovyx-ai/sovyx/releases/tag/v0.1.0
