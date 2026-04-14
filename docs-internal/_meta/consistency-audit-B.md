# Consistency Audit — Part B (llm/voice/persistence/observability/plugins)

**Gerado em**: 2026-04-14
**Escopo**: 5 docs de módulos em `docs/modules/` vs. código em `src/sovyx/`.
**Referência**: `docs/_meta/gap-analysis.md`.

---

## Sumário

| Doc | Checks OK | Issues | Status |
|---|---:|---:|---|
| `llm.md` | 5 / 6 | 2 (1 minor, 1 minor-factual) | ⚠️ Minor |
| `voice.md` | 6 / 6 | 1 (minor inconsistência interna) | ✅ Aligned (minor) |
| `persistence.md` | 6 / 6 | 0 | ✅ Aligned |
| `observability.md` | 5 / 6 | 2 (1 minor prose, 1 hidden gap) | ⚠️ Minor |
| `plugins.md` | 6 / 6 | 0 críticos; 1 clarificação editorial | ✅ Aligned |

Legenda: OK = claims verificados; Issue = divergência code↔doc, claim falso, ou gap não marcado.

---

## llm.md

| # | Check | Resultado | Detalhe |
|---|---|---|---|
| 1 | Paths citados existem | ✅ OK | `router.py`, `circuit.py`, `cost.py`, `models.py`, `providers/anthropic.py|openai.py|google.py|ollama.py|_shared.py` — todos presentes. 4 providers em `providers/` confere com spec. |
| 2 | Classes/funções nos snippets existem | ✅ OK | `ComplexityLevel@37`, `ComplexitySignals@46`, `classify_complexity@84`, `_equivalence` dict@442-465, pricing@492-506, `CircuitBreaker@circuit.py:13`, `CostGuard@cost.py:66`, `LLMResponse@models.py:28`, `ToolCall@9`, `ToolResult@18`, `CostBreakdown@cost.py:44` — todos confirmados. Line ranges dos snippets (37-42, 45-62, 84-125, 442-465) batem exatamente. |
| 3 | Claims de implementação | ✅ OK | Constantes (`_SIMPLE_MAX_LENGTH=500`, `_SIMPLE_MAX_TURNS=3`, `_COMPLEX_MIN_LENGTH=2000`, `_COMPLEX_MIN_TURNS=8`) conferem em `router.py:65-68`. `_SIMPLE_MODELS`/`_COMPLEX_MODELS` em 71/77 — conferem. 4 providers conferem (Anthropic/OpenAI/Google/Ollama). |
| 4 | [NOT IMPLEMENTED] batem com código | ✅ OK | Grep `def stream` em `src/sovyx/llm/` — **zero matches**. Streaming TTS de fato ausente. BYOK isolation também ausente (sem multi-user API key context). |
| 5 | Alinhamento com gap-analysis.md | ✅ OK | gap-analysis §llm lista exatamente: Streaming TTS + BYOK ausentes. Doc espelha. |
| 6 | Assinaturas/campos batem | ⚠️ ISSUE #1 | Seção "Dependências → Externas" lista `anthropic`, `openai`, `google-generativeai`. **Nenhum provider importa os SDKs nativos** — todos usam `httpx` direto (`providers/anthropic.py:9 import httpx`, sem `import anthropic`). Doc próprio Public API afirma "via httpx (sem SDK)" — **contradição interna**. Ação: remover `anthropic`/`openai`/`google-generativeai` da lista Externas; manter apenas `httpx`. |

**Issues llm.md (2)**:
- **ISSUE-LLM-1 (minor factual)**: Dependências externas listam SDKs que o código não usa. Corrigir para `httpx` + opcionais.
- **ISSUE-LLM-2 (minor editorial)**: Public API table lista `ToolCall` e `ToolResult` como classes do `llm`, mas `ToolResult` também é referenciado por `plugins` como tipo de retorno de tools. Não é bug — apenas vale clarificar ownership (está em `llm/models.py`).

---

## voice.md

