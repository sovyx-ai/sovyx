"""Tests for sovyx.brain.episode_repo — episode repository."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from sovyx.brain.episode_repo import EpisodeRepository
from sovyx.brain.models import Episode
from sovyx.engine.errors import SearchError
from sovyx.engine.types import ConceptId, ConversationId, EpisodeId, MindId
from sovyx.persistence.migrations import MigrationRunner
from sovyx.persistence.pool import DatabasePool
from sovyx.persistence.schemas.brain import get_brain_migrations

if TYPE_CHECKING:
    from pathlib import Path

    from sovyx.brain.embedding import EmbeddingEngine

MIND = MindId("aria")
CONV = ConversationId("conv1")


@pytest.fixture
async def pool(tmp_path: Path) -> DatabasePool:
    """Pool with brain schema applied."""
    p = DatabasePool(db_path=tmp_path / "brain.db", read_pool_size=1, load_extensions=["vec0"])
    await p.initialize()
    runner = MigrationRunner(p)
    await runner.initialize()
    await runner.run_migrations(get_brain_migrations(has_sqlite_vec=p.has_sqlite_vec))
    yield p  # type: ignore[misc]
    await p.close()


@pytest.fixture
def mock_embedding() -> EmbeddingEngine:
    """Mock embedding engine."""
    engine = AsyncMock()
    engine.has_embeddings = False
    engine.encode = AsyncMock(return_value=[0.1] * 384)
    return engine


@pytest.fixture
def repo(pool: DatabasePool, mock_embedding: EmbeddingEngine) -> EpisodeRepository:
    return EpisodeRepository(pool, mock_embedding)


def _make_episode(
    conv_id: str = "conv1",
    user: str = "hello",
    assistant: str = "hi there",
    **kwargs: object,
) -> Episode:
    return Episode(
        mind_id=MIND,
        conversation_id=ConversationId(conv_id),
        user_input=user,
        assistant_response=assistant,
        **kwargs,  # type: ignore[arg-type]
    )


class TestCRUD:
    """Basic CRUD operations."""

    async def test_create_and_get(self, repo: EpisodeRepository) -> None:
        episode = _make_episode()
        eid = await repo.create(episode)
        fetched = await repo.get(eid)
        assert fetched is not None
        assert fetched.user_input == "hello"
        assert fetched.assistant_response == "hi there"

    async def test_get_nonexistent(self, repo: EpisodeRepository) -> None:
        result = await repo.get(EpisodeId("nonexistent"))
        assert result is None

    async def test_delete(self, repo: EpisodeRepository) -> None:
        episode = _make_episode()
        eid = await repo.create(episode)
        await repo.delete(eid)
        assert await repo.get(eid) is None

    async def test_count(self, repo: EpisodeRepository) -> None:
        assert await repo.count(MIND) == 0
        await repo.create(_make_episode())
        await repo.create(_make_episode())
        assert await repo.count(MIND) == 2  # noqa: PLR2004


class TestGetByConversation:
    """Conversation-based queries."""

    async def test_returns_episodes_chronologically(self, repo: EpisodeRepository) -> None:
        e1 = _make_episode(user="first")
        await repo.create(e1)
        time.sleep(0.01)
        e2 = _make_episode(user="second")
        await repo.create(e2)

        results = await repo.get_by_conversation(CONV)
        assert len(results) == 2  # noqa: PLR2004
        assert results[0].user_input == "first"
        assert results[1].user_input == "second"

    async def test_nonexistent_conversation(self, repo: EpisodeRepository) -> None:
        results = await repo.get_by_conversation(ConversationId("nope"))
        assert results == []

    async def test_respects_limit(self, repo: EpisodeRepository) -> None:
        for i in range(10):
            await repo.create(_make_episode(user=f"msg{i}"))
        results = await repo.get_by_conversation(CONV, limit=3)
        assert len(results) == 3  # noqa: PLR2004


class TestGetRecent:
    """Recent episodes query."""

    async def test_ordered_by_created_at_desc(self, repo: EpisodeRepository) -> None:
        e1 = _make_episode(user="old")
        await repo.create(e1)
        time.sleep(0.01)
        e2 = _make_episode(user="new")
        await repo.create(e2)

        recent = await repo.get_recent(MIND, limit=10)
        assert len(recent) == 2  # noqa: PLR2004
        assert recent[0].user_input == "new"
        assert recent[1].user_input == "old"

    async def test_respects_limit(self, repo: EpisodeRepository) -> None:
        for i in range(10):
            await repo.create(_make_episode(user=f"msg{i}"))
        recent = await repo.get_recent(MIND, limit=3)
        assert len(recent) == 3  # noqa: PLR2004


class TestGetSince:
    """Lookback window query used by the DREAM phase."""

    async def test_returns_episodes_after_threshold(self, repo: EpisodeRepository) -> None:
        from datetime import UTC, datetime, timedelta  # noqa: PLC0415

        now = datetime.now(UTC)
        await repo.create(_make_episode(user="recent"))
        episodes = await repo.get_since(MIND, now - timedelta(hours=1))
        assert len(episodes) == 1
        assert episodes[0].user_input == "recent"

    async def test_excludes_earlier_episodes(self, repo: EpisodeRepository) -> None:
        from datetime import UTC, datetime, timedelta  # noqa: PLC0415

        await repo.create(_make_episode(user="any"))
        # Threshold strictly *after* now → no rows match.
        future = datetime.now(UTC) + timedelta(hours=1)
        episodes = await repo.get_since(MIND, future)
        assert episodes == []

    async def test_respects_limit(self, repo: EpisodeRepository) -> None:
        from datetime import UTC, datetime, timedelta  # noqa: PLC0415

        for i in range(8):
            await repo.create(_make_episode(user=f"msg{i}"))
        episodes = await repo.get_since(MIND, datetime.now(UTC) - timedelta(hours=1), limit=3)
        assert len(episodes) == 3  # noqa: PLR2004

    async def test_returns_chronological_order(self, repo: EpisodeRepository) -> None:
        from datetime import UTC, datetime, timedelta  # noqa: PLC0415

        await repo.create(_make_episode(user="first"))
        time.sleep(0.01)
        await repo.create(_make_episode(user="second"))
        episodes = await repo.get_since(MIND, datetime.now(UTC) - timedelta(hours=1))
        assert [e.user_input for e in episodes] == ["first", "second"]


class TestSearchByEmbedding:
    """Vector search."""

    async def test_raises_without_sqlite_vec(self, tmp_path: Path) -> None:
        """SearchError when sqlite-vec not loaded."""
        no_vec_pool = DatabasePool(db_path=tmp_path / "no_vec.db", read_pool_size=1)
        await no_vec_pool.initialize()
        runner = MigrationRunner(no_vec_pool)
        await runner.initialize()
        await runner.run_migrations(get_brain_migrations(has_sqlite_vec=False))
        mock_engine = AsyncMock()
        mock_engine.has_embeddings = False
        repo = EpisodeRepository(no_vec_pool, mock_engine)
        with pytest.raises(SearchError, match="sqlite-vec"):
            await repo.search_by_embedding([0.1] * 384, MIND)
        await no_vec_pool.close()


class TestCreateWithEmbedding:
    """Episode creation with embedding."""

    async def test_generates_embedding_when_available(self, pool: DatabasePool) -> None:
        mock_engine = AsyncMock()
        mock_engine.has_embeddings = True
        mock_engine.encode = AsyncMock(return_value=[0.1] * 384)

        repo = EpisodeRepository(pool, mock_engine)
        episode = _make_episode(user="question", assistant="answer")
        await repo.create(episode)

        mock_engine.encode.assert_called_once_with("question answer")

    async def test_empty_text_skips_encode(self, pool: DatabasePool) -> None:
        """Line 49→52: empty user_input + assistant_response → no encode."""
        mock_engine = AsyncMock()
        mock_engine.has_embeddings = True

        repo = EpisodeRepository(pool, mock_engine)
        episode = _make_episode(user="", assistant="")
        await repo.create(episode)

        mock_engine.encode.assert_not_called()

    async def test_skips_embedding_when_unavailable(self, pool: DatabasePool) -> None:
        mock_engine = AsyncMock()
        mock_engine.has_embeddings = False

        repo = EpisodeRepository(pool, mock_engine)
        await repo.create(_make_episode())

        mock_engine.encode.assert_not_called()

    async def test_uses_provided_embedding(self, pool: DatabasePool) -> None:
        mock_engine = AsyncMock()
        mock_engine.has_embeddings = True

        repo = EpisodeRepository(pool, mock_engine)
        episode = _make_episode(embedding=[0.5] * 384)
        await repo.create(episode)

        mock_engine.encode.assert_not_called()


class TestSerialization:
    """JSON serialization roundtrips."""

    async def test_concepts_mentioned_roundtrip(self, repo: EpisodeRepository) -> None:
        episode = _make_episode(concepts_mentioned=[ConceptId("c1"), ConceptId("c2")])
        eid = await repo.create(episode)
        fetched = await repo.get(eid)
        assert fetched is not None
        assert fetched.concepts_mentioned == [ConceptId("c1"), ConceptId("c2")]

    async def test_metadata_roundtrip(self, repo: EpisodeRepository) -> None:
        episode = _make_episode(metadata={"mood": "happy", "score": 0.9})
        eid = await repo.create(episode)
        fetched = await repo.get(eid)
        assert fetched is not None
        assert fetched.metadata == {"mood": "happy", "score": 0.9}

    async def test_all_fields_roundtrip(self, repo: EpisodeRepository) -> None:
        episode = _make_episode(
            user="complex input",
            assistant="detailed response",
            summary="a summary",
            importance=0.8,
            emotional_valence=-0.3,
            emotional_arousal=0.7,
        )
        eid = await repo.create(episode)
        fetched = await repo.get(eid)
        assert fetched is not None
        assert fetched.summary == "a summary"
        assert fetched.importance == 0.8
        assert abs(fetched.emotional_valence - (-0.3)) < 0.001
        assert abs(fetched.emotional_arousal - 0.7) < 0.001


# ── sqlite-vec tests (covers lines 76, 144-162, 168) ──


@pytest.fixture
def vec_repo(pool: DatabasePool) -> EpisodeRepository:
    """Repository with has_embeddings=True for sqlite-vec tests."""
    if not pool.has_sqlite_vec:
        pytest.skip("sqlite-vec not available")
    mock_engine = AsyncMock()
    mock_engine.has_embeddings = True
    mock_engine.encode = AsyncMock(return_value=[0.1] * 384)
    return EpisodeRepository(pool, mock_engine)


class TestSqliteVecOperations:
    """Tests requiring real sqlite-vec for embedding storage + search."""

    async def test_create_stores_embedding(self, vec_repo: EpisodeRepository) -> None:
        """Line 76: embedding INSERT when has_sqlite_vec=True."""
        episode = _make_episode(user="test question", assistant="test answer")
        eid = await vec_repo.create(episode)
        fetched = await vec_repo.get(eid)
        assert fetched is not None

    async def test_search_by_embedding(self, vec_repo: EpisodeRepository) -> None:
        """Lines 144-162: vector similarity search."""
        for i in range(3):
            ep = _make_episode(
                conv_id=f"conv{i}",
                user=f"question {i}",
                assistant=f"answer {i}",
            )
            await vec_repo.create(ep)

        query_vec = [0.1] * 384
        results = await vec_repo.search_by_embedding(
            query_embedding=query_vec,
            mind_id=MIND,
            limit=5,
        )
        assert isinstance(results, list)
        assert len(results) > 0
        for _episode, distance in results:
            assert isinstance(distance, float)

    async def test_delete_removes_embedding(self, vec_repo: EpisodeRepository) -> None:
        """Line 168: DELETE from episode_embeddings."""
        episode = _make_episode()
        eid = await vec_repo.create(episode)
        await vec_repo.delete(eid)
        fetched = await vec_repo.get(eid)
        assert fetched is None
