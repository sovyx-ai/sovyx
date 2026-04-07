"""AlertManager — threshold-based alerting with burn rate and event bus integration.

Implements system alerting (SPE-026 §8, IMPL-015 §2.4):

- Threshold-based alert rules evaluated against current metrics
- Burn rate alerting via SLOMonitor integration
- Event bus integration for alert propagation
- Structured logging for all alert state changes

Alert types (SPE-026 §8):

1. SLO breach — burn rate exceeding thresholds
2. High error rate — error count above threshold in window
3. Disk space low — usage above threshold
4. Memory pressure — RSS above threshold
5. LLM provider down — health check failure
6. Cost exceeds daily budget — cumulative cost above limit

Usage::

    from sovyx.observability.alerts import AlertManager, AlertRule, AlertSeverity

    rules = [
        AlertRule(
            name="high_error_rate",
            metric_name="error_count",
            threshold=100.0,
            window_seconds=300,
            severity=AlertSeverity.WARNING,
        ),
    ]
    manager = AlertManager(event_bus=bus, slo_monitor=monitor, rules=rules)
    fired = await manager.evaluate()
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import TYPE_CHECKING, Any

from sovyx.engine.events import Event, EventCategory
from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sovyx.engine.events import EventBus
    from sovyx.observability.slo import SLOMonitor

logger = get_logger(__name__)


# ── Enums ───────────────────────────────────────────────────────────────────


class AlertSeverity(Enum):
    """Alert severity levels."""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class AlertState(Enum):
    """Alert lifecycle state."""

    FIRING = auto()
    RESOLVED = auto()


# ── Data Classes ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AlertRule:
    """Definition of a threshold-based alert rule.

    Attributes:
        name: Unique human-readable rule name.
        metric_name: The metric key to evaluate (e.g., ``"error_count"``).
        threshold: Value above which the alert fires.
        window_seconds: Time window for metric aggregation.
        severity: Alert severity when fired.
        description: Optional description of what this rule checks.
        min_events: Minimum events in window before evaluating (avoids noise).
    """

    name: str
    metric_name: str
    threshold: float
    window_seconds: int
    severity: AlertSeverity
    description: str = ""
    min_events: int = 1


@dataclass(frozen=True)
class Alert:
    """A fired alert instance.

    Attributes:
        rule_name: Name of the rule that triggered this alert.
        severity: Alert severity.
        message: Human-readable alert message.
        metric_name: The metric that triggered the alert.
        current_value: The metric value that exceeded the threshold.
        threshold: The threshold that was exceeded.
        timestamp: Unix timestamp when the alert fired.
    """

    rule_name: str
    severity: AlertSeverity
    message: str
    metric_name: str
    current_value: float
    threshold: float
    timestamp: float = field(default_factory=time.monotonic)


# ── Alert Event (Event Bus Integration) ────────────────────────────────────


@dataclass(frozen=True)
class AlertFired(Event):
    """Emitted when an alert transitions to FIRING state."""

    rule_name: str = ""
    severity: str = ""
    message: str = ""
    metric_name: str = ""
    current_value: float = 0.0
    threshold: float = 0.0

    @property
    def category(self) -> EventCategory:
        """Return the event category."""
        return EventCategory.ENGINE


@dataclass(frozen=True)
class AlertResolved(Event):
    """Emitted when an alert transitions from FIRING to RESOLVED."""

    rule_name: str = ""
    severity: str = ""
    message: str = ""

    @property
    def category(self) -> EventCategory:
        """Return the event category."""
        return EventCategory.ENGINE


# ── Metric Sample ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class MetricSample:
    """A single metric data point.

    Attributes:
        timestamp: When the sample was recorded (monotonic clock).
        value: The metric value.
    """

    timestamp: float
    value: float


# ── Alert Manager ──────────────────────────────────────────────────────────


class AlertManager:
    """Threshold-based alerting with burn rate and event bus integration.

    Evaluates alert rules against recorded metrics and SLO burn rates.
    Emits :class:`AlertFired` / :class:`AlertResolved` events via the
    event bus when alert states transition.

    Args:
        event_bus: Event bus for emitting alert events. ``None`` disables events.
        slo_monitor: SLO monitor for burn rate checks. ``None`` disables SLO alerts.
        rules: Alert rules to evaluate. Can also be added later via
            :meth:`add_rule`.
        max_samples_per_metric: Maximum samples retained per metric.
    """

    def __init__(
        self,
        event_bus: EventBus | None = None,
        slo_monitor: SLOMonitor | None = None,
        rules: Sequence[AlertRule] | None = None,
        max_samples_per_metric: int = 100_000,
    ) -> None:
        self._event_bus = event_bus
        self._slo_monitor = slo_monitor
        self._rules: dict[str, AlertRule] = {}
        self._max_samples = max_samples_per_metric

        # metric_name → ring buffer of samples
        self._metrics: dict[str, deque[MetricSample]] = {}

        # rule_name → current state (tracks firing/resolved transitions)
        self._states: dict[str, AlertState] = {}

        if rules:
            for rule in rules:
                self.add_rule(rule)

    @property
    def rules(self) -> dict[str, AlertRule]:
        """Return all registered rules."""
        return dict(self._rules)

    @property
    def states(self) -> dict[str, AlertState]:
        """Return current alert states."""
        return dict(self._states)

    def add_rule(self, rule: AlertRule) -> None:
        """Register an alert rule.

        Args:
            rule: The alert rule to add.

        Raises:
            ValueError: If a rule with the same name already exists.
        """
        if rule.name in self._rules:
            msg = f"Alert rule already exists: {rule.name!r}"
            raise ValueError(msg)
        self._rules[rule.name] = rule
        self._states[rule.name] = AlertState.RESOLVED
        logger.debug("alert_rule_added", rule_name=rule.name, metric=rule.metric_name)

    def remove_rule(self, rule_name: str) -> None:
        """Remove an alert rule by name.

        Args:
            rule_name: The rule name to remove.

        Raises:
            KeyError: If the rule doesn't exist.
        """
        if rule_name not in self._rules:
            msg = f"Unknown alert rule: {rule_name!r}"
            raise KeyError(msg)
        del self._rules[rule_name]
        self._states.pop(rule_name, None)
        logger.debug("alert_rule_removed", rule_name=rule_name)

    def record_metric(self, metric_name: str, value: float) -> None:
        """Record a metric sample for alert evaluation.

        Args:
            metric_name: The metric identifier (must match ``AlertRule.metric_name``).
            value: The metric value.
        """
        if metric_name not in self._metrics:
            self._metrics[metric_name] = deque(maxlen=self._max_samples)
        self._metrics[metric_name].append(
            MetricSample(timestamp=time.monotonic(), value=value)
        )

    def get_metric_value_in_window(
        self, metric_name: str, window_seconds: int
    ) -> tuple[float, int]:
        """Aggregate metric value within a time window.

        Returns the sum of values and count of samples in the window.

        Args:
            metric_name: The metric to query.
            window_seconds: How far back to look.

        Returns:
            Tuple of (sum_of_values, sample_count).
        """
        samples = self._metrics.get(metric_name)
        if not samples:
            return 0.0, 0

        now = time.monotonic()
        cutoff = now - window_seconds
        total = 0.0
        count = 0

        for sample in reversed(samples):
            if sample.timestamp < cutoff:
                break
            total += sample.value
            count += 1

        return total, count

    def _evaluate_rule(self, rule: AlertRule) -> Alert | None:
        """Evaluate a single rule against current metrics.

        Returns an Alert if the rule fires, None otherwise.
        """
        total, count = self.get_metric_value_in_window(
            rule.metric_name, rule.window_seconds
        )

        if count < rule.min_events:
            return None

        if total > rule.threshold:
            return Alert(
                rule_name=rule.name,
                severity=rule.severity,
                message=(
                    f"{rule.name}: {rule.metric_name}={total:.2f} "
                    f"exceeds threshold {rule.threshold:.2f} "
                    f"(window={rule.window_seconds}s, samples={count})"
                ),
                metric_name=rule.metric_name,
                current_value=total,
                threshold=rule.threshold,
            )
        return None

    def _evaluate_slo_alerts(self) -> list[Alert]:
        """Evaluate SLO burn rates and generate alerts for breached SLOs."""
        if self._slo_monitor is None:
            return []

        alerts: list[Alert] = []
        report = self._slo_monitor.get_report()

        for slo_key, slo_report in report.items():
            from sovyx.observability.slo import AlertSeverity as SLOAlertSeverity
            from sovyx.observability.slo import SLOStatus

            # Map SLO alert severity to our AlertSeverity
            if slo_report.alert_severity == SLOAlertSeverity.PAGE:
                severity = AlertSeverity.CRITICAL
            elif slo_report.alert_severity == SLOAlertSeverity.TICKET:
                severity = AlertSeverity.WARNING
            else:
                # Check if breached even without burn rate alert
                if slo_report.status == SLOStatus.BREACHED:
                    severity = AlertSeverity.WARNING
                else:
                    continue

            rule_name = f"slo_{slo_key}"
            alerts.append(
                Alert(
                    rule_name=rule_name,
                    severity=severity,
                    message=(
                        f"SLO {slo_report.name}: "
                        f"rate={slo_report.current_rate:.3f} "
                        f"(target={slo_report.target:.3f}, "
                        f"burn_1h={slo_report.burn_rate_1h:.1f}, "
                        f"budget_remaining={slo_report.error_budget_remaining_pct:.1f}%)"
                    ),
                    metric_name=slo_key,
                    current_value=slo_report.current_rate,
                    threshold=slo_report.target,
                )
            )

        return alerts

    async def evaluate(self) -> list[Alert]:
        """Evaluate all alert rules and SLO burn rates.

        Checks each registered rule against current metrics and
        evaluates SLO burn rates. Emits :class:`AlertFired` and
        :class:`AlertResolved` events on state transitions.

        Returns:
            List of currently firing alerts.
        """
        fired_alerts: list[Alert] = []

        # Evaluate threshold rules
        currently_firing: set[str] = set()
        for rule in self._rules.values():
            alert = self._evaluate_rule(rule)
            if alert is not None:
                fired_alerts.append(alert)
                currently_firing.add(rule.name)

        # Evaluate SLO alerts
        slo_alerts = self._evaluate_slo_alerts()
        for alert in slo_alerts:
            fired_alerts.append(alert)
            currently_firing.add(alert.rule_name)

        # Handle state transitions
        await self._process_transitions(currently_firing, fired_alerts)

        return fired_alerts

    async def _process_transitions(
        self,
        currently_firing: set[str],
        fired_alerts: list[Alert],
    ) -> None:
        """Process alert state transitions and emit events.

        Args:
            currently_firing: Set of rule names currently firing.
            fired_alerts: The list of fired alerts for logging context.
        """
        # Build lookup for fired alerts
        alert_lookup: dict[str, Alert] = {a.rule_name: a for a in fired_alerts}

        # Check for new FIRING transitions (threshold rules only)
        for rule_name in currently_firing:
            prev_state = self._states.get(rule_name, AlertState.RESOLVED)
            if prev_state == AlertState.RESOLVED:
                self._states[rule_name] = AlertState.FIRING
                alert = alert_lookup.get(rule_name)
                if alert:
                    logger.warning(
                        "alert_firing",
                        rule_name=rule_name,
                        severity=alert.severity.value,
                        message=alert.message,
                    )
                    await self._emit_fired(alert)

        # Check for RESOLVED transitions (threshold rules only)
        for rule_name, state in list(self._states.items()):
            if state == AlertState.FIRING and rule_name not in currently_firing:
                self._states[rule_name] = AlertState.RESOLVED
                rule = self._rules.get(rule_name)
                severity = rule.severity.value if rule else "info"
                logger.info(
                    "alert_resolved",
                    rule_name=rule_name,
                    severity=severity,
                )
                await self._emit_resolved(rule_name, severity)

    async def _emit_fired(self, alert: Alert) -> None:
        """Emit an AlertFired event via the event bus."""
        if self._event_bus is None:
            return
        event = AlertFired(
            rule_name=alert.rule_name,
            severity=alert.severity.value,
            message=alert.message,
            metric_name=alert.metric_name,
            current_value=alert.current_value,
            threshold=alert.threshold,
        )
        await self._event_bus.emit(event)

    async def _emit_resolved(self, rule_name: str, severity: str) -> None:
        """Emit an AlertResolved event via the event bus."""
        if self._event_bus is None:
            return
        event = AlertResolved(
            rule_name=rule_name,
            severity=severity,
            message=f"Alert resolved: {rule_name}",
        )
        await self._event_bus.emit(event)

    def get_firing_alerts(self) -> list[str]:
        """Return names of all currently firing alert rules.

        Returns:
            List of rule names in FIRING state.
        """
        return [
            name for name, state in self._states.items()
            if state == AlertState.FIRING
        ]

    def get_alert_summary(self) -> dict[str, Any]:
        """Return a summary of alert state for dashboard/API consumption.

        Returns:
            Dict with counts by severity and list of firing alerts.
        """
        firing = self.get_firing_alerts()
        severity_counts: dict[str, int] = {
            AlertSeverity.INFO.value: 0,
            AlertSeverity.WARNING.value: 0,
            AlertSeverity.CRITICAL.value: 0,
        }
        for name in firing:
            rule = self._rules.get(name)
            if rule:
                severity_counts[rule.severity.value] += 1

        return {
            "total_rules": len(self._rules),
            "firing_count": len(firing),
            "firing_rules": firing,
            "severity_counts": severity_counts,
        }


# ── Factory ─────────────────────────────────────────────────────────────────


def create_default_rules() -> list[AlertRule]:
    """Create default alert rules matching SPE-026 §8 alert types.

    Returns:
        List of default :class:`AlertRule` instances.
    """
    return [
        AlertRule(
            name="high_error_rate",
            metric_name="error_count",
            threshold=100.0,
            window_seconds=300,
            severity=AlertSeverity.WARNING,
            description="Error count exceeds 100 in 5 minutes",
            min_events=10,
        ),
        AlertRule(
            name="disk_space_low",
            metric_name="disk_usage_pct",
            threshold=90.0,
            window_seconds=60,
            severity=AlertSeverity.CRITICAL,
            description="Disk usage above 90%",
        ),
        AlertRule(
            name="memory_pressure",
            metric_name="memory_usage_pct",
            threshold=85.0,
            window_seconds=60,
            severity=AlertSeverity.WARNING,
            description="Memory usage above 85%",
        ),
        AlertRule(
            name="cost_exceeded",
            metric_name="daily_cost_usd",
            threshold=10.0,
            window_seconds=86400,
            severity=AlertSeverity.WARNING,
            description="Daily LLM cost exceeds $10",
        ),
        AlertRule(
            name="provider_errors",
            metric_name="provider_error_count",
            threshold=10.0,
            window_seconds=600,
            severity=AlertSeverity.CRITICAL,
            description="LLM provider errors exceed 10 in 10 minutes",
            min_events=3,
        ),
    ]


def create_default_alert_manager(
    event_bus: EventBus | None = None,
    slo_monitor: SLOMonitor | None = None,
) -> AlertManager:
    """Create an AlertManager with default rules.

    Args:
        event_bus: Event bus for emitting alert events.
        slo_monitor: SLO monitor for burn rate checks.

    Returns:
        A configured :class:`AlertManager`.
    """
    return AlertManager(
        event_bus=event_bus,
        slo_monitor=slo_monitor,
        rules=create_default_rules(),
    )
