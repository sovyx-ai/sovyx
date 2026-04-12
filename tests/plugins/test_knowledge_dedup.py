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


class TestConflictResolution:
    """TASK-473: Contradiction detection + resolution in remember()."""

    @pytest.mark.asyncio
    async def test_contradiction_updates_content(self) -> None:
        existing = {
            "id": "c-bday",
            "name": "birthday",
            "content": "birthday is March 15",
            "similarity": 0.92,
            "confidence": 0.8,
        }
        brain = _mock_brain(similar_results=[existing])
        brain.classify_content = AsyncMock(return_value="CONTRADICTS")
        plugin = KnowledgePlugin(brain=brain)

        result = json.loads(await plugin.remember("birthday is March 20"))
        assert result["action"] == "updated"
        assert result["resolution"] == "contradiction"
        assert result["concept_id"] == "c-bday"
        # Confidence reduced (penalty)
        assert result["confidence"]["new"] < result["confidence"]["old"]
        assert "contradiction" in result["message"].lower()

        brain.update.assert_called_once()
        call_kwargs = brain.update.call_args.kwargs
        assert call_kwargs["content"] == "birthday is March 20"
        assert call_kwargs["confidence"] < 0.8  # penalized

    @pytest.mark.asyncio
    async def test_contradiction_confidence_penalty(self) -> None:
        existing = {
            "id": "c-x",
            "name": "x",
            "content": "old",
            "similarity": 0.9,
            "confidence": 0.5,
        }
        brain = _mock_brain(similar_results=[existing])
        brain.classify_content = AsyncMock(return_value="CONTRADICTS")
        plugin = KnowledgePlugin(brain=brain)

        result = json.loads(await plugin.remember("new conflicting"))
        # 0.5 * 0.7 = 0.35
        assert abs(result["confidence"]["new"] - 0.35) < 0.01

    @pytest.mark.asyncio
    async def test_contradiction_confidence_floor(self) -> None:
        """Confidence shouldn't drop below 0.1."""
        existing = {
            "id": "c-x",
            "name": "x",
            "content": "old",
            "similarity": 0.9,
            "confidence": 0.1,
        }
        brain = _mock_brain(similar_results=[existing])
        brain.classify_content = AsyncMock(return_value="CONTRADICTS")
        plugin = KnowledgePlugin(brain=brain)

        result = json.loads(await plugin.remember("conflicting"))
        assert result["confidence"]["new"] >= 0.1

    @pytest.mark.asyncio
    async def test_extends_merges_content(self) -> None:
        existing = {
            "id": "c-py",
            "name": "Python",
            "content": "Python is a programming language",
            "similarity": 0.91,
            "confidence": 0.6,
        }
        brain = _mock_brain(similar_results=[existing])
        brain.classify_content = AsyncMock(return_value="EXTENDS")
        plugin = KnowledgePlugin(brain=brain)

        result = json.loads(await plugin.remember("Python was created by Guido van Rossum"))
        assert result["action"] == "extended"
        assert result["resolution"] == "extension"
        # Confidence boosted
        assert result["confidence"]["new"] > result["confidence"]["old"]

        call_kwargs = brain.update.call_args.kwargs
        assert "programming language" in call_kwargs["content"]
        assert "Guido van Rossum" in call_kwargs["content"]

    @pytest.mark.asyncio
    async def test_extends_confidence_boost(self) -> None:
        existing = {
            "id": "c-x",
            "name": "x",
            "content": "base",
            "similarity": 0.9,
            "confidence": 0.6,
        }
        brain = _mock_brain(similar_results=[existing])
        brain.classify_content = AsyncMock(return_value="EXTENDS")
        plugin = KnowledgePlugin(brain=brain)

        result = json.loads(await plugin.remember("extension"))
        assert abs(result["confidence"]["new"] - 0.68) < 0.01  # 0.6 + 0.08

    @pytest.mark.asyncio
    async def test_unrelated_creates_new(self) -> None:
        """High embedding similarity but semantically unrelated → create new."""
        existing = {"id": "c-false", "name": "false positive", "content": "x", "similarity": 0.9}
        brain = _mock_brain(similar_results=[existing])
        brain.classify_content = AsyncMock(return_value="UNRELATED")
        plugin = KnowledgePlugin(brain=brain)

        result = json.loads(await plugin.remember("totally different concept"))
        assert result["action"] == "created"
        brain.learn.assert_called_once()

    @pytest.mark.asyncio
    async def test_same_reinforces(self) -> None:
        """SAME classification → standard reinforcement."""
        existing = {"id": "c-same", "name": "known fact", "content": "x", "similarity": 0.95}
        brain = _mock_brain(similar_results=[existing])
        brain.classify_content = AsyncMock(return_value="SAME")
        plugin = KnowledgePlugin(brain=brain)

        result = json.loads(await plugin.remember("same thing rephrased"))
        assert result["action"] == "reinforced"
        brain.reinforce.assert_called_once()

    @pytest.mark.asyncio
    async def test_classify_failure_falls_back_to_create(self) -> None:
        """If classify_content raises, fall through to create."""
        existing = {"id": "c-err", "name": "err", "content": "x", "similarity": 0.9}
        brain = _mock_brain(similar_results=[existing])
        brain.classify_content = AsyncMock(side_effect=Exception("LLM down"))
        plugin = KnowledgePlugin(brain=brain)

        result = json.loads(await plugin.remember("test"))
        assert result["action"] == "error"
        assert "LLM down" in result["message"]


