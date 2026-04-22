# Module: voice

## What it does

The `sovyx.voice` package is the local-first voice stack: wake word, VAD, speech-to-text, text-to-speech, and an orchestrator that turns them into a conversational loop. It runs ONNX models on the machine (Moonshine v2 for STT via `moonshine-voice`, Piper or Kokoro for TTS, Silero v5 for VAD) and can also speak the Wyoming protocol for Home Assistant integration. An auto-selector probes the hardware at startup and picks a model combination that fits the tier (Pi 5, N100 mini-PC, desktop CPU, desktop GPU, cloud).

## Key components

| Name | Responsibility |
|---|---|
| `VoicePipeline` | State-machine orchestrator — mic frames in, TTS out, with barge-in and filler injection. |
| `SileroVAD` | Voice activity detector running Silero v5 via ONNX Runtime. |
| `WakeWordDetector` | OpenWakeWord with a two-stage check (ONNX score + STT verification). |
| `MoonshineSTT` | Local STT via the `moonshine-voice` library (Moonshine v2 models). |
| `CloudSTT` | Fallback STT against OpenAI Whisper (BYOK). |
| `PiperTTS` | Fast local TTS (VITS ONNX), streaming synthesis. |
| `KokoroTTS` | Higher-quality TTS via `kokoro-onnx`. |
| `SovyxWyomingServer` | TCP server speaking the Wyoming JSONL + PCM protocol. |
| `VoiceModelAutoSelector` | Hardware probe + model selection + fallback chains. |
| `JarvisIllusion` | Filler phrases and confirmation beeps to mask latency. |
| `AudioCapture` / `AudioOutput` | Realtime capture and output via `sounddevice` with LUFS normalization. |

## Pipeline state machine

```mermaid
stateDiagram-v2
    [*] --> IDLE
    IDLE --> WAKE_DETECTED: wake word
    WAKE_DETECTED --> RECORDING: start capture
    RECORDING --> TRANSCRIBING: silence ~700ms or 10s timeout
    TRANSCRIBING --> IDLE: empty transcript
    TRANSCRIBING --> THINKING: text produced
    THINKING --> SPEAKING: first TTS chunk
    SPEAKING --> RECORDING: barge-in (~160ms of speech)
    SPEAKING --> IDLE: TTS finished
```

Frames are 512 int16 samples at 16 kHz (~32 ms). The pipeline does not own audio capture — callers push frames in via `feed_frame()`, which makes it testable without hardware.

## Example

```python
# src/sovyx/voice/pipeline.py — constants and state
_SAMPLE_RATE = 16_000
_FRAME_SAMPLES = 512            # 32 ms at 16 kHz
_SILENCE_FRAMES_END = 22        # ~700 ms silence ends an utterance
_MAX_RECORDING_FRAMES = 312     # ~10 s max per utterance
_BARGE_IN_THRESHOLD_FRAMES = 5  # ~160 ms of sustained speech → barge-in
_FILLER_DELAY_MS = 800          # delay before playing a filler


class VoicePipelineState(IntEnum):
    IDLE = auto()
    WAKE_DETECTED = auto()
    RECORDING = auto()
    TRANSCRIBING = auto()
    THINKING = auto()
    SPEAKING = auto()
```

Wiring a pipeline:

```python
from sovyx.voice.pipeline import VoicePipeline, VoicePipelineConfig
from sovyx.voice.vad import SileroVAD
from sovyx.voice.wake_word import WakeWordDetector
from sovyx.voice.stt import MoonshineSTT
from sovyx.voice.tts_piper import PiperTTS

pipeline = VoicePipeline(
    config=VoicePipelineConfig(mind_id="default"),
    vad=SileroVAD(...),
    wake_word=WakeWordDetector(...),
    stt=MoonshineSTT(...),
    tts=PiperTTS(...),
    event_bus=event_bus,
    on_perception=cog_loop.submit,
)

await pipeline.start()
for frame in mic_frames():          # int16, 512 samples @ 16 kHz
    await pipeline.feed_frame(frame)
```

The cognitive loop calls `pipeline.speak(text)` for one-shot replies or `pipeline.stream_text(chunk)` while LLM tokens arrive so TTS starts at the first sentence boundary.

## Barge-in and fillers

