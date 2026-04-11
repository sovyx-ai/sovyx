"""Tests for Sovyx Knowledge Plugin (TASK-445, updated for v2.0).

Basic plugin interface tests + backward compatibility checks.
Comprehensive dedup/JSON tests are in test_knowledge_dedup.py.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from sovyx.plugins.official.knowledge import KnowledgePlugin


def _mock_brain(
    search_results: list[dict[str, object]] | None = None,
    learn_id: str = "concept-123",
) -> AsyncMock:
    """Create a mock BrainAccess."""
    brain = AsyncMock()
    brain.search = AsyncMock(return_value=search_results or [])
    brain.find_similar = AsyncMock(return_value=[])
    brain.learn = AsyncMock(return_value=learn_id)
    brain.forget = AsyncMock(return_value=True)
    brain.update = AsyncMock(return_value=True)
    brain.boost_importance = AsyncMock(return_value=True)
    brain.get_related = AsyncMock(return_value=[])
    brain.get_stats = AsyncMock(
        return_value={
            "total_concepts": 0,
            "categories": {},
            "total_relations": 0,
            "total_episodes": 0,
        }
    )
    return brain


class TestKnowledgePlugin:
    """Basic plugin tests."""

    def test_name(self) -> None:
        assert KnowledgePlugin().name == "knowledge"

    def test_version(self) -> None:
        assert KnowledgePlugin().version == "2.0.0"

    def test_description(self) -> None:
        assert "knowledge" in KnowledgePlugin().description.lower()


class TestRemember:
    """Tests for remember tool."""

    @pytest.mark.anyio
    async def test_remember_basic(self) -> None:
        brain = _mock_brain()
        p = KnowledgePlugin(brain=brain)
        raw = await p.remember("I prefer dark mode", name="dark-mode-pref")
        data = json.loads(raw)
        assert data["action"] == "created"
        assert data["concept_id"] == "concept-123"
        brain.learn.assert_called_once()

    @pytest.mark.anyio
    async def test_remember_auto_name(self) -> None:
        brain = _mock_brain()
        p = KnowledgePlugin(brain=brain)
        raw = await p.remember("Short info")
        data = json.loads(raw)
        assert data["action"] == "created"
        call_kwargs = brain.learn.call_args.kwargs
        assert call_kwargs["name"] == "Short info"

    @pytest.mark.anyio
    async def test_remember_long_auto_name(self) -> None:
        brain = _mock_brain()
        p = KnowledgePlugin(brain=brain)
        long_text = "A" * 100
        await p.remember(long_text)
        call_kwargs = brain.learn.call_args.kwargs
        assert call_kwargs["name"].endswith("...")
        assert len(call_kwargs["name"]) <= 54  # 50 + "..."

    @pytest.mark.anyio
    async def test_remember_no_brain(self) -> None:
        p = KnowledgePlugin()
        raw = await p.remember("test")
        data = json.loads(raw)
        assert data["action"] == "error"

    @pytest.mark.anyio
    async def test_remember_error(self) -> None:
        brain = _mock_brain()
        brain.find_similar = AsyncMock(side_effect=RuntimeError("db error"))
        p = KnowledgePlugin(brain=brain)
        raw = await p.remember("test")
        data = json.loads(raw)
        assert data["action"] == "error"
        assert "db error" in data["message"]


class TestSearch:
    """Tests for search tool."""

    @pytest.mark.anyio
    async def test_search_found(self) -> None:
        results = [
            {
                "id": "c1",
                "name": "dark-mode",
                "content": "User prefers dark mode",
                "category": "preference",
                "importance": 0.5,
                "confidence": 0.5,
                "score": 0.9,
            },
            {
                "id": "c2",
                "name": "lang",
                "content": "User speaks Portuguese",
                "category": "fact",
                "importance": 0.5,
                "confidence": 0.5,
                "score": 0.8,
            },
        ]
        brain = _mock_brain(search_results=results)
        p = KnowledgePlugin(brain=brain)
        raw = await p.search("preferences")
        data = json.loads(raw)
        assert data["count"] == 2
        assert data["results"][0]["name"] == "dark-mode"

    @pytest.mark.anyio
    async def test_search_empty(self) -> None:
        brain = _mock_brain(search_results=[])
        p = KnowledgePlugin(brain=brain)
        raw = await p.search("nonexistent")
        data = json.loads(raw)
        assert data["results"] == []

    @pytest.mark.anyio
    async def test_search_truncates_content(self) -> None:
        results = [
            {
                "id": "c1",
                "name": "long",
                "content": "X" * 500,
                "category": "fact",
                "importance": 0.5,
                "confidence": 0.5,
                "score": 0.5,
            }
        ]
        brain = _mock_brain(search_results=results)
        p = KnowledgePlugin(brain=brain)
        raw = await p.search("long")
        data = json.loads(raw)
        assert data["results"][0]["content"].endswith("...")
        assert len(data["results"][0]["content"]) <= 304  # 300 + "..."

    @pytest.mark.anyio
    async def test_search_no_brain(self) -> None:
        p = KnowledgePlugin()
        raw = await p.search("test")
        data = json.loads(raw)
        assert data["action"] == "error"

    @pytest.mark.anyio
    async def test_search_error(self) -> None:
        brain = _mock_brain()
        brain.search = AsyncMock(side_effect=RuntimeError("fail"))
        p = KnowledgePlugin(brain=brain)
        raw = await p.search("test")
        data = json.loads(raw)
        assert data["action"] == "error"

    @pytest.mark.anyio
    async def test_search_limit_clamped(self) -> None:
        brain = _mock_brain()
        p = KnowledgePlugin(brain=brain)
        await p.search("test", limit=100)
        brain.search.assert_called_once_with("test", limit=10)

    @pytest.mark.anyio
    async def test_search_limit_min(self) -> None:
        brain = _mock_brain()
        p = KnowledgePlugin(brain=brain)
        await p.search("test", limit=0)
        brain.search.assert_called_once_with("test", limit=1)


class TestForget:
    """Tests for forget tool."""

    @pytest.mark.anyio
    async def test_forget_deletes(self) -> None:
        results = [{"id": "c-1", "name": "target"}]
        brain = _mock_brain(search_results=results)
        p = KnowledgePlugin(brain=brain)
        raw = await p.forget("target")
        data = json.loads(raw)
        assert data["action"] == "forgotten"
        brain.forget.assert_called_once_with("c-1")

    @pytest.mark.anyio
    async def test_forget_not_found(self) -> None:
        brain = _mock_brain(search_results=[])
        p = KnowledgePlugin(brain=brain)
        raw = await p.forget("nonexistent")
        data = json.loads(raw)
        assert data["action"] == "not_found"

    @pytest.mark.anyio
    async def test_forget_no_brain(self) -> None:
        p = KnowledgePlugin()
        raw = await p.forget("test")
        data = json.loads(raw)
        assert data["action"] == "error"


class TestRecallAbout:
    """Tests for recall_about tool."""

    @pytest.mark.anyio
    async def test_recall_found(self) -> None:
        results = [
            {
                "id": "c-1",
                "name": "Python",
                "content": "language",
                "category": "fact",
                "importance": 0.7,
                "confidence": 0.8,
                "score": 0.9,
            },
        ]
        brain = _mock_brain(search_results=results)
        p = KnowledgePlugin(brain=brain)
        raw = await p.recall_about("Python")
        data = json.loads(raw)
        assert data["count"] == 1
        assert data["results"][0]["name"] == "Python"

    @pytest.mark.anyio
    async def test_recall_empty(self) -> None:
        brain = _mock_brain(search_results=[])
        p = KnowledgePlugin(brain=brain)
        raw = await p.recall_about("nothing")
        data = json.loads(raw)
        assert data["results"] == []

    @pytest.mark.anyio
    async def test_recall_no_brain(self) -> None:
        p = KnowledgePlugin()
        raw = await p.recall_about("test")
        data = json.loads(raw)
        assert data["action"] == "error"


class TestWhatDoYouKnow:
    """Tests for what_do_you_know tool."""

    @pytest.mark.anyio
    async def test_empty_brain(self) -> None:
        brain = _mock_brain()
        p = KnowledgePlugin(brain=brain)
        raw = await p.what_do_you_know()
        data = json.loads(raw)
        assert data["total_concepts"] == 0

    @pytest.mark.anyio
    async def test_no_brain(self) -> None:
        p = KnowledgePlugin()
        raw = await p.what_do_you_know()
        data = json.loads(raw)
        assert data["action"] == "error"