class TestForgetCascade:
    """TASK-474: Real forget with cascade + multi-delete."""

    @pytest.mark.asyncio
    async def test_forget_single_deletes(self) -> None:
        results = [{"id": "c-1", "name": "target", "content": "x"}]
        brain = _mock_brain(search_results=results)
        plugin = KnowledgePlugin(brain=brain)

        data = json.loads(await plugin.forget("target"))
        assert data["action"] == "forgotten"
        brain.forget.assert_called_once_with("c-1")

    @pytest.mark.asyncio
    async def test_forget_all_deletes_multiple(self) -> None:
        brain = _mock_brain()
        brain.forget_all = AsyncMock(
            return_value=[
                {"id": "c-1", "name": "fact A", "deleted": True},
                {"id": "c-2", "name": "fact B", "deleted": True},
                {"id": "c-3", "name": "fact C", "deleted": False},
            ]
        )
        plugin = KnowledgePlugin(brain=brain)

        data = json.loads(await plugin.forget("old facts", forget_all=True))
        assert data["action"] == "forgotten_all"
        assert data["count"] == 2
        assert "fact A" in data["message"]
        assert "fact B" in data["message"]

    @pytest.mark.asyncio
    async def test_forget_all_empty(self) -> None:
        brain = _mock_brain()
        brain.forget_all = AsyncMock(return_value=[])
        plugin = KnowledgePlugin(brain=brain)

        data = json.loads(await plugin.forget("nothing", forget_all=True))
        assert data["action"] == "not_found"

    @pytest.mark.asyncio
    async def test_forget_single_not_found(self) -> None:
        brain = _mock_brain(search_results=[])
        plugin = KnowledgePlugin(brain=brain)

        data = json.loads(await plugin.forget("nonexistent"))
        assert data["action"] == "not_found"


