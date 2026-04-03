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
        return result or ActionResult(
            response_text="OK", target_channel="telegram"
        )

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
                        id="1", type=PerceptionType.USER_MESSAGE,
                        source="t", content="first", priority=10,
                    ),
                    mind_id=MIND, conversation_id=CONV,
                    conversation_history=[],
                )
            )
            r2 = gate.submit(
                CognitiveRequest(
                    perception=Perception(
                        id="2", type=PerceptionType.USER_MESSAGE,
                        source="t", content="second", priority=10,
                    ),
                    mind_id=MIND, conversation_id=CONV,
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
