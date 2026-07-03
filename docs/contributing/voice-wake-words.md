# Voice Wake Words — Contribution Guide

Operator-facing guide for configuring custom wake words and (optionally) contributing trained models back to the community.

## Architecture overview

Sovyx wake-word detection runs in two layers:

1. **ONNX detector** (`src/sovyx/voice/wake_word.py`) — OpenWakeWord 3-layer ONNX pipeline (mel-spectrogram → feature embedding → wake-word classifier). Sub-100 ms detection latency on CPU once a model exists for the mind's wake word. Resolves the model via `PretrainedModelRegistry` looking in `<data_dir>/wake_word_models/pretrained/<wake_word>.onnx` (default `data_dir` is `~/.sovyx/`).
2. **STT fallback** (`src/sovyx/voice/_wake_word_stt_fallback.py`) — when no ONNX model exists for the configured wake word, raw capture audio is routed through Moonshine STT and the transcription is matched against `MindConfig.wake_word_variants`. Latency ~500 ms vs ~80 ms for ONNX. The system swaps to ONNX automatically when a model is registered (`wake_word.register_mind` RPC).

A phonetic variant table (`src/sovyx/voice/_wake_word_variants.py`) augments matching with hand-curated mishears keyed by BCP-47 language prefix — e.g. `Lúcia` (pt) accepts `lousha`, `luchia`; `Müller` (de) accepts `mueller`, `miller`. The table is small + extensible by PR.

## Default state

Out-of-the-box, the only fully-supported wake word is **"Sovyx"** — and even then, the daemon downloads the OpenWakeWord ONNX model on first run. Sovyx ships **zero** pre-bundled wake-word ONNX models. Every other wake word — operator's mind named "Jonny", "Lúcia", "Athena", etc. — requires either:

- the **STT-fallback path** (no model needed; ~500 ms latency), OR
- a **custom-trained ONNX model** placed in `<data_dir>/wake_word_models/pretrained/<name>.onnx`.

Both paths are operator-friendly. The choice is latency-vs-friction.

## Custom wake word — option 1: phonetic + STT fallback (no training)

Lowest friction. No model training, no GPU, no audio collection. Latency is STT-bound (~500 ms–2 s depending on STT engine).

**Steps:**

1. Edit `~/.sovyx/<mind_id>/mind.yaml` (i.e. `<data_dir>/<mind_id>/mind.yaml`) and set:

    ```yaml
    wake_word: "Lúcia"
    wake_word_enabled: true
    # Optional — extend mishear list:
    wake_word_variants:
      - "lucia"
      - "lousha"
      - "loocha"
    ```

2. Restart the daemon (`sovyx stop && sovyx start`) or hot-reload via the dashboard's mind-config save flow.