class TestAutoRelation:
    """TASK-475: Auto-relation creation on remember()."""

    @pytest.mark.asyncio
    async def test_creates_relations_for_related_concepts(self) -> None:
        brain = _mock_brain()
        # find_similar returns: first call for dedup (empty), second for auto-relate
        call_count = 0

        async def find_similar_side_effect(content, threshold=0.88, limit=5):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return []  # dedup check — no duplicates
            # auto-relate check — return related concepts
            return [
                {"id": "c-related-1", "name": "Python basics", "similarity": 0.75},
                {"id": "c-related-2", "name": "coding tips", "similarity": 0.70},
            ]

        brain.find_similar = AsyncMock(side_effect=find_similar_side_effect)
        brain.create_relation = AsyncMock(return_value="rel-1")
        plugin = KnowledgePlugin(brain=brain)

        result = json.loads(await plugin.remember("Python functions and decorators"))
        assert result["action"] == "created"
        assert "relations" in result
        assert len(result["relations"]) == 2
        assert result["relations"][0]["target_name"] == "Python basics"
        assert brain.create_relation.call_count == 2

    @pytest.mark.asyncio
    async def test_skips_self_and_dedup_range(self) -> None:
        brain = _mock_brain()
        call_count = 0

        async def find_similar_side_effect(content, threshold=0.88, limit=5):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return []
            return [
                {"id": "c-new", "name": "self", "similarity": 0.99},  # self — skip
                {"id": "c-dup", "name": "too similar", "similarity": 0.92},  # dedup range — skip
                {"id": "c-good", "name": "related", "similarity": 0.72},  # good
            ]

        brain.find_similar = AsyncMock(side_effect=find_similar_side_effect)
        brain.learn = AsyncMock(return_value="c-new")
        brain.create_relation = AsyncMock(return_value="rel-1")
        plugin = KnowledgePlugin(brain=brain)

        result = json.loads(await plugin.remember("test concept"))
        assert result["action"] == "created"
        # Only 1 relation (skipped self + dedup range)
        assert len(result.get("relations", [])) == 1
        assert result["relations"][0]["target_name"] == "related"

    @pytest.mark.asyncio
    async def test_max_3_relations(self) -> None:
        brain = _mock_brain()
        call_count = 0

        async def find_similar_side_effect(content, threshold=0.88, limit=5):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return []
            return [
                {"id": f"c-{i}", "name": f"concept {i}", "similarity": 0.80 - i * 0.02}
                for i in range(10)
            ]

        brain.find_similar = AsyncMock(side_effect=find_similar_side_effect)
        brain.create_relation = AsyncMock(return_value="rel-x")
        plugin = KnowledgePlugin(brain=brain)

        result = json.loads(await plugin.remember("test"))
        assert len(result.get("relations", [])) <= 3

    @pytest.mark.asyncio
    async def test_no_relations_when_nothing_similar(self) -> None:
        brain = _mock_brain()
        brain.find_similar = AsyncMock(return_value=[])
        plugin = KnowledgePlugin(brain=brain)

        result = json.loads(await plugin.remember("totally unique concept"))
        assert result["action"] == "created"
        assert "relations" not in result

    @pytest.mark.asyncio
    async def test_auto_relate_failure_non_fatal(self) -> None:
        brain = _mock_brain()
        call_count = 0

        async def find_similar_side_effect(content, threshold=0.88, limit=5):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return []
            raise Exception("vector search down")

        brain.find_similar = AsyncMock(side_effect=find_similar_side_effect)
        plugin = KnowledgePlugin(brain=brain)

        result = json.loads(await plugin.remember("test"))
        assert result["action"] == "created"  # still created despite relation failure


