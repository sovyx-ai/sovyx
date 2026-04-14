# Gap Analysis — Documentação (sovyx-bible) vs Código Real

**Gerado em**: 2026-04-14
**Escopo**: 14 módulos Python (`src/sovyx/`) + dashboard React (`dashboard/src/`)
**Inputs**: 853 docs triados (Fase 2) + ~46k LOC Python + ~23k LOC TypeScript
**Detalhes por grupo de módulos**: ver `docs/_meta/gap-inputs/analysis-{A,B,C,D}-*.md`

---

## Sumário executivo

O Sovyx tem **núcleo cognitivo ~95% feature-complete** (engine, cognitive, brain, context, mind, llm, voice base, persistence, observability, dashboard). Os gaps críticos concentram-se em **3 áreas com impacto comercial direto**:

1. **Bridge / Relay Client** (audio streaming WebSocket+Opus) — bloqueia mobile app
2. **Cloud / Stripe Connect + Pricing experiments** — bloqueia plugin marketplace e revenue
3. **Upgrade / Conversation Importers** (ChatGPT/Claude/Gemini/Obsidian) — bloqueia onboarding e GDPR Art. 20

Adicionalmente, há **divergência de modelo emocional** (ADR-001 decide PAD 3D, código implementa 2D) e **fases CONSOLIDATE/DREAM do cognitive loop** estão orphaned (consolidation existe como background mas não é chamada pelo loop; dream não existe).

Dashboard tem **zero gaps críticos** e **100% type alignment** entre backend e frontend. (Obs: auditoria de consistência 2026-04-14 confirmou que `import "@/lib/i18n"` **já está presente** em `dashboard/src/main.tsx:3` — item previamente listado como gap foi resolvido antes desta análise.)

---

## Tabela consolidada por módulo

| Módulo | LOC Python | Docs | ✅ Done | ⚠️ Partial | ❌ Missing | Status |
|---|---:|---:|---:|---:|---:|---|
| `engine` | 2964 | 107 | 5 | 0 | 0 | ✅ Aligned |
| `cognitive` | 7877 | 15 | 5 | 0 | 2 | ⚠️ DREAM/CONSOLIDATE faltando |
| `brain` | 4829 | 13 | 7 | 1 | 0 | ⚠️ Emotional 2D vs 3D ADR-001 |
| `context` | 759 | 2 | 4 | 0 | 0 | ✅ Aligned |
| `mind` | 803 | 5 | 4 | 1 | 1 | ⚠️ Emotional baseline ausente |
| `llm` | 2378 | 4 | 3 | 1 | 2 | ⚠️ Streaming TTS, BYOK |
| `voice` | 6019 | 11 | 4 | 0 | 3 | ❌ Speaker rec, voice clone, Parakeet TDT |
| `persistence` | 1218 | 21 | 3 | 1 | 0 | ⚠️ vector queries não vistas |
| `observability` | 3221 | 5 | 4 | 1 | 0 | ✅ Quase aligned |
| `plugins` | 9860 | 32 | 6 | 1 | 4 | ⚠️ v2 (seccomp/namespaces) deferido |
| `bridge` | 1456 | 10 | 5 | 0 | 8 | ❌ Relay/HA/CalDAV faltando — 38% |
| `cloud` | 5410 | 31 | 13 | 1 | 8 | ⚠️ Stripe Connect, pricing PQL — 64% |
| `upgrade` | 3375 | 5 | 8 | 0 | 7 | ❌ Conversation importers — 53% |
| `cli` | 1846 | 94 | 10 | 1 | 7 | ⚠️ REPL, admin utils — 61% |
| `dashboard` (BE) | 5706 | 39 | 25 endpoints | — | — | ✅ |
| `dashboard` (FE) | 22928 | 39 | 11 pages | 3 stubs | 0 | ✅ Zero critical gaps |
| **TOTAL** | **~46k** | **— ** | **~110** | **10** | **42** | — |