| # | Check | Resultado | Detalhe |
|---|---|---|---|
| 1 | Paths citados existem | ✅ OK | Todos os 11 paths listados em Referências existem: `pipeline.py`, `wyoming.py`, `stt.py`, `stt_cloud.py`, `tts_piper.py`, `tts_kokoro.py`, `vad.py`, `wake_word.py`, `audio.py`, `auto_select.py`, `jarvis.py`. |
| 2 | Classes/funções nos snippets existem | ✅ OK | `VoicePipelineState(IntEnum)@pipeline.py:52`, constantes `_SAMPLE_RATE`/`_FRAME_SAMPLES`/`_SILENCE_FRAMES_END`/`_MAX_RECORDING_FRAMES`/`_BARGE_IN_THRESHOLD_FRAMES`/`_FILLER_DELAY_MS`/`_TEXT_MIN_WORDS` em `pipeline.py:38-44` — confere. Constantes Wyoming em `wyoming.py:31-44` — confere (doc diz 31-45, off-by-one trivial). `HardwareTier@auto_select.py:41` — confere. |
| 3 | Claims de implementação | ✅ OK | Classes do Public API table verificadas: `VoicePipeline@383`, `AudioOutputQueue@204`, `BargeInDetector@298`, `SovyxWyomingServer@wyoming.py:634`, `WyomingClientHandler@385`, `WyomingEvent@163`, `MoonshineSTT@stt.py:178`, `CloudSTT@stt_cloud.py:177`, `STTEngine@stt.py:140`, `TranscriptionResult@57`, `TranscriptionSegment@78`, `PartialTranscription@88`, `PiperTTS@tts_piper.py:158`, `KokoroTTS@tts_kokoro.py:118`, `TTSEngine@tts_piper.py:84`, `SileroVAD@vad.py:130`, `VADEvent@55`, `WakeWordDetector@wake_word.py:219`, `VerificationResult@160`, `VoiceModelAutoSelector@auto_select.py:367`, `AudioCapture@audio.py:207`, `AudioOutput@555`, `AudioDucker@461`, `JarvisIllusion@jarvis.py:196`. Todos os 8 eventos em `pipeline.py:76-129` confere (`WakeWordDetectedEvent`, `SpeechStartedEvent`, `SpeechEndedEvent`, `TranscriptionCompletedEvent`, `TTSStartedEvent`, `TTSCompletedEvent`, `BargeInEvent`, `PipelineErrorEvent`). |
| 4 | [NOT IMPLEMENTED] batem com código | ✅ OK | Confirmado via `ls src/sovyx/voice/ \| grep -iE "speaker\|parakeet\|clon"` — **zero matches**. Não existe `speaker_recognition.py`, `voice_cloning.py`, nem `parakeet.py`. Doc marca corretamente. |
| 5 | Alinhamento com gap-analysis.md | ✅ OK | gap-analysis.md lista Speaker Recognition (IMPL-005), Voice Cloning (IMPL-SUP-002), Parakeet TDT (IMPL-SUP-004) como missing — doc espelha com os mesmos IMPL IDs. |
| 6 | Assinaturas/campos batem | ⚠️ ISSUE #1 | **Inconsistência interna trivial**: seção Arquitetura diz "Moonshine v1.0 STT"; Public API table diz "Moonshine v2 via biblioteca moonshine-voice". Código em `stt.py:102` define `MoonshineConfig` mas não versiona; snippet de código na doc cita "Moonshine v1.0 API" em comentário. Baixa criticidade — recomendar padronizar para "Moonshine v2 (moonshine-voice)" que é o que bate com o Public API e a realidade (biblioteca é `moonshine-voice` atual). |

**Issues voice.md (1)**:
- **ISSUE-VOICE-1 (minor)**: Versão do Moonshine referenciada como "v1.0" em um lugar e "v2" em outro. Alinhar.

---

## persistence.md

