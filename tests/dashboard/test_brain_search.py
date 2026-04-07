"""Tests for brain search — /api/brain/search endpoint + search_brain (V05-P03).

Covers:
- Empty/whitespace query → empty list
- ConceptRepository FTS5 + vector search delegation
- Deduplication (FTS5 + vector, same concept)
- Score normalisation (FTS5 rank → 0-1, distance → 0-1)
- Limit enforcement
- Graceful degradation (no ConceptRepository, exceptions)
- /api/brain/search endpoint: auth, wiring, validation
"""

from __future__ import annotations

import secrets
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.testclient import TestClient

from sovyx.dashboard.brain import search_brain
from sovyx.dashboard.server import create_app

# ── Helpers ────────────────────────────────────────────────────────────


def _mock_concept(
    cid: str,
    name: str,
    category: str = "fact",
    importance: float = 0.7,
    confidence: float = 0.8,
    access_count: int = 5,
) -> MagicMock:
    c = MagicMock()
    c.id = cid
    c.name = name
    c.category = MagicMock(value=category)
    c.importance = importance
    c.confidence = confidence
    c.access_count = access_count
    return c


def _registry_with_repo(
    *,
    fts_results: list | None = None,
    vec_results: list | None = None,
) -> MagicMock:
    """Build a mock registry with ConceptRepository."""
    repo = AsyncMock()
    repo.search_by_text = AsyncMock(return_value=fts_results or [])
    repo.search_by_embedding = AsyncMock(return_value=vec_results or [])
    registry = MagicMock()
    registry.is_registered = MagicMock(return_value=True)
    registry.resolve = AsyncMock(return_value=repo)
    return registry


# ── Unit: search_brain ────────────────────────────────────────────────


class TestSearchBrain:
    """search_brain returns a flat list of result dicts."""

    @pytest.mark.asyncio
    async def test_empty_query(self) -> None:
        assert await search_brain(MagicMock(), "", limit=10) == []

    @pytest.mark.asyncio
    async def test_whitespace_query(self) -> None:
        assert await search_brain(MagicMock(), "   ", limit=10) == []

    @pytest.mark.asyncio
    async def test_fts_only(self) -> None:
        """FTS5 returns results, embedding unavailable → FTS-only."""
        c1 = _mock_concept("c1", "Python", "skill")
        c2 = _mock_concept("c2", "Django", "skill")
        registry = _registry_with_repo(fts_results=[(c1, -5.0), (c2, -2.0)])

        with (
            patch("sovyx.dashboard.brain._get_active_mind_id",
                  new_callable=AsyncMock, return_value="m1"),
            patch("sovyx.dashboard.brain._get_query_embedding",
                  new_callable=AsyncMock, return_value=None),
        ):
            results = await search_brain(registry, "Python", limit=10)

        assert isinstance(results, list)
        assert len(results) == 2
        assert results[0]["match_type"] == "text"
        # FTS5 rank -5.0 → score = 1/(1+5) ≈ 0.1667
        assert 0.1 < results[0]["score"] < 0.5

    @pytest.mark.asyncio
    async def test_hybrid_dedup(self) -> None:
        """Same concept from FTS5 and vector → deduplicated."""
        c1_fts = _mock_concept("c1", "Python")
        c1_vec = _mock_concept("c1", "Python")
        c2_vec = _mock_concept("c2", "FastAPI")

        registry = _registry_with_repo(
            fts_results=[(c1_fts, -1.0)],
            vec_results=[(c1_vec, 0.2), (c2_vec, 0.3)],
        )

        with (
            patch("sovyx.dashboard.brain._get_active_mind_id",
                  new_callable=AsyncMock, return_value="m1"),
            patch("sovyx.dashboard.brain._get_query_embedding",
                  new_callable=AsyncMock, return_value=[0.1]),
        ):
            results = await search_brain(registry, "Python", limit=10)

        ids = [r["id"] for r in results]
        assert ids.count("c1") == 1  # Deduplicated
        assert "c2" in ids
        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_limit_enforcement(self) -> None:
        concepts = [(_mock_concept(f"c{i}", f"C{i}"), float(-i)) for i in range(10)]
        registry = _registry_with_repo(fts_results=concepts)

        with (
            patch("sovyx.dashboard.brain._get_active_mind_id",
                  new_callable=AsyncMock, return_value="m1"),
            patch("sovyx.dashboard.brain._get_query_embedding",
                  new_callable=AsyncMock, return_value=None),
        ):
            results = await search_brain(registry, "test", limit=3)

        assert len(results) == 3

    @pytest.mark.asyncio
    async def test_sorted_by_score_desc(self) -> None:
        c1 = _mock_concept("c1", "Low")
        c2 = _mock_concept("c2", "High")
        registry = _registry_with_repo(fts_results=[(c1, -10.0), (c2, -0.5)])

        with (
            patch("sovyx.dashboard.brain._get_active_mind_id",
                  new_callable=AsyncMock, return_value="m1"),
            patch("sovyx.dashboard.brain._get_query_embedding",
                  new_callable=AsyncMock, return_value=None),
        ):
            results = await search_brain(registry, "test", limit=10)

        scores = [r["score"] for r in results]
        assert scores == sorted(scores, reverse=True)

    @pytest.mark.asyncio
    async def test_no_repo(self) -> None:
        registry = MagicMock()
        registry.is_registered = MagicMock(return_value=False)
        with patch("sovyx.dashboard.brain._get_active_mind_id",
                    new_callable=AsyncMock, return_value="m1"):
            assert await search_brain(registry, "test", limit=10) == []

    @pytest.mark.asyncio
    async def test_fts_exception_graceful(self) -> None:
        repo = AsyncMock()
        repo.search_by_text = AsyncMock(side_effect=RuntimeError("db locked"))
        registry = MagicMock()
        registry.is_registered = MagicMock(return_value=True)
        registry.resolve = AsyncMock(return_value=repo)

        with (
            patch("sovyx.dashboard.brain._get_active_mind_id",
                  new_callable=AsyncMock, return_value="m1"),
            patch("sovyx.dashboard.brain._get_query_embedding",
                  new_callable=AsyncMock, return_value=None),
        ):
            results = await search_brain(registry, "test", limit=10)
        assert results == []

    @pytest.mark.asyncio
    async def test_vector_score_normalisation(self) -> None:
        """Distance 0.0 → score 1.0, distance 1.0 → score 0.0."""
        c1 = _mock_concept("c1", "Close")
        c2 = _mock_concept("c2", "Far")
        registry = _registry_with_repo(vec_results=[(c1, 0.0), (c2, 1.0)])

        with (
            patch("sovyx.dashboard.brain._get_active_mind_id",
                  new_callable=AsyncMock, return_value="m1"),
            patch("sovyx.dashboard.brain._get_query_embedding",
                  new_callable=AsyncMock, return_value=[0.1]),
        ):
            results = await search_brain(registry, "test", limit=10)

        close = next(r for r in results if r["id"] == "c1")
        far = next(r for r in results if r["id"] == "c2")
        assert close["score"] == 1.0
        assert far["score"] == 0.0

    @pytest.mark.asyncio
    async def test_result_has_all_fields(self) -> None:
        """Each result dict has the expected keys."""
        c = _mock_concept("c1", "Test", "skill", 0.85, 0.9, 42)
        registry = _registry_with_repo(fts_results=[(c, -1.0)])

        with (
            patch("sovyx.dashboard.brain._get_active_mind_id",
                  new_callable=AsyncMock, return_value="m1"),
            patch("sovyx.dashboard.brain._get_query_embedding",
                  new_callable=AsyncMock, return_value=None),
        ):
            results = await search_brain(registry, "test", limit=10)

        r = results[0]
        assert set(r.keys()) == {
            "id", "name", "category", "importance",
            "confidence", "access_count", "score", "match_type",
        }


