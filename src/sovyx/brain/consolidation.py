"""Memory consolidation cycle and scheduler (SPE-004 §consolidation)."""

from __future__ import annotations

import asyncio
import contextlib
import math
import random
import sqlite3
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sovyx.engine.events import ConsolidationCompleted
from sovyx.observability.logging import get_logger
from sovyx.observability.tasks import spawn

if TYPE_CHECKING:
    from sovyx.brain.concept_repo import ConceptRepository
    from sovyx.brain.learning import EbbinghausDecay
    from sovyx.brain.relation_repo import RelationRepository
    from sovyx.brain.scoring import ConfidenceScorer, ImportanceScorer, ScoreNormalizer
    from sovyx.brain.service import BrainService
    from sovyx.engine.events import EventBus
    from sovyx.engine.types import ConceptId, MindId

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
        importance_scorer: ImportanceScorer | None = None,
        confidence_scorer: ConfidenceScorer | None = None,
    ) -> None:
        self._brain = brain_service
        self._decay = decay
        self._events = event_bus
        self._concepts = concept_repo
        self._relations = relation_repo
        self._importance_scorer = importance_scorer
        self._confidence_scorer = confidence_scorer
        self._normalizer: ScoreNormalizer | None = None
        if importance_scorer or confidence_scorer:
            from sovyx.brain.scoring import ScoreNormalizer  # noqa: PLC0415

            self._normalizer = ScoreNormalizer()

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

        # Step 1.5: Recalculate importance + confidence scores
        recalculated = await self._recalculate_scores(mind_id)
        if recalculated > 0:
            logger.info(
                "consolidation_scores_recalculated",
                mind_id=str(mind_id),
                recalculated=recalculated,
            )

        # Step 1.6: Normalize if needed (anti-convergence)
        normalized = await self._normalize_scores(mind_id)
        if normalized > 0:
            logger.info(
                "consolidation_scores_normalized",
                mind_id=str(mind_id),
                normalized=normalized,
            )

        # Step 1.7: Score drift detection (Shannon entropy)
        await self._check_score_drift(mind_id)

        # Step 1.8: Refresh category centroid cache
        centroids_cached = await self._refresh_centroids(mind_id)
        if centroids_cached > 0:
            logger.info(
                "consolidation_centroids_refreshed",
                mind_id=str(mind_id),
                cached=centroids_cached,
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

        # Plan §7.3 — single tick record with normalized field names so
        # the dashboard can chart consolidation throughput across mind ids
        # without joining the four legacy "consolidation_*" lines above.
        # ``brain.episodes_processed`` carries the recalculated-concept
        # count: Sovyx consolidates concepts (not episodes), and the
        # recalc pass is the throughput-bound step worth tracking.
        logger.info(
            "brain.consolidation.tick",
            **{
                "brain.episodes_processed": recalculated,
                "brain.promoted": merged_count,
                "brain.decayed": decayed_concepts + decayed_relations,
                "brain.pruned": pruned_concepts + pruned_relations,
                "brain.normalized": normalized,
                "brain.centroids_cached": centroids_cached,
                "brain.duration_s": round(duration, 3),
            },
        )

        return event

    # Batch size for score recalculation (DB writes per batch)
    _SCORE_BATCH_SIZE = 500
    # Maximum time allowed for score recalculation (seconds)
    _SCORE_TIMEOUT_S = 30.0

    async def _recalculate_scores(self, mind_id: MindId) -> int:
        """Recalculate importance and confidence for all concepts.

        Uses graph degree centrality, access patterns, recency, and
        emotional weight to produce meaningful score spread.

        Processes concepts in batches of ``_SCORE_BATCH_SIZE`` and
        enforces a ``_SCORE_TIMEOUT_S`` total timeout. If the timeout
        fires, already-flushed batches are committed but remaining
        concepts are skipped with a warning.

        Returns:
            Number of concepts with updated scores.
        """
        if (
            not self._concepts
            or not self._relations
            or not self._importance_scorer
            or not self._confidence_scorer
        ):
            return 0

        # Get all concepts for this mind
        concepts = await self._concepts.get_by_mind(mind_id, limit=10000)
        if not concepts:
            return 0

        # Get degree centrality from graph
        centrality = await self._relations.get_degree_centrality(mind_id)

        # Compute max values for normalization
        max_degree = max((d for d, _ in centrality.values()), default=1)
        max_access = max((c.access_count for c in concepts), default=1)

        total_updated = 0
        now = datetime.now(UTC)
        deadline = time.monotonic() + self._SCORE_TIMEOUT_S
        batch: list[tuple[ConceptId, float, float]] = []

        for concept in concepts:
            # Timeout guard — flush what we have and stop.
            # ``>=`` (not strict ``>``) so ``_SCORE_TIMEOUT_S=0`` means
            # "process nothing, return immediately" — matches caller
            # intuition and the test fixture. With strict ``>`` a
            # 0-timeout degenerated on Windows' coarse monotonic clock
            # (``now == deadline`` within the same ~15.6 ms tick, so
            # the guard never tripped and the entire concept list was
            # processed).
            if time.monotonic() >= deadline:
                logger.warning(
                    "score_recalculation_timeout",
                    mind_id=str(mind_id),
                    processed=total_updated + len(batch),
                    total=len(concepts),
                    timeout_s=self._SCORE_TIMEOUT_S,
                )
                break

            cid_str = str(concept.id)
            degree, avg_weight = centrality.get(cid_str, (0, 0.0))

            # Days since last access
            ref_time = concept.last_accessed or concept.created_at
            days = (now - ref_time).total_seconds() / 86400 if ref_time else 30.0

            # Recalculate importance — 3-axis PAD signal per ADR-001.
            new_importance = self._importance_scorer.recalculate(
                current_importance=concept.importance,
                access_count=concept.access_count,
                degree=degree,
                avg_weight=avg_weight,
                max_degree=max_degree,
                emotional_valence=concept.emotional_valence,
                emotional_arousal=concept.emotional_arousal,
                emotional_dominance=concept.emotional_dominance,
                days_since_access=days,
                max_access=max_access,
            )

            # Recalculate confidence (staleness decay)
            new_confidence = self._confidence_scorer.score_staleness_decay(
                current=concept.confidence,
                days_since_access=days,
            )

            # Only update if changed meaningfully
            if (
                abs(new_importance - concept.importance) > 0.005
                or abs(new_confidence - concept.confidence) > 0.005
            ):
                batch.append((concept.id, new_importance, new_confidence))

            # Flush batch when full
            if len(batch) >= self._SCORE_BATCH_SIZE:
                await self._concepts.batch_update_scores(batch)
                total_updated += len(batch)
                batch = []

        # Flush remaining
        if batch:
            await self._concepts.batch_update_scores(batch)
            total_updated += len(batch)

        logger.info(
            "scores_recalculated",
            mind_id=str(mind_id),
            total=len(concepts),
            updated=total_updated,
        )
        return total_updated

    async def _normalize_scores(self, mind_id: MindId) -> int:
        """Normalize importance scores if spread is too narrow.

        Prevents all concepts from converging to the same importance value
        over many consolidation cycles. Only activates when spread < 0.20.

        Preserves relative ordering and never pushes below floor (0.05).
        Writes in batches of ``_SCORE_BATCH_SIZE``.

        Returns:
            Number of concepts with adjusted scores.
        """
        if not self._concepts or not self._normalizer:
            return 0

        concepts = await self._concepts.get_by_mind(mind_id, limit=10000)
        if len(concepts) < 3:  # noqa: PLR2004
            return 0

        # Build importance score list
        scores = [(str(c.id), c.importance) for c in concepts]
        normalized = self._normalizer.normalize(scores)

        # Check if normalization actually changed anything
        original_map = dict(scores)
        concept_map = {str(c.id): c for c in concepts}
        batch: list[tuple[ConceptId, float, float]] = []
        total_updated = 0

        for cid_str, new_imp in normalized:
            old_imp = original_map.get(cid_str, 0.5)
            if abs(new_imp - old_imp) > 0.005:
                concept = concept_map[cid_str]
                batch.append((concept.id, new_imp, concept.confidence))

            if len(batch) >= self._SCORE_BATCH_SIZE:
                await self._concepts.batch_update_scores(batch)
                total_updated += len(batch)
                batch = []

        if batch:
            await self._concepts.batch_update_scores(batch)
            total_updated += len(batch)

        return total_updated

    # Entropy thresholds for score distribution health
    _ENTROPY_WARNING = 1.5  # Low entropy → scores concentrating
    _ENTROPY_CRITICAL = 1.0  # Very low → distribution collapsed

    async def _check_score_drift(self, mind_id: MindId) -> None:
        """Check Shannon entropy of importance score distribution.

        Healthy distributions have high entropy (scores spread out).
        Low entropy means scores converged to similar values, reducing
        the discriminative power of importance-weighted retrieval.

        Thresholds:
        - entropy < 1.0: CRITICAL — distribution collapsed, emit alert
        - entropy < 1.5: WARNING — scores concentrating, log warning
        - entropy >= 1.5: healthy, no-op
        """
        if not self._concepts:
            return

        concepts = await self._concepts.get_by_mind(mind_id, limit=10000)
        if len(concepts) < 5:  # noqa: PLR2004
            return  # Too few concepts for meaningful entropy

        importances = [c.importance for c in concepts]
        confidences = [c.confidence for c in concepts]

        imp_entropy = self._shannon_entropy(importances)
        conf_entropy = self._shannon_entropy(confidences)

        logger.debug(
            "score_drift_check",
            mind_id=str(mind_id),
            importance_entropy=round(imp_entropy, 3),
            confidence_entropy=round(conf_entropy, 3),
            concepts=len(concepts),
        )

        if imp_entropy < self._ENTROPY_CRITICAL:
            logger.warning(
                "score_drift_critical",
                mind_id=str(mind_id),
                importance_entropy=round(imp_entropy, 3),
                level="CRITICAL",
            )
        elif imp_entropy < self._ENTROPY_WARNING:
            logger.warning(
                "score_drift_warning",
                mind_id=str(mind_id),
                importance_entropy=round(imp_entropy, 3),
                level="WARNING",
            )

    @staticmethod
    def _shannon_entropy(values: list[float], bins: int = 20) -> float:
        """Compute Shannon entropy of a score distribution.

        Bins continuous [0, 1] scores into a histogram and computes
        entropy. Higher entropy = more spread. Maximum for 20 bins
        is log2(20) ≈ 4.32.

        Args:
            values: List of scores in [0, 1].
            bins: Number of histogram bins.

        Returns:
            Shannon entropy in bits. Returns 0.0 for empty/single-value.
        """
        if len(values) < 2:  # noqa: PLR2004
            return 0.0

        # Build histogram
        counts = [0] * bins
        for v in values:
            idx = min(int(v * bins), bins - 1)
            counts[idx] += 1

        n = len(values)
        entropy = 0.0
        for count in counts:
            if count > 0:
                p = count / n
                entropy -= p * math.log2(p)

        return entropy

    async def _refresh_centroids(self, mind_id: MindId) -> int:
        """Refresh category centroid cache on BrainService.

        Called after score recalculation so that novelty detection
        uses up-to-date cluster centers. No-op if brain service
        doesn't support centroid caching.

        Returns:
            Number of categories cached.
        """
        try:
            result = await self._brain.refresh_centroid_cache(mind_id)
            return int(result) if isinstance(result, (int, float)) else 0
        except (AttributeError, TypeError):
            # Shape drift in the brain API — fall back silently.
            return 0
        except (sqlite3.Error, ValueError):
            # sqlite3.Error: DB trouble during centroid recomputation.
            # ValueError: bad numeric embedding data. Either way, skip
            # this cycle's cache refresh; next cycle will retry. Log
            # with traceback so repeated failures surface as a real bug.
            logger.debug("centroid_cache_refresh_error", exc_info=True)
            return 0

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
                # Weighted average across all three PAD axes (ADR-001).
                # Each axis is merged independently so a high-dominance
                # survivor merging with a low-dominance candidate lands
                # at a combined dominance that reflects their relative
                # access counts — same pattern valence already used
                # pre-migration 006.
                total_access = survivor.access_count + to_merge.access_count
                if total_access > 0:
                    s_n = survivor.access_count
                    m_n = to_merge.access_count
                    survivor.emotional_valence = (
                        survivor.emotional_valence * s_n + to_merge.emotional_valence * m_n
                    ) / total_access
                    survivor.emotional_arousal = (
                        survivor.emotional_arousal * s_n + to_merge.emotional_arousal * m_n
                    ) / total_access
                    survivor.emotional_dominance = (
                        survivor.emotional_dominance * s_n + to_merge.emotional_dominance * m_n
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
            except (sqlite3.Error, ValueError, AttributeError):
                # Per-pair resilience: a single merge failure must not
                # abort the whole consolidation cycle. sqlite3.Error
                # covers update/transfer/delete DB errors; ValueError
                # and AttributeError catch degenerate concept state
                # (missing IDs, stale references). Programmer errors
                # (TypeError, NameError) still bubble up.
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
        self._task = spawn(self._loop(mind_id), name="consolidation-scheduler")
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
            except Exception:  # noqa: BLE001
                logger.exception("consolidation_cycle_failed", mind_id=str(mind_id))
                # Continue loop — don't crash scheduler on single failure
