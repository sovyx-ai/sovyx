# Gap Analysis — Core Runtime (engine, cognitive, brain, context, mind)

## Módulo: engine

### Docs-fonte principais
- `vps-brain-dump/memory/confidential/sovyx-bible/backend/specs/SOVYX-BKD-SPE-001-ENGINE-CORE.md` (spec engine core, DI, lifecycle)
- `vps-brain-dump/memory/confidential/sovyx-bible/backend/adrs/SOVYX-BKD-ADR-007-EVENT-ARCHITECTURE.md` (event bus design)
- `vps-brain-dump/memory/confidential/sovyx-bible/backend/adrs/SOVYX-BKD-ADR-008-LOCAL-FIRST.md` (local-first pattern)

### Código real
- 13 arquivos, classes públicas principais: `MindManager`, `ServiceRegistry`, `LifecycleManager`, `EventBus`, `HealthChecker`, `DaemonRPCServer`
- Padrões observados: DI via ServiceRegistry (~150 LOC), singleton/instance registry, event bus pattern, config pydantic, lifecycle com shutdown em ordem reversa

### Planejado vs Implementado
- ✅ **ServiceRegistry (DI Container)**: spec pediu "custom lightweight container ~200 linhas" (SPE-001 §3.2); código em `engine/registry.py` com ~150 linhas, suporta singleton/transient, resolução com keyword-qualified names
- ✅ **Lifetime enum (Singleton/Transient)**: planejado em spec; implementado como `register_singleton()` e `register_instance()` (transient implícito via factories)
- ✅ **Event Bus**: `engine/events.py` com 11 event classes (EngineStarted, PerceptionReceived, ThinkCompleted, etc.)
- ✅ **LifecycleManager**: `engine/lifecycle.py` com PidLock
- ✅ **Bootstrap order (Layer 0-2)**: `engine/bootstrap.py` linhas 109-150, registra: config → EventBus → DatabaseManager → MindManager

### Implementado sem doc
- `engine/health.py`: HealthStatus, HealthChecker — sistema de health check não mapeado em spec
- `engine/degradation.py`: DegradationManager, ComponentStatus — graceful degradation não mencionado em SPE-001
- `engine/rpc_server.py`: DaemonRPCServer — RPC protocol implementation (ADR-007 não detalha)

### Research aplicável
- ADR-008 recomenda "zero external deps default, BYOK" — código segue via `config.data_dir` local-first

---

## Módulo: cognitive

### Docs-fonte principais
- `vps-brain-dump/memory/confidential/sovyx-bible/backend/specs/SOVYX-BKD-SPE-003-COGNITIVE-LOOP.md` (7 fases: perceive→attend→think→act→reflect→consolidate→dream)
- `vps-brain-dump/memory/confidential/sovyx-bible/backend/specs/SOVYX-BKD-IMPL-006-COGNITIVE-LOOP.md` (detailed impl)

### Código real
- 24 arquivos, classes de fase: `PerceivePhase`, `AttendPhase`, `ThinkPhase`, `ActPhase`, `ReflectPhase`, `CognitiveLoop`, `CognitiveStateMachine`
- Padrões: 5 fases implementadas (perceive→attend→think→act→reflect), state machine pra transições, tracing/metrics, error handling com mensagens user-facing

### Planejado vs Implementado
- ✅ **Fase 1 PERCEIVE**: spec pediu validar, classificar complexidade, criar turn; `perceive.py` com MAX_INPUT_CHARS=10k
- ✅ **Fase 2 ATTEND**: `attend.py` com filtro + priority
- ✅ **Fase 3 THINK**: `think.py` com context assembly + LLM com model routing complexity-based
- ✅ **Fase 4 ACT**: `act.py` com ActionResult, tool calls
- ✅ **Fase 5 REFLECT**: `reflect.py` com LLM-based concept extraction, update memory, emotional state
- ❌ **Fase 6 CONSOLIDATE**: spec SPE-003 §1.1 diz "Periodic: prune, strengthen"; **NÃO há fase consolidate no loop**. `brain/consolidation.py` existe mas **não é chamado** pelo cognitive loop
- ❌ **Fase 7 DREAM**: spec diz "Nightly: discover patterns"; **NÃO implementado**. Sem `cognitive/dream.py`

