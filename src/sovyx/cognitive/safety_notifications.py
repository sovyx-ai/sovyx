"""Sovyx Safety Notifications — alert owners on safety escalations.

Sends notifications when:
- Alert threshold reached (10 blocks in 10 min from same source)
- Rate limit activated (5 blocks in 5 min)
- Multi-turn injection detected

Notifications go through the configured notification callback.
Default: structured log. Can be wired to channels (Telegram, email, etc.)
via bootstrap.

Design:
    - Debounce: max 1 notification per source per 15 minutes
    - Format: concise, actionable (source, count, category breakdown)
    - Privacy: NO content, only metadata
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Protocol

from sovyx.observability.logging import get_logger

logger = get_logger(__name__)

# ── Debounce ────────────────────────────────────────────────────────────
_DEBOUNCE_SEC = 900.0  # 15 minutes


class NotificationSink(Protocol):
    """Protocol for notification delivery."""

    def send(self, message: str) -> None:
        """Send a notification message."""
        ...


class LogNotificationSink:
    """Default sink: log to structured logger."""

    def send(self, message: str) -> None:
        """Log notification as warning."""
        logger.warning("safety_notification", message=message)


@dataclass(slots=True)
class SafetyAlert:
    """A safety alert notification.

    Attributes:
        source_id: Who triggered the alert.
        block_count: Number of blocks in window.
        level: Escalation level (rate_limited, alerted).
        timestamp: When the alert was generated.
    """

    source_id: str
    block_count: int
    level: str
    timestamp: float = field(default_factory=time.time)


class SafetyNotifier:
    """Manages safety alert notifications with debounce.

    Args:
        sink: Where to send notifications. Defaults to log.
        debounce_sec: Minimum seconds between notifications per source.
    """

    def __init__(
        self,
        sink: NotificationSink | None = None,
        debounce_sec: float = _DEBOUNCE_SEC,
    ) -> None:
        self._sink: NotificationSink = sink or LogNotificationSink()
        self._debounce_sec = debounce_sec
        self._last_notified: dict[str, float] = {}
        self._alert_count = 0

    def notify_escalation(
        self,
        source_id: str,
        block_count: int,
        level: str = "alerted",
    ) -> bool:
        """Send escalation notification (with debounce).

        Args:
            source_id: Source that triggered escalation.
            block_count: Number of blocks.
            level: Escalation level.

        Returns:
            True if notification was sent, False if debounced.
        """
        now = time.time()
        last = self._last_notified.get(source_id, 0.0)

        if now - last < self._debounce_sec:
            logger.debug(
                "safety_notification_debounced",
                source=source_id,
                seconds_since_last=round(now - last),
            )
            return False

        message = (
            f"⚠️ Safety Escalation: {level.upper()}\n"
            f"Source: {source_id}\n"
            f"Blocks: {block_count} in last 10 min\n"
            f"Action: {'Rate limited' if level == 'rate_limited' else 'Owner alert'}"
        )

        self._sink.send(message)
        self._last_notified[source_id] = now
        self._alert_count += 1

        logger.info(
            "safety_notification_sent",
            source=source_id,
            level=level,
            block_count=block_count,
        )

        return True

    @property
    def alert_count(self) -> int:
        """Total alerts sent."""
        return self._alert_count

    def clear(self) -> None:
        """Reset state (for testing)."""
        self._last_notified.clear()
        self._alert_count = 0


# ── Notifier accessor (delegates to SafetyContainer) ───────────────────


def get_notifier() -> SafetyNotifier:
    """Get the SafetyNotifier from the global container.

    Returns:
        The SafetyNotifier instance managed by SafetyContainer.
    """
    from sovyx.cognitive.safety_container import get_safety_container

    return get_safety_container().notifier


def setup_notifier(sink: NotificationSink | None = None) -> SafetyNotifier:
    """Initialize a new SafetyNotifier in the global container.

    Args:
        sink: Notification delivery sink (default: log).

    Returns:
        The newly created SafetyNotifier.
    """
    from sovyx.cognitive.safety_container import get_safety_container

    container = get_safety_container()
    container.notifier = SafetyNotifier(sink=sink)
    return container.notifier
