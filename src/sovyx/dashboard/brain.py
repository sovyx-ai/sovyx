"""Dashboard brain graph — read-only queries for concept graph visualization.

Returns nodes (concepts) and links (relations) in a format compatible
with react-force-graph-2d: {nodes: [{id, name, ...}], links: [{source, target, ...}]}.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from sovyx.engine.registry import ServiceRegistry

logger = get_logger(__name__)


async def get_brain_graph(
    registry: ServiceRegistry,
    *,
    limit: int = 200,
) -> dict[str, list[dict[str, Any]]]:
    """Build a graph of concepts and relations for visualization.

    Returns:
        {"nodes": [...], "links": [...]} where:
        - nodes: {id, name, category, importance, confidence, access_count}
        - links: {source, target, relation_type, weight}
    """
    nodes = await _get_concepts(registry, limit=limit)
    node_ids = {n["id"] for n in nodes}

    links = await _get_relations(registry, node_ids)

    return {"nodes": nodes, "links": links}


async def _get_concepts(
    registry: ServiceRegistry,
    *,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Get top concepts ordered by importance + access_count."""
    try:
        from sovyx.brain.concept_repo import ConceptRepository
        from sovyx.engine.types import MindId

        if not registry.is_registered(ConceptRepository):
            return []

        repo = await registry.resolve(ConceptRepository)
        mind_id = MindId(await _get_active_mind_id(registry))
        concepts = await repo.get_by_mind(mind_id, limit=limit, offset=0)

        return [
            {
                "id": str(c.id),
                "name": c.name,
                "category": c.category.value,
                "importance": round(c.importance, 3),
                "confidence": round(c.confidence, 3),
                "access_count": c.access_count,
            }
            for c in concepts
        ]
    except Exception:  # noqa: BLE001
        logger.debug("brain_graph_concepts_failed")
        return []


async def _get_relations(
    registry: ServiceRegistry,
    node_ids: set[str],
) -> list[dict[str, Any]]:
    """Get relations between the given concept IDs.

    Uses a single batch SQL query instead of N+1 per-concept queries.
    """
    if not node_ids:
        return []

    try:
        from sovyx.persistence.manager import DatabaseManager

        if not registry.is_registered(DatabaseManager):
            # Fallback: try via RelationRepository (slower, N+1)
            return await _get_relations_via_repo(registry, node_ids)

        db = await registry.resolve(DatabaseManager)
        mind_id_str = await _get_active_mind_id(registry)

        from sovyx.engine.types import MindId

        pool = db.get_brain_pool(MindId(mind_id_str))

        # Single query: all relations where BOTH endpoints are in node_ids
        placeholders = ",".join("?" for _ in node_ids)
        ids_list = list(node_ids)

        async with pool.read() as conn:
            cursor = await conn.execute(
                f"SELECT source_id, target_id, relation_type, weight "  # noqa: S608  # nosec B608
                f"FROM relations "
                f"WHERE source_id IN ({placeholders}) "
                f"AND target_id IN ({placeholders})",
                ids_list + ids_list,
            )
            rows = await cursor.fetchall()

        seen: set[str] = set()
        links: list[dict[str, Any]] = []

        for row in rows:
            src, tgt = str(row[0]), str(row[1])
            edge_key = f"{min(src, tgt)}:{max(src, tgt)}"
            if edge_key not in seen:
                seen.add(edge_key)
                links.append({
                    "source": src,
                    "target": tgt,
                    "relation_type": str(row[2]),
                    "weight": round(float(row[3]), 3),
                })

        return links
    except Exception:  # noqa: BLE001
        logger.debug("brain_graph_relations_failed")
        return []


async def _get_relations_via_repo(
    registry: ServiceRegistry,
    node_ids: set[str],
) -> list[dict[str, Any]]:
    """Fallback: get relations via RelationRepository (N+1, slower)."""
    from sovyx.brain.relation_repo import RelationRepository

    if not registry.is_registered(RelationRepository):
        return []

    repo = await registry.resolve(RelationRepository)
    from sovyx.engine.types import ConceptId

    all_links: list[dict[str, Any]] = []
    seen: set[str] = set()

    for nid in node_ids:
        relations = await repo.get_relations_for(ConceptId(nid))
        for r in relations:
            src = str(r.source_id)
            tgt = str(r.target_id)
            if src in node_ids and tgt in node_ids:
                edge_key = f"{min(src, tgt)}:{max(src, tgt)}"
                if edge_key not in seen:
                    seen.add(edge_key)
                    all_links.append({
                        "source": src,
                        "target": tgt,
                        "relation_type": r.relation_type.value,
                        "weight": round(r.weight, 3),
                    })

    return all_links


async def _get_active_mind_id(registry: ServiceRegistry) -> str:
    """Get active mind ID."""
    try:
        from sovyx.engine.bootstrap import MindManager

        if registry.is_registered(MindManager):
            manager = await registry.resolve(MindManager)
            minds = manager.get_active_minds()
            if minds:
                return minds[0]
    except Exception:  # noqa: BLE001
        logger.debug("_get_active_mind_id_failed")
    return "default"
