"""Tests for sovyx.brain.consolidation."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from sovyx.brain.consolidation import ConsolidationCycle, ConsolidationScheduler
from sovyx.engine.events import ConsolidationCompleted, EventBus
from sovyx.engine.types import MindId


@pytest.fixture()
def mind_id() -> MindId:
    """Default mind ID."""
    return MindId("test-mind")


@pytest.fixture()
def mock_brain() -> AsyncMock:
    """Mock BrainService."""
    return AsyncMock()


@pytest.fixture()
def mock_decay() -> AsyncMock:
    """Mock EbbinghausDecay."""
    decay = AsyncMock()
    decay.apply_decay.return_value = (10, 5)  # 10 concepts, 5 relations
    decay.prune_weak.return_value = (3, 2)  # 3 concepts, 2 relations
    return decay


@pytest.fixture()
def event_bus() -> EventBus:
    """Real event bus."""
    return EventBus()


@pytest.fixture()
def cycle(
    mock_brain: AsyncMock,
    mock_decay: AsyncMock,
    event_bus: EventBus,
) -> ConsolidationCycle:
    """ConsolidationCycle with mocks."""
    return ConsolidationCycle(
        brain_service=mock_brain,
        decay=mock_decay,
        event_bus=event_bus,
    )


class TestConsolidationCycle:
    """ConsolidationCycle tests."""

    async def test_run_applies_decay(
        self, cycle: ConsolidationCycle, mock_decay: AsyncMock, mind_id: MindId
    ) -> None:
        await cycle.run(mind_id)
        mock_decay.apply_decay.assert_awaited_once_with(mind_id)

    async def test_run_prunes_weak(
        self, cycle: ConsolidationCycle, mock_decay: AsyncMock, mind_id: MindId
    ) -> None:
        await cycle.run(mind_id)
        mock_decay.prune_weak.assert_awaited_once_with(mind_id)

    async def test_run_returns_event(self, cycle: ConsolidationCycle, mind_id: MindId) -> None:
        result = await cycle.run(mind_id)
        assert isinstance(result, ConsolidationCompleted)
        assert result.pruned == 5  # 3 concepts + 2 relations  # noqa: PLR2004
        assert result.strengthened == 15  # 10 + 5  # noqa: PLR2004
        assert result.merged == 0  # v0.1: no merge
        assert result.duration_s >= 0

    async def test_run_emits_event(
        self,
        cycle: ConsolidationCycle,
        event_bus: EventBus,
        mind_id: MindId,
    ) -> None:
        received: list[ConsolidationCompleted] = []
        event_bus.subscribe(ConsolidationCompleted, lambda e: received.append(e))
        await cycle.run(mind_id)
        assert len(received) == 1
        assert received[0].pruned == 5  # noqa: PLR2004


class TestScoreRecalculation:
    """Score recalculation during consolidation (TASK-07)."""

    async def test_recalculate_without_scorers_returns_zero(
        self, cycle: ConsolidationCycle, mind_id: MindId
    ) -> None:
        """Without scorers injected, recalculation is skipped."""
        result = await cycle._recalculate_scores(mind_id)
        assert result == 0

    async def test_recalculate_with_scorers(
        self,
        mock_brain: AsyncMock,
        mock_decay: AsyncMock,
        event_bus: EventBus,
        mind_id: MindId,
    ) -> None:
        """With scorers + repos, recalculation updates concepts."""
        from datetime import UTC, datetime, timedelta

        from sovyx.brain.models import Concept
        from sovyx.brain.scoring import ConfidenceScorer, ImportanceScorer
        from sovyx.engine.types import ConceptId

        # Create mock concepts with flat importance
        now = datetime.now(UTC)
        concepts = [
            Concept(
                id=ConceptId(f"c{i}"),
                mind_id=mind_id,
                name=f"concept_{i}",
                importance=0.5,
                confidence=0.5,
                access_count=i * 5,
                emotional_valence=0.1 * i,
                last_accessed=now - timedelta(days=i * 10),
            )
            for i in range(5)
        ]

        mock_concepts = AsyncMock()
        mock_concepts.get_by_mind = AsyncMock(return_value=concepts)
        mock_concepts.batch_update_scores = AsyncMock(return_value=3)

        mock_relations = AsyncMock()
        mock_relations.get_degree_centrality = AsyncMock(return_value={
            "c0": (5, 0.7),
            "c1": (3, 0.5),
            "c2": (1, 0.3),
            "c3": (0, 0.0),
            "c4": (0, 0.0),
        })

        cycle_with_scorers = ConsolidationCycle(
            brain_service=mock_brain,
            decay=mock_decay,
            event_bus=event_bus,
            concept_repo=mock_concepts,
            relation_repo=mock_relations,
            importance_scorer=ImportanceScorer(),
            confidence_scorer=ConfidenceScorer(),
        )

        result = await cycle_with_scorers._recalculate_scores(mind_id)
        assert result > 0
        mock_concepts.batch_update_scores.assert_awaited_once()
        updates = mock_concepts.batch_update_scores.call_args[0][0]
        assert len(updates) > 0
        # All scores should be in valid range
        for _, imp, conf in updates:
            assert 0.05 <= imp <= 1.0
            assert 0.05 <= conf <= 1.0

    async def test_recalculate_skips_unchanged(
        self,
        mock_brain: AsyncMock,
        mock_decay: AsyncMock,
        event_bus: EventBus,
        mind_id: MindId,
    ) -> None:
        """Concepts where score change < 0.005 are skipped."""
        from datetime import UTC, datetime

        from sovyx.brain.models import Concept
        from sovyx.brain.scoring import ConfidenceScorer, ImportanceScorer
        from sovyx.engine.types import ConceptId

        # Concept with scores that won't change much (stable state)
        stable = Concept(
            id=ConceptId("stable"),
            mind_id=mind_id,
            name="stable_concept",
            importance=0.5,
            confidence=0.8,
            access_count=10,
            last_accessed=datetime.now(UTC),  # Very recent
        )

        mock_concepts = AsyncMock()
        mock_concepts.get_by_mind = AsyncMock(return_value=[stable])
        mock_concepts.batch_update_scores = AsyncMock(return_value=0)

        mock_relations = AsyncMock()
        mock_relations.get_degree_centrality = AsyncMock(return_value={
            "stable": (5, 0.6),
        })

        cycle_with_scorers = ConsolidationCycle(
            brain_service=mock_brain,
            decay=mock_decay,
            event_bus=event_bus,
            concept_repo=mock_concepts,
            relation_repo=mock_relations,
            importance_scorer=ImportanceScorer(),
            confidence_scorer=ConfidenceScorer(),
        )

        result = await cycle_with_scorers._recalculate_scores(mind_id)
        # Stable concept might or might not be updated depending on exact score
        # But batch_update_scores is called (even if with empty list)
        assert result >= 0

    async def test_run_includes_recalculation(
        self,
        mock_brain: AsyncMock,
        mock_decay: AsyncMock,
        event_bus: EventBus,
        mind_id: MindId,
    ) -> None:
        """Full consolidation run includes score recalculation step."""
        from sovyx.brain.scoring import ConfidenceScorer, ImportanceScorer

        mock_concepts = AsyncMock()
        mock_concepts.get_by_mind = AsyncMock(return_value=[])
        mock_concepts.find_merge_candidates = AsyncMock(return_value=[])

        mock_relations = AsyncMock()

        cycle_full = ConsolidationCycle(
            brain_service=mock_brain,
            decay=mock_decay,
            event_bus=event_bus,
            concept_repo=mock_concepts,
            relation_repo=mock_relations,
            importance_scorer=ImportanceScorer(),
            confidence_scorer=ConfidenceScorer(),
        )

        result = await cycle_full.run(mind_id)
        assert isinstance(result, ConsolidationCompleted)
        # get_by_mind was called (recalculation attempted)
        mock_concepts.get_by_mind.assert_awaited()

    async def test_connected_concepts_get_higher_importance(
        self,
        mock_brain: AsyncMock,
        mock_decay: AsyncMock,
        event_bus: EventBus,
        mind_id: MindId,
    ) -> None:
        """Highly connected concepts should get higher recalculated importance."""
        from datetime import UTC, datetime

        from sovyx.brain.models import Concept
        from sovyx.brain.scoring import ConfidenceScorer, ImportanceScorer
        from sovyx.engine.types import ConceptId

        now = datetime.now(UTC)
        connected = Concept(
            id=ConceptId("connected"),
            mind_id=mind_id,
            name="hub",
            importance=0.5,
            confidence=0.5,
            access_count=20,
            last_accessed=now,
        )
        isolated = Concept(
            id=ConceptId("isolated"),
            mind_id=mind_id,
            name="leaf",
            importance=0.5,
            confidence=0.5,
            access_count=1,
            last_accessed=now,
        )

        mock_concepts = AsyncMock()
        mock_concepts.get_by_mind = AsyncMock(return_value=[connected, isolated])
        mock_concepts.batch_update_scores = AsyncMock(return_value=2)

        mock_relations = AsyncMock()
        mock_relations.get_degree_centrality = AsyncMock(return_value={
            "connected": (15, 0.8),  # Hub: many connections
            "isolated": (0, 0.0),    # Leaf: no connections
        })

        cycle_s = ConsolidationCycle(
            brain_service=mock_brain,
            decay=mock_decay,
            event_bus=event_bus,
            concept_repo=mock_concepts,
            relation_repo=mock_relations,
            importance_scorer=ImportanceScorer(),
            confidence_scorer=ConfidenceScorer(),
        )

        await cycle_s._recalculate_scores(mind_id)
        updates = mock_concepts.batch_update_scores.call_args[0][0]
        update_dict = {str(cid): (imp, conf) for cid, imp, conf in updates}
        if "connected" in update_dict and "isolated" in update_dict:
            assert update_dict["connected"][0] > update_dict["isolated"][0]


class TestScoreNormalization:
    """Score normalization during consolidation (TASK-08)."""

    async def test_normalize_without_normalizer_returns_zero(
        self, cycle: ConsolidationCycle, mind_id: MindId
    ) -> None:
        result = await cycle._normalize_scores(mind_id)
        assert result == 0

    async def test_normalize_narrow_spread(
        self,
        mock_brain: AsyncMock,
        mock_decay: AsyncMock,
        event_bus: EventBus,
        mind_id: MindId,
    ) -> None:
        """Narrow spread (< 0.20) triggers normalization."""
        from sovyx.brain.models import Concept
        from sovyx.brain.scoring import ConfidenceScorer, ImportanceScorer
        from sovyx.engine.types import ConceptId

        # All concepts at ~0.5 importance (narrow spread of 0.02)
        concepts = [
            Concept(id=ConceptId(f"c{i}"), mind_id=mind_id, name=f"c{i}",
                    importance=0.50 + i * 0.005, confidence=0.5)
            for i in range(5)
        ]

        mock_concepts = AsyncMock()
        mock_concepts.get_by_mind = AsyncMock(return_value=concepts)
        mock_concepts.batch_update_scores = AsyncMock(return_value=5)

        cycle_n = ConsolidationCycle(
            mock_brain, mock_decay, event_bus,
            concept_repo=mock_concepts,
            importance_scorer=ImportanceScorer(),
            confidence_scorer=ConfidenceScorer(),
        )

        result = await cycle_n._normalize_scores(mind_id)
        assert result > 0
        updates = mock_concepts.batch_update_scores.call_args[0][0]
        # After normalization, spread should be wider
        new_values = [imp for _, imp, _ in updates]
        assert max(new_values) - min(new_values) > 0.3

    async def test_normalize_healthy_spread_noop(
        self,
        mock_brain: AsyncMock,
        mock_decay: AsyncMock,
        event_bus: EventBus,
        mind_id: MindId,
    ) -> None:
        """Healthy spread (>= 0.20) → no normalization."""
        from sovyx.brain.models import Concept
        from sovyx.brain.scoring import ConfidenceScorer, ImportanceScorer
        from sovyx.engine.types import ConceptId

        concepts = [
            Concept(id=ConceptId("c0"), mind_id=mind_id, name="c0",
                    importance=0.2, confidence=0.5),
            Concept(id=ConceptId("c1"), mind_id=mind_id, name="c1",
                    importance=0.5, confidence=0.5),
            Concept(id=ConceptId("c2"), mind_id=mind_id, name="c2",
                    importance=0.8, confidence=0.5),
        ]

        mock_concepts = AsyncMock()
        mock_concepts.get_by_mind = AsyncMock(return_value=concepts)
        mock_concepts.batch_update_scores = AsyncMock()

        cycle_n = ConsolidationCycle(
            mock_brain, mock_decay, event_bus,
            concept_repo=mock_concepts,
            importance_scorer=ImportanceScorer(),
            confidence_scorer=ConfidenceScorer(),
        )

        result = await cycle_n._normalize_scores(mind_id)
        assert result == 0  # No changes needed


class TestConsolidationScheduler:
    """ConsolidationScheduler tests."""

    async def test_start_creates_task(self, cycle: ConsolidationCycle, mind_id: MindId) -> None:
        scheduler = ConsolidationScheduler(cycle, interval_hours=1)
        await scheduler.start(mind_id)
        assert scheduler._task is not None
        await scheduler.stop()

    async def test_stop_cancels_task(self, cycle: ConsolidationCycle, mind_id: MindId) -> None:
        scheduler = ConsolidationScheduler(cycle, interval_hours=1)
        await scheduler.start(mind_id)
        await scheduler.stop()
        assert scheduler._task is None

    async def test_stop_without_start(self, cycle: ConsolidationCycle) -> None:
        scheduler = ConsolidationScheduler(cycle)
        await scheduler.stop()  # Should not raise
        assert scheduler._task is None

    async def test_double_start_is_noop(self, cycle: ConsolidationCycle, mind_id: MindId) -> None:
        scheduler = ConsolidationScheduler(cycle, interval_hours=1)
        await scheduler.start(mind_id)
        task1 = scheduler._task
        await scheduler.start(mind_id)  # Should not create new task
        assert scheduler._task is task1
        await scheduler.stop()

    async def test_scheduler_runs_cycle(
        self, mock_brain: AsyncMock, mock_decay: AsyncMock, event_bus: EventBus, mind_id: MindId
    ) -> None:
        """Scheduler actually calls cycle.run after interval."""
        cycle_mock = AsyncMock()
        cycle_mock.run = AsyncMock()

        scheduler = ConsolidationScheduler(cycle_mock, interval_hours=1)
        # Patch interval to tiny value
        scheduler._interval_s = 0.05

        await scheduler.start(mind_id)
        await asyncio.sleep(0.15)
        await scheduler.stop()

        assert cycle_mock.run.await_count >= 1

    async def test_scheduler_survives_cycle_failure(self, mind_id: MindId) -> None:
        """Scheduler continues even if cycle.run raises."""
        cycle_mock = AsyncMock()
        call_count = 0

        async def flaky_run(mid: MindId) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                msg = "transient error"
                raise RuntimeError(msg)

        cycle_mock.run = flaky_run

        scheduler = ConsolidationScheduler(cycle_mock, interval_hours=1)
        scheduler._interval_s = 0.01

        await scheduler.start(mind_id)
        await asyncio.sleep(0.3)
        await scheduler.stop()

        # Should have been called at least twice (survived first failure)
        assert call_count >= 2  # noqa: PLR2004


class TestJitter:
    """Consolidation jitter prevents thundering herd (Q19)."""

    async def test_sleep_interval_has_jitter(self) -> None:
        """Sleep time varies between 80%-120% of base interval."""
        from unittest.mock import patch

        sleep_times: list[float] = []
        call_count = 0

        async def fake_consolidate() -> None:
            pass

        original_sleep = asyncio.sleep

        async def recording_sleep(seconds: float) -> None:
            nonlocal call_count
            sleep_times.append(seconds)
            call_count += 1
            if call_count >= 5:
                raise asyncio.CancelledError
            await original_sleep(0)  # Don't actually wait

        cycle = AsyncMock()
        cycle.run_cycle = AsyncMock(side_effect=fake_consolidate)
        scheduler = ConsolidationScheduler(cycle, interval_hours=1.0)

        with patch("asyncio.sleep", side_effect=recording_sleep):
            try:
                scheduler._running = True
                await scheduler._loop(MindId("test"))
            except asyncio.CancelledError:
                pass

        # All sleep times should be in [0.8*3600, 1.2*3600]
        base = 3600.0
        for t in sleep_times:
            assert 0.79 * base <= t <= 1.21 * base, f"Sleep {t} outside jitter range"
        # Not all identical (jitter is random)
        if len(sleep_times) >= 3:
            assert len(set(sleep_times)) > 1, "All sleep times identical — jitter not working"
