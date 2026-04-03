"""Sovyx cognitive state machine — OODA-inspired phase transitions.

IDLE → PERCEIVING → ATTENDING → THINKING → ACTING → REFLECTING → IDLE
"""

from __future__ import annotations

from sovyx.engine.errors import CognitiveError
from sovyx.engine.types import CognitivePhase
from sovyx.observability.logging import get_logger

logger = get_logger(__name__)

# Valid transitions for v0.1 (6 core states)
VALID_TRANSITIONS: dict[CognitivePhase, set[CognitivePhase]] = {
    CognitivePhase.IDLE: {CognitivePhase.PERCEIVING},
    CognitivePhase.PERCEIVING: {CognitivePhase.ATTENDING, CognitivePhase.IDLE},
    CognitivePhase.ATTENDING: {CognitivePhase.THINKING, CognitivePhase.IDLE},
    CognitivePhase.THINKING: {CognitivePhase.ACTING},
    CognitivePhase.ACTING: {CognitivePhase.REFLECTING},
    CognitivePhase.REFLECTING: {CognitivePhase.IDLE},
}


class CognitiveStateMachine:
    """Cognitive phase state machine with validated transitions.

    Enforces the OODA loop: only valid transitions are allowed.
    reset() returns to IDLE unconditionally (error recovery).
    """

    def __init__(self) -> None:
        self._current = CognitivePhase.IDLE

    @property
    def current(self) -> CognitivePhase:
        """Current cognitive phase."""
        return self._current

    def transition(self, target: CognitivePhase) -> None:
        """Transition to a new phase.

        Args:
            target: Target phase.

        Raises:
            CognitiveError: If transition is invalid.
        """
        valid = VALID_TRANSITIONS.get(self._current, set())
        if target not in valid:
            msg = (
                f"Invalid transition: {self._current.value} → {target.value}. "
                f"Valid targets: {{{', '.join(s.value for s in valid)}}}"
            )
            raise CognitiveError(msg)

        logger.debug(
            "cognitive_transition",
            from_phase=self._current.value,
            to_phase=target.value,
        )
        self._current = target

    def reset(self) -> None:
        """Reset to IDLE unconditionally.

        Used in error recovery to prevent deadlocks.
        CognitiveLoop.process_request() MUST use finally: reset()
        to ensure state machine never gets stuck.
        """
        previous = self._current
        self._current = CognitivePhase.IDLE
        if previous != CognitivePhase.IDLE:
            logger.info(
                "cognitive_reset",
                from_phase=previous.value,
            )
