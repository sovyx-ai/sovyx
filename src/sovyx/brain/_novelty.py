"""Novelty computation helpers — extracted from BrainService.

Pure functions that take the required repositories/cache/embedding engine
as arguments. BrainService's public ``compute_novelty`` method delegates
to ``compute_novelty`` here; the corresponding private helpers
(``_compute_novelty_embedding`` / ``_compute_novelty_fts5``) become thin
wrappers around the free functions below.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, MutableMapping
from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from sovyx.brain.concept_repo import ConceptRepository
    from sovyx.brain.embedding import EmbeddingEngine
    from sovyx.brain.models import Concept
    from sovyx.engine.types import MindId

logger = get_logger(__name__)

CentroidCache = MutableMapping[tuple[str, str], "list[float]"]
SearchFn = Callable[[str, "MindId", int], "Awaitable[list[tuple[Concept, float]]]"]


# Tuned thresholds — kept here so service.py and the helpers share one source.
COLD_START_THRESHOLD = 10
COLD_START_NOVELTY = 0.70

# Calibrated similarity -> novelty mapping.
_HIGH_SIM = 0.85
_LOW_SIM = 0.30


async def compute_novelty_embedding(
    text: str,
    category: str,
    mind_id: MindId,
    *,
    embedding: EmbeddingEngine,
    concepts: ConceptRepository,
    centroid_cache: CentroidCache,
) -> float:
    """Compute novelty via embedding cosine distance from the category centroid.

    High cosine similarity to centroid means low novelty (concept is "in
    the neighborhood" of known knowledge); low similarity means high
    novelty (concept is far from the cluster).

    Calibrated mapping:
        sim >= 0.85   -> novelty 0.05 (near-duplicate)
        sim ~  0.60   -> novelty 0.50 (moderately novel)
        sim <= 0.30   -> novelty 0.95 (very novel)
    """
    from sovyx.brain.embedding import EmbeddingEngine

    new_embedding = await embedding.encode(text, is_query=True)

    cache_key = (str(mind_id), category)
    centroid = centroid_cache.get(cache_key)

    if centroid is None:
        category_embeddings = await concepts.get_embeddings_by_category(
            mind_id,
            category,
            limit=500,
        )
        if not category_embeddings:
            return COLD_START_NOVELTY

        centroid = await embedding.compute_category_centroid(category_embeddings)
        centroid_cache[cache_key] = centroid

    similarity = EmbeddingEngine.cosine_similarity(new_embedding, centroid)

    if similarity >= _HIGH_SIM:
        return 0.05
    if similarity <= _LOW_SIM:
        return 0.95
    # Linear interpolation: (0.30, 0.95) -> (0.85, 0.05)
    t = (similarity - _LOW_SIM) / (_HIGH_SIM - _LOW_SIM)
    novelty = 0.95 - t * 0.90
    return max(0.05, min(1.0, novelty))


async def compute_novelty_fts5(
    text: str,
    mind_id: MindId,
    *,
    search_fn: SearchFn,
) -> float:
    """Compute novelty via FTS5 text search (fallback when embeddings unavailable).

    Uses the existing search() pipeline. High match score = low novelty.
    Less precise than embeddings but always available.
    """
    try:
        matches = await search_fn(text, mind_id, 3)
    except Exception:  # noqa: BLE001
        return COLD_START_NOVELTY

    if not matches:
        return 1.0

    best_concept, best_score = matches[0]
    if best_concept.name.lower() == text.lower():
        return 0.05
    return max(0.05, 1.0 - min(1.0, best_score * 1.5))
