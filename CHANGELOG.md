# Changelog

All notable changes to Sovyx will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.8.1] — 2026-04-11

### Fixed

- **ReAct loop**: Tool re-invocation now includes `tool_calls` on assistant message and
  `tool_call_id` on tool results — fixes OpenAI 400 error that caused raw fallback
  output (`✓ calculator.calculate: 2`) instead of natural LLM responses.
- **Plugin detail panel**: Complete UX redesign — proper spacing, sections in cards,
  labeled action buttons, smooth collapse animations, visual hierarchy.
- **Plugin card**: Larger badges, readable text (10px→11px), health warnings in styled
  cards, breathing room (p-4→p-5).
- **Cognitive timeline**: Scrollbar no longer overlaps right-aligned timestamps (pr-3).
- **Metric chart**: YAxis width increased (40→52) so cost labels aren't clipped.

## [0.8.0] — 2026-04-11

### Added — Plugin Dashboard & Management UI

- **Plugins Page** (`/plugins`): Full-featured plugin management page with grid layout,
  search, filters by status/category, and real-time stats (total, active, disabled, tools).
- **PluginCard Component**: Hero card with glass morphism, status badges, tool/permission
  indicators, category labels, and pricing display.
- **Plugin Detail Panel**: Slide-over Sheet with complete plugin information — description,
  version, author, permissions breakdown, available tools, and configuration.
- **Plugin Badge System**: Reusable badge components for tools count, permission levels,
  category tags, and pricing tiers.
- **Plugin Actions**: Enable/disable/remove with confirmation dialogs, toast notifications,
  kebab overflow menu, and double-click guard for destructive actions.
- **Permission Approval Dialog**: Security-first UX — users explicitly review and approve
  each permission a plugin requests before activation.
- **Plugin REST API** (`/api/plugins`): Backend endpoints for listing plugins with enriched
  data (status, tools, permissions, marketplace metadata).
- **Zustand Plugin Slice**: Frontend state management with optimistic updates, WebSocket
  event handling for real-time sync.
- **Plugin Animations**: Smooth mount/unmount transitions, skeleton loading states,
  responsive grid that adapts to viewport.
- **Engine State Awareness**: Dashboard distinguishes "plugin engine off" from "no plugins
  installed" — each with appropriate empty state and CTA.

### Added — Testing & Quality

- **25 Contract Tests**: Every field in backend response validated against frontend
  TypeScript types — bidirectional contract coverage.
- **12 E2E Integration Tests**: Real PluginManager through FastAPI endpoints — no mocks,
  full pipeline validation.
- **20 Vitest Plugin Tests**: Full zustand slice coverage — actions, selectors, edge cases.
- **689 dashboard backend tests** total (36 plugin + 25 contract + 12 E2E + 616 existing).
- **672 dashboard frontend tests** total (22 plugin + 650 existing).

### Fixed

- **Hardcoded strings**: 15 hardcoded English strings migrated to i18n system (107 keys
  total, zero hardcoded strings remaining in plugin components).
- **Dialog UX**: Replaced `window.confirm` with proper Dialog components, added Escape key
  handling on kebab menus, inline error display, and double-click prevention.

## [0.7.1] — 2026-04-11

### Fixed — Plugin SDK Deep Validation
- **ImportGuard PEP 451** (CRITICAL): `ImportGuard` used deprecated `find_module` (PEP 302)
  which Python 3.12+ ignores completely — runtime import guard did nothing. Replaced with
  `find_spec` (PEP 451) that raises `ImportError` directly.
- **Tool name sanitization**: Changed separator from `__` to `--` in LLM-facing tool names.
  Old `__` broke roundtrip for plugin names containing underscores (e.g., `calc__v2.add`).
  `--` is safe: manifests block consecutive hyphens, Python methods can't have hyphens.
- **Disabled plugins in tools=**: `get_tool_definitions()` now filters out disabled plugins.
  Previously LLM received tools for disabled plugins, causing execution errors.
- **Empty enabled set bypass**: `set() or None` converted empty enabled (= load nothing)
  to `None` (= load all). Fixed with explicit `is not None` check.
- **ThinkPhase tools=[] vs None**: Empty tool list now converted to `None` so providers
  don't receive an empty `tools` parameter.
- **Entry points group mismatch**: pyproject.toml used `sovyx_plugins` (underscore) but
  manager searched `sovyx.plugins` (dot). Aligned to `sovyx.plugins` everywhere.

### Added
- **Marketplace manifest fields**: `category`, `tags`, `icon_url`, `screenshots`, `pricing`,
  `price_usd`, `trial_days` — backward compatible, all optional with sensible defaults.
