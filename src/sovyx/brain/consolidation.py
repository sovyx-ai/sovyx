"""Memory consolidation cycle and scheduler (SPE-004 §consolidation)."""

from __future__ import annotations

import asyncio
import contextlib
import random
import time
from typing import TYPE_CHECKING

from sovyx.engine.events import ConsolidationCompleted
from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from sovyx.brain.concept_repo import ConceptRepository
    from sovyx.brain.learning import EbbinghausDecay
    from sovyx.brain.relation_repo import RelationRepository
    from sovyx.brain.service import BrainService
    from sovyx.engine.events import EventBus
    from sovyx.engine.types import MindId

logger = get_logger(__name__)


class ConsolidationCycle:
    """Run periodic memory maintenance.

    Steps:
        1. Ebbinghaus decay on all concepts/relations
        2. Merge similar concepts (FTS5 + Levenshtein)
        3. Prune weak concepts/relations (below threshold)
        4. Log consolidation metrics
        5. Emit ConsolidationCompleted event
    """

    def __init__(
        self,
        brain_service: BrainService,
        decay: EbbinghausDecay,
        event_bus: EventBus,
        concept_repo: ConceptRepository | None = None,
        relation_repo: RelationRepository | None = None,
    ) -> None:
        self._brain = brain_service
        self._decay = decay
        self._events = event_bus
        self._concepts = concept_repo
        self._relations = relation_repo

    async def run(self, mind_id: MindId) -> ConsolidationCompleted:
        """Execute one consolidation cycle.

        Args:
            mind_id: The mind to consolidate.

        Returns:
            ConsolidationCompleted event with metrics.
        """
        start = time.monotonic()

        # Step 1: Apply decay
        decayed_concepts, decayed_relations = await self._decay.apply_decay(mind_id)
        logger.info(
            "consolidation_decay_applied",
            mind_id=str(mind_id),
            decayed_concepts=decayed_concepts,
            decayed_relations=decayed_relations,
        )

        # Step 2: Merge similar concepts
        merged_count = await self._merge_similar(mind_id)
        logger.info(
            "consolidation_merge_complete",
            mind_id=str(mind_id),
            merged=merged_count,
        )

        # Step 3: Prune weak
        pruned_concepts, pruned_relations = await self._decay.prune_weak(mind_id)
        logger.info(
            "consolidation_prune_complete",
            mind_id=str(mind_id),
            pruned_concepts=pruned_concepts,
            pruned_relations=pruned_relations,
        )

        duration = time.monotonic() - start

        # Step 4: Emit event
        event = ConsolidationCompleted(
            merged=merged_count,
            pruned=pruned_concepts + pruned_relations,
            strengthened=decayed_concepts + decayed_relations,
            duration_s=round(duration, 3),
        )
        await self._events.emit(event)

        logger.info(
            "consolidation_cycle_complete",
            mind_id=str(mind_id),
            pruned=event.pruned,
            strengthened=event.strengthened,
            duration_s=event.duration_s,
        )

        return event

    async def _merge_similar(self, mind_id: MindId) -> int:
        """Merge similar concepts found by FTS5 + name similarity.

        Strategy for each (survivor, to_merge) pair:
        - Keep survivor (higher importance)
        - Merge content: keep longer
        - Sum access_counts
        - Max confidence
        - Transfer all relations from to_merge → survivor
        - Delete to_merge + its embedding

        Returns:
            Number of concepts merged (removed).
        """
        if self._concepts is None or self._relations is None:
            return 0

        pairs = await self._concepts.find_merge_candidates(mind_id)
        if not pairs:
            return 0

        merged = 0
        for survivor, to_merge in pairs:
            try:
                # Merge attributes
                if len(to_merge.content) > len(survivor.content):
                    survivor.content = to_merge.content
                survivor.access_count += to_merge.access_count
                survivor.confidence = max(survivor.confidence, to_merge.confidence)
                # Weighted average valence
                total_access = survivor.access_count + to_merge.access_count
                if total_access > 0:
                    survivor.emotional_valence = (
                        survivor.emotional_valence * survivor.access_count
                        + to_merge.emotional_valence * to_merge.access_count
                    ) / total_access

                await self._concepts.update(survivor)

                # Transfer relations
                await self._relations.transfer_relations(to_merge.id, survivor.id)

                # Delete merged concept
                await self._concepts.delete(to_merge.id)
                merged += 1

                logger.debug(
                    "concept_merged",
                    survivor=survivor.name,
                    merged=to_merge.name,
                    survivor_id=str(survivor.id),
                )
            except Exception:
                logger.warning(
                    "concept_merge_failed",
                    survivor=survivor.name,
                    to_merge=to_merge.name,
                    exc_info=True,
                )

        return merged


class ConsolidationScheduler:
    """Schedule ConsolidationCycle to run periodically.

    Uses asyncio background task with sleep loop.
    Graceful stop: cancels task on shutdown.
    """

    def __init__(
        self,
        cycle: ConsolidationCycle,
        interval_hours: int = 6,
    ) -> None:
        self._cycle = cycle
        self._interval_s = interval_hours * 3600
        self._task: asyncio.Task[None] | None = None
        self._running = False

    async def start(self, mind_id: MindId) -> None:
        """Start background consolidation loop.

        Args:
            mind_id: Mind to consolidate periodically.
        """
        if self._task is not None:
            return

        self._running = True
        self._task = asyncio.create_task(self._loop(mind_id))
        logger.info(
            "consolidation_scheduler_started",
            mind_id=str(mind_id),
            interval_hours=self._interval_s // 3600,
        )

    async def stop(self) -> None:
        """Stop background consolidation loop."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        logger.info("consolidation_scheduler_stopped")

    async def _loop(self, mind_id: MindId) -> None:
        """Internal loop: sleep → consolidate → repeat."""
        while self._running:
            try:
                # ±20% jitter to prevent thundering herd in multi-instance
                jitter = random.uniform(0.8, 1.2)  # nosec B311 — non-crypto jitter
                await asyncio.sleep(self._interval_s * jitter)
                await self._cycle.run(mind_id)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("consolidation_cycle_failed", mind_id=str(mind_id))
                # Continue loop — don't crash scheduler on single failure
