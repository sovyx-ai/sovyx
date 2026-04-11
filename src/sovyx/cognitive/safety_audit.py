"""Sovyx safety audit trail — structured logging and metrics for safety events.

Records every safety filter action (block, redact, replace) with metadata
for compliance, debugging, and dashboard display. Original content is NEVER
logged (privacy).

Designed as a singleton service injected into AttendPhase and OutputGuard.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum, unique
from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger
from sovyx.observability.metrics import get_metrics

if TYPE_CHECKING:
    from sovyx.cognitive.safety_patterns import FilterMatch

logger = get_logger(__name__)


@unique
class FilterDirection(Enum):
    """Direction of the filtered content."""

    INPUT = "input"
    OUTPUT = "output"


@unique
class FilterAction(Enum):
    """Action taken on filtered content."""

    BLOCKED = "blocked"
    REDACTED = "redacted"
    REPLACED = "replaced"


@dataclass(frozen=True, slots=True)
class SafetyEvent:
    """A recorded safety filter event (no original content — privacy).

    Attributes:
        timestamp: Unix timestamp of the event.
        direction: Input (user message) or output (LLM response).
        action: What was done (blocked, redacted, replaced).
        category: Pattern category that triggered the filter.
        tier: Filter tier that was active.
        pattern_description: Human-readable pattern description.
    """

    timestamp: float
    direction: str  # "input" | "output"
    action: str  # "blocked" | "redacted" | "replaced"
    category: str
    tier: str
    pattern_description: str


@dataclass
class SafetyStats:
    """Aggregated safety statistics for dashboard display."""

    total_blocks_24h: int = 0
    total_blocks_7d: int = 0
    total_blocks_30d: int = 0
    blocks_by_category: dict[str, int] = field(default_factory=dict)
    blocks_by_direction: dict[str, int] = field(default_factory=dict)
    recent_events: list[dict[str, object]] = field(default_factory=list)


class SafetyAuditTrail:
    """Records and queries safety filter events.

    Thread-safe via deque. Events are stored in-memory with a max size
    to prevent unbounded growth. For persistence, export to database
    in a future version.

    Args:
        max_events: Maximum events to keep in memory (FIFO).
    """

    def __init__(self, max_events: int = 10000) -> None:
        self._events: deque[SafetyEvent] = deque(maxlen=max_events)

    def record(
        self,
        direction: FilterDirection,
        action: FilterAction,
        match: FilterMatch,
    ) -> SafetyEvent:
        """Record a safety filter event.

        Args:
            direction: Whether this was input or output filtering.
            action: What action was taken.
            match: The FilterMatch that triggered the event.

        Returns:
            The recorded SafetyEvent.
        """
        category = match.category.value if match.category else "unknown"
        tier = match.tier.value if match.tier else "unknown"
        description = match.pattern.description if match.pattern else "unknown"

        event = SafetyEvent(
            timestamp=time.time(),
            direction=direction.value,
            action=action.value,
            category=category,
            tier=tier,
            pattern_description=description,
        )
        self._events.append(event)

        # Structured log (no content — privacy)
        logger.info(
            "safety_filter_event",
            direction=event.direction,
            action=event.action,
            category=event.category,
            tier=event.tier,
            pattern=event.pattern_description,
        )

        # Metrics
        m = get_metrics()
        m.safety_blocks.add(
            1,
            {
                "direction": event.direction,
                "tier": event.tier,
                "category": event.category,
            },
        )

        return event

    def get_stats(self) -> SafetyStats:
        """Compute aggregated statistics from recorded events.

        Returns:
            SafetyStats with 24h/7d/30d totals, category breakdown,
            and last 10 events (no content).
        """
        now = time.time()
        day = 86400
        stats = SafetyStats()
        category_counts: dict[str, int] = {}
        direction_counts: dict[str, int] = {}

        for event in self._events:
            age = now - event.timestamp

            if age <= day:
                stats.total_blocks_24h += 1
            if age <= 7 * day:
                stats.total_blocks_7d += 1
            if age <= 30 * day:
                stats.total_blocks_30d += 1

            category_counts[event.category] = category_counts.get(event.category, 0) + 1
            direction_counts[event.direction] = direction_counts.get(event.direction, 0) + 1

        stats.blocks_by_category = category_counts
        stats.blocks_by_direction = direction_counts

        # Last 10 events (most recent first)
        recent = list(self._events)[-10:]
        recent.reverse()
        stats.recent_events = [
            {
                "timestamp": e.timestamp,
                "direction": e.direction,
                "action": e.action,
                "category": e.category,
                "tier": e.tier,
            }
            for e in recent
        ]

        return stats

    @property
    def event_count(self) -> int:
        """Total events in memory."""
        return len(self._events)

    def clear(self) -> None:
        """Clear all events (for testing)."""
        self._events.clear()


# ── Module-level singleton ─────────────────────────────────────────────

_audit_trail: SafetyAuditTrail | None = None


def get_audit_trail() -> SafetyAuditTrail:
    """Get the global SafetyAuditTrail instance."""
    global _audit_trail  # noqa: PLW0603
    if _audit_trail is None:
        _audit_trail = SafetyAuditTrail()
    return _audit_trail


def setup_audit_trail(max_events: int = 10000) -> SafetyAuditTrail:
    """Initialize the global SafetyAuditTrail."""
    global _audit_trail  # noqa: PLW0603
    _audit_trail = SafetyAuditTrail(max_events=max_events)
    return _audit_trail
