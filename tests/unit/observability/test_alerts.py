"""Tests for AlertManager (V05-02).

Tests threshold alerting, burn rate alerting, cooldowns, deduplication,
raw metric evaluation, and the default rule factory.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from sovyx.observability.alerts import (
    Alert,
    AlertFired,
    AlertManager,
    AlertRule,
    AlertSeverity,
    create_default_alert_manager,
)
from sovyx.observability.slo import SLOReport, SLOStatus

# ── Fixtures ────────────────────────────────────────────────────────────

@pytest.fixture()
def manager() -> AlertManager:
    """Fresh AlertManager with short cooldown for testing."""
    return AlertManager(cooldown_seconds=1.0, max_history=100)


@pytest.fixture()
def default_manager() -> AlertManager:
    """AlertManager with default SLO rules."""
    return create_default_alert_manager(cooldown_seconds=1.0)


def _make_report(
    *,
    name: str = "test_metric",
    compliance: float = 0.999,
    burn_rate_1h: float = 0.5,
    status: SLOStatus = SLOStatus.MET,
) -> SLOReport:
    """Create a minimal SLOReport for testing."""
    return SLOReport(
        name=name,
        status=status,
        compliance=compliance,
        burn_rate_1h=burn_rate_1h,
        total_events=1000,
        good_events=990,
        bad_events=10,
        error_budget_remaining=0.5,
    )


def _make_rule(
    *,
    name: str = "test-rule",
    metric_name: str = "test_metric",
    threshold: float = 0.01,
    burn_rate_threshold: float = 0.0,
    severity: AlertSeverity = AlertSeverity.WARNING,
) -> AlertRule:
    """Create an AlertRule with sensible defaults."""
    return AlertRule(
        name=name,
        metric_name=metric_name,
        threshold=threshold,
        severity=severity,
        burn_rate_threshold=burn_rate_threshold,
    )


# ── AlertRule Tests ─────────────────────────────────────────────────────

class TestAlertRule:
    """Test AlertRule dataclass."""

    def test_frozen(self) -> None:
        rule = _make_rule()
        with pytest.raises(AttributeError):
            rule.name = "changed"  # type: ignore[misc]

    def test_default_values(self) -> None:
        rule = AlertRule(name="r", metric_name="m", threshold=1.0)
        assert rule.window_seconds == 300
        assert rule.severity == AlertSeverity.WARNING
        assert rule.description == ""
        assert rule.burn_rate_threshold == 0.0


class TestAlert:
    """Test Alert dataclass."""

    def test_has_unique_id(self) -> None:
        a1 = Alert()
        a2 = Alert()
        assert a1.alert_id != a2.alert_id

    def test_frozen(self) -> None:
        alert = Alert()
        with pytest.raises(AttributeError):
            alert.message = "changed"  # type: ignore[misc]


class TestAlertFired:
    """Test AlertFired event dataclass."""

    def test_holds_alert_and_rule(self) -> None:
        rule = _make_rule()
        alert = Alert(rule_name=rule.name)
        event = AlertFired(alert=alert, rule=rule)
        assert event.alert is alert
        assert event.rule is rule


# ── AlertSeverity Tests ────────────────────────────────────────────────

class TestAlertSeverity:
    """Test severity enum values."""

    def test_values(self) -> None:
        assert AlertSeverity.INFO.value == "info"
        assert AlertSeverity.WARNING.value == "warning"
        assert AlertSeverity.CRITICAL.value == "critical"

    def test_all_severities(self) -> None:
        assert len(AlertSeverity) == 3


# ── AlertManager Core Tests ────────────────────────────────────────────

class TestAlertManagerRules:
    """Test rule management."""

    def test_add_rule(self, manager: AlertManager) -> None:
        rule = _make_rule()
        manager.add_rule(rule)
        assert "test-rule" in manager.rules
        assert manager.rules["test-rule"] is rule

    def test_add_duplicate_rule_raises(self, manager: AlertManager) -> None:
        manager.add_rule(_make_rule(name="dup"))
        with pytest.raises(ValueError, match="already exists"):
            manager.add_rule(_make_rule(name="dup"))

    def test_remove_rule(self, manager: AlertManager) -> None:
        manager.add_rule(_make_rule(name="removable"))
        removed = manager.remove_rule("removable")
        assert removed is not None
        assert removed.name == "removable"
        assert "removable" not in manager.rules

    def test_remove_nonexistent_rule(self, manager: AlertManager) -> None:
        assert manager.remove_rule("nope") is None

    def test_rules_returns_copy(self, manager: AlertManager) -> None:
        manager.add_rule(_make_rule())
        rules = manager.rules
        rules.clear()
        assert len(manager.rules) == 1


class TestAlertManagerEvaluate:
    """Test SLO report evaluation."""

    def test_no_alert_when_healthy(self, manager: AlertManager) -> None:
        manager.add_rule(_make_rule(threshold=0.01))
        report = _make_report(compliance=0.999, burn_rate_1h=0.5)
        alerts = manager.evaluate({"test_metric": report})
        assert alerts == []

    def test_fires_on_low_compliance(self, manager: AlertManager) -> None:
        manager.add_rule(_make_rule(threshold=0.01))
        report = _make_report(compliance=0.95)  # below 1 - 0.01 = 0.99
        alerts = manager.evaluate({"test_metric": report})
        assert len(alerts) == 1
        assert alerts[0].rule_name == "test-rule"
        assert alerts[0].severity == AlertSeverity.WARNING

    def test_fires_on_high_burn_rate(self, manager: AlertManager) -> None:
        manager.add_rule(_make_rule(burn_rate_threshold=2.0, threshold=0.0))
        report = _make_report(burn_rate_1h=3.5)
        alerts = manager.evaluate({"test_metric": report})
        assert len(alerts) == 1
        assert "burn_rate_1h=3.50" in alerts[0].message

    def test_no_fire_when_burn_rate_below_threshold(self, manager: AlertManager) -> None:
        manager.add_rule(_make_rule(burn_rate_threshold=2.0, threshold=0.0))
        report = _make_report(burn_rate_1h=1.5, compliance=0.999)
        alerts = manager.evaluate({"test_metric": report})
        assert alerts == []

    def test_skips_unknown_metric(self, manager: AlertManager) -> None:
        manager.add_rule(_make_rule(metric_name="unknown"))
        report = _make_report(name="other_metric")
        alerts = manager.evaluate({"other_metric": report})
        assert alerts == []

    def test_multiple_rules_multiple_alerts(self, manager: AlertManager) -> None:
        manager.add_rule(_make_rule(name="r1", metric_name="m1", threshold=0.01))
        manager.add_rule(_make_rule(name="r2", metric_name="m2", threshold=0.01))
        reports = {
            "m1": _make_report(name="m1", compliance=0.5),
            "m2": _make_report(name="m2", compliance=0.5),
        }
        alerts = manager.evaluate(reports)
        assert len(alerts) == 2
        names = {a.rule_name for a in alerts}
        assert names == {"r1", "r2"}


class TestAlertManagerCooldown:
    """Test cooldown deduplication."""

    def test_cooldown_prevents_rapid_refire(self, manager: AlertManager) -> None:
        manager.add_rule(_make_rule(threshold=0.01))
        report = _make_report(compliance=0.5)

        first = manager.evaluate({"test_metric": report})
        assert len(first) == 1

        # Immediate re-evaluate should be suppressed
        second = manager.evaluate({"test_metric": report})
        assert len(second) == 0

    def test_fires_again_after_cooldown(self) -> None:
        mgr = AlertManager(cooldown_seconds=0.01, max_history=100)
        mgr.add_rule(_make_rule(threshold=0.01))
        report = _make_report(compliance=0.5)

        first = mgr.evaluate({"test_metric": report})
        assert len(first) == 1

        time.sleep(0.02)

        second = mgr.evaluate({"test_metric": report})
        assert len(second) == 1

    def test_reset_cooldowns(self, manager: AlertManager) -> None:
        manager.add_rule(_make_rule(threshold=0.01))
        report = _make_report(compliance=0.5)

        manager.evaluate({"test_metric": report})
        manager.reset_cooldowns()

        # Should fire immediately after reset
        alerts = manager.evaluate({"test_metric": report})
        assert len(alerts) == 1


class TestAlertManagerHistory:
    """Test alert history."""

    def test_history_records_alerts(self, manager: AlertManager) -> None:
        manager.add_rule(_make_rule(threshold=0.01))
        report = _make_report(compliance=0.5)
        manager.evaluate({"test_metric": report})
        assert len(manager.history) == 1

    def test_history_max_size(self) -> None:
        mgr = AlertManager(cooldown_seconds=0.0, max_history=3)
        mgr.add_rule(_make_rule(threshold=0.01))
        report = _make_report(compliance=0.5)
        for _ in range(5):
            mgr.evaluate({"test_metric": report})
        assert len(mgr.history) == 3

    def test_clear_history(self, manager: AlertManager) -> None:
        manager.add_rule(_make_rule(threshold=0.01))
        report = _make_report(compliance=0.5)
        manager.evaluate({"test_metric": report})
        count = manager.clear_history()
        assert count == 1
        assert len(manager.history) == 0

    def test_history_returns_copy(self, manager: AlertManager) -> None:
        manager.add_rule(_make_rule(threshold=0.01))
        report = _make_report(compliance=0.5)
        manager.evaluate({"test_metric": report})
        history = manager.history
        history.clear()
        assert len(manager.history) == 1


class TestAlertManagerRawMetrics:
    """Test evaluate_metrics with raw float values."""

    def test_fires_on_exceeding_threshold(self, manager: AlertManager) -> None:
        manager.add_rule(_make_rule(metric_name="cpu", threshold=90.0))
        alerts = manager.evaluate_metrics({"cpu": 95.0})
        assert len(alerts) == 1
        assert "cpu=95.0000" in alerts[0].message

    def test_no_fire_below_threshold(self, manager: AlertManager) -> None:
        manager.add_rule(_make_rule(metric_name="cpu", threshold=90.0))
        alerts = manager.evaluate_metrics({"cpu": 85.0})
        assert alerts == []

    def test_no_fire_at_exact_threshold(self, manager: AlertManager) -> None:
        manager.add_rule(_make_rule(metric_name="cpu", threshold=90.0))
        alerts = manager.evaluate_metrics({"cpu": 90.0})
        assert alerts == []

    def test_cooldown_applies_to_raw_metrics(self, manager: AlertManager) -> None:
        manager.add_rule(_make_rule(metric_name="cpu", threshold=90.0))
        first = manager.evaluate_metrics({"cpu": 95.0})
        second = manager.evaluate_metrics({"cpu": 95.0})
        assert len(first) == 1
        assert len(second) == 0


class TestAlertManagerEventBus:
    """Test EventBus integration."""

    def test_set_event_bus(self, manager: AlertManager) -> None:
        bus = MagicMock()
        manager.set_event_bus(bus)
        assert manager._event_bus is bus  # noqa: SLF001


# ── Default Manager Tests ──────────────────────────────────────────────

class TestCreateDefaultAlertManager:
    """Test the default alert manager factory."""

    def test_has_five_default_rules(self, default_manager: AlertManager) -> None:
        assert len(default_manager.rules) == 5

    def test_rule_names(self, default_manager: AlertManager) -> None:
        expected = {
            "brain-search-slow",
            "response-time-slow",
            "high-error-rate",
            "low-uptime",
            "cost-per-message-high",
        }
        assert set(default_manager.rules.keys()) == expected

    def test_error_rate_is_critical(self, default_manager: AlertManager) -> None:
        rule = default_manager.rules["high-error-rate"]
        assert rule.severity == AlertSeverity.CRITICAL

    def test_brain_search_uses_burn_rate(self, default_manager: AlertManager) -> None:
        rule = default_manager.rules["brain-search-slow"]
        assert rule.burn_rate_threshold == 2.0

    def test_custom_cooldown(self) -> None:
        mgr = create_default_alert_manager(cooldown_seconds=60.0)
        assert mgr._cooldown_seconds == 60.0  # noqa: SLF001


# ── Property-Based Tests ───────────────────────────────────────────────

class TestAlertManagerProperties:
    """Property-based tests with Hypothesis."""

    @given(
        threshold=st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
        compliance=st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
    )
    @settings(max_examples=50)
    def test_compliance_threshold_consistency(
        self,
        threshold: float,
        compliance: float,
    ) -> None:
        """Alert fires iff compliance < (1 - threshold)."""
        mgr = AlertManager(cooldown_seconds=0.0)
        mgr.add_rule(_make_rule(threshold=threshold, burn_rate_threshold=0.0))
        report = _make_report(compliance=compliance, burn_rate_1h=0.0)
        alerts = mgr.evaluate({"test_metric": report})

        if compliance < (1.0 - threshold):
            assert len(alerts) == 1
        else:
            assert len(alerts) == 0

    @given(
        burn_rate=st.floats(min_value=0.0, max_value=100.0, allow_nan=False),
        burn_threshold=st.floats(min_value=0.1, max_value=50.0, allow_nan=False),
    )
    @settings(max_examples=50)
    def test_burn_rate_threshold_consistency(
        self,
        burn_rate: float,
        burn_threshold: float,
    ) -> None:
        """Alert fires iff burn_rate >= burn_rate_threshold."""
        mgr = AlertManager(cooldown_seconds=0.0)
        mgr.add_rule(_make_rule(threshold=0.0, burn_rate_threshold=burn_threshold))
        report = _make_report(compliance=1.0, burn_rate_1h=burn_rate)
        alerts = mgr.evaluate({"test_metric": report})

        if burn_rate >= burn_threshold:
            assert len(alerts) == 1
        else:
            assert len(alerts) == 0

    @given(value=st.floats(min_value=0.0, max_value=1000.0, allow_nan=False))
    @settings(max_examples=50)
    def test_raw_metric_fires_only_above_threshold(self, value: float) -> None:
        """Raw metric alert fires iff value > threshold."""
        mgr = AlertManager(cooldown_seconds=0.0)
        mgr.add_rule(_make_rule(metric_name="x", threshold=50.0))
        alerts = mgr.evaluate_metrics({"x": value})

        if value > 50.0:
            assert len(alerts) == 1
        else:
            assert len(alerts) == 0
