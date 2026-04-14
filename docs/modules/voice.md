# Módulo: voice

## Objetivo

`sovyx.voice` implementa o stack completo de interação por voz — do
wake word até TTS — como um pipeline state-machine local-first. Integra
com Home Assistant via protocolo Wyoming (JSONL + PCM sobre TCP),
expondo STT / TTS / Wake Word / intent handling como serviços
discoveráveis por Zeroconf. Roda ONNX Runtime para modelos leves
(Moonshine v1.0 STT, Piper/Kokoro TTS, SileroVAD v5) com auto-seleção
por tier de hardware (Pi5 → N100 → Desktop CPU → Desktop GPU → Cloud).

## Responsabilidades

- Orquestrar o ciclo de conversação por voz via `VoicePipeline`:
  IDLE → WAKE_DETECTED → RECORDING → TRANSCRIBING → THINKING → SPEAKING
  → IDLE.
- Implementar **barge-in**: usuário fala enquanto TTS toca → interrompe
  e volta a gravar (threshold ~160ms de fala sustentada).
- Injetar **fillers estilo Jarvis** se LLM demora >800ms para começar
  a responder (reduz silêncio percebido).
- Servir protocolo Wyoming para HA: `_wyoming._tcp.local.` anunciado
  via Zeroconf, JSONL events + PCM binary sobre TCP:10700.
- Detectar wake word, rodar VAD sobre frames (512 samples @ 16kHz,
  ~32ms), transcrever com Moonshine, sintetizar com Piper/Kokoro.
- Auto-detectar hardware no boot e selecionar a combinação ótima de
  modelos, com fallback chains para graceful degradation.
- Emitir eventos no `EventBus`: `WakeWordDetectedEvent`,
  `SpeechStartedEvent`, `SpeechEndedEvent`, `TranscriptionCompletedEvent`,
  `TTSStartedEvent`, `TTSCompletedEvent`, `BargeInEvent`,
  `PipelineErrorEvent`.

## Arquitetura

`VoicePipeline` (state machine em `IntEnum`) consome stream de frames
PCM (int16) do microfone e despacha em 6 estados. Transições principais:

- IDLE → WAKE_DETECTED: `WakeWordDetector` dispara.
- WAKE_DETECTED → RECORDING: começa a gravar.
- RECORDING → TRANSCRIBING: silêncio por 22 frames (~700ms) ou
  `_MAX_RECORDING_FRAMES=312` (~10s).
- TRANSCRIBING → THINKING: STT terminou e texto não-vazio (vazio → IDLE).
- THINKING → SPEAKING: filler pode ser tocado se LLM atrasa; tokens
  do LLM alimentam TTS streaming (min 3 palavras).
- SPEAKING → RECORDING: barge-in (usuário fala por 5 frames).
- SPEAKING → IDLE: TTS termina naturalmente.

`WyomingServer` (wyoming.py) implementa o protocolo: handshake inicial
com `info`, roteia requests de STT (`transcribe` event → audio chunks
→ `transcript` response), TTS (`synthesize` → PCM stream), Wake Word
(frame-by-frame detection), Intent (via CogLoop).

`VoiceModelAutoSelection` probes hardware (CPU cores, RAM,
`nvidia-smi`), classifica em `HardwareTier`, escolhe modelos:

| Tier | STT | TTS | VAD | Wake |
|------|-----|-----|-----|------|
| PI5 | Moonshine-tiny | Piper-low | SileroV5 | custom ONNX |
| N100 | Moonshine-base | Piper-medium | SileroV5 | custom ONNX |
| DESKTOP_CPU | Moonshine-base | Kokoro-v0 | SileroV5 | custom ONNX |
| DESKTOP_GPU | Moonshine-large | Kokoro-v0 (GPU) | SileroV5 | custom ONNX |
| CLOUD | Moonshine-large | Kokoro-v1 | SileroV5 | custom ONNX |

## Código real