Conclusão geral: **~75-80% feature-complete**, com gaps concentrados em integrações externas (relay, marketplace, importers).

---

## ❌ Top 10 gaps críticos (ordem de impacto)

| # | Gap | Módulo | Doc-fonte | Impacto comercial |
|---|---|---|---|---|
| 1 | **Relay Client** (WebSocket+Opus audio) | bridge | IMPL-007 | Bloqueia mobile app + cloud relay |
| 2 | **Stripe Connect** (Express, destination charges, refunds) | cloud | IMPL-011 | Bloqueia plugin marketplace |
| 3 | **Conversation Importers** (ChatGPT/Claude/Gemini/Obsidian) | upgrade | IMPL-SUP-015 | Bloqueia migração de usuários + GDPR Art. 20 |
| 4 | **Speaker Recognition** (ECAPA-TDNN biometrics) | voice | IMPL-005 | Bloqueia voice auth multi-user |
| 5 | **Pricing Experiments** (Van Westendorp, Gabor-Granger, PQL) | cloud | IMPL-SUP-006 | Bloqueia revenue optimization |
| 6 | **Home Assistant bridge** | bridge | IMPL-008 | Bloqueia smart home positioning |
| 7 | **CalDAV sync** | bridge | IMPL-009 | Bloqueia calendar feature |
| 8 | **CONSOLIDATE phase** orphaned (existe mas não é chamada pelo loop) | cognitive | SPE-003 §1.1 | Memória degrada sem prune/strengthen automático |
| 9 | **DREAM phase** (nightly pattern discovery) | cognitive | SPE-003 §1.1 | Sem auto-discovery de padrões |
| 10 | **Voice Cloning** (speaker adaptation) | voice | IMPL-SUP-002 | Feature premium não disponível |

## ⚠️ Top 6 divergências (código diverge da spec)

| # | Divergência | Módulo | Doc-fonte | Risco |
|---|---|---|---|---|
| 1 | **Emotional model 2D em vez de 3D PAD** | brain, mind | ADR-001 §2 (Option D CHOSEN) | Schema migration necessária pra v1.0; afeta consolidation, context assembly, personality |
| 2 | **Emotional baseline config ausente em MindConfig** | mind | ADR-001 | Não dá pra configurar baseline + homeostasis_rate por mente |
| 3 | **Stripe Connect — webhook é dispatch registry sem eventos hardcoded** | cloud | IMPL-011 §2 | Eventos planejados (~20+) não são registrados em bootstrap; integration partial |
| 4 | **Sandbox plugin v2 deferido** (seccomp-BPF, namespaces, macOS Seatbelt) | plugins | IMPL-012 layers 5-7 | Aceitável v0.5; v1 in-process (layers 0-4) é seguro o suficiente |
| 5 | **Vector search SQL** (extensão sqlite-vec carrega mas queries não visíveis) | persistence | ADR-004 | Pode estar em código não inspecionado; verificar |
| 6 | **SpanProcessor síncrono** (`SimpleSpanProcessor`) em vez de `BatchSpanProcessor` | observability | IMPL-015 | Overhead maior em produção; troca trivial quando backend OTel estabilizar — tracing.py:39 |

## ✅ Top features implementadas sem doc dedicada

| # | Feature | Localização | Observação |
|---|---|---|---|
| 1 | **CogLoopGate** (serialização concorrente) | `cognitive/gate.py` | Padrão integrado correto, mas não em SPE-003 |
| 2 | **Safety stack completo** (PII guard, financial gate, shadow mode, escalation) | `cognitive/safety_*.py` (14 arquivos) | Provavelmente coberto em ADRs de segurança que não foram amostradas |
| 3 | **Graceful degradation** (DegradationManager) | `engine/degradation.py` | Cascading fallback chains não detalhado em SPE-001 |
| 4 | **HealthChecker** | `engine/health.py` | Sistema completo não documentado |
| 5 | **HebbianLearning + EbbinghausDecay + WorkingMemory** | `brain/learning.py`, `brain/working_memory.py` | Implementação rica com pouca documentação direta |
| 6 | **Dunning + Flex balance + usage cascade** | `cloud/dunning.py`, `cloud/flex.py`, `cloud/usage.py` | Lógica de pay-as-you-go presente sem spec dedicada |

