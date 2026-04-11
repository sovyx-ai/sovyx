"""Tests for SafetyContainer — DI container for safety subsystem.

TASK-390: Verify container creation, defaults, testing factory,
reset, and backward compatibility of get_* accessors.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from sovyx.cognitive.safety_container import (
    SafetyContainer,
    get_safety_container,
    reset_safety_container,
    set_safety_container,
)


@pytest.fixture(autouse=True)
def _clean_container() -> None:  # type: ignore[misc]
    """Reset global container before and after each test."""
    reset_safety_container()
    yield  # type: ignore[misc]
    reset_safety_container()


class TestSafetyContainerDefaults:
    """Container creates default instances for all components."""

    def test_creates_audit_trail(self) -> None:
        from sovyx.cognitive.safety_audit import SafetyAuditTrail

        c = SafetyContainer()
        assert isinstance(c.audit_trail, SafetyAuditTrail)

    def test_creates_classification_budget(self) -> None:
        from sovyx.cognitive.safety_classifier import ClassificationBudget

        c = SafetyContainer()
        assert isinstance(c.classification_budget, ClassificationBudget)

    def test_creates_classification_cache(self) -> None:
        from sovyx.cognitive.safety_classifier import ClassificationCache

        c = SafetyContainer()
        assert isinstance(c.classification_cache, ClassificationCache)

    def test_creates_escalation_tracker(self) -> None:
        from sovyx.cognitive.safety_escalation import SafetyEscalationTracker

        c = SafetyContainer()
        assert isinstance(c.escalation_tracker, SafetyEscalationTracker)

    def test_creates_notifier(self) -> None:
        from sovyx.cognitive.safety_notifications import SafetyNotifier

        c = SafetyContainer()
        assert isinstance(c.notifier, SafetyNotifier)

    def test_creates_audit_store(self) -> None:
        from sovyx.cognitive.audit_store import AuditStore

        c = SafetyContainer()
        assert isinstance(c.audit_store, AuditStore)


class TestSafetyContainerForTesting:
    """Factory method creates isolated test containers."""

    def test_for_testing_returns_container(self) -> None:
        c = SafetyContainer.for_testing()
        assert isinstance(c, SafetyContainer)

    def test_for_testing_custom_max_events(self) -> None:
        c = SafetyContainer.for_testing(max_audit_events=50)
        assert c.audit_trail._events.maxlen == 50

    def test_for_testing_custom_sink(self) -> None:
        sink = MagicMock()
        c = SafetyContainer.for_testing(notification_sink=sink)
        assert c.notifier._sink is sink

    def test_for_testing_fresh_instances(self) -> None:
        c1 = SafetyContainer.for_testing()
        c2 = SafetyContainer.for_testing()
        assert c1.audit_trail is not c2.audit_trail
        assert c1.escalation_tracker is not c2.escalation_tracker

    def test_for_testing_does_not_affect_global(self) -> None:
        global_c = get_safety_container()
        test_c = SafetyContainer.for_testing()
        assert global_c.audit_trail is not test_c.audit_trail


class TestSafetyContainerReset:
    """Reset clears internal state without replacing instances."""

    def test_reset_clears_audit_trail(self) -> None:
        from sovyx.cognitive.safety_audit import FilterAction, FilterDirection
        from sovyx.cognitive.safety_patterns import FilterMatch

        c = SafetyContainer.for_testing()
        # Record an event to have state
        match = MagicMock(spec=FilterMatch)
        match.category = MagicMock()
        match.category.value = "test"
        match.tier = MagicMock()
        match.tier.value = "standard"
        match.pattern = MagicMock()
        match.pattern.description = "test pattern"
        c.audit_trail.record(FilterDirection.INPUT, FilterAction.BLOCKED, match)
        assert c.audit_trail.event_count > 0

        c.reset()
        assert c.audit_trail.event_count == 0

    def test_reset_clears_cache(self) -> None:
        from sovyx.cognitive.safety_classifier import SafetyVerdict

        c = SafetyContainer.for_testing()
        c.classification_cache.put("test", SafetyVerdict(safe=True))
        assert c.classification_cache.size > 0

        c.reset()
        assert c.classification_cache.size == 0

    def test_reset_clears_escalation(self) -> None:
        c = SafetyContainer.for_testing()
        c.escalation_tracker.record_block("test-source")
        assert len(c.escalation_tracker._sources) > 0

        c.reset()
        assert len(c.escalation_tracker._sources) == 0

    def test_reset_clears_notifier(self) -> None:
        sink = MagicMock()
        c = SafetyContainer.for_testing(notification_sink=sink)
        c.notifier.notify_escalation("src", 10, "alerted")

        c.reset()
        assert c.notifier.alert_count == 0

    def test_reset_preserves_instances(self) -> None:
        c = SafetyContainer.for_testing()
        trail = c.audit_trail
        cache = c.classification_cache

        c.reset()
        assert c.audit_trail is trail
        assert c.classification_cache is cache


class TestGlobalContainerManagement:
    """Global container get/set/reset functions."""

    def test_get_returns_singleton(self) -> None:
        c1 = get_safety_container()
        c2 = get_safety_container()
        assert c1 is c2

    def test_set_replaces_global(self) -> None:
        custom = SafetyContainer.for_testing()
        set_safety_container(custom)
        assert get_safety_container() is custom

    def test_set_returns_container(self) -> None:
        custom = SafetyContainer.for_testing()
        result = set_safety_container(custom)
        assert result is custom

    def test_reset_clears_global(self) -> None:
        c1 = get_safety_container()
        reset_safety_container()
        c2 = get_safety_container()
        assert c1 is not c2


class TestBackwardCompatibility:
    """Existing get_*() functions still work via container delegation."""

    def test_get_audit_trail_uses_container(self) -> None:
        from sovyx.cognitive.safety_audit import get_audit_trail

        container = get_safety_container()
        assert get_audit_trail() is container.audit_trail

    def test_get_classification_budget_uses_container(self) -> None:
        from sovyx.cognitive.safety_classifier import get_classification_budget

        container = get_safety_container()
        assert get_classification_budget() is container.classification_budget

    def test_get_classification_cache_uses_container(self) -> None:
        from sovyx.cognitive.safety_classifier import get_classification_cache

        container = get_safety_container()
        assert get_classification_cache() is container.classification_cache

    def test_get_escalation_tracker_uses_container(self) -> None:
        from sovyx.cognitive.safety_escalation import get_escalation_tracker

        container = get_safety_container()
        assert get_escalation_tracker() is container.escalation_tracker

    def test_get_notifier_uses_container(self) -> None:
        from sovyx.cognitive.safety_notifications import get_notifier

        container = get_safety_container()
        assert get_notifier() is container.notifier

    def test_setup_audit_trail_updates_container(self) -> None:
        from sovyx.cognitive.safety_audit import setup_audit_trail

        old = get_safety_container().audit_trail
        new = setup_audit_trail(max_events=50)
        assert new is not old
        assert get_safety_container().audit_trail is new
        assert new._events.maxlen == 50

    def test_setup_notifier_updates_container(self) -> None:
        from sovyx.cognitive.safety_notifications import setup_notifier

        sink = MagicMock()
        old = get_safety_container().notifier
        new = setup_notifier(sink=sink)
        assert new is not old
        assert get_safety_container().notifier is new
        assert new._sink is sink

    def test_custom_container_changes_get_functions(self) -> None:
        """Setting a custom container makes get_*() return its components."""
        from sovyx.cognitive.safety_audit import get_audit_trail
        from sovyx.cognitive.safety_escalation import get_escalation_tracker

        custom = SafetyContainer.for_testing()
        set_safety_container(custom)

        assert get_audit_trail() is custom.audit_trail
        assert get_escalation_tracker() is custom.escalation_tracker


class TestContainerIsolation:
    """Containers are independent — no cross-contamination."""

    def test_two_containers_independent_audit(self) -> None:
        from sovyx.cognitive.safety_audit import FilterAction, FilterDirection
        from sovyx.cognitive.safety_patterns import FilterMatch

        c1 = SafetyContainer.for_testing()
        c2 = SafetyContainer.for_testing()

        match = MagicMock(spec=FilterMatch)
        match.category = MagicMock()
        match.category.value = "test"
        match.tier = MagicMock()
        match.tier.value = "standard"
        match.pattern = MagicMock()
        match.pattern.description = "test"

        c1.audit_trail.record(FilterDirection.INPUT, FilterAction.BLOCKED, match)
        assert c1.audit_trail.event_count == 1
        assert c2.audit_trail.event_count == 0

    def test_two_containers_independent_escalation(self) -> None:
        from sovyx.cognitive.safety_escalation import EscalationLevel

        c1 = SafetyContainer.for_testing()
        c2 = SafetyContainer.for_testing()

        # Record enough blocks to trigger warning (threshold=3)
        for _ in range(3):
            c1.escalation_tracker.record_block("source-a")
        assert c1.escalation_tracker.get_level("source-a") == EscalationLevel.WARNING
        assert c2.escalation_tracker.get_level("source-a") == EscalationLevel.NONE

    def test_two_containers_independent_cache(self) -> None:
        from sovyx.cognitive.safety_classifier import SafetyVerdict

        c1 = SafetyContainer.for_testing()
        c2 = SafetyContainer.for_testing()

        c1.classification_cache.put("hello", SafetyVerdict(safe=True))
        assert c1.classification_cache.get("hello") is not None
        assert c2.classification_cache.get("hello") is None
