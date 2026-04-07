"""Tests for sovyx.cognitive.gate — CogLoopGate."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from sovyx.cognitive.act import ActionResult
from sovyx.cognitive.gate import CogLoopGate, CognitiveRequest
from sovyx.cognitive.perceive import Perception
from sovyx.engine.errors import CognitiveError
from sovyx.engine.types import ConversationId, MindId, PerceptionType

MIND = MindId("aria")
CONV = ConversationId("conv1")


def _request(content: str = "Hello", priority: int = 10) -> CognitiveRequest:
    return CognitiveRequest(
        perception=Perception(
            id="p1",
            type=PerceptionType.USER_MESSAGE,
            source="telegram",
            content=content,
            priority=priority,
        ),
        mind_id=MIND,
        conversation_id=CONV,
        conversation_history=[],
    )


def _mock_loop(
    result: ActionResult | None = None,
    delay: float = 0.0,
) -> AsyncMock:
    loop = AsyncMock()

    async def process(req: CognitiveRequest) -> ActionResult:
        if delay > 0:
            await asyncio.sleep(delay)
        return result or ActionResult(response_text="OK", target_channel="telegram")

    loop.process_request = AsyncMock(side_effect=process)
    return loop


class TestSubmit:
    """Gate submit."""

    async def test_submit_returns_result(self) -> None:
        gate = CogLoopGate(_mock_loop())
        await gate.start()
        try:
            result = await gate.submit(_request())
            assert result.response_text == "OK"
        finally:
            await gate.stop()

    async def test_submit_timeout(self) -> None:
        gate = CogLoopGate(_mock_loop(delay=5.0))
        await gate.start()
        try:
            with pytest.raises(CognitiveError, match="timed out"):
                await gate.submit(_request(), timeout=0.1)
        finally:
            await gate.stop()


class TestSerialization:
    """Requests serialized."""

    async def test_sequential_processing(self) -> None:
        order: list[str] = []
        loop = AsyncMock()

        async def process(req: CognitiveRequest) -> ActionResult:
            order.append(req.perception.content)
            await asyncio.sleep(0.01)
            return ActionResult(response_text="OK", target_channel="test")

        loop.process_request = AsyncMock(side_effect=process)
        gate = CogLoopGate(loop)
        await gate.start()
        try:
            r1 = gate.submit(
                CognitiveRequest(
                    perception=Perception(
                        id="1",
                        type=PerceptionType.USER_MESSAGE,
                        source="t",
                        content="first",
                        priority=10,
                    ),
                    mind_id=MIND,
                    conversation_id=CONV,
                    conversation_history=[],
                )
            )
            r2 = gate.submit(
                CognitiveRequest(
                    perception=Perception(
                        id="2",
                        type=PerceptionType.USER_MESSAGE,
                        source="t",
                        content="second",
                        priority=10,
                    ),
                    mind_id=MIND,
                    conversation_id=CONV,
                    conversation_history=[],
                )
            )
            await asyncio.gather(r1, r2)
            assert len(order) == 2  # noqa: PLR2004
        finally:
            await gate.stop()


class TestBackpressure:
    """Queue full handling."""

    async def test_queue_full_raises(self) -> None:
        gate = CogLoopGate(_mock_loop(delay=10.0))
        gate._queue = asyncio.PriorityQueue(maxsize=1)
        await gate.start()
        try:
            # Fill the queue
            gate._queue.put_nowait((10, 0, _request(), asyncio.get_event_loop().create_future()))
            with pytest.raises(CognitiveError, match="queue full"):
                await gate.submit(_request(), timeout=0.1)
        finally:
            await gate.stop()


class TestLifecycle:
    """Start/stop."""

    async def test_start_stop(self) -> None:
        gate = CogLoopGate(_mock_loop())
        await gate.start()
        await gate.stop()

    async def test_stop_drains_pending(self) -> None:
        gate = CogLoopGate(_mock_loop(delay=10.0))
        # Don't start worker — queue items will be drained on stop
        future: asyncio.Future[ActionResult] = asyncio.get_event_loop().create_future()
        gate._queue.put_nowait((10, 0, _request(), future))
        await gate.stop()
        assert future.done()


class TestCognitiveRequest:
    """CognitiveRequest dataclass."""

    def test_fields(self) -> None:
        req = _request()
        assert req.mind_id == MIND
        assert req.conversation_id == CONV
        assert req.person_name is None


class TestGateCoverageGaps:
    """Cover remaining gate paths."""

    @pytest.mark.asyncio()
    async def test_stop_drains_pending_queue(self) -> None:
        """Stop sets exception on pending futures in queue."""
        from sovyx.cognitive.gate import CogLoopGate
        from sovyx.engine.errors import CognitiveError

        mock_loop = AsyncMock()
        # Make process_request block so items stay in queue
        mock_loop.process_request = AsyncMock(side_effect=asyncio.CancelledError)
        gate = CogLoopGate(mock_loop)

        # Don't start worker — manually add items to queue
        gate._running = True  # noqa: SLF001
        loop = asyncio.get_running_loop()
        future1: asyncio.Future[object] = loop.create_future()
        future2: asyncio.Future[object] = loop.create_future()
        await gate._queue.put((0, 0, AsyncMock(), future1))  # noqa: SLF001
        await gate._queue.put((1, 1, AsyncMock(), future2))  # noqa: SLF001

        # Stop should drain the queue (both items)
        await gate.stop()

        assert future1.done()
        assert future2.done()
        with pytest.raises(CognitiveError, match="shutting down"):
            future1.result()
        with pytest.raises(CognitiveError, match="shutting down"):
            future2.result()

    @pytest.mark.asyncio()
    async def test_worker_exception_sets_future(self) -> None:
        """When process_request raises, future gets the exception."""
        from sovyx.cognitive.gate import CogLoopGate

        mock_loop = AsyncMock()
        mock_loop.process_request = AsyncMock(
            side_effect=RuntimeError("process failed"),
        )
        gate = CogLoopGate(mock_loop)
        await gate.start()

        try:
            with pytest.raises(RuntimeError, match="process failed"):
                await asyncio.wait_for(
                    gate.submit(AsyncMock(mind_id="m", conversation_id="c")),
                    timeout=2.0,
                )
        finally:
            await gate.stop()
