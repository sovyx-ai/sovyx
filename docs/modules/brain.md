# Module: brain

## What it does

The `sovyx.brain` package is the memory system. It stores semantic knowledge (concepts), conversation history (episodes), and their relations as a graph, all on SQLite with sqlite-vec. It exposes a single `BrainService` used by the cognitive loop for recall and encoding.

## Key classes

| Name | Responsibility |
|---|---|
| `BrainService` | High-level facade. The only brain surface used by the cognitive loop. |
| `ConceptRepository` / `EpisodeRepository` / `RelationRepository` | Async CRUD + search on `DatabasePool`. |
| `EmbeddingEngine` | Generates embeddings via ONNX Runtime (E5-small-v2 by default). |
| `HybridRetrieval` | KNN (sqlite-vec) + FTS5 combined by Reciprocal Rank Fusion. |
| `SpreadingActivation` | Propagates activation from seed concepts through relations. |
| `HebbianLearning` | Strengthens connections between co-activated concepts. |
| `EbbinghausDecay` | Forgetting curve applied during consolidation. |
| `WorkingMemory` | RAM cache of currently active concepts with temporal decay. |
| `ConsolidationCycle` | Decay, merge similars, prune, emit `ConsolidationCompleted`. |
| `ImportanceScorer` / `ConfidenceScorer` / `ScoreNormalizer` | Multi-signal scoring. |

## Models

Pydantic v2 models in `src/sovyx/brain/models.py`.

### Concept

| Field | Type | Notes |
|---|---|---|
| `id` | `ConceptId` | Generated. |
| `mind_id` | `MindId` | Owning mind. |
| `name` | `str` | Short label. |
| `content` | `str` | Full text. |
| `category` | `ConceptCategory` | Default `FACT`. |
| `importance` | `float` | 0.0–1.0. |
| `confidence` | `float` | 0.0–1.0. |
| `access_count` | `int` | Incremented on recall. |
| `last_accessed` | `datetime | None` | |
| `emotional_valence` | `float` | -1.0–1.0. |
| `source` | `str` | Default `"conversation"`. |
| `metadata` | `dict[str, object]` | Free-form. |
| `created_at` / `updated_at` | `datetime` | UTC. |
| `embedding` | `list[float] | None` | Lazy. |

### Episode

| Field | Type | Notes |
|---|---|---|
| `id` | `EpisodeId` | |
| `mind_id` / `conversation_id` | ID types | |
| `user_input` / `assistant_response` | `str` | |
| `summary` | `str | None` | |
| `importance` | `float` | 0.0–1.0. |
| `emotional_valence` | `float` | -1.0–1.0. |
| `emotional_arousal` | `float` | -1.0–1.0. |
| `concepts_mentioned` | `list[ConceptId]` | |
| `metadata` | `dict[str, object]` | |
| `created_at` | `datetime` | |
| `embedding` | `list[float] | None` | |

### Relation

| Field | Type | Notes |
|---|---|---|
| `id` | `RelationId` | |
| `source_id` / `target_id` | `ConceptId` | |
| `relation_type` | `RelationType` | Default `RELATED_TO`. |
| `weight` | `float` | 0.0–1.0. Hebbian. |
| `co_occurrence_count` | `int` | Default 1. |
| `last_activated` / `created_at` | `datetime` | |

## BrainService.recall

```python
# src/sovyx/brain/service.py
async def recall(
    self,
    query: str,
    mind_id: MindId,
) -> tuple[list[tuple[Concept, float]], list[Episode]]:
    """Full recall: concepts (with scores) + episodes + spreading.

    Returns (concepts_with_scores, episodes).
    Scores are needed for ContextAssembler Lost-in-Middle ordering.
    """
    concepts = await self.search(query, mind_id)
    episodes_with_scores = await self._retrieval.search_episodes(query, mind_id)
    episodes = [ep for ep, _ in episodes_with_scores]
    return concepts, episodes
```

## Hybrid retrieval

`HybridRetrieval` runs KNN (sqlite-vec) and FTS5 in parallel, fuses the ranks with Reciprocal Rank Fusion, then modulates by quality:

```
rrf_score = sum(1 / (k + rank))       # k = 60
quality   = 0.60 * importance + 0.40 * confidence
score     = rrf_score * (1 + quality * 0.4)   # up to +40%
```

Relevance stays primary; quality only modulates. If sqlite-vec is unavailable, retrieval falls back to FTS5-only (see `engine.md` — degradation fallbacks).

## Spreading activation

