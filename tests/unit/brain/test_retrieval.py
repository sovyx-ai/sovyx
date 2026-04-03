"""Tests for sovyx.brain.retrieval — hybrid retrieval with RRF."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from sovyx.brain.models import Concept, Episode
from sovyx.brain.retrieval import HybridRetrieval
from sovyx.engine.types import ConceptId, ConversationId, EpisodeId, MindId

MIND = MindId("aria")


def _concept(name: str, cid: str = "") -> Concept:
    return Concept(
        id=ConceptId(cid or name),
        mind_id=MIND,
        name=name,
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
        """Verify RRF formula: score = Σ 1/(k + rank + 1)."""
        retrieval = HybridRetrieval(AsyncMock(), AsyncMock(), AsyncMock(), k_constant=60)
        c1 = _concept("test", "c1")

        # c1 is rank 0 in both lists
        fts = [(c1, -1.0)]
        vec = [(c1, 0.1)]

        results = retrieval._rrf_fusion(fts, vec, limit=1)
        expected_score = 1.0 / 61 + 1.0 / 61  # rank 0 in both
        assert abs(results[0][1] - expected_score) < 0.0001


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
        mock_embedding.has_embeddings = True
        mock_concept_repo._pool.has_sqlite_vec = True

        c1 = _concept("test", "c1")
        mock_concept_repo.search_by_text = AsyncMock(return_value=[(c1, -1.0)])
        mock_concept_repo.search_by_embedding = AsyncMock(side_effect=RuntimeError("vec failed"))

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
        mock_embedding.has_embeddings = True
        mock_episode_repo._pool.has_sqlite_vec = True
        mock_episode_repo.search_by_embedding = AsyncMock(side_effect=RuntimeError("fail"))
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
