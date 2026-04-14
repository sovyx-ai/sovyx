# Brain Graph

## Objetivo

O **Brain** é a memória persistente da Mind, modelada como **grafo cognitivo** em SQLite. Três modelos core (`Concept`, `Episode`, `Relation`) populam um grafo com 5 regiões neurológicas simuladas e algoritmos inspirados em neurociência cognitiva (spreading activation, Hebbian learning, Ebbinghaus decay, consolidation).

Fonte: `vps-brain-dump/memory/confidential/sovyx-bible/backend/specs/SOVYX-BKD-SPE-004-BRAIN-MEMORY.md` (109KB, maior spec do repo), `.../SOVYX-BKD-IMPL-002-BRAIN-ALGORITHMS.md`.

## Modelos core

### `Concept` — Neocortex (semantic memory)

Unidade atômica de conhecimento: fato, preferência, entidade, skill, crença, relação.

```python
class Concept(BaseModel):
    id: ConceptId
    mind_id: MindId
    name: str
    content: str = ""
    category: ConceptCategory = ConceptCategory.FACT  # FACT|ENTITY|PREFERENCE|SKILL|BELIEF|EVENT|RELATION
    importance: float = Field(default=0.5, ge=0.0, le=1.0)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    access_count: int = 0
    last_accessed: datetime | None = None
    emotional_valence: float = Field(default=0.0, ge=-1.0, le=1.0)  # ⚠️ 1D apenas
    source: str = "conversation"
    metadata: dict[str, object] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime
    embedding: list[float] | None = None  # 384d E5-small-v2
```

### `Episode` — Hippocampus (autobiographic memory)

Um turn de conversa ou evento memorável.

```python
class Episode(BaseModel):
    id: EpisodeId
    mind_id: MindId
    conversation_id: ConversationId
    user_input: str
    assistant_response: str
    summary: str | None = None
    importance: float = Field(default=0.5, ge=0.0, le=1.0)
    emotional_valence: float = Field(default=0.0, ge=-1.0, le=1.0)   # ⚠️ só 2D
    emotional_arousal: float = Field(default=0.0, ge=-1.0, le=1.0)
    concepts_mentioned: list[ConceptId] = Field(default_factory=list)
    metadata: dict[str, object] = Field(default_factory=dict)
    created_at: datetime
    embedding: list[float] | None = None
```

### `Relation` — synaptic connection

Aresta entre dois conceitos. Weight é a força sináptica; co-occurrence habilita Hebbian strengthening.

```python
class Relation(BaseModel):
    id: RelationId
    source_id: ConceptId
    target_id: ConceptId
    relation_type: RelationType = RelationType.RELATED_TO
    # RELATED_TO | IS_A | PART_OF | CAUSES | SIMILAR_TO | OPPOSITE | LOCATED_IN
    weight: float = Field(default=0.5, ge=0.0, le=1.0)
    co_occurrence_count: int = 1
    last_activated: datetime
    created_at: datetime
```

## Regiões neurológicas simuladas

Mapeamento implementado na composição do `BrainService` + serviços auxiliares:

```mermaid
flowchart TB
    subgraph BS["BrainService (src/sovyx/brain/service.py)"]
        HIP["Hippocampus<br/>EpisodeRepository<br/>Episodic memory, short→long"]
        NEO["Neocortex<br/>ConceptRepository + RelationRepository<br/>Semantic graph"]
        PFC["Prefrontal<br/>WorkingMemory<br/>Hot cache, current context"]
        AMY["Amygdala<br/>emotional_valence/arousal<br/>Memorability boost"]
        CER["Cerebellum<br/>Hebbian / procedural patterns<br/>(learning.py)"]
    end

    HIP <--> NEO: concept_mentions
    NEO <--> PFC: spreading activation
    AMY --> HIP: encoding boost
    AMY --> NEO: retrieval weighting
    CER --> NEO: relation strengthening
```

| Região | Classe Python | Arquivo | Função |
|---|---|---|---|
| Hippocampus | `EpisodeRepository` | `brain/episode_repo.py` | Armazena episodes, consolida pra neocortex |
| Neocortex | `ConceptRepository` + `RelationRepository` | `brain/concept_repo.py`, `brain/relation_repo.py` | Grafo semântico persistente |
| Prefrontal | `WorkingMemory` | `brain/working_memory.py` | Cache in-memory de conceitos ativos (spreading activation opera aqui) |
| Amygdala | Campos `emotional_*` + `ImportanceScorer` | `brain/models.py`, `brain/scoring.py` | Boost de memorabilidade emocional |
| Cerebellum | `HebbianLearning` + `EbbinghausDecay` | `brain/learning.py` | Strengthening por co-ocorrência + decay temporal |

## Spreading Activation (Collins & Loftus, 1975)

Algoritmo implementado em `brain/spreading.py`:

