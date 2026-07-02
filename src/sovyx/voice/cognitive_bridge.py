"""Bridge between the voice pipeline and the cognitive loop.

Coordinates the real-time flow:
    perception → start_thinking → filler timer → LLM stream →
    stream_text (per chunk; each sentence segment guarded before
    TTS) → flush_stream

The per-segment guard is the loop's regex-tier output/PII guard,
registered via :meth:`VoicePipeline.set_stream_segment_guard` so
streamed segments are filtered BEFORE synthesis.

The bridge does NOT own the voice pipeline or the cognitive loop —
it's a stateless adapter that wires them together for one request.

T1 / Gap 2 wire-up — barge-in cancellation
==========================================

When the user barges in mid-response, the orchestrator's
:meth:`VoicePipeline.cancel_speech_chain` runs the transactional
teardown chain. Step 3 invokes
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
        # P0 safety wire-up — register the loop's regex-tier output/PII
        # guards on the pipeline's per-segment hook. Pre-fix the
        # streaming path spoke raw LLM text: ActPhase's guards ran only
        # AFTER the chunks had already been synthesized and enqueued,
        # so the guarded text existed on the text surface while the
        # user heard the unfiltered audio. Best-effort: a pipeline
        # double without the setter (legacy tests) must not break
        # construction.
        set_guard = getattr(pipeline, "set_stream_segment_guard", None)
        if callable(set_guard):
            set_guard(cogloop.guard_streaming_segment)

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

        # Capture a local reference to THIS turn's hook so the finally
        # block can do an identity check before unwiring (v0.31.7 CR1).
        local_cancel_hook = _cancel_hook
        self._pipeline.register_llm_cancel_hook(local_cancel_hook)
        try:
            result = await cogloop_task
            # W1.2 / G-P1-1 — when the cognitive loop reports a real failure
            # (ThinkPhase LLM-degraded OR a dependency-missing short-circuit —
            # both surface as error=True), emit a PipelineErrorEvent so the
            # dashboard error banner can tell "LLM down" from a deliberately
            # short answer. The user still hears the canned reply (already
            # streamed / spoken); this only adds the missing bus signal. The
            # barge-in path returns via the CancelledError branch below with
            # error=False, so a cancellation never trips this.
            if result.error:
                try:
                    reason = str(result.metadata.get("reason", "cognitive_error"))
                    await self._pipeline.report_cognitive_error(
                        error=f"cognitive_loop_error: {reason}",
                    )
                except Exception:  # noqa: BLE001 — telemetry must not affect the turn
                    logger.warning("voice_cognitive_error_report_failed", exc_info=True)
            return result
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
            # v0.31.7 CR1 — cancel-hook null race between turns.
            #
            # Pre-v0.31.7 this block did
            # ``self._pipeline.register_llm_cancel_hook(None)``
            # UNCONDITIONALLY. That created a window where:
            #
            #   1. Turn N's task is cancelled by ``_on_perception`` for
            #      turn N+1 but its ``finally`` has not yet run.
            #   2. Turn N+1's bridge.process() runs, registers its own
            #      cancel hook on the pipeline.
            #   3. Turn N's ``finally`` finally runs and resets the hook
            #      to ``None`` — nulling the LIVE hook turn N+1 just
            #      registered.
            #   4. A barge-in during turn N+1 reports
            #      ``llm_cancel="no_hook_registered"`` even though there
            #      IS a live LLM stream — audio + TTS get cancelled but
            #      the LLM keeps producing tokens that bleed into the
            #      next turn.
            #
            # Fix: only null the hook if it is STILL the local hook
            # this turn registered. ``is`` (identity) — if turn N+1
            # already replaced the hook, ``_llm_cancel_hook`` no longer
            # IS our local reference, and we leave it alone.
            #
            # Companion fix in ``_on_perception`` await-with-timeout
            # ensures this finally block runs BEFORE the next turn
            # registers its hook in the common case; the identity check
            # is the durable guard for the race window where the await
            # times out and the new turn proceeds anyway.
            if self._pipeline._llm_cancel_hook is local_cancel_hook:
                self._pipeline.register_llm_cancel_hook(None)

    async def _process_streaming(self, request: CognitiveRequest) -> ActionResult:
        """Streaming path: chunk-by-chunk TTS as the LLM produces tokens.

        Counts the chunks the loop actually streamed so short-circuit
        paths that return an :class:`ActionResult` WITHOUT ever calling
        ``on_text_chunk`` (the C6 dependency-gate synthetic "no LLM
        provider" result is the canonical case) are still SPOKEN via
        the batch ``speak()`` path. Pre-fix the user heard the
        "thinking" filler and then silence forever in exactly the
        misconfiguration the gate exists to explain — the operator-
        actionable message reached Telegram/dashboard but never voice.
        """
        chunks_streamed = 0

        async def _counting_chunk(delta_text: str) -> None:
            nonlocal chunks_streamed
            chunks_streamed += 1
            await self._pipeline.stream_text(delta_text)

        try:
            result = await self._cogloop.process_request_streaming(
                request,
                on_text_chunk=_counting_chunk,
            )
        except asyncio.CancelledError:
            # Barge-in cancellation propagating from the orchestrator's
            # cancel_speech_chain → _llm_cancel_hook → this task.
            # ``discard_buffer=True`` drops the interrupted response's
            # residual text — pre-fix the normal flush SYNTHESIZED the
            # tail, so the user heard a fragment of the cancelled
            # utterance after barging in. Re-raise so process()'s outer
            # except converts to the sentinel ActionResult.
            await self._pipeline.flush_stream(discard_buffer=True)
            raise
        except Exception:  # noqa: BLE001
            logger.exception("voice_cognitive_bridge_error")
            await self._pipeline.flush_stream()
            raise

        if chunks_streamed == 0 and result.response_text and not result.filtered:
            # Nothing was streamed but the loop produced speakable text
            # (dependency-gate short-circuit, future non-streaming
            # fallbacks). speak() owns the SPEAKING→IDLE lifecycle.
            await self._pipeline.speak(result.response_text)
        else:
            await self._pipeline.flush_stream()

        logger.debug(
            "voice_request_complete",
            degraded=result.degraded,
            error=result.error,
            streamed=True,
            chunks_streamed=chunks_streamed,
        )
        return result

    async def _process_batch(self, request: CognitiveRequest) -> ActionResult:
        """Non-streaming path: wait for full response, then speak it.

        Every exit MUST hand the pipeline state back — the pipeline is
        in THINKING (set by ``start_thinking``) and only the TTS-out
        surfaces write it forward. Pre-fix, an empty/filtered response
        or a cogloop exception left the pipeline latched in THINKING
        forever (no wake word, no timeout — permanently deaf until
        process restart; the dwell watchdog now bounds this to its
        ceiling, but the bridge closes it immediately).
        """
        try:
            result = await self._cogloop.process_request(request)
        except asyncio.CancelledError:
            # Barge-in mid-batch: the LLM call itself was cancelled.
            # Re-raise so process() converts to the sentinel.
            raise
        except Exception:  # noqa: BLE001
            logger.exception("voice_cognitive_bridge_error")
            # Release the THINKING state before propagating — the
            # caller (_run_bridge_isolated) swallows the exception, so
            # nothing downstream would ever reset it.
            await self._pipeline.flush_stream()
            raise

        if result.response_text and not result.filtered:
            await self._pipeline.speak(result.response_text)
        else:
            # No speakable text (guardrail-filtered or empty) — reset
            # the turn explicitly; flush on an empty buffer is the
            # established idle-handoff (mirrors the streaming path).
            await self._pipeline.flush_stream()

        logger.debug(
            "voice_request_complete",
            degraded=result.degraded,
            error=result.error,
            streamed=False,
        )
        return result
