"""Tests for SLO monitoring (V05-01).

Covers: SLODefinition, SLOTracker, SLOMonitor, burn rate math,
multi-window alerting, factory functions, edge cases.
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from sovyx.observability.slo import (
    STANDARD_ALERT_RULES,
    AlertSeverity,
    BurnRateAlertRule,
    SLODefinition,
    SLOEvent,
    SLOMonitor,
    SLOReport,
    SLOStatus,
    SLOTracker,
    _severity_rank,
    create_default_monitor,
)

# ── SLODefinition ───────────────────────────────────────────────────────────


class TestSLODefinition:
    """Tests for SLODefinition dataclass."""

    def test_basic_creation(self) -> None:
        defn = SLODefinition(
            name="Test SLO",
            description="Test description",
            target=0.99,
            threshold=100.0,
            unit="ms",
        )
        assert defn.name == "Test SLO"
        assert defn.target == 0.99
        assert defn.threshold == 100.0
        assert defn.unit == "ms"
        assert defn.window_days == 30  # default

    def test_error_budget(self) -> None:
        defn = SLODefinition(
            name="Avail", description="99.5%", target=0.995,
            threshold=1.0, unit="bool",
        )
        assert defn.error_budget == pytest.approx(0.005)

    def test_error_budget_100pct_target(self) -> None:
        defn = SLODefinition(
            name="Perfect", description="100%", target=1.0,
            threshold=1.0, unit="bool",
        )
        assert defn.error_budget == 0.0

    def test_custom_window_days(self) -> None:
        defn = SLODefinition(
            name="Weekly", description="Weekly SLO", target=0.99,
            threshold=50.0, unit="ms", window_days=7,
        )
        assert defn.window_days == 7
        assert defn.error_budget == pytest.approx(0.01)

    def test_frozen(self) -> None:
        defn = SLODefinition(
            name="Frozen", description="Immutable", target=0.99,
            threshold=1.0, unit="bool",
        )
        with pytest.raises(AttributeError):
            defn.name = "Changed"  # type: ignore[misc]


# ── BurnRateAlertRule ───────────────────────────────────────────────────────


class TestBurnRateAlertRule:
    """Tests for multi-window burn rate alert rules."""

    def test_check_both_windows_exceed(self) -> None:
        rule = BurnRateAlertRule(AlertSeverity.PAGE, 60, 5, 14.4)
        # error_budget = 0.005 (99.5% target)
        # burn rate threshold 14.4 → need error_rate >= 14.4 * 0.005 = 0.072
        assert rule.check(0.08, 0.08, 0.005)

    def test_check_long_window_below(self) -> None:
        rule = BurnRateAlertRule(AlertSeverity.PAGE, 60, 5, 14.4)
        # Long window below threshold, short above
        assert not rule.check(0.01, 0.08, 0.005)

    def test_check_short_window_below(self) -> None:
        rule = BurnRateAlertRule(AlertSeverity.PAGE, 60, 5, 14.4)
        # Long above, short below
        assert not rule.check(0.08, 0.01, 0.005)

    def test_check_zero_error_budget(self) -> None:
        rule = BurnRateAlertRule(AlertSeverity.PAGE, 60, 5, 14.4)
        # 100% target → 0 error budget → never fires
        assert not rule.check(0.5, 0.5, 0.0)

    def test_check_negative_error_budget(self) -> None:
        rule = BurnRateAlertRule(AlertSeverity.PAGE, 60, 5, 14.4)
        assert not rule.check(0.5, 0.5, -0.01)

    def test_exact_threshold(self) -> None:
        rule = BurnRateAlertRule(AlertSeverity.TICKET, 4320, 360, 1.0)
        # burn = error_rate / budget → need exactly 1.0
        # budget = 0.01, so error_rate = 0.01
        assert rule.check(0.01, 0.01, 0.01)


# ── SLOEvent ────────────────────────────────────────────────────────────────


class TestSLOEvent:
    """Tests for SLOEvent dataclass."""

    def test_creation(self) -> None:
        event = SLOEvent(timestamp=1000.0, success=True, value=42.0)
        assert event.timestamp == 1000.0
        assert event.success is True
        assert event.value == 42.0

    def test_frozen(self) -> None:
        event = SLOEvent(timestamp=1.0, success=True, value=0.0)
        with pytest.raises(AttributeError):
            event.success = False  # type: ignore[misc]


# ── SLOTracker ──────────────────────────────────────────────────────────────


class TestSLOTracker:
    """Tests for per-SLO tracker with sliding window."""

    @pytest.fixture()
    def defn(self) -> SLODefinition:
        return SLODefinition(
            name="Brain Search",
            description="p95 < 100ms",
            target=0.95,
            threshold=100.0,
            unit="ms",
        )

    @pytest.fixture()
    def tracker(self, defn: SLODefinition) -> SLOTracker:
        return SLOTracker(defn, max_events=1000)

    # -- Basic recording --

    def test_empty_tracker(self, tracker: SLOTracker) -> None:
        assert tracker.event_count == 0
        assert tracker.success_rate() == 1.0

    def test_record_single_event(self, tracker: SLOTracker) -> None:
        tracker.record_event(success=True, value=50.0)
        assert tracker.event_count == 1
        assert tracker.success_rate() == 1.0

    def test_record_mixed_events(self, tracker: SLOTracker) -> None:
        for _ in range(90):
            tracker.record_event(success=True, value=50.0)
        for _ in range(10):
            tracker.record_event(success=False, value=150.0)
        assert tracker.event_count == 100
        assert tracker.success_rate() == pytest.approx(0.9)

    def test_max_events_eviction(self) -> None:
        defn = SLODefinition(
            name="Small", description="test", target=0.99,
            threshold=1.0, unit="bool",
        )
        tracker = SLOTracker(defn, max_events=10)
        for i in range(20):
            tracker.record_event(success=True, value=float(i))
        assert tracker.event_count == 10

    # -- Error rate in window --

    def test_error_rate_empty(self, tracker: SLOTracker) -> None:
        assert tracker.error_rate_in_window(3600) == 0.0

    def test_error_rate_all_success(self, tracker: SLOTracker) -> None:
        for _ in range(50):
            tracker.record_event(success=True, value=10.0)
        assert tracker.error_rate_in_window(3600) == 0.0

    def test_error_rate_all_failure(self, tracker: SLOTracker) -> None:
        for _ in range(50):
            tracker.record_event(success=False, value=200.0)
        assert tracker.error_rate_in_window(3600) == 1.0

    def test_error_rate_mixed(self, tracker: SLOTracker) -> None:
        # 80 success, 20 failure
        for _ in range(80):
            tracker.record_event(success=True, value=50.0)
        for _ in range(20):
            tracker.record_event(success=False, value=150.0)
        assert tracker.error_rate_in_window(3600) == pytest.approx(0.2)

    def test_error_rate_window_filters_old_events(self, defn: SLODefinition) -> None:
        tracker = SLOTracker(defn, max_events=1000)
        # Record events, then patch monotonic to simulate time passage
        base_time = time.monotonic()
        with patch("sovyx.observability.slo.time.monotonic", return_value=base_time):
            tracker.record_event(success=False, value=200.0)  # Old event

        # Move time forward 2 hours
        with patch("sovyx.observability.slo.time.monotonic", return_value=base_time + 7200):
            tracker.record_event(success=True, value=50.0)  # Recent event

        # Query with 1-hour window — should only see the recent event
        with patch("sovyx.observability.slo.time.monotonic", return_value=base_time + 7200):
            rate = tracker.error_rate_in_window(3600)
        assert rate == 0.0

    # -- Burn rate --

    def test_burn_rate_no_errors(self, tracker: SLOTracker) -> None:
        for _ in range(100):
            tracker.record_event(success=True, value=50.0)
        assert tracker.get_burn_rate(60) == 0.0

    def test_burn_rate_all_errors(self, tracker: SLOTracker) -> None:
        for _ in range(100):
            tracker.record_event(success=False, value=200.0)
        # error_budget = 0.05 (target 0.95), error_rate = 1.0
        # burn = 1.0 / 0.05 = 20.0
        assert tracker.get_burn_rate(60) == pytest.approx(20.0)

    def test_burn_rate_zero_budget(self) -> None:
        defn = SLODefinition(
            name="Perfect", description="100%", target=1.0,
            threshold=1.0, unit="bool",
        )
        tracker = SLOTracker(defn)
        tracker.record_event(success=False, value=0.0)
        assert tracker.get_burn_rate(60) == 0.0

    def test_burn_rate_partial_errors(self, tracker: SLOTracker) -> None:
        # 95 success + 5 failure = 5% error rate
        for _ in range(95):
            tracker.record_event(success=True, value=50.0)
        for _ in range(5):
            tracker.record_event(success=False, value=150.0)
        # error_budget = 0.05, error_rate = 0.05
        # burn = 0.05 / 0.05 = 1.0
        assert tracker.get_burn_rate(60) == pytest.approx(1.0)

    # -- Error budget remaining --

    def test_error_budget_remaining_full(self, tracker: SLOTracker) -> None:
        for _ in range(100):
            tracker.record_event(success=True, value=50.0)
        assert tracker.get_error_budget_remaining_pct() == 100.0

    def test_error_budget_remaining_zero(self, tracker: SLOTracker) -> None:
        for _ in range(100):
            tracker.record_event(success=False, value=200.0)
        # Consumed way more than budget → clamped to 0
        assert tracker.get_error_budget_remaining_pct() == 0.0

    def test_error_budget_remaining_zero_budget_defn(self) -> None:
        defn = SLODefinition(
            name="Perfect", description="100%", target=1.0,
            threshold=1.0, unit="bool",
        )
        tracker = SLOTracker(defn)
        assert tracker.get_error_budget_remaining_pct() == 100.0

    # -- Alerts --

    def test_no_alerts_when_healthy(self, tracker: SLOTracker) -> None:
        for _ in range(1000):
            tracker.record_event(success=True, value=50.0)
        assert tracker.check_alerts() == AlertSeverity.NONE

    def test_page_alert_on_high_error_rate(self, tracker: SLOTracker) -> None:
        # Massive errors → should trigger PAGE alerts
        for _ in range(1000):
            tracker.record_event(success=False, value=200.0)
        assert tracker.check_alerts() == AlertSeverity.PAGE

    def test_check_alerts_empty_tracker(self, tracker: SLOTracker) -> None:
        assert tracker.check_alerts() == AlertSeverity.NONE

    # -- Status --

    def test_status_met(self, tracker: SLOTracker) -> None:
        for _ in range(100):
            tracker.record_event(success=True, value=50.0)
        assert tracker.get_status() == SLOStatus.MET

    def test_status_breached(self, tracker: SLOTracker) -> None:
        # 90% success < 95% target → breached
        for _ in range(90):
            tracker.record_event(success=True, value=50.0)
        for _ in range(10):
            tracker.record_event(success=False, value=200.0)
        # 90/100 = 0.9 < 0.95 target
        assert tracker.get_status() == SLOStatus.BREACHED

    def test_status_warning_high_burn_rate(self, defn: SLODefinition) -> None:
        # Need: success_rate >= target BUT burn_rate_1h >= 6.0
        # This is tricky — we need overall success rate >= 0.95 but recent burn >= 6
        # Use time mocking: mostly good history + recent spike
        tracker = SLOTracker(defn, max_events=10000)
        base = time.monotonic()

        # 9500 successes "2 hours ago"
        with patch("sovyx.observability.slo.time.monotonic", return_value=base - 7200):
            for _ in range(9500):
                tracker.record_event(success=True, value=50.0)

        # 500 failures "now" (within last hour) — but total 95% = target met
        with patch("sovyx.observability.slo.time.monotonic", return_value=base):
            for _ in range(500):
                tracker.record_event(success=False, value=200.0)

        with patch("sovyx.observability.slo.time.monotonic", return_value=base):
            status = tracker.get_status()

        assert status == SLOStatus.WARNING

    # -- Report --

    def test_report_structure(self, tracker: SLOTracker) -> None:
        tracker.record_event(success=True, value=50.0)
        report = tracker.get_report()
        assert isinstance(report, SLOReport)
        assert report.name == "Brain Search"
        assert report.target == 0.95
        assert report.unit == "ms"
        assert report.threshold == 100.0
        assert report.event_count == 1
        assert report.current_rate == 1.0
        assert report.status == SLOStatus.MET
        assert report.alert_severity == AlertSeverity.NONE

    def test_definition_property(self, tracker: SLOTracker, defn: SLODefinition) -> None:
        assert tracker.definition is defn


# ── SLOMonitor ──────────────────────────────────────────────────────────────


class TestSLOMonitor:
    """Tests for the aggregate SLO monitor."""

    @pytest.fixture()
    def monitor(self) -> SLOMonitor:
        return SLOMonitor()

    def test_default_slo_keys(self, monitor: SLOMonitor) -> None:
        keys = monitor.slo_keys
        assert "brain_search" in keys
        assert "response_time" in keys
        assert "availability" in keys
        assert "error_rate" in keys
        assert "cost_per_message" in keys
        assert len(keys) == 5

    def test_get_tracker(self, monitor: SLOMonitor) -> None:
        tracker = monitor.get_tracker("brain_search")
        assert isinstance(tracker, SLOTracker)
        assert tracker.definition.name == "Brain Search Latency"

    def test_get_tracker_unknown(self, monitor: SLOMonitor) -> None:
        with pytest.raises(KeyError, match="Unknown SLO"):
            monitor.get_tracker("nonexistent")

    def test_record_event(self, monitor: SLOMonitor) -> None:
        monitor.record_event("brain_search", success=True, value=42.0)
        tracker = monitor.get_tracker("brain_search")
        assert tracker.event_count == 1

    def test_record_event_unknown_slo(self, monitor: SLOMonitor) -> None:
        with pytest.raises(KeyError):
            monitor.record_event("nonexistent", success=True, value=0.0)

    def test_record_latency(self, monitor: SLOMonitor) -> None:
        monitor.record_latency("brain_search", 50.0)  # < 100ms → success
        monitor.record_latency("brain_search", 150.0)  # > 100ms → failure
        tracker = monitor.get_tracker("brain_search")
        assert tracker.event_count == 2
        assert tracker.success_rate() == pytest.approx(0.5)

    def test_record_latency_at_threshold(self, monitor: SLOMonitor) -> None:
        monitor.record_latency("brain_search", 100.0)  # == threshold → success
        tracker = monitor.get_tracker("brain_search")
        assert tracker.success_rate() == 1.0

    def test_record_cost(self, monitor: SLOMonitor) -> None:
        monitor.record_cost(0.005)  # < $0.01 → success
        monitor.record_cost(0.02)  # > $0.01 → failure
        tracker = monitor.get_tracker("cost_per_message")
        assert tracker.event_count == 2
        assert tracker.success_rate() == pytest.approx(0.5)

    def test_get_report(self, monitor: SLOMonitor) -> None:
        monitor.record_event("availability", success=True, value=1.0)
        report = monitor.get_report()
        assert isinstance(report, dict)
        assert len(report) == 5
        for _key, slo_report in report.items():
            assert isinstance(slo_report, SLOReport)

    def test_get_breached_slos_empty(self, monitor: SLOMonitor) -> None:
        # No events → all SLOs default to MET (success_rate=1.0)
        assert monitor.get_breached_slos() == []

    def test_get_breached_slos(self, monitor: SLOMonitor) -> None:
        # Breach brain_search SLO
        for _ in range(100):
            monitor.record_event("brain_search", success=False, value=200.0)
        breached = monitor.get_breached_slos()
        assert "brain_search" in breached

    def test_get_active_alerts_none(self, monitor: SLOMonitor) -> None:
        for _ in range(100):
            monitor.record_event("brain_search", success=True, value=50.0)
        assert monitor.get_active_alerts() == {}

    def test_get_active_alerts_triggered(self, monitor: SLOMonitor) -> None:
        for _ in range(1000):
            monitor.record_event("error_rate", success=False, value=1.0)
        alerts = monitor.get_active_alerts()
        assert "error_rate" in alerts
        assert alerts["error_rate"] == AlertSeverity.PAGE

    def test_custom_slos(self) -> None:
        custom = {
            "latency": SLODefinition(
                name="Custom Latency",
                description="p99 < 50ms",
                target=0.99,
                threshold=50.0,
                unit="ms",
            ),
        }
        monitor = SLOMonitor(slos=custom)
        assert monitor.slo_keys == ["latency"]

    def test_custom_max_events(self) -> None:
        monitor = SLOMonitor(max_events_per_slo=50)
        for _ in range(100):
            monitor.record_event("brain_search", success=True, value=10.0)
        tracker = monitor.get_tracker("brain_search")
        assert tracker.event_count == 50


# ── Factory ─────────────────────────────────────────────────────────────────


class TestFactory:
    """Tests for create_default_monitor factory."""

    def test_creates_monitor(self) -> None:
        monitor = create_default_monitor()
        assert isinstance(monitor, SLOMonitor)
        assert len(monitor.slo_keys) == 5

    def test_custom_max_events(self) -> None:
        monitor = create_default_monitor(max_events_per_slo=500)
        # Fill beyond limit
        for _ in range(600):
            monitor.record_event("brain_search", success=True, value=10.0)
        assert monitor.get_tracker("brain_search").event_count == 500


# ── Constants ───────────────────────────────────────────────────────────────


class TestConstants:
    """Tests for module-level constants."""

    def test_standard_alert_rules(self) -> None:
        assert len(STANDARD_ALERT_RULES) == 3
        # PAGE, PAGE, TICKET
        assert STANDARD_ALERT_RULES[0].severity == AlertSeverity.PAGE
        assert STANDARD_ALERT_RULES[1].severity == AlertSeverity.PAGE
        assert STANDARD_ALERT_RULES[2].severity == AlertSeverity.TICKET

    def test_standard_alert_rules_windows(self) -> None:
        fast, medium, slow = STANDARD_ALERT_RULES
        assert fast.long_window_minutes == 60
        assert fast.short_window_minutes == 5
        assert medium.long_window_minutes == 360
        assert slow.long_window_minutes == 4320


# ── Helpers ─────────────────────────────────────────────────────────────────


class TestHelpers:
    """Tests for helper functions."""

    def test_severity_rank_ordering(self) -> None:
        assert _severity_rank(AlertSeverity.NONE) < _severity_rank(AlertSeverity.TICKET)
        assert _severity_rank(AlertSeverity.TICKET) < _severity_rank(AlertSeverity.PAGE)

    def test_severity_rank_unknown(self) -> None:
        # Should return 0 for unknown values
        assert _severity_rank("bogus") == 0  # type: ignore[arg-type]


# ── Enums ───────────────────────────────────────────────────────────────────


class TestEnums:
    """Tests for enum values."""

    def test_slo_status_values(self) -> None:
        assert SLOStatus.MET.value == "met"
        assert SLOStatus.WARNING.value == "warning"
        assert SLOStatus.BREACHED.value == "breached"

    def test_alert_severity_values(self) -> None:
        assert AlertSeverity.NONE.value == "none"
        assert AlertSeverity.TICKET.value == "ticket"
        assert AlertSeverity.PAGE.value == "page"


# ── Property-based tests ───────────────────────────────────────────────────


class TestPropertyBased:
    """Hypothesis property-based tests for SLO invariants."""

    @settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(
        success_count=st.integers(min_value=0, max_value=200),
        failure_count=st.integers(min_value=0, max_value=200),
    )
    def test_success_rate_bounded(self, success_count: int, failure_count: int) -> None:
        """Success rate must always be in [0.0, 1.0]."""
        defn = SLODefinition(
            name="Test", description="test", target=0.95,
            threshold=100.0, unit="ms",
        )
        tracker = SLOTracker(defn, max_events=10000)
        for _ in range(success_count):
            tracker.record_event(success=True, value=50.0)
        for _ in range(failure_count):
            tracker.record_event(success=False, value=200.0)
        rate = tracker.success_rate()
        assert 0.0 <= rate <= 1.0

    @settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(
        target=st.floats(min_value=0.5, max_value=1.0),
    )
    def test_error_budget_non_negative(self, target: float) -> None:
        """Error budget must be in [0.0, 0.5]."""
        defn = SLODefinition(
            name="Test", description="test", target=target,
            threshold=1.0, unit="bool",
        )
        assert 0.0 <= defn.error_budget <= 0.5

    @settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(
        n_events=st.integers(min_value=1, max_value=500),
    )
    def test_burn_rate_non_negative(self, n_events: int) -> None:
        """Burn rate must always be >= 0."""
        defn = SLODefinition(
            name="Test", description="test", target=0.95,
            threshold=100.0, unit="ms",
        )
        tracker = SLOTracker(defn, max_events=10000)
        for _ in range(n_events):
            tracker.record_event(success=False, value=200.0)
        assert tracker.get_burn_rate(60) >= 0.0

    @settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(
        n_success=st.integers(min_value=0, max_value=100),
        n_failure=st.integers(min_value=0, max_value=100),
    )
    def test_error_budget_remaining_bounded(self, n_success: int, n_failure: int) -> None:
        """Error budget remaining must be in [0, 100]."""
        defn = SLODefinition(
            name="Test", description="test", target=0.95,
            threshold=100.0, unit="ms",
        )
        tracker = SLOTracker(defn, max_events=10000)
        for _ in range(n_success):
            tracker.record_event(success=True, value=50.0)
        for _ in range(n_failure):
            tracker.record_event(success=False, value=200.0)
        pct = tracker.get_error_budget_remaining_pct()
        assert 0.0 <= pct <= 100.0

    @settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(
        latency=st.floats(min_value=0.0, max_value=10000.0),
    )
    def test_record_latency_auto_classifies(self, latency: float) -> None:
        """record_latency should auto-determine success based on threshold."""
        monitor = SLOMonitor()
        monitor.record_latency("brain_search", latency)
        tracker = monitor.get_tracker("brain_search")
        event_success = tracker.success_rate() == 1.0
        expected_success = latency <= 100.0
        assert event_success == expected_success
