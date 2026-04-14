# Gap Analysis — Services (llm, voice, persistence, observability, plugins)

## Module: llm
### Docs-fonte principais
- SOVYX-BKD-SPE-007-LLM-ROUTER.md (1062 linhas) — routing, complexity classification, fallback chain
- SOVYX-BKD-VR-085-CLOUD-LLM-PROXY.md — cloud proxy, multi-model, cost optimization

### Código real
- 11 arquivos, 2378 LOC total
- router.py — ComplexityLevel, ComplexitySignals, classify_complexity()
- providers: anthropic.py, google.py, ollama.py, openai.py

### Planejado vs Implementado
IMPLEMENTED: Complexity-based routing (SIMPLE/MODERATE/COMPLEX tiers)
IMPLEMENTED: Provider abstraction (AnthropicProvider, GoogleProvider, etc)
IMPLEMENTED: Circuit breaker, cost tracking, multi-model tiering
PARTIAL: Stream vs complete() methods integration with CogLoop unclear

### Gaps críticos
NOT IMPLEMENTED: Streaming response for speculative TTS
NOT IMPLEMENTED: BYOK token isolation per user API key

---

## Module: voice
### Docs-fonte principais
- SOVYX-BKD-IMPL-004-VOICE-ONNX.md — Moonshine v1.0 API, Piper pipeline, Kokoro
- SOVYX-BKD-IMPL-005-SPEAKER-RECOGNITION.md — ECAPA-TDNN, enrollment, verification
- SOVYX-BKD-IMPL-SUP-002-VOICE-CLONING.md — voice cloning, speaker adaptation
- SOVYX-BKD-IMPL-SUP-003-WYOMING-PROTOCOL.md — Wyoming JSONL+PCM, events

### Código real
- 12 arquivos, 6019 LOC total
- wyoming.py — WyomingServer, protocol events, service discovery
- pipeline.py — VoicePipeline state machine (IDLE→WAKE→RECORDING→THINKING→SPEAKING)
- stt.py, tts_piper.py, tts_kokoro.py, vad.py, wake_word.py, audio.py

### Planejado vs Implementado
IMPLEMENTED: Wyoming protocol (events, wire format, Zeroconf)
IMPLEMENTED: Moonshine STT, Piper TTS, Kokoro TTS
IMPLEMENTED: SileroVAD v5, barge-in, Jarvis filler
IMPLEMENTED: Hardware auto-select (Tier 1-4)

### Gaps críticos
NOT IMPLEMENTED: Speaker Recognition (IMPL-005) — ZERO files (no speaker_recognition.py)
NOT IMPLEMENTED: Voice Cloning (IMPL-SUP-002) — no speaker adaptation
NOT IMPLEMENTED: Parakeet TDT (IMPL-SUP-004) — no text detection, monolingual

---

## Module: persistence
### Docs-fonte principais
- SOVYX-BKD-ADR-004-DATABASE-STACK.md — SQLite WAL, sqlite-vec, pragmas (9 non-negotiable)
- SOVYX-BKD-SPE-005-PERSISTENCE-LAYER.md — transactions, migrations

### Código real
- 9 arquivos, 1218 LOC total
- pool.py — DatabasePool (1 writer + N readers, WAL, extensions)
- migrations.py, manager.py, schemas (brain, conversations, system)

### Planejado vs Implementado
IMPLEMENTED: WAL mode, sqlite-vec extension loading
IMPLEMENTED: 1 writer + N readers concurrency
IMPLEMENTED: Database-per-Mind isolation, migrations
PARTIAL: sqlite-vec queries not visible in code read

### Gaps críticos
PARTIAL: Vector search implementation (extension loads but no queries)
PARTIAL: Redis caching layer (mentioned in ADR-004 but no code)

---

## Module: observability
### Docs-fonte principais
- SOVYX-BKD-IMPL-015-OBSERVABILITY.md — BatchSpanProcessor, gen_ai conventions, SLO burn rate
- SOVYX-BKD-SPE-026-OBSERVABILITY-METRICS.md — 30+ metrics, Prometheus naming

### Código real
- 8 arquivos, 3221 LOC total
- alerts.py, health.py (10 checks), slo.py (burn rate), metrics.py, logging.py, tracing.py

### Planejado vs Implementado
IMPLEMENTED: OpenTelemetry traces, structlog, SLO burn rate
IMPLEMENTED: Health checks (10 covering all subsystems)
IMPLEMENTED: AlertManager, Prometheus exporter
PARTIAL: gen_ai semantic conventions (may use custom attributes)

### Gaps críticos
None critical

---

## Module: plugins (HEAVIEST: 9860 LOC + 32 docs)
### Docs-fonte principais
- SOVYX-BKD-IMPL-012-PLUGIN-SANDBOX.md — 7-layer sandbox, seccomp, escape vectors (18 mapped)
- SOVYX-BKD-SPE-008*.md (12 variants) — SDK, registry, review CI, governance

### Código real
- 19 arquivos, 9860 LOC total
- manager.py — PluginManager, state tracking, health
- security.py — AST scanner, ImportGuard runtime hook
- sandbox_fs.py, sandbox_http.py — FS/HTTP sandboxing
- lifecycle.py, manifest.py, permissions.py, sdk.py, context.py
- official/: calculator, financial_math, knowledge, weather, web_intelligence

### Planejado vs Implementado
IMPLEMENTED: AST scanner (BLOCKED_IMPORTS, BLOCKED_CALLS, BLOCKED_ATTRIBUTES)
IMPLEMENTED: ImportGuard runtime hook (catches __import__() at runtime)
IMPLEMENTED: Sandboxed FS (50MB/file, 500MB total, symlink checks)
IMPLEMENTED: Sandboxed HTTP (rate limiting)
IMPLEMENTED: Permission enforcer (18 types: file_read, http_get, brain_read, etc)
IMPLEMENTED: Plugin state machine, health monitoring, SDK (@tool decorator)

### Gaps críticos
NOT IMPLEMENTED (v2 only): seccomp-BPF syscall filters (Linux)
NOT IMPLEMENTED (v2 only): Namespace isolation (mount, PID, user)
NOT IMPLEMENTED (v2 only): macOS Seatbelt profiles
NOT IMPLEMENTED: Subprocess IPC protocol (v2 feature)
PARTIAL: Zero-downtime plugin update/rollback

---

## Summary

Gaps por módulo:
| Módulo | Not Impl | Partial |
|--------|----------|---------|
| llm | 2 | 0 |
| voice | 3 | 0 |
| persistence | 0 | 1 |
| observability | 0 | 1 |
| plugins | 4 | 1 |

### Top 3 Insights

1. **Voice: Production for Wyoming/TTS, MISSING speaker recognition** — IMPL-004 (TTS/STT) fully done. IMPL-005 (ECAPA-TDNN biometrics) completely absent. Critical for voice auth.

2. **Plugins: In-process v1 complete; v2 kernel isolation deferred** — Layers 0-4 (AST/ImportGuard/permissions/FS/HTTP) working. Layers 5-7 (seccomp/namespaces/macOS) are future (v2).

3. **LLM router: Complexity dispatch ready; streaming integration unclear** — SPE-007 routing implemented. Speculative TTS streaming may not be exposed.

### Files analyzed
- 47 Python files across 5 modules (23.6 KLOC)
- 20+ spec/implementation documents reviewed
- Output: E:/sovyx/docs/_meta/gap-inputs/analysis-B-services.md
