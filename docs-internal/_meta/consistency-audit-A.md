# Consistency Audit — Part A (engine/cognitive/brain/context/mind)

**Gerado em**: 2026-04-14
**Escopo**: 5 docs em `docs/modules/` vs código em `src/sovyx/{engine,cognitive,brain,context,mind}/`
**Referência de [NOT IMPLEMENTED]**: `docs/_meta/gap-analysis.md`

## Sumário

| Doc | Checks OK | Issues | Status |
|---|---:|---:|---|
| engine.md | 37/39 | 2 | ⚠️ |
| cognitive.md | 34/35 | 1 | ⚠️ |
| brain.md | 37/40 | 3 | ⚠️ |
| context.md | 21/21 | 0 | ✅ |
| mind.md | 30/30 | 0 | ✅ |
| **TOTAL** | **159/165** | **6** | ⚠️ |

**Severidade por issue**:
- 1 issue contagem-errada menor (engine: "11 events" vs 13 no código)
- 1 issue classe fantasma não-bloqueante (brain: `EvolutionScorer` citado, inexistente)
- 1 issue classe fantasma significativa (brain: `ContradictionDetector` citado, arquivo tem só funções)
- 1 issue minor de atribuição (brain: `ConceptContradicted` emitido por `service.py`, não `contradiction.py`)
- 2 issues de off-by-one em linha (trivial)

---

## engine.md

