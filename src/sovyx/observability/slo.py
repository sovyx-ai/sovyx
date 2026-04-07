"""SLO monitoring — Service Level Objectives with sliding window and burn rate alerting.

Implements SLO tracking for 5 core objectives (SPE-026 §6, IMPL-015 §2.4):

1. Brain Search Latency: p95 < 100ms
2. Response Time: p95 < 3s
3. Uptime/Availability: > 99.5%
4. Error Rate: < 1%
5. Cost per Message: < $0.01

Each SLO has a sliding event window and burn rate alerting following
the Google SRE multi-window approach.

Usage::

    from sovyx.observability.slo import SLOMonitor, create_default_monitor

    monitor = create_default_monitor()
    monitor.record_event("brain_search", success=True, value_ms=42.0)
    monitor.record_event("response_time", success=True, value_ms=1200.0)

    report = monitor.get_report()
    for slo_name, status in report.items():
        print(f"{slo_name}: {status.status} (burn_rate={status.burn_rate_1h:.1f})")
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = get_logger(__name__)


# ── Enums ───────────────────────────────────────────────────────────────────


class SLOStatus(Enum):
    """Current SLO health status."""

    MET = "met"
    WARNING = "warning"
    BREACHED = "breached"


class AlertSeverity(Enum):
    """Alert severity based on burn rate (Google SRE multi-window)."""

    NONE = "none"
    TICKET = "ticket"  # Slow burn — file a ticket
    PAGE = "page"  # Fast burn — wake someone up


# ── Data Classes ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SLODefinition:
    """Definition of a single Service Level Objective.

    Attributes:
        name: Human-readable SLO name.
        description: Brief description of what this SLO measures.
        target: Target value as a fraction (e.g., 0.995 for 99.5%).
        threshold: The absolute threshold value for per-event success/failure.
        unit: Unit string for display (e.g., 'ms', '%', 'USD').
        window_days: Error budget window in days (default 30).
    """

    name: str
    description: str
    target: float
    threshold: float
    unit: str
    window_days: int = 30

    @property
    def error_budget(self) -> float:
        """Fraction of events that can fail within budget."""
        return 1.0 - self.target


@dataclass(frozen=True)
class BurnRateAlertRule:
    """Multi-window burn rate alert rule (Google SRE Workbook).

    Both the long and short window must exceed the burn rate threshold
    for the alert to fire, reducing false positives.
    """

    severity: AlertSeverity
    long_window_minutes: int
    short_window_minutes: int
    burn_rate_threshold: float

    def check(
        self,
        long_window_error_rate: float,
        short_window_error_rate: float,
        error_budget: float,
    ) -> bool:
        """Return True if both windows exceed the burn rate threshold."""
        if error_budget <= 0:
            return False
        long_burn = long_window_error_rate / error_budget
        short_burn = short_window_error_rate / error_budget
        return (
            long_burn >= self.burn_rate_threshold
            and short_burn >= self.burn_rate_threshold
        )


@dataclass(frozen=True)
class SLOEvent:
    """A single recorded event for SLO tracking.

    Attributes:
        timestamp: Unix timestamp of the event.
        success: Whether the event met the SLO threshold.
        value: The measured value (latency in ms, cost in USD, etc.).
    """

    timestamp: float
    success: bool
    value: float


@dataclass
class SLOReport:
    """Status report for a single SLO.

    Attributes:
        name: SLO name.
        status: Current SLO health.
        target: Target value (fraction).
        current_rate: Actual success rate in the observation window.
        burn_rate_1h: Burn rate over the last 1 hour.
        error_budget_remaining_pct: Remaining error budget as %.
        alert_severity: Highest triggered alert severity.
        event_count: Total events in the window.
        unit: Display unit for the threshold.
        threshold: The absolute threshold value.
    """

    name: str
    status: SLOStatus
    target: float
    current_rate: float
    burn_rate_1h: float
    error_budget_remaining_pct: float
    alert_severity: AlertSeverity
    event_count: int
    unit: str
    threshold: float


# ── Constants ───────────────────────────────────────────────────────────────

# Standard multi-window alert rules (Google SRE Workbook)
STANDARD_ALERT_RULES: tuple[BurnRateAlertRule, ...] = (
    BurnRateAlertRule(AlertSeverity.PAGE, 60, 5, 14.4),  # Fast burn: 1h/5m
    BurnRateAlertRule(AlertSeverity.PAGE, 360, 30, 6.0),  # Medium burn: 6h/30m
    BurnRateAlertRule(AlertSeverity.TICKET, 4320, 360, 1.0),  # Slow burn: 3d/6h
)

# Default 5 SLO definitions
DEFAULT_SLOS: dict[str, SLODefinition] = {
    "brain_search": SLODefinition(
        name="Brain Search Latency",
        description="p95 brain search latency < 100ms",
        target=0.95,
        threshold=100.0,
        unit="ms",
    ),
    "response_time": SLODefinition(
        name="Response Time",
        description="p95 response time < 3s",
        target=0.95,
        threshold=3000.0,
        unit="ms",
    ),
    "availability": SLODefinition(
        name="Uptime / Availability",
        description="Availability > 99.5%",
        target=0.995,
        threshold=1.0,
        unit="bool",
    ),
    "error_rate": SLODefinition(
        name="Error Rate",
        description="Error rate < 1%",
        target=0.99,
        threshold=1.0,
        unit="bool",
    ),
    "cost_per_message": SLODefinition(
        name="Cost per Message",
        description="Average cost per message < $0.01",
        target=0.95,
        threshold=0.01,
        unit="USD",
    ),
}


# ── SLO Tracker (per-SLO) ──────────────────────────────────────────────────


class SLOTracker:
    """Tracks events and burn rate for a single SLO.

    Uses a bounded deque as a sliding window ring buffer.
    Thread-safe for single-threaded async (not multi-threaded).

    Args:
        definition: The SLO this tracker monitors.
        alert_rules: Multi-window alert rules to evaluate.
        max_events: Maximum events to retain (oldest are evicted).
    """

    def __init__(
        self,
        definition: SLODefinition,
        alert_rules: Sequence[BurnRateAlertRule] | None = None,
        max_events: int = 100_000,
    ) -> None:
        self._definition = definition
        self._alert_rules = alert_rules or list(STANDARD_ALERT_RULES)
        self._events: deque[SLOEvent] = deque(maxlen=max_events)

    @property
    def definition(self) -> SLODefinition:
        """Return the SLO definition."""
        return self._definition

    @property
    def event_count(self) -> int:
        """Total events in the window."""
        return len(self._events)

    def record_event(self, success: bool, value: float) -> None:
        """Record a success/failure event with measured value.

        Args:
            success: Whether this event met the SLO threshold.
            value: The raw measured value (e.g., latency in ms, cost in USD).
        """
        self._events.append(SLOEvent(
            timestamp=time.monotonic(),
            success=success,
            value=value,
        ))

    def error_rate_in_window(self, window_seconds: float) -> float:
        """Calculate error rate within a time window.

        Args:
            window_seconds: How far back to look (in seconds).

        Returns:
            Error rate as a fraction [0.0, 1.0]. Returns 0.0 if no events.
        """
        now = time.monotonic()
        cutoff = now - window_seconds
        total = 0
        errors = 0

        for event in reversed(self._events):
            if event.timestamp < cutoff:
                break
            total += 1
            if not event.success:
                errors += 1

        return errors / total if total > 0 else 0.0

    def success_rate(self) -> float:
        """Overall success rate across all events in the buffer.

        Returns:
            Success rate as a fraction [0.0, 1.0]. Returns 1.0 if no events.
        """
        if not self._events:
            return 1.0
        successes = sum(1 for e in self._events if e.success)
        return successes / len(self._events)

    def get_burn_rate(self, window_minutes: int = 60) -> float:
        """Calculate burn rate for a given time window.

        Burn rate 1.0 means consuming error budget at exactly the pace
        to exhaust it over the SLO window. Burn rate 2.0 = 2x as fast.

        Args:
            window_minutes: Window size in minutes (default 60).

        Returns:
            Burn rate as a float. Returns 0.0 if no error budget or no data.
        """
        error_budget = self._definition.error_budget
        if error_budget <= 0:
            return 0.0
        error_rate = self.error_rate_in_window(window_minutes * 60)
        return error_rate / error_budget

    def get_error_budget_remaining_pct(self) -> float:
        """Get remaining error budget as a percentage [0.0, 100.0].

        Calculated over the full SLO window (window_days).
        """
        error_budget = self._definition.error_budget
        if error_budget <= 0:
            return 100.0
        window_seconds = self._definition.window_days * 86400
        error_rate = self.error_rate_in_window(window_seconds)
        consumed = error_rate / error_budget
        return max(0.0, (1.0 - consumed) * 100)

    def check_alerts(self) -> AlertSeverity:
        """Evaluate all alert rules and return the highest severity triggered.

        Returns:
            Highest :class:`AlertSeverity` triggered, or ``NONE``.
        """
        triggered = AlertSeverity.NONE

        for rule in self._alert_rules:
            long_rate = self.error_rate_in_window(rule.long_window_minutes * 60)
            short_rate = self.error_rate_in_window(rule.short_window_minutes * 60)

            if (
                rule.check(long_rate, short_rate, self._definition.error_budget)
                and _severity_rank(rule.severity) > _severity_rank(triggered)
            ):
                triggered = rule.severity

        return triggered

    def get_status(self) -> SLOStatus:
        """Determine the current SLO status based on success rate and alerts.

        Returns:
            ``MET`` if within budget, ``WARNING`` if burn rate is elevated,
            ``BREACHED`` if error rate exceeds the error budget.
        """
        success_rate = self.success_rate()
        target = self._definition.target

        if success_rate >= target:
            # Even if meeting target, check if burn rate is concerning
            burn_1h = self.get_burn_rate(60)
            if burn_1h >= 6.0:
                return SLOStatus.WARNING
            return SLOStatus.MET

        # Success rate below target = breached
        return SLOStatus.BREACHED

    def get_report(self) -> SLOReport:
        """Generate a full status report for this SLO.

        Returns:
            A :class:`SLOReport` with all current metrics.
        """
        return SLOReport(
            name=self._definition.name,
            status=self.get_status(),
            target=self._definition.target,
            current_rate=self.success_rate(),
            burn_rate_1h=self.get_burn_rate(60),
            error_budget_remaining_pct=self.get_error_budget_remaining_pct(),
            alert_severity=self.check_alerts(),
            event_count=self.event_count,
            unit=self._definition.unit,
            threshold=self._definition.threshold,
        )


# ── SLO Monitor (aggregator) ───────────────────────────────────────────────


class SLOMonitor:
    """Aggregate monitor for all SLOs.

    Provides a unified interface to record events and query status
    across multiple SLO trackers.

    Args:
        slos: Mapping of SLO key → definition. Uses :data:`DEFAULT_SLOS`
            if not provided.
        alert_rules: Shared alert rules for all trackers.
        max_events_per_slo: Max events per tracker's ring buffer.
    """

    def __init__(
        self,
        slos: dict[str, SLODefinition] | None = None,
        alert_rules: Sequence[BurnRateAlertRule] | None = None,
        max_events_per_slo: int = 100_000,
    ) -> None:
        definitions = slos or DEFAULT_SLOS
        self._trackers: dict[str, SLOTracker] = {
            key: SLOTracker(defn, alert_rules, max_events_per_slo)
            for key, defn in definitions.items()
        }

    @property
    def slo_keys(self) -> list[str]:
        """List all registered SLO keys."""
        return list(self._trackers.keys())

    def get_tracker(self, slo_key: str) -> SLOTracker:
        """Get a specific SLO tracker by key.

        Args:
            slo_key: The SLO identifier (e.g., ``"brain_search"``).

        Raises:
            KeyError: If the SLO key is not registered.
        """
        if slo_key not in self._trackers:
            msg = f"Unknown SLO: {slo_key!r}. Known: {self.slo_keys}"
            raise KeyError(msg)
        return self._trackers[slo_key]

    def record_event(
        self,
        slo_key: str,
        *,
        success: bool,
        value: float = 0.0,
    ) -> None:
        """Record an event for a specific SLO.

        For latency SLOs (brain_search, response_time), ``success`` should
        be True if the value is below the threshold. For availability/error
        SLOs, ``success`` is True if the operation succeeded.

        Args:
            slo_key: Which SLO this event belongs to.
            success: Whether this event met the SLO.
            value: The measured raw value.

        Raises:
            KeyError: If the SLO key is not registered.
        """
        tracker = self.get_tracker(slo_key)
        tracker.record_event(success, value)

    def record_latency(self, slo_key: str, latency_ms: float) -> None:
        """Convenience method for latency SLOs.

        Automatically determines success based on the SLO threshold.

        Args:
            slo_key: SLO key (e.g., ``"brain_search"`` or ``"response_time"``).
            latency_ms: Measured latency in milliseconds.
        """
        tracker = self.get_tracker(slo_key)
        success = latency_ms <= tracker.definition.threshold
        tracker.record_event(success, latency_ms)

    def record_cost(self, cost_usd: float) -> None:
        """Convenience method for the cost_per_message SLO.

        Args:
            cost_usd: Cost in USD for this message.
        """
        tracker = self.get_tracker("cost_per_message")
        success = cost_usd <= tracker.definition.threshold
        tracker.record_event(success, cost_usd)

    def get_report(self) -> dict[str, SLOReport]:
        """Generate reports for all SLOs.

        Returns:
            Mapping of SLO key → :class:`SLOReport`.
        """
        return {key: tracker.get_report() for key, tracker in self._trackers.items()}

    def get_breached_slos(self) -> list[str]:
        """Return keys of all SLOs currently in BREACHED status.

        Returns:
            List of SLO keys with ``SLOStatus.BREACHED``.
        """
        return [
            key
            for key, tracker in self._trackers.items()
            if tracker.get_status() == SLOStatus.BREACHED
        ]

    def get_active_alerts(self) -> dict[str, AlertSeverity]:
        """Return active alerts for all SLOs (excluding NONE).

        Returns:
            Mapping of SLO key → :class:`AlertSeverity` for triggered alerts.
        """
        alerts: dict[str, AlertSeverity] = {}
        for key, tracker in self._trackers.items():
            severity = tracker.check_alerts()
            if severity != AlertSeverity.NONE:
                alerts[key] = severity
        return alerts


# ── Factory ─────────────────────────────────────────────────────────────────


def create_default_monitor(
    max_events_per_slo: int = 100_000,
) -> SLOMonitor:
    """Create an SLOMonitor with the 5 default SLOs.

    Args:
        max_events_per_slo: Max events per SLO ring buffer.

    Returns:
        A configured :class:`SLOMonitor`.
    """
    return SLOMonitor(
        slos=DEFAULT_SLOS,
        max_events_per_slo=max_events_per_slo,
    )


# ── Helpers ─────────────────────────────────────────────────────────────────


def _severity_rank(severity: AlertSeverity) -> int:
    """Numeric rank for alert severity comparison."""
    return {
        AlertSeverity.NONE: 0,
        AlertSeverity.TICKET: 1,
        AlertSeverity.PAGE: 2,
    }.get(severity, 0)
