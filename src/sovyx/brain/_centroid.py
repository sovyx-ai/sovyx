"""Category centroid cache helpers — extracted from BrainService.

Pure helpers operating on an externally-owned ``centroid_cache`` mutable
mapping. BrainService still owns the cache (so its lifetime matches the
service); these functions only encapsulate the population/invalidation
logic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sovyx.brain._novelty import COLD_START_THRESHOLD, CentroidCache
from sovyx.observability.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from sovyx.brain.concept_repo import ConceptRepository
    from sovyx.brain.embedding import EmbeddingEngine
    from sovyx.engine.types import MindId

logger = get_logger(__name__)


async def refresh_centroid_cache(
    mind_id: MindId,
    *,
    embedding: EmbeddingEngine,
    concepts: ConceptRepository,
    centroid_cache: CentroidCache,
) -> int:
    """Pre-compute and cache category centroids from current embeddings.

    Called by consolidation after score recalculation. Replaces stale
    centroids so subsequent ``compute_novelty`` calls use fresh cluster
    centers without per-call DB round-trips.

    Returns:
        Number of categories cached.
    """
    if not embedding.has_embeddings:
        return 0

    categories = await concepts.get_categories(mind_id)
    cached = 0

    for cat in categories:
        try:
            count = await concepts.count_by_category(mind_id, cat)
            if count < COLD_START_THRESHOLD:
                continue

            embeddings = await concepts.get_embeddings_by_category(
                mind_id,
                cat,
                limit=500,
            )
            if not embeddings:
                continue

            centroid = await embedding.compute_category_centroid(embeddings)
            centroid_cache[(str(mind_id), cat)] = centroid
            cached += 1
        except Exception:  # noqa: BLE001
            logger.debug(
                "centroid_cache_refresh_failed",
                category=cat,
                exc_info=True,
            )

    logger.info(
        "centroid_cache_refreshed",
        mind_id=str(mind_id),
        categories_cached=cached,
        total_categories=len(categories),
    )
    return cached


def invalidate_centroid_cache(
    centroid_cache: CentroidCache,
    mind_id: MindId | None = None,
) -> None:
    """Clear centroid cache (all or for a specific mind)."""
    if mind_id is None:
        centroid_cache.clear()
        return

    mind_str = str(mind_id)
    keys_to_remove = [k for k in centroid_cache if k[0] == mind_str]
    for k in keys_to_remove:
        del centroid_cache[k]
