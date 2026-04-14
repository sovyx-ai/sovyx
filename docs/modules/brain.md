# Módulo: brain

## Objetivo

`sovyx.brain` é o sistema de memória do Sovyx: mantém conhecimento
semântico (Concepts), episódios conversacionais (Episodes) e o grafo de
relações (Relations) sobre SQLite + sqlite-vec. Implementa três pilares
cognitivos — Spreading Activation (Collins & Loftus 1975), Hebbian
learning, Ebbinghaus forgetting — unidos por um BrainService que
concentra recall, retrieval híbrido e scoring multi-sinal. O módulo é a
camada de estado persistente da cognição: toda fase Reflect grava aqui,
toda fase Think consulta aqui.

## Responsabilidades

- Modelar `Concept`, `Episode`, `Relation` como pydantic v2.
- Persistir em 3 repositórios async (ConceptRepository, EpisodeRepository,
  RelationRepository) sobre `DatabasePool` (WAL + sqlite-vec).
- Gerar embeddings locais (ONNX Runtime) para concepts e episodes.
- Implementar Hybrid Retrieval: semantic (KNN via sqlite-vec) + keyword
  (FTS5) combinados por Reciprocal Rank Fusion (k=60), com fallback para
  FTS5-only quando sqlite-vec ausente.
- Spreading activation sobre grafo de relações com working memory em RAM.
- Scoring: importance multi-sinal (5 signals), confidence (4 signals),
  evolution (5 signals) para reescoragem durante consolidação.
- Consolidation cycle: decay → merge similares → prune abaixo de
  threshold → emit `ConsolidationCompleted`.
- Hebbian learning: strengthen/create relações quando concepts
  co-ocorrem; star topology cross-turn (K=15).
- Contradiction detection: `contradiction.py` detecta novo conteúdo que
  contradiz concept existente → emit `ConceptContradicted`.

## Arquitetura

Mapping mental → arquivo:

- Hipocampo → `episode_repo.py`
- Neocórtex → `concept_repo.py`
- Córtex pré-frontal → `working_memory.py` (RAM, star topology)
- Amígdala → tagging emocional em `Episode.emotional_valence/arousal`
- Cerebelo → `scoring.py` (calibração fina)

`BrainService` (712 LOC) expõe API de alto nível: `recall()`,
`encode_episode()`, `extract_concepts()`, `strengthen_connection()`,
`decay_working_memory()`. Chamadas do cognitive loop vão somente por
aqui.

`HybridRetrieval` executa KNN e FTS5 em paralelo, aplica RRF, então aplica
um boost por qualidade:

```
quality = 0.60 × importance + 0.40 × confidence
score = rrf_score × (1 + quality × 0.4)   # até 40% boost
```

Relevância (matching textual) continua primária; qualidade modula.

`SpreadingActivation` itera 3 vezes com `decay_factor = 0.7`; ativações
convergem ou são cortadas por `min_activation = 0.01`. Ativações somam
em nós com múltiplos caminhos convergentes — intencional, pois são scores
de ranking, não probabilidades.

## Código real

```python
# src/sovyx/brain/models.py:25-46 — Concept model (15 campos)
class Concept(BaseModel):
    id: ConceptId = Field(default_factory=lambda: ConceptId(generate_id()))
    mind_id: MindId
    name: str
    content: str = ""
    category: ConceptCategory = ConceptCategory.FACT
    importance: float = Field(default=0.5, ge=0.0, le=1.0)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    access_count: int = 0
    last_accessed: datetime | None = None
    emotional_valence: float = Field(default=0.0, ge=-1.0, le=1.0)
    source: str = "conversation"
    metadata: dict[str, object] = Field(default_factory=dict)
    created_at: datetime = ...
    updated_at: datetime = ...
    embedding: list[float] | None = None
```

