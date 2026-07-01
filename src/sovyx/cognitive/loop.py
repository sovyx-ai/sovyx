"""Sovyx CognitiveLoop — the heart of the system.

Perceive → Attend → Think → Act → Reflect.
Orchestrates all phases, manages state machine, emits events.
"""

from __future__ import annotations

import contextlib
import json
import time
from typing import TYPE_CHECKING

from sovyx.cognitive.act import ActionResult
from sovyx.cognitive.think import is_degraded_llm_response
from sovyx.engine.types import CognitivePhase
from sovyx.llm.models import LLMResponse, ToolCall
from sovyx.observability.logging import get_logger
from sovyx.observability.metrics import MetricsRegistry, get_metrics
from sovyx.observability.saga import trace_saga
from sovyx.observability.tracing import SovyxTracer, get_tracer


@contextlib.contextmanager
def _measure_phase_latency(phase: CognitivePhase) -> Iterator[None]:
    """T06 helper — record the elapsed wall-clock to the
    ``sovyx.cognitive.phase_latency`` Histogram with ``phase`` attribute.

    Wraps each phase invocation in the cognitive loop. Defensive:
    silently no-ops when the registry is missing the attribute (e.g.
    in tests with a bare registry). Companion to the existing
    ``t.start_cognitive_span(...)`` tracing — spans are sampled;
    this histogram aggregates without sampling for SLO dashboards.
    """
    t0 = time.monotonic()
    try:
        yield
    finally:
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        instrument = getattr(get_metrics(), "cognitive_phase_latency", None)
        if instrument is not None:
            instrument.record(elapsed_ms, attributes={"phase": phase.value})


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator

    from sovyx.brain.service import BrainService
    from sovyx.cognitive.act import ActPhase
    from sovyx.cognitive.attend import AttendPhase
    from sovyx.cognitive.gate import CognitiveRequest
    from sovyx.cognitive.perceive import PerceivePhase
    from sovyx.cognitive.reflect import ReflectPhase
    from sovyx.cognitive.state import CognitiveStateMachine
    from sovyx.cognitive.think import ThinkPhase
    from sovyx.engine.events import EventBus
    from sovyx.llm.router import LLMRouter
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
        llm_router: LLMRouter | None = None,
        cognitive_degraded_mode_fail_fast: bool = True,
    ) -> None:
        self._state = state_machine
        self._perceive = perceive
        self._attend = attend
        self._think = think
        self._act = act
        self._reflect = reflect
        self._events = event_bus
        self._brain = brain
        # Mission C6 §T4.1 — dependency gate (anti-pattern #44).
        # Optional ``llm_router`` keeps backward-compat with the pre-C6
        # constructor; bootstrap.py passes the registry-resolved router.
        # When ``None`` the LLM-side dependency check is skipped (treated
        # as "always available") so existing tests don't need updates.
        self._llm_router = llm_router
        self._fail_fast_on_missing_dep = cognitive_degraded_mode_fail_fast
        # State recorded at :meth:`start`. ``True`` until ``start`` runs
        # — preserves the legacy "no dependency check" behaviour for
        # callers that never call ``start`` (some test fixtures).
        self._dependency_ready: bool = True
        self._missing_dependencies: tuple[str, ...] = ()

    async def start(self) -> None:
        """Start the cognitive loop with dependency-gate enforcement (Mission C6 §T4.1).

        Verifies every external dependency the loop's phases will need to
        produce useful output. When any dependency is absent, emits a
        structured ``cognitive.loop.started_in_degraded_mode`` WARN AND
        records the missing-dependency list on the instance so
        :meth:`process_request` short-circuits without firing the
        invisible perceive→attend→think→act→reflect chain.

        Anti-pattern #44: dependency-gated workers MUST verify their
        dependency contract at startup, emit a structured signal when
        the dependency is absent, AND gate every iteration.
        """
        missing: list[str] = []
        verdict_llm: str | None = None
        if self._llm_router is not None and not self._llm_router.has_available_provider():
            missing.append("llm_router_no_available_provider")
            report = getattr(self._llm_router, "discovery_report", None)
            verdict_llm = (
                report.verdict.value
                if report is not None and hasattr(report, "verdict")
                else "unknown"
            )
        brain_ready: bool | None = None
        if self._brain is not None:
            brain_ready = self._brain.embedding_model_ready
            if not brain_ready:
                missing.append("brain_embedding_model_not_ready")
        self._missing_dependencies = tuple(missing)
        self._dependency_ready = not missing

        if missing:
            # c4-allowlist: cognitive-loop degradation is a CONSEQUENCE of axis=llm/brain (store record already lands via Mission C6 §T2.2 dispatch).  # noqa: E501
            logger.warning(
                "cognitive.loop.started_in_degraded_mode",
                missing_dependencies=list(missing),
                verdict_llm=verdict_llm,
                embedding_model_ready=brain_ready,
                fail_fast=self._fail_fast_on_missing_dep,
            )
        else:
            logger.info("cognitive_loop_started")

    async def stop(self) -> None:
        """Stop the cognitive loop."""
        logger.info("cognitive_loop_stopped")

    def guard_streaming_segment(self, text: str) -> str:
        """Filter one streamed sentence segment through the output guards.

        Public delegation to :meth:`ActPhase.guard_streaming_segment`
        so the voice cognitive bridge can register the regex-tier
        output/PII guards on the pipeline's per-segment hook without
        reaching into the loop's phase internals. See the ActPhase
        method for the full contract (sync, <1 ms, LLM-classifier tier
        remains batch-only by design).
        """
        return self._act.guard_streaming_segment(text)

    def _maybe_synthetic_dependency_missing(
        self,
        request: CognitiveRequest,
    ) -> ActionResult | None:
        """Return a synthetic :class:`ActionResult` when dependencies are missing.

        Mission C6 §T4.4 — when ``cognitive_degraded_mode_fail_fast`` is
        True AND any dependency the loop needs is absent, short-circuit
        before running any phase. The synthetic result carries the
        ``degraded=True`` + ``error=True`` flags + a metadata payload
        the channels can render as an operator-actionable message.

        Returns ``None`` when the loop is healthy (caller proceeds with
        the normal phase chain) OR when fail-fast is disabled (caller
        runs the loop and individual phases surface their own failures).
        """
        if self._dependency_ready:
            return None
        if not self._fail_fast_on_missing_dep:
            return None
        # C-Σ-002: derive channel + reply target from the perception (the real
        # source), mirroring ActPhase's normal path. The previous code read
        # request.channel / request.request_id / request.message_id — none of
        # which exist on CognitiveRequest — so target_channel was ALWAYS
        # "unknown" and reply_to ALWAYS None.
        perception = request.perception
        target_channel = perception.source or "unknown"
        reply_to = str(perception.metadata.get("reply_to", "")) or None
        logger.info(
            "cognitive.loop.short_circuit_degraded",
            missing_dependencies=list(self._missing_dependencies),
            target_channel=target_channel,
        )
        return ActionResult(
            response_text=(
                "I can't respond right now — the cognitive loop is in degraded "
                "mode (no LLM provider available). Configure a provider via "
                "'sovyx llm setup' or check the dashboard provider settings."
            ),
            target_channel=target_channel,
            reply_to=str(reply_to) if reply_to is not None else None,
            degraded=True,
            error=True,
            metadata={
                "reason": "cognitive_dependency_missing",
                "missing_dependencies": list(self._missing_dependencies),
            },
        )

    @trace_saga("cognitive_loop", kind="cognitive")
    async def process_request(self, request: CognitiveRequest) -> ActionResult:
        """Process a CognitiveRequest through the full loop.

        NEVER raises an exception — always returns ActionResult.
        State machine always resets to IDLE via finally block.

        Mission C6 §T4.4 dependency-gate short-circuit: when
        ``self._dependency_ready`` is False AND ``cognitive_degraded_mode_fail_fast``
        is True, returns a synthetic ActionResult immediately without
        running the perceive→attend→think→act→reflect chain (closes
        forensic finding H5 — the 439-second silent worker spin).

        Wrapped in ``@trace_saga`` so every log emitted during the loop
        (perceive/attend/think/act/reflect, brain calls, LLM calls,
        plugin invokes, brain writes) shares the same ``saga_id`` and
        the operator can reconstruct the full causal chain by filter.
        """
        short_circuit = self._maybe_synthetic_dependency_missing(request)
        if short_circuit is not None:
            return short_circuit

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

        Mission C6 §T4.4 dependency-gate short-circuit applies to the
        streaming path as well — the synthetic ActionResult bypasses the
        ``on_text_chunk`` callback (no chunks fired) so the channel can
        render the operator-actionable error message instead of an empty
        stream.
        """
        short_circuit = self._maybe_synthetic_dependency_missing(request)
        if short_circuit is not None:
            return short_circuit

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
            with (
                t.start_cognitive_span("perceive", mind_id=str(request.mind_id)),
                _measure_phase_latency(CognitivePhase.PERCEIVING),
            ):
                perception = await self._perceive.process(request.perception)

            # ── ATTEND ──
            self._state.transition(CognitivePhase.ATTENDING)
            with (
                t.start_cognitive_span("attend"),
                _measure_phase_latency(CognitivePhase.ATTENDING),
            ):
                should_process = await self._attend.process(perception, str(request.mind_id))

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
            with (
                t.start_cognitive_span("think"),
                _measure_phase_latency(CognitivePhase.THINKING),
            ):
                llm_response, assembled_msgs = await self._think.process(
                    perception=perception,
                    mind_id=request.mind_id,
                    conversation_history=request.conversation_history,
                    person_name=request.person_name,
                )

            # ── ACT ──
            self._state.transition(CognitivePhase.ACTING)
            with (
                t.start_cognitive_span("act"),
                _measure_phase_latency(CognitivePhase.ACTING),
            ):
                action_result = await self._act.process(
                    llm_response,
                    assembled_msgs,
                    perception,
                    str(request.mind_id),
                )

            # Attach LLM metadata to ActionResult for dashboard
            action_result.metadata["model"] = llm_response.model
            action_result.metadata["tokens_in"] = llm_response.tokens_in
            action_result.metadata["tokens_out"] = llm_response.tokens_out
            action_result.metadata["cost_usd"] = llm_response.cost_usd
            action_result.metadata["latency_ms"] = llm_response.latency_ms
            action_result.metadata["provider"] = llm_response.provider

            # W1.2 / G-P1-1 — the ThinkPhase swallows LLM failures and returns a
            # degradation sentinel; mark the result honestly so every channel
            # (and the voice bridge) distinguishes "LLM down" from a real short
            # answer instead of treating the canned fallback as a normal reply.
            if is_degraded_llm_response(
                model=llm_response.model,
                provider=llm_response.provider,
                finish_reason=llm_response.finish_reason,
            ):
                action_result.degraded = True
                action_result.error = True
                action_result.metadata.setdefault("reason", "llm_think_degraded")
                # Attribution — which exception class degraded the turn
                # (stamped by the ThinkPhase fallback). Lets the W1.2 voice
                # bridge signal + dashboards tell a provider outage from a
                # real bug without grepping the log traceback.
                action_result.metadata["degraded_stage"] = "think"
                if llm_response.degraded_reason:
                    action_result.metadata["degraded_reason"] = llm_response.degraded_reason
                if llm_response.degraded_detail:
                    action_result.metadata["degraded_detail"] = llm_response.degraded_detail

            # ── REFLECT ──
            self._state.transition(CognitivePhase.REFLECTING)
            with (
                t.start_cognitive_span("reflect"),
                _measure_phase_latency(CognitivePhase.REFLECTING),
            ):
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
            with (
                t.start_cognitive_span("perceive", mind_id=str(request.mind_id)),
                _measure_phase_latency(CognitivePhase.PERCEIVING),
            ):
                perception = await self._perceive.process(request.perception)

            # ── ATTEND ──
            self._state.transition(CognitivePhase.ATTENDING)
            await _emit_phase("attending", "checking safety")
            with (
                t.start_cognitive_span("attend"),
                _measure_phase_latency(CognitivePhase.ATTENDING),
            ):
                should_process = await self._attend.process(perception, str(request.mind_id))

            if not should_process:
                return ActionResult(
                    response_text="",
                    target_channel=perception.source,
                    filtered=True,
                )

            # ── THINK (streaming) ──
            self._state.transition(CognitivePhase.THINKING)
            await _emit_phase("thinking")
            with (
                t.start_cognitive_span("think"),
                _measure_phase_latency(CognitivePhase.THINKING),
            ):
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
                degraded_reason: str | None = None
                degraded_detail: str | None = None

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

                    # ThinkPhase streaming fallback attribution (only ever
                    # set on the degradation sentinel chunk).
                    if chunk.degraded_reason:
                        degraded_reason = chunk.degraded_reason
                    if chunk.degraded_detail:
                        degraded_detail = chunk.degraded_detail

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
                degraded_reason=degraded_reason,
                degraded_detail=degraded_detail,
            )

            # ── ACT ──
            self._state.transition(CognitivePhase.ACTING)
            tool_detail = ""
            if tool_calls:
                tool_names = [tc.function_name.split(".", 1)[0] for tc in tool_calls]
                tool_detail = f"using {', '.join(tool_names)}"
            await _emit_phase("acting", tool_detail)
            with (
                t.start_cognitive_span("act"),
                _measure_phase_latency(CognitivePhase.ACTING),
            ):
                action_result = await self._act.process(
                    llm_response,
                    assembled_msgs,
                    perception,
                    str(request.mind_id),
                )

            # Attach LLM metadata to ActionResult for dashboard
            action_result.metadata["model"] = final_model
            action_result.metadata["tokens_in"] = tokens_in
            action_result.metadata["tokens_out"] = tokens_out
            action_result.metadata["provider"] = final_provider

            # W1.2 / G-P1-1 — mark the streamed result honestly when the
            # ThinkPhase streaming fallback fired (see the non-streaming path).
            # The degradation text was already forwarded to TTS chunk-by-chunk,
            # so the user still hears it; this only makes the voice/dashboard
            # surface able to tell it was an LLM failure, not a real answer.
            if is_degraded_llm_response(
                model=final_model,
                provider=final_provider,
                finish_reason=finish_reason,
            ):
                action_result.degraded = True
                action_result.error = True
                action_result.metadata.setdefault("reason", "llm_think_degraded")
                # Attribution — mirrors the non-streaming path (see
                # process_request side): exception class + sanitized summary
                # from the ThinkPhase fallback chunk.
                action_result.metadata["degraded_stage"] = "think"
                if llm_response.degraded_reason:
                    action_result.metadata["degraded_reason"] = llm_response.degraded_reason
                if llm_response.degraded_detail:
                    action_result.metadata["degraded_detail"] = llm_response.degraded_detail

            # ── REFLECT ──
            self._state.transition(CognitivePhase.REFLECTING)
            await _emit_phase("reflecting")
            with (
                t.start_cognitive_span("reflect"),
                _measure_phase_latency(CognitivePhase.REFLECTING),
            ):
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
