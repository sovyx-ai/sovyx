"""Sovyx Knowledge Plugin — Brain interface for LLM tool calling.

Enterprise-grade knowledge management via the Plugin SDK.
Deduplication, conflict-aware storage, semantic search, episodic recall.

Permissions required: brain:read, brain:write

Response Schema (all tools):
    Every tool returns a JSON object with at minimum:
    - action: str — what happened (e.g. "created", "reinforced", "search")
    - ok: bool — true if successful
    - message: str — human-readable summary

    Error responses always have:
    - action: "error"
    - ok: false
    - message: str — error description
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
        about_person: str = "",
    ) -> str:
        """Store information in long-term memory with semantic deduplication.

        Before creating a new concept, checks if semantically similar content
        already exists (cosine similarity >= threshold). If found, classifies
        the relationship (SAME/EXTENDS/CONTRADICTS/UNRELATED) and acts.

        Use about_person to scope a memory to a specific person, e.g.:
        remember("prefers dark mode", about_person="Guipe")

        Args:
            what: The information to remember.
            name: Short name/title (auto-generated if empty).
            category: Category (fact, preference, event, person).
            about_person: Person this memory is about (optional).

        Returns:
            JSON with action (created|reinforced|updated|extended), details.
        """
        if self._brain is None:
            return _err("brain access not configured")

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
                # Found near-duplicate — classify relationship before acting
                best = similar[0]
                existing_id = str(best.get("id", ""))
                existing_name = str(best.get("name", "?"))
                existing_content = str(best.get("content", ""))
                similarity = _float(best.get("similarity", 0))

                # Classify: SAME, EXTENDS, CONTRADICTS, UNRELATED
                relation = await self._brain.classify_content(
                    existing_content,
                    what,
                )

                if relation == "CONTRADICTS":
                    # Contradiction: update content (recency wins), reduce confidence
                    old_conf = _float(best.get("confidence", 0.5))
                    new_conf = max(0.1, old_conf * 0.7)  # 30% confidence penalty
                    await self._brain.update(
                        existing_id,
                        content=what,
                        confidence=new_conf,
                    )
                    return json.dumps(
                        {
                            "action": "updated",
                            "ok": True,
                            "resolution": "contradiction",
                            "concept_id": existing_id,
                            "name": existing_name,
                            "old_content": _truncate(existing_content, 200),
                            "new_content": _truncate(what, 200),
                            "confidence": {"old": round(old_conf, 3), "new": round(new_conf, 3)},
                            "message": (
                                f"Updated '{existing_name}' — detected contradiction. "
                                f"New info replaces old. Confidence reduced "
                                f"{old_conf:.0%} → {new_conf:.0%} (needs reconfirmation)."
                            ),
                        }
                    )

                if relation == "EXTENDS":
                    # Extension: append new info, boost confidence
                    merged = f"{existing_content}\n{what}"
                    if len(merged) > 10_000:
                        merged = merged[:10_000]
                    old_conf = _float(best.get("confidence", 0.5))
                    new_conf = min(1.0, old_conf + 0.08)
                    await self._brain.update(
                        existing_id,
                        content=merged,
                        confidence=new_conf,
                    )
                    return json.dumps(
                        {
                            "action": "extended",
                            "ok": True,
                            "resolution": "extension",
                            "concept_id": existing_id,
                            "name": existing_name,
                            "confidence": {"old": round(old_conf, 3), "new": round(new_conf, 3)},
                            "message": (
                                f"Extended '{existing_name}' with new details. "
                                f"Confidence {old_conf:.0%} → {new_conf:.0%}."
                            ),
                        }
                    )

                if relation == "UNRELATED":
                    # High embedding similarity but semantically unrelated
                    # → create as new concept (false positive dedup)
                    pass  # fall through to create
                else:
                    # SAME: full reinforcement cycle
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
                            f"I already knew this: '{existing_name}' "
                            f"(similarity: {similarity:.0%}). Reinforced — "
                            f"confidence {old_conf:.0%} → {new_conf:.0%}, "
                            f"importance {old_imp:.0%} → {new_imp:.0%}."
                        )
                        if established:
                            msg += " This is now an established memory."

                        return json.dumps(
                            {
                                "action": "reinforced",
                                "ok": True,
                                "concept_id": existing_id,
                                "name": existing_name,
                                "similarity": round(similarity, 3),
                                "importance": {
                                    "old": round(old_imp, 3),
                                    "new": round(new_imp, 3),
                                },
                                "confidence": {
                                    "old": round(old_conf, 3),
                                    "new": round(new_conf, 3),
                                },
                                "reinforcement_count": rc,
                                "established": established,
                                "message": msg,
                            }
                        )

            # Phase 2: No semantic duplicate — create via BrainService
            # (BrainService handles name-based dedup + contradiction detection)
            # Build metadata
            meta: dict[str, object] = {}
            if about_person:
                meta["person"] = about_person
                # If no explicit category, auto-set to "person"
                if category == "fact":
                    category = "person"

            concept_id = await self._brain.learn(
                name=name,
                content=what,
                category=category,
                metadata=meta if meta else None,
            )

            # Phase 3: Auto-relation — link to related existing concepts
            relations_created = await self._auto_relate(concept_id, what)

            result: dict[str, object] = {
                "action": "created",
                "ok": True,
                "concept_id": concept_id,
                "name": name,
                "category": category,
                "message": f"Remembered: '{name}'",
            }
            if about_person:
                result["about_person"] = about_person
            if relations_created:
                result["relations"] = relations_created
                result["message"] = (
                    f"Remembered: '{name}' (linked to {len(relations_created)} related concept(s))"
                )

            return json.dumps(result)

        except Exception as e:  # noqa: BLE001
            return _err(f"Error remembering: {e}")

    # ── auto-relation (internal) ──

    async def _auto_relate(
        self,
        concept_id: str,
        content: str,
    ) -> list[dict[str, object]]:
        """Link a new concept to related existing concepts.

        Finds existing concepts with moderate similarity (0.65–0.87)
        — similar enough to be related but not duplicates — and creates
        RELATED_TO relations.

        Args:
            concept_id: Newly created concept ID.
            content: Content text for similarity search.

        Returns:
            List of relation dicts with target_id, target_name, similarity.
        """
        if self._brain is None:
            return []

        try:
            # Find related (not duplicate) concepts
            similar = await self._brain.find_similar(
                content,
                threshold=_AUTO_RELATE_THRESHOLD,
                limit=_AUTO_RELATE_MAX + 5,  # over-fetch, filter dedup range
            )

            relations: list[dict[str, object]] = []
            for candidate in similar:
                cid = str(candidate.get("id", ""))
                sim = _float(candidate.get("similarity", 0))

                # Skip self
                if cid == concept_id:
                    continue
                # Skip dedup range (those would have been caught by dedup)
                if sim >= self._dedup_threshold:
                    continue
                if len(relations) >= _AUTO_RELATE_MAX:
                    break

                # Create relation
                try:
                    await self._brain.create_relation(
                        concept_id,
                        cid,
                        "related_to",
                    )
                    relations.append(
                        {
                            "target_id": cid,
                            "target_name": str(candidate.get("name", "")),
                            "similarity": round(sim, 3),
                        }
                    )
                except Exception:  # noqa: BLE001
                    continue  # relation creation failure is non-fatal

            return relations

        except Exception:  # noqa: BLE001
            return []  # auto-relation failure is non-fatal

    # ── search ──

    @tool(
        description=(
            "Search memory for information matching a query. "
            "Use about_person to filter memories about a specific person."
        ),
    )
    async def search(
        self,
        query: str,
        limit: int = 5,
        about_person: str = "",
    ) -> str:
        """Search long-term memory via hybrid retrieval (semantic + keyword).

        Args:
            query: What to search for.
            limit: Max results (1–10).
            about_person: Filter results to this person only.

        Returns:
            JSON with results array.
        """
        if self._brain is None:
            return _err("brain access not configured")

        # Over-fetch when filtering by person (post-filter)
        fetch_limit = max(1, min(self._max_results, limit))
        if about_person:
            fetch_limit = min(50, fetch_limit * 5)

        try:
            results = await self._brain.search(query, limit=fetch_limit)
        except Exception as e:  # noqa: BLE001
            return _err(f"Error searching: {e}")

        # Post-filter by person if specified
        if about_person:
            person_lower = about_person.lower()
            results = [r for r in results if _match_person(r, person_lower)][:limit]

        if not results:
            return json.dumps(
                {
                    "action": "search",
                    "ok": True,
                    "query": query,
                    "about_person": about_person or None,
                    "results": [],
                    "message": f"No memories found for: {query}"
                    + (f" (about {about_person})" if about_person else ""),
                }
            )

        return json.dumps(
            {
                "action": "search",
                "ok": True,
                "query": query,
                "about_person": about_person or None,
                "count": len(results),
                "message": f"Found {len(results)} result(s) for: {query}",
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

    @tool(
        description=(
            "Forget a piece of information. "
            "Use forget_all=true to remove everything matching the query."
        ),
    )
    async def forget(self, query: str, forget_all: bool = False) -> str:
        """Remove matching information from memory.

        Single mode (default): finds best match, deletes it with full cascade
        (relations, embeddings, working memory, emits ConceptForgotten event).
        Bulk mode (forget_all=true): deletes ALL matches (up to 20).

        Args:
            query: What to forget.
            forget_all: If true, delete all matches.

        Returns:
            JSON with action and details of what was deleted.
        """
        if self._brain is None:
            return _err("brain access not configured")

        try:
            if forget_all:
                deleted_list = await self._brain.forget_all(query, limit=20)
                if not deleted_list:
                    return json.dumps(
                        {
                            "action": "not_found",
                            "ok": True,
                            "query": query,
                            "message": f"Nothing found matching: {query}",
                        }
                    )
                success_count = sum(1 for d in deleted_list if d.get("deleted"))
                names = [str(d.get("name", "?")) for d in deleted_list if d.get("deleted")]
                return json.dumps(
                    {
                        "action": "forgotten_all",
                        "ok": True,
                        "query": query,
                        "count": success_count,
                        "deleted": deleted_list,
                        "message": (
                            f"Forgotten {success_count} memory(ies) about '{query}': "
                            + ", ".join(names)
                        ),
                    }
                )

            # Single delete — best match
            results = await self._brain.search(query, limit=1)

            if not results:
                return json.dumps(
                    {
                        "action": "not_found",
                        "ok": True,
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
                        "ok": True,
                        "concept_id": concept_id,
                        "name": concept_name,
                        "message": f"Forgotten: '{concept_name}'",
                    }
                )

            return json.dumps(
                {
                    "action": "not_found",
                    "ok": True,
                    "concept_id": concept_id,
                    "message": f"Concept '{concept_name}' not found for deletion.",
                }
            )

        except Exception as e:  # noqa: BLE001
            return _err(f"Error: {e}")

    # ── recall_about ──

    @tool(
        description=(
            "Recall everything known about a topic. Broader than search — returns more context."
        ),
    )
    async def recall_about(self, topic: str) -> str:
        """Deep recall — concepts + graph neighbors + episodic context.

        Three enrichment layers:
        1. Semantic search for matching concepts
        2. Graph neighbors for top-3 results (what's connected)
        3. Episode search for temporal context (when was this discussed)

        Args:
            topic: Topic to recall about.

        Returns:
            JSON with concepts (enriched), episodes, and summary.
        """
        if self._brain is None:
            return _err("brain access not configured")

        try:
            results = await self._brain.search(topic, limit=self._max_results)

            if not results:
                return json.dumps(
                    {
                        "action": "recall",
                        "ok": True,
                        "topic": topic,
                        "results": [],
                        "episodes": [],
                        "message": f"I don't have any memories about: {topic}",
                    }
                )

            # Layer 1+2: Concepts + graph neighbors
            concepts: list[dict[str, object]] = []
            for r in results:
                concept_data: dict[str, object] = {
                    "id": str(r.get("id", "")),
                    "name": str(r.get("name", "")),
                    "content": _truncate(str(r.get("content", "")), 400),
                    "category": str(r.get("category", "")),
                    "importance": round(_float(r.get("importance", 0)), 3),
                    "confidence": round(_float(r.get("confidence", 0)), 3),
                }

                # Graph neighbors for top-3
                if len(concepts) < 3:
                    cid = str(r.get("id", ""))
                    if cid:
                        try:
                            related = await self._brain.get_related(cid, limit=3)
                            concept_data["related"] = [str(rel.get("name", "")) for rel in related]
                        except Exception:  # noqa: BLE001
                            concept_data["related"] = []

                concepts.append(concept_data)

            # Layer 3: Episodic context — when was this topic discussed?
            episodes: list[dict[str, object]] = []
            try:
                raw_episodes = await self._brain.search_episodes(topic, limit=5)
                for ep in raw_episodes:
                    episodes.append(
                        {
                            "summary": _truncate(str(ep.get("summary", "")), 200),
                            "timestamp": str(ep.get("timestamp", "")),
                            "channel": str(ep.get("channel", "")),
                            "turn_count": ep.get("turn_count", 0),
                        }
                    )
            except Exception:  # noqa: BLE001
                pass  # episode search failure is non-fatal

            ep_msg = f" ({len(episodes)} episode(s))" if episodes else ""
            result_data: dict[str, object] = {
                "action": "recall",
                "ok": True,
                "topic": topic,
                "count": len(concepts),
                "message": f"Found {len(concepts)} concept(s) about '{topic}'{ep_msg}",
                "results": concepts,
            }

            if episodes:
                result_data["episodes"] = episodes
                result_data["episode_count"] = len(episodes)

            return json.dumps(result_data)

        except Exception as e:  # noqa: BLE001
            return _err(f"Error recalling: {e}")

    # ── what_do_you_know ──

    @tool(description="List what you know — summary of stored memories.")
    async def what_do_you_know(self) -> str:
        """Summary of all stored knowledge using brain stats.

        Returns category breakdown, total counts, and top concepts.
        """
        if self._brain is None:
            return _err("brain access not configured")

        try:
            stats = await self._brain.get_stats()

            total = int(_float(stats.get("total_concepts", 0)))
            if total == 0:
                return json.dumps(
                    {
                        "action": "introspection",
                        "ok": True,
                        "total_concepts": 0,
                        "message": "My memory is empty — I haven't learned anything yet.",
                    }
                )

            # Fetch top concepts by importance
            top_concepts: list[dict[str, object]] = []
            try:
                raw_top = await self._brain.get_top_concepts(limit=5)
                top_concepts = [
                    {
                        "name": str(c.get("name", "")),
                        "category": str(c.get("category", "")),
                        "importance": round(_float(c.get("importance", 0)), 3),
                        "confidence": round(_float(c.get("confidence", 0)), 3),
                        "access_count": c.get("access_count", 0),
                    }
                    for c in raw_top
                ]
            except Exception:  # noqa: BLE001
                pass  # top concepts fetch failure is non-fatal

            cats = stats.get("categories", {})
            cat_count = len(cats) if isinstance(cats, dict) else 0

            result: dict[str, object] = {
                "action": "introspection",
                "ok": True,
                "total_concepts": total,
                "categories": cats,
                "total_relations": stats.get("total_relations", 0),
                "total_episodes": stats.get("total_episodes", 0),
                "message": (
                    f"I know {total} concept(s) across "
                    f"{cat_count} categories, "
                    f"with {stats.get('total_relations', 0)} connections "
                    f"and {stats.get('total_episodes', 0)} conversation memories."
                ),
            }

            if top_concepts:
                result["top_concepts"] = top_concepts

            return json.dumps(result)

        except Exception as e:  # noqa: BLE001
            return _err(f"Error: {e}")


# ── Helpers ──


def _float(val: object, default: float = 0.0) -> float:
    """Safely extract float from dict value."""
    try:
        return float(val)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


_AUTO_RELATE_THRESHOLD = 0.65  # lower than dedup — "related" not "same"
_AUTO_RELATE_MAX = 3


class _RelationInfo(typing.NamedTuple):
    """Lightweight relation info for auto-relation results."""

    target_id: str
    target_name: str
    similarity: float


def _ok(action: str, message: str, **extra: object) -> str:
    """Build a successful JSON response."""
    data: dict[str, object] = {"action": action, "ok": True, "message": message}
    data.update(extra)
    return json.dumps(data)


def _err(message: str) -> str:
    """Build an error JSON response."""
    return json.dumps({"action": "error", "ok": False, "message": message})


def _match_person(result: dict[str, object], person_lower: str) -> bool:
    """Check if a search result is about a specific person.

    Checks metadata.person, content text, and name for person mention.
    """
    # Check metadata.person field (set by about_person param)
    meta = result.get("metadata")
    if isinstance(meta, dict):
        meta_person = str(meta.get("person", "")).lower()
        if meta_person and person_lower in meta_person:
            return True

    # Check content and name for person mention
    content = str(result.get("content", "")).lower()
    name = str(result.get("name", "")).lower()
    return person_lower in content or person_lower in name


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
