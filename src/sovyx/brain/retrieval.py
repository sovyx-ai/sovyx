"""Sovyx hybrid retrieval — KNN + FTS5 + Reciprocal Rank Fusion.

Combines vector similarity search and keyword search for optimal recall.
Falls back to FTS5-only when sqlite-vec is unavailable.
"""

from __future__ import annotations

import sqlite3
import time
from typing import TYPE_CHECKING

from sovyx.engine.errors import EmbeddingError, SearchError
from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sovyx.brain.concept_repo import ConceptRepository
    from sovyx.brain.embedding import EmbeddingEngine
    from sovyx.brain.episode_repo import EpisodeRepository
    from sovyx.brain.models import Concept, Episode
    from sovyx.engine.types import MindId

logger = get_logger(__name__)


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

    def __init__(
        self,
        concept_repo: ConceptRepository,
        episode_repo: EpisodeRepository,
        embedding_engine: EmbeddingEngine,
        k_constant: int = 60,
    ) -> None:
        self._concepts = concept_repo
        self._episodes = episode_repo
        self._embedding = embedding_engine
        self._k = k_constant

    async def search_concepts(
        self,
        query: str,
        mind_id: MindId,
        limit: int = 10,
    ) -> list[tuple[Concept, float]]:
        """Search concepts by text using hybrid retrieval.

        Args:
            query: Search query.
            mind_id: Mind to search in.
            limit: Max results to return.

        Returns:
            List of (concept, rrf_score) sorted by score DESC.
        """
        started_at = time.monotonic()
        logger.info(
            "brain.search.started",
            **{
                "brain.k": limit,
                "brain.filter": "concepts",
                "brain.query_len": len(query),
            },
        )

        fts_results = await self._concepts.search_by_text(query, mind_id, limit=limit * 2)

        vec_results: list[tuple[Concept, float]] = []
        if self._embedding.has_embeddings and self._concepts._pool.has_sqlite_vec:
            try:
                query_emb = await self._embedding.encode(query, is_query=True)
                vec_results = await self._concepts.search_by_embedding(
                    query_emb, mind_id, limit=limit * 2
                )
            except (EmbeddingError, SearchError, sqlite3.Error, ValueError):
                # EmbeddingError / SearchError: typed subsystem failures
                # (model not loaded, bad input). sqlite3.Error: DB issue
                # during the vec0 MATCH query. ValueError: embedding-
                # shape mismatch. All fall through to FTS5-only —
                # full-text search still works without the semantic
                # ranking. Traceback preserved for the debug log.
                logger.debug("vector_search_failed_using_fts_only", exc_info=True)

        if not vec_results:
            # FTS5-only fallback: convert FTS rank to RRF-like score
            # Apply quality boost from importance + confidence
            fts_only: list[tuple[Concept, float]] = []
            for rank_pos, (concept, _) in enumerate(fts_results):
                base = 1.0 / (self._k + rank_pos + 1)
                quality = 0.60 * concept.importance + 0.40 * concept.confidence
                boosted = base * (1.0 + quality * 0.4)  # Up to 40% boost
                fts_only.append((concept, boosted))
            fts_only.sort(key=lambda x: x[1], reverse=True)
            results = fts_only[:limit]
            self._emit_search_completed(
                started_at=started_at,
                limit=limit,
                kind="concepts",
                results=results,
                mode="fts5_only",
            )
            return results

        merged = self._rrf_fusion(fts_results, vec_results, limit)
        self._emit_search_completed(
            started_at=started_at,
            limit=limit,
            kind="concepts",
            results=merged,
            mode="hybrid_rrf",
        )
        return merged

    async def search_episodes(
        self,
        query: str,
        mind_id: MindId,
        limit: int = 5,
    ) -> list[tuple[Episode, float]]:
        """Search episodes by text.

        Episodes use embedding search only (no FTS5 on episodes).
        Falls back to get_recent when embeddings unavailable.

        Args:
            query: Search query.
            mind_id: Mind to search in.
            limit: Max results.

        Returns:
            List of (episode, score) sorted by score DESC.
        """
        started_at = time.monotonic()
        logger.info(
            "brain.search.started",
            **{
                "brain.k": limit,
                "brain.filter": "episodes",
                "brain.query_len": len(query),
            },
        )

        if self._embedding.has_embeddings and self._episodes._pool.has_sqlite_vec:
            try:
                query_emb = await self._embedding.encode(query, is_query=True)
                vec_results = await self._episodes.search_by_embedding(
                    query_emb, mind_id, limit=limit
                )
                results = [
                    (episode, 1.0 / (self._k + rank_pos + 1))
                    for rank_pos, (episode, _) in enumerate(vec_results)
                ]
                self._emit_search_completed(
                    started_at=started_at,
                    limit=limit,
                    kind="episodes",
                    results=results,
                    mode="vector",
                )
                return results
            except (EmbeddingError, SearchError, sqlite3.Error, ValueError):
                # Same failure profile as the concept-search path above.
                # Fall through to the recency-only fallback below.
                logger.debug("episode_vector_search_failed", exc_info=True)

        # Fallback: return recent episodes
        recent = await self._episodes.get_recent(mind_id, limit=limit)
        fallback_results = [
            (episode, 1.0 / (self._k + rank_pos + 1)) for rank_pos, episode in enumerate(recent)
        ]
        self._emit_search_completed(
            started_at=started_at,
            limit=limit,
            kind="episodes",
            results=fallback_results,
            mode="recency_fallback",
        )
        return fallback_results

    @staticmethod
    def _emit_search_completed(
        *,
        started_at: float,
        limit: int,
        kind: str,
        results: Sequence[tuple[object, float]],
        mode: str,
    ) -> None:
        """Emit ``brain.search.completed`` with latency, count, and top score."""
        latency_ms = int((time.monotonic() - started_at) * 1000)
        top_score = results[0][1] if results else 0.0
        logger.info(
            "brain.search.completed",
            **{
                "brain.k": limit,
                "brain.filter": kind,
                "brain.latency_ms": latency_ms,
                "brain.result_count": len(results),
                "brain.top_score": round(float(top_score), 6),
                "brain.search_mode": mode,
            },
        )

    async def search_all(
        self,
        query: str,
        mind_id: MindId,
        concept_limit: int = 10,
        episode_limit: int = 5,
    ) -> tuple[list[tuple[Concept, float]], list[tuple[Episode, float]]]:
        """Search concepts and episodes simultaneously.

        Args:
            query: Search query.
            mind_id: Mind to search in.
            concept_limit: Max concepts.
            episode_limit: Max episodes.

        Returns:
            Tuple of (concept_results, episode_results).
        """
        concepts = await self.search_concepts(query, mind_id, concept_limit)
        episodes = await self.search_episodes(query, mind_id, episode_limit)
        return concepts, episodes

    def _rrf_fusion(
        self,
        fts_results: list[tuple[Concept, float]],
        vec_results: list[tuple[Concept, float]],
        limit: int,
    ) -> list[tuple[Concept, float]]:
        """Apply Reciprocal Rank Fusion to merge result lists.

        RRF score = Σ 1/(k + rank_i) across all lists.

        Args:
            fts_results: FTS5 results with rank.
            vec_results: Vector results with distance.
            limit: Max results to return.

        Returns:
            Merged results sorted by RRF score DESC.
        """
        scores: dict[str, float] = {}
        concept_map: dict[str, Concept] = {}

        # FTS5 results
        for rank_pos, (concept, _) in enumerate(fts_results):
            cid = str(concept.id)
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (self._k + rank_pos + 1)
            concept_map[cid] = concept

        # Vector results
        for rank_pos, (concept, _) in enumerate(vec_results):
            cid = str(concept.id)
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (self._k + rank_pos + 1)
            concept_map[cid] = concept

        # Apply quality boost from importance + confidence.
        # Important + confident concepts get up to 40% score boost,
        # but relevance (text match) remains primary ranking signal.
        for cid in scores:
            concept = concept_map[cid]
            quality = 0.60 * concept.importance + 0.40 * concept.confidence
            scores[cid] *= 1.0 + quality * 0.4

        # Sort by boosted RRF score DESC
        sorted_ids = sorted(scores, key=lambda k: scores.get(k, 0.0), reverse=True)

        return [(concept_map[cid], scores[cid]) for cid in sorted_ids[:limit]]
