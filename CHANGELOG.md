# Changelog

All notable changes to Sovyx will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.14.0] — 2026-04-16

**Stripe Connect marketplace + tier alignment (IMPL-011).**

### Changed

- **Tier nomenclature aligned with GTM strategy** (gtm-strategy.md §5):
  `STARTER` ($3.99) renamed to `SYNC`; `SYNC` ($5.99) renamed to
  `BYOK_PLUS`. Affects `SubscriptionTier`, `UsageTier`, `RateTier`,
  `TIER_FEATURES`, `TIER_MIND_LIMITS`, `TIER_MONTHLY_TOKENS`,
  `TIER_RATE_LIMITS`, and all tests.
- `create_checkout()` gains `interval` (`"month"` or `"year"`) and
  `coupon_code` parameters. Annual billing uses `TIER_ANNUAL_PRICES`
  (2 months free). Coupons add Stripe `discounts` to the session.

### Added

- **`cloud/marketplace.py`** — plugin marketplace service layer:
  - `MarketplaceConfig`: platform fee 15% (developer keeps 85%),
    geo-restriction US+BR only, per-plugin fee overrides.
  - `RevenueCalculator`: split with `math.floor` rounding (Sovyx
    absorbs fractional cents — developer never shorted).
  - `PluginAuthorService`: Stripe Express account creation,
    geo-restriction enforcement, onboarding URL generation.
  - `PluginChargeService`: destination charges with
    `application_fee_amount` + `transfer_data.destination`.
- **`cloud/marketplace_store.py`** — SQLite-backed persistence
  (not InMemory) for connected accounts, charges (with revenue
  split tracking), transfers, and payouts.
- **`persistence/schemas/marketplace.py`** — migration 001: 4 tables
  (`connected_accounts`, `marketplace_charges`,
  `marketplace_transfers`, `marketplace_payouts`) with indices.
- **`cloud/webhook_handlers.py`** — `register_all_handlers()` wires
  11 Stripe events to the `WebhookHandler` dispatch registry:
  - Billing MVP (5): `checkout.session.completed`,
    `customer.subscription.updated/deleted`,
    `invoice.payment_succeeded/failed`.
  - Connect (3): `account.updated` (updates charges_enabled /
    payouts_enabled in SQLite), `transfer.created/failed`.
  - P1 (3): `charge.refunded` (marks charge "refunded"),
    `charge.dispute.created/closed`.
- **`dashboard/routes/marketplace.py`** — 5 REST endpoints:
  `GET /api/marketplace/authors`, `GET .../authors/{id}`,
  `POST .../authors/onboard` (403 on geo-restriction),
  `GET .../authors/{id}/payouts`, `GET /api/marketplace/revenue`.
- **Annual pricing**: `TIER_ANNUAL_PRICES` (2 months free).
- **Launch coupons**: `LAUNCH_COUPON_DISCOUNTS` per tier (17-17.5%).
- **Business extra seats**: `BUSINESS_EXTRA_SEAT_CENTS = 400` ($4/mo).
- **Enterprise minimum**: `ENTERPRISE_MIN_SEAT_CENTS = 600` ($6/seat).
- **ADR-012**: Stripe Tax deferral + geo-restriction rationale with
  6 acceptance criteria for removal.

### Design decisions

- **Revenue split 85/15** (developer/Sovyx) per roadmap.md:50.
  Configurable per-plugin via `MarketplaceConfig.plugin_overrides`.
- **Stripe Express accounts** — Stripe-hosted onboarding (simplest
  Connect type).
- **SQLite persistence** for marketplace data — real money flows
  must survive daemon restarts (unlike billing/flex InMemory stores).
- **Geo-restriction US+BR only** until Stripe Tax is integrated
  (ADR-012). EU/UK blocked to avoid tax compliance risk.
- **Floor rounding**: `math.floor(total * fee)` guarantees the
  developer never receives less than their contracted share.

### Tests

- 72 marketplace-specific tests across 4 files:
  - `test_marketplace.py` (18): service layer + geo-restriction.
  - `test_marketplace_store.py` (11): SQLite CRUD + split arithmetic.
  - `test_webhook_handlers.py` (18): 11 event registrations + handlers.
  - `test_marketplace_e2e.py` (25): full flows (checkout→license,
    destination charge→webhook→SQLite→payout, refund reversal,
    dispute lifecycle, geo-restriction with zero persistence verified,
    custom 70/30 split, dashboard auth 401, 1000-value arithmetic
    sweep, 9 fee-rate matrix, tier-price boundary values).