- **Barge-in** — while `SPEAKING`, every incoming frame is still fed to the VAD. If five consecutive speech frames (~160 ms) arrive, the audio queue is interrupted, a `BargeInEvent` is emitted, and the pipeline jumps back to `RECORDING`.
- **Fillers** — when `start_thinking()` is called, a timer is armed. If no LLM token arrives within `filler_delay_ms` (800 ms default), `JarvisIllusion` plays a phrase like "Let me think…" through the output queue. The timer is cancelled as soon as the first token shows up.

## Wyoming protocol

`SovyxWyomingServer` listens on TCP `10700` and announces `_wyoming._tcp.local.` via Zeroconf. This lets Home Assistant Voice Assist use Sovyx directly for STT, TTS, wake word, and intent handling.

```python
# src/sovyx/voice/wyoming.py
_WYOMING_TCP_PORT = 10700
_MIC_RATE = 16_000      # 16 kHz mono PCM input
_MIC_WIDTH = 2          # 16-bit signed LE
_SND_RATE = 22_050      # Piper default output
_OUTPUT_CHUNK_BYTES = 4410
WYOMING_SERVICE_TYPE = "_wyoming._tcp.local."
```

The handshake returns an `info` payload describing the supported services (`asr`, `tts`, `wake`, `intent`) and then routes `transcribe`, `synthesize`, and `detect` events to the corresponding engines.

## Hardware auto-selection

`VoiceModelAutoSelector` reads CPU cores, total RAM, and checks for an NVIDIA GPU via `nvidia-smi`, then picks a tier and a model set.

| Tier | Target hardware | STT | TTS | VAD | Wake |
|---|---|---|---|---|---|
| `PI5` | Raspberry Pi 5, 4–8 GB | Moonshine-tiny | Piper-low | Silero v5 | OpenWakeWord |
| `N100` | Intel N100 mini-PC, 8–16 GB | Moonshine-base | Piper-medium | Silero v5 | OpenWakeWord |
| `DESKTOP_CPU` | Modern x86, no GPU | Moonshine-base | Kokoro v0 | Silero v5 | OpenWakeWord |
| `DESKTOP_GPU` | x86 + NVIDIA GPU (≥ 4 GB VRAM) | Moonshine-large | Kokoro v0 (GPU) | Silero v5 | OpenWakeWord |
| `CLOUD` | Cloud GPU instance | Moonshine-large | Kokoro v1 | Silero v5 | OpenWakeWord |

## Hot-enable from dashboard

Since v0.14.0, voice can be enabled at runtime from the dashboard without restarting the daemon.

### Extras group

Voice dependencies are optional. Install them with:

```bash
pip install sovyx[voice]
# or with uv:
uv pip install sovyx[voice]
```

This pulls in `moonshine-voice`, `piper-tts`, `sounddevice`, and `kokoro-onnx`.

### Voice factory

`sovyx.voice.factory.create_voice_pipeline()` is the async factory that instantiates all five components (SileroVAD, MoonshineSTT, TTS, WakeWord, VoicePipeline). All ONNX model loads are wrapped in `asyncio.to_thread()` to avoid blocking the event loop. SileroVAD (2.3 MB) is auto-downloaded on first use; Moonshine auto-downloads via HuggingFace Hub.

```python
from sovyx.voice.factory import create_voice_pipeline

pipeline = await create_voice_pipeline(
    event_bus=event_bus,
    wake_word_enabled=False,
    mind_id="default",
)
```

### Model registry

`sovyx.voice.model_registry` provides:

- `check_voice_deps()` -- returns `(installed, missing)` package lists
- `detect_tts_engine()` -- returns `"piper"`, `"kokoro"`, or `"none"` (priority order)
- `ensure_silero_vad(model_dir)` -- auto-downloads SileroVAD ONNX if missing, atomic write with cleanup on failure

### Dashboard endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/api/voice/hardware-detect` | GET | CPU, RAM, GPU, audio devices, tier, recommended models |
| `/api/voice/enable` | POST | Check deps, check audio, create pipeline, register, persist config |
| `/api/voice/disable` | POST | Graceful stop, unregister, persist config |

The enable endpoint validates in order: dependencies, TTS engine, audio hardware, idempotency. Each failure returns 400 with a structured error body that the dashboard renders as a specific panel (missing deps with install command, audio hardware warning, etc.).

### Setup wizard UI