---

## Detalhe por módulo

### `engine` ✅
**Specs**: SPE-001-ENGINE-CORE, ADR-007-EVENT-ARCHITECTURE, ADR-008-LOCAL-FIRST.
**Classes**: MindManager, ServiceRegistry, LifecycleManager, EventBus, HealthChecker, DaemonRPCServer.
**Status**: 0 gaps significativos. ServiceRegistry custom (~150 LOC), EventBus com 11 event types, bootstrap em camadas.
Detalhe: `gap-inputs/analysis-A-core.md`.

### `cognitive` ⚠️
**Specs**: SPE-003-COGNITIVE-LOOP (1408 linhas), IMPL-006-COGNITIVE-LOOP.
**Classes**: PerceivePhase, AttendPhase, ThinkPhase, ActPhase, ReflectPhase, CognitiveLoop, CognitiveStateMachine.
**Status**: 5/7 fases implementadas. CONSOLIDATE existe em `brain/consolidation.py` mas **não é chamada pelo loop**; DREAM **não existe**.
**Spec-extra (não-gap)**: `cognitive/gate.py` (CogLoopGate) e `cognitive/safety_*.py` (14 arquivos) são robustos mas com documentação esparsa.
Detalhe: `gap-inputs/analysis-A-core.md`.

### `brain` ⚠️
**Specs**: SPE-004-BRAIN-MEMORY, IMPL-002-BRAIN-ALGORITHMS, ADR-001-EMOTIONAL-MODEL.
**Classes**: Concept, Episode, Relation, BrainService, EmbeddingEngine, SpreadingActivation, HybridRetrieval, ConsolidationCycle, HebbianLearning, EbbinghausDecay, WorkingMemory.
**Status**: Modelos core implementados. **Episode armazena 2D emotional (valence+arousal); Concept armazena apenas 1D (valence). ADR-001 §2 decidiu 3D PAD.** Implementação diverge da decisão arquitetural.
**Scoring**: ImportanceWeights (cat=0.15, llm=0.35, emo=0.10, novelty=0.15, explicit=0.25).
Detalhe: `gap-inputs/analysis-A-core.md`.

### `context` ✅
**Specs**: SPE-006-CONTEXT-ASSEMBLY, IMPL-003-CONTEXT-ASSEMBLY.
**Classes**: ContextAssembler, AssembledContext, TokenBudgetManager, TokenCounter, ContextFormatter.
**Status**: 6 slots implementados conforme spec. Token allocation adaptativo. Lost-in-Middle ordering respeitado. Zero gaps.
Detalhe: `gap-inputs/analysis-A-core.md`.

### `mind` ⚠️
**Specs**: SPE-002-MIND-DEFINITION, ADR-001-EMOTIONAL-MODEL.
**Classes**: MindConfig, PersonalityConfig (OCEAN+behavioral traits), LLMConfig, BrainConfig, SafetyConfig, PersonalityEngine.
**Status**: OCEAN Big Five completo. Behavioral traits (tone, formality, humor, assertiveness, curiosity, empathy, verbosity) presentes. **Falta emotional baseline (valence/arousal/dominance + homeostasis_rate)** conforme ADR-001.
Detalhe: `gap-inputs/analysis-A-core.md`.

### `llm` ⚠️
**Specs**: SPE-007-LLM-ROUTER (1062 linhas), VR-085-CLOUD-LLM-PROXY.
**Classes**: ComplexityLevel, ComplexitySignals, classify_complexity(), AnthropicProvider, GoogleProvider, OllamaProvider, OpenAIProvider, circuit breaker, cost tracker.
**Status**: Routing por complexidade (SIMPLE/MODERATE/COMPLEX) implementado. Provider abstraction OK. **Streaming pra TTS especulativo não exposto**. **BYOK token isolation por user API key não implementado**.
Detalhe: `gap-inputs/analysis-B-services.md`.

