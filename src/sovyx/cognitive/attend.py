"""Sovyx AttendPhase — filter perceptions by priority and safety.

Second phase: decides if a perception should be processed or filtered.
Uses tiered regex patterns from ``safety_patterns`` module.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sovyx.cognitive.safety_audit import FilterAction, FilterDirection, get_audit_trail
from sovyx.cognitive.safety_escalation import get_escalation_tracker
from sovyx.cognitive.safety_patterns import check_content
from sovyx.observability.logging import get_logger
from sovyx.observability.metrics import get_metrics

if TYPE_CHECKING:
    from sovyx.cognitive.perceive import Perception
    from sovyx.mind.config import SafetyConfig

logger = get_logger(__name__)


class AttendPhase:
    """Filter perceptions by priority and safety.

    - SafetyCheck: content passes tiered regex filter (re-evaluated per call)
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
        # Escalation check: reject if source is rate-limited
        tracker = get_escalation_tracker()
        if tracker.is_rate_limited(perception.source):
            logger.warning(
                "perception_rate_limited",
                perception_id=perception.id,
                source=perception.source,
            )
            return False

        # Safety check via tiered regex patterns (with latency measurement)
        m = get_metrics()
        with m.measure_latency(m.safety_filter_latency, {"direction": "input"}):
            result = check_content(perception.content, self._safety)

        if result.matched:
            logger.warning(
                "perception_filtered_safety",
                perception_id=perception.id,
                reason="blocked_content",
                category=result.category.value if result.category else "unknown",
                tier=result.tier.value if result.tier else "unknown",
            )
            get_audit_trail().record(
                direction=FilterDirection.INPUT,
                action=FilterAction.BLOCKED,
                match=result,
            )
            tracker.record_block(perception.source)
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
