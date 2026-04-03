"""Sovyx CognitiveLoop — the heart of the system.

Perceive → Attend → Think → Act → Reflect.
Orchestrates all phases, manages state machine, emits events.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sovyx.cognitive.act import ActionResult
from sovyx.engine.types import CognitivePhase
from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from sovyx.cognitive.act import ActPhase
    from sovyx.cognitive.attend import AttendPhase
    from sovyx.cognitive.gate import CognitiveRequest
    from sovyx.cognitive.perceive import PerceivePhase
    from sovyx.cognitive.reflect import ReflectPhase
    from sovyx.cognitive.state import CognitiveStateMachine
    from sovyx.cognitive.think import ThinkPhase
    from sovyx.engine.events import EventBus

logger = get_logger(__name__)


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
    ) -> None:
        self._state = state_machine
        self._perceive = perceive
        self._attend = attend
        self._think = think
        self._act = act
        self._reflect = reflect
        self._events = event_bus

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
        try:
            # ── PERCEIVE ──
            self._state.transition(CognitivePhase.PERCEIVING)
            perception = await self._perceive.process(request.perception)

            # ── ATTEND ──
            self._state.transition(CognitivePhase.ATTENDING)
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
            llm_response, assembled_msgs = await self._think.process(
                perception=perception,
                mind_id=request.mind_id,
                conversation_history=request.conversation_history,
                person_name=request.person_name,
            )

            # ── ACT ──
            self._state.transition(CognitivePhase.ACTING)
            action_result = await self._act.process(llm_response, assembled_msgs, perception)

            # ── REFLECT ──
            self._state.transition(CognitivePhase.REFLECTING)
            try:
                await self._reflect.process(
                    perception=perception,
                    response=llm_response,
                    mind_id=request.mind_id,
                    conversation_id=request.conversation_id,
                )
            except Exception:
                # Reflect is best-effort — user already got response
                logger.warning("reflect_phase_failed", exc_info=True)

            logger.debug(
                "cognitive_loop_complete",
                perception_id=perception.id,
                degraded=action_result.degraded,
            )
            return action_result

        except Exception as e:
            logger.exception("cognitive_loop_error", error=str(e))
            return ActionResult(
                response_text="Something went wrong.",
                target_channel=request.perception.source,
                error=True,
            )

        finally:
            self._state.reset()