- **PluginManager wired into bootstrap**: `load_all()` on startup, tools passed to LLM via
  `ThinkPhase`, cleanup on shutdown.
- **72 new validation tests** (VAL-001 through VAL-014): sanitization fuzzing, contract
  chain, security probing, chaos testing, dashboard consistency, full integration pipeline.
- **1038 plugin-related tests** total, all passing.

## [0.7.0] — 2026-04-11

### Added — Plugin SDK

- **Plugin SDK** (`sovyx.plugins.sdk`): `ISovyxPlugin` base class, `@tool` decorator,
  `ToolDefinition` schema — everything needed to build plugins for Sovyx Minds.
- **Plugin Manager** (`sovyx.plugins.manager`): Load, unload, execute, and lifecycle
  management with error boundary (5 consecutive failures → auto-disable).
- **Permission System** (`sovyx.plugins.permissions`): Capability-based (`network:internet`,
  `brain:read`, `fs:write`, etc.) with `PermissionEnforcer` + `PermissionDeniedError`.
- **Sandbox** (`sovyx.plugins.sandbox_http`, `sandbox_fs`): Domain-whitelisted HTTP and
  scoped filesystem access for plugins.
- **Security Scanner** (`sovyx.plugins.security`): AST scanner blocks `eval`, `exec`,
  `subprocess`, `__import__`; runtime `ImportGuard` on `sys.meta_path`.
- **Plugin Events** (`sovyx.plugins.events`): Frozen dataclass events — `PluginLoaded`,
  `PluginUnloaded`, `PluginAutoDisabled`, `PluginToolExecuted`, `PluginStateChanged`.
- **Plugin Config** (`sovyx.mind.config`): Whitelist/blacklist model in `mind.yaml` with
  `PluginsConfig`, `PluginConfigEntry`, and JSON Schema validation.
- **LLM Tool Integration**: `tools=` parameter on all 4 providers (Anthropic, OpenAI,
  Google, Ollama) with provider-specific formatting (`input_schema`, `functionDeclarations`,
  `{"type":"function",...}`).
- **LLMRouter.generate()** passes tools from `PluginManager.get_tool_definitions()` +
  `tool_definitions_to_dicts()` static helper.
- **ReAct Loop** in `ActPhase`: LLM → tool_call → PluginManager.execute() → result injected
  → LLM re-invoked (max 3 iterations). Financial gate preserved across iterations.
- **Plugin CLI** (`sovyx plugin`): `list`, `info`, `install` (local/pip/git), `enable`,
  `disable`, `remove`, `create` (scaffold), `validate` (quality gates).
- **Hot Reload** (`sovyx.plugins.hot_reload`): `watchdog`-based file watcher for dev mode —
  teardown → reimport → setup on file change.
- **Built-in Plugins**: Calculator (safe AST math), Weather (Open-Meteo, 3 tools),
  Knowledge (brain interface, 5 tools).
- **Testing Harness** (`sovyx.plugins.testing`): `MockPluginContext`, `MockBrainAccess`,
  `MockEventBus`, `MockHttpClient`, `MockFsAccess` for plugin developers.
- **Dashboard Plugin Endpoints** (`sovyx.dashboard.plugins`): `get_plugins_status()`,
  `get_plugin_detail()`, `get_tools_list()`.
- **Plugin Developer Guide** (`docs/plugin-developer-guide.md`): Complete documentation
  covering architecture, SDK, permissions, testing, CLI, distribution.
- **Integration Tests**: Full pipeline — perception → LLM → tool_call → plugin → response.
- **504 new plugin tests**, 97.61% coverage across all plugin modules.
- **`[project.entry-points.sovyx_plugins]`** for built-in plugin discovery.

### Changed
- `ActPhase` refactored: `ToolExecutor` now dispatches to `PluginManager` instead of
  internal tool registry.
- `LLMResponse.tool_calls` and `ToolCall` model fully integrated across all providers.

## [0.6.0] — 2026-04-10

### Added
- Financial Gate v2: Language-agnostic with inline buttons + LLM fallback.

## [0.5.40] — 2026-04-10

### Fixed
- `__version__` derived from `importlib.metadata` instead of hardcoded.

## [0.5.38] — 2026-04-09

### Added
- Safety Guardrails: Enterprise multilingual safety system.
- Enterprise Audit: 13-task compliance suite.

## [0.5.14] — 2026-04-08