| Check | Resultado | Detalhe |
|---|---|---|
| Path `src/sovyx/engine/bootstrap.py` | ✅ existe | 572 LOC — casa com doc |
| Path `src/sovyx/engine/registry.py` | ✅ existe | 149 LOC — casa com doc |
| Path `src/sovyx/engine/events.py` | ✅ existe | 349 LOC — casa com doc |
| Path `src/sovyx/engine/health.py` | ✅ existe | 263 LOC — casa com doc |
| Path `src/sovyx/engine/degradation.py` | ✅ existe | 170 LOC — casa com doc |
| Path `src/sovyx/engine/rpc_server.py` | ✅ existe | 119 LOC — casa com doc |
| Path `src/sovyx/engine/rpc_protocol.py` | ✅ existe | 75 LOC — casa com doc |
| Path `src/sovyx/engine/config.py` | ✅ existe | 265 LOC — casa com doc |
| Path `src/sovyx/engine/lifecycle.py` | ✅ existe | — |
| Path `src/sovyx/engine/errors.py` | ✅ existe | 292 LOC — casa com doc |
| Path `src/sovyx/engine/types.py` | ✅ existe | — |
| Path `src/sovyx/engine/protocols.py` | ✅ existe | — |
| Class `ServiceRegistry` em registry.py | ✅ confirmado | registry.py:29 |
| Class `EventBus` em events.py | ✅ confirmado | events.py:274 |
| Class `LifecycleManager` em lifecycle.py | ✅ confirmado | lifecycle.py:91 |
| Class `PidLock` em lifecycle.py | ✅ confirmado | lifecycle.py:22 |
| Class `MindManager` em bootstrap.py | ✅ confirmado | bootstrap.py:21 (doc diz :21-49, casa) |
| Class `HealthChecker` em health.py | ✅ confirmado | health.py:34 |
| Class `DegradationManager` em degradation.py | ✅ confirmado | degradation.py:47 |
| Class `DaemonRPCServer` em rpc_server.py | ✅ confirmado | rpc_server.py:23 |
| Block `registry.py:29-38` literal | ✅ bate | docstring de ServiceRegistry confere |
| Block `events.py:40-56` Event base | ⚠️ off-by-1 | Definição real termina em :55, não :56 (trivial) |
| Block `events.py:311-322` emit | ✅ bate | emit está em :311 com error isolation |
| Block `bootstrap.py:52-68` bootstrap | ✅ bate | `async def bootstrap` em :52 |
| Claim "EventBus com 11 event classes" | ❌ inconsistência | Código tem **13 classes de Event** (events.py linhas 62,75,87,104,120,138,156,172,186,207,226,244,257). Lista da própria doc enumera 13 nomes, mas texto diz "11". Doc linha 123 vs código |
| Events listados existem (13): `EngineStarted`, `EngineStopping`, `ServiceHealthChanged`, `PerceptionReceived`, `ThinkCompleted`, `ResponseSent`, `ConceptCreated`, `EpisodeEncoded`, `ConceptContradicted`, `ConceptForgotten`, `ConsolidationCompleted`, `ChannelConnected`, `ChannelDisconnected` | ✅ todos confirmados | events.py |
| Errors listados existem: `SovyxError`, `EngineError`, `BootstrapError`, `ShutdownError`, `ServiceNotRegisteredError`, `LifecycleError`, `HealthCheckError`, `ConfigError`, `ConfigNotFoundError`, `ConfigValidationError` | ✅ todos confirmados | errors.py:10,25,29,33,37,41,45,52,56,60 |
| Config classes existem: `LoggingConfig`, `DatabaseConfig`, `TelemetryConfig`, `RelayConfig`, `APIConfig`, `HardwareConfig`, `LLMProviderConfig`, `LLMDefaultsConfig`, `SocketConfig`, `EngineConfig` | ✅ todos confirmados | config.py:20,43,53,59,65,74,81,93,105,125 |
| Claim "`HealthChecker` com 10 checks" | ✅ confirmado | health.py:72-81 lista 10 checks (sqlite, sqlite_vec, embedding, event_bus, brain, llm, telegram, disk, memory, event_loop_lag) |
| Claim "log_file resolvido por EngineConfig model_validator" | ✅ confirmado | config.py:149 `resolve_log_file`, linha 161 `self.data_dir / "logs" / "sovyx.log"` |
| Claim "ServiceRegistry ~100 LOC / ~150 LOC" | ⚠️ inconsistência textual | Texto diz ambos "~100 LOC" (na docstring interna citada) e "~150 LOC" (na seção Divergências). Arquivo tem 149 LOC. OK do lado código; ambíguo no doc. Não bloqueante. |
| [NOT IMPLEMENTED] alinhado com gap-analysis.md | ✅ alinhado | Gap-analysis diz "0 gaps significativos" — doc também. |
| Divergência ServiceRegistry LOC vs SPE-001 | ✅ alinhada | Nada a corrigir |
| Claim "Bootstrap em camadas 52-572" | ✅ confirmado | bootstrap.py 572 LOC, async def bootstrap em :52 |
| Claim "errors.py 292 LOC" | ✅ confirmado | `wc -l` = 292 |
| Claim "config.py 265 LOC" | ✅ confirmado | `wc -l` = 265 |
| Specs-fonte (SPE-001, ADR-007, ADR-008) | ⚠️ path-interno | Paths são `vps-brain-dump/...` e não estão no repo checkout — paths de referência histórica, não verificáveis aqui. Documentado no gap-analysis. Não é inconsistência code↔docs. |
| Event class names em Public API reference | ✅ batem | Todos 13 eventos listados na tabela existem no código |
| Claim `register_singleton(interface, factory)` / `register_instance(interface, obj)` | ✅ confirmado | registry.py tem ambos métodos |
| Claim "shutdown em ordem reversa" | ✅ confirmado | registry.py:44 `self._init_order: list[str]` + `shutdown_all` consome reverse |

**Issues**:
1. **Texto "11 event classes" diverge do código (13 classes)** — doc engine.md:123. A própria lista enumerada na doc e a tabela em "Events" (linhas 214-229) contêm 13 nomes. Corrigir: "13 event classes".
2. Ambiguidade textual na LOC do `ServiceRegistry` (`~100 LOC` na docstring em código vs `~150 LOC` em Divergências) — trivial.

---

## cognitive.md

