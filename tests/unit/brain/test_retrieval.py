"""Tests for sovyx.brain.retrieval — hybrid retrieval with RRF."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from sovyx.brain.models import Concept, Episode
from sovyx.brain.retrieval import HybridRetrieval
from sovyx.engine.types import ConceptId, ConversationId, EpisodeId, MindId

MIND = MindId("aria")


def _concept(
    name: str,
    cid: str = "",
    importance: float = 0.5,
    confidence: float = 0.5,
) -> Concept:
    return Concept(
        id=ConceptId(cid or name),
        mind_id=MIND,
        name=name,
        importance=importance,
        confidence=confidence,
    )


def _episode(user: str, eid: str = "") -> Episode:
    return Episode(
        id=EpisodeId(eid or user),
        mind_id=MIND,
        conversation_id=ConversationId("conv1"),
        user_input=user,
        assistant_response="response",
    )


@pytest.fixture
def mock_concept_repo() -> AsyncMock:
    repo = AsyncMock()
    repo._pool = AsyncMock()
    repo._pool.has_sqlite_vec = False
    return repo


@pytest.fixture
def mock_episode_repo() -> AsyncMock:
    repo = AsyncMock()
    repo._pool = AsyncMock()
    repo._pool.has_sqlite_vec = False
    repo.get_recent = AsyncMock(return_value=[])
    return repo


@pytest.fixture
def mock_embedding() -> AsyncMock:
    engine = AsyncMock()
    engine.has_embeddings = False
    engine.encode = AsyncMock(return_value=[0.1] * 384)
    return engine


@pytest.fixture
def retrieval(
    mock_concept_repo: AsyncMock,
    mock_episode_repo: AsyncMock,
    mock_embedding: AsyncMock,
) -> HybridRetrieval:
    return HybridRetrieval(
        mock_concept_repo,
        mock_episode_repo,
        mock_embedding,
    )


class TestFTS5Only:
    """FTS5-only fallback when no sqlite-vec."""

    async def test_search_concepts_fts_only(
        self, retrieval: HybridRetrieval, mock_concept_repo: AsyncMock
    ) -> None:
        c1 = _concept("quantum physics", "c1")
        c2 = _concept("quantum computing", "c2")
        mock_concept_repo.search_by_text = AsyncMock(return_value=[(c1, -1.0), (c2, -2.0)])

        results = await retrieval.search_concepts("quantum", MIND)
        assert len(results) == 2  # noqa: PLR2004
        assert results[0][0].name == "quantum physics"
        assert results[0][1] > 0  # RRF score

    async def test_empty_results(
        self, retrieval: HybridRetrieval, mock_concept_repo: AsyncMock
    ) -> None:
        mock_concept_repo.search_by_text = AsyncMock(return_value=[])
        results = await retrieval.search_concepts("nonexistent", MIND)
        assert results == []


class TestRRFFusion:
    """Reciprocal Rank Fusion."""

    def test_rrf_basic(self) -> None:
        retrieval = HybridRetrieval(AsyncMock(), AsyncMock(), AsyncMock(), k_constant=60)
        c1 = _concept("shared", "c1")
        c2 = _concept("fts only", "c2")
        c3 = _concept("vec only", "c3")

        fts = [(c1, -1.0), (c2, -2.0)]
        vec = [(c1, 0.1), (c3, 0.5)]

        results = retrieval._rrf_fusion(fts, vec, limit=10)

        # c1 appears in BOTH lists → highest RRF score
        assert results[0][0].id == ConceptId("c1")
        assert results[0][1] > results[1][1]

    def test_rrf_respects_limit(self) -> None:
        retrieval = HybridRetrieval(AsyncMock(), AsyncMock(), AsyncMock(), k_constant=60)
        concepts = [_concept(f"c{i}", f"c{i}") for i in range(10)]
        fts = [(c, float(-i)) for i, c in enumerate(concepts)]
        vec = [(c, float(i) * 0.1) for i, c in enumerate(concepts)]

        results = retrieval._rrf_fusion(fts, vec, limit=3)
        assert len(results) == 3  # noqa: PLR2004

    def test_rrf_score_formula(self) -> None:
        """Verify RRF formula: score = Σ 1/(k + rank + 1) * quality_boost."""
        retrieval = HybridRetrieval(AsyncMock(), AsyncMock(), AsyncMock(), k_constant=60)
        c1 = _concept("test", "c1", importance=0.5, confidence=0.5)

        # c1 is rank 0 in both lists
        fts = [(c1, -1.0)]
        vec = [(c1, 0.1)]

        results = retrieval._rrf_fusion(fts, vec, limit=1)
        base_rrf = 1.0 / 61 + 1.0 / 61  # rank 0 in both
        quality = 0.60 * 0.5 + 0.40 * 0.5  # 0.5
        expected = base_rrf * (1.0 + quality * 0.4)
        assert abs(results[0][1] - expected) < 0.0001


class TestQualityBoost:
    """Importance + confidence quality boost in retrieval."""

    def test_high_quality_ranks_higher_in_rrf(self) -> None:
        """High importance+confidence concept beats equal-ranked low-quality."""
        retrieval = HybridRetrieval(AsyncMock(), AsyncMock(), AsyncMock(), k_constant=60)
        high = _concept("important", "c1", importance=0.9, confidence=0.9)
        low = _concept("trivial", "c2", importance=0.1, confidence=0.1)

        # Both at same rank position in FTS and VEC
        fts = [(high, -1.0), (low, -2.0)]
        vec = [(low, 0.1), (high, 0.5)]

        results = retrieval._rrf_fusion(fts, vec, limit=2)
        # high quality should rank first (quality boost overcomes any rank difference)
        scores = {str(c.id): s for c, s in results}
        assert scores["c1"] > scores["c2"]

    def test_quality_boost_bounded_at_40_percent(self) -> None:
        """Max quality boost is 40% (quality=1.0 → 1.4x multiplier)."""
        retrieval = HybridRetrieval(AsyncMock(), AsyncMock(), AsyncMock(), k_constant=60)
        max_q = _concept("max", "c1", importance=1.0, confidence=1.0)

        fts = [(max_q, -1.0)]
        vec = [(max_q, 0.1)]

        results = retrieval._rrf_fusion(fts, vec, limit=1)
        base_rrf = 1.0 / 61 + 1.0 / 61
        max_boosted = base_rrf * 1.4  # quality=1.0 → 40% boost
        assert results[0][1] == pytest.approx(max_boosted, abs=0.0001)

    def test_low_quality_minimal_boost(self) -> None:
        """Low importance+confidence → minimal boost."""
        retrieval = HybridRetrieval(AsyncMock(), AsyncMock(), AsyncMock(), k_constant=60)
        low = _concept("low", "c1", importance=0.0, confidence=0.0)

        fts = [(low, -1.0)]
        vec = [(low, 0.1)]

        results = retrieval._rrf_fusion(fts, vec, limit=1)
        base_rrf = 1.0 / 61 + 1.0 / 61
        # quality=0 → no boost (1.0x multiplier)
        assert results[0][1] == pytest.approx(base_rrf, abs=0.0001)

    async def test_fts_fallback_also_boosted(
        self, retrieval: HybridRetrieval, mock_concept_repo: AsyncMock
    ) -> None:
        """FTS-only fallback also applies quality boost."""
        high = _concept("important", "c1", importance=0.9, confidence=0.9)
        low = _concept("trivial", "c2", importance=0.1, confidence=0.1)
        # low has better FTS rank (position 0) but lower quality
        mock_concept_repo.search_by_text = AsyncMock(return_value=[(low, -1.0), (high, -2.0)])

        results = await retrieval.search_concepts("test", MIND)
        # After quality boost, high-quality concept should rank higher
        # despite being at worse FTS position
        assert results[0][0].id == ConceptId("c1")

    def test_relevance_still_primary(self) -> None:
        """Quality boost doesn't override large relevance differences."""
        retrieval = HybridRetrieval(AsyncMock(), AsyncMock(), AsyncMock(), k_constant=60)
        # high_quality at rank 5, low_quality at rank 0
        high = _concept("distant", "c1", importance=1.0, confidence=1.0)
        low = _concept("exact match", "c2", importance=0.1, confidence=0.1)

        # Build results where c2 appears in both lists at top, c1 only in one at bottom
        fts = [
            (low, -1.0),
            (_concept("filler1", "f1"), -2.0),
            (_concept("filler2", "f2"), -3.0),
            (_concept("filler3", "f3"), -4.0),
            (_concept("filler4", "f4"), -5.0),
            (high, -6.0),
        ]
        vec = [
            (low, 0.1),
            (_concept("filler5", "f5"), 0.2),
        ]

        results = retrieval._rrf_fusion(fts, vec, limit=10)
        # low is in BOTH lists at rank 0 → much higher base RRF
        # Even with max quality boost, high can't overcome that
        assert results[0][0].id == ConceptId("c2")


