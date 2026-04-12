"""Sovyx Plugin Context — Sandboxed access objects for plugins.

PluginContext is the single entry point for plugins to interact with
the engine. Each access object (brain, events, etc.) is gated by
permissions and enforced at runtime by PermissionEnforcer.

Spec: SPE-008 §3 (PluginContext), SPE-008-SANDBOX §4.2
TASK-470: Expanded BrainAccess with forget/update/find_similar/get_related/search_episodes
"""

from __future__ import annotations

import contextlib
import dataclasses
import typing

from sovyx.observability.logging import get_logger
from sovyx.plugins.permissions import (
    PermissionDeniedError,
    PermissionEnforcer,
)

if typing.TYPE_CHECKING:  # pragma: no cover
    import logging
    from pathlib import Path

    from sovyx.brain.service import BrainService
    from sovyx.engine.events import Event, EventBus, EventHandler

logger = get_logger(__name__)

# ── Brain Access (permission-gated) ────────────────────────────────


_MAX_SEARCH_RESULTS = 50
_MAX_CONCEPT_CONTENT = 10_240  # 10KB per concept
_SIMILARITY_THRESHOLD = 0.9  # cosine similarity for "near-duplicate"


class BrainAccess:
    """Scoped brain access for plugins.

    Enforces:
    - brain:read for search/recall/find_similar/get_related/search_episodes
    - brain:write for learn/forget/update
    - Source tagging: all plugin-created concepts tagged "plugin:{name}"
    - Result cap: max 50 results per search
    - Content limit: max 10KB per concept
    - Audit logging: all write operations logged with plugin name

    Spec: SPE-008-SANDBOX §4.2 (EnforcedBrainAccess)
    """

    def __init__(
        self,
        brain: BrainService,
        enforcer: PermissionEnforcer,
        *,
        write_allowed: bool,
        plugin_name: str,
        mind_id: str = "default",
    ) -> None:
        self._brain = brain
        self._enforcer = enforcer
        self._write = write_allowed
        self._plugin = plugin_name
        self._mind_id = mind_id

    # ── Read Operations ──

    async def search(self, query: str, *, limit: int = 5) -> list[dict[str, object]]:
        """Search concepts by semantic similarity (hybrid: KNN + FTS5 + RRF).

        Args:
            query: Search text.
            limit: Max results (capped at 50).

        Returns:
            List of dicts with id, name, content, category, importance,
            confidence, access_count, source.

        Raises:
            PermissionDeniedError: brain:read not granted.
        """
        from sovyx.engine.types import MindId

        self._enforcer.check("brain:read")
        capped = min(limit, _MAX_SEARCH_RESULTS)
        results = await self._brain.search(
            query=query,
            mind_id=MindId(self._mind_id),
            limit=capped,
        )
        return [self._concept_to_dict(concept, score) for concept, score in results]

    async def find_similar(
        self,
        content: str,
        *,
        threshold: float = _SIMILARITY_THRESHOLD,
        limit: int = 5,
    ) -> list[dict[str, object]]:
        """Find concepts with similar content via embedding cosine similarity.

        Used for deduplication: if cosine similarity >= threshold, concepts
        are considered near-duplicates.

        Args:
            content: Content text to compare against.
            threshold: Minimum cosine similarity (0.0–1.0). Default 0.9.
            limit: Max results (capped at 50).

        Returns:
            List of concept dicts with added 'similarity' field.
            Only concepts above threshold are returned.

        Raises:
            PermissionDeniedError: brain:read not granted.
        """
        from sovyx.engine.types import MindId

        self._enforcer.check("brain:read")
        capped = min(limit, _MAX_SEARCH_RESULTS)

        # Encode content → embedding
        embedding = await self._brain._embedding.encode(content, is_query=False)

        # Search by embedding (returns distance, not similarity)
        try:
            raw = await self._brain._concepts.search_by_embedding(
                embedding,
                MindId(self._mind_id),
                limit=capped,
            )
        except Exception:  # noqa: BLE001
            # sqlite-vec unavailable — fall back to FTS5 text search
            logger.debug("find_similar_vec_unavailable_using_fts5")
            fts = await self._brain._concepts.search_by_text(
                content[:200],
                MindId(self._mind_id),
                limit=capped,
            )
            return [self._concept_to_dict(c, 0.0) for c, _ in fts]

        # Convert L2 distance to cosine similarity approximation
        # For normalized embeddings: cosine_sim ≈ 1 - (distance² / 2)
        results: list[dict[str, object]] = []
        for concept, distance in raw:
            similarity = max(0.0, 1.0 - (distance * distance / 2.0))
            if similarity >= threshold:
                d = self._concept_to_dict(concept, similarity)
                d["similarity"] = similarity
                results.append(d)

        results.sort(key=lambda x: float(x.get("similarity", 0) or 0), reverse=True)  # type: ignore[arg-type]
        return results[:capped]

    async def get_related(
        self,
        concept_id: str,
        *,
        limit: int = 10,
    ) -> list[dict[str, object]]:
        """Get concepts connected to a given concept via the knowledge graph.

        Traverses relations (RELATED_TO, PART_OF, CAUSES, etc.) to find
        neighbors in the concept graph.

        Args:
            concept_id: ID of the source concept.
            limit: Max neighbors (capped at 50).

        Returns:
            List of related concept dicts.

        Raises:
            PermissionDeniedError: brain:read not granted.
        """
        from sovyx.engine.types import ConceptId

        self._enforcer.check("brain:read")
        capped = min(limit, _MAX_SEARCH_RESULTS)
        concepts = await self._brain.get_related(ConceptId(concept_id), limit=capped)
        return [self._concept_to_dict(c, 0.0) for c in concepts]

    async def search_episodes(
        self,
        query: str,
        *,
        limit: int = 5,
    ) -> list[dict[str, object]]:
        """Search episodic memories (conversation exchanges).

        Episodes represent actual conversation turns with emotional context.
        Useful for temporal queries: "when did I mention X?", "what was
        the context when we talked about Y?"

        Args:
            query: Search text.
            limit: Max results (capped at 50).

        Returns:
            List of episode dicts with user_input, assistant_response,
            summary, importance, emotional_valence, emotional_arousal,
            created_at.

        Raises:
            PermissionDeniedError: brain:read not granted.
        """
        from sovyx.engine.types import MindId

        self._enforcer.check("brain:read")
        capped = min(limit, _MAX_SEARCH_RESULTS)
        results = await self._brain._retrieval.search_episodes(
            query,
            MindId(self._mind_id),
            limit=capped,
        )
        return [
            {
                "id": str(ep.id),
                "user_input": ep.user_input,
                "assistant_response": ep.assistant_response,
                "summary": ep.summary or "",
                "importance": ep.importance,
                "emotional_valence": ep.emotional_valence,
                "emotional_arousal": ep.emotional_arousal,
                "conversation_id": str(ep.conversation_id),
                "created_at": ep.metadata.get(
                    "created_at",
                    "",
                ),
            }
            for ep, _score in results
        ]

    # ── Write Operations ──

    async def learn(
        self,
        name: str,
        content: str,
        *,
        category: str = "fact",
        importance: float | None = None,
        confidence: float | None = None,
        emotional_valence: float = 0.0,
        metadata: dict[str, object] | None = None,
    ) -> str:
        """Create a new concept in the Mind's memory.

        All plugin-created concepts are tagged with source="plugin:{name}".
        Content is limited to 10KB.

        If a concept with the same name+category already exists, the Brain
        automatically handles dedup: reinforces importance/confidence,
        detects contradictions, and updates content as needed.

        Args:
            name: Concept name/title.
            content: Concept content text (max 10KB).
            category: Category string. Default "fact".
            importance: Initial importance [0.0, 1.0]. None = 0.5.
            confidence: Initial confidence [0.0, 1.0]. None = 0.5.
            emotional_valence: Sentiment [-1.0, 1.0]. Default 0.0.

        Returns:
            Created/reinforced concept ID string.

        Raises:
            PermissionDeniedError: brain:write not granted.
            ValueError: Content exceeds 10KB limit.
        """
        from sovyx.engine.types import ConceptCategory, MindId

        self._enforcer.check("brain:write")
        if not self._write:
            raise PermissionDeniedError(self._plugin, "brain:write")
        if len(content) > _MAX_CONCEPT_CONTENT:
            msg = (
                f"Concept content exceeds {_MAX_CONCEPT_CONTENT} byte limit ({len(content)} bytes)"
            )
            raise ValueError(msg)

        # Map string category to enum, default FACT
        try:
            cat_enum = ConceptCategory(category)
        except ValueError:
            cat_enum = ConceptCategory.FACT

        concept_id = await self._brain.learn_concept(
            mind_id=MindId(self._mind_id),
            name=name,
            content=content,
            category=cat_enum,
            source=f"plugin:{self._plugin}",
            importance=importance,
            confidence=confidence,
            emotional_valence=emotional_valence,
            metadata=metadata or {},
        )

        logger.info(
            "brain_access_learn",
            plugin=self._plugin,
            concept_id=str(concept_id),
            name=name,
            category=category,
        )
        return str(concept_id)

    async def forget(self, concept_id: str) -> bool:
        """Delete a concept with full cascade cleanup.

        1. Count relations (for audit)
        2. Delete concept (relations cascade via FK, embeddings cleaned)
        3. Remove from working memory
        4. Emit ConceptForgotten event

        Args:
            concept_id: ID of the concept to delete.

        Returns:
            True if deleted, False if concept not found.

        Raises:
            PermissionDeniedError: brain:write not granted.
        """
        from sovyx.engine.events import ConceptForgotten
        from sovyx.engine.types import ConceptId

        self._enforcer.check("brain:write")
        if not self._write:
            raise PermissionDeniedError(self._plugin, "brain:write")

        cid = ConceptId(concept_id)
        concept = await self._brain.get_concept(cid)
        if concept is None:
            return False

        # Count relations for audit trail
        relation_count = 0
        try:
            relations = await self._brain._relations.get_relations_for(cid)
            relation_count = len(relations)
        except Exception:  # noqa: BLE001
            pass

        # Delete (cascades relations + embeddings)
        await self._brain._concepts.delete(cid)

        # Remove from working memory
        try:
            self._brain._memory._activations.pop(str(cid), None)
            self._brain._memory._importance.pop(str(cid), None)
        except (AttributeError, KeyError):
            pass  # working memory may not have this concept

        # Emit event (failure shouldn't block deletion)
        with contextlib.suppress(Exception):
            await self._brain._events.emit(
                ConceptForgotten(
                    concept_id=concept_id,
                    concept_name=concept.name,
                    source=f"plugin:{self._plugin}",
                    cascade_relations=relation_count,
                )
            )

        logger.info(
            "brain_access_forget",
            plugin=self._plugin,
            concept_id=concept_id,
            concept_name=concept.name,
            cascade_relations=relation_count,
        )
        return True

    async def forget_all(self, query: str, *, limit: int = 10) -> list[dict[str, object]]:
        """Delete all concepts matching a query.

        Searches for matching concepts, then deletes each one with
        full cascade (forget()). Use for "forget everything about X".

        Args:
            query: Search query to find concepts to delete.
            limit: Max concepts to delete in one call (safety cap, max 20).

        Returns:
            List of dicts with id, name, deleted status for each concept.

        Raises:
            PermissionDeniedError: brain:write not granted.
        """
        from sovyx.engine.types import MindId

        self._enforcer.check("brain:write")
        if not self._write:
            raise PermissionDeniedError(self._plugin, "brain:write")

        capped = min(limit, 20)
        results = await self._brain.search(
            query=query,
            mind_id=MindId(self._mind_id),
            limit=capped,
        )

        deleted: list[dict[str, object]] = []
        for concept, _score in results:
            cid = str(concept.id)
            name = concept.name
            success = await self.forget(cid)
            deleted.append({"id": cid, "name": name, "deleted": success})

        logger.info(
            "brain_access_forget_all",
            plugin=self._plugin,
            query=query,
            count=len(deleted),
            success=sum(1 for d in deleted if d["deleted"]),
        )
        return deleted

    async def update(
        self,
        concept_id: str,
        *,
        content: str | None = None,
        name: str | None = None,
        importance: float | None = None,
        confidence: float | None = None,
    ) -> bool:
        """Update an existing concept's fields.

        Only provided fields are updated; None fields are left unchanged.
        Content is validated against the 10KB limit.

        Args:
            concept_id: ID of the concept to update.
            content: New content (max 10KB). None = no change.
            name: New name. None = no change.
            importance: New importance [0.0, 1.0]. None = no change.
            confidence: New confidence [0.0, 1.0]. None = no change.

        Returns:
            True if updated, False if concept not found.

        Raises:
            PermissionDeniedError: brain:write not granted.
            ValueError: Content exceeds 10KB limit.
        """
        from sovyx.engine.types import ConceptId

        self._enforcer.check("brain:write")
        if not self._write:
            raise PermissionDeniedError(self._plugin, "brain:write")

        if content is not None and len(content) > _MAX_CONCEPT_CONTENT:
            msg = (
                f"Concept content exceeds {_MAX_CONCEPT_CONTENT} byte limit ({len(content)} bytes)"
            )
            raise ValueError(msg)

        cid = ConceptId(concept_id)
        concept = await self._brain.get_concept(cid)
        if concept is None:
            return False

        if content is not None:
            concept.content = content
        if name is not None:
            concept.name = name
        if importance is not None:
            concept.importance = max(0.0, min(1.0, importance))
        if confidence is not None:
            concept.confidence = max(0.0, min(1.0, confidence))

        await self._brain._concepts.update(concept)

        logger.info(
            "brain_access_update",
            plugin=self._plugin,
            concept_id=concept_id,
        )
        return True

    async def create_relation(
        self,
        source_id: str,
        target_id: str,
        relation_type: str = "related_to",
    ) -> str:
        """Create or retrieve a relation between two concepts.

        The relation is canonicalized (order doesn't matter): calling
        create_relation(A, B) and create_relation(B, A) yields the
        same relation row.

        Valid relation types: related_to, part_of, causes, contradicts,
        example_of, temporal, emotional.

        Args:
            source_id: One concept ID.
            target_id: Other concept ID.
            relation_type: Relation type string. Default "related_to".

        Returns:
            Relation ID string.

        Raises:
            PermissionDeniedError: brain:write not granted.
            ValueError: Invalid relation type.
        """
        from sovyx.engine.types import ConceptId, RelationType

        self._enforcer.check("brain:write")
        if not self._write:
            raise PermissionDeniedError(self._plugin, "brain:write")

        try:
            rel_type = RelationType(relation_type)
        except ValueError:
            valid = [r.value for r in RelationType]
            msg = f"Invalid relation_type '{relation_type}'. Valid: {valid}"
            raise ValueError(msg) from None

        relation = await self._brain._relations.get_or_create(
            source_id=ConceptId(source_id),
            target_id=ConceptId(target_id),
            relation_type=rel_type,
        )

        logger.info(
            "brain_access_create_relation",
            plugin=self._plugin,
            source=source_id,
            target=target_id,
            relation_type=relation_type,
            relation_id=str(relation.id),
        )
        return str(relation.id)

    async def get_top_concepts(
        self,
        limit: int = 10,
        *,
        category: str | None = None,
    ) -> list[dict[str, object]]:
        """Get top concepts ordered by importance (descending).

        Args:
            limit: Max results (capped at 50).
            category: Optional filter by category.

        Returns:
            List of concept dicts with id, name, content, category,
            importance, confidence, access_count.

        Raises:
            PermissionDeniedError: brain:read not granted.
        """
        from sovyx.engine.types import MindId

        self._enforcer.check("brain:read")
        capped = min(limit, _MAX_SEARCH_RESULTS)

        async with self._brain._concepts._pool.read() as conn:
            if category:
                cursor = await conn.execute(
                    "SELECT id, name, content, category, importance, confidence, "
                    "access_count FROM concepts WHERE mind_id = ? AND category = ? "
                    "ORDER BY importance DESC, confidence DESC LIMIT ?",
                    (str(MindId(self._mind_id)), category, capped),
                )
            else:
                cursor = await conn.execute(
                    "SELECT id, name, content, category, importance, confidence, "
                    "access_count FROM concepts WHERE mind_id = ? "
                    "ORDER BY importance DESC, confidence DESC LIMIT ?",
                    (str(MindId(self._mind_id)), capped),
                )
            rows = await cursor.fetchall()

        return [
            {
                "id": str(r[0]),
                "name": str(r[1]),
                "content": str(r[2]),
                "category": str(r[3]),
                "importance": float(r[4]) if r[4] is not None else 0.0,
                "confidence": float(r[5]) if r[5] is not None else 0.0,
                "access_count": int(r[6]) if r[6] is not None else 0,
            }
            for r in rows
        ]

    async def classify_content(
        self,
        old_content: str,
        new_content: str,
    ) -> str:
        """Classify the semantic relationship between two content strings.

        Uses the Brain's contradiction detection engine (LLM-assisted with
        heuristic fallback) to classify as:
        - SAME: Semantically equivalent (paraphrase, synonym)
        - EXTENDS: New adds info without contradicting
        - CONTRADICTS: New conflicts with existing
        - UNRELATED: Different topics

        Permission: brain:read (analysis only, no mutation).

        Args:
            old_content: Existing concept content.
            new_content: Incoming content.

        Returns:
            Classification string: "SAME", "EXTENDS", "CONTRADICTS", "UNRELATED".

        Raises:
            PermissionDeniedError: brain:read not granted.
        """
        from sovyx.brain.contradiction import detect_contradiction

        self._enforcer.check("brain:read")
        relation = await detect_contradiction(
            old_content=old_content,
            new_content=new_content,
            llm_router=self._brain._llm_router,
            fast_model=self._brain._fast_model,
        )
        return relation.value

    async def reinforce(
        self,
        concept_id: str,
        *,
        importance_delta: float = 0.05,
        confidence_delta: float = 0.10,
    ) -> dict[str, object] | None:
        """Reinforce a concept — boost importance, confidence, access, metadata.

        Full reinforcement cycle:
        1. Boost importance by delta (capped at 1.0)
        2. Boost confidence by delta (capped at 1.0)
        3. Increment access_count + update last_accessed
        4. Track reinforcement_count in metadata
        5. Mark as 'established' if reinforcement_count >= 5

        Args:
            concept_id: Concept to reinforce.
            importance_delta: Importance boost [0.0, 0.5]. Default 0.05.
            confidence_delta: Confidence boost [0.0, 0.5]. Default 0.10.

        Returns:
            Dict with old/new scores + reinforcement_count + established flag.
            None if concept not found.

        Raises:
            PermissionDeniedError: brain:write not granted.
        """
        from datetime import UTC, datetime

        from sovyx.engine.types import ConceptId

        self._enforcer.check("brain:write")
        if not self._write:
            raise PermissionDeniedError(self._plugin, "brain:write")

        cid = ConceptId(concept_id)
        concept = await self._brain.get_concept(cid)
        if concept is None:
            return None

        # Snapshot old values
        old_importance = concept.importance
        old_confidence = concept.confidence

        # 1. Boost importance (clamped)
        imp_delta = max(0.0, min(0.5, importance_delta))
        concept.importance = min(1.0, concept.importance + imp_delta)

        # 2. Boost confidence (clamped)
        conf_delta = max(0.0, min(0.5, confidence_delta))
        concept.confidence = min(1.0, concept.confidence + conf_delta)

        # 3. Access tracking
        concept.access_count += 1
        concept.last_accessed = datetime.now(UTC)

        # 4. Reinforcement count in metadata
        rc_raw = concept.metadata.get("reinforcement_count", 0)
        reinforcement_count = (int(rc_raw) if isinstance(rc_raw, (int, float)) else 0) + 1
        concept.metadata["reinforcement_count"] = reinforcement_count

        # 5. Established flag (>= 5 reinforcements)
        established = reinforcement_count >= 5
        if established and not concept.metadata.get("established"):
            concept.metadata["established"] = True
            # Ensure importance reflects established status
            concept.importance = max(concept.importance, 0.8)

        # Persist
        await self._brain._concepts.update(concept)

        logger.info(
            "brain_access_reinforce",
            plugin=self._plugin,
            concept_id=concept_id,
            reinforcement_count=reinforcement_count,
            established=established,
        )

        return {
            "concept_id": concept_id,
            "importance": {"old": old_importance, "new": concept.importance},
            "confidence": {"old": old_confidence, "new": concept.confidence},
            "reinforcement_count": reinforcement_count,
            "established": established,
            "access_count": concept.access_count,
        }

    async def boost_importance(
        self,
        concept_id: str,
        delta: float = 0.05,
    ) -> bool:
        """Boost a concept's importance score.

        The delta is added to the current importance, capped at 1.0.
        Useful for reinforcement: repeated mentions should increase
        a concept's importance rather than create duplicates.

        Args:
            concept_id: Concept to boost.
            delta: Amount to add [0.0, 0.5]. Default 0.05.
                   Clamped to prevent importance inflation.

        Returns:
            True if concept found and boosted, False if not found.

        Raises:
            PermissionDeniedError: brain:write not granted.
        """
        from sovyx.engine.types import ConceptId

        self._enforcer.check("brain:write")
        if not self._write:
            raise PermissionDeniedError(self._plugin, "brain:write")

        cid = ConceptId(concept_id)
        concept = await self._brain.get_concept(cid)
        if concept is None:
            return False

        clamped_delta = max(0.0, min(0.5, delta))
        await self._brain._concepts.boost_importance(cid, clamped_delta)

        logger.debug(
            "brain_access_boost",
            plugin=self._plugin,
            concept_id=concept_id,
            delta=clamped_delta,
        )
        return True

    async def get_stats(self) -> dict[str, object]:
        """Get brain statistics for the current Mind.

        Provides a high-level overview: concept count, category breakdown,
        relation count, episode count. This is the Mind's self-awareness
        data — answers "how much do I know?" and "what categories?"

        Permission: brain:read (read-only introspection).

        Returns:
            Dict with total_concepts, categories (dict of name→count),
            total_relations, total_episodes.

        Raises:
            PermissionDeniedError: brain:read not granted.
        """
        from sovyx.engine.types import MindId

        self._enforcer.check("brain:read")
        mind_id = MindId(self._mind_id)

        # Category breakdown
        categories_raw = await self._brain._concepts.get_categories(mind_id)
        category_counts: dict[str, int] = {}
        total = 0
        for cat in categories_raw:
            count = await self._brain._concepts.count_by_category(mind_id, cat)
            category_counts[cat] = count
            total += count

        # Relation count — get all concepts and count their relations
        # Use a lightweight query approach
        try:
            async with self._brain._relations._pool.read() as conn:
                cursor = await conn.execute(
                    "SELECT COUNT(*) FROM relations WHERE source_id IN "
                    "(SELECT id FROM concepts WHERE mind_id = ?)",
                    (str(mind_id),),
                )
                row = await cursor.fetchone()
                relation_count = int(row[0]) if row else 0
        except Exception:  # noqa: BLE001
            relation_count = 0

        # Episode count
        try:
            async with self._brain._episodes._pool.read() as conn:
                cursor = await conn.execute(
                    "SELECT COUNT(*) FROM episodes WHERE mind_id = ?",
                    (str(mind_id),),
                )
                row = await cursor.fetchone()
                episode_count = int(row[0]) if row else 0
        except Exception:  # noqa: BLE001
            episode_count = 0

        return {
            "total_concepts": total,
            "categories": category_counts,
            "total_relations": relation_count,
            "total_episodes": episode_count,
            "mind_id": self._mind_id,
        }

    # ── Internal Helpers ──

    @staticmethod
    def _concept_to_dict(
        concept: object,
        score: float,
    ) -> dict[str, object]:
        """Convert a Concept model to a plugin-safe dict.

        Exposes all fields the plugin needs without leaking internal models.
        """
        return {
            "id": str(getattr(concept, "id", "")),
            "name": getattr(concept, "name", ""),
            "content": getattr(concept, "content", ""),
            "category": cat.value  # type: ignore[union-attr]
            if hasattr((cat := getattr(concept, "category", None)), "value")
            else str(getattr(concept, "category", "")),
            "importance": getattr(concept, "importance", 0.0),
            "confidence": getattr(concept, "confidence", 0.0),
            "access_count": getattr(concept, "access_count", 0),
            "source": getattr(concept, "source", ""),
            "score": score,
        }


