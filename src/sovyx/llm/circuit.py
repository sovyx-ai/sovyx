"""Sovyx CircuitBreaker — per-provider failure isolation."""

from __future__ import annotations

import time
from typing import Literal

from sovyx.observability.logging import get_logger

logger = get_logger(__name__)


class CircuitBreaker:
    """Circuit breaker per-provider.

    States: CLOSED (normal) → OPEN (failing) → HALF_OPEN (testing)
    Thresholds: N failures → OPEN. Timeout → HALF_OPEN. 1 success → CLOSED.
    """

    def __init__(
        self,
        failure_threshold: int = 3,
        recovery_timeout_s: int = 60,
    ) -> None:
        self._failure_threshold = failure_threshold
        self._recovery_timeout_s = recovery_timeout_s
        self._failure_count: int = 0
        self._state: Literal["closed", "open", "half_open"] = "closed"
        self._last_failure_time: float = 0.0

    @property
    def state(self) -> Literal["closed", "open", "half_open"]:
        """Current circuit state."""
        if self._state == "open":
            elapsed = time.monotonic() - self._last_failure_time
            if elapsed >= self._recovery_timeout_s:
                self._state = "half_open"
        return self._state

    def can_call(self) -> bool:
        """Check if calls are allowed.

        Returns:
            True if CLOSED or HALF_OPEN.
        """
        current = self.state
        return current != "open"

    def record_success(self) -> None:
        """Record a successful call. Resets to CLOSED."""
        self._failure_count = 0
        self._state = "closed"

    def record_failure(self) -> None:
        """Record a failed call. May transition to OPEN."""
        self._failure_count += 1
        self._last_failure_time = time.monotonic()

        if self._failure_count >= self._failure_threshold:
            self._state = "open"
            logger.warning(
                "circuit_opened",
                failures=self._failure_count,
                threshold=self._failure_threshold,
            )
