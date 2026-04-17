# Module: voice / device test

## What it does

The voice device-test subsystem powers the setup-wizard affordances that
let a user verify their microphone and speakers **without** booting the
full voice pipeline. It exposes:

- A WebSocket that streams live RMS / peak / hold meter frames from a
  chosen input device, so the wizard can render a 60 FPS VU meter while
  the user is talking.
- A one-shot HTTP job that plays a short localised phrase through a
  chosen output device, so the user can confirm speakers work and hear
  the TTS voice they will get.

It is strictly an opt-in diagnostic path — nothing runs until the user
clicks **Test microphone** or **Test speakers** in the wizard. The live
voice pipeline and the device-test subsystem are mutually exclusive:
starting either one while the other is running returns a machine-readable
`pipeline_active` error.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/voice/test/devices` | Enumerate audio devices (thin wrapper over hardware-detect, scoped to the test surface). |
| `WS`  | `/api/voice/test/input?token=…&device_id=…&sample_rate=…` | Live input meter stream. |
| `POST` | `/api/voice/test/output` | Queue a TTS playback job; returns `{ job_id, status: "queued" }`. |
| `GET`  | `/api/voice/test/output/{job_id}` | Poll the job until `status` is `done` or `error`. |

Auth for the WebSocket is by `?token=` query param (the browser WS API
does not let you set headers). The server validates the token the same
way it does for REST.

## Frame protocol (v1)

All WebSocket payloads carry a version + discriminator envelope:

```json
{ "v": 1, "t": "ready",   "device_id": 2, "device_name": "MyMic",
  "sample_rate": 16000, "channels": 1 }

{ "v": 1, "t": "level",   "rms_db": -24.5, "peak_db": -18.0,
  "hold_db": -18.0, "clipping": false, "vad_trigger": true }

{ "v": 1, "t": "error",   "code": "device_busy",
  "detail": "held by another app", "retryable": false }