```python
class SpreadingActivation:
    """Activation spreads from seed concepts to neighbors via relations.

    Algorithm (IMPL-002):
        1. Seed concepts receive initial activation
        2. For each active concept, spread to neighbors:
           neighbor.activation += concept.activation × relation.weight × decay_factor
        3. Repeat for max_iterations
        4. Activation attenuated by distance (geometric decay)
        5. Threshold: concepts below min_activation are ignored
    """
```

Parâmetros default: `max_iterations=3`, `decay_factor=0.7`, `min_activation=0.01`.

**Não clampa** ativação a [0, 1] — múltiplos paths que convergem no mesmo nó **somam** ativações (ranking, não probabilidade). Opera em `WorkingMemory` + `RelationRepository`, não modifica DB.

## Hybrid Retrieval (KNN + FTS5 + RRF)

`brain/retrieval.py::HybridRetrieval` combina busca semântica (embedding via sqlite-vec) e busca lexical (FTS5), fundidas via **Reciprocal Rank Fusion** (k=60).

```python
class HybridRetrieval:
    """Combined search: semantic (KNN) + keyword (FTS5) + RRF fusion.

    Algorithm (IMPL-002 §RRF):
        1. Execute KNN search (sqlite-vec) → top-K with distance
        2. Execute FTS5 search → top-K with rank
        3. Apply RRF: score = Σ 1/(k + rank_i) for each list
           where k=60 (standard RRF constant)
        4. Merge and sort by RRF score DESC
        5. Return top-N

    Fallback: if sqlite-vec unavailable, uses FTS5 only.
    """
```

Embeddings gerados por `EmbeddingEngine` (`brain/embedding.py`) com **E5-small-v2** (384 dimensões, ONNX Runtime).

## Scoring (multi-signal)

`brain/scoring.py` — dois eixos ortogonais:

### ImportanceWeights — "quanto isso importa?"

```python
@dataclass(frozen=True, slots=True)
class ImportanceWeights:
    category_base: float = 0.15     # Per-category baseline (entity=0.80, fact=0.60, ...)
    llm_assessment: float = 0.35    # LLM-assessed importance
    emotional: float = 0.10         # |valence| boost
    novelty: float = 0.15           # Semantic distance from existing knowledge
    explicit_signal: float = 0.25   # User said "remember that"
    # Must sum to 1.0 (validated in __post_init__)
```

### ConfidenceWeights — "quanto posso confiar?"

```python
@dataclass(frozen=True, slots=True)
class ConfidenceWeights:
    source_quality: float = 0.35    # LLM > regex > heuristic
    llm_assessment: float = 0.30    # LLM self-assessed certainty
    explicitness: float = 0.20      # Directly stated vs inferred
    content_richness: float = 0.15  # Length/detail as quality proxy
```

Floor de 0.05 (nunca chega a 0); pruning threshold em consolidation é 0.01.

Classes: `ImportanceScorer`, `ConfidenceScorer`, `ScoreNormalizer`, `EvolutionWeights` (re-scoring durante consolidation).

## Consolidation (Ebbinghaus decay + merge + prune)

`brain/consolidation.py::ConsolidationCycle`:

1. **Ebbinghaus decay**: importance decay exponencial com tempo desde `last_accessed`:
   `new_importance = old × exp(-t/tau)` com `tau` calibrado por categoria.
   Emotional boost: `emotional_boost = |valence| × 0.6 + arousal × 0.4` — episódios intensos resistem mais ao decay (per ADR-001).
2. **Merge**: conceitos com embedding similarity > threshold E nomes relacionados são fundidos (preserva o de maior importance+confidence).
3. **Prune**: conceitos com `importance < 0.01` e `access_count == 0` após grace period são removidos.

`ConsolidationScheduler` orquestra runs periódicas.

**[NOT CALLED]** Ver `docs/architecture/cognitive-loop.md` — loop não invoca consolidation ainda.

## Hebbian Learning

`brain/learning.py::HebbianLearning` implementa "neurons that fire together, wire together":

- Ao co-mencionar conceitos A e B no mesmo episode, `RelationRepository.strengthen(A, B)` incrementa `co_occurrence_count` e ajusta `weight` via formula sigmóide bounded.
- `EbbinghausDecay` aplica decay inverso em relations não-acessadas.

## Working Memory (prefrontal cache)

`brain/working_memory.py::WorkingMemory` — cache in-memory de conceitos ativos e suas activations correntes. Populado no início de cada turn (seed = conceitos do turn atual), usado pelo `SpreadingActivation`. Não persistido — reset a cada request.

## [DIVERGENCE] Emotional model: 2D vs PAD 3D

