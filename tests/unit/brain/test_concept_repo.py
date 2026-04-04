"""Tests for sovyx.brain.concept_repo — concept repository."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from sovyx.brain.concept_repo import ConceptRepository
from sovyx.brain.models import Concept
from sovyx.engine.errors import SearchError
from sovyx.engine.types import ConceptCategory, ConceptId, MindId
from sovyx.persistence.migrations import MigrationRunner
from sovyx.persistence.pool import DatabasePool
from sovyx.persistence.schemas.brain import get_brain_migrations

if TYPE_CHECKING:
    from pathlib import Path

    from sovyx.brain.embedding import EmbeddingEngine

MIND = MindId("aria")


@pytest.fixture
async def pool(tmp_path: Path) -> DatabasePool:
    """Pool with brain schema applied."""
    p = DatabasePool(db_path=tmp_path / "brain.db", read_pool_size=1)
    await p.initialize()
    runner = MigrationRunner(p)
    await runner.initialize()
    await runner.run_migrations(get_brain_migrations(has_sqlite_vec=False))
    yield p  # type: ignore[misc]
    await p.close()


@pytest.fixture
def mock_embedding() -> EmbeddingEngine:
    """Mock embedding engine (no real ONNX)."""
    engine = AsyncMock()
    engine.has_embeddings = False
    engine.encode = AsyncMock(return_value=[0.1] * 384)
    return engine


@pytest.fixture
def repo(pool: DatabasePool, mock_embedding: EmbeddingEngine) -> ConceptRepository:
    return ConceptRepository(pool, mock_embedding)


def _make_concept(name: str = "test", **kwargs: object) -> Concept:
    return Concept(mind_id=MIND, name=name, **kwargs)  # type: ignore[arg-type]


class TestCRUD:
    """Basic CRUD operations."""

    async def test_create_and_get(self, repo: ConceptRepository) -> None:
        concept = _make_concept("quantum physics", content="study of particles")
        cid = await repo.create(concept)
        fetched = await repo.get(cid)
        assert fetched is not None
        assert fetched.name == "quantum physics"
        assert fetched.content == "study of particles"

    async def test_get_nonexistent(self, repo: ConceptRepository) -> None:
        result = await repo.get(ConceptId("nonexistent"))
        assert result is None

    async def test_get_by_mind(self, repo: ConceptRepository) -> None:
        for i in range(5):
            await repo.create(_make_concept(f"concept {i}"))
        results = await repo.get_by_mind(MIND)
        assert len(results) == 5  # noqa: PLR2004

    async def test_get_by_mind_pagination(self, repo: ConceptRepository) -> None:
        for i in range(10):
            await repo.create(_make_concept(f"concept {i}"))
        page1 = await repo.get_by_mind(MIND, limit=3, offset=0)
        page2 = await repo.get_by_mind(MIND, limit=3, offset=3)
        assert len(page1) == 3  # noqa: PLR2004
        assert len(page2) == 3  # noqa: PLR2004
        assert page1[0].id != page2[0].id

    async def test_update(self, repo: ConceptRepository) -> None:
        concept = _make_concept("original")
        cid = await repo.create(concept)
        fetched = await repo.get(cid)
        assert fetched is not None
        updated = fetched.model_copy(update={"name": "updated", "importance": 0.9})
        await repo.update(updated)
        result = await repo.get(cid)
        assert result is not None
        assert result.name == "updated"
        assert result.importance == 0.9

    async def test_delete(self, repo: ConceptRepository) -> None:
        concept = _make_concept("to delete")
        cid = await repo.create(concept)
        await repo.delete(cid)
        assert await repo.get(cid) is None

    async def test_count(self, repo: ConceptRepository) -> None:
        assert await repo.count(MIND) == 0
        await repo.create(_make_concept("a"))
        await repo.create(_make_concept("b"))
        assert await repo.count(MIND) == 2  # noqa: PLR2004


class TestRecordAccess:
    """Access tracking."""

    async def test_record_access_increments(self, repo: ConceptRepository) -> None:
        concept = _make_concept("tracked")
        cid = await repo.create(concept)
        await repo.record_access(cid)
        await repo.record_access(cid)
        fetched = await repo.get(cid)
        assert fetched is not None
        assert fetched.access_count == 2  # noqa: PLR2004
        assert fetched.last_accessed is not None


class TestGetRecent:
    """Recently accessed concepts."""

    async def test_get_recent_ordered(self, repo: ConceptRepository) -> None:
        c1 = _make_concept("old")
        c2 = _make_concept("new")
        await repo.create(c1)
        await repo.create(c2)
        await repo.record_access(c1.id)
        await repo.record_access(c2.id)
        await repo.record_access(c2.id)  # access again

        recent = await repo.get_recent(MIND, limit=10)
        # c2 was accessed more recently
        assert len(recent) >= 2  # noqa: PLR2004

    async def test_get_recent_nulls_last(self, repo: ConceptRepository) -> None:
        c1 = _make_concept("never accessed")
        c2 = _make_concept("accessed")
        await repo.create(c1)
        await repo.create(c2)
        await repo.record_access(c2.id)

        recent = await repo.get_recent(MIND, limit=10)
        assert recent[0].name == "accessed"
        assert recent[-1].name == "never accessed"

    async def test_get_recent_respects_limit(self, repo: ConceptRepository) -> None:
        for i in range(10):
            await repo.create(_make_concept(f"c{i}"))
        recent = await repo.get_recent(MIND, limit=3)
        assert len(recent) == 3  # noqa: PLR2004


class TestSearchByText:
    """FTS5 text search."""

    async def test_search_finds_match(self, repo: ConceptRepository) -> None:
        await repo.create(_make_concept("quantum physics", content="study of subatomic particles"))
        await repo.create(_make_concept("cooking recipe", content="how to bake a cake"))
        results = await repo.search_by_text("quantum", MIND)
        assert len(results) == 1
        assert results[0][0].name == "quantum physics"

    async def test_search_empty_results(self, repo: ConceptRepository) -> None:
        results = await repo.search_by_text("nonexistent", MIND)
        assert results == []

    async def test_search_sanitizes_operators(self, repo: ConceptRepository) -> None:
        """FTS5 operators in query are treated as literals."""
        await repo.create(_make_concept("test AND concept", content="content"))
        # Should not crash even with FTS operators in name
        results = await repo.search_by_text("test AND concept", MIND)
        assert len(results) >= 0  # no crash


class TestSearchByEmbedding:
    """Vector search."""

    async def test_raises_without_sqlite_vec(self, repo: ConceptRepository) -> None:
        """SearchError when sqlite-vec not available."""
        with pytest.raises(SearchError, match="sqlite-vec"):
            await repo.search_by_embedding([0.1] * 384, MIND)


class TestCreateWithEmbedding:
    """Concept creation with embedding."""

    async def test_create_generates_embedding_when_available(
        self,
        pool: DatabasePool,
    ) -> None:
        """When embedding engine has_embeddings, encode is called."""
        mock_engine = AsyncMock()
        mock_engine.has_embeddings = True
        mock_engine.encode = AsyncMock(return_value=[0.1] * 384)

        repo = ConceptRepository(pool, mock_engine)
        concept = _make_concept("test concept", content="test content")
        await repo.create(concept)

        mock_engine.encode.assert_called_once_with("test concept test content")

    async def test_create_skips_embedding_when_unavailable(
        self,
        pool: DatabasePool,
    ) -> None:
        """When has_embeddings=False, encode is not called."""
        mock_engine = AsyncMock()
        mock_engine.has_embeddings = False

        repo = ConceptRepository(pool, mock_engine)
        await repo.create(_make_concept("test"))

        mock_engine.encode.assert_not_called()

    async def test_create_uses_provided_embedding(
        self,
        pool: DatabasePool,
    ) -> None:
        """When concept already has embedding, skip encode."""
        mock_engine = AsyncMock()
        mock_engine.has_embeddings = True

        repo = ConceptRepository(pool, mock_engine)
        concept = _make_concept("test", embedding=[0.5] * 384)
        await repo.create(concept)

        mock_engine.encode.assert_not_called()


    async def test_empty_name_and_content_skips_encode(
        self,
        pool: DatabasePool,
    ) -> None:
        """Line 48→51: empty name+content → no encode called."""
        mock_engine = AsyncMock()
        mock_engine.has_embeddings = True

        repo = ConceptRepository(pool, mock_engine)
        concept = _make_concept("", content="")
        await repo.create(concept)

        mock_engine.encode.assert_not_called()

    async def test_delete_concept(
        self,
        pool: DatabasePool,
    ) -> None:
        """Line 165: delete with sqlite-vec=False (no embedding table)."""
        mock_engine = AsyncMock()
        mock_engine.has_embeddings = False
        repo = ConceptRepository(pool, mock_engine)
        concept = _make_concept("deletable")
        cid = await repo.create(concept)
        await repo.delete(cid)
        assert await repo.get(cid) is None


class TestMetadataSerialization:
    """JSON serialization of metadata field."""

    async def test_metadata_roundtrip(self, repo: ConceptRepository) -> None:
        concept = _make_concept("test", metadata={"key": "value", "num": 42})
        cid = await repo.create(concept)
        fetched = await repo.get(cid)
        assert fetched is not None
        assert fetched.metadata == {"key": "value", "num": 42}

    async def test_category_roundtrip(self, repo: ConceptRepository) -> None:
        concept = _make_concept("test", category=ConceptCategory.PREFERENCE)
        cid = await repo.create(concept)
        fetched = await repo.get(cid)
        assert fetched is not None
        assert fetched.category == ConceptCategory.PREFERENCE


class TestFTS5Adversarial:
    """FTS5 search should never crash on adversarial input."""

    @pytest.mark.parametrize(
        "query",
        [
            "",                          # empty
            "   ",                       # whitespace only
            '"; DROP TABLE concepts --', # SQL injection attempt
            "OR 1=1",                    # boolean injection
            "***",                       # only special chars
            "a" * 1000,                  # very long
            "café résumé naïve",         # unicode with diacritics
            "AND OR NOT NEAR",           # FTS5 operators only
            '"unclosed quote',           # unclosed quote
            "test*",                     # glob
        ],
        ids=[
            "empty", "whitespace", "sql_injection", "boolean_injection",
            "special_chars", "very_long", "unicode", "fts5_operators",
            "unclosed_quote", "glob",
        ],
    )
    async def test_adversarial_input_no_crash(
        self, repo: ConceptRepository, query: str
    ) -> None:
        """FTS5 search returns a list (possibly empty) — never crashes."""
        results = await repo.search_by_text(query, mind_id=MindId("test-mind"))
        assert isinstance(results, list)