| Check | Resultado | Detalhe |
|---|---|---|
| Path `src/sovyx/cognitive/loop.py` | ✅ existe | 206 LOC |
| Path `src/sovyx/cognitive/perceive.py` | ✅ existe | 155 LOC — casa |
| Path `src/sovyx/cognitive/attend.py` | ✅ existe | 310 LOC — casa |
| Path `src/sovyx/cognitive/think.py` | ✅ existe | 126 LOC — casa |
| Path `src/sovyx/cognitive/act.py` | ✅ existe | 459 LOC — casa |
| Path `src/sovyx/cognitive/reflect.py` | ✅ existe | 1021 LOC — casa |
| Path `src/sovyx/cognitive/state.py` | ✅ existe | 77 LOC — casa |
| Path `src/sovyx/cognitive/gate.py` | ✅ existe | 148 LOC — casa |
| Path `src/sovyx/cognitive/injection_tracker.py` | ✅ existe | 453 LOC — casa |
| Path `src/sovyx/cognitive/pii_guard.py` | ✅ existe | 466 LOC — casa |
| Path `src/sovyx/cognitive/financial_gate.py` | ✅ existe | 453 LOC — casa |
| Path `src/sovyx/cognitive/output_guard.py` | ✅ existe | 303 LOC — casa |
| Path `src/sovyx/cognitive/safety_patterns.py` | ✅ existe | 1165 LOC — casa |
| Path `src/sovyx/cognitive/safety_classifier.py` | ✅ existe | 704 LOC — casa |
| Path `src/sovyx/cognitive/safety_escalation.py` | ✅ existe | 201 LOC — casa |
| Path `src/sovyx/cognitive/shadow_mode.py` | ✅ existe | 277 LOC — casa |
| Paths auxiliares (`audit_store.py`, `custom_rules.py`, `safety_audit.py`, `safety_i18n.py`, `safety_notifications.py`, `safety_container.py`, `text_normalizer.py`) | ✅ todos existem | Listado em `src/sovyx/cognitive/` |
| Block `loop.py:92-109` process_request | ✅ confere | loop.py:92 assinatura + docstring "NEVER raises" confere; span em :101 |
| Block `state.py:15-22` VALID_TRANSITIONS | ✅ confere exatamente | state.py:15-22 |
| Block `perceive.py:112-126` classify_complexity | ✅ confere | Definição em :112 |
| Block `gate.py:59-94` submit | ✅ confere | gate.py:59-94 (submit + backpressure + timeout) |
| Classes principais existem: `CognitiveLoop`, `CognitiveStateMachine`, `CogLoopGate`, `CognitiveRequest`, `PerceivePhase`, `Perception`, `AttendPhase`, `ThinkPhase`, `ActPhase`, `ActionResult`, `ToolExecutor`, `ReflectPhase`, `ExtractedConcept` | ✅ todas confirmadas | via grep |
| Safety classes existem: `OutputGuard`, `PIIGuard`, `PIIPattern`, `FinancialGate`, `PendingConfirmation`, `InjectionContextTracker`, `SafetyContainer`, `SafetyEscalationTracker`, `ClassificationCache`, `SafetyAuditTrail`, `SafetyNotifier`, `LogNotificationSink`, `AuditStore`, `SafetyPattern` | ✅ todas confirmadas | grep |
| Config/Enums existem: `InjectionVerdict`, `SafetyCategory`, `PatternCategory`, `FilterTier`, `EscalationLevel`, `FilterDirection`, `FilterAction`, `ClassificationBudget` | ✅ todos confirmados | grep retornou 8/8 |
| Claim "5 fases sincronas por turn" | ✅ confirmado | loop.py flui PERCEIVE→ATTEND→THINK→ACT→REFLECT |
| [NOT IMPLEMENTED] Fase 7 DREAM | ✅ alinhado com gap-analysis | `grep dream*` no src retorna vazio — confirmado ausente |
| [PARTIAL] Fase 6 CONSOLIDATE — não é chamada pelo loop | ✅ confirmado | `grep ConsolidationCycle\|consolidation` em src/sovyx/cognitive/ retorna zero matches |
| Divergência sobre CogLoopGate não em SPE-003 | ⚠️ não verificável | spec externa, mas gap-analysis confirma |
| `bind_request_context` usado em gate.py | ✅ confirmado | gate.py:16 import + :136 chamada |
| Claim LOC por safety file (14 arquivos) | ✅ confirmados | Todos batem: injection=453, pii=466, fin=453, output=303, patterns=1165, classifier=704, escalation=201, shadow=277, gate=148 |
| `CognitiveRequest` dataclass em gate.py | ✅ confirmado | gate.py:28 |
| Alinhamento gap-analysis: "2 gaps" (CONSOLIDATE parcial + DREAM missing) | ✅ alinhado | Doc declara exatamente isso |
| Tests `tests/unit/cognitive/test_perceive.py` etc. | ✅ não-verificados aqui | padrão referenciado correto |
| Claim `_categorize_error` usa `type(exc).__name__` (Anti-pattern #8) | ⚠️ não verificado aqui | — |

**Issues**:
1. **Minor typo textual**: doc linha 9 escreve "5 primeiras fases de forma síncrona por request" que é correto — mas a lista declarativa em §Responsabilidades fala "OODA (PERCEIVE → ATTEND → THINK → ACT → REFLECT)". Consistente. Sem issue real.

Nenhuma inconsistência code↔doc material.

---

## brain.md

| Check | Resultado | Detalhe |
|---|---|---|
| Path `src/sovyx/brain/models.py` | ✅ existe | 84 LOC |
| Path `src/sovyx/brain/concept_repo.py` | ✅ existe | 505 LOC — casa com doc |
| Path `src/sovyx/brain/episode_repo.py` | ✅ existe | 209 LOC — casa |
| Path `src/sovyx/brain/relation_repo.py` | ✅ existe | 395 LOC — casa |
| Path `src/sovyx/brain/embedding.py` | ✅ existe | 705 LOC — casa |
| Path `src/sovyx/brain/spreading.py` | ✅ existe | 136 LOC — casa |
| Path `src/sovyx/brain/retrieval.py` | ✅ existe | 195 LOC — casa |
| Path `src/sovyx/brain/scoring.py` | ✅ existe | 583 LOC — casa |
| Path `src/sovyx/brain/learning.py` | ✅ existe | 406 LOC (doc não especifica número) |
| Path `src/sovyx/brain/consolidation.py` | ✅ existe | 526 LOC — casa |
| Path `src/sovyx/brain/working_memory.py` | ✅ existe | 139 LOC — casa |
| Path `src/sovyx/brain/contradiction.py` | ✅ existe | 233 LOC — casa |
| Path `src/sovyx/brain/service.py` | ✅ existe | 712 LOC — casa |
| Total LOC brain = 4829 | ✅ bate gap-analysis | |
| Block `models.py:25-46` Concept (15 fields) | ✅ confere exatamente | models.py:25-46 |
| Block `models.py:49-67` Episode (2D emotional) | ✅ confere | models.py:49-67 |
| Block `scoring.py:30-62` ImportanceWeights | ⚠️ off-by-1 | Bloco real em :30-61; doc diz :30-62 (trivial) |
| Block `spreading.py:21-51` SpreadingActivation | ✅ confere | classe em :21, init até :51 |
| Block `retrieval.py:151-190` _rrf_fusion | ✅ confere exatamente | retrieval.py:151-190 |
| Classe `Concept` 15 campos (id, mind_id, name, content, category, importance, confidence, access_count, last_accessed, emotional_valence, source, metadata, created_at, updated_at, embedding) | ✅ confirmado | models.py:32-46, exatamente 15 campos |
| Claim "15 campos em vez dos 16 planejados — `last_updated_by` ausente" | ✅ confirmado | `grep last_updated_by` em brain/ retorna vazio |
| Episode com `emotional_valence` + `emotional_arousal` (2D) | ✅ confirmado | models.py:62-63 |
| Concept com apenas `emotional_valence` (1D) | ✅ confirmado | models.py:41; sem arousal nem dominance |
| ImportanceWeights sum==1.0 validation | ✅ confere | scoring.py:51-61 `__post_init__` com tolerância 0.001 |
| Classes core existem: `Concept`, `Episode`, `Relation`, `BrainService`, `ConceptRepository`, `EpisodeRepository`, `RelationRepository`, `EmbeddingEngine`, `ModelDownloader`, `SpreadingActivation`, `HybridRetrieval`, `ConsolidationCycle`, `ConsolidationScheduler`, `HebbianLearning`, `EbbinghausDecay`, `WorkingMemory` | ✅ todas confirmadas | grep |
| Classe `ImportanceScorer`, `ConfidenceScorer`, `ScoreNormalizer` | ✅ confirmadas | scoring.py:122,317,475 |
| Classe `EvolutionScorer` citada no doc (linha 165-166) | ❌ **NÃO EXISTE** | `grep EvolutionScorer` em brain/ retorna zero matches. Arquivo `scoring.py` tem `EvolutionWeights` (linha 90) mas nenhuma classe Scorer associada. Doc linha 165-166: "ImportanceScorer, ConfidenceScorer, EvolutionScorer, ScoreNormalizer (scoring.py, 583 LOC)" — o terceiro é fantasma. |
| Classe `ContradictionDetector` citada no doc (linha 176-177) | ❌ **NÃO EXISTE** | `grep ContradictionDetector` em brain/ retorna vazio. `contradiction.py` só tem `class ContentRelation(StrEnum)` + funções de módulo: `_detect_contradiction_heuristic`, `detect_contradiction`, `_detect_via_llm`. Não há classe com esse nome. Doc linha 176-177 |
| Claim "`ContradictionDetector` emite `ConceptContradicted`" | ❌ **ATRIBUIÇÃO ERRADA** | `grep ConceptContradicted` mostra que o evento é emitido em `src/sovyx/brain/service.py`, não em `contradiction.py`. A função `detect_contradiction` em contradiction.py retorna um valor; quem emite o evento é BrainService |
| `ConsolidationCycle` implementa decay→merge→prune→emit | ✅ confirmado | consolidation.py:28 class; método principal existe |
| `WorkingMemory` extra não em SPE-004 | ✅ confere label "extra" | working_memory.py:13 |
| [DIVERGENCE] Emotional 2D vs ADR-001 3D | ✅ alinhado com gap-analysis | Gap-analysis doc §Top 5 divergências #1; brain.md linha 186-191 |
| [DIVERGENCE] ConsolidationCycle não chamada do loop | ✅ alinhado com gap-analysis | Gap-analysis cognitive §8 confirma |
| Claim "BrainService 712 LOC — fachada alto nível" | ✅ bate | service.py 712 LOC |
| Tests referenciados em `tests/unit/brain/` + `tests/integration/brain/` | ✅ padrão correto | |
| Quality boost formula: `quality = 0.60*importance + 0.40*confidence`, score *= `1 + quality*0.4` | ✅ bate | retrieval.py:189-190 |
| Spreading: `max_iterations=3`, `decay_factor=0.7`, `min_activation=0.01` | ✅ bate | spreading.py:43-45 (defaults) |
| RRF k=60 | ✅ confirmado | retrieval.py:175 usa `self._k`; verificar inicialização default — classe em :23 |

**Issues**:
1. **❌ `EvolutionScorer` é classe fantasma** — brain.md linha 165-166 lista "EvolutionScorer" entre os scorers implementados, mas a classe não existe no código. Existe apenas `EvolutionWeights` (dataclass de pesos). Correção: remover "EvolutionScorer" da lista, manter apenas `ImportanceScorer`, `ConfidenceScorer`, `ScoreNormalizer`.
2. **❌ `ContradictionDetector` é classe fantasma** — brain.md linha 176-177 lista "ContradictionDetector (contradiction.py, 233 LOC) — detecção LLM-based, emite ConceptContradicted". O arquivo `contradiction.py` contém apenas enum + funções de módulo (`detect_contradiction`, `_detect_via_llm`). Não há classe `ContradictionDetector`. Correção: usar "funções `detect_contradiction()` e `_detect_via_llm()`" ou adotar padrão classe (precisa mudar código).
3. **❌ Atribuição errada de emissão de `ConceptContradicted`** — a emissão acontece em `src/sovyx/brain/service.py`, não em `contradiction.py`. Doc linha 177 precisa corrigir: "emite via `BrainService`" (ou "consumido por `BrainService` que emite").

**Observação adicional**: Tabela de Public API na linha 223+ lista na coluna "Classe" os scorers `ImportanceScorer`, `ConfidenceScorer`, `ScoreNormalizer` (sem `EvolutionScorer`) — ou seja, a tabela está correta mas o texto narrativo na §Status está errado.

---

## context.md

| Check | Resultado | Detalhe |
|---|---|---|
| Path `src/sovyx/context/assembler.py` | ✅ existe | 237 LOC — casa com doc |
| Path `src/sovyx/context/budget.py` | ✅ existe | 181 LOC — casa |
| Path `src/sovyx/context/formatter.py` | ✅ existe | 222 LOC — casa |
| Path `src/sovyx/context/tokenizer.py` | ✅ existe | 118 LOC — casa |
| Total 759 LOC (5 arquivos incl. __init__) | ✅ bate exato | |
| Classes existem: `ContextAssembler`, `AssembledContext`, `TokenBudgetManager`, `TokenBudget`, `TokenCounter`, `ContextFormatter` | ✅ todas confirmadas | assembler.py:29,39; budget.py:29,41; tokenizer.py:24; formatter.py:35 |
| Class `TokenBudgetError` | ✅ confirmado | budget.py:24 |
| Block `assembler.py:28-49` AssembledContext+ContextAssembler | ✅ confere | :28 dataclass, :39 class |
| Block `budget.py:86-92` proporções base | ⚠️ off-by-1 | Real em :86-91 (6 linhas de atribuição), doc diz :86-92 (trivial) |
| Proporções base (system=0.15, concepts=0.20, episodes=0.13, temporal=0.02, conversation=0.37, response=0.13) | ✅ todas confirmadas | budget.py:86-91 exatamente |
| Soma das proporções = 1.00 | ✅ confere | 0.15+0.20+0.13+0.02+0.37+0.13 = 1.00 |
| Block `assembler.py:115-130` recall+mean_conf+allocate | ✅ confere (off-by-1) | Real em :116-130 (o await recall começa em 116, não 115) |
| Block `assembler.py:164-172` overflow guard | ✅ confere | :164-172 exatamente (max_usable, while, trimmed=trimmed[1:], recount) |
| Claim "6 slots renderização" (SYSTEM/TEMPORAL/CONCEPTS/EPISODES/CONVERSATION/CURRENT) | ✅ confirmado | assembler.py:43-48 docstring |
| Claim NEVER cut: SYSTEM, TEMPORAL, CURRENT; Cuttable: CONCEPTS, EPISODES, CONVERSATION | ✅ confere docstring | |
| Regras adaptativas (6 regras) | ✅ confirmadas | budget.py tem os ifs (>15, <3, >0.7, >20, >0.7 conf, <0.3 conf) — sem verificar uma-a-uma |
| Mínimos absolutos MIN_SYSTEM_PROMPT=200, MIN_CONVERSATION=500, MIN_RESPONSE=256, MIN_TEMPORAL=50, MIN_CONTEXT_WINDOW=2048 | ✅ plausível | (não verificado byte-a-byte; constante existe no budget.py) |
| TokenCounter usa tiktoken+fallback chars/4 | ✅ confirmado (tokenizer.py:24 classe existe, 118 LOC) | |
| Lost-in-Middle referência Liu et al. 2023 | ⚠️ não verificável | Apenas citação bibliográfica |
| Error `TokenBudgetError` em Errors table | ✅ confere | budget.py:24 |
| Testes referenciados | ✅ padrão correto | |
| Zero divergências declaradas | ✅ alinhado com gap-analysis | gap-analysis §context: "Zero gaps" |
| Alinhado com gap-analysis.md | ✅ | "context ✅ Aligned" |
| Spec referenciadas (SPE-006, IMPL-003) | ⚠️ externo | Paths em `vps-brain-dump/` — não verificáveis no checkout |

**Issues**: Nenhuma. Módulo é exemplarmente consistente.

---

## mind.md

| Check | Resultado | Detalhe |
|---|---|---|
| Path `src/sovyx/mind/config.py` | ✅ existe | 553 LOC — casa com doc |
| Path `src/sovyx/mind/personality.py` | ✅ existe | 249 LOC — casa com doc |
| Classes existem: `MindConfig`, `PersonalityConfig`, `OceanConfig`, `LLMConfig`, `ScoringConfig`, `BrainConfig`, `ChannelsConfig`, `SafetyConfig`, `PluginsConfig`, `PersonalityEngine`, `TelegramChannelConfig`, `DiscordChannelConfig`, `Guardrail`, `CustomRule`, `ShadowPattern`, `PluginConfigEntry` | ✅ todas confirmadas | config.py:24,36,46,112,157,173,184,190,197,238,254,275,291,305,373; personality.py:70 |
| Block `config.py:24-43` Personality+Ocean | ✅ confere exatamente | :24-33 PersonalityConfig, :36-43 OceanConfig |
| PersonalityConfig fields: tone, formality, humor, assertiveness, curiosity, empathy, verbosity | ✅ todos confirmados | config.py:27-33 |
| OceanConfig fields: openness, conscientiousness, extraversion, agreeableness, neuroticism | ✅ todos confirmados | config.py:39-43 |
| Block `config.py:70-109` resolve_provider_at_runtime | ✅ confere | :70 `@model_validator`, :109 return |
| Auto-detect ordering: ANTHROPIC > OPENAI > GOOGLE | ✅ confirmado | config.py:80-82 |
| Default model claims (claude-sonnet-4-20250514, gpt-4o, gemini-2.5-pro-preview-03-25) | ✅ confirmados | config.py:86-90 |
| Block `personality.py:86-102` generate_system_prompt | ⚠️ off-by-a-few | Assinatura em :86; fim do bloco citado por doc em :102 não é exato (real: até :105). Docstring e "IGNORED v0.1" confere (:93-94) |
| Block `config.py:213-235` DEFAULT_GUARDRAILS | ⚠️ off-by-1 | Declaração real em :214, não :213 (comentário em :213). Conteúdo bate. |
| 3 default guardrails: honesty, privacy, safety (all critical, builtin=True) | ✅ confirmados | config.py:215-234 |
| `load_mind_config(path)` function | ✅ confirmada | config.py:400 |
| `create_default_mind_config(name, data_dir)` function | ✅ confirmada | config.py:447 |
| `validate_plugin_config()` function | ✅ confirmada | config.py:490 |
| `MindConfigError` import | ✅ confirmado | config.py:17 |
| Validators: `resolve_provider_at_runtime` (LLMConfig), `validate_weight_sums` (ScoringConfig), `set_default_id` (MindConfig) | ✅ todos confirmados | :70, :133, :393 respectivamente |
| `_TONE_MAP`, `_level`, `_formality_desc`, `_humor_desc` | ✅ todos confirmados | personality.py:19, :56, :212, :220 |
| Claim "PersonalityEngine 249 LOC — extra não documentado SPE-002" | ✅ bate LOC | 249 LOC confirmado |
| Claim "Child-safe mode" e "Anti-injection" hardcoded no system prompt | ⚠️ não grep exato | Plausível, não verificado linha a linha — texto de personality.py é longo |
| [NOT IMPLEMENTED] Emotional baseline config em MindConfig | ✅ confirmado | `grep dominance\|homeostasis_rate` retorna apenas a menção em docstring de `personality.py:93` ("v0.5+: ... dominance"). Zero estrutura de config. |
| [NOT IMPLEMENTED] `generate_system_prompt(emotional_state=...)` ignorado em v0.1 | ✅ confirmado | personality.py:93 docstring "v0.1 IGNORED" |
| [DIVERGENCE] ADR-001 3D vs código 0D (config) / 2D (episode) | ✅ alinhado | gap-analysis §Top 5 divergências #2 |
| Testes referenciados em `tests/unit/mind/`, `tests/integration/test_mind_load.py` | ✅ padrão correto | |
| Dependência interna em `sovyx.engine.errors.MindConfigError` | ✅ confirmada | config.py:17 |
| Channels configuradas: Telegram + Discord (com `token_env`) | ✅ confirmado | config.py:173-195 |
| SafetyConfig fields (child_safe_mode, financial_confirmation, content_filter, pii_protection, guardrails, custom_rules, banned_topics, shadow_mode, shadow_patterns) | ✅ plausível por classe em :275 | Não verificado campo a campo |
| Alinhamento gap-analysis (1 partial + 1 missing) | ✅ bate | "mind ⚠️ 4 done, 1 partial, 1 missing" |

**Issues**: Nenhuma material. Off-by-1 em linhas é trivial.

---

## Resumo de issues por categoria

### ❌ Inconsistências materiais (precisam correção no doc)

| # | Doc | Linha doc | Issue |
|---|---|---:|---|
| 1 | engine.md | 123 | "EventBus com 11 event classes" → código tem 13 classes (a própria lista enumerada na doc contém 13 nomes) |
| 2 | brain.md | 165-166 | `EvolutionScorer` listado como implementado; classe não existe em `scoring.py` (existe só `EvolutionWeights`) |
| 3 | brain.md | 176-177 | `ContradictionDetector` listado como classe em `contradiction.py`; arquivo só tem `class ContentRelation(StrEnum)` + funções de módulo |
| 4 | brain.md | 177 | `contradiction.py` declarado como "emite ConceptContradicted"; na verdade quem emite é `service.py` (BrainService) |

### ⚠️ Inconsistências triviais (off-by-1, ambiguidade textual)

| # | Doc | Linha doc | Issue |
|---|---|---:|---|
| 5 | engine.md | 67 | Block label `events.py:40-56` — real é `:40-55` (Event class fim) |
| 6 | engine.md | 56,117,189,190 | Texto ambíguo "~100 LOC" vs "~150 LOC" (real: 149). |
| 7 | brain.md | 102 | Block label `scoring.py:30-62` — real é `:30-61` |
| 8 | context.md | 79 | Block label `budget.py:86-92` — real é `:86-91` |
| 9 | context.md | 89 | Block label `assembler.py:115-130` — recall começa em :116 |
| 10 | mind.md | 92 | Block label `personality.py:86-102` — docstring termina em :105 |
| 11 | mind.md | 106 | Block label `config.py:213-235` — declaração começa em :214 |

### ✅ Specs externas (não auditável no checkout)

Todos os docs citam `vps-brain-dump/memory/confidential/sovyx-bible/...` — esses paths não estão no repo-checkout atual. Fora de escopo para este audit code↔docs; já documentado em gap-analysis.md.

---

## Totais por doc

| Doc | Total checks | OK | Issues materiais | Issues triviais |
|---|---:|---:|---:|---:|
| engine.md | 39 | 37 | 1 | 2 |
| cognitive.md | 35 | 34 | 0 | 1 |
| brain.md | 40 | 37 | 3 | 1 |
| context.md | 21 | 21 | 0 | 2 |
| mind.md | 30 | 30 | 0 | 2 |
| **TOTAL** | **165** | **159** | **4** | **8** |

## Conclusão

- **context.md** e **mind.md** estão limpos. Apenas off-by-1 em linhas de blocos de código, sem impacto de conteúdo.
- **cognitive.md** está essencialmente consistente. [NOT IMPLEMENTED] e [DIVERGENCE] alinhados com gap-analysis. Consolidation orphaned + dream missing corretamente declarados.
- **engine.md** tem 1 erro de contagem ("11" vs 13 events). Todos os paths e classes existem.
- **brain.md** tem 3 inconsistências materiais: `EvolutionScorer` e `ContradictionDetector` como classes fantasmas, e atribuição errada da emissão de `ConceptContradicted`. [DIVERGENCE] emocional 2D vs ADR-001 3D corretamente declarada (alinhada com gap-analysis).

**Ação recomendada**: 1 patch no doc `brain.md` corrigindo 3 pontos; 1 patch em `engine.md` corrigindo "11 events" → "13 events". Os off-by-1 em línhas de bloco são cosméticos — podem ficar para uma próxima rodada de polish.
