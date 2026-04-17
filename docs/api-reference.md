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

| Method | Path                              | Description                                                           |
| ------ | --------------------------------- | --------------------------------------------------------------------- |
| GET    | `/api/voice/status`               | Voice pipeline state and device info.                                 |
| GET    | `/api/voice/models`               | Installed STT/TTS/VAD model catalogue.                                |
| GET    | `/api/voice/hardware-detect`      | Probe GPU/CPU and return the recommended voice profile.               |
| POST   | `/api/voice/enable`               | Start the voice pipeline (downloads models on first run).             |
| POST   | `/api/voice/disable`              | Stop the voice pipeline and release audio devices.                    |
| GET    | `/api/voice/test/devices`         | Enumerate audio devices available to the setup wizard.                |
| WS     | `/api/voice/test/input`           | Live RMS/peak/hold meter stream for mic sanity-check.                 |
| POST   | `/api/voice/test/output`          | Queue a TTS playback job on an output device.                         |
| GET    | `/api/voice/test/output/{job_id}` | Poll playback job until `done` or `error`.                            |

See [`voice-device-test`](modules/voice-device-test.md) for the frame protocol,
error taxonomy, rate-limits, and tuning knobs.

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