```python
# src/sovyx/brain/models.py:49-67 — Episode model (2D emotional)
class Episode(BaseModel):
    id: EpisodeId = Field(default_factory=lambda: EpisodeId(generate_id()))
    mind_id: MindId
    conversation_id: ConversationId
    user_input: str
    assistant_response: str
    summary: str | None = None
    importance: float = Field(default=0.5, ge=0.0, le=1.0)
    emotional_valence: float = Field(default=0.0, ge=-1.0, le=1.0)
    emotional_arousal: float = Field(default=0.0, ge=-1.0, le=1.0)
    concepts_mentioned: list[ConceptId] = Field(default_factory=list)
    ...
```

```python
# src/sovyx/brain/scoring.py:30-62 — ImportanceWeights (soma == 1.0)
@dataclass(frozen=True, slots=True)
class ImportanceWeights:
    category_base: float = 0.15
    llm_assessment: float = 0.35
    emotional: float = 0.10
    novelty: float = 0.15
    explicit_signal: float = 0.25

    def __post_init__(self) -> None:
        total = (self.category_base + self.llm_assessment + self.emotional
                 + self.novelty + self.explicit_signal)
        if abs(total - 1.0) > 0.001:
            raise ValueError(f"ImportanceWeights must sum to 1.0, got {total:.4f}")
```

```python
# src/sovyx/brain/spreading.py:21-51 — Collins & Loftus 1975
class SpreadingActivation:
    """Activation spreads from seed concepts to neighbors via relations.
    neighbor.activation += concept.activation × relation.weight × decay_factor
    Repeat for max_iterations=3 with decay_factor=0.7, threshold=0.01.
    """
```

```python
# src/sovyx/brain/retrieval.py:151-190 — RRF fusion + quality boost
def _rrf_fusion(self, fts_results, vec_results, limit):
    # RRF: 1/(k + rank), k=60
    # quality = 0.60*importance + 0.40*confidence, up to 40% boost
    for cid in scores:
        quality = 0.60 * concept.importance + 0.40 * concept.confidence
        scores[cid] *= 1.0 + quality * 0.4
```

## Specs-fonte

- `SOVYX-BKD-SPE-004-BRAIN-MEMORY.md` — Concept/Episode/Relation models,
  repositórios, consolidação.
- `SOVYX-BKD-IMPL-002-BRAIN-ALGORITHMS.md` — Spreading Activation, RRF,
  Hebbian formula, Ebbinghaus curve.
- `SOVYX-BKD-ADR-001-EMOTIONAL-MODEL.md` — decisão PAD 3D (valence,
  arousal, dominance) com homeostasis_rate.
- Research citado: Collins & Loftus (1975), Anderson ACT-R, PNAS 2022
  (consolidation model).

## Status de implementação

### ✅ Implementado

- `Concept` (15 campos em vez dos 16 planejados — `last_updated_by`
  ausente mas equivalente via `metadata`).
- `Episode` com `emotional_valence` + `emotional_arousal` (2D).
- `Relation` com source/target/type/weight + co_occurrence_count +
  last_activated.
- `ConceptRepository` (505 LOC), `EpisodeRepository` (209 LOC),
  `RelationRepository` (395 LOC) — todos async sobre pool.
- `EmbeddingEngine` (705 LOC) — ONNX Runtime, preload em bootstrap via
  `ensure_loaded()`.
- `SpreadingActivation` (`spreading.py`, 136 LOC): `activate()`,
  `activate_from_text()` com seed activation `0.5 + 0.5*importance`.
- `HybridRetrieval` (`retrieval.py`, 195 LOC): RRF com k=60, fallback
  FTS5-only quando sqlite-vec indisponível.
- `ImportanceScorer`, `ConfidenceScorer`, `EvolutionScorer`,
  `ScoreNormalizer` (`scoring.py`, 583 LOC). Weights configuráveis via
  `mind.yaml` (`BrainConfig.scoring` — ver módulo `mind`).
