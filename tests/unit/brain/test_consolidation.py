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
