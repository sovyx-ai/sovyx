"""Sovyx safety bypass escalation tracker.

Tracks consecutive safety blocks per source and escalates:
- 3 blocks in 5min → warning log
- 5 blocks in 5min → rate limit (cooldown)
- 10 blocks in 10min → owner alert

Thread-safe via simple dict + deque (GIL-protected).
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from enum import StrEnum, unique

from sovyx.observability.logging import get_logger

logger = get_logger(__name__)

# ── Thresholds ──────────────────────────────────────────────────────────

WARN_THRESHOLD = 3
WARN_WINDOW_SEC = 300  # 5 min

RATE_LIMIT_THRESHOLD = 5
RATE_LIMIT_WINDOW_SEC = 300  # 5 min

ALERT_THRESHOLD = 10
ALERT_WINDOW_SEC = 600  # 10 min

COOLDOWN_SEC = 900  # 15 min — reset after no blocks


@unique
class EscalationLevel(StrEnum):
    """Current escalation state for a source."""

    NONE = "none"
    WARNING = "warning"
    RATE_LIMITED = "rate_limited"
    ALERTED = "alerted"


@dataclass(slots=True)
class SourceState:
    """Tracking state for a single source (session/IP)."""

    timestamps: deque[float]
    level: EscalationLevel
    last_block: float

    def __init__(self) -> None:
        self.timestamps: deque[float] = deque(maxlen=1000)
        self.level = EscalationLevel.NONE
        self.last_block = 0.0


class SafetyEscalationTracker:
    """Tracks safety blocks per source and manages escalation.

    Args:
        on_alert: Optional callback when alert threshold is reached.
            Receives source_id and block count.
    """

    def __init__(
        self,
        on_alert: object | None = None,
    ) -> None:
        self._sources: dict[str, SourceState] = {}
        self._on_alert = on_alert

    def record_block(self, source_id: str) -> EscalationLevel:
        """Record a safety block for a source.

        Args:
            source_id: Unique identifier (session ID, IP, etc.).

        Returns:
            Current escalation level after this block.
        """
        now = time.time()
        state = self._sources.get(source_id)

        if state is None:
            state = SourceState()
            self._sources[source_id] = state

        # Check cooldown: if last block was >15min ago, reset
        if state.last_block > 0 and (now - state.last_block) > COOLDOWN_SEC:
            state.timestamps.clear()
            state.level = EscalationLevel.NONE

        state.timestamps.append(now)
        state.last_block = now

        # Prune old timestamps (keep only last 10 min)
        cutoff = now - ALERT_WINDOW_SEC
        while state.timestamps and state.timestamps[0] < cutoff:
            state.timestamps.popleft()

        count = len(state.timestamps)

        # Evaluate escalation (highest level wins)
        if count >= ALERT_THRESHOLD:
            if state.level != EscalationLevel.ALERTED:
                state.level = EscalationLevel.ALERTED
                logger.warning(
                    "safety_escalation_alert",
                    source=source_id,
                    blocks=count,
                    window_sec=ALERT_WINDOW_SEC,
                )
                if callable(self._on_alert):
                    self._on_alert(source_id, count)
                try:
                    from sovyx.cognitive.safety_notifications import get_notifier

                    get_notifier().notify_escalation(
                        source_id,
                        count,
                        "alerted",
                    )
                except Exception:  # noqa: BLE001
                    pass
        elif count >= RATE_LIMIT_THRESHOLD:
            # Check within 5-min window
            recent = sum(1 for t in state.timestamps if t > now - RATE_LIMIT_WINDOW_SEC)
            if recent >= RATE_LIMIT_THRESHOLD and state.level not in (
                EscalationLevel.RATE_LIMITED,
                EscalationLevel.ALERTED,
            ):
                state.level = EscalationLevel.RATE_LIMITED
                logger.warning(
                    "safety_escalation_rate_limited",
                    source=source_id,
                    blocks=recent,
                    window_sec=RATE_LIMIT_WINDOW_SEC,
                )
                try:
                    from sovyx.cognitive.safety_notifications import get_notifier

                    get_notifier().notify_escalation(
                        source_id,
                        recent,
                        "rate_limited",
                    )
                except Exception:  # noqa: BLE001
                    pass
        elif count >= WARN_THRESHOLD:
            recent = sum(1 for t in state.timestamps if t > now - WARN_WINDOW_SEC)
            if recent >= WARN_THRESHOLD and state.level == EscalationLevel.NONE:
                state.level = EscalationLevel.WARNING
                logger.warning(
                    "safety_escalation_warning",
                    source=source_id,
                    blocks=recent,
                    window_sec=WARN_WINDOW_SEC,
                )

        return state.level

    def get_level(self, source_id: str) -> EscalationLevel:
        """Get current escalation level for a source."""
        state = self._sources.get(source_id)
        if state is None:
            return EscalationLevel.NONE

        # Check cooldown
        now = time.time()
        if state.last_block > 0 and (now - state.last_block) > COOLDOWN_SEC:
            state.timestamps.clear()
            state.level = EscalationLevel.NONE
            return EscalationLevel.NONE

        return state.level

    def is_rate_limited(self, source_id: str) -> bool:
        """Check if a source is currently rate-limited."""
        level = self.get_level(source_id)
        return level in (EscalationLevel.RATE_LIMITED, EscalationLevel.ALERTED)

    def clear(self) -> None:
        """Clear all tracking state (for testing)."""
        self._sources.clear()


# ── Tracker accessor (delegates to SafetyContainer) ────────────────────


def get_escalation_tracker() -> SafetyEscalationTracker:
    """Get the SafetyEscalationTracker from the global container.

    Returns:
        The SafetyEscalationTracker instance managed by SafetyContainer.
    """
    from sovyx.cognitive.safety_container import get_safety_container

    return get_safety_container().escalation_tracker