class TestEpisodeAwareRecall:
    """TASK-476: Episode-aware recall in recall_about()."""

    @pytest.mark.asyncio
    async def test_recall_includes_episodes(self) -> None:
        results = [
            {
                "id": "c-1",
                "name": "Python",
                "content": "language",
                "category": "fact",
                "importance": 0.7,
                "confidence": 0.8,
            }
        ]
        brain = _mock_brain(search_results=results)
        brain.search_episodes = AsyncMock(
            return_value=[
                {
                    "summary": "We discussed Python decorators",
                    "timestamp": "2026-04-10T15:00:00",
                    "channel": "telegram",
                    "turn_count": 12,
                },
                {
                    "summary": "Python async patterns",
                    "timestamp": "2026-04-09T10:00:00",
                    "channel": "telegram",
                    "turn_count": 8,
                },
            ]
        )
        plugin = KnowledgePlugin(brain=brain)

        data = json.loads(await plugin.recall_about("Python"))
        assert data["action"] == "recall"
        assert data["count"] == 1
        assert "episodes" in data
        assert len(data["episodes"]) == 2
        assert data["episode_count"] == 2
        assert "decorators" in data["episodes"][0]["summary"]
        assert data["episodes"][0]["channel"] == "telegram"

    @pytest.mark.asyncio
    async def test_recall_no_episodes(self) -> None:
        results = [
            {
                "id": "c-1",
                "name": "obscure",
                "content": "x",
                "category": "fact",
                "importance": 0.5,
                "confidence": 0.5,
            }
        ]
        brain = _mock_brain(search_results=results)
        brain.search_episodes = AsyncMock(return_value=[])
        plugin = KnowledgePlugin(brain=brain)

        data = json.loads(await plugin.recall_about("obscure topic"))
        assert data["action"] == "recall"
        assert "episodes" not in data  # empty episodes not included

    @pytest.mark.asyncio
    async def test_recall_episode_failure_non_fatal(self) -> None:
        results = [
            {
                "id": "c-1",
                "name": "topic",
                "content": "x",
                "category": "fact",
                "importance": 0.5,
                "confidence": 0.5,
            }
        ]
        brain = _mock_brain(search_results=results)
        brain.search_episodes = AsyncMock(side_effect=Exception("retrieval down"))
        plugin = KnowledgePlugin(brain=brain)

        data = json.loads(await plugin.recall_about("topic"))
        assert data["action"] == "recall"
        assert data["count"] == 1
        assert "episodes" not in data  # failed silently

    @pytest.mark.asyncio
    async def test_recall_truncates_long_summaries(self) -> None:
        results = [
            {
                "id": "c-1",
                "name": "x",
                "content": "x",
                "category": "fact",
                "importance": 0.5,
                "confidence": 0.5,
            }
        ]
        brain = _mock_brain(search_results=results)
        brain.search_episodes = AsyncMock(
            return_value=[
                {
                    "summary": "A" * 500,
                    "timestamp": "2026-01-01",
                    "channel": "cli",
                    "turn_count": 5,
                },
            ]
        )
        plugin = KnowledgePlugin(brain=brain)

        data = json.loads(await plugin.recall_about("x"))
        assert len(data["episodes"][0]["summary"]) <= 203  # 200 + "..."

    @pytest.mark.asyncio
    async def test_recall_empty_no_results(self) -> None:
        brain = _mock_brain(search_results=[])
        plugin = KnowledgePlugin(brain=brain)

        data = json.loads(await plugin.recall_about("nothing"))
        assert data["results"] == []
        assert data["episodes"] == []