`SpreadingActivation` (Collins & Loftus 1975) seeds activation on recalled concepts and propagates through relations:

```
neighbor.activation += concept.activation * relation.weight * decay_factor
```

Defaults: `max_iterations = 3`, `decay_factor = 0.7`, `min_activation = 0.01`. Activation sums at nodes with multiple converging paths — intentional, since these are ranking scores, not probabilities.

## Scoring weights

Weight dataclasses enforce `sum == 1.0`:

```python
# src/sovyx/brain/scoring.py
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

`ConfidenceWeights` (4 signals) and `EvolutionWeights` (5 signals, used in consolidation reweighting) follow the same contract.

## Hebbian learning

`HebbianLearning.strengthen()` raises the weight on relations whose concepts co-activated inside one turn. `strengthen_star()` links all concepts of one turn to the previous turn's last concept in a star topology with `K = 15` maximum neighbors per turn. Weights are bounded in `[0.0, 1.0]`.

## Ebbinghaus decay

`EbbinghausDecay` applies `retention = e^(-t/tau)` to concept importance during consolidation, where `t` is time since `last_accessed` and `tau` is a per-concept strength parameter boosted by `access_count`. Reinforcement on recall resets the clock.

## Consolidation cycle

`ConsolidationCycle.run()` does:

1. Apply Ebbinghaus decay to importance.
2. Merge semantically similar concepts (FTS5 candidates + Levenshtein confirmation).
3. Prune concepts below the importance threshold.
4. Emit `ConsolidationCompleted` with `merged`, `pruned`, `strengthened`, `duration_s`.

`ConsolidationScheduler` runs it periodically (configured via `BrainConfig.consolidation_interval_hours`, default 6 h).

## Dream cycle (v0.11.6)

`DreamCycle.run()` is the seventh phase of the cognitive loop (SPE-003 §1.1, "nightly: discover patterns"). Lives in `brain/dream.py` rather than `cognitive/dream.py` because it is an operation over brain state, not a per-turn cognitive phase.

Per run:

1. Fetch episodes in `BrainConfig.dream_lookback_hours` (default 24 h) via `EpisodeRepository.get_since`.
2. Short-circuit if fewer than 3 episodes — not enough signal for *recurring* themes.
3. One LLM call extracts up to `BrainConfig.dream_max_patterns` recurring themes (default 5).
4. Each pattern becomes a `Concept` with `source="dream:pattern"`, `category=BELIEF`, and `confidence=0.4` (lifts via access-driven reinforcement).
5. Concepts that appear in ≥2 distinct episodes are fed to `HebbianLearning.strengthen` with attenuated activation (0.5) — cross-episode is a weaker signal than within-turn. Capped at 12 concepts per run to bound the within-pair O(n²) cost.
6. Emit `DreamCompleted` with `patterns_found, concepts_derived, relations_strengthened, episodes_analyzed, duration_s`.

`DreamScheduler` is a separate background task wired alongside `ConsolidationScheduler`. It sleeps until the next `dream_time` (HH:MM, default `02:00`) in the mind's timezone, with ±15 min jitter. Survives cycle exceptions (logged, not bubbled). Kill-switch: `dream_max_patterns: 0` causes bootstrap to skip registration entirely — zero runtime overhead when disabled.

## Events

| Event | Emitted when |
|---|---|
| `ConceptCreated` | A concept is stored. |
| `EpisodeEncoded` | An episode is persisted. |
| `ConceptContradicted` | New content contradicts an existing concept (via `detect_contradiction` utility). |
| `ConceptForgotten` | A concept is pruned by consolidation. |
| `ConsolidationCompleted` | A consolidation cycle finished. |
| `DreamCompleted` | A nightly DREAM run finished (v0.11.6). |

## Configuration

```yaml
brain:
  consolidation:
    interval_hours: 24
    prune_threshold: 0.1
  retrieval:
    rrf_k: 60
  spreading:
    max_iterations: 3
    decay_factor: 0.7
    min_activation: 0.01
  scoring:
    importance:
      category_base: 0.15
      llm_assessment: 0.35
      emotional: 0.10
      novelty: 0.15
      explicit_signal: 0.25
```

## Roadmap

- 3D emotional model (valence / arousal / dominance) with homeostasis rate.
- Consolidate phase invoked from the cognitive loop, not only from the scheduler.
- Pluggable embedding backends beyond ONNX Runtime.

## See also

- `cognitive.md` — how `BrainService` is called from Reflect and Think
- `engine.md` — layered bootstrap that constructs the brain
- `../architecture.md` — memory in the end-to-end flow
