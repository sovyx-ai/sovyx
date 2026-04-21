# Observability

Sovyx ships every signal you need to trust the daemon in production —
structured logs with a stable wire contract, sagas that stitch a single
user intent across the cognitive loop / voice pipeline / bridges /
plugins, anomaly detection on the live tail, a privacy-conscious PII
redactor, Prometheus metrics, and an opt-in OpenTelemetry exporter.

This page is the public reference. For implementation deep-dives see
[`docs/modules/observability.md`](modules/observability.md).

## Overview — the envelope

Every structured log entry the daemon emits carries the same nine
**envelope** fields. The list is pinned at version `1.0.0`
(`SCHEMA_VERSION`) so downstream readers (FTS5 indexer, dashboard,
log forwarders, OTel exporters) can refuse incompatible payloads and
fail fast on drift.

| Field            | Type     | Meaning                                                    |
|------------------|----------|------------------------------------------------------------|
| `timestamp`      | string   | ISO-8601 UTC, e.g. `2026-04-20T18:30:01.234Z`.             |
| `level`          | enum     | `DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL`.       |
| `logger`         | string   | Dotted module name (e.g. `sovyx.voice.pipeline`).          |
| `event`          | string   | Canonical event name (snake.dot — see catalog below).      |
| `schema_version` | const    | Always `"1.0.0"` for v1 — bump on any breaking change.     |
| `process_id`     | int      | OS process id of the emitting daemon.                      |
| `host`           | string   | Hostname (`platform.node()`).                              |
| `sovyx_version`  | string   | Daemon package version (from `importlib.metadata`).        |
| `sequence_no`    | int ≥0   | Per-process monotonic counter (starts at 0).               |

`(timestamp, process_id, sequence_no)` is globally unique across a
daemon's lifetime. A log forwarder that retries a batch is expected to
deduplicate on this tuple.

Three additional **contextual ids** travel alongside the envelope when
the emit happens inside a saga or span:

| Field      | Meaning                                                              |
|------------|----------------------------------------------------------------------|
| `saga_id`  | Top-level operation id (a single user turn, a single bridge inbound). |
| `span_id`  | Sub-operation inside a saga (e.g. one LLM call inside a turn).        |
| `cause_id` | Parent event id within an EventBus dispatch chain.                    |