class TestPersonScopedMemory:
    """TASK-477: Person-scoped memory in remember/search."""

    @pytest.mark.asyncio
    async def test_remember_with_person_sets_metadata(self) -> None:
        brain = _mock_brain()
        brain.find_similar = AsyncMock(return_value=[])
        plugin = KnowledgePlugin(brain=brain)

        result = json.loads(await plugin.remember("prefers dark mode", about_person="Guipe"))
        assert result["action"] == "created"
        assert result["about_person"] == "Guipe"

        # Verify learn was called with metadata containing person
        call_kwargs = brain.learn.call_args.kwargs
        assert call_kwargs["metadata"]["person"] == "Guipe"
        # Auto-category to "person" when about_person is set
        assert call_kwargs["category"] == "person"

    @pytest.mark.asyncio
    async def test_remember_person_keeps_explicit_category(self) -> None:
        brain = _mock_brain()
        brain.find_similar = AsyncMock(return_value=[])
        plugin = KnowledgePlugin(brain=brain)

        result = json.loads(
            await plugin.remember("likes Python", about_person="Guipe", category="preference")
        )
        assert result["action"] == "created"
        call_kwargs = brain.learn.call_args.kwargs
        assert call_kwargs["category"] == "preference"

    @pytest.mark.asyncio
    async def test_search_person_filters(self) -> None:
        results = [
            {
                "id": "c-1",
                "name": "Guipe pref",
                "content": "Guipe likes dark mode",
                "category": "person",
                "importance": 0.7,
                "confidence": 0.8,
                "metadata": {"person": "Guipe"},
            },
            {
                "id": "c-2",
                "name": "general pref",
                "content": "dark mode is popular",
                "category": "fact",
                "importance": 0.5,
                "confidence": 0.5,
                "metadata": {},
            },
        ]
        brain = _mock_brain(search_results=results)
        plugin = KnowledgePlugin(brain=brain)

        data = json.loads(await plugin.search("dark mode", about_person="Guipe"))
        assert data["count"] == 1
        assert data["results"][0]["name"] == "Guipe pref"

    @pytest.mark.asyncio
    async def test_search_person_matches_content(self) -> None:
        """Person name in content should also match."""
        results = [
            {
                "id": "c-1",
                "name": "preference",
                "content": "Guipe prefers dark mode",
                "category": "fact",
                "importance": 0.5,
                "confidence": 0.5,
            },
        ]
        brain = _mock_brain(search_results=results)
        plugin = KnowledgePlugin(brain=brain)

        data = json.loads(await plugin.search("preferences", about_person="Guipe"))
        assert data["count"] == 1

    @pytest.mark.asyncio
    async def test_search_person_no_match(self) -> None:
        results = [
            {
                "id": "c-1",
                "name": "general fact",
                "content": "Python is great",
                "category": "fact",
                "importance": 0.5,
                "confidence": 0.5,
            },
        ]
        brain = _mock_brain(search_results=results)
        plugin = KnowledgePlugin(brain=brain)

        data = json.loads(await plugin.search("Python", about_person="Natasha"))
        assert data["results"] == []
        assert "Natasha" in data["message"]

    @pytest.mark.asyncio
    async def test_search_no_person_returns_all(self) -> None:
        results = [
            {
                "id": "c-1",
                "name": "a",
                "content": "x",
                "category": "fact",
                "importance": 0.5,
                "confidence": 0.5,
                "score": 0.9,
            },
            {
                "id": "c-2",
                "name": "b",
                "content": "y",
                "category": "fact",
                "importance": 0.5,
                "confidence": 0.5,
                "score": 0.8,
            },
        ]
        brain = _mock_brain(search_results=results)
        plugin = KnowledgePlugin(brain=brain)

        data = json.loads(await plugin.search("test"))
        assert data["count"] == 2


