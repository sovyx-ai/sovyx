# API Reference

Sovyx exposes an HTTP + WebSocket API served by the embedded FastAPI dashboard.
The default bind address is `127.0.0.1:7777`. All endpoints except `/metrics`
require a Bearer token.

## Authentication

On first start the daemon generates a 32-byte URL-safe token and stores it in
`~/.sovyx/token` with `0o600` permissions. Retrieve it with:

```bash
sovyx token
```

All requests must include the token:

```
Authorization: Bearer <token>
```

For WebSocket connections the token is passed as a query parameter:

```
GET /ws?token=<token>
```

### Error responses

| Status | Meaning                                                        |
| ------ | -------------------------------------------------------------- |
| 401    | Missing or invalid Bearer token.                               |
| 403    | Token valid but request is forbidden in the current state.     |
| 422    | Request body failed pydantic validation.                       |
| 429    | Per-route rate limit exceeded. Check `X-RateLimit-*` headers.  |
| 500    | Unhandled server error. Correlate with `X-Request-Id`.         |
| 503    | Service registry not ready or a required subsystem is down.    |

Every response carries `X-Request-Id`. Include it when reporting bugs.

---

## REST endpoints

### Health and status

| Method | Path                      | Description                                              |
| ------ | ------------------------- | -------------------------------------------------------- |
| GET    | `/api/status`             | Daemon version, uptime, and mind summary.                |
| GET    | `/api/stats/history`      | Time-series of engine stats for dashboard charts.        |
| GET    | `/api/health`             | Aggregate health of registered subsystems.               |
| GET    | `/api/engine/degraded`    | Composite cross-axis degraded snapshot (voice + LLM + STT + dashboard + future axes) with per-axis severity / title token / body token / action chips. Severity escalation: 1 axis=`warn`, 2=`error`, 3+=`critical`. Mission C4 §T1.6 + Mission C5 §T2.3 (dashboard axis). |
| GET    | `/metrics`                | Prometheus exposition (no auth — bind to loopback only). |

### Conversations

| Method | Path                                  | Description                          |
| ------ | ------------------------------------- | ------------------------------------ |
| GET    | `/api/conversations`                  | List conversations, newest first.    |
| GET    | `/api/conversations/{conversation_id}`| Fetch messages for a conversation.   |

### Brain

| Method | Path                        | Description                                                             |
| ------ | --------------------------- | ----------------------------------------------------------------------- |
| GET    | `/api/brain/graph`          | Concept and relation graph for visualisation.                           |
| GET    | `/api/brain/search`         | Hybrid lexical (FTS5) + vector (sqlite-vec) search across the brain.    |
| GET    | `/api/brain/search/vector`  | Pure vector search — embedding kNN, bypasses FTS5.                      |

### Emotions

| Method | Path                          | Description                                                      |
| ------ | ----------------------------- | ---------------------------------------------------------------- |
| GET    | `/api/emotions/current`       | Current emotional state (valence, arousal, dominance, mood tag). |
| GET    | `/api/emotions/timeline`      | Time-series of emotional state transitions.                      |
| GET    | `/api/emotions/triggers`      | Recent triggers that moved emotional state, ranked by impact.    |
| GET    | `/api/emotions/distribution`  | Aggregate distribution across emotional categories.              |

### Activity and logs

| Method | Path                      | Description                                      |
| ------ | ------------------------- | ------------------------------------------------ |
| GET    | `/api/activity/timeline`  | Recent cognitive activity timeline.              |
| GET    | `/api/logs`               | Paginated access to the rotating log file.       |

### Settings and configuration

| Method | Path              | Description                                           |
| ------ | ----------------- | ----------------------------------------------------- |
| GET    | `/api/settings`   | Mutable user settings.                                |
| PUT    | `/api/settings`   | Update user settings (partial).                       |
| GET    | `/api/config`     | Resolved `EngineConfig` (read-only view).             |
| PUT    | `/api/config`     | Update mutable config keys; invalid keys return 422.  |

### Voice

