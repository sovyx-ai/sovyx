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
    from sovyx.llm.router import LLMRouter

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
        llm_router: LLMRouter | None = None,
        fast_model: str = "",
    ) -> None:
        self._concepts = concept_repo
        self._episodes = episode_repo
        self._relations = relation_repo
        self._embedding = embedding_engine
        self._spreading = spreading
        self._hebbian = hebbian
        self._decay = decay
        self._retrieval = retrieval
        self._llm_router = llm_router
        self._fast_model = fast_model
        self._memory = working_memory
        self._events = event_bus
        self._mind_id: MindId | None = None
        self._background_tasks: set[asyncio.Task[None]] = set()

    async def start(self, mind_id: MindId) -> None:
        """Load top-50 recent concepts into working memory."""
        self._mind_id = mind_id
        recent = await self._concepts.get_recent(mind_id, limit=50)
        for concept in recent:
            self._memory.activate(concept.id, concept.importance, importance=concept.importance)
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

            # Re-score with spreading activation + quality factor
            rescored: list[tuple[Concept, float]] = []
            for concept, rrf_score in results:
                spread_score = spread_map.get(str(concept.id), 0.0)
                quality = 0.60 * concept.importance + 0.40 * concept.confidence
                combined = rrf_score + spread_score * 0.1 + quality * 0.05
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
        importance: float | None = None,
        confidence: float | None = None,
        emotional_valence: float = 0.0,
        **kwargs: object,
    ) -> ConceptId:
        """Learn a new concept with dedup check (v13 audit fix).

        If a concept with the same name+category exists, reinforce it
        instead of creating a duplicate.

        Args:
            importance: Initial importance score [0.0, 1.0]. If None,
                uses model default (0.5). Callers should pass category-based
                or LLM-assessed importance for meaningful differentiation.
            confidence: Initial confidence score [0.0, 1.0]. If None,
                uses model default (0.5). Callers should pass source-quality
                based confidence for meaningful differentiation.
            emotional_valence: Sentiment score (-1.0 to 1.0) for this
                concept. On dedup, uses weighted average with existing.
        """
        # Dedup check via FTS5
        existing = await self._concepts.search_by_text(name, mind_id, limit=3)
        for concept, _rank in existing:
            if concept.name.lower() == name.lower() and concept.category == category:
                # Concept exists — reinforce, don't duplicate

                # Semantic relationship detection via LLM (or heuristic fallback)
                from sovyx.brain.contradiction import (  # noqa: PLC0415
                    ContentRelation,
                    detect_contradiction,
                )

                relation = await detect_contradiction(
                    old_content=concept.content,
                    new_content=content,
                    llm_router=self._llm_router,
                    fast_model=self._fast_model,
                )

                # Apply cascade based on classification
                if relation == ContentRelation.CONTRADICTS:
                    # Contradiction: reduce confidence, override content
                    from sovyx.brain.scoring import ConfidenceScorer  # noqa: PLC0415

                    old_conf = concept.confidence
                    scorer = ConfidenceScorer()
                    concept.confidence = scorer.score_contradiction(concept.confidence)
                    concept.content = content  # Recency wins
                    concept.metadata["last_contradiction"] = True
                    logger.info(
                        "concept_contradiction_detected",
                        concept_id=str(concept.id),
                        old_confidence=old_conf,
                        new_confidence=concept.confidence,
                        relation="CONTRADICTS",
                    )

                elif relation == ContentRelation.EXTENDS:
                    # Extension: update content + corroboration boost
                    concept.content = content
                    corr_raw = concept.metadata.get("corroboration_count", 0)
                    corr = int(corr_raw) if isinstance(corr_raw, (int, float, str)) else 0
                    corr += 1
                    concept.metadata["corroboration_count"] = corr
                    confidence_boost = 0.08 * (1.0 - concept.confidence)
                    concept.confidence = min(1.0, concept.confidence + confidence_boost + 0.03)

                elif relation == ContentRelation.SAME:
                    # Corroboration: same info repeated → confidence boost
                    corr_raw = concept.metadata.get("corroboration_count", 0)
                    corr = int(corr_raw) if isinstance(corr_raw, (int, float, str)) else 0
                    corr += 1
                    concept.metadata["corroboration_count"] = corr
                    confidence_boost = 0.08 * (1.0 - concept.confidence)
                    concept.confidence = min(1.0, concept.confidence + confidence_boost)

                else:
                    # UNRELATED: shouldn't happen on same-name dedup, log and skip
                    logger.warning(
                        "dedup_unrelated_content",
                        concept_id=str(concept.id),
                        relation="UNRELATED",
                    )

                # Importance reinforcement: factor in the incoming importance
                # signal if it's higher than current. Weighted reinforcement
                # replaces flat +0.05 for more nuanced evolution.
                if importance is not None and importance > concept.importance:
                    # Incoming signal is stronger — pull importance toward it
                    boost = 0.03 * (importance - concept.importance)
                    concept.importance = min(1.0, concept.importance + boost + 0.02)
                else:
                    # Standard reinforcement: smaller than before (0.02 vs 0.05)
                    # to avoid inflating everything equally
                    concept.importance = min(1.0, concept.importance + 0.02)

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
                # Re-activate in working memory with actual importance
                # (not flat 0.5) so concept visibility reflects true importance.
                self._memory.activate(
                    concept.id, concept.importance, importance=concept.importance,
                )
                return concept.id

        # New concept — use provided importance/confidence or defaults
        from sovyx.brain.models import Concept

        effective_importance = max(0.0, min(1.0, importance)) if importance is not None else 0.5
        effective_confidence = max(0.0, min(1.0, confidence)) if confidence is not None else 0.5

        concept = Concept(
            mind_id=mind_id,
            name=name,
            content=content,
            category=category,
            source=source,
            importance=effective_importance,
            confidence=effective_confidence,
            emotional_valence=max(-1.0, min(1.0, emotional_valence)),
        )
        concept_id = await self._concepts.create(concept)

        # Activate in working memory with actual importance
        self._memory.activate(concept_id, concept.importance, importance=concept.importance)

        # Record metrics
        get_metrics().concepts_created.add(1, {"source": source})

        # Emit event
        await self._events.emit(
            ConceptCreated(
                concept_id=str(concept_id),
                title=name,
                source=source,
                importance=concept.importance,
                confidence=concept.confidence,
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

    # ── Novelty Detection ───────────────────────────────────────────

    # Cold start threshold: below this, embedding-based novelty isn't reliable
    _COLD_START_THRESHOLD = 10
    # Cold start default: moderate-high novelty (not 1.0 to avoid over-inflation)
    _COLD_START_NOVELTY = 0.70

    async def compute_novelty(
        self,
        text: str,
        category: str,
        mind_id: MindId,
    ) -> float:
        """Compute semantic novelty of a concept against existing knowledge.

        Strategy (3-tier with graceful degradation):

        1. **Embedding cosine distance** (preferred): encode text, compare
           against category centroid. High distance = high novelty.
        2. **FTS5 text search** (fallback): if embeddings unavailable,
           use text search similarity as proxy.
        3. **Cold start prior** (0.70): if category has < 10 concepts,
           embeddings are unreliable — return moderate-high novelty.

        Novelty scale:
        - 1.0: completely unprecedented topic
        - 0.70: cold start / insufficient data
        - 0.50: moderately novel
        - 0.05: near-duplicate of existing knowledge

        Args:
            text: Concept name + content to assess.
            category: ConceptCategory value for scoped comparison.
            mind_id: Mind to compare against.

        Returns:
            Novelty score in [0.05, 1.0].
        """
        # Check category population for cold start
        try:
            count = await self._concepts.count_by_category(mind_id, category)
        except Exception:
            return self._COLD_START_NOVELTY

        if count < self._COLD_START_THRESHOLD:
            return self._COLD_START_NOVELTY

        # Tier 1: Embedding-based novelty
        if self._embedding.has_embeddings:
            try:
                return await self._compute_novelty_embedding(
                    text, category, mind_id,
                )
            except Exception:
                logger.debug("embedding_novelty_failed_falling_back_to_fts5")

        # Tier 2: FTS5-based novelty (existing approach)
        return await self._compute_novelty_fts5(text, mind_id)

    async def _compute_novelty_embedding(
        self,
        text: str,
        category: str,
        mind_id: MindId,
    ) -> float:
        """Compute novelty via embedding cosine distance from category centroid.

        Encodes the new concept text, fetches existing embeddings in the
        same category, computes the centroid, and measures cosine distance.

        High cosine similarity to centroid = low novelty (concept is
        "in the neighborhood" of known knowledge).
        Low similarity = high novelty (concept is far from the cluster).

        The mapping from similarity to novelty uses a calibrated curve:
        - similarity >= 0.85 → novelty 0.05 (near-duplicate)
        - similarity ~0.60 → novelty 0.50 (moderately novel)
        - similarity <= 0.30 → novelty 0.95 (very novel)
        """
        from sovyx.brain.embedding import EmbeddingEngine

        # Encode the new concept
        new_embedding = await self._embedding.encode(text, is_query=True)

        # Get category embeddings for centroid
        category_embeddings = await self._concepts.get_embeddings_by_category(
            mind_id, category, limit=500,
        )

        if not category_embeddings:
            return self._COLD_START_NOVELTY

        # Compute centroid
        centroid = await self._embedding.compute_category_centroid(
            category_embeddings,
        )

        # Cosine similarity (both vectors are L2-normalized)
        similarity = EmbeddingEngine.cosine_similarity(new_embedding, centroid)

        # Map similarity → novelty with calibrated piecewise linear curve:
        # sim >= 0.85 → novelty 0.05
        # sim in [0.30, 0.85] → linear from 0.95 to 0.05
        # sim <= 0.30 → novelty 0.95
        if similarity >= 0.85:  # noqa: PLR2004
            return 0.05
        if similarity <= 0.30:  # noqa: PLR2004
            return 0.95
        # Linear interpolation: (0.30, 0.95) → (0.85, 0.05)
        t = (similarity - 0.30) / (0.85 - 0.30)  # 0.0 → 1.0
        novelty = 0.95 - t * 0.90  # 0.95 → 0.05
        return max(0.05, min(1.0, novelty))

    async def _compute_novelty_fts5(
        self,
        text: str,
        mind_id: MindId,
    ) -> float:
        """Compute novelty via FTS5 text search (fallback).

        Uses the existing search() pipeline. High match score = low novelty.
        Less precise than embeddings but always available.
        """
        try:
            matches = await self.search(text, mind_id, limit=3)
        except Exception:
            return self._COLD_START_NOVELTY

        if not matches:
            return 1.0

        best_concept, best_score = matches[0]
        # Exact name match = very low novelty
        if best_concept.name.lower() == text.lower():
            return 0.05
        # Convert search score to novelty (inverse relationship)
        novelty = max(0.05, 1.0 - min(1.0, best_score * 1.5))
        return novelty