### `voice` ❌
**Specs**: IMPL-004-VOICE-ONNX, IMPL-005-SPEAKER-RECOGNITION, IMPL-SUP-002-VOICE-CLONING, IMPL-SUP-003-WYOMING-PROTOCOL, IMPL-SUP-004-PARAKEET-TDT.
**Classes**: WyomingServer, VoicePipeline (state machine IDLE→WAKE→RECORDING→THINKING→SPEAKING), STT (Moonshine), TTS (Piper, Kokoro), VAD (SileroVAD v5), wake_word, audio.
**Status**: TTS/STT/Wyoming/VAD/wake-word/barge-in/Jarvis filler/hardware tier auto-select **completos**. **3 features inteiras faltando**:
- ❌ Speaker Recognition (ECAPA-TDNN biometrics, enrollment) — zero arquivos
- ❌ Voice Cloning (speaker adaptation)
- ❌ Parakeet TDT (text detection / monolingual fallback)
Detalhe: `gap-inputs/analysis-B-services.md`.

### `persistence` ⚠️
**Specs**: ADR-004-DATABASE-STACK (SQLite WAL, sqlite-vec, 9 pragmas non-negotiable), SPE-005-PERSISTENCE-LAYER.
**Classes**: DatabasePool (1 writer + N readers), migrations, manager, schemas (brain, conversations, system).
**Status**: WAL + sqlite-vec extension loading + 1W+NR concurrency + DB-per-Mind isolation + migrations OK. **Vector search queries não visíveis no scan** (extensão carrega mas falta evidência das queries). **Redis caching ADR-004 não implementado**.
Detalhe: `gap-inputs/analysis-B-services.md`.

### `observability` ✅
**Specs**: IMPL-015-OBSERVABILITY (BatchSpanProcessor, gen_ai conventions, SLO burn rate), SPE-026-OBSERVABILITY-METRICS (30+ metrics).
**Classes**: AlertManager, HealthChecker (10 checks), SLO burn rate, MetricsExporter (Prometheus), structlog logging, OTel tracing.
**Status**: OTel + structlog + SLO + Prometheus + 10 health checks **completos**. Possível gap menor: `gen_ai` semantic conventions pode usar atributos custom em vez do padrão OTel.
Detalhe: `gap-inputs/analysis-B-services.md`.

### `plugins` ⚠️ (módulo mais pesado: 9860 LOC + 32 docs)
**Specs**: IMPL-012-PLUGIN-SANDBOX (7-layer, seccomp, 18 escape vectors mapped), SPE-008-PLUGIN-* (12 variantes — SDK, registry, review CI, governance).
**Classes**: PluginManager, AST scanner (BLOCKED_IMPORTS/CALLS/ATTRIBUTES), ImportGuard (runtime hook), sandbox_fs (50MB/file, 500MB total, symlink check), sandbox_http (rate limit), permissions enforcer (18 types), lifecycle, manifest, SDK (`@tool`), context.
**Plugins oficiais**: calculator, financial_math, knowledge, weather, web_intelligence.
**Status**: Sandbox v1 (in-process, layers 0-4) **completo e seguro**. **v2 deferido** intencionalmente: layers 5-7 (seccomp-BPF Linux, namespaces, macOS Seatbelt). **Subprocess IPC não implementado** (v2). Zero-downtime update/rollback **parcial**.
Detalhe: `gap-inputs/analysis-B-services.md`.