Voice routes are spread across 9 router files (`src/sovyx/dashboard/routes/voice*.py`).
The tables below group them by area. Every route resolves the active mind via
`_shared.resolve_active_mind_id_for_request` and emits
`dashboard.shared.fallback_default_mind` (WARN) when no mind id is provided in
the request — see [`observability.md`](observability.md) §"Pending catalog
promotion (post-v0.39.0)" for the event contract.

#### Voice — control plane

| Method | Path                              | Description                                                           |
| ------ | --------------------------------- | --------------------------------------------------------------------- |
| GET    | `/api/voice/status`               | Voice pipeline state and device info. **Mission H2 v0.49.7+**: the `capture` sub-object carries two optional+nullable platform-metadata fields populated from the most recent bypass-coordinator dispatch — `last_bypass_event_platform` (Literal `linux` / `windows` / `darwin` / `other`) and `last_bypass_event_family` (open string mirroring `PlatformAudioFamily` — `alsa_capture_chain`, `voice_clarity`, `module_echo_cancel`, `pipewire_filter_chain`, `wireplumber_default_source`, `voice_isolation`, `coreaudio_voice_processing`, or `noop`). Both default `null` on pristine boots + on every snapshot before the first dispatch fires. Promoted to required at v0.51.0 STRICT per ADR-D15 + anti-pattern #29 forward-additive optional discipline. |
| GET    | `/api/voice/service-health`       | Aggregated readiness probe (T6.20). 4-field response for monitoring.  |
| GET    | `/api/voice/models`               | Installed STT/TTS/VAD model catalogue.                                |
| GET    | `/api/voice/models/status`        | Per-model on-disk status (present / downloading / missing).           |
| POST   | `/api/voice/models/download`      | Trigger a model download. SSE-style progress on the same request.     |
| GET    | `/api/voice/voices`               | Available Piper / Kokoro voice catalogue (per spoken language).       |
| GET    | `/api/voice/hardware-detect`      | Probe GPU/CPU and return the recommended voice profile.               |
| POST   | `/api/voice/enable`               | Start the voice pipeline (downloads models on first run).             |
| POST   | `/api/voice/disable`              | Stop the voice pipeline and release audio devices.                    |
| POST   | `/api/voice/forget`               | Erase voice-derived data per the GDPR / LGPD lifecycle.               |
| GET    | `/api/voice/wake-word/status`     | Per-mind wake-word state — armed model, cooldown, last score.         |
| GET    | `/api/voice/capture-diagnostics`  | RMS / VAD / APO / device-resolver snapshot for triage.                |
| POST   | `/api/voice/capture-exclusive`    | Request WASAPI exclusive-mode capture (Windows). Emits `CaptureRestartFrame` (anti-pattern #29). |
| GET    | `/api/voice/linux-mixer-diagnostics` | Linux ALSA / PipeWire mixer probe + saturation report.             |
| POST   | `/api/voice/linux-mixer-reset`    | Reset mixer levels to known-good defaults (closes `linux_mixer_saturated` confidence=1.00 finding). |
| GET    | `/api/voice/platform-diagnostics` | Cross-platform parity probe — Linux/Windows/macOS deltas.             |

`GET /api/voice/service-health` returns
`{ready: bool, reason: str, last_diagnosis: str | null, watchdog_state:
str | null, user_remediation: str | null}` — never 5xx; failure modes
degrade via the `reason` field (closed enum: `ok`, `voice_disabled`,
`engine_not_running`, `voice_pipeline_not_registered`,
`last_diagnosis_unhealthy`). `user_remediation` (T6.12) carries an
operator-facing hint when `last_diagnosis` maps to a known
remediation.

#### Voice — health & quarantine

| Method | Path                              | Description                                                           |
| ------ | --------------------------------- | --------------------------------------------------------------------- |
| GET    | `/api/voice/health`               | ComboStore + overrides + quarantine snapshot (dashboard-grade).       |
| GET    | `/api/voice/health/quarantine`    | Kernel-invalidated quarantine snapshot.                               |
| GET    | `/api/voice/health/failover-history` | Runtime failover-ladder history ring (default 32 entries, newest first) — per ladder run: verdict (`succeeded`/`exhausted`/`in_progress`), per-candidate detail (verdict + `error_class` + `elapsed_ms` + `skipped_reason`), `from_endpoint`, `ladder_id` (uuid4) for log correlation. Mission C3 §T2.9. |
| POST   | `/api/voice/health/reprobe`       | Re-run diagnosis on the active capture endpoint.                      |
| POST   | `/api/voice/health/forget`        | Forget a device entry from the ComboStore (manual operator override). |
| POST   | `/api/voice/health/pin`           | Pin a device's diagnosis verdict — disables auto-quarantine.          |
| GET    | `/api/voice/frame-history`        | Bounded ring buffer of `PipelineFrame` transitions (256 entries, observability per anti-pattern #25). |
| GET    | `/api/voice/restart-history`      | Bounded ring buffer of `CaptureRestartFrame` events (anti-pattern #29). |

#### Voice — calibration

| Method | Path                                              | Description                                                           |
| ------ | ------------------------------------------------- | --------------------------------------------------------------------- |
| POST   | `/api/voice/calibration/start`                    | Start a calibration run. Mind id required (`feedback_no_speculation` Pattern A — no sentinel default per anti-pattern #35). |
| GET    | `/api/voice/calibration/status/{job_id}`          | Poll calibration progress.                                            |
| POST   | `/api/voice/calibration/cancel/{job_id}`          | Cancel an in-flight run.                                              |
| GET    | `/api/voice/calibration/profile`                  | Read the active calibration profile (schema v1; signature verdict).   |
| GET    | `/api/voice/calibration/profile/inspect`          | Detailed profile dump with rule-firing trace + measurement breakdown. |
| POST   | `/api/voice/calibration/profile/regenerate`       | Regenerate profile from current measurements (no fresh capture).      |
| GET    | `/api/voice/calibration/backups`                  | List timestamped profile backups for the resolved mind.               |
| POST   | `/api/voice/calibration/backup/restore`           | Restore a backup by timestamp.                                        |
| WS     | `/api/voice/calibration/jobs/{job_id}/stream`     | Live calibration progress stream (per-rule firing + decision events). |
| POST   | `/api/voice/calibration/signing-key/generate`     | Server-side Ed25519 key-gen flow (alternative to the CLI command).    |
| GET    | `/api/voice/calibration/signing-key/status`       | Key presence + fingerprint without exposing the private half.         |

#### Voice — wizard (onboarding flow)

| Method | Path                              | Description                                                           |
| ------ | --------------------------------- | --------------------------------------------------------------------- |
| GET    | `/api/voice/wizard/devices`       | Enumerate audio devices for the wizard picker.                        |
| POST   | `/api/voice/wizard/test-record`   | Capture a short test recording for the device-sanity step.            |
| GET    | `/api/voice/wizard/tts-engines`   | Available TTS engines (Piper / Kokoro) + per-engine voice list.       |
| GET    | `/api/voice/wizard/diagnostic`    | Wizard-grade `doctor voice` summary (capture / VAD / wake-word).      |
| GET    | `/api/voice/wizard/...`           | Additional wizard probes — see `routes/voice_wizard.py` for the full set. |
| POST   | `/api/voice/wizard/telemetry`     | Operator-attribution telemetry on wizard step completion (204).       |

#### Voice — wake-word training

| Method | Path                                              | Description                                                           |
| ------ | ------------------------------------------------- | --------------------------------------------------------------------- |
| POST   | `/api/voice/training/jobs`                        | Submit a wake-word training job for the resolved mind.                |
| GET    | `/api/voice/training/jobs`                        | List training jobs.                                                   |
| GET    | `/api/voice/training/jobs/{job_id}`               | Job detail + status.                                                  |
| POST   | `/api/voice/training/jobs/{job_id}/cancel`        | Cancel a queued / running job.                                        |
| WS     | `/api/voice/training/jobs/{job_id}/stream`        | Live training progress (steps + loss + samples).                      |

#### Voice — KB profile (anti-pattern #26)

| Method | Path                                              | Description                                                           |
| ------ | ------------------------------------------------- | --------------------------------------------------------------------- |
| GET    | `/api/voice/kb/profiles`                          | List installed KB profiles for the active mind.                       |
| GET    | `/api/voice/kb/profiles/{name}`                   | Inspect a KB profile (signature verdict, content, provenance).        |
| POST   | `/api/voice/kb/profiles/{name}/install`           | Install / pin a profile from the local store.                         |
| POST   | `/api/voice/kb/contribute`                        | Submit a calibration profile back to the community KB queue (post-anonymisation). |

#### Voice — device-test (legacy URL — superseded by wizard endpoints)

| Method | Path                              | Description                                                           |
| ------ | --------------------------------- | --------------------------------------------------------------------- |
| GET    | `/api/voice/test/devices`         | Enumerate audio devices available to the setup wizard.                |
| WS     | `/api/voice/test/input`           | Live RMS/peak/hold meter stream for mic sanity-check.                 |
| POST   | `/api/voice/test/output`          | Queue a TTS playback job on an output device.                         |
| GET    | `/api/voice/test/output/{job_id}` | Poll playback job until `done` or `error`.                            |

See [`voice-device-test`](modules/voice-device-test.md) for the frame protocol,
error taxonomy, rate-limits, and tuning knobs.

The route list above is verified against
`src/sovyx/dashboard/routes/voice*.py` at HEAD. The auto-generated OpenAPI
schema at `/openapi.json` is the contract source of truth — refer to it when
implementing a client, and treat this table as a navigation aid.

### Plugins

Runtime control of plugins. For install-time credential and configuration
flows, see the [Setup](#setup) section below.

| Method | Path                      | Description                                   |
| ------ | ------------------------- | --------------------------------------------- |
| GET    | `/api/plugins`            | Installed plugins with permissions and state. |
| GET    | `/api/plugins/tools`      | Registered tools across all enabled plugins.  |
| GET    | `/api/plugins/{id}`       | Manifest, risk, and health for one plugin.    |
| POST   | `/api/plugins/{id}/enable`| Enable a plugin (permissions must be granted).|
| POST   | `/api/plugins/{id}/disable`| Disable a plugin.                            |
| POST   | `/api/plugins/{id}/reload`| Hot-reload a plugin's manifest and code.      |

### Setup

Install-time wizard flow for plugins that need external credentials or
configuration (Telegram, Home Assistant, CalDAV, custom LLM providers, …).
Separated from `/api/plugins/*` so runtime enable/disable is never mixed
with credential entry.

| Method | Path                                        | Description                                                  |
| ------ | ------------------------------------------- | ------------------------------------------------------------ |
| GET    | `/api/setup/{plugin_name}/schema`           | Return the JSON schema the wizard uses to render fields.     |
| POST   | `/api/setup/{plugin_name}/test-connection`  | Validate credentials against the remote service.             |
| POST   | `/api/setup/{plugin_name}/configure`        | Persist configuration + secrets (secrets go to the keyring). |
| POST   | `/api/setup/{plugin_name}/enable`           | Enable after configuration is complete.                      |
| POST   | `/api/setup/{plugin_name}/disable`          | Disable from the wizard (shortcut for `/api/plugins/{id}/disable`). |

### Onboarding

First-run flow that configures the default provider, personality, and an
optional Telegram bridge. Each step returns the updated onboarding state
so the dashboard wizard can drive a step machine.

| Method | Path                                  | Description                                                         |
| ------ | ------------------------------------- | ------------------------------------------------------------------- |
| GET    | `/api/onboarding/state`               | Current onboarding progress and the next step.                      |
| POST   | `/api/onboarding/provider`            | Pick and validate an LLM provider; persists the API key.            |
| POST   | `/api/onboarding/personality`         | Write the mind's personality and name.                              |
| POST   | `/api/onboarding/channel/telegram`    | Provision an optional Telegram bridge.                              |
| POST   | `/api/onboarding/complete`            | Mark onboarding complete; idempotent.                               |

### Channels

| Method | Path                              | Description                                 |
| ------ | --------------------------------- | ------------------------------------------- |
| GET    | `/api/channels`                   | Connected bridge channels and their health. |
| POST   | `/api/channels/telegram/setup`    | Provision the Telegram channel interactively|

### Chat

| Method | Path                 | Description                                                                                   |
| ------ | -------------------- | --------------------------------------------------------------------------------------------- |
| POST   | `/api/chat`          | Synchronous single-turn chat used by the dashboard UI when streaming is unavailable.          |
| POST   | `/api/chat/stream`   | Server-Sent Events (SSE) stream. Emits `token`, `phase`, `done`, and `error` events.          |

The SSE stream sends one event per chunk:

```
event: phase
data: {"phase": "thinking"}

event: token
data: {"delta": "Hello"}

event: done
data: {"conversation_id": "...", "tokens_in": 512, "tokens_out": 40, "latency_ms": 812}
```

Any failure mid-stream is delivered as a terminal `error` event with `code`
and `message`, then the connection is closed. Dashboards should fall back
to `POST /api/chat` if the stream cannot be opened.

### Data portability

| Method | Path                                  | Description                                               |
| ------ | ------------------------------------- | --------------------------------------------------------- |
| GET    | `/api/export`                         | Export the active mind in SMF format.                     |
| POST   | `/api/import`                         | Import an SMF archive (replace or merge, declared in body). |
| POST   | `/api/import/conversations`           | Multipart upload (`platform=chatgpt\|claude\|gemini` + `file`). Returns `202` with `{job_id, conversations_total}`; encoding runs in a background `asyncio.Task`. |
| GET    | `/api/import/{job_id}/progress`       | Live snapshot for an import job: `{state, conversations_processed/skipped, episodes_created, concepts_learned, warnings, error, elapsed_ms}`. |

### Safety

| Method | Path                      | Description                                    |
| ------ | ------------------------- | ---------------------------------------------- |
| GET    | `/api/safety/stats`       | Aggregate safety counters (blocks, redactions).|
| GET    | `/api/safety/status`      | Current mode (enforce / shadow) and thresholds.|
| GET    | `/api/safety/history`     | Recent safety audit events.                    |
| GET    | `/api/safety/rules`       | Active custom rules and banned topics.         |
| PUT    | `/api/safety/rules`       | Update rules; schema validated against spec.   |

### LLM providers

| Method | Path              | Description                                                |
| ------ | ----------------- | ---------------------------------------------------------- |
| GET    | `/api/providers`  | Configured providers, current routing, and credit balance. |
| PUT    | `/api/providers`  | Update provider keys and routing preferences.              |
| GET    | `/api/llm/health` | Mission C6 §T2.7 — cached `LLMRouterDiscoveryReport` snapshot with verdict + per-provider liveness matrix. Forward-additive (`extra="allow"`). |
| POST   | `/api/llm/test-connection` | Mission C6 §T2.7 — probe a candidate provider transiently. Body: `{provider, api_key?, model?}`. Returns `{ok, message, latency_ms}`. Never persists; never hot-registers. 422 on invalid provider name or missing key for cloud providers. |

### `axis="llm"` refined reason taxonomy (Mission C6)

The composite `/api/engine/degraded` payload now distinguishes seven
LLM-axis failure modes (was a single `no_llm_provider` reason
pre-Mission-C6):

| Reason | Severity | When |
|---|---|---|
| `no_provider_configured` | critical | No cloud keys AND Ollama not reachable AND no `default_provider="ollama"` regression signal |
| `ollama_unreachable` | error | `default_provider="ollama"` AND Ollama ping fails (regression from known-good state) |
| `ollama_no_models` | warn | Ollama reachable + `list_models() == []` AND no cloud fallback |
| `cloud_key_invalid` | error | Every configured cloud key validated as invalid + no Ollama fallback |
| `all_providers_unhealthy` | error | At least one provider configured but none currently available |
| `default_model_unavailable` | error | Provider available but `default_model` not in its catalogue |
| `partial_health` | warn | Some providers available + some unhealthy (routing continues) |

The legacy `no_llm_provider` reason is dual-emitted through the v0.49.x
cycle (ADR-D14); Phase 3 v0.50.0 drops it. See
[docs/modules/llm-provider-integrity.md](modules/llm-provider-integrity.md)
for the full taxonomy + action-chip mapping.

### Telemetry

| Method | Path                              | Description                                                          |
| ------ | --------------------------------- | -------------------------------------------------------------------- |
| POST   | `/api/telemetry/frontend-error`   | Dashboard `ErrorBoundary` posts unhandled React errors to structlog. |

Payload is untrusted: all fields are length-capped and the endpoint is
rate-limited to 20 reports per 60 s across all clients so a crash loop in
a lazy chunk cannot flood the log file. The response is always `200` —
dropped reports return `{"ok": true, "dropped": true}`.

### SPA fallback

Any unmatched `GET /{path}` returns `index.html` from the bundled static
dashboard so that client-side routing works under deep links. API paths
beginning with `/api/` or `/ws` bypass the fallback.

---

## `/api/engine/degraded` — composite axis taxonomy

The composite endpoint surfaces every operator-actionable degraded
subsystem in a single payload. Severity escalates by distinct axis count
(1 = `warn`, 2 = `error`, 3+ = `critical`). Each axis entry carries an
i18n `title_token` + `body_token`, an `action_chips` array of
operator-actionable next steps, and a free-form `metadata` map for
axis-specific context. The schema is forward-additive (Pydantic
`extra="allow"` + zod `.passthrough()`) — new axes ship without a
route migration.

| `axis` | `reason` values | Severity | Producer | Mission |
|---|---|---|---|---|
| `voice` | `failover_ladder_exhausted` | `error` | `voice/health/_runtime_failover.py` on `voice.failover.ladder_complete{verdict=exhausted}` | C3 §T1.3 + C4 §T1.4 |
| `llm`   | `no_llm_provider` | `error` | `engine/bootstrap.py:735` on `no_llm_provider_detected` | C4 §T1.2 |
| `stt`   | `stt_language_coerced` | `warn` | `voice/factory/_validate.py:542` on `voice.factory.stt_language_unsupported` | C4 §T1.3 |
| `dashboard` | `bundle_partial` \| `bundle_missing` | `error` (partial) \| `critical` (missing) | `dashboard/server.py::create_app()` four-state classifier + `_IntegrityAwareStaticFiles` reactive on-404 arm | C5 §T2.1 / §T2.2 |

### `axis="dashboard"` (Mission C5)

The dashboard axis surfaces failures in the SPA bundle integrity layer
— the wheel-baked static assets needed for the browser UI to render.

`reason="bundle_partial"` (severity `error`):

The integrity scanner found `index.html` plus at least one referenced
chunk, but not ALL referenced chunks, on disk. The SPA may still render
some routes; the operator's install is corrupted. `body_token` resolves
to `degraded.dashboard.bundle_partial.partial.body`.

`reason="bundle_missing"` (severity `critical`):

Either `index.html`, the entire `static/` directory, or the `assets/`
directory is absent. The SPA cannot render at all. The `body_token`
resolves to one of three verdict-discriminated strings:

* `degraded.dashboard.bundle_missing.index_html_missing.body`
* `degraded.dashboard.bundle_missing.static_dir_missing.body`
* `degraded.dashboard.bundle_missing.legacy_index_html_no_assets.body`

The exact verdict is also carried as `metadata.verdict` for tooling
discrimination.

#### `metadata` fields (dashboard axis)

| Field | Type | Description |
|---|---|---|
| `verdict` | string | One of `partial` / `index_html_missing` / `static_dir_missing` / `legacy_index_html_no_assets` |
| `missing_count` | int | Number of referenced chunks absent on disk |
| `missing_sample` | string[] | First 5 missing-asset paths (POSIX-relative to `static/`) |
| `static_dir` | string | Absolute path of the scanned static directory |
| `scan_duration_ms` | float | Wall-clock scan duration in milliseconds |

#### `action_chips` (dashboard axis)

Two operator-action chips emitted by the producer:

| `label_token` | `action` | `target` (default — override via `SOVYX_TUNING__DASHBOARD__INTEGRITY_ACTION_CHIP_*_URL`) |
|---|---|---|
| `degraded.dashboard.reinstall` | `external_link` | `https://sovyx.dev/docs/install/troubleshooting#reinstall` |
| `degraded.dashboard.runDoctor` | `external_link` | `https://sovyx.dev/docs/cli/doctor#dashboard` |

See [`docs/modules/dashboard-distribution-integrity.md`](modules/dashboard-distribution-integrity.md)
for the full triage workflow, the related `dashboard.distribution.*`
OpenTelemetry events, and the tuning-knob reference.

---

## WebSocket

### Endpoint

```
GET /ws?token=<token>
```

The server upgrades to WebSocket and starts broadcasting engine events as JSON
messages. Each message has the following shape:

```json
{
  "type": "ThinkCompleted",
  "timestamp": "2026-04-14T12:00:00.123456+00:00",
  "correlation_id": "c1b2a3...",
  "data": {
    "tokens_in": 512,
    "tokens_out": 128,
    "model": "gpt-4o-mini",
    "cost_usd": 0.000384,
    "latency_ms": 812
  }
}
```

### Event types

The bridge forwards twelve event types from the engine bus:

| Event                     | `data` fields                                                                  |
| ------------------------- | ------------------------------------------------------------------------------ |
| `EngineStarted`           | (empty)                                                                        |
| `EngineStopping`          | `reason`                                                                       |
| `ServiceHealthChanged`    | `service`, `status`                                                            |
| `PerceptionReceived`      | `source`, `person_id`                                                          |
| `ThinkCompleted`          | `tokens_in`, `tokens_out`, `model`, `cost_usd`, `latency_ms`                   |
| `ResponseSent`            | `channel`, `latency_ms`                                                        |
| `ConceptCreated`          | `concept_id`, `title`, `source`                                                |
| `EpisodeEncoded`          | `episode_id`, `importance`                                                     |
| `ConsolidationCompleted`  | `merged`, `pruned`, `strengthened`, `duration_s`                               |
| `DreamCompleted`          | `patterns_found`, `concepts_derived`, `relations_strengthened`, `episodes_analyzed`, `duration_s` (v0.11.6) |
| `ChannelConnected`        | `channel_type`                                                                 |
| `ChannelDisconnected`     | `channel_type`, `reason`                                                       |

---

## Example — REST

```bash
TOKEN=$(sovyx token)

curl -sS http://127.0.0.1:7777/api/status \
  -H "Authorization: Bearer $TOKEN" | jq

curl -sS -X POST http://127.0.0.1:7777/api/chat \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"message": "summarise today"}' | jq
```

## Example — WebSocket (Python)

```python
import asyncio
import json
import websockets

async def main() -> None:
    token = "your-token"
    uri = f"ws://127.0.0.1:7777/ws?token={token}"
    async with websockets.connect(uri) as ws:
        async for raw in ws:
            event = json.loads(raw)
            print(event["type"], event["data"])

asyncio.run(main())
```

## Example — WebSocket (JavaScript)

```javascript
const token = await (await fetch("/api/token")).text();
const ws = new WebSocket(`ws://127.0.0.1:7777/ws?token=${token}`);

ws.onmessage = (ev) => {
  const msg = JSON.parse(ev.data);
  console.log(msg.type, msg.data);
};
```

## Request IDs and tracing

Every response carries `X-Request-Id`. When filing a bug report, include this
header together with the relevant lines from `sovyx logs`. The same identifier
is attached to every structured log record produced by the request and to the
OpenTelemetry span, so a single identifier is enough to reconstruct the full
path through the engine.

## Rate limits

The defaults are:

- `GET` endpoints: 120 requests per minute.
- Mutating endpoints (`POST`, `PUT`, `PATCH`, `DELETE`): 30 per minute.
- `/api/chat`: 20 per minute.
- `/api/export`: 5 per minute.
- `/api/import`: 10 per minute.

Limits are per-token and use a 60-second sliding window. Responses include
`X-RateLimit-Limit`, `X-RateLimit-Remaining`, and `X-RateLimit-Reset`.
