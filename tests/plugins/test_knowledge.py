"""Tests for Sovyx Knowledge Plugin (TASK-445)."""

from __future__ import annotations

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
    brain.learn = AsyncMock(return_value=learn_id)
    return brain


class TestKnowledgePlugin:
    """Basic plugin tests."""

    def test_name(self) -> None:
        assert KnowledgePlugin().name == "knowledge"

    def test_version(self) -> None:
        assert KnowledgePlugin().version == "1.0.0"

    def test_description(self) -> None:
        assert "knowledge" in KnowledgePlugin().description.lower()


class TestRemember:
    """Tests for remember tool."""

    @pytest.mark.anyio()
    async def test_remember_basic(self) -> None:
        brain = _mock_brain()
        p = KnowledgePlugin(brain=brain)
        result = await p.remember("I prefer dark mode", name="dark-mode-pref")
        assert "Remembered" in result
        assert "concept-123" in result
        brain.learn.assert_called_once()

    @pytest.mark.anyio()
    async def test_remember_auto_name(self) -> None:
        brain = _mock_brain()
        p = KnowledgePlugin(brain=brain)
        result = await p.remember("Short info")
        assert "Remembered" in result
        # Name auto-generated from content
        call_kwargs = brain.learn.call_args
        assert call_kwargs[1]["name"] == "Short info"

    @pytest.mark.anyio()
    async def test_remember_long_auto_name(self) -> None:
        brain = _mock_brain()
        p = KnowledgePlugin(brain=brain)
        long_text = "A" * 100
        await p.remember(long_text)
        call_kwargs = brain.learn.call_args
        assert call_kwargs[1]["name"].endswith("...")
        assert len(call_kwargs[1]["name"]) <= 54  # 50 + "..."

    @pytest.mark.anyio()
    async def test_remember_no_brain(self) -> None:
        p = KnowledgePlugin()
        result = await p.remember("test")
        assert "Error" in result

    @pytest.mark.anyio()
    async def test_remember_error(self) -> None:
        brain = _mock_brain()
        brain.learn = AsyncMock(side_effect=RuntimeError("db error"))
        p = KnowledgePlugin(brain=brain)
        result = await p.remember("test")
        assert "Error" in result


class TestSearch:
    """Tests for search tool."""

    @pytest.mark.anyio()
    async def test_search_found(self) -> None:
        results = [
            {"name": "dark-mode", "content": "User prefers dark mode"},
            {"name": "lang", "content": "User speaks Portuguese"},
        ]
        brain = _mock_brain(search_results=results)
        p = KnowledgePlugin(brain=brain)
        result = await p.search("preferences")
        assert "2 memory" in result
        assert "dark-mode" in result

    @pytest.mark.anyio()
    async def test_search_empty(self) -> None:
        brain = _mock_brain(search_results=[])
        p = KnowledgePlugin(brain=brain)
        result = await p.search("nonexistent")
        assert "No memories" in result

    @pytest.mark.anyio()
    async def test_search_truncates_content(self) -> None:
        results = [{"name": "long", "content": "X" * 300}]
        brain = _mock_brain(search_results=results)
        p = KnowledgePlugin(brain=brain)
        result = await p.search("long")
        assert "..." in result

    @pytest.mark.anyio()
    async def test_search_no_brain(self) -> None:
        p = KnowledgePlugin()
        result = await p.search("test")
        assert "Error" in result

    @pytest.mark.anyio()
    async def test_search_error(self) -> None:
        brain = _mock_brain()
        brain.search = AsyncMock(side_effect=RuntimeError("db"))
        p = KnowledgePlugin(brain=brain)
        result = await p.search("test")
        assert "Error" in result

    @pytest.mark.anyio()
    async def test_search_limit_clamped(self) -> None:
        brain = _mock_brain()
        p = KnowledgePlugin(brain=brain)
        await p.search("test", limit=99)
        # Should be clamped to max_results (10)
        call_kwargs = brain.search.call_args
        assert call_kwargs[1]["limit"] == 10


