"""Tests for AlertManager (V05-02).

Tests threshold alerting with metric samples, SLO integration,
state transitions, event bus emission, and the default rule factory.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from sovyx.observability.alerts import (
    Alert,
    AlertFired,
    AlertManager,
    AlertResolved,
    AlertRule,
    AlertSeverity,
    AlertState,
    MetricSample,
    create_default_alert_manager,
    create_default_rules,
)

# ── Helpers ─────────────────────────────────────────────────────────────


def _make_rule(
    *,
    name: str = "test-rule",
    metric_name: str = "test_metric",
    threshold: float = 10.0,
    window_seconds: int = 60,
    severity: AlertSeverity = AlertSeverity.WARNING,
    description: str = "",
    min_events: int = 1,
) -> AlertRule:
    """Create an AlertRule with sensible defaults."""
    return AlertRule(
        name=name,
        metric_name=metric_name,
        threshold=threshold,
        window_seconds=window_seconds,
        severity=severity,
        description=description,
        min_events=min_events,
    )


# ── AlertRule Tests ─────────────────────────────────────────────────────


class TestAlertRule:
    """Test AlertRule dataclass."""

    def test_frozen(self) -> None:
        rule = _make_rule()
        with pytest.raises(AttributeError):
            rule.name = "changed"  # type: ignore[misc]

    def test_default_values(self) -> None:
        rule = AlertRule(
            name="r",
            metric_name="m",
            threshold=1.0,
            window_seconds=300,
            severity=AlertSeverity.WARNING,
        )
        assert rule.description == ""
        assert rule.min_events == 1

    def test_all_fields(self) -> None:
        rule = _make_rule(
            name="cpu-high",
            metric_name="cpu_pct",
            threshold=90.0,
            window_seconds=120,
            severity=AlertSeverity.CRITICAL,
            description="CPU above 90%",
            min_events=5,
        )
        assert rule.name == "cpu-high"
        assert rule.metric_name == "cpu_pct"
        assert rule.threshold == 90.0
        assert rule.window_seconds == 120
        assert rule.severity == AlertSeverity.CRITICAL
        assert rule.description == "CPU above 90%"
        assert rule.min_events == 5


class TestAlert:
    """Test Alert dataclass."""

    def test_frozen(self) -> None:
        alert = Alert(
            rule_name="test",
            severity=AlertSeverity.WARNING,
            message="msg",
            metric_name="m",
            current_value=1.0,
            threshold=0.5,
        )
        with pytest.raises(AttributeError):
            alert.message = "changed"  # type: ignore[misc]

    def test_has_timestamp(self) -> None:
        alert = Alert(
            rule_name="test",
            severity=AlertSeverity.WARNING,
            message="msg",
            metric_name="m",
            current_value=1.0,
            threshold=0.5,
        )
        assert alert.timestamp > 0


class TestAlertFired:
    """Test AlertFired event dataclass."""

    def test_has_alert_data(self) -> None:
        event = AlertFired(
            rule_name="test-rule",
            severity="warning",
            message="test message",
            metric_name="m",
            current_value=1.5,
            threshold=1.0,
        )
        assert event.rule_name == "test-rule"
        assert event.severity == "warning"
        assert event.message == "test message"

    def test_frozen(self) -> None:
        event = AlertFired()
        with pytest.raises(AttributeError):
            event.rule_name = "changed"  # type: ignore[misc]


class TestAlertResolved:
    """Test AlertResolved event dataclass."""

    def test_has_resolution_data(self) -> None:
        event = AlertResolved(
            rule_name="test-rule",
            severity="warning",
            message="Alert resolved: test-rule",
        )
        assert event.rule_name == "test-rule"
        assert event.message == "Alert resolved: test-rule"


# ── AlertSeverity Tests ────────────────────────────────────────────────


class TestAlertSeverity:
    """Test severity enum values."""

    def test_values(self) -> None:
        assert AlertSeverity.INFO.value == "info"
        assert AlertSeverity.WARNING.value == "warning"
        assert AlertSeverity.CRITICAL.value == "critical"

    def test_all_severities(self) -> None:
        assert len(AlertSeverity) == 3


# ── MetricSample Tests ─────────────────────────────────────────────────


class TestMetricSample:
    """Test MetricSample dataclass."""

    def test_frozen(self) -> None:
        sample = MetricSample(timestamp=1.0, value=42.0)
        with pytest.raises(AttributeError):
            sample.value = 0.0  # type: ignore[misc]

    def test_values(self) -> None:
        sample = MetricSample(timestamp=1.5, value=99.0)
        assert sample.timestamp == 1.5
        assert sample.value == 99.0


# ── AlertManager Core Tests ────────────────────────────────────────────


class TestAlertManagerRules:
    """Test rule management."""

    def test_add_rule(self) -> None:
        mgr = AlertManager()
        rule = _make_rule()
        mgr.add_rule(rule)
        assert "test-rule" in mgr.rules
        assert mgr.rules["test-rule"] is rule

    def test_add_duplicate_rule_raises(self) -> None:
        mgr = AlertManager()
        mgr.add_rule(_make_rule(name="dup"))
        with pytest.raises(ValueError, match="already exists"):
            mgr.add_rule(_make_rule(name="dup"))

    def test_remove_rule(self) -> None:
        mgr = AlertManager()
        mgr.add_rule(_make_rule(name="removable"))
        mgr.remove_rule("removable")
        assert "removable" not in mgr.rules

    def test_remove_nonexistent_rule_raises(self) -> None:
        mgr = AlertManager()
        with pytest.raises(KeyError, match="Unknown"):
            mgr.remove_rule("nope")

    def test_rules_returns_copy(self) -> None:
        mgr = AlertManager()
        mgr.add_rule(_make_rule())
        rules = mgr.rules
        rules.clear()
        assert len(mgr.rules) == 1

    def test_states_returns_copy(self) -> None:
        mgr = AlertManager()
        mgr.add_rule(_make_rule())
        states = mgr.states
        states.clear()
        assert len(mgr.states) == 1

    def test_initial_state_is_resolved(self) -> None:
        mgr = AlertManager()
        mgr.add_rule(_make_rule(name="r1"))
        assert mgr.states["r1"] == AlertState.RESOLVED


class TestAlertManagerRecordMetric:
    """Test metric recording."""

    def test_record_and_query(self) -> None:
        mgr = AlertManager()
        mgr.record_metric("cpu", 80.0)
        mgr.record_metric("cpu", 90.0)
        total, count = mgr.get_metric_value_in_window("cpu", 60)
        assert total == 170.0
        assert count == 2

    def test_empty_metric(self) -> None:
        mgr = AlertManager()
        total, count = mgr.get_metric_value_in_window("missing", 60)
        assert total == 0.0
        assert count == 0


class TestAlertManagerEvaluate:
    """Test threshold rule evaluation."""

    async def test_no_alert_when_below_threshold(self) -> None:
        mgr = AlertManager()
        mgr.add_rule(_make_rule(threshold=100.0))
        mgr.record_metric("test_metric", 50.0)
        alerts = await mgr.evaluate()
        assert alerts == []

    async def test_fires_on_exceeding_threshold(self) -> None:
        mgr = AlertManager()
        mgr.add_rule(_make_rule(threshold=10.0, metric_name="errors"))
        mgr.record_metric("errors", 15.0)
        alerts = await mgr.evaluate()
        assert len(alerts) == 1
        assert alerts[0].rule_name == "test-rule"
        assert alerts[0].severity == AlertSeverity.WARNING

    async def test_no_fire_at_exact_threshold(self) -> None:
        mgr = AlertManager()
        mgr.add_rule(_make_rule(threshold=10.0))
        mgr.record_metric("test_metric", 10.0)
        alerts = await mgr.evaluate()
        assert alerts == []

    async def test_min_events_required(self) -> None:
        mgr = AlertManager()
        mgr.add_rule(_make_rule(threshold=5.0, min_events=3))
        mgr.record_metric("test_metric", 10.0)
        # Only 1 event, need 3
        alerts = await mgr.evaluate()
        assert alerts == []

    async def test_min_events_met(self) -> None:
        mgr = AlertManager()
        mgr.add_rule(_make_rule(threshold=5.0, min_events=3))
        for _ in range(3):
            mgr.record_metric("test_metric", 5.0)
        # 3 events × 5.0 = 15.0 > threshold 5.0
        alerts = await mgr.evaluate()
        assert len(alerts) == 1

    async def test_multiple_rules_multiple_alerts(self) -> None:
        mgr = AlertManager()
        mgr.add_rule(_make_rule(name="r1", metric_name="m1", threshold=5.0))
        mgr.add_rule(_make_rule(name="r2", metric_name="m2", threshold=5.0))
        mgr.record_metric("m1", 10.0)
        mgr.record_metric("m2", 10.0)
        alerts = await mgr.evaluate()
        assert len(alerts) == 2
        names = {a.rule_name for a in alerts}
        assert names == {"r1", "r2"}

    async def test_skips_metric_without_data(self) -> None:
        mgr = AlertManager()
        mgr.add_rule(_make_rule(metric_name="missing"))
        alerts = await mgr.evaluate()
        assert alerts == []


class TestAlertManagerStateTransitions:
    """Test RESOLVED → FIRING → RESOLVED transitions."""

    async def test_transition_to_firing(self) -> None:
        mgr = AlertManager()
        mgr.add_rule(_make_rule(name="r1", threshold=5.0))
        mgr.record_metric("test_metric", 10.0)
        await mgr.evaluate()
        assert mgr.states["r1"] == AlertState.FIRING

    async def test_transition_to_resolved(self) -> None:
        mgr = AlertManager()
        mgr.add_rule(_make_rule(name="r1", threshold=100.0, window_seconds=1))
        mgr.record_metric("test_metric", 200.0)
        await mgr.evaluate()
        assert mgr.states["r1"] == AlertState.FIRING

        # Wait for window to expire, then re-evaluate with healthy metric
        import asyncio

        await asyncio.sleep(1.1)
        mgr.record_metric("test_metric", 1.0)
        await mgr.evaluate()
        assert mgr.states["r1"] == AlertState.RESOLVED

    async def test_already_firing_stays_firing(self) -> None:
        mgr = AlertManager()
        mgr.add_rule(_make_rule(name="r1", threshold=5.0))
        mgr.record_metric("test_metric", 10.0)
        await mgr.evaluate()
        # Record more, still firing
        mgr.record_metric("test_metric", 10.0)
        await mgr.evaluate()
        assert mgr.states["r1"] == AlertState.FIRING


class TestAlertManagerEventBus:
    """Test EventBus integration."""

    async def test_emits_fired_event(self) -> None:
        bus = MagicMock()
        bus.emit = AsyncMock()
        mgr = AlertManager(event_bus=bus)
        mgr.add_rule(_make_rule(threshold=5.0))
        mgr.record_metric("test_metric", 10.0)
        await mgr.evaluate()
        bus.emit.assert_called_once()
        event = bus.emit.call_args[0][0]
        assert isinstance(event, AlertFired)
        assert event.rule_name == "test-rule"

    async def test_emits_resolved_event(self) -> None:
        import asyncio

        bus = MagicMock()
        bus.emit = AsyncMock()
        mgr = AlertManager(event_bus=bus)
        mgr.add_rule(_make_rule(threshold=5.0, window_seconds=1))
        mgr.record_metric("test_metric", 10.0)
        await mgr.evaluate()  # fires

        await asyncio.sleep(1.1)
        await mgr.evaluate()  # resolves (window expired, no new data above threshold)

        # Should have emitted AlertFired then AlertResolved
        assert bus.emit.call_count == 2
        resolved_event = bus.emit.call_args_list[1][0][0]
        assert isinstance(resolved_event, AlertResolved)

    async def test_no_bus_no_crash(self) -> None:
        mgr = AlertManager()  # no event_bus
        mgr.add_rule(_make_rule(threshold=5.0))
        mgr.record_metric("test_metric", 10.0)
        await mgr.evaluate()  # should not crash


class TestAlertManagerSummary:
    """Test get_alert_summary and get_firing_alerts."""

    async def test_summary_no_alerts(self) -> None:
        mgr = AlertManager()
        mgr.add_rule(_make_rule(threshold=100.0))
        summary = mgr.get_alert_summary()
        assert summary["total_rules"] == 1
        assert summary["firing_count"] == 0
        assert summary["firing_rules"] == []

    async def test_summary_with_firing(self) -> None:
        mgr = AlertManager()
        mgr.add_rule(_make_rule(name="r1", threshold=5.0, severity=AlertSeverity.CRITICAL))
        mgr.record_metric("test_metric", 10.0)
        await mgr.evaluate()
        summary = mgr.get_alert_summary()
        assert summary["firing_count"] == 1
        assert "r1" in summary["firing_rules"]
        assert summary["severity_counts"]["critical"] == 1

    async def test_get_firing_alerts(self) -> None:
        mgr = AlertManager()
        mgr.add_rule(_make_rule(name="r1", threshold=5.0))
        mgr.add_rule(_make_rule(name="r2", metric_name="other", threshold=5.0))
        mgr.record_metric("test_metric", 10.0)
        await mgr.evaluate()
        firing = mgr.get_firing_alerts()
        assert firing == ["r1"]


class TestAlertManagerSLOIntegration:
    """Test SLO monitor integration."""

    async def test_slo_alerts_from_breached_slo(self) -> None:
        from sovyx.observability.slo import (
            AlertSeverity as SLOAlertSeverity,
        )
        from sovyx.observability.slo import (
            SLOMonitor,
            SLOReport,
            SLOStatus,
        )

        slo_monitor = MagicMock(spec=SLOMonitor)
        slo_monitor.get_report.return_value = {
            "brain_search": SLOReport(
                name="Brain Search",
                status=SLOStatus.BREACHED,
                target=0.99,
                current_rate=0.85,
                burn_rate_1h=5.0,
                error_budget_remaining_pct=0.0,
                alert_severity=SLOAlertSeverity.PAGE,
                event_count=1000,
                unit="ms",
                threshold=100.0,
            ),
        }
        mgr = AlertManager(slo_monitor=slo_monitor)
        alerts = await mgr.evaluate()
        assert len(alerts) == 1
        assert "Brain Search" in alerts[0].message
        assert alerts[0].severity == AlertSeverity.CRITICAL

    async def test_no_slo_alerts_when_healthy(self) -> None:
        from sovyx.observability.slo import (
            AlertSeverity as SLOAlertSeverity,
        )
        from sovyx.observability.slo import (
            SLOMonitor,
            SLOReport,
            SLOStatus,
        )

        slo_monitor = MagicMock(spec=SLOMonitor)
        slo_monitor.get_report.return_value = {
            "brain_search": SLOReport(
                name="Brain Search",
                status=SLOStatus.MET,
                target=0.99,
                current_rate=0.999,
                burn_rate_1h=0.1,
                error_budget_remaining_pct=90.0,
                alert_severity=SLOAlertSeverity.NONE,
                event_count=1000,
                unit="ms",
                threshold=100.0,
            ),
        }
        mgr = AlertManager(slo_monitor=slo_monitor)
        alerts = await mgr.evaluate()
        assert alerts == []

    async def test_no_slo_monitor_no_crash(self) -> None:
        mgr = AlertManager()  # no slo_monitor
        alerts = await mgr.evaluate()
        assert alerts == []


# ── Default Manager Tests ──────────────────────────────────────────────


class TestCreateDefaultRules:
    """Test the default rule factory."""

    def test_has_five_default_rules(self) -> None:
        rules = create_default_rules()
        assert len(rules) == 5

    def test_rule_names(self) -> None:
        rules = create_default_rules()
        names = {r.name for r in rules}
        expected = {
            "high_error_rate",
            "disk_space_low",
            "memory_pressure",
            "cost_exceeded",
            "provider_errors",
        }
        assert names == expected

    def test_disk_space_is_critical(self) -> None:
        rules = create_default_rules()
        disk_rule = next(r for r in rules if r.name == "disk_space_low")
        assert disk_rule.severity == AlertSeverity.CRITICAL

    def test_provider_errors_is_critical(self) -> None:
        rules = create_default_rules()
        rule = next(r for r in rules if r.name == "provider_errors")
        assert rule.severity == AlertSeverity.CRITICAL


class TestCreateDefaultAlertManager:
    """Test the default alert manager factory."""

    def test_has_five_rules(self) -> None:
        mgr = create_default_alert_manager()
        assert len(mgr.rules) == 5

    def test_accepts_event_bus(self) -> None:
        bus = MagicMock()
        mgr = create_default_alert_manager(event_bus=bus)
        assert mgr._event_bus is bus  # noqa: SLF001

    def test_accepts_slo_monitor(self) -> None:
        mon = MagicMock()
        mgr = create_default_alert_manager(slo_monitor=mon)
        assert mgr._slo_monitor is mon  # noqa: SLF001


# ── Property-Based Tests ───────────────────────────────────────────────


class TestAlertManagerProperties:
    """Property-based tests with Hypothesis."""

    @given(
        threshold=st.floats(min_value=1.0, max_value=1000.0, allow_nan=False),
        value=st.floats(min_value=0.0, max_value=1000.0, allow_nan=False),
    )
    @settings(max_examples=50)
    async def test_fires_only_above_threshold(
        self,
        threshold: float,
        value: float,
    ) -> None:
        """Alert fires iff recorded value > threshold."""
        mgr = AlertManager()
        mgr.add_rule(_make_rule(threshold=threshold))
        mgr.record_metric("test_metric", value)
        alerts = await mgr.evaluate()

        if value > threshold:
            assert len(alerts) == 1
        else:
            assert len(alerts) == 0
