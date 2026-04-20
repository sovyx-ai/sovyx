"""Sovyx CognitiveLoop — the heart of the system.

Perceive → Attend → Think → Act → Reflect.
Orchestrates all phases, manages state machine, emits events.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from sovyx.cognitive.act import ActionResult
from sovyx.engine.types import CognitivePhase
from sovyx.llm.models import LLMResponse, ToolCall
from sovyx.observability.logging import get_logger
from sovyx.observability.metrics import MetricsRegistry, get_metrics
from sovyx.observability.saga import trace_saga
from sovyx.observability.tracing import SovyxTracer, get_tracer

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sovyx.brain.service import BrainService
    from sovyx.cognitive.act import ActPhase
    from sovyx.cognitive.attend import AttendPhase
    from sovyx.cognitive.gate import CognitiveRequest
    from sovyx.cognitive.perceive import PerceivePhase
    from sovyx.cognitive.reflect import ReflectPhase
    from sovyx.cognitive.state import CognitiveStateMachine
    from sovyx.cognitive.think import ThinkPhase
    from sovyx.engine.events import EventBus
    from sovyx.observability.metrics import _NoOpRegistry

logger = get_logger(__name__)


def _categorize_error(exc: Exception) -> str:
    """Map exception to user-facing message.

    Three categories:
    - **Informable**: user action may help (budget exhausted)
    - **Temporary**: transient, retryable (provider down)
    - **Internal**: bugs — never expose internals to user
    """
    # Anti-pattern #8: dispatch by class name — isinstance fails when pytest-cov
    # reimports the exception classes, making the in-module and in-test class
    # objects distinct.
    name = type(exc).__name__
    if name == "CostLimitExceededError":
        return "I've reached my conversation budget limit. Please try again later."
    if name == "ProviderUnavailableError":
        return (
            "No AI provider is available right now. "
            "Please check the LLM Providers section in Settings — "
            "you can configure a cloud API key (Anthropic, OpenAI, Google) "
            "or start a local Ollama server (ollama serve)."
        )
    # Default: internal error — don't leak details
    return "I encountered an unexpected error. Please try again."


class CognitiveLoop:
    """Complete cognitive loop: Perceive → Attend → Think → Act → Reflect.

    Orchestrates all phases. Manages state machine.
    Gate → calls → Loop.process_request(). Loop never calls Gate.
    """

    def __init__(
        self,
        state_machine: CognitiveStateMachine,
        perceive: PerceivePhase,
        attend: AttendPhase,
        think: ThinkPhase,
        act: ActPhase,
        reflect: ReflectPhase,
        event_bus: EventBus,
        brain: BrainService | None = None,
    ) -> None:
        self._state = state_machine
        self._perceive = perceive
        self._attend = attend
        self._think = think
        self._act = act
        self._reflect = reflect
        self._events = event_bus
        self._brain = brain

    async def start(self) -> None:
        """Start the cognitive loop."""
        logger.info("cognitive_loop_started")

    async def stop(self) -> None:
        """Stop the cognitive loop."""
        logger.info("cognitive_loop_stopped")

    @trace_saga("cognitive_loop", kind="cognitive")
    async def process_request(self, request: CognitiveRequest) -> ActionResult:
        """Process a CognitiveRequest through the full loop.

        NEVER raises an exception — always returns ActionResult.
        State machine always resets to IDLE via finally block.

        Wrapped in ``@trace_saga`` so every log emitted during the loop
        (perceive/attend/think/act/reflect, brain calls, LLM calls,
        plugin invokes, brain writes) shares the same ``saga_id`` and
        the operator can reconstruct the full causal chain by filter.
        """
        tracer = get_tracer()
        metrics = get_metrics()

        with (
            tracer.start_span(
                "cognitive.loop",
                mind_id=str(request.mind_id),
                conversation_id=str(request.conversation_id),
            ),
            metrics.measure_latency(metrics.cognitive_loop_latency),
        ):
            return await self._execute_loop(request, tracer, metrics)

    @trace_saga("cognitive_loop", kind="cognitive_streaming")
    async def process_request_streaming(
        self,
        request: CognitiveRequest,
        on_text_chunk: Callable[[str], Awaitable[None]],
        on_phase: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> ActionResult:
        """Process with streaming LLM output — voice channel fast-path.

        Like :meth:`process_request` but yields text chunks to
        ``on_text_chunk`` as the LLM produces them (for real-time TTS).
        Optional ``on_phase(phase_name, detail)`` callback emits cognitive
        phase transitions for dashboard transparency.
        Reconstructs a complete :class:`LLMResponse` from the stream so
        ActPhase + ReflectPhase run identically to the non-streaming path.

        When the stream ends with ``finish_reason="tool_use"``, the
        reconstructed response (with tool_calls) goes through the normal
        ReAct loop in ActPhase — no chunks were forwarded for that
        iteration (the filler continues while tools execute). The FINAL
        response of the ReAct loop is NOT streamed (V2 work).

        NEVER raises — always returns ActionResult.
        """
        tracer = get_tracer()
        metrics = get_metrics()

        with (
            tracer.start_span(
                "cognitive.loop.streaming",
                mind_id=str(request.mind_id),
                conversation_id=str(request.conversation_id),
            ),
            metrics.measure_latency(metrics.cognitive_loop_latency),
        ):
            return await self._execute_loop_streaming(
                request,
                on_text_chunk,
                tracer,
                metrics,
                on_phase=on_phase,
            )

    async def _execute_loop(
        self,
        request: CognitiveRequest,
        t: SovyxTracer,
        m: MetricsRegistry | _NoOpRegistry,
    ) -> ActionResult:
        """Execute the cognitive loop phases with tracing and metrics."""
        try:
            # ── PERCEIVE ──
            self._state.transition(CognitivePhase.PERCEIVING)
            with t.start_cognitive_span("perceive", mind_id=str(request.mind_id)):
                perception = await self._perceive.process(request.perception)

            # ── ATTEND ──
            self._state.transition(CognitivePhase.ATTENDING)
            with t.start_cognitive_span("attend"):
                should_process = await self._attend.process(perception)

            if not should_process:
                logger.debug(
                    "perception_filtered",
                    perception_id=perception.id,
                )
                return ActionResult(
                    response_text="",
                    target_channel=perception.source,
                    filtered=True,
                )

            # ── THINK ──
            self._state.transition(CognitivePhase.THINKING)
            with t.start_cognitive_span("think"):
                llm_response, assembled_msgs = await self._think.process(
                    perception=perception,
                    mind_id=request.mind_id,
                    conversation_history=request.conversation_history,
                    person_name=request.person_name,
                )

            # ── ACT ──
            self._state.transition(CognitivePhase.ACTING)
            with t.start_cognitive_span("act"):
                action_result = await self._act.process(
                    llm_response,
                    assembled_msgs,
                    perception,
                )

            # Attach LLM metadata to ActionResult for dashboard
            action_result.metadata["model"] = llm_response.model
            action_result.metadata["tokens_in"] = llm_response.tokens_in
            action_result.metadata["tokens_out"] = llm_response.tokens_out
            action_result.metadata["cost_usd"] = llm_response.cost_usd
            action_result.metadata["latency_ms"] = llm_response.latency_ms
            action_result.metadata["provider"] = llm_response.provider

            # ── REFLECT ──
            self._state.transition(CognitivePhase.REFLECTING)
            with t.start_cognitive_span("reflect"):
                try:
                    await self._reflect.process(
                        perception=perception,
                        response=llm_response,
                        mind_id=request.mind_id,
                        conversation_id=request.conversation_id,
                    )
                except Exception:  # noqa: BLE001 — reflect phase isolated from main request path
                    logger.warning("reflect_phase_failed", exc_info=True)

                # Decay working memory after reflect — concepts not
                # re-activated will gradually fade, keeping star topology
                # focused on recent/relevant concepts.
                try:
                    if self._brain is not None:
                        self._brain.decay_working_memory()
                except Exception:  # noqa: BLE001 — working-memory decay isolated from main request path
                    logger.warning("working_memory_decay_failed", exc_info=True)

            m.messages_processed.add(1, {"mind_id": str(request.mind_id)})

            logger.debug(
                "cognitive_loop_complete",
                perception_id=perception.id,
                degraded=action_result.degraded,
            )
            return action_result

        except Exception as e:  # noqa: BLE001
            error_type = type(e).__name__
            user_message = _categorize_error(e)
            m.errors.add(1, {"error_type": error_type, "module": "cognitive"})
            logger.exception(
                "cognitive_loop_error",
                error=str(e),
                error_type=error_type,
            )
            return ActionResult(
                response_text=user_message,
                target_channel=request.perception.source,
                error=True,
            )

        finally:
            self._state.reset()

    async def _execute_loop_streaming(
        self,
        request: CognitiveRequest,
        on_text_chunk: Callable[[str], Awaitable[None]],
        t: SovyxTracer,
        m: MetricsRegistry | _NoOpRegistry,
        *,
        on_phase: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> ActionResult:
        """Streaming variant of ``_execute_loop``.

        Consumes the LLM stream, forwarding text deltas to
        ``on_text_chunk`` for real-time TTS. Rebuilds a full
        ``LLMResponse`` for ActPhase + ReflectPhase.
        """
        try:

            async def _emit_phase(name: str, detail: str = "") -> None:
                if on_phase is not None:
                    await on_phase(name, detail)

            # ── PERCEIVE ──
            self._state.transition(CognitivePhase.PERCEIVING)
            await _emit_phase("perceiving")
            with t.start_cognitive_span("perceive", mind_id=str(request.mind_id)):
                perception = await self._perceive.process(request.perception)

            # ── ATTEND ──
            self._state.transition(CognitivePhase.ATTENDING)
            await _emit_phase("attending", "checking safety")
            with t.start_cognitive_span("attend"):
                should_process = await self._attend.process(perception)

            if not should_process:
                return ActionResult(
                    response_text="",
                    target_channel=perception.source,
                    filtered=True,
                )

            # ── THINK (streaming) ──
            self._state.transition(CognitivePhase.THINKING)
            await _emit_phase("thinking")
            with t.start_cognitive_span("think"):
                chunk_iter, assembled_msgs = await self._think.process_streaming(
                    perception=perception,
                    mind_id=request.mind_id,
                    conversation_history=request.conversation_history,
                    person_name=request.person_name,
                )

                # Consume stream, forwarding text and accumulating for
                # the final LLMResponse reconstruction.
                content_parts: list[str] = []
                # tool_call_deltas keyed by index → accumulates id, name, args.
                tc_accum: dict[int, dict[str, str]] = {}
                final_model = "unknown"
                final_provider = "unknown"
                tokens_in = 0
                tokens_out = 0
                finish_reason = "stop"

                async for chunk in chunk_iter:
                    if chunk.delta_text:
                        content_parts.append(chunk.delta_text)
                        # Only forward text to TTS when NOT a tool_use
                        # stream (we check at the end, but since
                        # tool_use streams rarely interleave text, this
                        # is fine — any stray text before tool_calls
                        # will just be spoken).
                        await on_text_chunk(chunk.delta_text)

                    if chunk.tool_call_delta:
                        tcd = chunk.tool_call_delta
                        entry = tc_accum.setdefault(tcd.index, {"id": "", "name": "", "args": ""})
                        if tcd.id:
                            entry["id"] = tcd.id
                        if tcd.function_name:
                            entry["name"] = tcd.function_name
                        entry["args"] += tcd.arguments_json_delta

                    if chunk.is_final:
                        tokens_in = chunk.tokens_in
                        tokens_out = chunk.tokens_out
                        finish_reason = chunk.finish_reason or "stop"

                    if chunk.model:
                        final_model = chunk.model
                    if chunk.provider:
                        final_provider = chunk.provider

            # Reconstruct LLMResponse from accumulated stream data.
            tool_calls: list[ToolCall] | None = None
            if tc_accum:
                tool_calls = []
                for _idx, entry in sorted(tc_accum.items()):
                    try:
                        args = json.loads(entry["args"]) if entry["args"] else {}
                    except json.JSONDecodeError:
                        args = {}
                    tool_calls.append(
                        ToolCall(
                            id=entry["id"],
                            function_name=entry["name"],
                            arguments=args,
                        )
                    )

            llm_response = LLMResponse(
                content="".join(content_parts),
                model=final_model,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                latency_ms=0,
                cost_usd=0.0,
                finish_reason=finish_reason,
                provider=final_provider,
                tool_calls=tool_calls,
            )

            # ── ACT ──
            self._state.transition(CognitivePhase.ACTING)
            tool_detail = ""
            if tool_calls:
                tool_names = [tc.function_name.split(".", 1)[0] for tc in tool_calls]
                tool_detail = f"using {', '.join(tool_names)}"
            await _emit_phase("acting", tool_detail)
            with t.start_cognitive_span("act"):
                action_result = await self._act.process(
                    llm_response,
                    assembled_msgs,
                    perception,
                )

            # Attach LLM metadata to ActionResult for dashboard
            action_result.metadata["model"] = final_model
            action_result.metadata["tokens_in"] = tokens_in
            action_result.metadata["tokens_out"] = tokens_out
            action_result.metadata["provider"] = final_provider

            # ── REFLECT ──
            self._state.transition(CognitivePhase.REFLECTING)
            await _emit_phase("reflecting")
            with t.start_cognitive_span("reflect"):
                try:
                    await self._reflect.process(
                        perception=perception,
                        response=llm_response,
                        mind_id=request.mind_id,
                        conversation_id=request.conversation_id,
                    )
                except Exception:  # noqa: BLE001
                    logger.warning("reflect_phase_failed", exc_info=True)

                try:
                    if self._brain is not None:
                        self._brain.decay_working_memory()
                except Exception:  # noqa: BLE001
                    logger.warning("working_memory_decay_failed", exc_info=True)

            m.messages_processed.add(1, {"mind_id": str(request.mind_id)})
            return action_result

        except Exception as e:  # noqa: BLE001
            error_type = type(e).__name__
            user_message = _categorize_error(e)
            m.errors.add(1, {"error_type": error_type, "module": "cognitive"})
            logger.exception(
                "cognitive_loop_streaming_error",
                error=str(e),
                error_type=error_type,
            )
            return ActionResult(
                response_text=user_message,
                target_channel=request.perception.source,
                error=True,
            )

        finally:
            self._state.reset()