# ── Event Bus Access (permission-gated) ─────────────────────────────


class EventBusAccess:
    """Scoped event bus access for plugins.

    Enforces:
    - event:subscribe for listening
    - event:emit for emitting
    - Auto-cleanup of all subscriptions on teardown

    Spec: SPE-008 §3 (PluginContext events), SPE-008-PLUGIN-IPC §1
    """

    def __init__(
        self,
        event_bus: EventBus,
        enforcer: PermissionEnforcer,
        *,
        plugin_name: str,
    ) -> None:
        self._bus = event_bus
        self._enforcer = enforcer
        self._plugin = plugin_name
        self._subscriptions: list[tuple[type[Event], EventHandler]] = []

    def subscribe(
        self,
        event_type: type[Event],
        handler: EventHandler,
    ) -> None:
        """Subscribe to a typed engine event.

        Subscriptions are tracked and auto-cleaned on teardown().

        Args:
            event_type: Event class to listen for.
            handler: Async handler coroutine.

        Raises:
            PermissionDeniedError: event:subscribe not granted.
        """
        self._enforcer.check("event:subscribe")
        self._bus.subscribe(event_type, handler)
        self._subscriptions.append((event_type, handler))

    async def emit(self, event: Event) -> None:
        """Emit an event. Plugins can emit any event type.

        For cross-plugin communication, use PluginEvent with
        namespace "plugin.{plugin_name}.*".

        Args:
            event: Event instance to emit.

        Raises:
            PermissionDeniedError: event:emit not granted.
        """
        self._enforcer.check("event:emit")
        await self._bus.emit(event)

    def cleanup(self) -> None:
        """Unsubscribe all handlers. Called during plugin teardown."""
        for event_type, handler in self._subscriptions:
            self._bus.unsubscribe(event_type, handler)
        self._subscriptions.clear()

    @property
    def subscription_count(self) -> int:
        """Number of active subscriptions."""
        return len(self._subscriptions)


