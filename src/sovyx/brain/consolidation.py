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
    from sovyx.brain.learning import EbbinghausDecay
    from sovyx.brain.service import BrainService
    from sovyx.engine.events import EventBus
    from sovyx.engine.types import MindId

logger = get_logger(__name__)


class ConsolidationCycle:
    """Run periodic memory maintenance.

    Steps:
        1. Ebbinghaus decay on all concepts/relations
        2. Prune weak concepts/relations (below threshold)
        3. Log consolidation metrics
        4. Emit ConsolidationCompleted event
    """

    def __init__(
        self,
        brain_service: BrainService,
        decay: EbbinghausDecay,
        event_bus: EventBus,
    ) -> None:
        self._brain = brain_service
        self._decay = decay
        self._events = event_bus

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

        # Step 2: Prune weak
        pruned_concepts, pruned_relations = await self._decay.prune_weak(mind_id)
        logger.info(
            "consolidation_prune_complete",
            mind_id=str(mind_id),
            pruned_concepts=pruned_concepts,
            pruned_relations=pruned_relations,
        )

        duration = time.monotonic() - start

        # Step 3: Emit event
        event = ConsolidationCompleted(
            merged=0,  # v0.1: merge deferred (needs sqlite-vec KNN)
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