| # | Check | Resultado | Detalhe |
|---|---|---|---|
| 1 | Paths citados existem | ✅ OK | `pool.py`, `manager.py`, `migrations.py`, `schemas/{system,brain,conversations}.py`, `datetime_utils.py` — todos presentes. |
| 2 | Classes/funções nos snippets existem | ✅ OK | `_DEFAULT_PRAGMAS@pool.py:29`, `DatabasePool@40`, `initialize@79`, `close@106`, `_load_extensions@150`, `_setup_connection@137`, `Migration@migrations.py:26` com `compute_checksum@42`, `MigrationRunner@65`, `DatabaseManager@manager.py:27`. Snippets refletem código real (shutdown: readers→checkpoint→writer em `close()@106-132`). |
| 3 | Claims de implementação | ✅ OK | 9 pragmas — 7 no `_DEFAULT_PRAGMAS` dict (`journal_mode`, `synchronous`, `temp_store`, `foreign_keys`, `busy_timeout`, `wal_autocheckpoint`, `auto_vacuum`) + 2 condicionais (`cache_size@pool.py:142`, `mmap_size@pool.py:144`) = **9 non-negotiable confirmados**. DB-per-Mind via `get_brain_pool()@manager.py:155` e `get_system_pool()@144`. Extension loading com fallback silencioso confirmado em `_load_extensions@150-170`. |
| 4 | [NOT IMPLEMENTED] batem com código | ✅ OK | Vector search queries — grep do módulo confirma extensão carrega mas não há queries `vec_*` aqui (delegação implícita a `brain/`). Doc marca como Partial corretamente. Redis caching — zero arquivos `redis*.py`, marcado Not Implemented corretamente. |
| 5 | Alinhamento com gap-analysis.md | ✅ OK | gap-analysis §persistence fala: WAL + sqlite-vec + 1W+NR OK, vector queries não visíveis, Redis não implementado. Doc espelha. |
| 6 | Assinaturas/campos batem | ✅ OK | Tabela "9 Pragmas Non-Negotiable" bate com código. Linhas 49-71 (snippet pool) correspondem estruturalmente a `pool.py:29-93`. |

**Issues persistence.md**: nenhum.

---

## observability.md

| # | Check | Resultado | Detalhe |
|---|---|---|---|
| 1 | Paths citados existem | ✅ OK | `logging.py`, `tracing.py`, `metrics.py`, `health.py`, `slo.py`, `alerts.py`, `prometheus.py` — todos presentes. 7 arquivos = estrutura da árvore no doc. |
| 2 | Classes/funções nos snippets existem | ✅ OK | `bind_request_context@logging.py:38`, `CheckStatus`@health.py, `CheckResult` @health.py, `AlertSeverity(StrEnum)@slo.py:54` (`NONE/TICKET/PAGE`), `SecretMasker@logging.py:157`, `MetricsRegistry@metrics.py:57`, `SovyxTracer@tracing.py:57`, `PrometheusExporter@prometheus.py:90`, `AlertManager@alerts.py:176`, `SLOMonitor@slo.py:388`, `SLOTracker@slo.py:214`. |
| 3 | Claims de implementação | ✅ OK | 10 HealthCheck subclasses confirmadas em `health.py`: `DiskSpaceCheck@147`, `RAMCheck@202`, `CPUCheck@249`, `DatabaseCheck@292`, `BrainIndexedCheck@330`, `LLMReachableCheck@371`, `ModelLoadedCheck@418`, `ChannelConnectedCheck@453`, `ConsolidationCheck@499`, `CostBudgetCheck@540`. Exatos 10. |
| 4 | [NOT IMPLEMENTED] batem com código | ⚠️ ISSUE #1 | Doc marca apenas "gen_ai semantic conventions OTel" como Partial. **Checagem adicional**: `tracing.py` usa `SimpleSpanProcessor@39` (não `BatchSpanProcessor`). IMPL-015 especifica `BatchSpanProcessor` — **divergência não registrada** no doc. Grep de `BatchSpanProcessor` e `gen_ai` no módulo → 0 matches. Recomendar adicionar segunda linha Partial: "BatchSpanProcessor não usado — código usa SimpleSpanProcessor; impacto em produção (perf de export batch)". |
| 5 | Alinhamento com gap-analysis.md | ✅ OK | gap-analysis §observability fala "4 done / 1 partial / 0 missing; possível gap menor em gen_ai conventions". Doc espelha mas não pega o `SimpleSpanProcessor`. |
| 6 | Assinaturas/campos batem | ⚠️ ISSUE #2 | Prosa abaixo da tabela Public API diz "**HealthChecker** descobre automaticamente 10 implementações de `HealthCheck`". O nome real da classe em `observability/health.py:85` é **`HealthRegistry`** (não `HealthChecker`). Há um `HealthChecker` separado em `engine/health.py:34` — classes distintas. Erro de nome na prosa do módulo. |

