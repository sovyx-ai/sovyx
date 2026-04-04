"""Sovyx CogLoopGate — serialize requests to the cognitive loop.

INT-001: PriorityQueue + single worker pattern.
Multiple channels submit requests, gate serializes processing.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import itertools
from typing import TYPE_CHECKING

from sovyx.engine.errors import CognitiveError
from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from sovyx.cognitive.act import ActionResult
    from sovyx.cognitive.loop import CognitiveLoop
    from sovyx.cognitive.perceive import Perception
    from sovyx.engine.types import ConversationId, MindId

logger = get_logger(__name__)


@dataclasses.dataclass
class CognitiveRequest:
    """Bundle of data needed to process a perception.

    The Gate is the boundary between Bridge and Cognitive.
    BridgeManager builds CognitiveRequest with ALL needed data.
    """

    perception: Perception
    mind_id: MindId
    conversation_id: ConversationId
    conversation_history: list[dict[str, str]]
    person_name: str | None = None


class CogLoopGate:
    """Serialize requests to CognitiveLoop via PriorityQueue.

    - PriorityQueue(maxsize=10) with backpressure
    - Single worker drains sequentially
    - asyncio.Future per request — caller awaits with timeout
    """

    def __init__(self, cognitive_loop: CognitiveLoop) -> None:
        self._loop = cognitive_loop
        self._queue: asyncio.PriorityQueue[
            tuple[int, int, CognitiveRequest, asyncio.Future[ActionResult]]
        ] = asyncio.PriorityQueue(maxsize=10)
        self._counter = itertools.count()
        self._worker_task: asyncio.Task[None] | None = None
        self._running = False

    async def submit(
        self,
        request: CognitiveRequest,
        timeout: float = 30.0,
    ) -> ActionResult:
        """Submit a request and wait for result.

        Args:
            request: CognitiveRequest bundle.
            timeout: Max wait time in seconds.

        Returns:
            ActionResult from the cognitive loop.

        Raises:
            CognitiveError: On timeout or queue full.
        """
        future: asyncio.Future[ActionResult] = asyncio.get_running_loop().create_future()
        item = (
            request.perception.priority,
            next(self._counter),
            request,
            future,
        )

        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            msg = "Cognitive loop queue full (backpressure)"
            raise CognitiveError(msg) from None

        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError:
            msg = f"Cognitive loop timed out after {timeout}s"
            raise CognitiveError(msg) from None

    async def start(self) -> None:
        """Start background worker."""
        self._running = True
        self._worker_task = asyncio.create_task(self._worker())
        logger.info("cogloop_gate_started")

    async def stop(self) -> None:
        """Stop worker, drain pending."""
        self._running = False
        if self._worker_task:
            self._worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker_task
        # Drain pending with errors
        while not self._queue.empty():
            try:
                _, _, _, future = self._queue.get_nowait()
                if not future.done():
                    future.set_exception(CognitiveError("Gate shutting down"))
            except asyncio.QueueEmpty:
                break
        logger.info("cogloop_gate_stopped")

    async def _worker(self) -> None:
        """Single worker draining the queue."""
        while self._running:
            try:
                priority, _, request, future = await asyncio.wait_for(
                    self._queue.get(), timeout=1.0
                )
            except (TimeoutError, asyncio.CancelledError):
                continue

            try:
                result = await self._loop.process_request(request)
                if not future.done():
                    future.set_result(result)
            except Exception as e:
                if not future.done():
                    future.set_exception(e)
