"""Tests for sovyx.llm.circuit — CircuitBreaker."""

from __future__ import annotations

import time

from sovyx.llm.circuit import CircuitBreaker


class TestCircuitBreaker:
    """Circuit breaker state transitions."""

    def test_starts_closed(self) -> None:
        cb = CircuitBreaker()
        assert cb.state == "closed"
        assert cb.can_call() is True

    def test_opens_after_threshold(self) -> None:
        cb = CircuitBreaker(failure_threshold=3)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == "closed"
        cb.record_failure()
        assert cb.state == "open"
        assert cb.can_call() is False

    def test_half_open_after_timeout(self) -> None:
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout_s=1)
        cb.record_failure()
        assert cb.state == "open"

        # Simulate time passing
        cb._last_failure_time = time.monotonic() - 2
        assert cb.state == "half_open"
        assert cb.can_call() is True

    def test_success_resets_to_closed(self) -> None:
        cb = CircuitBreaker(failure_threshold=1)
        cb.record_failure()
        assert cb.state == "open"

        # Simulate recovery
        cb._last_failure_time = time.monotonic() - 100
        assert cb.state == "half_open"

        cb.record_success()
        assert cb.state == "closed"
        assert cb.can_call() is True

    def test_failures_below_threshold(self) -> None:
        cb = CircuitBreaker(failure_threshold=5)
        for _ in range(4):
            cb.record_failure()
        assert cb.state == "closed"

    def test_success_resets_count(self) -> None:
        cb = CircuitBreaker(failure_threshold=3)
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        cb.record_failure()
        # Only 1 failure after reset, not 3
        assert cb.state == "closed"