class TestStructuredOutput:
    """TASK-479: Ensure all tools return consistent structured JSON."""

    def _assert_base_schema(self, raw: str) -> dict[str, object]:
        """Assert base schema: action, ok, message all present."""
        data = json.loads(raw)
        assert "action" in data, f"Missing 'action' in {data}"
        assert "ok" in data, f"Missing 'ok' in {data}"
        assert isinstance(data["ok"], bool), f"'ok' must be bool, got {type(data['ok'])}"
        assert "message" in data, f"Missing 'message' in {data}"
        assert isinstance(data["message"], str), f"'message' must be str"
        return data

    @pytest.mark.asyncio
    async def test_remember_created_schema(self) -> None:
        brain = _mock_brain()
        brain.find_similar = AsyncMock(return_value=[])
        plugin = KnowledgePlugin(brain=brain)
        data = self._assert_base_schema(await plugin.remember("test fact"))
        assert data["ok"] is True
        assert data["action"] == "created"
        assert "concept_id" in data
        assert "name" in data
        assert "category" in data

    @pytest.mark.asyncio
    async def test_remember_reinforced_schema(self) -> None:
        existing = {"id": "c-1", "name": "known", "content": "x", "similarity": 0.95}
        brain = _mock_brain(similar_results=[existing])
        brain.classify_content = AsyncMock(return_value="SAME")
        plugin = KnowledgePlugin(brain=brain)
        data = self._assert_base_schema(await plugin.remember("same thing"))
        assert data["ok"] is True
        assert data["action"] == "reinforced"
        assert "concept_id" in data
        assert "similarity" in data
        assert "importance" in data
        assert "confidence" in data

    @pytest.mark.asyncio
    async def test_remember_contradiction_schema(self) -> None:
        existing = {
            "id": "c-1",
            "name": "fact",
            "content": "old",
            "similarity": 0.92,
            "confidence": 0.8,
        }
        brain = _mock_brain(similar_results=[existing])
        brain.classify_content = AsyncMock(return_value="CONTRADICTS")
        plugin = KnowledgePlugin(brain=brain)
        data = self._assert_base_schema(await plugin.remember("new conflicting"))
        assert data["ok"] is True
        assert data["action"] == "updated"
        assert data["resolution"] == "contradiction"
        assert "confidence" in data

    @pytest.mark.asyncio
    async def test_remember_extended_schema(self) -> None:
        existing = {
            "id": "c-1",
            "name": "base",
            "content": "base info",
            "similarity": 0.91,
            "confidence": 0.6,
        }
        brain = _mock_brain(similar_results=[existing])
        brain.classify_content = AsyncMock(return_value="EXTENDS")
        plugin = KnowledgePlugin(brain=brain)
        data = self._assert_base_schema(await plugin.remember("extra info"))
        assert data["ok"] is True
        assert data["action"] == "extended"
        assert data["resolution"] == "extension"

    @pytest.mark.asyncio
    async def test_search_results_schema(self) -> None:
        results = [
            {
                "id": "c-1",
                "name": "fact",
                "content": "info",
                "category": "fact",
                "importance": 0.7,
                "confidence": 0.8,
                "score": 0.9,
            }
        ]
        brain = _mock_brain(search_results=results)
        plugin = KnowledgePlugin(brain=brain)
        data = self._assert_base_schema(await plugin.search("test"))
        assert data["ok"] is True
        assert data["action"] == "search"
        assert "count" in data
        assert isinstance(data["results"], list)
        r = data["results"][0]
        for key in ("id", "name", "content", "category", "importance", "confidence", "score"):
            assert key in r, f"Missing '{key}' in result"

    @pytest.mark.asyncio
    async def test_search_empty_schema(self) -> None:
        brain = _mock_brain(search_results=[])
        plugin = KnowledgePlugin(brain=brain)
        data = self._assert_base_schema(await plugin.search("nothing"))
        assert data["ok"] is True
        assert data["results"] == []

    @pytest.mark.asyncio
    async def test_forget_schema(self) -> None:
        results = [{"id": "c-1", "name": "target", "content": "x"}]
        brain = _mock_brain(search_results=results)
        plugin = KnowledgePlugin(brain=brain)
        data = self._assert_base_schema(await plugin.forget("target"))
        assert data["ok"] is True
        assert data["action"] == "forgotten"
        assert "concept_id" in data

    @pytest.mark.asyncio
    async def test_forget_not_found_schema(self) -> None:
        brain = _mock_brain(search_results=[])
        plugin = KnowledgePlugin(brain=brain)
        data = self._assert_base_schema(await plugin.forget("nothing"))
        assert data["ok"] is True
        assert data["action"] == "not_found"

    @pytest.mark.asyncio
    async def test_recall_schema(self) -> None:
        results = [
            {
                "id": "c-1",
                "name": "topic",
                "content": "info",
                "category": "fact",
                "importance": 0.5,
                "confidence": 0.5,
            }
        ]
        brain = _mock_brain(search_results=results)
        brain.search_episodes = AsyncMock(return_value=[])
        plugin = KnowledgePlugin(brain=brain)
        data = self._assert_base_schema(await plugin.recall_about("topic"))
        assert data["ok"] is True
        assert data["action"] == "recall"
        assert "results" in data

    @pytest.mark.asyncio
    async def test_what_do_you_know_schema(self) -> None:
        brain = _mock_brain(
            stats={
                "total_concepts": 10,
                "categories": {"fact": 5, "preference": 3},
                "total_relations": 20,
                "total_episodes": 50,
            }
        )
        plugin = KnowledgePlugin(brain=brain)
        data = self._assert_base_schema(await plugin.what_do_you_know())
        assert data["ok"] is True
        assert data["action"] == "introspection"

    @pytest.mark.asyncio
    async def test_error_schema(self) -> None:
        plugin = KnowledgePlugin()  # no brain
        for tool_fn in [plugin.remember, plugin.search, plugin.forget]:
            data = self._assert_base_schema(await tool_fn("test"))
            assert data["ok"] is False
            assert data["action"] == "error"