### Added — Brain Semantic Enrichment
- Full-spectrum concept categorization — 7 categories (fact, preference, entity, skill, belief, event, relationship) with rewritten LLM extraction prompt, regex fallback for all types, and alias mapping
- LLM-based relation type classification — second LLM call classifies within-turn concept pairs into 7 relation types (related_to, part_of, causes, contradicts, example_of, temporal, emotional)
- Emotional valence and arousal — sentiment field (-1.0 to 1.0) extracted per concept via LLM, `ExtractedConcept` dataclass replaces raw tuples, episode-level valence (avg) and arousal (max |sentiment|)
- Confidence evolution — +0.1 per corroboration on concept re-encounter, capped at 1.0, tracks `corroboration_count` in metadata
- Importance reinforcement — +0.05 on dedup (repeated access), +0.02 on high co-activation (>0.7) in Hebbian learning
- Dynamic episode importance — `compute_episode_importance()` based on input length, concept count, and emotional arousal (replaces hardcoded 0.5)
- Episode summary generation — LLM-generated 1-sentence summary per episode, used in context formatting
- Episode `concepts_mentioned` wiring — concept IDs from extraction now stored in episode records
- Concept merging in consolidation — FTS5 name containment + Levenshtein distance ≤3, same mind + category; transfers relations, deletes merged concept; max 10 per cycle
- Dashboard relation legend — shows counts per relation type, hides zero-count types, link hover tooltip with type + weight
- Comprehensive integration test — 5 realistic messages through full pipeline, verifies ≥4 categories, ≥2 relation types, confidence growth, emotional valence, dynamic importance, summaries, merging

### Changed
- `ConsolidationCycle` — new `_merge_similar()` step between decay and prune; `merged` field reflects actual count
- `HebbianLearning` — accepts optional `concept_repo` for importance boost; `_strengthen_pair` boosts importance when co_activation > 0.7
- `ContextFormatter.format_episode()` — uses episode summary when available, falls back to truncated input
- `BrainService.encode_episode()` — accepts `summary` and `concepts_mentioned` params

## [0.5.11] — 2026-04-08

### Added
- Star topology Hebbian learning — `strengthen_star()` with 3-layer pairing (within-turn, cross-turn top-K, existing reinforcement). Linear O(n*K) scaling replaces O(n^2) all-pairs
- Canonical relation ordering — `_canonical_order(a, b)` ensures `min(source, target)` as source_id. Eliminates bidirectional duplicates at write time
- Migration v3 — merges pre-existing duplicate relations (sum co_occurrence, max weight, flip non-canonical)
- Working memory decay in cognitive loop — `decay_all()` called after reflect phase, rate 0.15
- Graph API orphan audit — nodes with 0 edges rescued via top-3 relations from RelationRepository
- Dynamic graph cap — `nodes * 30` for small graphs (<500), `limit * 3` for large
- Bidirectional graph query — `WHERE source_id IN (...) OR target_id IN (...)`
- Integration test suite for island prevention — 6 tests with real SQLite, BFS connectivity check
- `@pytest.mark.no_islands` regression marker
- `docs/brain-architecture.md` — full brain subsystem architecture documentation

### Fixed
- Hebbian island formation — new concepts no longer become isolated when total concepts > 20
- Working memory dedup path — `learn_concept()` now re-activates concepts in working memory (0.5) on dedup, preventing decay-induced invisibility for star topology top-K selection
- Graph API missed edges — bidirectional query catches relations where concept is in target_id column
- Graph API ORDER BY — strongest edges returned first (weight DESC), weakest dropped if cap hit

### Changed
- `HebbianLearning.strengthen()` — removed `priority_ids` param, now within-turn only
- `BrainService.encode_episode()` — uses `strengthen_star()` with new/existing concept separation
- `WorkingMemory` default decay_rate — 0.10 to 0.15
- `CognitiveLoop` — accepts optional `brain` parameter for decay integration
- Graph API chunk_size — 900 to 450 (halved for bidirectional placeholders)

## [0.5.1] — 2026-04-08

### Added
- Dashboard chat — `POST /api/chat` endpoint with `ChannelType.DASHBOARD`, full cognitive loop integration
- Chat page — `/chat` route with optimistic UI, auto-scroll, conversation continuity
- CLI `sovyx token` command — display dashboard authentication token
- Startup banner — prints dashboard URL and token on `sovyx start`
- Welcome banner — 3-step onboarding for fresh engines (choose model, set API key, start chatting)
- Channel status card — real-time indicator for configured channels via `/api/channels`
- Request ID middleware — `X-Request-Id` header on every request/response for tracing
- Dashboard build step in CI workflow
- E2E integration tests for full dashboard bootstrap + chat flow
- Attack testing suite — 74 security tests across 10 categories (XSS, token exposure, CSP, auth bypass, input sanitization, CORS, information disclosure, WebSocket, devtools)
- `publish.yml` workflow — tag-triggered PyPI release via OIDC trusted publishing
- Dashboard quickstart documentation (`docs/dashboard-quickstart.md`)
- Smoke test checklist for manual validation

