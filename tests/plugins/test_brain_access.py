"""Tests for BrainAccess expanded API (TASK-470).

Covers: search, find_similar, get_related, search_episodes, learn,
forget, update + permission denial for each write op.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sovyx.plugins.context import BrainAccess
from sovyx.plugins.permissions import PermissionDeniedError, PermissionEnforcer

# ── Fixtures ──


def _make_concept(
    *,
    concept_id: str = "c-001",
    name: str = "test concept",
    content: str = "test content",
    category: str = "fact",
    importance: float = 0.5,
    confidence: float = 0.5,
    access_count: int = 0,
    source: str = "plugin:test",
) -> MagicMock:
    """Create a mock Concept object."""
    c = MagicMock()
    c.id = concept_id
    c.name = name
    c.content = content
    cat = MagicMock()
    cat.value = category
    c.category = cat
    c.importance = importance
    c.confidence = confidence
    c.access_count = access_count
    c.source = source
    return c


def _make_episode(
    *,
    episode_id: str = "ep-001",
    user_input: str = "hello",
    assistant_response: str = "hi there",
    summary: str = "greeting exchange",
    importance: float = 0.5,
    emotional_valence: float = 0.0,
    emotional_arousal: float = 0.0,
    conversation_id: str = "conv-001",
) -> MagicMock:
    """Create a mock Episode object."""
    ep = MagicMock()
    ep.id = episode_id
    ep.user_input = user_input
    ep.assistant_response = assistant_response
    ep.summary = summary
    ep.importance = importance
    ep.emotional_valence = emotional_valence
    ep.emotional_arousal = emotional_arousal
    ep.conversation_id = conversation_id
    ep.metadata = {}
    return ep


def _make_brain_access(
    *,
    permissions: set[str] | None = None,
    write: bool = True,
) -> tuple[BrainAccess, AsyncMock]:
    """Create BrainAccess with mock brain and real PermissionEnforcer."""
    perms = permissions if permissions is not None else {"brain:read", "brain:write"}
    enforcer = PermissionEnforcer(plugin_name="test-plugin", granted=perms)
    brain = AsyncMock()
    brain._embedding = AsyncMock()
    brain._concepts = AsyncMock()
    brain._retrieval = AsyncMock()
    ba = BrainAccess(
        brain=brain,
        enforcer=enforcer,
        write_allowed=write,
        plugin_name="test-plugin",
        mind_id="test-mind",
    )
    return ba, brain


# ── search() ──


class TestSearch:
    @pytest.mark.asyncio
    async def test_search_returns_enriched_dicts(self) -> None:
        ba, brain = _make_brain_access()
        concept = _make_concept(name="dark mode", content="user prefers dark mode")
        brain.search = AsyncMock(return_value=[(concept, 0.95)])

        results = await ba.search("dark mode")
        assert len(results) == 1
        assert results[0]["name"] == "dark mode"
        assert results[0]["content"] == "user prefers dark mode"
        assert results[0]["score"] == 0.95
        assert results[0]["id"] == "c-001"
        assert results[0]["confidence"] == 0.5

    @pytest.mark.asyncio
    async def test_search_caps_limit_at_50(self) -> None:
        ba, brain = _make_brain_access()
        brain.search = AsyncMock(return_value=[])

        await ba.search("test", limit=100)
        call_args = brain.search.call_args
        assert call_args.kwargs["limit"] == 50

    @pytest.mark.asyncio
    async def test_search_permission_denied(self) -> None:
        ba, brain = _make_brain_access(permissions=set())  # no perms
        brain.search = AsyncMock(return_value=[])
        with pytest.raises(PermissionDeniedError):
            await ba.search("test")


# ── find_similar() ──


class TestFindSimilar:
    @pytest.mark.asyncio
    async def test_find_similar_returns_above_threshold(self) -> None:
        ba, brain = _make_brain_access()
        concept = _make_concept(name="dark mode pref")
        # Distance 0.1 → similarity ≈ 0.995
        brain._concepts.search_by_embedding = AsyncMock(return_value=[(concept, 0.1)])
        brain._embedding.encode = AsyncMock(return_value=[0.1] * 384)

        results = await ba.find_similar("user prefers dark mode")
        assert len(results) == 1
        assert "similarity" in results[0]
        assert results[0]["similarity"] > 0.9

    @pytest.mark.asyncio
    async def test_find_similar_filters_below_threshold(self) -> None:
        ba, brain = _make_brain_access()
        concept = _make_concept()
        # Distance 2.0 → similarity ≈ -1.0 → clamped to 0.0
        brain._concepts.search_by_embedding = AsyncMock(return_value=[(concept, 2.0)])
        brain._embedding.encode = AsyncMock(return_value=[0.1] * 384)

        results = await ba.find_similar("completely different", threshold=0.9)
        assert len(results) == 0

    @pytest.mark.asyncio
    async def test_find_similar_custom_threshold(self) -> None:
        ba, brain = _make_brain_access()
        concept = _make_concept()
        # Distance 0.6 → similarity ≈ 0.82
        brain._concepts.search_by_embedding = AsyncMock(return_value=[(concept, 0.6)])
        brain._embedding.encode = AsyncMock(return_value=[0.1] * 384)

        # 0.82 < 0.9 → no results at default threshold
        results_strict = await ba.find_similar("test", threshold=0.9)
        assert len(results_strict) == 0

        # 0.82 > 0.7 → results at lower threshold
        results_lenient = await ba.find_similar("test", threshold=0.7)
        assert len(results_lenient) == 1

    @pytest.mark.asyncio
    async def test_find_similar_falls_back_to_fts5(self) -> None:
        ba, brain = _make_brain_access()
        concept = _make_concept()
        brain._concepts.search_by_embedding = AsyncMock(
            side_effect=Exception("sqlite-vec unavailable")
        )
        brain._concepts.search_by_text = AsyncMock(return_value=[(concept, 1.0)])
        brain._embedding.encode = AsyncMock(return_value=[0.1] * 384)

        results = await ba.find_similar("test")
        assert len(results) == 1
        brain._concepts.search_by_text.assert_called_once()

    @pytest.mark.asyncio
    async def test_find_similar_permission_denied(self) -> None:
        ba, _ = _make_brain_access(permissions=set())
        with pytest.raises(PermissionDeniedError):
            await ba.find_similar("test")


# ── get_related() ──


class TestGetRelated:
    @pytest.mark.asyncio
    async def test_get_related_returns_neighbors(self) -> None:
        ba, brain = _make_brain_access()
        neighbor = _make_concept(concept_id="c-002", name="related concept")
        brain.get_related = AsyncMock(return_value=[neighbor])

        results = await ba.get_related("c-001")
        assert len(results) == 1
        assert results[0]["id"] == "c-002"
        assert results[0]["name"] == "related concept"

    @pytest.mark.asyncio
    async def test_get_related_empty(self) -> None:
        ba, brain = _make_brain_access()
        brain.get_related = AsyncMock(return_value=[])

        results = await ba.get_related("c-001")
        assert results == []

    @pytest.mark.asyncio
    async def test_get_related_caps_limit(self) -> None:
        ba, brain = _make_brain_access()
        brain.get_related = AsyncMock(return_value=[])

        await ba.get_related("c-001", limit=100)
        call_args = brain.get_related.call_args
        assert (
            call_args.kwargs.get("limit", call_args.args[-1] if len(call_args.args) > 1 else 50)
            <= 50
        )

    @pytest.mark.asyncio
    async def test_get_related_permission_denied(self) -> None:
        ba, _ = _make_brain_access(permissions=set())
        with pytest.raises(PermissionDeniedError):
            await ba.get_related("c-001")


# ── search_episodes() ──


class TestSearchEpisodes:
    @pytest.mark.asyncio
    async def test_search_episodes_returns_dicts(self) -> None:
        ba, brain = _make_brain_access()
        ep = _make_episode(user_input="what is 1+1?", assistant_response="2!")
        brain._retrieval.search_episodes = AsyncMock(return_value=[(ep, 0.8)])

        results = await ba.search_episodes("math")
        assert len(results) == 1
        assert results[0]["user_input"] == "what is 1+1?"
        assert results[0]["assistant_response"] == "2!"
        assert results[0]["summary"] == "greeting exchange"

    @pytest.mark.asyncio
    async def test_search_episodes_empty(self) -> None:
        ba, brain = _make_brain_access()
        brain._retrieval.search_episodes = AsyncMock(return_value=[])

        results = await ba.search_episodes("nothing")
        assert results == []

    @pytest.mark.asyncio
    async def test_search_episodes_permission_denied(self) -> None:
        ba, _ = _make_brain_access(permissions=set())
        with pytest.raises(PermissionDeniedError):
            await ba.search_episodes("test")


# ── forget() ──


class TestForget:
    @pytest.mark.asyncio
    async def test_forget_deletes_concept(self) -> None:
        ba, brain = _make_brain_access()
        concept = _make_concept()
        brain.get_concept = AsyncMock(return_value=concept)
        brain._concepts.delete = AsyncMock()

        result = await ba.forget("c-001")
        assert result is True
        brain._concepts.delete.assert_called_once()

    @pytest.mark.asyncio
    async def test_forget_returns_false_if_not_found(self) -> None:
        ba, brain = _make_brain_access()
        brain.get_concept = AsyncMock(return_value=None)

        result = await ba.forget("nonexistent")
        assert result is False
        brain._concepts.delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_forget_permission_denied_no_write(self) -> None:
        ba, _ = _make_brain_access(permissions={"brain:read"})
        with pytest.raises(PermissionDeniedError):
            await ba.forget("c-001")

    @pytest.mark.asyncio
    async def test_forget_permission_denied_write_flag_false(self) -> None:
        ba, brain = _make_brain_access(write=False)
        brain.get_concept = AsyncMock(return_value=_make_concept())
        with pytest.raises(PermissionDeniedError):
            await ba.forget("c-001")


# ── update() ──


class TestUpdate:
    @pytest.mark.asyncio
    async def test_update_modifies_fields(self) -> None:
        ba, brain = _make_brain_access()
        concept = _make_concept(content="old content", importance=0.5)
        brain.get_concept = AsyncMock(return_value=concept)
        brain._concepts.update = AsyncMock()

        result = await ba.update(
            "c-001",
            content="new content",
            importance=0.8,
        )
        assert result is True
        assert concept.content == "new content"
        assert concept.importance == 0.8
        brain._concepts.update.assert_called_once_with(concept)

    @pytest.mark.asyncio
    async def test_update_leaves_none_fields_unchanged(self) -> None:
        ba, brain = _make_brain_access()
        concept = _make_concept(name="original", content="original content")
        brain.get_concept = AsyncMock(return_value=concept)
        brain._concepts.update = AsyncMock()

        await ba.update("c-001", content="new content")
        assert concept.name == "original"
        assert concept.content == "new content"

    @pytest.mark.asyncio
    async def test_update_returns_false_if_not_found(self) -> None:
        ba, brain = _make_brain_access()
        brain.get_concept = AsyncMock(return_value=None)

        result = await ba.update("nonexistent", content="x")
        assert result is False

    @pytest.mark.asyncio
    async def test_update_clamps_importance(self) -> None:
        ba, brain = _make_brain_access()
        concept = _make_concept()
        brain.get_concept = AsyncMock(return_value=concept)
        brain._concepts.update = AsyncMock()

        await ba.update("c-001", importance=1.5)
        assert concept.importance == 1.0

        await ba.update("c-001", importance=-0.5)
        assert concept.importance == 0.0

    @pytest.mark.asyncio
    async def test_update_rejects_oversized_content(self) -> None:
        ba, brain = _make_brain_access()
        concept = _make_concept()
        brain.get_concept = AsyncMock(return_value=concept)

        with pytest.raises(ValueError, match="10240"):
            await ba.update("c-001", content="x" * 11_000)

    @pytest.mark.asyncio
    async def test_update_permission_denied(self) -> None:
        ba, _ = _make_brain_access(permissions={"brain:read"})
        with pytest.raises(PermissionDeniedError):
            await ba.update("c-001", content="new")


# ── learn() expanded ──


class TestLearnExpanded:
    @pytest.mark.asyncio
    async def test_learn_passes_importance_confidence(self) -> None:
        ba, brain = _make_brain_access()
        brain.learn_concept = AsyncMock(return_value="c-new")

        result = await ba.learn(
            "test",
            "content",
            importance=0.8,
            confidence=0.9,
            emotional_valence=0.5,
        )
        assert result == "c-new"
        call_kwargs = brain.learn_concept.call_args.kwargs
        assert call_kwargs["importance"] == 0.8
        assert call_kwargs["confidence"] == 0.9
        assert call_kwargs["emotional_valence"] == 0.5

    @pytest.mark.asyncio
    async def test_learn_audit_logging(self) -> None:
        ba, brain = _make_brain_access()
        brain.learn_concept = AsyncMock(return_value="c-new")

        with patch("sovyx.plugins.context.logger") as mock_logger:
            await ba.learn("test", "content")
            mock_logger.info.assert_called_once()
            log_kwargs = mock_logger.info.call_args
            assert "brain_access_learn" in log_kwargs.args


# ── _concept_to_dict() ──


class TestConceptToDict:
    def test_concept_to_dict_complete(self) -> None:
        concept = _make_concept(
            concept_id="c-042",
            name="test",
            content="hello",
            importance=0.7,
            confidence=0.6,
            access_count=5,
            source="plugin:knowledge",
        )
        d = BrainAccess._concept_to_dict(concept, 0.95)
        assert d["id"] == "c-042"
        assert d["name"] == "test"
        assert d["content"] == "hello"
        assert d["importance"] == 0.7
        assert d["confidence"] == 0.6
        assert d["access_count"] == 5
        assert d["source"] == "plugin:knowledge"
        assert d["score"] == 0.95
        assert d["category"] == "fact"


# ── create_relation() ──


class TestCreateRelation:
    @pytest.mark.asyncio
    async def test_create_relation_success(self) -> None:
        ba, brain = _make_brain_access()
        rel = MagicMock()
        rel.id = "rel-001"
        brain._relations = AsyncMock()
        brain._relations.get_or_create = AsyncMock(return_value=rel)

        result = await ba.create_relation("c-001", "c-002", "related_to")
        assert result == "rel-001"
        brain._relations.get_or_create.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_relation_invalid_type(self) -> None:
        ba, brain = _make_brain_access()
        brain._relations = AsyncMock()

        with pytest.raises(ValueError, match="Invalid relation_type"):
            await ba.create_relation("c-001", "c-002", "nonsense")

    @pytest.mark.asyncio
    async def test_create_relation_all_valid_types(self) -> None:
        ba, brain = _make_brain_access()
        rel = MagicMock()
        rel.id = "rel-x"
        brain._relations = AsyncMock()
        brain._relations.get_or_create = AsyncMock(return_value=rel)

        valid = [
            "related_to",
            "part_of",
            "causes",
            "contradicts",
            "example_of",
            "temporal",
            "emotional",
        ]
        for rt in valid:
            result = await ba.create_relation("c-001", "c-002", rt)
            assert result == "rel-x"

    @pytest.mark.asyncio
    async def test_create_relation_permission_denied(self) -> None:
        ba, _ = _make_brain_access(permissions={"brain:read"})
        with pytest.raises(PermissionDeniedError):
            await ba.create_relation("c-001", "c-002")

    @pytest.mark.asyncio
    async def test_create_relation_write_flag_false(self) -> None:
        ba, _ = _make_brain_access(write=False)
        with pytest.raises(PermissionDeniedError):
            await ba.create_relation("c-001", "c-002")


# ── boost_importance() ──


class TestBoostImportance:
    @pytest.mark.asyncio
    async def test_boost_success(self) -> None:
        ba, brain = _make_brain_access()
        concept = _make_concept(importance=0.5)
        brain.get_concept = AsyncMock(return_value=concept)
        brain._concepts.boost_importance = AsyncMock()

        result = await ba.boost_importance("c-001", delta=0.1)
        assert result is True
        brain._concepts.boost_importance.assert_called_once()

    @pytest.mark.asyncio
    async def test_boost_clamps_delta(self) -> None:
        ba, brain = _make_brain_access()
        concept = _make_concept()
        brain.get_concept = AsyncMock(return_value=concept)
        brain._concepts.boost_importance = AsyncMock()

        # Delta > 0.5 should be clamped
        await ba.boost_importance("c-001", delta=2.0)
        call_args = brain._concepts.boost_importance.call_args
        assert call_args.args[1] == 0.5  # clamped

        # Negative delta clamped to 0
        await ba.boost_importance("c-001", delta=-1.0)
        call_args = brain._concepts.boost_importance.call_args
        assert call_args.args[1] == 0.0

    @pytest.mark.asyncio
    async def test_boost_not_found(self) -> None:
        ba, brain = _make_brain_access()
        brain.get_concept = AsyncMock(return_value=None)

        result = await ba.boost_importance("nonexistent")
        assert result is False

    @pytest.mark.asyncio
    async def test_boost_permission_denied(self) -> None:
        ba, _ = _make_brain_access(permissions={"brain:read"})
        with pytest.raises(PermissionDeniedError):
            await ba.boost_importance("c-001")

    @pytest.mark.asyncio
    async def test_boost_default_delta(self) -> None:
        ba, brain = _make_brain_access()
        concept = _make_concept()
        brain.get_concept = AsyncMock(return_value=concept)
        brain._concepts.boost_importance = AsyncMock()

        await ba.boost_importance("c-001")  # default delta=0.05
        call_args = brain._concepts.boost_importance.call_args
        assert call_args.args[1] == 0.05


# ── get_stats() ──


class TestGetStats:
    @pytest.mark.asyncio
    async def test_get_stats_returns_complete_data(self) -> None:
        ba, brain = _make_brain_access()

        # Mock concept repo
        brain._concepts.get_categories = AsyncMock(return_value=["fact", "preference"])
        brain._concepts.count_by_category = AsyncMock(side_effect=[10, 5])

        # Mock relation count
        mock_conn = AsyncMock()
        mock_cursor = AsyncMock()
        mock_cursor.fetchone = AsyncMock(return_value=(42,))
        mock_conn.execute = AsyncMock(return_value=mock_cursor)
        mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_conn.__aexit__ = AsyncMock(return_value=None)
        brain._relations = MagicMock()
        brain._relations._pool = MagicMock()
        brain._relations._pool.read = MagicMock(return_value=mock_conn)

        # Mock episode count
        mock_conn2 = AsyncMock()
        mock_cursor2 = AsyncMock()
        mock_cursor2.fetchone = AsyncMock(return_value=(7,))
        mock_conn2.execute = AsyncMock(return_value=mock_cursor2)
        mock_conn2.__aenter__ = AsyncMock(return_value=mock_conn2)
        mock_conn2.__aexit__ = AsyncMock(return_value=None)
        brain._episodes = MagicMock()
        brain._episodes._pool = MagicMock()
        brain._episodes._pool.read = MagicMock(return_value=mock_conn2)

        stats = await ba.get_stats()

        assert stats["total_concepts"] == 15
        assert stats["categories"] == {"fact": 10, "preference": 5}
        assert stats["total_relations"] == 42
        assert stats["total_episodes"] == 7
        assert stats["mind_id"] == "test-mind"

    @pytest.mark.asyncio
    async def test_get_stats_empty_brain(self) -> None:
        ba, brain = _make_brain_access()

        brain._concepts.get_categories = AsyncMock(return_value=[])

        # Relations fail gracefully
        brain._relations = MagicMock()
        brain._relations._pool = MagicMock()
        brain._relations._pool.read = MagicMock(side_effect=Exception("no table"))

        # Episodes fail gracefully
        brain._episodes = MagicMock()
        brain._episodes._pool = MagicMock()
        brain._episodes._pool.read = MagicMock(side_effect=Exception("no table"))

        stats = await ba.get_stats()

        assert stats["total_concepts"] == 0
        assert stats["categories"] == {}
        assert stats["total_relations"] == 0
        assert stats["total_episodes"] == 0

    @pytest.mark.asyncio
    async def test_get_stats_permission_denied(self) -> None:
        ba, _ = _make_brain_access(permissions=set())
        with pytest.raises(PermissionDeniedError):
            await ba.get_stats()

    @pytest.mark.asyncio
    async def test_get_stats_only_needs_read(self) -> None:
        """get_stats only requires brain:read, not brain:write."""
        ba, brain = _make_brain_access(permissions={"brain:read"}, write=False)

        brain._concepts.get_categories = AsyncMock(return_value=[])
        brain._relations = MagicMock()
        brain._relations._pool = MagicMock()
        brain._relations._pool.read = MagicMock(side_effect=Exception("x"))
        brain._episodes = MagicMock()
        brain._episodes._pool = MagicMock()
        brain._episodes._pool.read = MagicMock(side_effect=Exception("x"))

        stats = await ba.get_stats()
        assert stats["total_concepts"] == 0  # works without brain:write
