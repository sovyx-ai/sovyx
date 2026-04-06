"""Tests for sovyx.dashboard.brain — brain graph queries."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sovyx.dashboard.brain import get_brain_graph


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
        # Both c1 and c2 report the same relation
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
            return relation_repo  # RelationRepository

        registry.resolve = AsyncMock(side_effect=resolve)

        with patch(
            "sovyx.dashboard.brain._get_active_mind_id",
            new_callable=AsyncMock,
            return_value="mind-1",
        ):
            result = await get_brain_graph(registry, limit=100)

        # Should deduplicate: c1→c2 seen from both c1 and c2 traversal
        assert len(result["links"]) == 1
        assert result["links"][0]["weight"] == 0.8