class TestHybridWithVec:
    """Hybrid search with sqlite-vec available."""

    async def test_hybrid_combines_results(
        self,
        mock_concept_repo: AsyncMock,
        mock_episode_repo: AsyncMock,
        mock_embedding: AsyncMock,
    ) -> None:
        mock_embedding.has_embeddings = True
        mock_concept_repo._pool.has_sqlite_vec = True

        c1 = _concept("shared result", "c1")
        c2 = _concept("fts only", "c2")
        c3 = _concept("vec only", "c3")

        mock_concept_repo.search_by_text = AsyncMock(return_value=[(c1, -1.0), (c2, -2.0)])
        mock_concept_repo.search_by_embedding = AsyncMock(return_value=[(c1, 0.1), (c3, 0.5)])

        retrieval = HybridRetrieval(
            mock_concept_repo,
            mock_episode_repo,
            mock_embedding,
        )
        results = await retrieval.search_concepts("test", MIND)

        # c1 should be first (in both lists)
        assert results[0][0].id == ConceptId("c1")
        assert len(results) == 3  # noqa: PLR2004

    async def test_vec_failure_falls_back_to_fts(
        self,
        mock_concept_repo: AsyncMock,
        mock_episode_repo: AsyncMock,
        mock_embedding: AsyncMock,
    ) -> None:
        """Typed vector-search failure → fall back to FTS5.

        Seeds ``ValueError`` because it's in the narrow catch tuple
        in ``HybridRetrieval.search_concepts`` AND is a builtin — so
        the test-side reference and the production-side reference are
        always the same class object. Using an internal class
        (``SearchError``) here tripped CLAUDE.md anti-pattern #8:
        under Linux CI (coverage + Python 3.11), the test's class
        object didn't match the production catch's class object,
        and the "typed" exception propagated unhandled. ``SearchError``
        stays in the production narrow for real-world propagation
        via the subsystem; the test just uses a builtin that can't
        suffer class-identity drift.
        """
        mock_embedding.has_embeddings = True
        mock_concept_repo._pool.has_sqlite_vec = True

        c1 = _concept("test", "c1")
        mock_concept_repo.search_by_text = AsyncMock(return_value=[(c1, -1.0)])
        mock_concept_repo.search_by_embedding = AsyncMock(
            side_effect=ValueError("vec failed"),
        )

        retrieval = HybridRetrieval(
            mock_concept_repo,
            mock_episode_repo,
            mock_embedding,
        )
        results = await retrieval.search_concepts("test", MIND)
        assert len(results) == 1
        assert results[0][0].id == ConceptId("c1")