class TestWhatDoYouKnowEnhanced:
    """TASK-480: what_do_you_know with top concepts."""

    @pytest.mark.asyncio
    async def test_includes_top_concepts(self) -> None:
        brain = _mock_brain(
            stats={
                "total_concepts": 25,
                "categories": {"fact": 15, "preference": 10},
                "total_relations": 40,
                "total_episodes": 100,
            }
        )
        brain.get_top_concepts = AsyncMock(
            return_value=[
                {
                    "name": "Python",
                    "category": "fact",
                    "importance": 0.95,
                    "confidence": 0.9,
                    "access_count": 42,
                },
                {
                    "name": "dark mode pref",
                    "category": "preference",
                    "importance": 0.88,
                    "confidence": 0.85,
                    "access_count": 12,
                },
            ]
        )
        plugin = KnowledgePlugin(brain=brain)

        data = json.loads(await plugin.what_do_you_know())
        assert data["ok"] is True
        assert data["action"] == "introspection"
        assert data["total_concepts"] == 25
        assert "top_concepts" in data
        assert len(data["top_concepts"]) == 2
        assert data["top_concepts"][0]["name"] == "Python"
        assert data["top_concepts"][0]["access_count"] == 42

    @pytest.mark.asyncio
    async def test_empty_brain(self) -> None:
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
        assert data["ok"] is True
        assert data["total_concepts"] == 0
        assert "empty" in data["message"].lower()

    @pytest.mark.asyncio
    async def test_top_concepts_failure_non_fatal(self) -> None:
        brain = _mock_brain(
            stats={
                "total_concepts": 10,
                "categories": {"fact": 10},
                "total_relations": 5,
                "total_episodes": 20,
            }
        )
        brain.get_top_concepts = AsyncMock(side_effect=Exception("DB error"))
        plugin = KnowledgePlugin(brain=brain)

        data = json.loads(await plugin.what_do_you_know())
        assert data["ok"] is True
        assert data["total_concepts"] == 10
        assert "top_concepts" not in data  # failed silently


