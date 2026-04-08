"""Dashboard brain graph — read-only queries for concept graph visualization.

Returns nodes (concepts) and links (relations) in a format compatible
with react-force-graph-2d: {nodes: [{id, name, ...}], links: [{source, target, ...}]}.

Also provides semantic search via ``search_brain()`` which uses hybrid
FTS5+vector retrieval with deduplication and score normalisation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from sovyx.brain.models import Concept
    from sovyx.engine.registry import ServiceRegistry

logger = get_logger(__name__)


# ── Graph API ──────────────���───────────────────────────────────────────


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

    Connectivity guarantee: every node has ≥1 edge in the response.
    Small graphs (<500 nodes) get a generous cap; large graphs are
    capped at ``limit × 3`` but orphan audit ensures no islands.
    """
    nodes = await _get_concepts(registry, limit=limit)
    node_ids = {n["id"] for n in nodes}

    # Dynamic cap: generous for small graphs, bounded for large
    max_links = len(node_ids) * 30 if len(node_ids) < 500 else limit * 3  # noqa: PLR2004

    links = await _get_relations(registry, node_ids, max_links=max_links)

    # Orphan audit: find nodes with zero edges, fetch their top relations
    linked_ids = set()
    for link in links:
        linked_ids.add(link["source"])
        linked_ids.add(link["target"])
    orphans = node_ids - linked_ids

    if orphans:
        rescue_links = await _rescue_orphans(registry, orphans, node_ids)
        links.extend(rescue_links)
        logger.debug(
            "orphan_audit",
            orphans_found=len(orphans),
            edges_rescued=len(rescue_links),
        )

    return {"nodes": nodes, "links": links}


# ── Search API ──────────��──────────────────────────────────────────────


def _concept_to_search_result(
    concept: Concept,
    score: float,
    match_type: str,
) -> dict[str, Any]:
    """Convert a Concept + score into a JSON-serialisable search result.

    Args:
        concept: A Concept model instance.
        score: Normalised relevance score (0.0–1.0).
        match_type: Origin of the match — ``"text"`` or ``"vector"``.

    Returns:
        Dict with id, name, category, importance, confidence,
        access_count, score (4dp), and match_type.
    """
    return {
        "id": str(concept.id),
        "name": concept.name,
        "category": concept.category.value,
        "importance": round(concept.importance, 3),
        "confidence": round(concept.confidence, 3),
        "access_count": concept.access_count,
        "score": round(score, 4),
        "match_type": match_type,
    }


async def _get_query_embedding(
    registry: ServiceRegistry,
    query: str,
) -> list[float] | None:
    """Attempt to encode *query* into an embedding vector.

    Returns ``None`` when the embedding service is unavailable or fails.
    """
    try:
        from sovyx.brain.embedding import EmbeddingEngine

        if not registry.is_registered(EmbeddingEngine):
            return None

        engine = await registry.resolve(EmbeddingEngine)
        if not engine.has_embeddings:
            return None

        emb: list[float] = await engine.encode(query, is_query=True)
        return emb
    except Exception:  # noqa: BLE001
        logger.debug("query_embedding_failed", exc_info=True)
        return None