**Issues observability.md (2)**:
- **ISSUE-OBS-1 (hidden gap)**: `SimpleSpanProcessor` vs `BatchSpanProcessor` não marcado como divergência. Código usa Simple, spec IMPL-015 manda Batch.
- **ISSUE-OBS-2 (nome errado)**: prosa após Public API table escreve "HealthChecker descobre automaticamente" — deve ser "HealthRegistry". `HealthChecker` vive em outro módulo (`engine/`) e é outra coisa.

---

## plugins.md

| # | Check | Resultado | Detalhe |
|---|---|---|---|
| 1 | Paths citados existem | ✅ OK | 15 arquivos em Referências — todos existem. `manager.py`, `security.py`, `permissions.py`, `sandbox_fs.py`, `sandbox_http.py`, `sdk.py`, `context.py`, `manifest.py`, `lifecycle.py`, `hot_reload.py`, `events.py`, `testing.py` e 5 plugins oficiais (`calculator`/`financial_math`/`knowledge`/`weather`/`web_intelligence`). Total em `src/sovyx/plugins/`: **19 arquivos .py** — bate com "19 arquivos" na Objetivo. |
| 2 | Classes/funções nos snippets existem | ✅ OK | `PluginSecurityScanner@security.py:54` com `BLOCKED_IMPORTS@63`/`BLOCKED_CALLS@89`/`BLOCKED_ATTRIBUTES@98`. `Permission(StrEnum)@permissions.py:24`. `_MAX_FILE_BYTES@sandbox_fs.py:32`, `_MAX_TOTAL_BYTES@33`, `SandboxedFsAccess@39`. `_DEFAULT_TOOL_TIMEOUT_S@manager.py:42`, `_MAX_CONSECUTIVE_FAILURES@43`, `_PluginHealth@58`. `ToolDefinition@sdk.py:29`. |
| 3 | Claims de implementação | ✅ OK | **13 permissões** confirmadas contando valores do enum: BRAIN_READ, BRAIN_WRITE, EVENT_SUBSCRIBE, EVENT_EMIT, NETWORK_LOCAL, NETWORK_INTERNET, FS_READ, FS_WRITE, SCHEDULER_READ, SCHEDULER_WRITE, VAULT_READ, VAULT_WRITE, PROACTIVE = **13**. Doc na Responsabilidades fala "13 permissões ... o doc cita 18 tipos ao considerar variantes historical" — refere-se a IMPL-012 spec mapeando 18 vetores de escape, não tipos de permissão. Interpretação correta. |
| 4 | [NOT IMPLEMENTED] batem com código | ✅ OK | Confirmado via grep `seccomp\|namespace\|Seatbelt` em `src/sovyx/plugins/` → matches apenas em **comentários** (`manager.py`, `context.py`). Nenhum código funcional seccomp-BPF / namespaces / Seatbelt / subprocess IPC. Doc marca 4 layers v2 como Not Implemented — correto. |
| 5 | Alinhamento com gap-analysis.md | ✅ OK | gap-analysis §plugins fala "Sandbox v1 (layers 0-4) completo; v2 (layers 5-7 + subprocess IPC) deferido; zero-downtime parcial". Doc espelha 1:1. |
| 6 | Assinaturas/campos batem | ✅ OK | Public API table confere com classes reais: `PluginManager@manager.py:129`, `ISovyxPlugin@sdk.py:332`, `ToolDefinition@29`, `PluginContext@context.py:934`, `BrainAccess@40`, `EventBusAccess@860`, `Permission@permissions.py:24`, `PermissionEnforcer@165`, `PluginSecurityScanner@security.py:54`, `ImportGuard@255`, `SandboxedFsAccess@sandbox_fs.py:39`, `SandboxedHttpClient@sandbox_http.py:135`, `PluginFileWatcher@hot_reload.py:31`, `PluginStateTracker@lifecycle.py:59`, `PluginState@28`, `LoadedPlugin@manager.py:68`, `SecurityFinding@security.py:35`, `MockPluginContext@testing.py:386`. Errors confere: `PluginError@manager.py:49`, `PluginDisabledError@53`, `PermissionDeniedError@permissions.py:138`, `PluginAutoDisabledError@150`, `InvalidTransitionError@lifecycle.py:200`, `ManifestError@manifest.py:157`. Events confere: `PluginStateChanged@events.py:18`, `PluginLoaded@33`, `PluginUnloaded@50`, `PluginToolExecuted@66`, `PluginAutoDisabled@82`. Configs confere: `PluginManifest@manifest.py:66`, `NetworkConfig@26`, `PluginDependency@32`, `EventsConfig@49`, `EventDeclaration@39`, `ToolDeclaration@56`. Tabela 100% verificada. |

