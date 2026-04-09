# Score Tuning Guide — Importance & Confidence

Sovyx uses two orthogonal score axes to prioritize and filter knowledge:

- **Importance** — "How much does this matter?" (0.05–1.0)
- **Confidence** — "How much can we trust it?" (0.05–1.0)

## Weight Groups

All weights within a group sum to 1.0 (validated by Pydantic).

### Importance Weights (LLM path)

| Weight | Default | Controls | Increase when… | Decrease when… |
|--------|---------|----------|----------------|----------------|
| `llm_weight` | 0.35 | LLM-assessed importance | LLM quality is high; want nuance | LLM is unreliable or expensive |
| `category_weight` | 0.15 | Category baseline (entity=0.80, fact=0.50…) | Domain has clear priority categories | All categories equally important |
| `emotion_weight` | 0.10 | Emotional valence signal | Emotional memory matters (personal AI) | Factual/professional context |
| `novelty_weight` | 0.15 | Embedding cosine distance from centroid | Want to boost new/unique info | Knowledge base is sparse |
| `explicit_weight` | 0.25 | "Remember this" / explicit user signal | User trust is high | Users rarely use explicit signals |

### Importance Weights (Regex fallback)

| Weight | Default | Controls |
|--------|---------|----------|
| `category_weight` | 0.60 | Category baseline |
| `novelty_weight` | 0.40 | Embedding novelty |

### Confidence Weights

| Weight | Default | Controls | Increase when… | Decrease when… |
|--------|---------|----------|----------------|----------------|
| `source_weight` | 0.35 | Source reliability (explicit > implicit > inferred) | Source quality varies a lot | Single high-quality source |
| `llm_weight` | 0.30 | LLM-assessed confidence | LLM calibration is good | LLM overconfident |
| `explicitness_weight` | 0.20 | How explicit the statement was | Explicit facts matter more | Implicit context is reliable |
| `richness_weight` | 0.15 | Content length/detail | Longer = more reliable in your domain | Brevity doesn't indicate uncertainty |

## Recalculation Weights (Consolidation)

| Weight | Default | Controls |
|--------|---------|----------|
| `degree_weight` | 0.25 | Graph connectivity (centrality) |
| `access_weight` | 0.20 | How often concept is accessed |
| `recency_weight` | 0.25 | Time since last access |
| `emotion_weight` | 0.15 | Emotional valence |
| `graph_weight` | 0.15 | Average relation weight |

## Velocity & Safety

| Parameter | Default | Purpose |
|-----------|---------|---------|
| `max_delta` | 0.10 | Maximum score change per consolidation cycle |
| `soft_ceiling` | 0.90 | 70% damping above this threshold |
| `floor` | 0.05 | Minimum score (above pruning threshold 0.01) |
| `staleness_half_life` | 30.0 | Days until confidence halves from staleness |

## Example: mind.yaml Override

```yaml
scoring:
  importance_weights:
    llm_weight: 0.40      # Trust LLM more
    category_weight: 0.10  # Categories less important
    emotion_weight: 0.10
    novelty_weight: 0.15
    explicit_weight: 0.25  # Keep explicit high
  confidence_weights:
    source_weight: 0.40    # Source reliability matters more
    llm_weight: 0.25
    explicitness_weight: 0.20
    richness_weight: 0.15
  recalculation_weights:
    degree_weight: 0.30    # Boost connected concepts
    access_weight: 0.15
    recency_weight: 0.25
    emotion_weight: 0.15
    graph_weight: 0.15
```

> ⚠️ Each weight group **must sum to 1.0** — Pydantic validation rejects invalid configs at startup.

## Observability

- **Score distribution entropy** is computed during consolidation.
  - Entropy > 1.5: healthy (scores well-spread)
  - Entropy < 1.5: WARNING (scores concentrating)
  - Entropy < 1.0: CRITICAL (distribution collapsed)
- **Feedback counters** in concept metadata:
  - `retrieval_hit_count`: search appearances
  - `context_inclusion_count`: LLM context inclusions
- Use these to identify over-represented or under-utilized concepts.

## Score Flow Diagram

```
User Message
    │
    ├─ LLM Extraction ──→ importance (LLM + category + emotion + novelty + explicit)
    │                  ──→ confidence (source + LLM + explicitness + richness)
    │
    ├─ Regex Fallback ──→ importance (category + novelty)
    │                  ──→ confidence (source baseline)
    │
    ├─ Dedup ──→ SAME: confidence += diminishing boost
    │        ──→ EXTENDS: confidence += boost + 0.03
    │        ──→ CONTRADICTS: confidence *= 0.60
    │
    ├─ Consolidation ──→ importance recalculation (centrality, access, recency, emotion)
    │                 ──→ confidence staleness decay (half-life 30 days)
    │                 ──→ normalization (anti-convergence, spread < 0.20 triggers)
    │
    └─ Retrieval ──→ quality factor: 0.60 * importance + 0.40 * confidence
                 ──→ budget allocation: mean_confidence adjusts concept budget ±5%
```
