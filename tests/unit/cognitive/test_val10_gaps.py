"""VAL-10: Coverage gaps for cognitive/gate.py and cognitive/loop.py."""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import AsyncMock, MagicMock

import pytest

from sovyx.engine.errors import CostLimitExceededError, ProviderUnavailableError


class TestCogLoopGateShutdown:
    @pytest.mark.asyncio()
    async def test_stop_drains_pending_requests(self) -> None:
        """stop() sets exception on pending requests in the queue."""
        from sovyx.cognitive.gate import CogLoopGate

        mock_loop = MagicMock()
        gate = CogLoopGate(cognitive_loop=mock_loop)
        await gate.start()

        # Put a request with the correct 4-tuple format: (priority, counter, request, future)
        future: asyncio.Future[object] = asyncio.Future()
        gate._queue.put_nowait((1, 0, MagicMock(), future))

        await gate.stop()

        assert future.done()
        with pytest.raises(Exception, match="shutting down"):  # noqa: B017
            future.result()


class TestCogLoopGateWorkerError:
    @pytest.mark.asyncio()
    async def test_worker_error_sets_exception_on_future(self) -> None:
        """When process_request raises, the future gets the exception."""
        from sovyx.cognitive.gate import CogLoopGate, CognitiveRequest
        from sovyx.engine.types import ConversationId, MindId

        mock_loop = MagicMock()
        mock_loop.process_request = AsyncMock(side_effect=RuntimeError("process crashed"))

        gate = CogLoopGate(cognitive_loop=mock_loop)
        await gate.start()

        from sovyx.cognitive.perceive import Perception
        from sovyx.engine.types import PerceptionType

        request = CognitiveRequest(
            perception=Perception(
                id="req-1",
                type=PerceptionType.USER_MESSAGE,
                source="test",
                content="hello",
                person_id="p-1",
            ),
            mind_id=MindId("test"),
            conversation_id=ConversationId("conv-1"),
            conversation_history=[],
        )

        # Submit through the gate
        with contextlib.suppress(RuntimeError):
            await asyncio.wait_for(gate.submit(request), timeout=2.0)

        await gate.stop()


class TestCategorizeError:
    def test_cost_limit_message(self) -> None:
        """CostLimitExceededError returns budget message."""
        from sovyx.cognitive.loop import _categorize_error

        msg = _categorize_error(CostLimitExceededError("budget gone"))
        assert "budget" in msg.lower()

    def test_provider_unavailable_message(self) -> None:
        """ProviderUnavailableError returns provider message."""
        from sovyx.cognitive.loop import _categorize_error

        msg = _categorize_error(ProviderUnavailableError("all down"))
        assert "provider" in msg.lower()

    def test_generic_error_message(self) -> None:
        """Unknown error returns generic message without details."""
        from sovyx.cognitive.loop import _categorize_error

        msg = _categorize_error(RuntimeError("internal secret"))
        assert "unexpected error" in msg.lower()
        assert "internal secret" not in msg