# ── Integration: /api/brain/search endpoint ────────────────────────────


def _make_client(tmp_path_factory: pytest.TempPathFactory) -> tuple[TestClient, dict[str, str]]:
    tmp = tmp_path_factory.mktemp("bsearch")
    token = secrets.token_urlsafe(32)
    (tmp / "token").write_text(token)
    with patch("sovyx.dashboard.server.TOKEN_FILE", tmp / "token"):
        app = create_app()
    return TestClient(app), {"Authorization": f"Bearer {token}"}


class TestBrainSearchEndpoint:
    def test_empty_query(self, tmp_path_factory: pytest.TempPathFactory) -> None:
        client, hdr = _make_client(tmp_path_factory)
        resp = client.get("/api/brain/search?q=", headers=hdr)
        assert resp.status_code == 200
        data = resp.json()
        assert data["results"] == []
        assert data["query"] == ""

    def test_wired_search(self, tmp_path_factory: pytest.TempPathFactory) -> None:
        client, hdr = _make_client(tmp_path_factory)
        mock_results = [
            {"id": "c1", "name": "Python", "score": 0.95,
             "category": "skill", "importance": 0.8,
             "confidence": 0.9, "access_count": 5, "match_type": "text"},
        ]
        with patch(
            "sovyx.dashboard.brain.search_brain",
            new_callable=AsyncMock,
            return_value=mock_results,
        ):
            client.app.state.registry = MagicMock()  # type: ignore[union-attr]
            resp = client.get("/api/brain/search?q=Python", headers=hdr)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["results"]) == 1
        assert data["results"][0]["name"] == "Python"
        assert data["query"] == "Python"

    def test_no_registry(self, tmp_path_factory: pytest.TempPathFactory) -> None:
        client, hdr = _make_client(tmp_path_factory)
        resp = client.get("/api/brain/search?q=test", headers=hdr)
        assert resp.status_code == 200
        assert resp.json()["results"] == []

    def test_requires_auth(self, tmp_path_factory: pytest.TempPathFactory) -> None:
        client, _ = _make_client(tmp_path_factory)
        assert client.get("/api/brain/search?q=test").status_code == 401

    def test_limit_min(self, tmp_path_factory: pytest.TempPathFactory) -> None:
        client, hdr = _make_client(tmp_path_factory)
        assert client.get("/api/brain/search?q=t&limit=0", headers=hdr).status_code == 422

    def test_limit_max(self, tmp_path_factory: pytest.TempPathFactory) -> None:
        client, hdr = _make_client(tmp_path_factory)
        assert client.get("/api/brain/search?q=t&limit=999", headers=hdr).status_code == 422