### `bridge` ❌ (38% complete)
**Specs**: IMPL-007-RELAY-CLIENT, IMPL-008-HOME-ASSISTANT, IMPL-009-CALDAV, SPE-014-COMMUNICATION-BRIDGE.
**Classes**: BridgeManager, TelegramChannel (aiogram), SignalChannel (signal-cli-rest-api), InboundMessage/OutboundMessage protocol, person resolver, conversation tracker.
**Status**: Telegram + Signal + manager + financial confirmation + person/conversation resolver **OK**. **Faltando 8 features**:
- ❌ **Relay Client** (RelayClient, Opus codec 24kbps, audio ring buffer 60ms, resampling 16↔48kHz, offline queue, exponential backoff) — IMPL-007 §1.1-1.3
- ❌ **Home Assistant** (HomeAssistantBridge, entity registry 10 domains, ActionSafety SAFE/CONFIRM/DENY, mDNS, WS reconnect) — IMPL-008
- ❌ **CalDAV** (CalendarAdapter/CalDAVClient, ctag+etag incremental, RRULE expansion, timezones DATE vs DATE-TIME, conflict resolution) — IMPL-009
Detalhe: `gap-inputs/analysis-C-integration.md`.

### `cloud` ⚠️ (64% complete)
**Specs**: IMPL-011-STRIPE-CONNECT, IMPL-SUP-006-PRICING-PQL, SPE-033-CLOUD-SERVICES, MONETIZATION-LIFECYCLE.
**Classes**: SubscriptionTier (6 tiers), checkout, billing webhook (HMAC-SHA256), license (JWT Ed25519, grace period), backup (R2 + encryption + VACUUM), scheduler (GFS retention), dunning, flex (pay-as-you-go), usage (cascade), api_keys.
**Status**:
- ✅ Billing fundamentals (6 tiers, checkout, webhook 6 events)
- ⚠️ **Stripe Connect parcial**: webhook há, mas faltam Express onboarding, destination charges, refund, dispute, payout, Stripe Tax, completar 20+ webhooks
- ❌ **Pricing experiments**: Van Westendorp (4-question), Gabor-Granger (WTP), PQLScorer, FunnelTracker — IMPL-SUP-006
Detalhe: `gap-inputs/analysis-C-integration.md`.

### `upgrade` ❌ (53% complete)
**Specs**: SPE-028-UPGRADE-MIGRATION, IMPL-SUP-015-IMPORTS-INTERMIND-PAGINATION.
**Classes**: Doctor (10+ checks), MindImporter (SMF/ZIP), exporter (SMF), schema (SemVer migrations), backup_manager, blue_green.
**Status**: Doctor + migrations + Mind import/export + backup + blue-green **OK**. **Faltando 7 features**:
- ❌ **ChatGPTImporter** (conversations.json tree)
- ❌ **ClaudeImporter**
- ❌ **GeminiImporter**
- ❌ **ObsidianImporter** (markdown + wikilinks)
- ❌ **InterMindBridge** (multi-instance sync)
- ❌ **CursorPagination** (REST API)
- ❌ **SMFExporter completo** (GDPR Art. 20 data portability)
Detalhe: `gap-inputs/analysis-C-integration.md`.

### `cli` ⚠️ (61% complete)
**Specs**: SPE-015-CLI-TOOLS (Typer + Rich, JSON-RPC).
**Classes**: Typer app, DaemonClient (Unix socket JSON-RPC 2.0), commands (init/start/stop/status/token/doctor/brain/mind/plugin/dashboard/logs).
**Status**: Framework + RPC + comandos core OK. **DaemonRPCServer sketch only** (não tem registry completo de métodos). **Faltando**: REPL interativo (multi-line, auto-complete, history), Admin utilities (db inspection, config reset, user/mind management).
Detalhe: `gap-inputs/analysis-C-integration.md`.

### `dashboard` ✅ (zero critical)
**Backend**: 17 módulos / 5706 LOC / 25 endpoints / 15 WS events / token auth via `create_app(token=...)`.
**Frontend**: 14 páginas (11 full + 3 stubs: Voice/Emotions/Productivity), 11 Zustand slices, 40+ components, 4 hooks (useAuth, useWebSocket debounced 300ms, useMobile, useOnboarding), api.ts com 20+ schemas.
**Type alignment**: 100% (zero drifts) entre backend e frontend.
**Immersion docs F01-F08**: todos os 8 (shadcn/ui v4, recharts, tanstack-virtual, i18next, force-graph-2d, framer-motion, cmdk, patterns) **aplicados no código**.
**Gap único actionable**: ~~falta `import "@/lib/i18n"` em `dashboard/src/main.tsx`~~ — **resolvido** (verificado em 2026-04-14: import presente em main.tsx:3).
Detalhe: `gap-inputs/analysis-D-dashboard.md`.

