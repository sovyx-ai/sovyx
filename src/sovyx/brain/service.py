"""Sovyx BrainService — unified API for the brain subsystem.

Orchestrates: ConceptRepository, EpisodeRepository, RelationRepository,
EmbeddingEngine, SpreadingActivation, HebbianLearning, EbbinghausDecay,
HybridRetrieval, WorkingMemory.

Implements BrainReader + BrainWriter protocols.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from sovyx.engine.events import ConceptCreated, EpisodeEncoded
from sovyx.engine.types import ConceptCategory
from sovyx.observability.logging import get_logger
from sovyx.observability.metrics import get_metrics
from sovyx.observability.tracing import get_tracer

if TYPE_CHECKING:
    from sovyx.brain.concept_repo import ConceptRepository
    from sovyx.brain.embedding import EmbeddingEngine
    from sovyx.brain.episode_repo import EpisodeRepository
    from sovyx.brain.learning import EbbinghausDecay, HebbianLearning
    from sovyx.brain.models import Concept, Episode
    from sovyx.brain.relation_repo import RelationRepository
    from sovyx.brain.retrieval import HybridRetrieval
    from sovyx.brain.spreading import SpreadingActivation
    from sovyx.brain.working_memory import WorkingMemory
    from sovyx.engine.events import EventBus
    from sovyx.engine.types import (
        ConceptId,
        ConversationId,
        EpisodeId,
        MindId,
    )

logger = get_logger(__name__)


class BrainService:
    """Public brain API. Satisfies BrainReader + BrainWriter protocols.

    Lifecycle: start() loads working memory from DB. stop() is a no-op
    (working memory is ephemeral and rebuilt on start).
    """

    def __init__(
        self,
        concept_repo: ConceptRepository,
        episode_repo: EpisodeRepository,
        relation_repo: RelationRepository,
        embedding_engine: EmbeddingEngine,
        spreading: SpreadingActivation,
        hebbian: HebbianLearning,
        decay: EbbinghausDecay,
        retrieval: HybridRetrieval,
        working_memory: WorkingMemory,
        event_bus: EventBus,
    ) -> None:
        self._concepts = concept_repo
        self._episodes = episode_repo
        self._relations = relation_repo
        self._embedding = embedding_engine
        self._spreading = spreading
        self._hebbian = hebbian
        self._decay = decay
        self._retrieval = retrieval
        self._memory = working_memory
        self._events = event_bus
        self._mind_id: MindId | None = None
        self._background_tasks: set[asyncio.Task[None]] = set()

    async def start(self, mind_id: MindId) -> None:
        """Load top-50 recent concepts into working memory."""
        self._mind_id = mind_id
        recent = await self._concepts.get_recent(mind_id, limit=50)
        for concept in recent:
            self._memory.activate(concept.id, concept.importance)
        logger.info(
            "brain_started",
            mind_id=str(mind_id),
            concepts_loaded=len(recent),
        )

    async def stop(self) -> None:
        """Clean up resources."""
        self._memory.clear()
        logger.info("brain_stopped")

    # ── BrainReader interface ──

    async def search(
        self,
        query: str,
        mind_id: MindId,
        limit: int = 10,
    ) -> list[tuple[Concept, float]]:
        """Hybrid search + spreading activation.

        1. HybridRetrieval.search_concepts(query)
        2. SpreadingActivation from results
        3. Record access for each returned concept (fire-and-forget)
        4. Merge and return
        """
        tracer = get_tracer()
        metrics = get_metrics()
        with (
            tracer.start_brain_span("search", query_length=len(query)),
            metrics.measure_latency(metrics.brain_search_latency),
        ):
            results = await self._retrieval.search_concepts(
                query,
                mind_id,
                limit=limit,
            )

        if results:
            seeds = [(c.id, score) for c, score in results]
            spread = await self._spreading.activate(seeds)

            # Build activation map
            spread_map = {str(cid): act for cid, act in spread}

            # Re-score with spreading activation
            rescored: list[tuple[Concept, float]] = []
            for concept, rrf_score in results:
                spread_score = spread_map.get(str(concept.id), 0.0)
                combined = rrf_score + spread_score * 0.1
                rescored.append((concept, combined))

            rescored.sort(key=lambda x: x[1], reverse=True)
            results = rescored[:limit]

        # Fire-and-forget access tracking (v12 audit fix)
        self._track_access([c.id for c, _ in results])

        return results

    async def recall(
        self,
        query: str,
        mind_id: MindId,
    ) -> tuple[list[tuple[Concept, float]], list[Episode]]:
        """Full recall: concepts (with scores) + episodes + spreading.

        Returns (concepts_with_scores, episodes).
        Scores are needed for ContextAssembler Lost-in-Middle ordering.
        """
        concepts = await self.search(query, mind_id)
        episodes_with_scores = await self._retrieval.search_episodes(query, mind_id)
        episodes = [ep for ep, _ in episodes_with_scores]
        return concepts, episodes

    async def get_concept(self, concept_id: ConceptId) -> Concept | None:
        """Get a concept by ID."""
        return await self._concepts.get(concept_id)

    async def get_related(self, concept_id: ConceptId, limit: int = 10) -> list[Concept]:
        """Get concepts related to the given concept via graph."""
        neighbors = await self._relations.get_neighbors(concept_id, limit=limit)
        concepts: list[Concept] = []
        for neighbor_id, _ in neighbors:
            concept = await self._concepts.get(neighbor_id)
            if concept is not None:
                concepts.append(concept)
        return concepts

    # ── BrainWriter interface ──

    async def learn_concept(
        self,
        mind_id: MindId,
        name: str,
        content: str,
        category: ConceptCategory = ConceptCategory.FACT,
        source: str = "conversation",
        *,
        emotional_valence: float = 0.0,
        **kwargs: object,
    ) -> ConceptId:
        """Learn a new concept with dedup check (v13 audit fix).

        If a concept with the same name+category exists, reinforce it
        instead of creating a duplicate.

        Args:
            emotional_valence: Sentiment score (-1.0 to 1.0) for this
                concept. On dedup, uses weighted average with existing.
        """
        # Dedup check via FTS5
        existing = await self._concepts.search_by_text(name, mind_id, limit=3)
        for concept, _rank in existing:
            if concept.name.lower() == name.lower() and concept.category == category:
                # Concept exists — reinforce, don't duplicate

                # Update content if new is longer (more information)
                if len(content) > len(concept.content):
                    concept.content = content

                # Confidence evolution: each corroboration increases
                # confidence by 0.1, capped at 1.0.
                # Formula: base(0.5) + 0.1 * min(count, 5) → max 1.0
                corr_raw = concept.metadata.get("corroboration_count", 0)
                corr = int(corr_raw) if isinstance(corr_raw, (int, float, str)) else 0
                corr += 1
                concept.metadata["corroboration_count"] = corr
                concept.confidence = min(1.0, concept.confidence + 0.1)

                # Importance reinforcement: repeated mention = more important
                # +0.05 per encounter, counters Ebbinghaus decay naturally
                concept.importance = min(1.0, concept.importance + 0.05)

                # Update emotional valence: weighted average
                # (existing has more history, weight it 2:1)
                if emotional_valence != 0.0:
                    old_v = concept.emotional_valence
                    concept.emotional_valence = max(
                        -1.0,
                        min(1.0, (old_v * 2 + emotional_valence) / 3),
                    )

                await self._concepts.update(concept)
                await self._concepts.record_access(concept.id)
                # Re-activate in working memory so decayed concepts
                # regain visibility for star topology's top-K selection.
                # Uses max(current, 0.5) — won't overwrite spreading-boosted ones.
                self._memory.activate(concept.id, 0.5)
                return concept.id

        # New concept
        from sovyx.brain.models import Concept

        concept = Concept(
            mind_id=mind_id,
            name=name,
            content=content,
            category=category,
            source=source,
            emotional_valence=max(-1.0, min(1.0, emotional_valence)),
        )
        concept_id = await self._concepts.create(concept)

        # Activate in working memory
        self._memory.activate(concept_id, concept.importance)

        # Record metrics
        get_metrics().concepts_created.add(1, {"source": source})

        # Emit event
        await self._events.emit(
            ConceptCreated(
                concept_id=str(concept_id),
                title=name,
                source=source,
            )
        )

        logger.debug(
            "concept_learned",
            concept_id=str(concept_id),
            name=name,
        )
        return concept_id

    async def encode_episode(
        self,
        mind_id: MindId,
        conversation_id: ConversationId,
        user_input: str,
        assistant_response: str,
        importance: float = 0.5,
        *,
        new_concept_ids: list[ConceptId] | None = None,
        emotional_valence: float = 0.0,
        emotional_arousal: float = 0.0,
        concepts_mentioned: list[ConceptId] | None = None,
        summary: str | None = None,
        **kwargs: object,
    ) -> EpisodeId:
        """Encode an episode + embedding + star topology Hebbian learning.

        Uses star topology: new concepts pair with each other (within-turn)
        and with top-K existing concepts by activation (cross-turn).
        Existing concepts only reinforce pre-existing relations.

        Args:
            new_concept_ids: Concepts learned this turn — form the hub
                of the star topology. Each connects to top-K existing.
            emotional_valence: Average sentiment of the exchange
                (-1.0 to 1.0). Computed from extracted concept sentiments.
            emotional_arousal: Intensity of emotion in the exchange
                (0.0 to 1.0). Max absolute sentiment across concepts.
            concepts_mentioned: Concept IDs extracted from this exchange.
                Stored on the episode for future retrieval/linking.
            summary: Optional 1-sentence summary of the exchange.
                Generated by LLM, used in context formatting.
        """
        from sovyx.brain.models import Episode

        episode = Episode(
            mind_id=mind_id,
            conversation_id=conversation_id,
            user_input=user_input,
            assistant_response=assistant_response,
            importance=importance,
            emotional_valence=max(-1.0, min(1.0, emotional_valence)),
            emotional_arousal=max(-1.0, min(1.0, emotional_arousal)),
            concepts_mentioned=concepts_mentioned or [],
            summary=summary,
        )
        episode_id = await self._episodes.create(episode)

        # Star topology Hebbian learning
        active = self._memory.get_active_concepts(min_activation=0.3)
        if len(active) >= 2:  # noqa: PLR2004
            activations = dict(active)
            new_set = set(new_concept_ids or [])
            new_ids = [cid for cid, _ in active if cid in new_set]
            existing_ids = [cid for cid, _ in active if cid not in new_set]
            await self._hebbian.strengthen_star(
                new_ids,
                existing_ids,
                activations,
            )

        # Record metrics
        get_metrics().episodes_encoded.add(
            1,
            {"conversation_id": str(conversation_id)},
        )

        # Emit event
        await self._events.emit(
            EpisodeEncoded(
                episode_id=str(episode_id),
                conversation_id=str(conversation_id),
                importance=importance,
            )
        )

        logger.debug(
            "episode_encoded",
            episode_id=str(episode_id),
        )
        return episode_id

    async def strengthen_connection(
        self,
        concept_ids: list[ConceptId],
        *,
        relation_types: dict[tuple[str, str], str] | None = None,
    ) -> None:
        """Hebbian learning between co-activated concepts.

        Args:
            concept_ids: Concepts that co-occurred in the same turn.
            relation_types: Optional mapping of (concept_id_a, concept_id_b)
                to RelationType value string. Used for within-turn typed
                relations from LLM classification. Keys use canonical
                order (min, max) of string IDs.
        """
        await self._hebbian.strengthen(concept_ids, relation_types=relation_types)

    def decay_working_memory(self) -> None:
        """Apply decay to all concepts in working memory.

        Called after reflect phase to simulate natural forgetting.
        Concepts not re-activated will gradually lose activation,
        making room for newer, more relevant concepts in star topology.
        """
        self._memory.decay_all()

    # ── Internal ──

    def _track_access(self, concept_ids: list[ConceptId]) -> None:
        """Fire-and-forget access tracking with task lifecycle management.

        Uses a task set to prevent garbage collection of running tasks
        (replacing ``ensure_future`` which loses references).
        """
        for cid in concept_ids:
            task = asyncio.create_task(self._safe_record_access(cid))
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

    async def _safe_record_access(self, concept_id: ConceptId) -> None:
        """Record access with error swallowing."""
        try:
            await self._concepts.record_access(concept_id)
        except Exception:
            logger.warning(
                "access_tracking_failed",
                concept_id=str(concept_id),
                exc_info=True,
            )