## [0.13.1] — 2026-04-15

**6 new LLM providers via OpenAI-compatible base class.**

### Added

- **`OpenAICompatibleProvider`** base class
  (`llm/providers/_openai_compat.py`) — shared `generate()` +
  `stream()` logic for any provider that speaks the OpenAI Chat
  Completions wire format. ~200 LOC replaces what would be ~1800 LOC
  of copy-paste across providers.
- **xAI (Grok)** — `api.x.ai/v1`, `XGROK_API_KEY`, models:
  `grok-2`, `grok-3`.
- **DeepSeek** — `api.deepseek.com/v1`, `DEEPSEEK_API_KEY`, models:
  `deepseek-chat`, `deepseek-reasoner`.
- **Mistral** — `api.mistral.ai/v1`, `MISTRAL_API_KEY`, models:
  `mistral-large-latest`, `mistral-small-latest`.
- **Together AI** — `api.together.xyz/v1`, `TOGETHER_API_KEY`,
  models: `meta-llama/Llama-3.1-70B-Instruct-Turbo` and others.
- **Groq** — `api.groq.com/openai/v1`, `GROQ_API_KEY`, models:
  `llama-3.1-70b-versatile`, `mixtral-8x7b-32768`.
- **Fireworks AI** — `api.fireworks.ai/inference/v1`,
  `FIREWORKS_API_KEY`, models:
  `accounts/fireworks/models/llama-v3p1-70b-instruct`.
- All 6 providers support both `generate()` and `stream()` from day 1.
- Pricing table extended with 12 new model entries.
- Router equivalence map extended: flagship tier (grok-3,
  mistral-large ↔ claude-sonnet, gpt-4o, gemini-pro), fast tier
  (deepseek-chat, mistral-small ↔ haiku, gpt-4o-mini, gemini-flash),
  reasoning tier (+deepseek-reasoner ↔ o1, claude-opus).
- Auto-detection priority chain: Anthropic > OpenAI > Google > xAI >
  DeepSeek > Mistral > Groq > Together > Fireworks.

### Changed

- **`OpenAIProvider` refactored** to subclass
  `OpenAICompatibleProvider` — same public interface, zero duplication
  with the 6 new providers. Existing tests pass unchanged.

### Tests

- 16 unit tests: base class properties, generate with mocked httpx,
  stream with mocked SSE, all 7 subclass shapes, Together's org/
  prefix matching.

## [0.13.0] — 2026-04-15

**LLM streaming — router to voice pipeline (SPE-007 §streaming).**
First-token latency drops from 3-7 s (full LLM response) to ~300 ms
(first SSE chunk → TTS synthesis). The voice pipeline's speculative
TTS path (`stream_text` / `flush_stream` / `start_thinking`) was
scaffolded in v0.9 but never wired — this release closes the loop.

### Added

- `LLMStreamChunk` + `ToolCallDelta` models in `llm/models.py`.
- `LLMProvider.stream()` method added to the Protocol — yields
  `LLMStreamChunk` per token.
- Streaming implementations for all 4 providers:
  **Anthropic** (Messages SSE), **OpenAI** (Chat Completions SSE),
  **Google** (Gemini `streamGenerateContent?alt=sse`), **Ollama**
  (NDJSON `stream: true`).
- `LLMRouter.stream()` — provider selection + complexity routing
  identical to `generate()`; failover only before first chunk;
  cost/metrics/events deferred to the final `is_final` chunk.
- `ThinkStreamStarted` event with `ttft_ms` (time-to-first-token).
  `ThinkCompleted` gains `streamed: bool` + `ttft_ms: int`.
- `ThinkPhase.process_streaming()` — streaming counterpart of
  `process()`. Degradation path yields a single fake chunk.
- `CognitiveLoop.process_request_streaming(request, on_text_chunk)` —
  streaming cognitive loop that reconstructs `LLMResponse` from
  accumulated chunks for ActPhase + ReflectPhase. Tool-call streams
  fall back to the normal ReAct path (no voice streaming during
  tool execution — fillers continue playing).
- `VoiceCognitiveBridge` (`voice/cognitive_bridge.py`) — wires
  `pipeline.start_thinking()` → `cogloop.process_request_streaming`
  → `pipeline.stream_text` → `pipeline.flush_stream`.