class TestSearchEpisodes:
    """Episode search."""

    async def test_fallback_to_recent(
        self, retrieval: HybridRetrieval, mock_episode_repo: AsyncMock
    ) -> None:
        e1 = _episode("hello", "e1")
        mock_episode_repo.get_recent = AsyncMock(return_value=[e1])

        results = await retrieval.search_episodes("hello", MIND)
        assert len(results) == 1
        assert results[0][0].id == EpisodeId("e1")

    async def test_vec_search_episodes(
        self,
        mock_concept_repo: AsyncMock,
        mock_episode_repo: AsyncMock,
        mock_embedding: AsyncMock,
    ) -> None:
        mock_embedding.has_embeddings = True
        mock_episode_repo._pool.has_sqlite_vec = True

        e1 = _episode("relevant", "e1")
        mock_episode_repo.search_by_embedding = AsyncMock(return_value=[(e1, 0.1)])

        retrieval = HybridRetrieval(mock_concept_repo, mock_episode_repo, mock_embedding)
        results = await retrieval.search_episodes("test", MIND)
        assert len(results) == 1

    async def test_episode_vec_failure_falls_back(
        self,
        mock_concept_repo: AsyncMock,
        mock_episode_repo: AsyncMock,
        mock_embedding: AsyncMock,
    ) -> None:
        """Typed vec-search failure → fall back to recency.

        Seeds ``ValueError`` (builtin, in the narrow tuple) for the
        same CLAUDE.md anti-pattern #8 reason as the concept-path
        sibling: internal class identity isn't stable under Linux
        CI's pytest-cov instrumentation.
        """
        mock_embedding.has_embeddings = True
        mock_episode_repo._pool.has_sqlite_vec = True
        mock_episode_repo.search_by_embedding = AsyncMock(
            side_effect=ValueError("fail"),
        )
        e1 = _episode("recent", "e1")
        mock_episode_repo.get_recent = AsyncMock(return_value=[e1])

        retrieval = HybridRetrieval(mock_concept_repo, mock_episode_repo, mock_embedding)
        results = await retrieval.search_episodes("test", MIND)
        assert len(results) == 1


class TestSearchAll:
    """Combined search."""

    async def test_returns_both_types(
        self, retrieval: HybridRetrieval, mock_concept_repo: AsyncMock
    ) -> None:
        c1 = _concept("test", "c1")
        mock_concept_repo.search_by_text = AsyncMock(return_value=[(c1, -1.0)])

        concepts, episodes = await retrieval.search_all("test", MIND)
        assert len(concepts) == 1
        assert isinstance(episodes, list)
