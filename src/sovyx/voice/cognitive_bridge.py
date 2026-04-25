"""Bridge between the voice pipeline and the cognitive loop.

Coordinates the real-time flow:
    perception → start_thinking → filler timer → LLM stream →
    stream_text (per chunk) → flush_stream → output guard (final)

The bridge does NOT own the voice pipeline or the cognitive loop —
it's a stateless adapter that wires them together for one request.

T1 / Gap 2 wire-up — barge-in cancellation
==========================================

When the user barges in mid-response, the orchestrator's
:meth:`VoicePipeline.cancel_speech_chain` runs the four-step
transactional teardown. Step 3 invokes
:attr:`VoicePipeline._llm_cancel_hook` to stop the LLM from producing
tokens that would leak into the next turn (the pre-T1 silent failure
mode). The bridge OWNS that hook for the duration of every
:meth:`process` call:

* On entry, the bridge spawns the cogloop work as a separate
  :class:`asyncio.Task` so the orchestrator's hook can cancel it
  without taking down the bridge's own coroutine.
* It registers an idempotent cancel hook with the pipeline before
  awaiting the task — so the hook is wired by the time TTS starts and
  the consumer can detect barge-in.
* On :class:`asyncio.CancelledError` (the expected outcome of a
  barge-in cancellation), the bridge returns a sentinel
  :class:`ActionResult` with ``degraded=True`` and a
  ``cancelled_reason="barge_in"`` metadata key — never re-raises
  ``CancelledError`` because that would propagate up the orchestrator's
  consumer task and crash the whole pipeline.
* In ``finally``, the hook is unwired (set back to ``None``) so a
  subsequent un-bridged ``speak`` call doesn't accidentally trigger a
  stale hook for a torn-down task.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from sovyx.cognitive.act import ActionResult
    from sovyx.cognitive.gate import CognitiveRequest
    from sovyx.cognitive.loop import CognitiveLoop
    from sovyx.voice.pipeline._orchestrator import VoicePipeline

logger = get_logger(__name__)


_CANCELLED_BY_BARGE_IN_REASON = "barge_in"
"""Sentinel value stamped on :attr:`ActionResult.metadata['cancelled_reason']`
when the bridge returns the cancellation sentinel. Stable string so
dashboards / log search join on it without parsing the message."""


class VoiceCognitiveBridge:
    """Wire voice pipeline to the cognitive loop.

    When ``streaming=True`` (default, mirrors ``LLMConfig.streaming``),
    the bridge feeds LLM token deltas into ``pipeline.stream_text()``
    for real-time TTS (~300 ms perceived latency).

    When ``streaming=False``, the bridge falls back to a full
    ``process_request()`` call and speaks the complete response via
    ``pipeline.speak()`` (3-7 s latency, but simpler and useful when
    the LLM provider doesn't support streaming or the user disables
    it in ``mind.yaml``).

    In both paths, ``start_thinking()`` fires the filler timer so the
    user hears a natural "thinking" cue while the LLM processes.

    Every :meth:`process` call wraps the cogloop work in a cancellable
    :class:`asyncio.Task` and registers an LLM cancel hook with the
    pipeline so a barge-in mid-response stops not just audio playback
    but the LLM stream itself (T1 / Gap 2 — without the hook the LLM
    keeps producing tokens that bleed into the next turn).

    Bootstrap wiring::

        bridge = VoiceCognitiveBridge(
            cogloop, pipeline,
            streaming=mind_config.llm.streaming,
        )
    """

    def __init__(
        self,
        cogloop: CognitiveLoop,
        pipeline: VoicePipeline,
        *,
        streaming: bool = True,
    ) -> None:
        self._cogloop = cogloop
        self._pipeline = pipeline
        self._streaming = streaming

    async def process(self, request: CognitiveRequest) -> ActionResult:
        """Run one voice request through the cognitive loop.

        Chooses the streaming or non-streaming path based on the
        ``streaming`` flag set at construction time.

        The cogloop work runs on a separate :class:`asyncio.Task` whose
        cancellation hook is registered with
        :meth:`VoicePipeline.register_llm_cancel_hook` before the await
        starts. A barge-in invokes that hook → cancels the task →
        ``await`` here raises :class:`asyncio.CancelledError` → we
        return the sentinel :class:`ActionResult` (``degraded=True``,
        ``metadata={"cancelled_reason": "barge_in"}``) so the caller's
        ``await self.process(...)`` completes cleanly instead of
        propagating the cancellation up the consumer task. The hook is
        always unregistered in ``finally`` so a stale hook never fires
        for a torn-down task.
        """
        await self._pipeline.start_thinking()

        # Spawn the cogloop work as a separate, named, cancellable task.
        # Naming makes asyncio.all_tasks() output legible during incident
        # forensics; the name carries the mind id + a short suffix so a
        # multi-mind deployment doesn't collide on identical strings.
        if self._streaming:
            coro = self._process_streaming(request)
        else:
            coro = self._process_batch(request)
        cogloop_task: asyncio.Task[ActionResult] = asyncio.create_task(
            coro,
            name=f"voice-cogbridge-{request.mind_id}",
        )

        async def _cancel_hook() -> None:
            """Cancel the in-flight cogloop task on barge-in.

            Idempotent: the orchestrator's cancellation chain may invoke
            this multiple times (concurrent barge-ins serialise under
            the chain lock, but a defensive idempotent hook is cheaper
            than reasoning about race windows). Never raises — the chain
            contract requires the hook to handle its own failures so
            chain-step accounting stays meaningful.
            """
            if cogloop_task.done():
                return
            cogloop_task.cancel()

        self._pipeline.register_llm_cancel_hook(_cancel_hook)
        try:
            return await cogloop_task
        except asyncio.CancelledError:
            # Expected outcome of a barge-in cancellation. Returning a
            # sentinel keeps the caller (`_on_perception`) from propagating
            # CancelledError up the consumer task — that would crash the
            # capture loop on every barge-in. The metadata reason lets
            # dashboards distinguish a cancelled turn from a degraded /
            # errored one.
            from sovyx.cognitive.act import ActionResult as _ActionResult

            logger.info(
                "voice_cognitive_bridge_cancelled_by_barge_in",
                mind_id=str(request.mind_id),
                streaming=self._streaming,
                **{"voice.utterance_id": self._pipeline.current_utterance_id},
            )
            return _ActionResult(
                response_text="",
                target_channel="voice",
                degraded=True,
                metadata={"cancelled_reason": _CANCELLED_BY_BARGE_IN_REASON},
            )
        finally:
            # Unwire the hook so a subsequent un-bridged speak() (e.g. a
            # proactive prompt from the cognitive layer) does not invoke a
            # stale hook bound to a now-completed task. None is the
            # documented "no hook" sentinel on register_llm_cancel_hook.
            self._pipeline.register_llm_cancel_hook(None)

    async def _process_streaming(self, request: CognitiveRequest) -> ActionResult:
        """Streaming path: chunk-by-chunk TTS as the LLM produces tokens."""
        try:
            result = await self._cogloop.process_request_streaming(
                request,
                on_text_chunk=self._pipeline.stream_text,
            )
        except asyncio.CancelledError:
            # Barge-in cancellation propagating from the orchestrator's
            # cancel_speech_chain → _llm_cancel_hook → this task. Flush
            # to release any buffered text + drain the output queue
            # cleanly, then re-raise so process()'s outer except can
            # convert to the sentinel ActionResult.
            await self._pipeline.flush_stream()
            raise
        except Exception:  # noqa: BLE001
            logger.exception("voice_cognitive_bridge_error")
            await self._pipeline.flush_stream()
            raise

        await self._pipeline.flush_stream()

        logger.debug(
            "voice_request_complete",
            degraded=result.degraded,
            error=result.error,
            streamed=True,
        )
        return result

    async def _process_batch(self, request: CognitiveRequest) -> ActionResult:
        """Non-streaming path: wait for full response, then speak it."""
        try:
            result = await self._cogloop.process_request(request)
        except asyncio.CancelledError:
            # Barge-in mid-batch: the LLM call itself was cancelled.
            # Re-raise so process() converts to the sentinel.
            raise
        except Exception:  # noqa: BLE001
            logger.exception("voice_cognitive_bridge_error")
            raise

        if result.response_text and not result.filtered:
            await self._pipeline.speak(result.response_text)

        logger.debug(
            "voice_request_complete",
            degraded=result.degraded,
            error=result.error,
            streamed=False,
        )
        return result
