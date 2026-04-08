"""Sovyx CognitiveLoop — the heart of the system.

Perceive → Attend → Think → Act → Reflect.
Orchestrates all phases, manages state machine, emits events.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sovyx.cognitive.act import ActionResult
from sovyx.engine.errors import (
    CostLimitExceededError,
    ProviderUnavailableError,
)
from sovyx.engine.types import CognitivePhase
from sovyx.observability.logging import get_logger
from sovyx.observability.metrics import MetricsRegistry, get_metrics
from sovyx.observability.tracing import SovyxTracer, get_tracer

if TYPE_CHECKING:
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
    if isinstance(exc, CostLimitExceededError):
        return "I've reached my conversation budget limit. Please try again later."
    if isinstance(exc, ProviderUnavailableError):
        return "I'm having trouble connecting to my AI provider. Please try again in a moment."
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

    async def process_request(self, request: CognitiveRequest) -> ActionResult:
        """Process a CognitiveRequest through the full loop.

        NEVER raises an exception — always returns ActionResult.
        State machine always resets to IDLE via finally block.
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
                except Exception:
                    logger.warning("reflect_phase_failed", exc_info=True)

                # Decay working memory after reflect — concepts not
                # re-activated will gradually fade, keeping star topology
                # focused on recent/relevant concepts.
                try:
                    if self._brain is not None:
                        self._brain.decay_working_memory()
                except Exception:
                    logger.warning("working_memory_decay_failed", exc_info=True)

            m.messages_processed.add(1, {"mind_id": str(request.mind_id)})

            logger.debug(
                "cognitive_loop_complete",
                perception_id=perception.id,
                degraded=action_result.degraded,
            )
            return action_result

        except Exception as e:
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