{ "v": 1, "t": "closed",  "reason": "client_disconnect" }
```

`rms_db` / `peak_db` / `hold_db` are clamped to `[-120, 0] dBFS`.
`hold_db` uses a peak-hold ballistic: it latches the last peak for a
configurable window (default 1 000 ms) then decays at 10 dB/s.

### Error taxonomy

| `code` | Meaning |
|---|---|
| `disabled` | The device-test subsystem is disabled in config. |
| `rate_limited` | Per-token rate limit exceeded (default: 1 test/sec, burst 3). |
| `unauthorized` | Invalid / missing token. |
| `pipeline_active` | The live voice pipeline is running — cannot run a test concurrently. |
| `tts_unavailable` | No TTS engine configured or the model is missing. |
| `device_not_found` | The requested PortAudio index does not exist. |
| `device_busy` | The device is held by another process. |
| `permission_denied` | OS denied access (typically macOS microphone permission). |
| `invalid_request` | Payload rejected by Pydantic validation. |
| `internal_error` | Anything else — surface `detail` to the user. |

All terminal failures on the WebSocket map to **application close codes**
in the `4xxx` range (e.g. `4003 disabled`, `4029 rate_limited`,
`4012 pipeline_active`). Transient drops use the standard `1006`, which
the client retries with exponential backoff up to a 3-attempt budget.

## Backend layout

```
src/sovyx/voice/device_test/
├── __init__.py           # Public surface re-exports
├── _models.py            # Pydantic v2 frames + envelope (v=1, t=…)
├── _session.py           # TestSession: orchestrates source → meter → sink
├── _meter.py             # PeakHoldMeter (ballistics + clipping detector)
├── _limiter.py           # Per-token token-bucket rate limiter
├── _source.py            # AudioSource Protocol + Sounddevice + Fake impls
├── _sink.py              # AudioSink Protocol + Sounddevice + Fake impls
└── _metrics.py           # OTel counters + histograms
```

Tests mirror this layout under `tests/unit/voice/device_test/`. Cross-
endpoint integration coverage lives in
`tests/dashboard/test_voice_test_routes.py`, and property-based invariants
for the meter are in `tests/property/test_voice_meter_properties.py`.

The router that wires it all into FastAPI is
`src/sovyx/dashboard/routes/voice.py` (search for `@router.websocket`).
Dependencies (rate limiter, TTS factory, audio sink) are injected via
`app.state.voice_test_*` so tests can substitute fakes without touching
PortAudio.

## Frontend layout

```
dashboard/src/hooks/use-audio-level-stream.ts          # WS hook + state machine
dashboard/src/hooks/use-audio-level-stream.test.ts
dashboard/src/components/setup-wizard/AudioLevelMeter.tsx   # Canvas 60 Hz meter
dashboard/src/components/setup-wizard/AudioLevelMeter.test.tsx
dashboard/src/components/setup-wizard/TtsTestButton.tsx     # Playback test button
dashboard/src/components/setup-wizard/TtsTestButton.test.tsx
dashboard/src/types/api.ts          # VoiceTest* compile-time types
dashboard/src/types/schemas.ts      # VoiceTest* zod runtime schemas
```

`HardwareDetection.tsx` hosts both affordances under the matching
device dropdown:

- Under **Input** → "Test microphone" button → mounts
  `useAudioLevelStream` (which opens the WebSocket) and renders
  `AudioLevelMeter` live. A "Stop test" button tears the stream down.
- Under **Output** → `TtsTestButton`, which POSTs the playback job and
  polls until it is `done` or `error`.

Both components use `api.*` (never raw `fetch`) and validate responses
through the zod schemas so backend contract drift surfaces as a visible
warning in dev tools rather than silent mis-render.

## Tuning knobs

All thresholds live on `EngineConfig.tuning.voice` (flat fields prefixed
`device_test_*`) and can be overridden via
`SOVYX_TUNING__VOICE__DEVICE_TEST_*` env vars:

| Field | Default | Meaning |
|---|---|---|
| `device_test_enabled` | `true` | Master switch. When `false`, every endpoint returns `disabled`. |
| `device_test_frame_rate_hz` | `30` | Meter frames per second emitted to the WebSocket. |
| `device_test_peak_hold_ms` | `1500` | Peak-hold latch window before decay starts. |
| `device_test_peak_decay_db_per_sec` | `20.0` | Decay rate once the hold window expires. |
| `device_test_vad_trigger_db` | `-30.0` | dBFS marker drawn on the meter as the VAD threshold. |
| `device_test_clipping_db` | `-0.3` | dBFS threshold above which the frame is flagged as clipping. |
| `device_test_reconnect_limit_per_min` | `10` | Per-token reconnect budget for the meter WebSocket. |
| `device_test_max_sessions_per_token` | `1` | Concurrent meter sessions per auth token (1 = singleton). |
| `device_test_max_phrase_chars` | `200` | Cap on the TTS test phrase length. |
| `device_test_output_job_ttl_seconds` | `60` | How long finished playback jobs remain pollable. |

## Observability

OpenTelemetry instruments the subsystem via `_metrics.py`:

- `sovyx.voice.test.sessions_total{result=…}` — counter for started
  sessions by outcome (`ok`, `error`, `rate_limited`, …).
- `sovyx.voice.test.output_jobs_total{result=…}` — counter for playback
  jobs.
- `sovyx.voice.test.output_duration_s` — histogram of playback wall-clock.
- `sovyx.voice.test.meter_frames_total` — counter of meter frames emitted
  (lets Grafana show a live FPS and spot stalls).

Logs use the structured logger (`logger = get_logger(__name__)`) and
include `session_id`, `device_id`, `code`, and `retryable` fields on
failures.

## Security notes

- Per-token rate limiter prevents a malicious client from spamming the
  subsystem. The bucket is scoped by the auth token, so a compromised
  browser session can't starve other clients.
- The WebSocket refuses to start while the live voice pipeline is
  running and vice-versa (`pipeline_active`), so the PortAudio stream
  is never opened twice.
- Playback text is a server-side catalogue keyed by `phrase_key`; the
  client only picks a key and a language — it cannot inject arbitrary
  TTS text through this endpoint.