class TestForget:
    """Tests for forget tool."""

    @pytest.mark.anyio()
    async def test_forget_found(self) -> None:
        results = [{"name": "old-info", "content": "stale data"}]
        brain = _mock_brain(search_results=results)
        p = KnowledgePlugin(brain=brain)
        result = await p.forget("old info")
        assert "old-info" in result
        assert "review" in result.lower()

    @pytest.mark.anyio()
    async def test_forget_not_found(self) -> None:
        brain = _mock_brain(search_results=[])
        p = KnowledgePlugin(brain=brain)
        result = await p.forget("ghost")
        assert "Nothing found" in result

    @pytest.mark.anyio()
    async def test_forget_no_brain(self) -> None:
        p = KnowledgePlugin()
        result = await p.forget("test")
        assert "Error" in result

    @pytest.mark.anyio()
    async def test_forget_error(self) -> None:
        brain = _mock_brain()
        brain.search = AsyncMock(side_effect=RuntimeError("db"))
        p = KnowledgePlugin(brain=brain)
        result = await p.forget("test")
        assert "Error" in result


class TestRecallAbout:
    """Tests for recall_about tool."""

    @pytest.mark.anyio()
    async def test_recall_found(self) -> None:
        results = [
            {"name": "crypto-bg", "content": "Investor since 2017", "category": "fact"},
        ]
        brain = _mock_brain(search_results=results)
        p = KnowledgePlugin(brain=brain)
        result = await p.recall_about("crypto")
        assert "crypto" in result
        assert "2017" in result
        assert "[fact]" in result

    @pytest.mark.anyio()
    async def test_recall_empty(self) -> None:
        brain = _mock_brain(search_results=[])
        p = KnowledgePlugin(brain=brain)
        result = await p.recall_about("unknown")
        assert "don't have" in result

    @pytest.mark.anyio()
    async def test_recall_truncates(self) -> None:
        results = [{"name": "big", "content": "Y" * 500, "category": ""}]
        brain = _mock_brain(search_results=results)
        p = KnowledgePlugin(brain=brain)
        result = await p.recall_about("big")
        assert "..." in result

    @pytest.mark.anyio()
    async def test_recall_no_brain(self) -> None:
        p = KnowledgePlugin()
        result = await p.recall_about("test")
        assert "Error" in result

    @pytest.mark.anyio()
    async def test_recall_error(self) -> None:
        brain = _mock_brain()
        brain.search = AsyncMock(side_effect=RuntimeError("db"))
        p = KnowledgePlugin(brain=brain)
        result = await p.recall_about("test")
        assert "Error" in result


class TestWhatDoYouKnow:
    """Tests for what_do_you_know tool."""

    @pytest.mark.anyio()
    async def test_has_memories(self) -> None:
        results = [
            {"name": "fact-1", "category": "fact"},
            {"name": "pref-1", "category": "preference"},
        ]
        brain = _mock_brain(search_results=results)
        p = KnowledgePlugin(brain=brain)
        result = await p.what_do_you_know()
        assert "2 relevant" in result
        assert "fact-1" in result
        assert "[preference]" in result

    @pytest.mark.anyio()
    async def test_empty_memory(self) -> None:
        brain = _mock_brain(search_results=[])
        p = KnowledgePlugin(brain=brain)
        result = await p.what_do_you_know()
        assert "empty" in result.lower()

    @pytest.mark.anyio()
    async def test_no_brain(self) -> None:
        p = KnowledgePlugin()
        result = await p.what_do_you_know()
        assert "Error" in result

    @pytest.mark.anyio()
    async def test_error(self) -> None:
        brain = _mock_brain()
        brain.search = AsyncMock(side_effect=RuntimeError("db"))
        p = KnowledgePlugin(brain=brain)
        result = await p.what_do_you_know()
        assert "Error" in result