---

## Research que embasou decisões mas não foi totalmente seguida

| Research doc | Decisão recomendada | Status no código |
|---|---|---|
| ADR-001 | Emotional PAD 3D (Option D) | Implementado 2D (valence+arousal) |
| ADR-007 | Event bus + RPC protocol detalhado | Event bus ✓, RPC parcial |
| IMPL-005 | ECAPA-TDNN voice biometrics | Não implementado |
| IMPL-007 | Opus 24kbps + 60ms latency target | Não implementado |
| IMPL-SUP-006 | Pricing PQL + Van Westendorp + Gabor-Granger | Não implementado |
| IMPL-SUP-015 | Importers ChatGPT/Claude/Gemini/Obsidian | Não implementado |
| SPE-003 §1.1 | 7 fases (perceive→…→consolidate→dream) | 5 fases por interação; consolidate orphaned; dream ausente |

---

## Roadmap derivado dos gaps

### v0.5 (atual — fechar polish)
- [x] ~~`dashboard/src/main.tsx`: adicionar `import "@/lib/i18n"`~~ (já presente — main.tsx:3)
- [ ] Auditar i18n namespace consistency entre páginas
- [ ] Remover/clarificar 3 stubs (Voice, Emotions, Productivity) — marcar como "v0.6 planned"

### v0.6 (próxima major)
**Bloqueadores comerciais:**
- [ ] **bridge/relay**: `RelayClient` + Opus + audio ring buffer (3-5 dias)
- [ ] **cloud/stripe-connect**: completar Express, destination charges, refund, dispute, payout, Stripe Tax
- [ ] **upgrade/importers**: ChatGPT + Claude + Gemini + Obsidian + InterMind
- [ ] **voice/speaker-recognition**: ECAPA-TDNN + enrollment + verification

**Refinamentos arquiteturais:**
- [ ] **brain/emotional**: schema migration 2D → 3D PAD (ADR-001)
- [ ] **mind**: adicionar emotional baseline config + homeostasis_rate
- [ ] **cognitive/consolidate**: chamar `brain.ConsolidationCycle` periodicamente do loop
- [ ] **cognitive/dream**: implementar fase nightly de pattern discovery

**Features secundárias:**
- [ ] **bridge/home-assistant**, **bridge/caldav**
- [ ] **cloud/pricing-experiments** (Van Westendorp, Gabor-Granger, PQL)
- [ ] **voice/voice-cloning**, **voice/parakeet-tdt**
- [ ] **cli/repl**, **cli/admin**
- [ ] **persistence**: validar/expor vector search via sqlite-vec

### v1.0 (estabilidade + plugin marketplace)
- [ ] **plugins/sandbox-v2**: seccomp-BPF (Linux), namespaces, macOS Seatbelt
- [ ] **plugins/subprocess-ipc**
- [ ] **plugins/zero-downtime-update** completo
- [ ] **persistence/redis-caching** (opcional, conforme ADR-004)

---

## Saneamento de doc gerada

Esta análise é input pra Fase 4 (reescrita da documentação consolidada). Próximos passos:
1. Cada módulo doc em `docs/modules/<mod>.md` deve seguir a estrutura "Overview | Specs source | Implementation | Gaps `[NOT IMPLEMENTED]` | References"
2. Gaps marcados aqui viram seções `[NOT IMPLEMENTED]` na doc final, com link para o doc-fonte
3. Top divergências viram ADRs novas em `docs/architecture/decisions/` ou notas em `docs/_meta/divergences.md`
4. Roadmap acima vira `docs/planning/roadmap.md`
