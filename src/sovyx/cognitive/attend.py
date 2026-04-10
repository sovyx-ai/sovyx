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
_BLOCKED_PATTERNS_STANDARD: frozenset[str] = frozenset(
    {
        "how to make a bomb",
        "how to hack",
        "how to kill",
        "self-harm instructions",
    }
)

_BLOCKED_PATTERNS_CHILD_SAFE: frozenset[str] = frozenset(
    {
        "violence",
        "weapons",
        "drugs",
        "gambling",
        "adult content",
        *_BLOCKED_PATTERNS_STANDARD,
    }
)


def _resolve_blocked(safety: SafetyConfig) -> frozenset[str]:
    """Resolve the blocked pattern set from current safety config state."""
    if safety.child_safe_mode:
        return _BLOCKED_PATTERNS_CHILD_SAFE
    if safety.content_filter != "none":
        return _BLOCKED_PATTERNS_STANDARD
    return frozenset()


class AttendPhase:
    """Filter perceptions by priority and safety.

    - SafetyCheck: content passes safety filter (re-evaluated per call)
    - PriorityCheck: priority sufficient to process

    The safety config is read dynamically on each ``process()`` call so
    that runtime changes via the dashboard take effect immediately
    without restarting the engine.
    """

    def __init__(self, safety_config: SafetyConfig) -> None:
        self._safety = safety_config

    async def process(self, perception: Perception) -> bool:
        """Check if perception should be processed.

        Args:
            perception: Enriched perception from PerceivePhase.

        Returns:
            True if perception passes filters, False if filtered.
        """
        # Resolve blocked patterns dynamically from current safety state
        blocked = _resolve_blocked(self._safety)

        # Safety check
        lower = perception.content.lower()
        for pattern in blocked:
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