**Issues plugins.md**: nenhum crítico. Clarificação editorial sugerida:
- **ISSUE-PLUG-EDIT (clarificação, não bug)**: Responsabilidades diz "13 permissões ... o doc cita 18 tipos ao considerar variantes historical" — a redação é confusa. O "18" é da IMPL-012 §escape vectors (atacantes, não permissões). Refactoring: separar em duas frases para não induzir leitor a pensar que permissions.py deveria ter 18 entries.

---

## Consolidado final

| Doc | Issues críticos | Issues minor | Total |
|---|---:|---:|---:|
| llm.md | 0 | 2 | 2 |
| voice.md | 0 | 1 | 1 |
| persistence.md | 0 | 0 | 0 |
| observability.md | 0 | 2 | 2 |
| plugins.md | 0 | 1 (editorial) | 1 |
| **TOTAL** | **0** | **6** | **6** |

**Zero divergências críticas** em Part B. Todos os [NOT IMPLEMENTED] batem com ausências reais no código. Todas as classes do Public API existem onde o doc diz.

Ações recomendadas (Fase 4 — correções na doc final):

1. **llm.md**: remover SDKs nativos da lista "Externas" (código é 100% httpx).
2. **voice.md**: padronizar versão do Moonshine (v1.0 vs v2).
3. **observability.md**: adicionar Partial "SimpleSpanProcessor em vez de BatchSpanProcessor (IMPL-015)"; corrigir "HealthChecker" → "HealthRegistry" na prosa pós-tabela.
4. **plugins.md**: refatorar frase do "13 vs 18" para não confundir permissions com escape vectors.

---

## Verificações de referência (comandos executados)

- `ls src/sovyx/llm/providers/` → 4 providers (anthropic, openai, google, ollama) + _shared.
- `ls src/sovyx/voice/` → **nenhum** speaker_recognition.py / parakeet*.py / *_cloning*.py (confirma [NOT IMPLEMENTED]).
- `grep 'class \w+Check' observability/health.py` → 10 concrete subclasses de `HealthCheck`.
- Contagem de valores do enum `Permission` em `permissions.py` → **13**.
- `_DEFAULT_PRAGMAS` + `cache_size`/`mmap_size` condicionais em `pool.py` → **9 pragmas**.
- `grep 'def stream' src/sovyx/llm/` → 0 matches.
- `grep 'seccomp|namespace|Seatbelt' src/sovyx/plugins/` → só comentários, nenhum código funcional.
- `grep 'BatchSpanProcessor|gen_ai' src/sovyx/observability/` → 0 matches (hidden gap em observability.md).
- `find src/sovyx/plugins -name "*.py"` → 19 arquivos (bate com doc).
