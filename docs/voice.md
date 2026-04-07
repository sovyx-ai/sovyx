# Voice Pipeline

!!! note "v0.5 Feature"
    The voice pipeline is available starting in Sovyx v0.5.

Sovyx includes a full voice pipeline: wake word detection, speech-to-text (STT), and text-to-speech (TTS) — all running locally. No cloud required for basic operation.

## Overview

```
Mic → VAD → Wake Word → STT → Cognitive Loop → TTS → Speaker
```

Each stage runs as an independent async component, connected via the voice pipeline orchestrator.

## Hardware-Adaptive Model Selection

Sovyx automatically detects your hardware and selects optimal models:

| Hardware | STT Model | TTS Model | Detection |
|----------|-----------|-----------|-----------|
| **Raspberry Pi 5** | Moonshine Tiny (27M) | Piper (22MB int8) | ARM64, <12GB RAM |
| **N100 / x86 mini-PC** | Moonshine Base (61M) | Kokoro-82M (ONNX q8) | AVX2, ≥12GB RAM |
| **GPU (NVIDIA)** | Parakeet TDT 0.6B | Qwen3-TTS 0.6B | CUDA detected |
| **Cloud fallback** | Deepgram Nova-2 | ElevenLabs | API key configured |

Override auto-detection in `mind.yaml`:

```yaml
voice:
  stt_provider: moonshine_base   # moonshine_tiny | moonshine_base | parakeet | deepgram
  tts_provider: kokoro            # piper | kokoro | qwen3_tts | elevenlabs
  hardware_tier: n100             # pi5 | n100 | gpu | cloud
```

## Voice Activity Detection (VAD)

Silero VAD runs continuously on the audio stream with a 512-sample window at 16kHz. It detects when someone starts and stops speaking, triggering the STT pipeline only when voice is present.

```yaml
voice:
  vad:
    threshold: 0.5        # Detection sensitivity (0.0–1.0)
    min_speech_ms: 250     # Minimum speech duration
    min_silence_ms: 300    # Silence before end-of-speech
```

## Wake Word Detection

Optional wake word gate before STT activation. Uses a lightweight keyword-spotting model.

```yaml
voice:
  wake_word:
    enabled: true
    phrase: "hey sovyx"
    sensitivity: 0.5
```

When disabled, all detected speech is sent directly to STT (push-to-talk or always-on mode).

## Speech-to-Text (STT)

All local STT models use ONNX Runtime for optimized inference at 16kHz sample rate.

### Moonshine (Default for Pi 5 / N100)

Compact, fast models optimized for edge deployment:

- **Moonshine Tiny** (27M params) — Best for Pi 5, ~200ms latency
- **Moonshine Base** (61M params) — Better accuracy, ~150ms on N100

### Parakeet TDT (GPU)

NVIDIA's Parakeet TDT 0.6B — highest accuracy for GPU-equipped systems.

### Deepgram Nova-2 (Cloud)

Cloud fallback with streaming support. Requires API key:

```bash
export SOVYX_DEEPGRAM_API_KEY="..."
```

## Text-to-Speech (TTS)

Output at 22.05kHz for natural-sounding speech.

### Piper (Pi 5)

Ultra-lightweight int8 model (22MB). Fast synthesis, decent quality.

```yaml
voice:
  tts:
    piper_voice: "en_US-lessac-medium"  # See piper-voices for options
```

### Kokoro (N100)

82M parameter ONNX model with expressive, natural speech.

```yaml
voice:
  tts:
    kokoro_voice: "af_sky"     # Voice preset
    kokoro_speed: 1.0          # Playback speed
```

### ElevenLabs (Cloud)

Premium cloud TTS with voice cloning support:

```bash
export SOVYX_ELEVENLABS_API_KEY="..."
```

## Jarvis Illusion

The Jarvis Illusion pipeline creates a seamless conversational feel by overlapping STT → LLM → TTS stages:

1. STT begins streaming partial transcripts
2. LLM starts generating response from partial input
3. TTS begins synthesizing from partial LLM output
4. Audio playback starts before full response is complete

Result: perceived latency drops from seconds to ~500ms.

```yaml
voice:
  jarvis_illusion: true     # Enable pipeline overlap
```

## Wyoming Protocol

Sovyx voice components can integrate with [Wyoming Protocol](https://github.com/rhasspy/wyoming) for Home Assistant compatibility:

```yaml
voice:
  wyoming:
    enabled: true
    port: 10300
```

This exposes Sovyx's STT and TTS as Wyoming services, discoverable by Home Assistant's voice pipeline.

## Audio I/O

```yaml
voice:
  audio:
    input_device: null       # null = system default
    output_device: null
    sample_rate: 16000       # STT input rate
    channels: 1              # Mono
    chunk_size: 512          # Samples per VAD frame
```

List available devices:

```bash
sovyx audio list-devices
```

## Setup

1. Enable voice in `mind.yaml`:

```yaml
voice:
  enabled: true
```

2. Sovyx auto-downloads required models on first start:

```bash
sovyx start  # Downloads ~50-200MB of models depending on tier
```

3. Test with:

```bash
sovyx voice test  # Records 5s, transcribes, speaks response
```