Forward-compatibility is part of the contract: every event schema sets
`additionalProperties: true`, so a phase can ship a new payload field
on an emit site before the catalog catches up. The
[`KnownEventValidator`](#known-events) processor surfaces those drifts
as `meta.unknown_field` rather than dropping records.

## Catalog of events

The canonical event names below are the contract. Anything emitted by
the daemon that's *not* in this list is tagged with
`meta.unknown_event=true` so an operator can spot drift on the
dashboard.

The table is generated from `scripts/_gen_log_schemas.py` and
materialised under `src/sovyx/observability/log_schema/<event>.json`
(JSON Schema Draft 2020-12) plus
`src/sovyx/observability/log_schema/_models.py` (typed pydantic
models). Editing those files by hand is a contract violation —
[update the generator](contributing.md) and re-run it instead.

<!-- BEGIN AUTO-GENERATED EVENTS TABLE — do not edit by hand -->

_36 canonical events. Regenerate via `uv run python scripts/_gen_log_schemas.py`._

| Event | Description | Required payload | Optional payload |
|---|---|---|---|
| `voice.audio.frame` | Per-frame audio capture telemetry from voice/audio.py. | `voice.frames`, `voice.rms`, `voice.sample_rate` | `voice.dropped`, `voice.peak` |
| `voice.vad.frame` | Silero VAD per-frame probability + rolling RMS window. | `voice.offset_threshold`, `voice.onset_threshold`, `voice.probability`, `voice.rms`, `voice.state` | — |
| `voice.vad.state_change` | VAD finite-state-machine transition (silence ↔ speech). | `voice.from_state`, `voice.offset_threshold`, `voice.onset_threshold`, `voice.prob_window`, `voice.probability`, `voice.rms`, `voice.rms_window`, `voice.to_state` | — |
| `voice.wake.score` | OpenWakeWord score per inference window. | `voice.cooldown_ms_remaining`, `voice.model_name`, `voice.score`, `voice.stage2_threshold`, `voice.state`, `voice.threshold` | — |
| `voice.wake.detected` | Wake-word fired and the orchestrator armed STT. | `voice.model_name`, `voice.score`, `voice.stage1_threshold`, `voice.stage2_threshold`, `voice.transcription`, `voice.window_frames` | — |
| `voice.stt.request` | STT request submitted (local Moonshine or cloud provider). | `voice.audio_ms`, `voice.language`, `voice.model`, `voice.provider`, `voice.sample_rate` | — |
| `voice.stt.response` | STT result returned with transcript + confidence. | `voice.audio_ms`, `voice.confidence`, `voice.language`, `voice.latency_ms`, `voice.model`, `voice.provider`, `voice.text_chars`, `voice.transcript` | — |
| `voice.tts.synth.start` | TTS synthesis kicked off (Kokoro or Piper). | `voice.engine`, `voice.model`, `voice.text_chars` | — |
| `voice.tts.synth.end` | TTS synthesis finished — total chunks + wall-clock duration. | `voice.model`, `voice.total_chunks`, `voice.total_ms` | — |
| `voice.tts.chunk` | Per-chunk TTS generation timing (audio_ms vs wall-clock ms). | `voice.audio_ms`, `voice.chunk_index`, `voice.generation_ms`, `voice.model`, `voice.sample_rate`, `voice.text_chars`, `voice.voice` | `voice.speaker_id` |
| `voice.tts.chunk.played` | TTS chunk dequeued from the output buffer and played to PortAudio. | `voice.chunk_index`, `voice.output_queue_depth`, `voice.playback_latency_ms` | — |
| `voice.deaf` | Capture stream is wedged — VAD probability stayed below floor. | `voice.consecutive_deaf_warnings`, `voice.frames_processed`, `voice.max_vad_probability`, `voice.mind_id`, `voice.state`, `voice.threshold`, `voice.voice_clarity_active` | — |
| `voice.frame.drop` | PortAudio reported a missing/late capture frame. | `voice.expected_frame_index`, `voice.gap_ms`, `voice.missing_frame_index`, `voice.stream_id` | — |
| `voice.barge_in.detected` | User started speaking while TTS was playing — playback was interrupted. | `voice.frames_sustained`, `voice.mind_id`, `voice.output_was_playing`, `voice.prob`, `voice.threshold_frames` | — |
| `voice.apo.detected` | Windows capture-side APO (Voice Clarity etc.) was found on the active endpoint. | `voice.apo_name`, `voice.device_id`, `voice.enabled`, `voice.endpoint_guid` | — |
| `voice.apo.bypass` | Coordinator switched the capture stream out of the APO chain. | `voice.auto_bypass_enabled`, `voice.consecutive_deaf_warnings`, `voice.mind_id`, `voice.threshold`, `voice.voice_clarity_active` | `voice.attempt_index`, `voice.strategy_name`, `voice.verdict` |
| `voice.stream.opened` | PortAudio capture stream opened with negotiated format. | `voice.channel_count`, `voice.device_id`, `voice.mode`, `voice.sample_rate`, `voice.stream_id` | — |
| `voice.device.hotplug` | OS audio endpoint changed (arrival / removal / default switch). | `voice.device_id`, `voice.device_name`, `voice.endpoint_guid`, `voice.event_type` | — |
| `plugin.invoke.start` | PluginManager.invoke() entered — tool dispatched to the sandbox. | `plugin.args_preview`, `plugin.timeout_s`, `plugin.tool_name`, `plugin_id` | — |
| `plugin.invoke.end` | Plugin tool returned (success or error) — duration + health snapshot. | `plugin.duration_ms`, `plugin.health.active_tasks`, `plugin.health.consecutive_failures`, `plugin.success`, `plugin.tool_name`, `plugin_id` | `plugin.error`, `plugin.result_preview` |
| `plugin.http.fetch` | SandboxedHttpClient.request() — request log + paired response log. | `plugin.http.allowed_domain`, `plugin.http.body_bytes`, `plugin.http.headers_redacted`, `plugin.http.method`, `plugin.http.rate_limited`, `plugin.http.url_host_only`, `plugin_id` | `plugin.http.attempt`, `plugin.http.latency_ms`, `plugin.http.response_bytes`, `plugin.http.status` |
| `plugin.fs.access` | SandboxedFsAccess read or write — path is relative to the plugin sandbox root. | `plugin.fs.bytes`, `plugin.fs.path_relative`, `plugin_id` | `plugin.fs.binary`, `plugin.fs.mode` |
| `llm.request.start` | LLMRouter dispatched a request to a provider. | `llm.context_tokens`, `llm.model`, `llm.provider`, `llm.system_tokens`, `llm.tokens_in` | — |
| `llm.request.end` | LLMRouter received the provider response — tokens, latency, cost. | `llm.cost_usd`, `llm.duration_ms`, `llm.model`, `llm.provider`, `llm.stop_reason`, `llm.tokens_in`, `llm.tokens_out` | — |
| `brain.query` | BrainService retrieval — start log + completion log share the event name. | `brain.filter`, `brain.k`, `brain.query_len` | `brain.latency_ms`, `brain.result_count`, `brain.search_mode`, `brain.top_score` |
| `brain.episode.encoded` | Reflect phase wrote a new episode + its concept extractions. | `brain.concepts_extracted`, `brain.episode_id`, `brain.novelty`, `brain.top_concept` | — |
| `bridge.send` | Outbound message dispatched on a bridge channel (telegram, signal, …). | `bridge.channel_type`, `bridge.message_bytes`, `bridge.message_id`, `bridge.recipient_hash` | — |
| `bridge.receive` | Inbound message accepted from a bridge channel. | `bridge.channel_type`, `bridge.message_bytes`, `bridge.message_id`, `bridge.sender_hash` | — |
| `ws.connect` | Dashboard WebSocket client connected. | `net.active_count`, `net.client` | — |
| `http.request` | Dashboard HTTP request — method, path, status, latency. | `net.client`, `net.method`, `net.path`, `net.request_bytes` | `net.error_type`, `net.failed`, `net.latency_ms`, `net.response_bytes`, `net.status_code` |
| `config.value.resolved` | Startup cascade emitted the resolved value for one EngineConfig field. | `cfg.field`, `cfg.source`, `cfg.value` | `cfg.env_key` |
| `config.value.changed` | Runtime config mutation (dashboard or RPC) — audit trail. | `audit.actor`, `audit.field`, `audit.source` | `audit.new`, `audit.old`, `audit.request_id` |
| `license.validated` | LicenseValidator accepted (or refused) the JWT — tier + expiry surfaced. | `license.expiry`, `license.feature_count`, `license.minds_max`, `license.subject_hash`, `license.tier` | `license.expired_for_seconds`, `license.grace_days_remaining` |
| `audit.permission_change` | Plugin permission denied or escalated — emitted on permission. | `plugin.permission.detail`, `plugin.tool_name`, `plugin_id` | `plugin.permission.attempted_resource`, `plugin.permission.required` |
| `meta.canary.tick` | Synthetic heartbeat — confirms the logging pipeline is reachable end-to-end. | `meta.lag_ms`, `meta.tick_id`, `meta.timestamp` | — |
| `meta.audit.tick` | Audit-of-auditor — verifies the tamper chain is advancing. | `meta.audit_entries_count`, `meta.chain_hash`, `meta.tick_id` | — |

<!-- END AUTO-GENERATED EVENTS TABLE -->

### Known events {#known-events}

`sovyx.observability.schema.KNOWN_EVENTS` maps each event name to the
typed pydantic class. Use it from tests and tooling:

```python
from sovyx.observability.schema import KNOWN_EVENTS, validate_event

model = validate_event(entry)  # → typed LogEvent or None
print(sorted(KNOWN_EVENTS))    # → every cataloged event name
```

`validate_event` returns `None` when the event isn't in the catalog —
the wire stays open, but `KnownEventValidator` (installed in the
default structlog processor chain) flags those records with
`meta.unknown_event=true` so they surface on the operator dashboard.
Records on a *known* event that carry payload keys not in the catalog
get tagged with `meta.unknown_fields=[…]`.

## Sagas

A **saga** is one logical user intent — a single chat turn, one voice
turn, one inbound bridge message. Sagas stitch together every log
entry that belongs to that intent so an operator can pull a single
timeline.

Three id fields collaborate:

* `saga_id` — UUID for the top-level operation. Generated by
  `@trace_saga` decorators on entry points (`cognitive/loop.py`,
  `voice/pipeline/_orchestrator.py`, `bridge/manager.py`,
  `dashboard/routes/chat.py`).
* `span_id` — UUID for a child operation (e.g. one LLM call, one STT
  request). Multiple spans share one saga.
* `cause_id` — id of the parent event in an EventBus dispatch chain.
  Lets a tool reconstruct *which* upstream event caused a downstream
  one inside the same saga.

Sagas propagate via `contextvars`, so they survive `asyncio.create_task`
spawn (`spawn()` in `observability/tasks.py` carries the context) and
EventBus emit chains.

Surfaces:

* **API**: `GET /api/logs/sagas/{saga_id}` returns every entry, ordered
  by `(timestamp, sequence_no)`.
* **API**: `GET /api/logs/sagas/{saga_id}/story` runs the
  `narrative.py` template engine over the entries and returns a
  human-readable account ("Voice wake fired at 18:30:02, STT returned
  in 380 ms, LLM call cost $0.004…").
* **Dashboard**: the *Logs* page exposes a saga selector that pivots
  the live tail to a single saga + renders the timeline, causality
  graph, and narrative panel.

## Anomalies and narrative

`observability/anomaly.py` runs a sliding-window detector on the live
tail and flags:

* repeated identical events (e.g. `voice.deaf` more than 3× in a row),
* level escalations (an event whose default level was `INFO` but is
  now arriving at `WARNING`/`ERROR`),
* missing heartbeats (`meta.canary.tick` overdue),
* sudden cardinality spikes on `event` or `logger`.

Detected anomalies surface in `GET /api/logs/anomalies` and in the
dashboard's *Anomalies* drawer.

`observability/narrative.py` ships 30+ templates that translate raw
event sequences into prose. The dashboard's *Narrative* panel feeds
the current saga through it; the same engine powers the
`/story` endpoint above.

## PII modes

`observability/pii.py` ships three redaction modes (`OFF`, `MASK`,
`HASH`) controlled via `EngineConfig.observability.pii_mode`. The
default is `MASK` — emails, phone numbers, IPv4/IPv6 addresses, JWTs,
and secrets matched by `SecretMasker` are replaced with a typed token
(`<email>`, `<phone>`, `<ip>`).

`HASH` mode swaps the token for a stable per-process SHA-256 prefix so
operators can correlate hits across entries without learning the
underlying value. `OFF` is for offline debugging only — never enable
it on a daemon that ships logs off-host.

The PII processor runs *after* the secret masker and *before* the
field clamp + renderer, so even a rogue caller that smuggles a
secret-shaped string into a payload field gets redacted.

## Rotation and retention

The daemon writes JSONL to `data/logs/sovyx.log` via a
`RotatingFileHandler` (default: 50 MB × 5 files). Rotation is wrapped
in `AsyncQueueHandler` + `BackgroundLogWriter` so emit sites are never
blocked on disk IO.

Critical and security-tagged records bypass the queue via
`FastPathHandler`, which `fsync`s synchronously to a separate
`fast_path.log` so a crash can't lose them.

The `RingBufferHandler` keeps the most recent N records in memory and
dumps them to `data/logs/crash.log` on `sys.excepthook` /
`asyncio` unhandled-exception hook firing, so a hard crash leaves a
forensic trail even when the rotating file handler couldn't flush.

Retention beyond rotation is up to the operator — Sovyx itself does
not delete rotated files. Use `logrotate(8)` or your platform's
equivalent.

## Endpoints

| Endpoint                             | Purpose                                             |
|--------------------------------------|-----------------------------------------------------|
| `GET /api/logs`                      | Paginated tail + filter by level / logger / event.  |
| `GET /api/logs/search`               | FTS5-backed full-text query (falls back to `/api/logs` if the sidecar is absent — clients see HTTP 503 + `X-Fallback: /api/logs`). |
| `GET /api/logs/sagas/{saga_id}`      | Every entry for one saga, ordered by sequence.      |
| `GET /api/logs/sagas/{saga_id}/story`| Narrative-engine prose for that saga.               |
| `GET /api/logs/sagas/{saga_id}/causality` | Causality graph (DAG of `cause_id` edges).      |
| `GET /api/logs/anomalies`            | Currently-active anomaly findings.                  |
| `WS /api/logs/stream`                | Live tail with the same filter contract as `/logs`. |

All endpoints require the dashboard auth token. The WebSocket also
forwards the token via the `Sec-WebSocket-Protocol` header.

## Prometheus metrics

`/metrics` (default port: 9101) exposes counters and histograms for
every instrumented hot path. Names follow the
[OTel semantic conventions](https://opentelemetry.io/docs/specs/semconv/)
where one exists; new metrics get a cardinality budget enforced at
registration time (`MetricsRegistry.register(..., max_cardinality=N)`)
so a runaway label can't blow up scrape size.

Headline series:

* `sovyx_voice_vad_state` — current VAD state per mind.
* `sovyx_voice_deaf_total` — count of deaf events (capture stream wedged).
* `sovyx_llm_request_seconds` — histogram per provider × model.
* `sovyx_llm_cost_usd_total` — cumulative cost per provider × model.
* `sovyx_brain_query_seconds` — retrieval latency histogram.
* `sovyx_plugin_invoke_seconds` — sandbox dispatch latency per plugin.
* `sovyx_log_dropped_total{reason="…"}` — async-queue overflow / clamp.

The Prometheus exporter is on by default. Disable via
`SOVYX_OBSERVABILITY__PROMETHEUS__ENABLED=false`.

## OpenTelemetry exporter (opt-in)

`observability/_otel.py` wires an OTLP exporter when
`SOVYX_OBSERVABILITY__OTEL__ENABLED=true`. Default endpoint:
`localhost:4317` (OTLP/gRPC).

When enabled:

* every saga becomes a tracer span (saga_id → `trace_id`, span_id →
  `span_id`),
* envelope fields land on `Resource` attributes following OTel
  semconv (`service.name=sovyx`, `service.version=<sovyx_version>`,
  `host.name=<host>`),
* metrics are forwarded to the same OTLP endpoint via a periodic
  exporter (default 10 s interval).

The exporter is **off by default** because it adds an outbound network
dependency operators must explicitly opt into.

## FAQ

**Why dotted event names like `voice.vad.frame`?**
Two reasons: it lets a single regex (`event:voice.*`) target a domain
on the dashboard, and it survives JSON-serialisation as-is (no escape
gymnastics). The [`KnownEventValidator`](#known-events) makes sure
typos are caught in CI before they ship.

**My new emit site isn't in the catalog. Is that a CI failure?**
No. The wire is forward-compatible — `additionalProperties: true` on
every schema and `KnownEventValidator` tags unknown events with
`meta.unknown_event=true` instead of dropping them. The CI gate
(`scripts/check_log_schemas.py`) only fails on entries whose event
name *is* cataloged but whose payload doesn't match the schema. To
make the new event official, add it to the EVENTS table in
`scripts/_gen_log_schemas.py` and regenerate.

**How do I trace one user request across the whole daemon?**
Pull `saga_id` from any log entry that interests you and call
`GET /api/logs/sagas/{saga_id}` (or `/story` for a prose summary).
The dashboard *Logs* page has a "filter by this saga" pivot on every
row.

**Where do anomaly findings live?**
Active findings: `GET /api/logs/anomalies` and the dashboard's
*Anomalies* drawer. Historic findings are not persisted — anomaly
detection runs on the live ring buffer.

**Can I disable PII redaction temporarily for a session?**
`SOVYX_OBSERVABILITY__PII_MODE=OFF` works, but it's a footgun for
production. Prefer `MASK` (default) or `HASH` if you need correlation.
Logs that ship off-host (forwarders, OTel exporter) should *never* run
with `OFF`.

**How do I deduplicate at-least-once log delivery on my forwarder?**
Use `(timestamp, process_id, sequence_no)` — a globally-unique key
across the daemon's lifetime. `sequence_no` is monotonic per process
and starts at 0 each time the daemon boots.