# ── Plugin Context ──────────────────────────────────────────────────


@dataclasses.dataclass
class PluginContext:
    """Sandboxed context provided to plugins during setup().

    Plugins ONLY get access objects for declared+approved permissions.
    Undeclared services are None.

    Always available:
    - plugin_name, plugin_version, data_dir, config, logger

    Permission-gated (None if not granted):
    - brain: BrainAccess (brain:read / brain:write)
    - event_bus: EventBusAccess (event:subscribe / event:emit)
    - http: SandboxedHttpClient (network:internet / network:local)
    - filesystem: SandboxedFsAccess (fs:read / fs:write)

    Spec: SPE-008 §3
    """

    # Always available
    plugin_name: str
    plugin_version: str
    data_dir: Path
    config: dict[str, object]
    logger: logging.Logger

    # Permission-gated (None = not granted)
    brain: BrainAccess | None = None
    event_bus: EventBusAccess | None = None
    http: object | None = None  # SandboxedHttpClient (TASK-429)
    filesystem: object | None = None  # SandboxedFsAccess (TASK-430)

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, object],
        *,
        timeout_seconds: float = 5.0,
    ) -> object:
        """Invoke a tool from another plugin (cross-plugin IPC).

        Available in v1.1. Current implementation raises NotImplementedError.

        Args:
            tool_name: Fully qualified "plugin_name.tool_name".
            arguments: Tool arguments dict.
            timeout_seconds: Max wait time.

        Returns:
            ToolResult from the target plugin.

        Raises:
            NotImplementedError: Cross-plugin tool invocation not yet available.

        Spec: SPE-008-PLUGIN-IPC §2
        """
        raise NotImplementedError(
            f"Cross-plugin tool invocation available in v1.1. Requested: {tool_name}"
        )

    def is_plugin_available(self, plugin_name: str) -> bool:
        """Check if another plugin is installed and active.

        Available in v1.1. Currently returns False.

        Args:
            plugin_name: Plugin identifier.

        Returns:
            True if plugin is loaded and active.

        Spec: SPE-008-PLUGIN-IPC §2.2
        """
        return False