```python
# src/sovyx/voice/pipeline.py:38-44 — constantes audio
_SAMPLE_RATE = 16_000
_FRAME_SAMPLES = 512  # 32ms at 16kHz
_SILENCE_FRAMES_END = 22  # ~700ms silence → end of utterance
_MAX_RECORDING_FRAMES = 312  # ~10s max recording
_BARGE_IN_THRESHOLD_FRAMES = 5  # ~160ms sustained speech → barge-in
_FILLER_DELAY_MS = 800  # Play filler if no LLM token within this
_TEXT_MIN_WORDS = 3  # Min words before TTS synthesis
```

```python
# src/sovyx/voice/pipeline.py:52-67 — state machine
class VoicePipelineState(IntEnum):
    """Transitions (SPE-010 §13):
    IDLE → WAKE_DETECTED → RECORDING → TRANSCRIBING → THINKING → SPEAKING → IDLE
    Barge-in: SPEAKING → RECORDING (skip wake word — already engaged).
    Timeout:  RECORDING → IDLE (10s max).
    Empty:    TRANSCRIBING → IDLE (empty transcription).
    """
    IDLE = auto()
    WAKE_DETECTED = auto()
    RECORDING = auto()
    TRANSCRIBING = auto()
    THINKING = auto()
    SPEAKING = auto()
```

```python
# src/sovyx/voice/wyoming.py:31-45 — constantes do protocolo
_WYOMING_TCP_PORT = 10700
_MIC_RATE = 16_000   # 16 kHz mono PCM input
_MIC_WIDTH = 2       # 16-bit signed LE
_SND_RATE = 22_050   # Piper default output
_INPUT_CHUNK_MS = 20
_INPUT_CHUNK_BYTES = 640
_OUTPUT_CHUNK_MS = 100
_OUTPUT_CHUNK_BYTES = 4410

SOVYX_ATTRIBUTION = {"name": "Sovyx", "url": "https://sovyx.dev"}
WYOMING_SERVICE_TYPE = "_wyoming._tcp.local."
```

```python
# src/sovyx/voice/auto_select.py:41-49 — hardware tiers
class HardwareTier(IntEnum):
    PI5 = auto()         # BCM2712, Cortex A76, 4-8GB
    N100 = auto()        # Intel Alder Lake-N, 8-16GB
    DESKTOP_CPU = auto() # Modern x86, no GPU
    DESKTOP_GPU = auto() # x86 + NVIDIA GPU
    CLOUD = auto()       # Cloud instance with GPU
```

## Specs-fonte

- `SOVYX-BKD-SPE-010-VOICE.md` — VoicePipeline, state machine,
  barge-in, fillers, Wyoming integration.
- `SOVYX-BKD-IMPL-004-VOICE-ONNX.md` — Moonshine v1.0 API,
  Piper pipeline, Kokoro TTS, SileroVAD v5.
- `SOVYX-BKD-IMPL-005-SPEAKER-RECOGNITION.md` — ECAPA-TDNN, enrollment,
  verification.
- `SOVYX-BKD-IMPL-SUP-002-VOICE-CLONING.md` — speaker adaptation.
- `SOVYX-BKD-IMPL-SUP-003-WYOMING-PROTOCOL.md` — Wyoming JSONL+PCM,
  events, Zeroconf.
- `SOVYX-BKD-IMPL-SUP-004-PARAKEET.md` — Parakeet TDT, text detection.
- `SOVYX-BKD-IMPL-SUP-005-AUTO-SELECTION.md` — hardware tier detection.

## Status de implementação

### ✅ Implementado

- **WyomingServer** (`wyoming.py`): handshake `info`, roteamento de
  transcribe/synthesize/detect, Zeroconf announce, TCP:10700.
- **VoicePipeline** (`pipeline.py`, 6 estados): orchestration completa
  com barge-in e filler injection.
- **Moonshine STT** (`stt.py`): ONNX Runtime, Moonshine v1.0 API.
  Fallback cloud STT em `stt_cloud.py`.
- **Piper TTS** (`tts_piper.py`): streaming synthesis, `AudioChunk`,
  voice selection por mind.
- **Kokoro TTS** (`tts_kokoro.py`): alternativa high-quality.
- **SileroVAD v5** (`vad.py`): `SileroVAD`, `VADEvent`.
- **Wake word** (`wake_word.py`): `WakeWordDetector` ONNX-based.
- **Barge-in**: pipeline transita SPEAKING → RECORDING em 5 frames de
  fala sustentada.
