"""Tests for sovyx.dashboard.brain — brain graph queries."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

import pytest

from sovyx.dashboard.brain import (
    _get_relations,
    _get_relations_via_repo,
    get_brain_graph,
)


def _mock_concept(
    cid: str,
    name: str,
    category: str = "fact",
    importance: float = 0.5,
) -> MagicMock:
    c = MagicMock()
    c.id = cid
    c.name = name
    c.category = MagicMock(value=category)
    c.importance = importance
    c.confidence = 0.7
    c.access_count = 3
    return c


def _mock_relation(src: str, tgt: str, weight: float = 0.5) -> MagicMock:
    r = MagicMock()
    r.source_id = src
    r.target_id = tgt
    r.relation_type = MagicMock(value="related_to")
    r.weight = weight
    return r


def _make_registry_with_db(
    concepts: list[MagicMock],
    rows: list[tuple[str, str, str, float]],
    mind_id: str = "mind-1",
) -> MagicMock:
    """Create a registry with DatabaseManager + ConceptRepository for DB path."""
    concept_repo = AsyncMock()
    concept_repo.get_by_mind = AsyncMock(return_value=concepts)

    # Mock the DB pool + connection for _get_relations batch SQL path
    mock_cursor = AsyncMock()
    mock_cursor.fetchall = AsyncMock(return_value=rows)

    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock(return_value=mock_cursor)

    mock_pool = MagicMock()

    @asynccontextmanager
    async def fake_read() -> AsyncGenerator[AsyncMock, None]:
        yield mock_conn

    mock_pool.read = fake_read

    mock_db = MagicMock()
    mock_db.get_brain_pool = MagicMock(return_value=mock_pool)

    registry = MagicMock()

    def is_registered(cls: type) -> bool:
        from sovyx.brain.concept_repo import ConceptRepository
        from sovyx.persistence.manager import DatabaseManager

        return cls in (ConceptRepository, DatabaseManager)

    registry.is_registered.side_effect = is_registered

    async def resolve(cls: type) -> object:
        from sovyx.brain.concept_repo import ConceptRepository
        from sovyx.persistence.manager import DatabaseManager

        if cls is ConceptRepository:
            return concept_repo
        if cls is DatabaseManager:
            return mock_db
        msg = f"Unknown: {cls}"
        raise ValueError(msg)

    registry.resolve = AsyncMock(side_effect=resolve)
    return registry


class TestGetBrainGraph:
    @pytest.mark.asyncio()
    async def test_returns_nodes_and_links(self) -> None:
        concepts = [
            _mock_concept("c1", "Python"),
            _mock_concept("c2", "FastAPI"),
            _mock_concept("c3", "SQLite"),
        ]
        relations_c1 = [_mock_relation("c1", "c2", 0.8)]
        relations_c2 = [_mock_relation("c2", "c3", 0.6)]
        relations_c3: list[MagicMock] = []

        concept_repo = AsyncMock()
        concept_repo.get_by_mind = AsyncMock(return_value=concepts)

        relation_repo = AsyncMock()

        async def get_relations_for(cid: str) -> list[MagicMock]:
            return {"c1": relations_c1, "c2": relations_c2, "c3": relations_c3}.get(str(cid), [])

        relation_repo.get_relations_for = AsyncMock(side_effect=get_relations_for)

        registry = MagicMock()

        def is_registered(cls: type) -> bool:
            from sovyx.brain.concept_repo import ConceptRepository
            from sovyx.brain.relation_repo import RelationRepository

            return cls in (ConceptRepository, RelationRepository)

        registry.is_registered.side_effect = is_registered

        async def resolve(cls: type) -> AsyncMock:
            from sovyx.brain.concept_repo import ConceptRepository
            from sovyx.brain.relation_repo import RelationRepository

            if cls is ConceptRepository:
                return concept_repo
            if cls is RelationRepository:
                return relation_repo
            msg = f"Unknown: {cls}"
            raise ValueError(msg)

        registry.resolve = AsyncMock(side_effect=resolve)

        with patch(
            "sovyx.dashboard.brain._get_active_mind_id",
            new_callable=AsyncMock,
            return_value="mind-1",
        ):
            result = await get_brain_graph(registry, limit=100)

        assert len(result["nodes"]) == 3
        assert result["nodes"][0]["name"] == "Python"
        assert result["nodes"][0]["category"] == "fact"

        assert len(result["links"]) == 2
        sources = {link["source"] for link in result["links"]}
        targets = {link["target"] for link in result["links"]}
        assert "c1" in sources
        assert "c2" in sources or "c2" in targets

    @pytest.mark.asyncio()
    async def test_empty_when_no_repos(self) -> None:
        registry = MagicMock()
        registry.is_registered.return_value = False

        result = await get_brain_graph(registry)

        assert result["nodes"] == []
        assert result["links"] == []

    @pytest.mark.asyncio()
    async def test_survives_repo_error(self) -> None:
        registry = MagicMock()
        registry.is_registered.return_value = True
        registry.resolve = AsyncMock(side_effect=RuntimeError("boom"))

        result = await get_brain_graph(registry)
        assert result["nodes"] == []
        assert result["links"] == []

    @pytest.mark.asyncio()
    async def test_deduplicates_edges(self) -> None:
        """Same edge from both directions should appear only once."""
        concepts = [
            _mock_concept("c1", "A"),
            _mock_concept("c2", "B"),
        ]
        rel = _mock_relation("c1", "c2", 0.8)

        concept_repo = AsyncMock()
        concept_repo.get_by_mind = AsyncMock(return_value=concepts)

        relation_repo = AsyncMock()
        relation_repo.get_relations_for = AsyncMock(return_value=[rel])

        registry = MagicMock()

        def is_registered(cls: type) -> bool:
            from sovyx.brain.concept_repo import ConceptRepository
            from sovyx.brain.relation_repo import RelationRepository

            return cls in (ConceptRepository, RelationRepository)

        registry.is_registered.side_effect = is_registered

        async def resolve(cls: type) -> AsyncMock:
            from sovyx.brain.concept_repo import ConceptRepository

            if cls is ConceptRepository:
                return concept_repo
            return relation_repo

        registry.resolve = AsyncMock(side_effect=resolve)

        with patch(
            "sovyx.dashboard.brain._get_active_mind_id",
            new_callable=AsyncMock,
            return_value="mind-1",
        ):
            result = await get_brain_graph(registry, limit=100)

        assert len(result["links"]) == 1
        assert result["links"][0]["weight"] == 0.8


class TestGetRelationsViaDatabaseManager:
    """Tests for the batch SQL path in _get_relations (lines 93-144)."""

    @pytest.mark.asyncio()
    async def test_db_path_returns_links(self) -> None:
        """When DatabaseManager is registered, use batch SQL path."""
        concepts = [
            _mock_concept("c1", "A"),
            _mock_concept("c2", "B"),
            _mock_concept("c3", "C"),
        ]
        # Rows from SQL: (source_id, target_id, relation_type, weight)
        rows = [
            ("c1", "c2", "related_to", 0.8),
            ("c2", "c3", "causes", 0.6),
        ]
        registry = _make_registry_with_db(concepts, rows)

        with patch(
            "sovyx.dashboard.brain._get_active_mind_id",
            new_callable=AsyncMock,
            return_value="mind-1",
        ):
            result = await get_brain_graph(registry, limit=100)

        assert len(result["nodes"]) == 3
        assert len(result["links"]) == 2
        assert result["links"][0]["source"] == "c1"
        assert result["links"][0]["target"] == "c2"
        assert result["links"][0]["relation_type"] == "related_to"
        assert result["links"][0]["weight"] == 0.8

    @pytest.mark.asyncio()
    async def test_db_path_filters_out_of_scope_targets(self) -> None:
        """Edges whose target is NOT in node_ids should be filtered out."""
        concepts = [_mock_concept("c1", "A")]
        rows = [("c1", "c999", "related_to", 0.5)]  # c999 not in node set
        registry = _make_registry_with_db(concepts, rows)

        with patch(
            "sovyx.dashboard.brain._get_active_mind_id",
            new_callable=AsyncMock,
            return_value="mind-1",
        ):
            result = await get_brain_graph(registry, limit=100)

        assert result["links"] == []

    @pytest.mark.asyncio()
    async def test_db_path_deduplicates_edges(self) -> None:
        """Same edge (c1→c2 and c2→c1) should appear only once."""
        concepts = [_mock_concept("c1", "A"), _mock_concept("c2", "B")]
        rows = [
            ("c1", "c2", "related_to", 0.8),
            ("c2", "c1", "related_to", 0.9),  # same edge, reversed
        ]
        registry = _make_registry_with_db(concepts, rows)

        with patch(
            "sovyx.dashboard.brain._get_active_mind_id",
            new_callable=AsyncMock,
            return_value="mind-1",
        ):
            result = await get_brain_graph(registry, limit=100)

        # Should only have 1 link (deduplicated by edge_key)
        assert len(result["links"]) == 1

    @pytest.mark.asyncio()
    async def test_db_path_respects_max_links(self) -> None:
        """When max_links reached, stop adding."""
        concepts = [_mock_concept(f"c{i}", f"N{i}") for i in range(5)]
        # Many rows — but limit=1 means max_links=3
        rows = [
            ("c0", "c1", "r", 0.5),
            ("c0", "c2", "r", 0.5),
            ("c0", "c3", "r", 0.5),
            ("c0", "c4", "r", 0.5),
        ]
        registry = _make_registry_with_db(concepts, rows)

        with patch(
            "sovyx.dashboard.brain._get_active_mind_id",
            new_callable=AsyncMock,
            return_value="mind-1",
        ):
            # limit=1 → max_links = 1*3 = 3
            graph = await get_brain_graph(registry, limit=1)

        # limit=1 → only 1 concept node returned, so most links filtered
        assert isinstance(graph["links"], list)

    @pytest.mark.asyncio()
    async def test_get_relations_max_links_cap(self) -> None:
        """Direct test: _get_relations respects max_links."""
        rows = [
            ("c0", "c1", "r", 0.5),
            ("c0", "c2", "r", 0.6),
            ("c0", "c3", "r", 0.7),
            ("c1", "c2", "r", 0.8),
        ]

        mock_cursor = AsyncMock()
        mock_cursor.fetchall = AsyncMock(return_value=rows)
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock(return_value=mock_cursor)
        mock_pool = MagicMock()

        @asynccontextmanager
        async def fake_read() -> AsyncGenerator[AsyncMock, None]:
            yield mock_conn

        mock_pool.read = fake_read
        mock_db = MagicMock()
        mock_db.get_brain_pool = MagicMock(return_value=mock_pool)

        registry = MagicMock()

        def is_registered(cls: type) -> bool:
            from sovyx.persistence.manager import DatabaseManager

            return cls is DatabaseManager

        registry.is_registered.side_effect = is_registered
        registry.resolve = AsyncMock(return_value=mock_db)

        node_ids = {"c0", "c1", "c2", "c3"}

        with patch(
            "sovyx.dashboard.brain._get_active_mind_id",
            new_callable=AsyncMock,
            return_value="mind-1",
        ):
            # max_links=2 should cap at 2
            result = await _get_relations(registry, node_ids, max_links=2)

        assert len(result) == 2

    @pytest.mark.asyncio()
    async def test_get_relations_empty_node_ids(self) -> None:
        """Empty node_ids → empty links."""
        registry = MagicMock()
        result = await _get_relations(registry, set(), max_links=100)
        assert result == []

    @pytest.mark.asyncio()
    async def test_db_path_survives_error(self) -> None:
        """If DB query fails, returns empty list."""
        concepts = [_mock_concept("c1", "A")]

        concept_repo = AsyncMock()
        concept_repo.get_by_mind = AsyncMock(return_value=concepts)

        registry = MagicMock()

        def is_registered(cls: type) -> bool:
            from sovyx.brain.concept_repo import ConceptRepository
            from sovyx.persistence.manager import DatabaseManager

            return cls in (ConceptRepository, DatabaseManager)

        registry.is_registered.side_effect = is_registered

        async def resolve(cls: type) -> object:
            from sovyx.brain.concept_repo import ConceptRepository

            if cls is ConceptRepository:
                return concept_repo
            raise RuntimeError("DB connection failed")

        registry.resolve = AsyncMock(side_effect=resolve)

        with patch(
            "sovyx.dashboard.brain._get_active_mind_id",
            new_callable=AsyncMock,
            return_value="mind-1",
        ):
            result = await get_brain_graph(registry, limit=100)

        node = result["nodes"][0]
        assert node["id"] == "c1"
        assert node["name"] == "A"
        assert node["category"] == "fact"
        assert node["importance"] == 0.5
        assert node["confidence"] == 0.7
        assert node["access_count"] == 3
        assert "emotional_valence" in node
        assert result["links"] == []

    @pytest.mark.asyncio()
    async def test_db_path_no_database_manager_falls_through(self) -> None:
        """When DatabaseManager not registered, falls back to RelationRepo."""
        concepts = [_mock_concept("c1", "A"), _mock_concept("c2", "B")]

        concept_repo = AsyncMock()
        concept_repo.get_by_mind = AsyncMock(return_value=concepts)

        # No RelationRepository either — should return empty links
        registry = MagicMock()

        def is_registered(cls: type) -> bool:
            from sovyx.brain.concept_repo import ConceptRepository

            return cls is ConceptRepository

        registry.is_registered.side_effect = is_registered
        registry.resolve = AsyncMock(return_value=concept_repo)

        with patch(
            "sovyx.dashboard.brain._get_active_mind_id",
            new_callable=AsyncMock,
            return_value="mind-1",
        ):
            result = await get_brain_graph(registry, limit=100)

        assert len(result["nodes"]) == 2
        assert result["links"] == []


class TestGetRelationsViaRepo:
    """Tests for the _get_relations_via_repo fallback path."""

    @pytest.mark.asyncio()
    async def test_no_relation_repo_returns_empty(self) -> None:
        """When RelationRepository not registered, return empty."""
        registry = MagicMock()
        registry.is_registered.return_value = False

        result = await _get_relations_via_repo(registry, {"c1", "c2"})
        assert result == []

    @pytest.mark.asyncio()
    async def test_with_relations(self) -> None:
        """With RelationRepository, returns filtered relations."""
        rel1 = _mock_relation("c1", "c2", 0.8)
        rel2 = _mock_relation("c1", "c99", 0.5)  # c99 not in node set

        repo = AsyncMock()
        repo.get_relations_for = AsyncMock(return_value=[rel1, rel2])

        registry = MagicMock()

        def is_registered(cls: type) -> bool:
            from sovyx.brain.relation_repo import RelationRepository

            return cls is RelationRepository

        registry.is_registered.side_effect = is_registered
        registry.resolve = AsyncMock(return_value=repo)

        result = await _get_relations_via_repo(registry, {"c1", "c2"})
        assert len(result) == 1
        assert result[0]["source"] == "c1"
        assert result[0]["target"] == "c2"

    @pytest.mark.asyncio()
    async def test_dedup_in_repo_path(self) -> None:
        """Same edge from different node traversals → only 1."""
        rel = _mock_relation("c1", "c2", 0.8)
        repo = AsyncMock()
        repo.get_relations_for = AsyncMock(return_value=[rel])

        registry = MagicMock()

        def is_registered(cls: type) -> bool:
            from sovyx.brain.relation_repo import RelationRepository

            return cls is RelationRepository

        registry.is_registered.side_effect = is_registered
        registry.resolve = AsyncMock(return_value=repo)

        result = await _get_relations_via_repo(registry, {"c1", "c2"})
        assert len(result) == 1


class TestOrphanAudit:
    """Orphan rescue: every node gets ≥1 edge."""

    @pytest.mark.asyncio()
    async def test_orphan_gets_rescued(self) -> None:
        """Node with zero edges in main query gets rescued via repo."""
        concepts = [
            _mock_concept("c1", "A"),
            _mock_concept("c2", "B"),
            _mock_concept("c3", "Orphan"),
        ]
        # Only c1-c2 edge — c3 is orphaned
        rows = [("c1", "c2", "related_to", 0.8)]

        # Set up DB path for main relations
        mock_cursor = AsyncMock()
        mock_cursor.fetchall = AsyncMock(return_value=rows)
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock(return_value=mock_cursor)
        mock_pool = MagicMock()

        @asynccontextmanager
        async def fake_read() -> AsyncGenerator[AsyncMock, None]:
            yield mock_conn

        mock_pool.read = fake_read
        mock_db = MagicMock()
        mock_db.get_brain_pool = MagicMock(return_value=mock_pool)

        # Set up RelationRepo for orphan rescue
        relation_repo = AsyncMock()

        async def get_relations_for(cid: object) -> list[MagicMock]:
            if str(cid) == "c3":
                return [_mock_relation("c1", "c3", 0.3)]
            return []

        relation_repo.get_relations_for = AsyncMock(side_effect=get_relations_for)

        concept_repo = AsyncMock()
        concept_repo.get_by_mind = AsyncMock(return_value=concepts)

        registry = MagicMock()

        def is_registered(cls: type) -> bool:
            from sovyx.brain.concept_repo import ConceptRepository
            from sovyx.brain.relation_repo import RelationRepository
            from sovyx.persistence.manager import DatabaseManager

            return cls in (ConceptRepository, DatabaseManager, RelationRepository)

        registry.is_registered.side_effect = is_registered

        async def resolve(cls: type) -> object:
            from sovyx.brain.concept_repo import ConceptRepository
            from sovyx.brain.relation_repo import RelationRepository
            from sovyx.persistence.manager import DatabaseManager

            if cls is ConceptRepository:
                return concept_repo
            if cls is DatabaseManager:
                return mock_db
            if cls is RelationRepository:
                return relation_repo
            msg = f"Unknown: {cls}"
            raise ValueError(msg)

        registry.resolve = AsyncMock(side_effect=resolve)

        with patch(
            "sovyx.dashboard.brain._get_active_mind_id",
            new_callable=AsyncMock,
            return_value="mind-1",
        ):
            result = await get_brain_graph(registry, limit=100)

        # All 3 nodes should have edges
        linked = set()
        for link in result["links"]:
            linked.add(link["source"])
            linked.add(link["target"])
        assert "c3" in linked, "Orphan c3 should be rescued"
        assert len(result["links"]) == 2  # noqa: PLR2004  # c1-c2 + c3-c1

    @pytest.mark.asyncio()
    async def test_no_orphans_no_rescue(self) -> None:
        """When all nodes are connected, rescue is not needed."""
        concepts = [_mock_concept("c1", "A"), _mock_concept("c2", "B")]
        rows = [("c1", "c2", "related_to", 0.8)]
        registry = _make_registry_with_db(concepts, rows)

        with patch(
            "sovyx.dashboard.brain._get_active_mind_id",
            new_callable=AsyncMock,
            return_value="mind-1",
        ):
            result = await get_brain_graph(registry, limit=100)

        assert len(result["links"]) == 1
        linked = set()
        for link in result["links"]:
            linked.add(link["source"])
            linked.add(link["target"])
        assert linked == {"c1", "c2"}


class TestDynamicCap:
    """Dynamic max_links: generous for small, bounded for large."""

    @pytest.mark.asyncio()
    async def test_small_graph_generous_cap(self) -> None:
        """<500 nodes → max_links = nodes × 30."""
        concepts = [_mock_concept(f"c{i}", f"N{i}") for i in range(10)]
        # Create many rows — should all be returned (10×30=300 cap)
        rows = [(f"c{i}", f"c{j}", "r", 0.5) for i in range(10) for j in range(i + 1, 10)]
        registry = _make_registry_with_db(concepts, rows)

        with patch(
            "sovyx.dashboard.brain._get_active_mind_id",
            new_callable=AsyncMock,
            return_value="mind-1",
        ):
            result = await get_brain_graph(registry, limit=200)

        # C(10,2) = 45 links, all should be present (cap=300)
        assert len(result["links"]) == 45  # noqa: PLR2004


class TestRescueOrphansEdgeCases:
    """Edge cases for _rescue_orphans."""

    @pytest.mark.asyncio()
    async def test_rescue_empty_orphans(self) -> None:
        """Empty orphan set → empty result."""
        from sovyx.dashboard.brain import _rescue_orphans

        registry = MagicMock()
        result = await _rescue_orphans(registry, set(), {"c1"})
        assert result == []

    @pytest.mark.asyncio()
    async def test_rescue_no_relation_repo(self) -> None:
        """RelationRepository not registered → empty result."""
        from sovyx.dashboard.brain import _rescue_orphans

        registry = MagicMock()
        registry.is_registered.return_value = False
        result = await _rescue_orphans(registry, {"c1"}, {"c1", "c2"})
        assert result == []

    @pytest.mark.asyncio()
    async def test_rescue_error_returns_empty(self) -> None:
        """Exception in rescue → graceful empty return."""
        from sovyx.dashboard.brain import _rescue_orphans

        registry = MagicMock()
        registry.is_registered.return_value = True
        registry.resolve = AsyncMock(side_effect=RuntimeError("boom"))
        result = await _rescue_orphans(registry, {"c1"}, {"c1", "c2"})
        assert result == []


class TestGetActiveMindId:
    """Test _get_active_mind_id delegation to _shared."""

    @pytest.mark.asyncio()
    async def test_delegates_to_shared(self) -> None:
        from sovyx.dashboard.brain import _get_active_mind_id

        registry = MagicMock()
        registry.is_registered.return_value = False

        result = await _get_active_mind_id(registry)
        assert result == "default"

    @pytest.mark.asyncio()
    async def test_returns_active_mind(self) -> None:
        from sovyx.dashboard.brain import _get_active_mind_id

        mock_manager = MagicMock()
        mock_manager.get_active_minds.return_value = ["nyx-mind"]

        registry = MagicMock()

        def is_registered(cls: type) -> bool:
            from sovyx.engine.bootstrap import MindManager

            return cls is MindManager

        registry.is_registered.side_effect = is_registered
        registry.resolve = AsyncMock(return_value=mock_manager)

        result = await _get_active_mind_id(registry)
        assert result == "nyx-mind"