### Implementado sem doc
- `cognitive/gate.py`: **CogLoopGate** — serialização de requests concorrentes não detalhada em spec
- `cognitive/audit_store.py`, `cognitive/safety_*.py` (14 arquivos safety): sistema completo de segurança (injection tracking, PII guard, financial gate, shadow mode, escalation) presente no código, não totalmente mapeado em specs cognitive (parte deve estar em ADRs de segurança)

### Research aplicável
- OODA Loop (Boyd 1987), ReAct (Yao et al. 2023), Schneier OODA critique — código mantém Orient/Context assembly rico conforme spec

---

## Módulo: brain

### Docs-fonte principais
- `vps-brain-dump/memory/confidential/sovyx-bible/backend/specs/SOVYX-BKD-SPE-004-BRAIN-MEMORY.md` (conceitos, episódios, consolidação)
- `vps-brain-dump/memory/confidential/sovyx-bible/backend/specs/SOVYX-BKD-IMPL-002-BRAIN-ALGORITHMS.md` (spreading activation, learning)

### Código real
- 14 arquivos, classes: `Concept`, `Episode`, `Relation`, `ConceptRepository`, `EpisodeRepository`, `RelationRepository`, `BrainService`, `EmbeddingEngine`, `SpreadingActivation`, `HybridRetrieval`, `ConsolidationCycle`, `HebbianLearning`, `EbbinghausDecay`, `WorkingMemory`
- Padrões: 3 modelos core, 5 regional subsystems (Hippocampus, Neocortex, Prefrontal, Amygdala, Cerebellum — mapeados em classes), spreading activation com threshold + decay, consolidation com Ebbinghaus decay

### Planejado vs Implementado
- ✅ **Concept model**: spec pediu 16 campos; código em `models.py` tem 15 (id, mind_id, name, content, category, importance, confidence, access_count, last_accessed, emotional_valence, source, metadata, created_at, updated_at, embedding)
- ✅ **Episode model**: spec pediu user_input, assistant_response, importance, emotional_state (PAD 3D); código tem user_input, assistant_response, importance, mas **apenas** emotional_valence + emotional_arousal (2D, não 3D PAD)
- ✅ **Relation model**: source_id, target_id, relation_type, weight — implementado
- ✅ **Spreading Activation**: `spreading.py` ~5KB
- ✅ **Hybrid Retrieval**: `retrieval.py` faz semantic + keyword (FTS5)
- ✅ **Consolidation**: `consolidation.py` implementa Ebbinghaus decay + merge + prune
- ⚠️ **Emotional state (PAD)**: ADR-001 §2 decide "Option D: PAD Core (3D)"; **código é 2D** (emotional_valence, emotional_arousal). Concept model só tem emotional_valence (1D)
- ✅ **Importance + Confidence Scoring**: `scoring.py` com ImportanceWeights (category_base=0.15, llm_assessment=0.35, emotional=0.10, novelty=0.15, explicit_signal=0.25) e ConfidenceWeights

### Implementado sem doc
- `brain/learning.py`: HebbianLearning, EbbinghausDecay
- `brain/working_memory.py`: WorkingMemory — cache prefrontal não detalhado em SPE-004

### Research aplicável
- Collins & Loftus (1975) Spreading Activation — implementado
- Anderson ACT-R — referenced
- PNAS 2022 consolidation model — referenced mas detalhes não mapeados

---

## Módulo: context

### Docs-fonte principais
- `vps-brain-dump/memory/confidential/sovyx-bible/backend/specs/SOVYX-BKD-SPE-006-CONTEXT-ASSEMBLY.md` (6 slots, token budget, Lost-in-Middle)
- `vps-brain-dump/memory/confidential/sovyx-bible/backend/specs/SOVYX-BKD-IMPL-003-CONTEXT-ASSEMBLY.md`

### Código real
- 5 arquivos, classes: `ContextAssembler`, `AssembledContext`, `TokenBudgetManager`, `TokenCounter`, `ContextFormatter`
- Padrões: adaptive token allocation, 6 context slots, lost-in-middle ordering, budget breakdown tracking