async def search_brain(
    registry: ServiceRegistry,
    query: str,
    *,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Hybrid semantic search over brain concepts.

    Pipeline:
    1. FTS5 full-text search (always attempted).
    2. Vector similarity search (if embedding service available).
    3. Deduplication by concept ID (keep first occurrence).
    4. Sort by score descending, cap at *limit*.

    Score normalisation:
    - FTS5 rank (negative float) → ``1 / (1 + abs(rank))``
    - Vector distance (0=identical, 1=opposite) → ``1.0 - distance``

    Returns:
        List of result dicts (see ``_concept_to_search_result``).
        Empty list on any error (graceful degradation).
    """
    if not query or not query.strip():
        return []

    try:
        from sovyx.brain.concept_repo import ConceptRepository
        from sovyx.engine.types import MindId

        if not registry.is_registered(ConceptRepository):
            return []

        repo = await registry.resolve(ConceptRepository)
        mind_id = MindId(await _get_active_mind_id(registry))

        # ── FTS5 search ──
        fts_results: list[dict[str, Any]] = []
        try:
            raw_fts = await repo.search_by_text(
                query,
                mind_id,
                limit=limit * 2,
            )
            for concept, rank in raw_fts:
                score = 1.0 / (1.0 + abs(float(rank)))
                fts_results.append(
                    _concept_to_search_result(concept, score, "text"),
                )
        except Exception:  # noqa: BLE001
            logger.debug("brain_fts_search_failed", exc_info=True)

        # ── Vector search ──
        vec_results: list[dict[str, Any]] = []
        embedding = await _get_query_embedding(registry, query)
        if embedding is not None:
            try:
                raw_vec = await repo.search_by_embedding(
                    embedding,
                    mind_id,
                    limit=limit * 2,
                )
                for concept, distance in raw_vec:
                    score = max(0.0, 1.0 - float(distance))
                    vec_results.append(
                        _concept_to_search_result(
                            concept,
                            score,
                            "vector",
                        ),
                    )
            except Exception:  # noqa: BLE001
                logger.debug("brain_vec_search_failed", exc_info=True)

        # ── Merge + deduplicate ──
        seen: set[str] = set()
        merged: list[dict[str, Any]] = []
        for result in fts_results + vec_results:
            if result["id"] not in seen:
                seen.add(result["id"])
                merged.append(result)

        # Sort by score descending
        merged.sort(key=lambda r: r["score"], reverse=True)

        return merged[:limit]

    except Exception:  # noqa: BLE001
        logger.warning(
            "brain_search_failed",
            query=query,
            exc_info=True,
        )
        return []


# ── Graph internals ────────────────────────────────────────────────────


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
    max_links: int = 600,
) -> list[dict[str, Any]]:
    """Get relations between the given concept IDs.

    Uses a single batch SQL query instead of N+1 per-concept queries.
    """
    if not node_ids:
        return []

    try:
        from sovyx.persistence.manager import DatabaseManager

        if not registry.is_registered(DatabaseManager):
            return await _get_relations_via_repo(registry, node_ids)

        db = await registry.resolve(DatabaseManager)
        mind_id_str = await _get_active_mind_id(registry)

        from sovyx.engine.types import MindId

        pool = db.get_brain_pool(MindId(mind_id_str))

        ids_list = list(node_ids)
        rows: list[Any] = []
        chunk_size = 450  # halved: each ID appears in 2 placeholders

        async with pool.read() as conn:
            for i in range(0, len(ids_list), chunk_size):
                chunk = ids_list[i : i + chunk_size]
                placeholders = ",".join("?" for _ in chunk)
                # Bidirectional query (defense-in-depth)
                cursor = await conn.execute(
                    f"SELECT source_id, target_id, relation_type, weight "  # noqa: S608  # nosec B608
                    f"FROM relations "
                    f"WHERE source_id IN ({placeholders}) "
                    f"OR target_id IN ({placeholders}) ",
                    chunk + chunk,
                )
                rows.extend(await cursor.fetchall())

        # Global sort by weight DESC — ensures strongest edges survive cap
        rows.sort(key=lambda r: float(r[3]), reverse=True)

        seen: set[str] = set()
        links: list[dict[str, Any]] = []

        for row in rows:
            if len(links) >= max_links:
                break
            src, tgt = str(row[0]), str(row[1])
            # Both ends must be in the visible node set
            if src not in node_ids or tgt not in node_ids:
                continue
            edge_key = f"{min(src, tgt)}:{max(src, tgt)}"
            if edge_key not in seen:
                seen.add(edge_key)
                links.append(
                    {
                        "source": src,
                        "target": tgt,
                        "relation_type": str(row[2]),
                        "weight": round(float(row[3]), 3),
                    }
                )

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
                    all_links.append(
                        {
                            "source": src,
                            "target": tgt,
                            "relation_type": r.relation_type.value,
                            "weight": round(r.weight, 3),
                        }
                    )

    return all_links


async def _rescue_orphans(
    registry: ServiceRegistry,
    orphan_ids: set[str],
    node_ids: set[str],
) -> list[dict[str, Any]]:
    """Fetch top-3 relations by weight for each orphan node.

    Guarantees every node has ≥1 edge in the graph response.
    Only includes edges where both endpoints are in ``node_ids``.
    """
    if not orphan_ids:
        return []

    try:
        from sovyx.brain.relation_repo import RelationRepository
        from sovyx.engine.types import ConceptId

        if not registry.is_registered(RelationRepository):
            return []

        repo = await registry.resolve(RelationRepository)
        rescued: list[dict[str, Any]] = []
        seen: set[str] = set()

        for orphan in orphan_ids:
            relations = await repo.get_relations_for(ConceptId(orphan))
            # Sort by weight DESC, take top 3 that connect to visible nodes
            relations.sort(key=lambda r: r.weight, reverse=True)
            added = 0
            for rel in relations:
                if added >= 3:  # noqa: PLR2004
                    break
                src, tgt = str(rel.source_id), str(rel.target_id)
                other = tgt if src == orphan else src
                if other not in node_ids:
                    continue
                edge_key = f"{min(src, tgt)}:{max(src, tgt)}"
                if edge_key in seen:
                    continue
                seen.add(edge_key)
                rescued.append(
                    {
                        "source": src,
                        "target": tgt,
                        "relation_type": rel.relation_type.value,
                        "weight": round(rel.weight, 3),
                    }
                )
                added += 1

        return rescued
    except Exception:  # noqa: BLE001
        logger.debug("orphan_rescue_failed", exc_info=True)
        return []


async def _get_active_mind_id(registry: ServiceRegistry) -> str:
    """Get active mind ID — delegates to shared utility."""
    from sovyx.dashboard._shared import get_active_mind_id

    return await get_active_mind_id(registry)