| Modelo | Onde aparece | Valores |
|---|---|---|
| **Implementado (2D)** | `Episode.emotional_valence`, `Episode.emotional_arousal`, `Concept.emotional_valence` (só 1D!) | `[-1.0, 1.0]` valence; `[-1.0, 1.0]` arousal |
| **Decidido em ADR-001** | Option D: **PAD 3D** (Pleasure, Arousal, **Dominance**) | 3 floats, adiciona `dominance ∈ [0.0, 1.0]` |

**Por que PAD 3D foi escolhido** (ADR-001 §2):
- 2D falha em distinguir pares: fear vs anger (ambos valence negativa + arousal alto, diferem por dominance), awe vs excitement, contempt vs disgust.
- Dominance mapeia diretamente em comportamento: high-D → assertive/proactive; low-D → cautious/hedging.
- +1 float por ponto — custo desprezível.

**Risco**: schema migration necessária pra v1.0. Afeta `ImportanceScorer` (emotional boost formula), `ContextAssembler` (emotional context injection), personality modulation (current: só influencia tone via OCEAN), proactive behavior triggers, TTS modulation (futuro).

**Gap #1 em `docs/_meta/gap-analysis.md` — Top divergências.**

## Diagrama do grafo

```mermaid
graph LR
    C1[Concept: "Pi 5"<br/>category=ENTITY<br/>importance=0.8]
    C2[Concept: "8GB RAM"<br/>category=FACT<br/>importance=0.6]
    C3[Concept: "Sovyx target hw"<br/>category=PREFERENCE<br/>importance=0.9]
    C4[Concept: "low power"<br/>category=FACT<br/>importance=0.5]

    E1[(Episode: "pergunta sobre hw"<br/>valence=+0.2<br/>arousal=0.3)]
    E2[(Episode: "confirma escolha"<br/>valence=+0.5<br/>arousal=0.4)]

    C1 -- PART_OF w=0.9 --> C3
    C1 -- RELATED_TO w=0.7 --> C2
    C1 -- RELATED_TO w=0.5 --> C4
    C3 -- RELATED_TO w=0.8 --> C4

    E1 -. concepts_mentioned .-> C1
    E1 -. concepts_mentioned .-> C3
    E2 -. concepts_mentioned .-> C3
```

Legenda: caixas = Concepts (neocortex), pills = Episodes (hippocampus), arestas sólidas = Relations, arestas pontilhadas = concept_mentions no episode.

## Persistência

- `brain.db` por Mind — concepts, episodes, relations, embeddings, FTS5 virtual tables
- `conversations.db` separado (tamanho, export diferenciado)
- Pragmas non-negotiable (ADR-004): WAL, synchronous=NORMAL, foreign_keys=ON, etc.
- Extensão `sqlite-vec` carregada para KNN
- 1 writer + N readers

## Rastreabilidade

### Docs originais
- `vps-brain-dump/.../specs/SOVYX-BKD-SPE-004-BRAIN-MEMORY.md` (109KB)
- `.../specs/SOVYX-BKD-IMPL-002-BRAIN-ALGORITHMS.md` (40KB — spreading, Hebbian, RRF, Ebbinghaus)
- `.../adrs/SOVYX-BKD-ADR-001-EMOTIONAL-MODEL.md` (PAD 3D decision)
- `.../adrs/SOVYX-BKD-ADR-004-DATABASE-STACK.md` (SQLite + sqlite-vec + FTS5)
- `.../specs/SOVYX-BKD-IMPL-014-EMOTIONAL-MODEL.md` (mapping discreto → PAD)

### Código-fonte
- `src/sovyx/brain/models.py` — `Concept`, `Episode`, `Relation`
- `src/sovyx/brain/service.py` — `BrainService` facade
- `src/sovyx/brain/concept_repo.py` / `episode_repo.py` / `relation_repo.py` — persistência
- `src/sovyx/brain/embedding.py` — `EmbeddingEngine` (E5-small-v2 ONNX) + `ModelDownloader`
- `src/sovyx/brain/spreading.py` — `SpreadingActivation` (Collins & Loftus)
- `src/sovyx/brain/retrieval.py` — `HybridRetrieval` (KNN + FTS5 + RRF)
- `src/sovyx/brain/scoring.py` — `ImportanceWeights`, `ConfidenceWeights`, `EvolutionWeights`, scorers
- `src/sovyx/brain/learning.py` — `HebbianLearning`, `EbbinghausDecay`
- `src/sovyx/brain/consolidation.py` — `ConsolidationCycle`, `ConsolidationScheduler`
- `src/sovyx/brain/working_memory.py` — `WorkingMemory` prefrontal cache
- `src/sovyx/brain/contradiction.py` — `ContentRelation` enum (contradiction detection)

### Gap analysis
- `docs/_meta/gap-inputs/analysis-A-core.md` §brain
- `docs/_meta/gap-analysis.md` — divergência #1 (2D vs PAD 3D)