- **Jarvis filler** (`jarvis.py`): injeção de "hmm, let me think..."
  após 800ms sem token.
- **Hardware auto-select** (`auto_select.py`, Tier 1-4 + CLOUD):
  probe CPU/RAM/GPU, tabela de modelos por tier, fallback chains.
- **Audio utils** (`audio.py`): conversões int16 ↔ float32, resampling,
  framing.
- **Eventos Wyoming** publicados no EventBus com `mind_id` + metadados.

### ❌ [NOT IMPLEMENTED]

- **Speaker Recognition** (IMPL-005): ECAPA-TDNN biometrics, enrollment,
  verification. **Zero arquivos** — não existe `speaker_recognition.py`.
  Crítico para voice auth; multi-user voice desabilitado na prática.
- **Voice Cloning** (IMPL-SUP-002): speaker adaptation com few-shot
  samples. Sem implementação.
- **Parakeet TDT** (IMPL-SUP-004): text detection em áudio (multilingual),
  não implementado. Pipeline hoje é monolingual (inglês default,
  config por mind).

### ⚠️ Parcial

- Integração streaming LLM → TTS depende de `LLMRouter.stream()` que
  não está exposto (ver gaps do módulo `llm`). Hoje TTS começa só
  quando LLM termina, perdendo o benefício do filler em queries longas.

## Divergências [DIVERGENCE]

- Nenhuma divergência significativa contra SPE-010 para o stack STT/TTS
  implementado. Os gaps acima são features ausentes, não divergências.

## Dependências

- **Externas**: `onnxruntime`, `numpy`, `zeroconf`, `sounddevice`
  (mic/speaker), `wyoming` (opcional), `httpx` (cloud STT fallback).
- **Internas**: `sovyx.engine.events.EventBus`,
  `sovyx.observability.logging`, `sovyx.cognitive.gate.CogLoopGate`
  (intent handling via Wyoming → cognitive loop).

## Testes

- `tests/unit/voice/test_wyoming.py` — protocolo handshake, roteamento,
  Zeroconf announce.
- `tests/unit/voice/test_pipeline.py` — transições de state machine,
  barge-in, filler timeout, recording timeout.
- `tests/unit/voice/test_stt.py`, `test_tts_piper.py`, `test_tts_kokoro.py`,
  `test_vad.py`, `test_wake_word.py` — cada engine isoladamente.
- `tests/unit/voice/test_auto_select.py` — detecção por tier, fallback
  chain quando GPU ausente.
- `tests/integration/voice/` — pipeline end-to-end com audio sintético
  (tones + silêncio) e Wyoming client mock.
- Fixtures: `tmp_path` para ONNX models, `AsyncMock` para LLM stream.

## Referências

- Code: `src/sovyx/voice/pipeline.py`, `src/sovyx/voice/wyoming.py`,
  `src/sovyx/voice/stt.py`, `src/sovyx/voice/stt_cloud.py`,
  `src/sovyx/voice/tts_piper.py`, `src/sovyx/voice/tts_kokoro.py`,
  `src/sovyx/voice/vad.py`, `src/sovyx/voice/wake_word.py`,
  `src/sovyx/voice/audio.py`, `src/sovyx/voice/auto_select.py`,
  `src/sovyx/voice/jarvis.py`.
- Specs: `SOVYX-BKD-SPE-010-VOICE.md`, `SOVYX-BKD-IMPL-004-VOICE-ONNX.md`,
  `SOVYX-BKD-IMPL-005-SPEAKER-RECOGNITION.md`,
  `SOVYX-BKD-IMPL-SUP-002-VOICE-CLONING.md`,
  `SOVYX-BKD-IMPL-SUP-003-WYOMING-PROTOCOL.md`,
  `SOVYX-BKD-IMPL-SUP-004-PARAKEET.md`,
  `SOVYX-BKD-IMPL-SUP-005-AUTO-SELECTION.md`.
- Gap analysis: `docs/_meta/gap-inputs/analysis-B-services.md` §voice.