- Shared SSE/NDJSON parsers in `llm/providers/_streaming.py`
  (`iter_sse_events`, `iter_ndjson_lines`).

### Design decisions

- **Output guard**: runs on the FINAL text only (option A). If the
  guard rejects, `pipeline.output.interrupt()` stops playback. Per-
  chunk regex guard deferred to V2.
- **Tool-use mid-stream**: when `finish_reason="tool_use"`, no chunks
  reach the voice pipeline — filler continues. Only the final post-
  tool response is spoken (non-streamed — V2 work).
- **Failover**: only before the first chunk. Once a provider starts
  emitting, mid-stream errors propagate to the caller.
- **Cost accounting**: waits for the `is_final` chunk because cloud
  providers emit usage only at SSE stream end.

### Tests

- 12 unit tests: SSE parser, NDJSON parser, LLMStreamChunk shape,
  Router stream provider selection + accounting, CognitiveLoop
  streaming chunk forwarding + LLMResponse reconstruction.

## [0.12.1] — 2026-04-15

**PAD 3D emotional model (ADR-001).** The single highest-priority
architectural divergence from the spec — the 1D emotional model
(concepts) / 2D (episodes) moves to unified 3D Pleasure-Arousal-
Dominance (Mehrabian 1996). Additive, backward-compatible: existing
rows backfill to neutral (0.0) on all new axes, no data migration
required beyond ALTER TABLE ADD COLUMN.

### Changed

- **Concepts** gain `emotional_arousal` (activation, [-1, +1]) and
  `emotional_dominance` (agency, [-1, +1]).
- **Episodes** gain `emotional_dominance` ([-1, +1]). `emotional_arousal`
  was already there from earlier work.
- **Importance scoring** — the existing `emotional` signal weight
  (0.10) is now apportioned across the three axes via fixed
  sub-weights: valence 0.45, arousal 0.30, dominance 0.25. Total
  emotional contribution stays at 0.10, so the formula's overall
  calibration is unchanged — a purely-valence concept at |v|=1 now
  lands at 0.045 of emotional weight (down from 0.10), but a concept
  that's emotional on all three axes saturates at the full 0.10.
  Both axes use `abs()` — fear (low-dominance, high-arousal) and
  triumph (high-dominance, high-arousal) are equally memorable.
- **Consolidation** — weighted-average merge now applies independently
  to all three axes (valence, arousal, dominance) during concept
  reinforcement. Guard: only averages an axis when the incoming signal
  is non-zero, so neutral baselines don't drag existing affect toward
  zero on every reinforcement.
- **REFLECT phase** — concept-extraction LLM prompt now asks for
  arousal + dominance alongside sentiment/valence. Clamps to
  [-1, +1], defaults to 0.0 when the LLM omits a field. Episode
  arousal prefers the explicit LLM value when any is present, falls
  back to the legacy peak-magnitude heuristic otherwise.
- **Conversation import (IMPL-SUP-015)** — summariser prompt extracts
  `emotional_dominance` alongside the existing valence/arousal; the
  summary-first encoder passes all three axes into `learn_concept`
  and `encode_episode`.
- **Exports** — SMF / .sovyx-mind archives now carry the three axes
  in concept + episode frontmatter. Legacy archives lacking the new
  fields re-import cleanly with 0.0 fallbacks.
- **Dashboard** — `/api/brain/graph` node payloads now include
  `emotional_arousal` and `emotional_dominance` alongside valence
  (3dp rounding, frontend-compatible additive change).

### Added

- Migration 006 on brain.db: ALTER TABLE ADD COLUMN for the three
  new fields with DEFAULT 0.0.
- `_emotional_intensity(v, a, d)` helper in `brain/scoring.py` — the
  single source of truth for how PAD axes combine into the scorer's
  scalar `emotional` signal.

### Non-goals for v0.12.1 (deferred)

Deliberate MVP scope — the following PAD consumers stay on the roadmap
for a later patch but are not load-bearing for v0.12.1:

- Homeostasis processing (baseline drift from recent PAD exposure).
- Personality prompt modulation (PAD → system-prompt coloring).
- TTS affective modulation (PAD → voice prosody).
- Frontend types + visualisations (dashboard currently exposes the
  fields but no UI widget renders them).

### Migration notes

- **Backward compatibility.** `_row_to_concept` / `_row_to_episode`
  defensively fall back to 0.0 when a row predates migration 006 —
  handles edge cases like partial SELECT on mid-migration DBs.