3. Speak the phrase. The capture pipeline routes audio through STT + matches the transcription against `wake_word_variants` (auto-derived if you don't set them: original + ASCII-fold + `hey <wake>` matrix, plus any matching language-prefix entries from the mishear table).

**Pros:** zero training cost; works for any phrase the STT engine can transcribe; immune to model-content licensing issues.

**Cons:** higher latency (STT must transcribe the full window); STT engine quality bounds detection quality; CPU cost is paid on every audio chunk (vs ONNX which is 80 ms per 1.28 s frame).

## Custom wake word — option 2: train an OpenWakeWord ONNX model

Sub-second detection, lowest CPU. Requires audio samples + a one-time training run.

### Sovyx-native training (CLI)

The `sovyx voice train-wake-word` command wraps the Sovyx-internal trainer (`KokoroSampleSynthesizer` + `TrainingOrchestrator`). It generates synthetic positive samples via Kokoro TTS and trains an ONNX model end-to-end:

```bash
sovyx voice train-wake-word "Lúcia" --mind-id <id>
```

The default output path is `<data_dir>/wake_word_models/pretrained/<job_id>.onnx`. After completion, the CLI calls the `wake_word.register_mind` RPC so the running daemon hot-loads the model with no restart. Cancel with Ctrl+C (writes a `.cancel` file; orchestrator polls and exits clean).

Training UX details: `docs/modules/voice.md` (training surface) + `dashboard/src/types/api.ts` (REST endpoints `GET /api/voice/training/jobs`).

### Manual training (OpenWakeWord notebook)

For control over training data (real recordings instead of TTS, custom negative pool, etc.) use OpenWakeWord upstream. Repository: <https://github.com/dscripka/openWakeWord>.

**Verified at time of writing (2026-05-08):** OpenWakeWord v0.6.0 (Feb 2024). Code license **Apache 2.0**; pre-trained models license **CC BY-NC-SA 4.0** (non-commercial — review your deployment context).

Two training entry points are documented upstream:

- **Google Colab notebook** — simplified UI; trains a model in under one hour from a wake phrase. Linked from the OpenWakeWord README.
- **`notebooks/automatic_model_training.ipynb`** — local, more configurable; recommended for production-quality models.

Rough sample budget per OpenWakeWord docs:

- ~1000 positive samples (TTS-augmented or real recordings).
- ~10 000 negative samples (pulled from a generic speech corpus the upstream notebook references).

Training output: a single `<name>.onnx` file. Place it at:

```
<data_dir>/wake_word_models/pretrained/<name>.onnx
```

where `<data_dir>` defaults to `~/.sovyx/`. Filename stem matches the configured wake word case-insensitively + ASCII-folded — `lucia.onnx` matches `wake_word: "Lúcia"`. Then either restart the daemon (`sovyx stop && sovyx start`) or use the dashboard's per-mind wake-word panel, which hot-loads the model into the running daemon. (Both paths ultimately drive the internal `wake_word.register_mind` RPC — there is no dedicated CLI subcommand for it; `sovyx voice train-wake-word` invokes the RPC automatically on success.)

**Why no `wake_word_model_path` field:** Sovyx resolves models by wake-word name through `PretrainedModelRegistry` rather than an explicit path field on `MindConfig`. This keeps the config surface small and lets one ONNX file (e.g. `lucia.onnx`) cover every mind that uses that wake word.

## Community contribution path

The Sovyx pretrained pool (mission task **T8.11**) is currently **DEFERRED** — see `docs-internal/missions/MISSION-voice-final-skype-grade-2026.md` "Phase 8 — Deferred items" for blockers (name list, training audio source, storage strategy). No `sovyx-wake-words/` community repo exists yet.

If you have trained a high-quality model and want to contribute:

1. **For now:** open a GitHub issue at <https://github.com/sovyx-ai/sovyx> describing the wake word, training corpus, and FRR/FAR measurements. Maintainers will evaluate inclusion against the deferral blockers above.
2. **Future state:** once T8.11 unblocks, contributions will route through a dedicated `sovyx-wake-words` community repo with a signed-mirror distribution model (analogous to the Mixer KB profile signing flow at `docs/contributing/voice-kb-rotation.md`).

## License + attribution

Operators are responsible for clearing the license of any audio used to train a wake-word model:

- **TTS-generated samples** (Kokoro / Piper paths) — license is the TTS engine's. Kokoro and Piper are permissively licensed at the time of writing; verify the version your daemon ships.
- **Real human recordings** — collect under written consent + retention policy aligned with `docs/compliance.md`. GDPR + LGPD treat voice biometrics as sensitive personal data.
- **Negative-pool corpora** — most public ASR corpora (LibriSpeech, Common Voice) carry attribution requirements. Read the upstream license before redistributing a model trained against them.

OpenWakeWord upstream uses Apache 2.0 for code and CC BY-NC-SA 4.0 for shipped pretrained models. **Models you train yourself are yours to license** — but if you base your training pipeline on an OpenWakeWord notebook, the Apache 2.0 attribution carries through.

## Quality gates — recommended thresholds before deploying a custom model

Before shipping a custom wake-word model to production minds:

| Metric | Target | Why |
|--------|--------|-----|
| **FRR (false reject rate)** | ≤ 5% on a held-out positive set | Below this, operators stop trusting the wake word and switch to `wake_word_enabled: false`. |
| **FAR (false accept rate)** | ≤ 1 false-fire per 10 hours of background speech | Phase 7 (`T7.7`) added `voice.wake_word.false_fire_count` telemetry — measure against your noise corpus. |
| **Stage-2 STT verification confidence** | ≥ 0.7 on positive samples | The ONNX detector is stage 1; STT verification (stage 2) gates the dispatch. Tune `_DEFAULT_STAGE2_THRESHOLD` only with telemetry justifying the change. |
| **Latency p95** | ≤ 500 ms wake-event-to-pipeline-dispatch | Industry parity (Alexa/Google/Siri) per Phase 7 mission contract. |

Validate against Sovyx's own telemetry namespace: `voice.wake_word.{confidence, false_fire_count, latency_ms, detection_method}` (Phase 7) + `voice.wake_word.{detection_method, mind_id}` (Phase 8). Dashboard surfaces these in Settings → Voice → Wake-word panel.

## See also

- `docs/getting-started.md` — first-run quick start.
- `docs/modules/voice.md` — voice subsystem reference.
- `docs/contributing/voice-kb-rotation.md` — analogous signing flow for Mixer KB profiles (the pattern future `sovyx-wake-words/` will follow).
- `docs-internal/missions/MISSION-voice-final-skype-grade-2026.md` §Phase 8 — full Phase 8 task table including deferred items.