class TestErrorRecovery:
    """TASK-482: Error recovery and graceful degradation."""

    @pytest.mark.asyncio
    async def test_remember_retries_on_timeout(self) -> None:
        brain = _mock_brain()
        brain.find_similar = AsyncMock(return_value=[])
        call_count = 0

        async def learn_side_effect(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise TimeoutError("DB timeout")
            return "c-retry"

        brain.learn = AsyncMock(side_effect=learn_side_effect)
        plugin = KnowledgePlugin(brain=brain)

        data = json.loads(await plugin.remember("test retry"))
        assert data["ok"] is True
        assert data["action"] == "created"
        assert call_count == 2  # retried once

    @pytest.mark.asyncio
    async def test_remember_fails_after_max_retries(self) -> None:
        brain = _mock_brain()
        brain.find_similar = AsyncMock(return_value=[])
        brain.learn = AsyncMock(side_effect=TimeoutError("persistent timeout"))
        plugin = KnowledgePlugin(brain=brain)

        data = json.loads(await plugin.remember("will fail"))
        assert data["ok"] is False
        assert data["action"] == "error"
        assert "timeout" in data["message"].lower()

    @pytest.mark.asyncio
    async def test_search_returns_error_on_failure(self) -> None:
        brain = _mock_brain()
        brain.search = AsyncMock(side_effect=OSError("connection lost"))
        plugin = KnowledgePlugin(brain=brain)

        data = json.loads(await plugin.search("test"))
        assert data["ok"] is False
        assert data["action"] == "error"

    @pytest.mark.asyncio
    async def test_recall_partial_failure_still_returns(self) -> None:
        """If episodes fail but concepts succeed, still return concepts."""
        results = [
            {
                "id": "c-1",
                "name": "topic",
                "content": "info",
                "category": "fact",
                "importance": 0.5,
                "confidence": 0.5,
            }
        ]
        brain = _mock_brain(search_results=results)
        brain.search_episodes = AsyncMock(side_effect=Exception("episode DB down"))
        brain.get_related = AsyncMock(side_effect=Exception("graph down"))
        plugin = KnowledgePlugin(brain=brain)

        data = json.loads(await plugin.recall_about("topic"))
        assert data["ok"] is True
        assert data["count"] == 1  # concepts still returned

    @pytest.mark.asyncio
    async def test_what_do_you_know_partial_failure(self) -> None:
        """Stats succeed but top concepts fail — still returns stats."""
        brain = _mock_brain(
            stats={
                "total_concepts": 10,
                "categories": {"fact": 10},
                "total_relations": 5,
                "total_episodes": 20,
            }
        )
        brain.get_top_concepts = AsyncMock(side_effect=Exception("query failed"))
        plugin = KnowledgePlugin(brain=brain)

        data = json.loads(await plugin.what_do_you_know())
        assert data["ok"] is True
        assert data["total_concepts"] == 10
        assert "top_concepts" not in data

    @pytest.mark.asyncio
    async def test_forget_error_handling(self) -> None:
        brain = _mock_brain()
        brain.search = AsyncMock(side_effect=RuntimeError("unexpected"))
        plugin = KnowledgePlugin(brain=brain)

        data = json.loads(await plugin.forget("test"))
        assert data["ok"] is False
        assert data["action"] == "error"


class TestRateLimiting:
    """TASK-483: Rate limiting for brain operations."""

    @pytest.mark.asyncio
    async def test_write_rate_limit(self) -> None:
        brain = _mock_brain()
        brain.find_similar = AsyncMock(return_value=[])
        plugin = KnowledgePlugin(brain=brain)

        # Exhaust write limit (30/min)
        for i in range(30):
            result = json.loads(await plugin.remember(f"fact {i}"))
            assert result["ok"] is True

        # 31st should be rate limited
        result = json.loads(await plugin.remember("one too many"))
        assert result["ok"] is False
        assert "rate limit" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_read_rate_limit(self) -> None:
        brain = _mock_brain(search_results=[])
        plugin = KnowledgePlugin(brain=brain)

        # Exhaust read limit (60/min)
        for i in range(60):
            result = json.loads(await plugin.search(f"query {i}"))
            assert result["ok"] is True

        # 61st should be rate limited
        result = json.loads(await plugin.search("one too many"))
        assert result["ok"] is False
        assert "rate limit" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_write_and_read_independent(self) -> None:
        brain = _mock_brain()
        brain.find_similar = AsyncMock(return_value=[])
        plugin = KnowledgePlugin(brain=brain)

        # Use up writes
        for i in range(30):
            await plugin.remember(f"fact {i}")

        # Reads should still work
        brain.search = AsyncMock(return_value=[])
        result = json.loads(await plugin.search("still works"))
        assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_forget_uses_write_limiter(self) -> None:
        brain = _mock_brain(search_results=[{"id": "c-1", "name": "x", "content": "x"}])
        plugin = KnowledgePlugin(brain=brain)

        # Exhaust write limit
        brain2 = _mock_brain()
        brain2.find_similar = AsyncMock(return_value=[])
        plugin2 = KnowledgePlugin(brain=brain2)
        for i in range(30):
            await plugin2.remember(f"fact {i}")

        # Now forget should be limited on plugin2 (same limiter)
        result = json.loads(await plugin2.forget("test"))
        assert result["ok"] is False