- **Existing rows stay neutral.** We do NOT LLM-backfill historical
  concepts/episodes. Neutral (0.0 on all three axes) is the honest
  "we don't know" signal, and scoring treats 0.0 as contributing
  nothing to the emotional boost — rows just look emotionally silent
  until they're re-learned or consolidated.

## [0.11.9] — 2026-04-15

CalDAV calendar integration as a plugin — IMPL-009 v0, scope-tightened
from spec to read-only.

### Added

- **CalDAV plugin** (`plugins/official/caldav.py`) — 6 read-only tools
  (`list_calendars`, `get_today`, `get_upcoming`, `get_event`,
  `find_free_slot`, `search_events`). Compatible with Nextcloud,
  iCloud, Fastmail, Radicale, SOGo, and Baikal. Talks PROPFIND /
  REPORT XML directly through the existing `SandboxedHttpClient`
  (with the new public `request()` method) — does **not** use the
  third-party `caldav` package because it routes its own HTTP and
  bypasses the sandbox. iCalendar parsing via the lightweight
  `icalendar` library; RRULE expansion via `python-dateutil`, capped
  at 200 instances. `defusedxml` parses every server-controlled XML
  body to defuse XXE risk on REPORT/PROPFIND responses. Per-window
  event cache (5 min TTL). Configuration in `mind.yaml` under
  `plugins_config.caldav` with `base_url`, `username`, `password`
  (use app-specific passwords for iCloud / Fastmail), optional
  `verify_ssl`, `default_calendar`, `allow_local`, `timezone`.
- **`SandboxedHttpClient.request(method, url, ...)`** — public
  arbitrary-method entry point for plugins that speak HTTP-extension
  protocols (CalDAV PROPFIND/REPORT, WebDAV). Every existing sandbox
  guard — URL allowlist, local-IP block, DNS rebinding check, rate
  limit, response size cap, timeout — applies unchanged.
- New deps: `icalendar>=5.0`, `defusedxml>=0.7`.

### Non-goals (deliberate)

- No write surface — events are read-only. No create / edit / delete.
- No incremental sync (no ctag/etag) — every refresh re-issues a full
  REPORT for the time window. Acceptable for v0 (~50 KB per request);
  ctag/etag is on the next-PR list.
- No subscribe / push notifications.
- One calendar source per plugin instance (multi-account is v0.2).
- **Google Calendar discontinued CalDAV in 2023** — not supported.

### Tests

- 43 unit tests covering metadata, lifecycle, every tool's success
  and error paths (auth failure / not-found / malformed XML / empty
  results), calendar discovery + cache TTL, calendar-name filtering,
  free-slot algorithm pure logic, helpers.

## [0.11.8] — 2026-04-15

Home Assistant integration as a plugin — IMPL-008 v0.

### Added

- **Home Assistant plugin** (`plugins/official/home_assistant.py`) —
  4 domains, 8 LLM-callable tools across light (`list_lights`,
  `turn_on_light`, `turn_off_light`), switch (`turn_on_switch`,
  `turn_off_switch`), sensor (`read_sensor`, `list_sensors`), and
  climate (`set_temperature`, the only confirmation-required tool in
  v0). Talks REST to the user's Home Assistant instance via
  `SandboxedHttpClient` with `allow_local=True` (HA usually lives at
  `http://homeassistant.local:8123` or a private IP). Per-domain
  in-memory entity cache (60 s TTL) with eviction on service-call.
  Declares `Permission.NETWORK_LOCAL`.
- **Architectural decision**: HA was originally specced as a bridge
  (IMPL-008). Shipped as a **plugin** instead — HA exposes a device
  API, not a conversational channel; the plugin substrate gives it
  sandbox, permissions, lifecycle, dashboard UI, and HACS-compatible
  packaging for free.

### Non-goals (deliberate)

- No WebSocket subscription — entity state is fetched on demand. The
  mind doesn't see a light flipped manually until the next tool call.
- No mDNS discovery — caller supplies `base_url` explicitly.
- Only 4 domains in v0 — covers / locks / fans / media_player /
  scenes / scripts ship one PR per domain.

### Tests

- 50 unit tests covering metadata, lifecycle, the not-configured
  guard, every tool's happy path, every tool's error paths
  (401 / 404 / 500 / network exception / invalid entity_id / wrong
  domain), cache TTL behaviour (hit / invalidation / staleness /
  fallback), and module-level helpers.

