"""Tests for Knowledge Plugin dedup engine (TASK-472).

Covers: semantic dedup on remember(), reinforcement behavior,
threshold edge cases, no-match creation, JSON output format.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from sovyx.plugins.official.knowledge import KnowledgePlugin


def _mock_brain(
    *,
    search_results: list[dict[str, object]] | None = None,
    similar_results: list[dict[str, object]] | None = None,
    learn_id: str = "c-new",
    stats: dict[str, object] | None = None,
) -> AsyncMock:
    """Create a mock BrainAccess with full API."""
    brain = AsyncMock()
    brain.search = AsyncMock(return_value=search_results or [])
    brain.find_similar = AsyncMock(return_value=similar_results or [])
    brain.learn = AsyncMock(return_value=learn_id)
    brain.forget = AsyncMock(return_value=True)
    brain.update = AsyncMock(return_value=True)
    brain.boost_importance = AsyncMock(return_value=True)
    brain.reinforce = AsyncMock(
        return_value={
            "concept_id": "c-existing",
            "importance": {"old": 0.5, "new": 0.55},
            "confidence": {"old": 0.5, "new": 0.6},
            "reinforcement_count": 1,
            "established": False,
            "access_count": 2,
        }
    )
    brain.get_related = AsyncMock(return_value=[])
    brain.get_stats = AsyncMock(
        return_value=stats
        or {
            "total_concepts": 0,
            "categories": {},
            "total_relations": 0,
            "total_episodes": 0,
            "mind_id": "test",
        }
    )
    return brain


class TestRememberDedup:
    """Semantic deduplication on remember()."""

    @pytest.mark.asyncio
    async def test_creates_new_when_no_similar(self) -> None:
        brain = _mock_brain(similar_results=[])
        plugin = KnowledgePlugin(brain=brain)

        result = json.loads(await plugin.remember("dark mode is preferred"))
        assert result["action"] == "created"
        assert result["concept_id"] == "c-new"
        brain.learn.assert_called_once()
        brain.boost_importance.assert_not_called()

    @pytest.mark.asyncio
    async def test_reinforces_when_similar_found(self) -> None:
        existing = {
            "id": "c-existing",
            "name": "dark mode preference",
            "content": "user prefers dark mode",
            "similarity": 0.95,
            "confidence": 0.5,
            "importance": 0.6,
        }
        brain = _mock_brain(similar_results=[existing])
        brain.reinforce = AsyncMock(
            return_value={
                "concept_id": "c-existing",
                "importance": {"old": 0.6, "new": 0.65},
                "confidence": {"old": 0.5, "new": 0.6},
                "reinforcement_count": 1,
                "established": False,
                "access_count": 3,
            }
        )
        plugin = KnowledgePlugin(brain=brain)

        result = json.loads(await plugin.remember("I like dark mode"))
        assert result["action"] == "reinforced"
        assert result["concept_id"] == "c-existing"
        assert result["similarity"] == 0.95
        assert result["confidence"]["old"] == 0.5
        assert result["confidence"]["new"] == 0.6
        assert result["importance"]["old"] == 0.6
        assert result["importance"]["new"] == 0.65
        assert result["reinforcement_count"] == 1
        assert result["established"] is False

        brain.reinforce.assert_called_once_with(
            "c-existing",
            importance_delta=0.05,
            confidence_delta=0.10,
        )
        brain.learn.assert_not_called()

    @pytest.mark.asyncio
    async def test_established_after_5_reinforcements(self) -> None:
        existing = {"id": "c-est", "name": "well known", "similarity": 0.92}
        brain = _mock_brain(similar_results=[existing])
        brain.reinforce = AsyncMock(
            return_value={
                "concept_id": "c-est",
                "importance": {"old": 0.75, "new": 0.8},
                "confidence": {"old": 0.9, "new": 1.0},
                "reinforcement_count": 5,
                "established": True,
                "access_count": 10,
            }
        )
        plugin = KnowledgePlugin(brain=brain)

        result = json.loads(await plugin.remember("same thing again"))
        assert result["established"] is True
        assert result["reinforcement_count"] == 5
        assert "established memory" in result["message"]

    @pytest.mark.asyncio
    async def test_threshold_edge_just_below(self) -> None:
        """Similarity just below threshold → creates new."""
        existing = {
            "id": "c-maybe",
            "name": "somewhat similar",
            "similarity": 0.87,  # below default 0.88
            "confidence": 0.5,
        }
        brain = _mock_brain(similar_results=[existing])
        # find_similar with threshold=0.88 will filter this out internally,
        # but we're mocking, so simulate empty return
        brain.find_similar = AsyncMock(return_value=[])
        plugin = KnowledgePlugin(brain=brain)

        result = json.loads(await plugin.remember("new concept"))
        assert result["action"] == "created"

    @pytest.mark.asyncio
    async def test_custom_threshold(self) -> None:
        existing = {
            "id": "c-x",
            "name": "test",
            "similarity": 0.75,
            "confidence": 0.5,
        }
        brain = _mock_brain(similar_results=[existing])
        plugin = KnowledgePlugin(brain=brain, dedup_threshold=0.7)

        result = json.loads(await plugin.remember("similar enough at 0.7"))
        assert result["action"] == "reinforced"

    @pytest.mark.asyncio
    async def test_picks_best_match(self) -> None:
        """When multiple similar found, uses the highest similarity."""
        similar = [
            {"id": "c-best", "name": "best match", "similarity": 0.98, "confidence": 0.5},
            {"id": "c-ok", "name": "ok match", "similarity": 0.90, "confidence": 0.5},
        ]
        brain = _mock_brain(similar_results=similar)
        plugin = KnowledgePlugin(brain=brain)

        result = json.loads(await plugin.remember("test"))
        assert result["concept_id"] == "c-best"
        assert result["similarity"] == 0.98

    @pytest.mark.asyncio
    async def test_auto_generates_name(self) -> None:
        brain = _mock_brain()
        plugin = KnowledgePlugin(brain=brain)

        await plugin.remember("the weather in Sorocaba is usually warm and humid")
        call_kwargs = brain.learn.call_args.kwargs
        assert call_kwargs["name"].startswith("the weather in Sorocaba")

    @pytest.mark.asyncio
    async def test_explicit_name_used(self) -> None:
        brain = _mock_brain()
        plugin = KnowledgePlugin(brain=brain)

        await plugin.remember("content here", name="My Custom Name")
        call_kwargs = brain.learn.call_args.kwargs
        assert call_kwargs["name"] == "My Custom Name"

    @pytest.mark.asyncio
    async def test_error_returns_json(self) -> None:
        brain = _mock_brain()
        brain.find_similar = AsyncMock(side_effect=Exception("DB down"))
        plugin = KnowledgePlugin(brain=brain)

        result = json.loads(await plugin.remember("test"))
        assert result["action"] == "error"
        assert "DB down" in result["message"]

    @pytest.mark.asyncio
    async def test_no_brain_returns_error(self) -> None:
        plugin = KnowledgePlugin(brain=None)
        result = json.loads(await plugin.remember("test"))
        assert result["action"] == "error"


class TestSearchJSON:
    """Search returns JSON with enriched results."""

    @pytest.mark.asyncio
    async def test_search_returns_json(self) -> None:
        results = [
            {
                "id": "c-1",
                "name": "fact1",
                "content": "hello",
                "category": "fact",
                "importance": 0.7,
                "confidence": 0.8,
                "score": 0.95,
            },
        ]
        brain = _mock_brain(search_results=results)
        plugin = KnowledgePlugin(brain=brain)

        data = json.loads(await plugin.search("hello"))
        assert data["action"] == "search"
        assert data["count"] == 1
        assert data["results"][0]["name"] == "fact1"
        assert data["results"][0]["score"] == 0.95

    @pytest.mark.asyncio
    async def test_search_empty(self) -> None:
        brain = _mock_brain(search_results=[])
        plugin = KnowledgePlugin(brain=brain)

        data = json.loads(await plugin.search("nothing"))
        assert data["results"] == []


class TestForgetReal:
    """Forget actually deletes via BrainAccess."""

    @pytest.mark.asyncio
    async def test_forget_deletes(self) -> None:
        results = [{"id": "c-1", "name": "old fact", "content": "x"}]
        brain = _mock_brain(search_results=results)
        plugin = KnowledgePlugin(brain=brain)

        data = json.loads(await plugin.forget("old fact"))
        assert data["action"] == "forgotten"
        assert data["concept_id"] == "c-1"
        brain.forget.assert_called_once_with("c-1")

    @pytest.mark.asyncio
    async def test_forget_not_found(self) -> None:
        brain = _mock_brain(search_results=[])
        plugin = KnowledgePlugin(brain=brain)

        data = json.loads(await plugin.forget("nonexistent"))
        assert data["action"] == "not_found"


class TestWhatDoYouKnow:
    """what_do_you_know uses get_stats."""

    @pytest.mark.asyncio
    async def test_introspection_with_data(self) -> None:
        brain = _mock_brain(
            stats={
                "total_concepts": 42,
                "categories": {"fact": 30, "preference": 12},
                "total_relations": 15,
                "total_episodes": 7,
                "mind_id": "aria",
            }
        )
        plugin = KnowledgePlugin(brain=brain)

        data = json.loads(await plugin.what_do_you_know())
        assert data["action"] == "introspection"
        assert data["total_concepts"] == 42
        assert data["categories"]["fact"] == 30
        assert data["total_relations"] == 15
        assert "42 concept" in data["message"]

    @pytest.mark.asyncio
    async def test_introspection_empty(self) -> None:
        brain = _mock_brain(
            stats={
                "total_concepts": 0,
                "categories": {},
                "total_relations": 0,
                "total_episodes": 0,
            }
        )
        plugin = KnowledgePlugin(brain=brain)

        data = json.loads(await plugin.what_do_you_know())
        assert data["total_concepts"] == 0
        assert "empty" in data["message"].lower()


class TestRecallAbout:
    """recall_about enriches with graph neighbors."""

    @pytest.mark.asyncio
    async def test_recall_with_related(self) -> None:
        results = [
            {
                "id": "c-1",
                "name": "Python",
                "content": "programming language",
                "category": "fact",
                "importance": 0.8,
                "confidence": 0.9,
                "score": 0.9,
            },
        ]
        related = [{"name": "machine learning"}, {"name": "data science"}]
        brain = _mock_brain(search_results=results)
        brain.get_related = AsyncMock(return_value=related)
        plugin = KnowledgePlugin(brain=brain)

        data = json.loads(await plugin.recall_about("Python"))
        assert data["count"] == 1
        assert data["results"][0]["related"] == ["machine learning", "data science"]
