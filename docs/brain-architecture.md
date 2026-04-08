# Brain Architecture

Sovyx's brain subsystem implements a knowledge graph with Hebbian learning,
working memory decay, and spreading activation. This document covers the
architecture decisions made during the brain hardening effort (v0.5.11).

## Overview

```
Message → ReflectPhase → learn_concept() → encode_episode()
                              |                    |
                         ConceptRepo          BrainService
                         (FTS5 dedup)              |
                              |              ┌─────┴──────┐
                              v              v            v
                        WorkingMemory   strengthen()  strengthen_star()
                        (activate 0.5)  (within-turn)  (cross-turn)
                              |              |            |
                              v              v            v
                         decay_all()    RelationRepo  RelationRepo
                        (after reflect) (get_or_create) (canonical)
```

## Canonical Relation Ordering

**Problem:** Relations `A->B` and `B->A` stored as separate rows, causing
duplicate edges and inflated graph metrics.

**Solution:** All relations stored with `min(source, target)` as `source_id`.

```python
def _canonical_order(a, b):
    return (a, b) if str(a) <= str(b) else (b, a)
```

Applied in:
- `RelationRepository.create()`
- `RelationRepository.get_or_create()`
- `RelationRepository.increment_co_occurrence()`

Migration v3 merges existing duplicates: sum co_occurrence, max weight.

## Star Topology Hebbian Learning

**Problem:** O(n^2) all-pairs Hebbian with a hard cap at 20 concepts. When
total concepts exceeded 20, newest were dropped. New concepts only linked
to each other, creating isolated islands.

**Solution:** Star topology with three pairing layers:

| Layer | Pairs | Scaling | Creates edges? |
|-------|-------|---------|----------------|
| Within-turn | new x new | O(n^2) on 3-8 concepts | Yes |
| Cross-turn | new x top-K existing | O(n * K), K=15 | Yes |
| Existing | pre-existing only | O(edges found) | No |

```python
await hebbian.strengthen_star(
    new_ids=concept_ids_from_this_turn,
    existing_ids=previously_active_concepts,
    activations=working_memory_activations,
    k=15,
)
```

**Key insight:** Layer 3 (existing reinforcement) only strengthens
pre-existing relations. It never creates new edges between old concepts
that happen to both be in working memory. This prevents spurious
connections between unrelated concepts.

## Working Memory Decay

**Problem:** Without decay, all concepts accumulate equal activation.
Star topology's top-K selection becomes arbitrary. Also, the dedup path
in `learn_concept()` called `record_access()` but not
`working_memory.activate()`, leaving re-mentioned concepts at decayed
activation.

**Solution:**

- `decay_rate = 0.15` applied after every reflect phase
- Activation curve: 0.50 -> 0.43 -> 0.36 -> 0.31 -> 0.26 -> 0.22 (5 turns)
- Dedup path adds `self._memory.activate(concept.id, 0.5)` to refresh
- `WorkingMemory.activate()` uses `max(current, activation)` — won't
  overwrite spreading-boosted concepts

## Graph API

**Problem:** Fixed cap at `limit * 3` dropped edges from small graphs.
Query only checked `source_id IN (...)`, missing edges where the concept
was in `target_id`. No guarantee that every node had at least one edge.

**Solution:**

1. **ORDER BY weight DESC** — strongest edges returned first
2. **Bidirectional query** — `WHERE source_id IN (...) OR target_id IN (...)`
3. **Dynamic cap** — `nodes * 30` for <500 nodes (58 nodes = 1,740 cap)
4. **Orphan audit** — after building links, find nodes with 0 edges,
   rescue via top-3 relations from RelationRepository

## Testing

### Unit Tests
- `test_relation_repo.py` — canonical ordering, dedup, migration merge
- `test_learning.py` — star topology (connectivity BFS, custom K, scaling)
- `test_loop.py` — decay called after reflect, error resilience
- `test_brain.py` — orphan audit, dynamic cap, bidirectional query

### Integration Tests
- `test_brain_connectivity.py` — 8-message conversation with real SQLite:
  - Single connected component (BFS verification)
  - Zero bidirectional duplicates
  - Concept reuse maintains connectivity
  - Decay doesn't isolate early concepts
  - Linear relation scaling (< quadratic max)

### Regression
All connectivity tests marked `@pytest.mark.no_islands`.
CI runs them on every push.