## [0.11.7] — 2026-04-15

Interactive CLI REPL — `sovyx chat` (SPE-015 §3.1). Closes a long-
standing gap noted in the CLI module spec.

### Added

- **`sovyx chat`** — line-oriented REPL over the existing JSON-RPC
  Unix socket (not HTTP). Runs even when the dashboard is disabled.
  prompt_toolkit session with persistent history at
  `~/.sovyx/history` (chmod 0600), word-completer over the slash
  command vocabulary, history search.
- **7 slash commands**: `/help` (also `/?`), `/exit` / `/quit`
  (Ctrl+D works too), `/new` (rotate `conversation_id`), `/clear`
  (wipe screen + rotate), `/status`, `/minds`, `/config`. Every
  unknown command returns a friendly help-pointer instead of
  raising. Every boundary handler wraps the call in a `try` that
  renders the error inline — one bad turn never crashes the session.
- **3 new RPC handlers** wired in `engine/_rpc_handlers.py`:
  `chat`, `mind.list`, `config.get`. The `chat` handler reuses
  `dashboard.chat.handle_chat_message` (the same entry point
  `POST /api/chat` uses) with `ChannelType.CLI` and a stable
  `cli-user` channel id, so `PersonResolver` keeps CLI sessions on
  a separate identity from the dashboard.
- New dep: `prompt_toolkit>=3.0`.

### Tests

- 47 tests across slash-command parsing + dispatch (24) and REPL
  loop integration with mocked client + fake session (23). Covers
  every command, every error path, EOF handling, history-file
  permissions on POSIX, and the full driven-session entry point.

## [0.11.6] — 2026-04-15

DREAM phase — the seventh and final phase of the cognitive loop
(SPE-003 §1.1, "nightly: discover patterns"). Closes Top-10 gap #9.

### Added