The Voice page in the dashboard shows a "Set up Voice" button that opens a modal with:

1. Hardware detection (auto-fetches `/api/voice/hardware-detect`)
2. CPU, RAM, GPU, audio device summary
3. "Enable Voice" button (always visible after detection)
4. Error panels for missing deps (with copy-able install command) or missing audio hardware

Each tier has a fallback chain: if the primary TTS or STT fails to load, the selector walks down to the next lighter model.

## Events

All events are frozen dataclasses emitted on the `EventBus`.

| Event | Emitted when |
|---|---|
| `WakeWordDetectedEvent` | Wake word score crosses the threshold. |
| `SpeechStartedEvent` | Recording starts (after wake word or barge-in). |
| `SpeechEndedEvent` | Silence threshold reached or 10 s timeout hit. |
| `TranscriptionCompletedEvent` | STT returns text — includes confidence and latency. |
| `TTSStartedEvent` / `TTSCompletedEvent` | Playback begins / ends. |
| `BargeInEvent` | User interrupted TTS. |
| `PipelineErrorEvent` | STT or TTS raised an unrecoverable error. |

## Configuration

```yaml
voice:
  pipeline:
    mind_id: default
    wake_word_enabled: true
    barge_in_enabled: true
    fillers_enabled: true
    filler_delay_ms: 800
    silence_frames_end: 22
    max_recording_frames: 312
    confirmation_tone: beep   # or "none"
  wyoming:
    enabled: true
    host: 0.0.0.0
    port: 10700
    zeroconf: true
  stt:
    backend: moonshine
    language: en
  tts:
    backend: piper            # or "kokoro"
    voice: en_US-amy-medium
```

## Windows Voice Clarity / capture APO handling

Since early 2026, Windows Update ships the *Voice Clarity* package
(`VocaEffectPack` / `voiceclarityep`) as a per-endpoint capture APO.
On a significant fraction of hardware the post-APO signal keeps
plausible RMS but Silero v5 never crosses `0.01` speech probability —
the pipeline looks healthy yet silently stays in IDLE.

Sovyx handles this automatically:

1. At startup, `sovyx.voice._apo_detector.detect_capture_apos()` walks
   `HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\
   Capture\*\FxProperties` and classifies each active endpoint. The
   result lands in the structured log event `voice_apo_detected`.
2. The orchestrator tracks consecutive "deaf" heartbeats (VAD peak
   below `_DEAF_VAD_MAX_THRESHOLD`). After
   `tuning.voice.deaf_warnings_before_exclusive_retry` (default **2**)
   consecutive warnings *and* `voice_clarity_active=True` *and*
   `tuning.voice.voice_clarity_autofix=True` (default), Sovyx closes
   the stream and reopens it with `capture_wasapi_exclusive=true` —
   exclusive mode bypasses the entire APO chain. The decision is
   one-shot (latched) to avoid oscillation.
3. If the exclusive open fails (device busy, not granted), capture
   falls back to shared mode so the pipeline stays alive, and the
   dashboard banner guides the user through the manual fix
   ("Voice isolation" toggle in Windows Sound settings).

Operators can disable the autofix and pin exclusive mode permanently:

```bash
# Never auto-bypass; leave mic in shared mode
SOVYX_TUNING__VOICE__VOICE_CLARITY_AUTOFIX=false

# Always open in exclusive mode (no APO, ever)
SOVYX_TUNING__VOICE__CAPTURE_WASAPI_EXCLUSIVE=true

# Trigger earlier (after 1 deaf heartbeat instead of 2)
SOVYX_TUNING__VOICE__DEAF_WARNINGS_BEFORE_EXCLUSIVE_RETRY=1
```

Diagnostics surfaces:

- **CLI**: `sovyx doctor` runs the `voice_capture_apo` check and
  WARNs with the fix command when Voice Clarity is active on any
  endpoint.
- **Dashboard**: `GET /api/voice/capture-diagnostics` returns the
  full per-endpoint APO list + an `active_endpoint` summary +
  `voice_clarity_active` flag. The setup wizard renders a one-click
  "enable exclusive mode" card when the bit is set.

## Linux session-manager contention (VLX-002 / VLX-003)