### Fixed
- FastAPI version hardcoded as `"0.1.0"` — now reads from `__version__`
- Error detail leak in chat endpoint — `str(exc)` replaced with generic message
- `BridgeManager._mind_id` private access — exposed as `mind_id` property (returns `MindId`)
- 4 additional private attribute accesses (`SLF001`) resolved with public properties on `PersonalityEngine`, `CloudBackupService`, `MigrationRunner`, `DatabasePool`
- 9 unnecessary `type: ignore` suppressions eliminated (lambda keys, explicit casts, timezone union, intermediate typed variables)

### Changed
- Dashboard `package.json` version synced to `0.5.0` (was `0.0.0`)
- Token modal command updated from `cat ~/.sovyx/token` to `sovyx token`
- Dashboard static assets rebuilt (37 chunks, chat: 6.13kB / 2.26kB gzip)

### Technical
- 4,396 backend tests (pytest), 381 frontend tests (vitest)
- 98% coverage on `chat.py`, 95%+ on all modified files
- 28 remaining `type: ignore` — all audited and documented (optional deps, upstream stub limitations)
- Zero `SLF001` violations remaining
- CI green: ruff, mypy strict, bandit, pytest, dashboard build

## [0.5.0] — 2026-04-06

### Added
- Voice pipeline — wake word detection (Silero VAD), streaming STT (Moonshine), TTS (Piper, Kokoro), Home Assistant Wyoming protocol
- Dashboard — real-time web UI with brain visualization, conversations, logs, settings, system status, WebSocket live updates
- Cloud backup — zero-knowledge encrypted (Argon2id + AES-256-GCM) to Cloudflare R2, with Stripe billing and usage metering
- Signal integration — via signal-cli-rest-api
- Observability — SLO monitoring, Prometheus `/metrics` endpoint, structured logging, cost tracking
- Zero-downtime upgrades — blue-green pipeline with automatic rollback, schema migrations
- Performance benchmarks — hardware-tier budgets (Pi5, N100, GPU), baseline regression detection
- Plugin system — architecture ready (v1.0 feature)
- Security headers middleware — CSP, X-Frame-Options, X-Content-Type-Options, Referrer-Policy, Permissions-Policy
- Token-based dashboard authentication with timing-safe comparison

### Technical
- 54 tasks completed across 4 development phases
- 4,246 tests at release
- Published to PyPI (`pip install sovyx`), Docker (`ghcr.io/sovyx-ai/sovyx`), GitHub Releases

## [0.1.0] — 2026-04-03

### Added
- Cognitive Loop — perception, attention, thinking, action, reflection (OODA)
- Brain system — concept/episode/relation storage with SQLite + sqlite-vec embeddings
- Working memory — activation-based with geometric decay
- Spreading activation — multi-hop concept retrieval
- Hebbian learning — co-occurrence strengthening
- Ebbinghaus decay — forgetting curve with rehearsal reinforcement
- Hybrid retrieval — RRF fusion of FTS5 text search + vector KNN
- Memory consolidation — scheduled decay and pruning cycles
- Personality engine — OCEAN model with 3-level descriptors
- Context assembly — token-budget-aware with Lost-in-Middle ordering (Liu et al. 2023)
- LLM router — multi-provider failover (Anthropic, OpenAI, Ollama) with circuit breaker
- Cost guard — per-conversation and daily budget limits
- Telegram channel — aiogram 3.x with exponential backoff reconnect
- Person resolver — auto-create identity on first contact
- Conversation tracker — 30-minute timeout, 50-turn history
- CLI — `sovyx init/start/stop/status/doctor/brain/mind` commands (typer + rich)
- Daemon — JSON-RPC 2.0 over Unix socket (0o600 permissions)
- Lifecycle manager — PID lock, SIGTERM/SIGINT graceful shutdown, sd_notify
- Health checker — 10 concurrent checks (SQLite, brain, LLM, disk, memory)
- Service registry — lightweight DI with singleton factories
- Event bus — typed pub/sub for system events
- Docker — multi-stage build, non-root user, healthcheck
- systemd unit file with security hardening

### Technical
- 1,138 tests (1,130 passed, 8 skipped)
- 95%+ code coverage
- mypy strict, ruff, bandit — zero errors
- Python 3.11 + 3.12 CI matrix
- Property-based tests (Hypothesis) for core algorithms

[0.5.1]: https://github.com/sovyx-ai/sovyx/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/sovyx-ai/sovyx/compare/v0.1.0...v0.5.0
[0.1.0]: https://github.com/sovyx-ai/sovyx/releases/tag/v0.1.0
