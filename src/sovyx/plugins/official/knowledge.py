"""Sovyx Knowledge Plugin — Brain interface for LLM tool calling.

Enterprise-grade knowledge management via the Plugin SDK.
Deduplication, conflict-aware storage, semantic search, episodic recall.

Permissions required: brain:read, brain:write

Ref: SPE-008 Appendix A.6
TASK-470/471: BrainAccess expansion
TASK-472: Semantic deduplication engine
"""

from __future__ import annotations

import json
import typing
from typing import ClassVar

from sovyx.plugins.sdk import ISovyxPlugin, tool

if typing.TYPE_CHECKING:  # pragma: no cover
    from sovyx.plugins.context import BrainAccess

# ── Defaults ──

_DEFAULT_DEDUP_THRESHOLD = 0.88
_DEFAULT_MAX_RESULTS = 10
_REINFORCEMENT_IMPORTANCE_DELTA = 0.05
_REINFORCEMENT_CONFIDENCE_DELTA = 0.10


class KnowledgePlugin(ISovyxPlugin):
    """Brain knowledge interface for LLM tool calling.

    Features:
    - Semantic deduplication: detects near-duplicate content via embeddings
    - Confidence reinforcement: repeated mentions strengthen, not duplicate
    - Category-aware storage with auto-generated names
    - Configurable similarity threshold per instance

    Config (mind.yaml plugins_config.knowledge.config):
        max_results: int (default 10)
        dedup_threshold: float (default 0.88, range 0.5–0.99)
    """

    config_schema: ClassVar[dict[str, object]] = {
        "properties": {
            "max_results": {"type": "integer", "minimum": 1, "maximum": 50},
            "dedup_threshold": {"type": "number", "minimum": 0.5, "maximum": 0.99},
        },
    }

    def __init__(
        self,
        brain: BrainAccess | None = None,
        *,
        dedup_threshold: float = _DEFAULT_DEDUP_THRESHOLD,
        max_results: int = _DEFAULT_MAX_RESULTS,
    ) -> None:
        self._brain = brain
        self._dedup_threshold = max(0.5, min(0.99, dedup_threshold))
        self._max_results = max(1, min(50, max_results))

    @property
    def name(self) -> str:
        return "knowledge"

    @property
    def version(self) -> str:
        return "2.0.0"

    @property
    def description(self) -> str:
        return "Brain knowledge interface — remember, search, recall with semantic dedup."

    # ── remember (with dedup) ──

    @tool(description="Remember a piece of information for later recall.")
    async def remember(
        self,
        what: str,
        name: str = "",
        category: str = "fact",
    ) -> str:
        """Store information in long-term memory with semantic deduplication.

        Before creating a new concept, checks if semantically similar content
        already exists (cosine similarity >= threshold). If found:
        - Boosts importance and confidence of the existing concept
        - Returns reinforcement info instead of creating a duplicate

        If no near-duplicate exists, creates a new concept via BrainService
        (which also handles name-based dedup with contradiction detection).

        Args:
            what: The information to remember.
            name: Short name/title (auto-generated if empty).
            category: Category (fact, preference, event, person).

        Returns:
            JSON with action (created|reinforced), concept_id, name, details.
        """
        if self._brain is None:
            return json.dumps({"action": "error", "message": "brain access not configured"})

        if not name:
            name = _auto_name(what)

        try:
            # Phase 1: Semantic dedup check
            similar = await self._brain.find_similar(
                what,
                threshold=self._dedup_threshold,
                limit=3,
            )

            if similar:
                # Found near-duplicate — full reinforcement cycle
                best = similar[0]
                existing_id = str(best.get("id", ""))
                existing_name = str(best.get("name", "?"))
                similarity = _float(best.get("similarity", 0))

                # Full reinforcement: importance + confidence + access + metadata
                rr = await self._brain.reinforce(
                    existing_id,
                    importance_delta=_REINFORCEMENT_IMPORTANCE_DELTA,
                    confidence_delta=_REINFORCEMENT_CONFIDENCE_DELTA,
                )

                if rr is not None:
                    imp = rr.get("importance", {})
                    conf = rr.get("confidence", {})
                    old_imp = _float(imp.get("old", 0.5) if isinstance(imp, dict) else 0.5)
                    new_imp = _float(imp.get("new", 0.5) if isinstance(imp, dict) else 0.5)
                    old_conf = _float(conf.get("old", 0.5) if isinstance(conf, dict) else 0.5)
                    new_conf = _float(conf.get("new", 0.5) if isinstance(conf, dict) else 0.5)
                    rc = int(_float(rr.get("reinforcement_count", 1)))
                    established = bool(rr.get("established", False))

                    msg = (
                        f"I already knew something similar: '{existing_name}' "
                        f"(similarity: {similarity:.0%}). Reinforced — "
                        f"confidence {old_conf:.0%} → {new_conf:.0%}, "
                        f"importance {old_imp:.0%} → {new_imp:.0%}."
                    )
                    if established:
                        msg += " This is now an established memory."

                    return json.dumps(
                        {
                            "action": "reinforced",
                            "concept_id": existing_id,
                            "name": existing_name,
                            "similarity": round(similarity, 3),
                            "importance": {"old": round(old_imp, 3), "new": round(new_imp, 3)},
                            "confidence": {"old": round(old_conf, 3), "new": round(new_conf, 3)},
                            "reinforcement_count": rc,
                            "established": established,
                            "message": msg,
                        }
                    )
                # Concept disappeared between find_similar and reinforce — fall through

            # Phase 2: No semantic duplicate — create via BrainService
            # (BrainService handles name-based dedup + contradiction detection)
            concept_id = await self._brain.learn(
                name=name,
                content=what,
                category=category,
            )

            return json.dumps(
                {
                    "action": "created",
                    "concept_id": concept_id,
                    "name": name,
                    "category": category,
                    "message": f"Remembered: '{name}'",
                }
            )

        except Exception as e:  # noqa: BLE001
            return json.dumps({"action": "error", "message": f"Error remembering: {e}"})

    # ── search ──

    @tool(description="Search memory for information matching a query.")
    async def search(self, query: str, limit: int = 5) -> str:
        """Search long-term memory via hybrid retrieval (semantic + keyword).

        Args:
            query: What to search for.
            limit: Max results (1–10).

        Returns:
            JSON with results array (name, content, category, importance,
            confidence, score).
        """
        if self._brain is None:
            return json.dumps({"action": "error", "message": "brain access not configured"})

        limit = max(1, min(self._max_results, limit))

        try:
            results = await self._brain.search(query, limit=limit)
        except Exception as e:  # noqa: BLE001
            return json.dumps({"action": "error", "message": f"Error searching: {e}"})

        if not results:
            return json.dumps(
                {
                    "action": "search",
                    "query": query,
                    "results": [],
                    "message": f"No memories found for: {query}",
                }
            )

        return json.dumps(
            {
                "action": "search",
                "query": query,
                "count": len(results),
                "results": [
                    {
                        "id": str(r.get("id", "")),
                        "name": str(r.get("name", "")),
                        "content": _truncate(str(r.get("content", "")), 300),
                        "category": str(r.get("category", "")),
                        "importance": round(_float(r.get("importance", 0)), 3),
                        "confidence": round(_float(r.get("confidence", 0)), 3),
                        "score": round(_float(r.get("score", 0)), 3),
                    }
                    for r in results
                ],
            }
        )

    # ── forget ──

    @tool(description="Forget a piece of information (remove from memory).")
    async def forget(self, query: str) -> str:
        """Remove matching information from memory.

        Searches for the best match, then deletes it via BrainAccess.forget()
        which cascades (removes relations and embeddings too).

        Args:
            query: What to forget.

        Returns:
            JSON with action (forgotten|not_found), details.
        """
        if self._brain is None:
            return json.dumps({"action": "error", "message": "brain access not configured"})

        try:
            results = await self._brain.search(query, limit=1)

            if not results:
                return json.dumps(
                    {
                        "action": "not_found",
                        "query": query,
                        "message": f"Nothing found matching: {query}",
                    }
                )

            target = results[0]
            concept_id = str(target.get("id", ""))
            concept_name = str(target.get("name", "?"))

            deleted = await self._brain.forget(concept_id)

            if deleted:
                return json.dumps(
                    {
                        "action": "forgotten",
                        "concept_id": concept_id,
                        "name": concept_name,
                        "message": f"Forgotten: '{concept_name}'",
                    }
                )

            return json.dumps(
                {
                    "action": "not_found",
                    "concept_id": concept_id,
                    "message": f"Concept '{concept_name}' not found for deletion.",
                }
            )

        except Exception as e:  # noqa: BLE001
            return json.dumps({"action": "error", "message": f"Error: {e}"})

    # ── recall_about ──

    @tool(
        description=(
            "Recall everything known about a topic. Broader than search — returns more context."
        ),
    )
    async def recall_about(self, topic: str) -> str:
        """Deep recall about a topic — concepts + graph neighbors.

        Args:
            topic: Topic to recall about.

        Returns:
            JSON with concepts, related concepts, and summary.
        """
        if self._brain is None:
            return json.dumps({"action": "error", "message": "brain access not configured"})

        try:
            results = await self._brain.search(topic, limit=self._max_results)

            if not results:
                return json.dumps(
                    {
                        "action": "recall",
                        "topic": topic,
                        "results": [],
                        "message": f"I don't have any memories about: {topic}",
                    }
                )

            # Enrich top results with graph neighbors
            concepts: list[dict[str, object]] = []
            for r in results:
                concept_data = {
                    "id": str(r.get("id", "")),
                    "name": str(r.get("name", "")),
                    "content": _truncate(str(r.get("content", "")), 400),
                    "category": str(r.get("category", "")),
                    "importance": round(_float(r.get("importance", 0)), 3),
                    "confidence": round(_float(r.get("confidence", 0)), 3),
                }

                # Get graph neighbors for top-3 results
                if len(concepts) < 3:
                    cid = str(r.get("id", ""))
                    if cid:
                        try:
                            related = await self._brain.get_related(cid, limit=3)
                            concept_data["related"] = [str(rel.get("name", "")) for rel in related]
                        except Exception:  # noqa: BLE001
                            concept_data["related"] = []

                concepts.append(concept_data)

            return json.dumps(
                {
                    "action": "recall",
                    "topic": topic,
                    "count": len(concepts),
                    "results": concepts,
                }
            )

        except Exception as e:  # noqa: BLE001
            return json.dumps({"action": "error", "message": f"Error recalling: {e}"})

    # ── what_do_you_know ──

    @tool(description="List what you know — summary of stored memories.")
    async def what_do_you_know(self) -> str:
        """Summary of all stored knowledge using brain stats.

        Returns category breakdown, total counts, and top concepts.
        """
        if self._brain is None:
            return json.dumps({"action": "error", "message": "brain access not configured"})

        try:
            stats = await self._brain.get_stats()

            total = int(_float(stats.get("total_concepts", 0)))
            if total == 0:
                return json.dumps(
                    {
                        "action": "introspection",
                        "total_concepts": 0,
                        "message": "My memory is empty — I haven't learned anything yet.",
                    }
                )

            return json.dumps(
                {
                    "action": "introspection",
                    "total_concepts": total,
                    "categories": stats.get("categories", {}),
                    "total_relations": stats.get("total_relations", 0),
                    "total_episodes": stats.get("total_episodes", 0),
                    "message": (
                        f"I know {total} concept(s) across "
                        f"{len(stats.get('categories', {}))} categories, "  # type: ignore[arg-type]
                        f"with {stats.get('total_relations', 0)} connections "
                        f"and {stats.get('total_episodes', 0)} conversation memories."
                    ),
                }
            )

        except Exception as e:  # noqa: BLE001
            return json.dumps({"action": "error", "message": f"Error: {e}"})


# ── Helpers ──


def _float(val: object, default: float = 0.0) -> float:
    """Safely extract float from dict value."""
    try:
        return float(val)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _auto_name(content: str) -> str:
    """Generate a short name from content."""
    name = content[:50].strip().replace("\n", " ")
    if len(content) > 50:  # noqa: PLR2004
        name += "..."
    return name


def _truncate(text: str, max_len: int) -> str:
    """Truncate text with ellipsis."""
    if len(text) > max_len:
        return text[:max_len] + "..."
    return text