On modern Linux distributions (Mint 22, Ubuntu 24.04+, Fedora 40+)
PipeWire runs as the default session manager and grabs every hardware
ALSA device (`hw:X,Y`) in shared mode at boot. When the user pins a
bare `hw:X,Y` PCM as the Sovyx capture device — either explicitly via
`mind.yaml::voice.input_device_name` or via the onboarding picker —
PortAudio's exclusive-mode open paths return `-9985 Device
unavailable` because PipeWire already holds the kernel ALSA handle.

### How Sovyx recovers

The cascade does **not** fail closed. `build_capture_candidates`
(`sovyx.voice.health._candidate_builder`) expands the resolved
`DeviceEntry` into an ordered candidate set on Linux:

1. The user-preferred device (rank 0).
2. Canonical-name siblings of the preferred device (rank 1..N) —
   empty on modern Linux where PortAudio only exposes the ALSA host
   API.
3. Session-manager virtuals — `pipewire`, `pulse` PCMs.
4. The `default` / `sysdefault` ALSA alias.
5. Any remaining enumerated input device (catch-all tail).

`run_cascade_for_candidates` iterates the list in order. The first
candidate that produces a HEALTHY probe wins. When `hw:X,Y` is
contended, the cascade transparently falls over to `pipewire` or
`default` — both are shared-mode and resolve cleanly.

### Observability

The dashboard renders the winning candidate's `kind` so the user can
see "running on `pipewire` (fallback from `hw:1,0`)" when the mic is
contested. Events emitted:

- `voice_cascade_probe_call` / `voice_cascade_probe_result` — per-probe
  telemetry across every candidate × combo pair.
- `voice_cascade_winner_selected` — carries `source`, `combo_host_api`,
  `device_index`, `device_friendly_name`.
- `voice_cascade_candidate_set_resolved` — cross-candidate summary with
  `winning_rank`, `winning_source`, `winning_kind`.

### User-facing cure (when every candidate fails)

When every candidate falls with the session-manager contention
pattern, `/api/voice/enable` returns HTTP 503 with
`error: "capture_device_contended"` + a list of
`alternative_devices`. The onboarding UI renders clickable chips so
the user can retry against `pipewire` / `default` with one click
without re-opening settings.

Proactive diagnosis is available via:

- `sovyx doctor linux_session_manager_grab` — probes `pactl list
  source-outputs` + a bounded `/proc/*/fd/*` scan to identify the
  process holding the mic.
- `GET /api/voice/capture-diagnostics` — same report, JSON payload for
  the dashboard.

### Runtime escape

`LinuxSessionManagerEscapeBypass` covers the dynamic case — the
pipeline booted healthy on `hw:X,Y`, then a later event (user opens
Zoom, Bluetooth handset connects) grabs the hardware. The coordinator
invokes the strategy on deaf-heartbeat and the stream reopens
against the preferred session-manager virtual without recreating the
pipeline. Complementary inverse of `LinuxPipeWireDirectBypass`
(opt-in; covers `filter-chain` APO-degraded sources going in the
opposite direction).

### `mind.yaml` invariant

The user's `input_device_name` preference is **never overwritten** by
fallback. A subsequent boot where the preferred device is free will
naturally pick it as rank-0 candidate and win the cascade. This
decouples "what the user configured" from "what's actually capturing
right now" — both are observable, neither corrupts the other.

## Roadmap

- **Speaker recognition** (ECAPA-TDNN) — enrollment, verification, multi-user voice auth.
- **Voice cloning** — few-shot speaker adaptation on top of the existing TTS.
- **Multilingual text detection** — Parakeet TDT for language-agnostic pipelines.
- **Per-chunk output guard** — regex pass per streaming delta (currently output guard runs on the final assembled text only).

## See also

- Source: `src/sovyx/voice/pipeline.py`, `src/sovyx/voice/wyoming.py`, `src/sovyx/voice/stt.py`, `src/sovyx/voice/tts_piper.py`, `src/sovyx/voice/tts_kokoro.py`, `src/sovyx/voice/vad.py`, `src/sovyx/voice/wake_word.py`, `src/sovyx/voice/auto_select.py`, `src/sovyx/voice/jarvis.py`.
- Tests: `tests/unit/voice/`, `tests/integration/voice/`.
- Related modules: [`cognitive`](./engine.md) for the loop that consumes perceptions, [`dashboard`](./dashboard.md) for `/api/voice/status`.