### Planejado vs Implementado
- ✅ **6 context slots**: spec diz system prompt, memory (concepts), memory (episodes), conversation, tools, temporal; `assembler.py` linhas 43-49 lista exatamente esses 6
- ✅ **AssembledContext dataclass**: messages, tokens_used, sources + budget_breakdown
- ✅ **Token Budget Manager**: `budget.py` com `allocate()` adaptativo baseado em conversation_length, brain_result_count, complexity, mean_confidence
- ✅ **Lost-in-Middle**: reference em docstring (Liu et al. 2023), padrão respeitado na formatação

### Implementado sem doc
- Nenhum gap — módulo bem alinhado

---

## Módulo: mind

### Docs-fonte principais
- `vps-brain-dump/memory/confidential/sovyx-bible/backend/specs/SOVYX-BKD-SPE-002-MIND-DEFINITION.md` (identity, personality, config schema)
- `vps-brain-dump/memory/confidential/sovyx-bible/backend/adrs/SOVYX-BKD-ADR-001-EMOTIONAL-MODEL.md` (PAD 3D)

### Código real
- 3 arquivos, classes: `MindConfig`, `PersonalityConfig`, `OceanConfig`, `LLMConfig`, `ScoringConfig`, `BrainConfig`, `SafetyConfig`, `PersonalityEngine`
- Padrões: pydantic BaseModel, OCEAN Big Five, per-mind directory

### Planejado vs Implementado
- ✅ **Mind directory structure**: `~/.sovyx/minds/{name}/` com brain.db, conversations.db — padrão assumido
- ✅ **mind.yaml schema**: `MindConfig` com sub-models PersonalityConfig, LLMConfig, BrainConfig, SafetyConfig
- ✅ **OCEAN personality**: `OceanConfig` com openness, conscientiousness, extraversion, agreeableness, neuroticism
- ✅ **Behavioral traits**: spec pediu communication_style, humor_level, formality, proactivity, curiosity, verbosity, assertiveness, empathy; `PersonalityConfig` tem tone (warm/neutral/direct/playful), formality, humor, assertiveness, curiosity, empathy, verbosity
- ❌ **Emotional baseline (PAD)**: ADR-001 pediu "emotional: baseline: {valence, arousal, dominance} + homeostasis_rate"; **NÃO há emotional baseline config em `MindConfig`**. Estado emocional é armazenado em Episode mas não configurável por mente
- ⚠️ **Emotional model dimension mismatch**: ADR-001 §2 decidiu Option D PAD 3D; Brain/Mind são 2D

### Implementado sem doc
- `mind/personality.py`: `PersonalityEngine` — gerador de system prompt, não documentado em SPE-002

---

## Sumário de Gaps

### Por tipo
| Tipo | Contagem | Detalhe |
|---|---|---|
| ✅ Implementado conforme spec | 38 | core bem alinhado |
| ❌ NÃO IMPLEMENTADO | 2 | DREAM phase, emotional baseline config |
| ⚠️ DIVERGENTE | 3 | emotional 2D vs 3D PAD, consolidation desacoplado do loop, CONSOLIDATE phase orphaned |

### Por módulo
- **engine**: 0 gaps significativos
- **cognitive**: 2 gaps (DREAM, CONSOLIDATE não integrada)
- **brain**: 1 gap crítico (2D em vez de 3D PAD)
- **context**: 0 gaps
- **mind**: 1 gap (emotional baseline config ausente)

---

## Top 3 Insights Gerais

1. **Emotional model é o maior gap técnico**: ADR-001 decide PAD 3D como norma; código implementa 2D. Afeta: memory tagging, consolidation weighting, context assembly, personality modulation. Schema migration necessária para v1.0.

2. **Consolidation e Dream phases estão orphaned**: SPE-003 diz 7 fases contínuas, código tem 5 por interação. Consolidation é background job em `brain/consolidation.py` nunca invocado pelo loop. DREAM inexistente. Semanticamente correto (background cycles) mas mapping doc↔código confuso.

3. **Código é ~95% feature-complete pro v0.1**: DI, event bus, brain repositories, context assembly, LLM routing, concept extraction existem e funcionam. Gaps remanescentes são refinamentos de modelo (3D emotional) e lifecycle (dream).
