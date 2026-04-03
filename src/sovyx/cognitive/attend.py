"""Sovyx AttendPhase — filter perceptions by priority and safety.

Second phase: decides if a perception should be processed or filtered.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from sovyx.cognitive.perceive import Perception
    from sovyx.mind.config import SafetyConfig

logger = get_logger(__name__)

# Blocked content patterns (basic v0.1 safety filter)
_BLOCKED_PATTERNS_STANDARD: frozenset[str] = frozenset({
    "how to make a bomb",
    "how to hack",
    "how to kill",
    "self-harm instructions",
})

_BLOCKED_PATTERNS_CHILD_SAFE: frozenset[str] = frozenset({
    "violence",
    "weapons",
    "drugs",
    "gambling",
    "adult content",
    *_BLOCKED_PATTERNS_STANDARD,
})


class AttendPhase:
    """Filter perceptions by priority and safety.

    - SafetyCheck: content passes safety filter
    - PriorityCheck: priority sufficient to process
    """

    def __init__(self, safety_config: SafetyConfig) -> None:
        self._safety = safety_config
        self._blocked = (
            _BLOCKED_PATTERNS_CHILD_SAFE
            if safety_config.child_safe_mode
            else _BLOCKED_PATTERNS_STANDARD
            if safety_config.content_filter != "none"
            else frozenset()
        )

    async def process(self, perception: Perception) -> bool:
        """Check if perception should be processed.

        Args:
            perception: Enriched perception from PerceivePhase.

        Returns:
            True if perception passes filters, False if filtered.
        """
        # Safety check
        lower = perception.content.lower()
        for pattern in self._blocked:
            if pattern in lower:
                logger.warning(
                    "perception_filtered_safety",
                    perception_id=perception.id,
                    reason="blocked_content",
                )
                return False

        # Priority check (v0.1: accept all priorities)
        if perception.priority < 0:
            logger.debug(
                "perception_filtered_priority",
                perception_id=perception.id,
                priority=perception.priority,
            )
            return False

        logger.debug(
            "perception_accepted",
            perception_id=perception.id,
        )
        return True