- `HebbianLearning` (`learning.py`): `strengthen()` dentro de turn,
  `strengthen_star()` cross-turn com K=15.
- `EbbinghausDecay` (`learning.py`): curva `retention = e^(-t/τ)`.
- `ConsolidationCycle` (`consolidation.py`, 526 LOC): decay → merge
  (FTS5 + Levenshtein) → prune → emit `ConsolidationCompleted` com
  metrics (merged, pruned, strengthened, duration_s).
- `WorkingMemory` (`working_memory.py`, 139 LOC) — cache prefrontal em
  RAM com decay temporal (**extra, não documentado em SPE-004**).
- `ContradictionDetector` (`contradiction.py`, 233 LOC) — detecção
  LLM-based, emite `ConceptContradicted`.
- `BrainService` (`service.py`, 712 LOC) — fachada de alto nível.

### ❌ [NOT IMPLEMENTED]

- Emotional dimension 3D (dominance). Ver Divergências.

## Divergências [DIVERGENCE]

- [DIVERGENCE] **Emotional model**: ADR-001 §2 decide "Option D: PAD Core
  (3D)" com valence + arousal + **dominance** + homeostasis_rate. Código
  implementa 2D: `Episode.emotional_valence` e `Episode.emotional_arousal`.
  `Concept` tem apenas `emotional_valence` (1D). Impacto: tagging,
  consolidation weighting, context assembly e modulação de personalidade
  perdem a dimensão de dominância. Migration schema necessária para v1.0.
- [DIVERGENCE] `ConsolidationCycle` existe mas o CognitiveLoop não a
  invoca. SPE-003 trata consolidation como fase 6 contínua; o código
  depende de `ConsolidationScheduler` (job de background), o que é
  semanticamente equivalente mas o mapping doc↔código é confuso.

## Dependências

- **Externas**: `pydantic`, `aiosqlite`, `sqlite-vec` (opcional, fallback
  FTS5), `onnxruntime`.
- **Internas**: `sovyx.persistence.pool.DatabasePool`, `sovyx.engine.types`
  (IDs e `ConceptCategory`, `RelationType`), `sovyx.engine.events.EventBus`,
  `sovyx.observability.{logging,metrics,tracing}`.

## Testes

- `tests/unit/brain/` — um arquivo por componente (test_models,
  test_concept_repo, test_episode_repo, test_relation_repo,
  test_spreading, test_retrieval, test_scoring, test_learning,
  test_consolidation, test_working_memory, test_contradiction,
  test_service).
- `tests/integration/brain/` — end-to-end com SQLite + FTS5 + sqlite-vec.
- `tests/property/brain/` — property tests com Hypothesis em
  `SpreadingActivation` (convergência, não-crescimento ilimitado) e
  `scoring` (weight sum == 1.0).
- Fixtures: `brain_pool` com tmp SQLite + migrations aplicadas.

## Referências

- Code: `src/sovyx/brain/models.py`, `src/sovyx/brain/concept_repo.py`,
  `src/sovyx/brain/episode_repo.py`, `src/sovyx/brain/relation_repo.py`,
  `src/sovyx/brain/embedding.py`, `src/sovyx/brain/spreading.py`,
  `src/sovyx/brain/retrieval.py`, `src/sovyx/brain/scoring.py`,
  `src/sovyx/brain/learning.py`, `src/sovyx/brain/consolidation.py`,
  `src/sovyx/brain/working_memory.py`, `src/sovyx/brain/contradiction.py`,
  `src/sovyx/brain/service.py`.
- Specs: `SOVYX-BKD-SPE-004-BRAIN-MEMORY.md`,
  `SOVYX-BKD-IMPL-002-BRAIN-ALGORITHMS.md`,
  `SOVYX-BKD-ADR-001-EMOTIONAL-MODEL.md`.
- Gap analysis: `docs/_meta/gap-inputs/analysis-A-core.md` §brain.