- **DREAM phase** (`brain/dream.py`) — `DreamCycle` + `DreamScheduler`
  in the same module, mirroring `brain/consolidation.py`. Unlike the
  request-driven phases (Perceive → Reflect), DREAM runs on a
  time-of-day schedule (default `02:00` in the mind's timezone) while
  the user is likely asleep — biologically inspired by REM-era
  hippocampal replay (Buzsáki 2006).
- **3-phase pipeline per run**: (1) fetch episodes in
  `dream_lookback_hours` window (default 24 h) via the new
  `EpisodeRepository.get_since`, (2) short-circuit if fewer than 3
  episodes, (3) one LLM call extracts up to `dream_max_patterns`
  recurring themes (default 5) → each pattern becomes a `Concept`
  with `source="dream:pattern"`, `category=BELIEF`, and a modest
  `confidence=0.4` (lifts via access). Concepts that appear in two
  or more distinct episodes get fed to `HebbianLearning.strengthen`
  with attenuated activation (0.5) — cross-episode is a weaker
  signal than within-turn. Capped at 12 concepts per run to bound
  the O(n²) within-pair cost.
- **Time-of-day scheduler** — `DreamScheduler._loop` sleeps until
  the next `dream_time` occurrence in the mind's timezone, with
  ±15 min jitter. Survives cycle exceptions (logged, not bubbled).
  Time arithmetic in `_seconds_until_next_dream(now=...)` accepts an
  injectable clock so tests can drive it deterministically.
- **`DreamCompleted` event** — `patterns_found, concepts_derived,
  relations_strengthened, episodes_analyzed, duration_s`. Emitted on
  every run (including short-circuits). Subscribed by the dashboard
  WebSocket bridge with a Moon icon in the activity feed.
- **Kill-switch via config**: `dream_max_patterns: 0` in `mind.yaml`
  causes bootstrap to skip `DreamScheduler` registration entirely.
  No flag sprawl, zero runtime overhead when disabled.
- **`EpisodeRepository.get_since(mind_id, since, limit=500)`** — new
  method returning episodes created at or after `since` in
  chronological order.
- **`BrainConfig.dream_lookback_hours`** (default 24, range 1–168)
  and `BrainConfig.dream_max_patterns` (default 5, range 0–50).

### Tests

- 27 cycle tests across short-circuits, pattern extraction (LLM
  failure, malformed JSON, code-fenced wrappers, empty fields),
  cross-episode Hebbian (co-occurring boost, single-episode skip,
  Hebbian failure, activation damping), event payload, digest
  rendering (long summary truncation, missing summary fallback),
  and lookback window respect.
- 13 scheduler tests on time arithmetic (target later today, target
  passed, exactly-now rolls to tomorrow, midnight edge, naive `now`
  treated as scheduler tz, delta never exceeds one day), fallbacks
  (invalid HH:MM, unknown timezone), lifecycle idempotency.
- 4 `EpisodeRepository.get_since` tests.

### Fixed

- `lifecycle.py`: gate `MindManager.resolve` behind scheduler
  registration. The DREAM wiring originally hoisted the resolve out
  of the per-scheduler `if`-block to share `mind_id`, which broke
  seven lifecycle tests on Linux CI that wire only the cognitive
  loop without `MindManager`. Resolve now happens only when at least
  one scheduler is registered.

## [0.11.5] — 2026-04-15

Claude and Gemini conversation importers — second and third of four
planned platforms (ChatGPT shipped in v0.11.4; Obsidian remains).

### Added

- **ClaudeImporter** (`upgrade/conv_import/claude.py`) — parses the
  `conversations.json` that Anthropic emails users on data export.
  Substantially simpler shape than ChatGPT's regeneration-capable
  tree: a flat array of conversation objects, each with a flat
  `chat_messages` list in chronological order. Maps `sender:"human"`
  → `role:"user"`, prefers the newer typed `content[]` array over
  the legacy flat `text` field, parses ISO-8601 timestamps via
  `datetime.fromisoformat` (Z-suffix tolerated). Attachments and
  files explicitly ignored in v1 (consistent non-goal across all
  importers).
- **GeminiImporter** (`upgrade/conv_import/gemini.py`) — handles
  Google Takeout's activity-stream format (no native conversation
  boundaries, no role field — just a flat stream of localized
  "You said:" / "Gemini said:" title strings). Three-pass pipeline:
  (1) classify + filter — keep entries from `Gemini Apps` /
  `Bard` headers, drop meta-activity ("You used Gemini"); (2) sort
  chronologically (Takeout emits newest-first); (3) group by time
  gap — consecutive turns within 30 minutes form one conversation.
  Locale prefix catalogs for EN, PT, ES, FR, DE, IT, plus legacy
  `Bard` headers. HTML entities decoded (`&aacute;`, `&#39;`); `<b>`
  / `<i>` tags stripped. Synthesized `conversation_id` =
  `sha256(f"gemini:{first_turn_iso}").hexdigest()[:16]` — re-importing
  the same archive produces identical IDs, so the
  `conversation_imports` dedup table skips previously-seen sessions.
  The 30-minute session-gap is a load-bearing constant: changing it
  retroactively shifts group boundaries and therefore IDs (documented
  as a dedup-stability contract in the constant's docstring).
- Both importers wired into
  `dashboard/routes/conversation_import.py::_IMPORTERS` and the
  frontend `ConversationImportPlatform` type extended to accept
  `"claude"` and `"gemini"`.

### Tests

- ~70 parser tests across the two new platforms (role detection,
  session grouping boundaries, content[]+text fallback, meta-activity
  filtering, HTML handling, ID stability, malformed input,
  unsupported-locale drop, title synthesis).
- HTTP smoke tests assert the dashboard router accepts
  `platform=claude` and `platform=gemini` and starts a job for each.

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

[Unreleased]: https://github.com/sovyx-ai/sovyx/compare/v0.11.9...HEAD
[0.11.9]: https://github.com/sovyx-ai/sovyx/compare/v0.11.8...v0.11.9
[0.11.8]: https://github.com/sovyx-ai/sovyx/compare/v0.11.7...v0.11.8
[0.11.7]: https://github.com/sovyx-ai/sovyx/compare/v0.11.6...v0.11.7
[0.11.6]: https://github.com/sovyx-ai/sovyx/compare/v0.11.5...v0.11.6
[0.11.5]: https://github.com/sovyx-ai/sovyx/compare/v0.11.4...v0.11.5
[0.11.4]: https://github.com/sovyx-ai/sovyx/compare/v0.11.3...v0.11.4
[0.11.3]: https://github.com/sovyx-ai/sovyx/compare/v0.11.2...v0.11.3
[0.11.2]: https://github.com/sovyx-ai/sovyx/compare/v0.11.1...v0.11.2
[0.11.1]: https://github.com/sovyx-ai/sovyx/compare/v0.11.0...v0.11.1
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
