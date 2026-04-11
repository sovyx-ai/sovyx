"""Sovyx Safety DI Container — dependency injection for safety subsystem.

Replaces module-level singletons with a composable container that holds
all safety components. Tests create isolated containers without
monkey-patching globals.

Usage (production):
    container = get_safety_container()
    audit = container.audit_trail
    tracker = container.escalation_tracker

Usage (test):
    container = SafetyContainer(
        audit_trail=SafetyAuditTrail(max_events=100),
        notifier=SafetyNotifier(sink=FakeSink()),
    )
    # Pass container to code under test — no globals touched.

TASK-390: DI Container — eliminar globals com dependency injection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sovyx.cognitive.audit_store import AuditStore
    from sovyx.cognitive.safety_audit import SafetyAuditTrail
    from sovyx.cognitive.safety_classifier import ClassificationBudget, ClassificationCache
    from sovyx.cognitive.safety_escalation import SafetyEscalationTracker
    from sovyx.cognitive.safety_notifications import NotificationSink, SafetyNotifier


def _default_audit_trail() -> SafetyAuditTrail:
    from sovyx.cognitive.safety_audit import SafetyAuditTrail

    return SafetyAuditTrail()


def _default_classification_budget() -> ClassificationBudget:
    from sovyx.cognitive.safety_classifier import ClassificationBudget

    return ClassificationBudget()


def _default_classification_cache() -> ClassificationCache:
    from sovyx.cognitive.safety_classifier import ClassificationCache

    return ClassificationCache()


def _default_escalation_tracker() -> SafetyEscalationTracker:
    from sovyx.cognitive.safety_escalation import SafetyEscalationTracker

    return SafetyEscalationTracker()


def _default_notifier() -> SafetyNotifier:
    from sovyx.cognitive.safety_notifications import SafetyNotifier

    return SafetyNotifier()


def _default_audit_store() -> AuditStore:
    from sovyx.cognitive.audit_store import AuditStore

    return AuditStore()


@dataclass
class SafetyContainer:
    """Dependency injection container for safety subsystem components.

    All fields have lazy defaults — omitted dependencies are created
    on first access. Pass explicit instances for testing or custom
    configuration.

    Attributes:
        audit_trail: Records safety filter events (blocks, redactions).
        classification_budget: Tracks LLM classification spending.
        classification_cache: LRU cache for safety verdicts.
        escalation_tracker: Tracks consecutive blocks per source.
        notifier: Sends alert notifications on escalation.
        audit_store: SQLite persistence for audit events.
    """

    audit_trail: SafetyAuditTrail = field(default_factory=_default_audit_trail)
    classification_budget: ClassificationBudget = field(
        default_factory=_default_classification_budget,
    )
    classification_cache: ClassificationCache = field(
        default_factory=_default_classification_cache,
    )
    escalation_tracker: SafetyEscalationTracker = field(
        default_factory=_default_escalation_tracker,
    )
    notifier: SafetyNotifier = field(default_factory=_default_notifier)
    audit_store: AuditStore = field(default_factory=_default_audit_store)

    def reset(self) -> None:
        """Clear all component state (for testing between test cases).

        Does NOT replace instances — just resets internal state so tests
        don't leak state between cases.
        """
        self.audit_trail.clear()
        self.classification_cache.clear()
        self.escalation_tracker.clear()
        self.notifier.clear()

    @classmethod
    def for_testing(
        cls,
        *,
        max_audit_events: int = 100,
        notification_sink: NotificationSink | None = None,
    ) -> SafetyContainer:
        """Create a container configured for unit tests.

        - Small audit trail (100 events)
        - Log-only notification sink (or custom)
        - Fresh instances — no shared global state

        Args:
            max_audit_events: Max events in audit trail.
            notification_sink: Custom notification sink (default: log).

        Returns:
            Isolated SafetyContainer for testing.
        """
        from sovyx.cognitive.safety_audit import SafetyAuditTrail
        from sovyx.cognitive.safety_classifier import ClassificationBudget, ClassificationCache
        from sovyx.cognitive.safety_escalation import SafetyEscalationTracker
        from sovyx.cognitive.safety_notifications import SafetyNotifier

        return cls(
            audit_trail=SafetyAuditTrail(max_events=max_audit_events),
            classification_budget=ClassificationBudget(),
            classification_cache=ClassificationCache(),
            escalation_tracker=SafetyEscalationTracker(),
            notifier=SafetyNotifier(sink=notification_sink),
        )


# ── Global default container ───────────────────────────────────────────
# Backward compatibility: existing get_*() functions delegate here.
# Production code that imports get_audit_trail() etc. keeps working.

_container: SafetyContainer | None = None


def get_safety_container() -> SafetyContainer:
    """Get the global SafetyContainer (lazy-initialized).

    Returns:
        The global SafetyContainer instance.
    """
    global _container  # noqa: PLW0603
    if _container is None:
        _container = SafetyContainer()
    return _container


def set_safety_container(container: SafetyContainer) -> SafetyContainer:
    """Replace the global SafetyContainer.

    Used during app bootstrap to inject custom configuration,
    or in tests to inject a test container.

    Args:
        container: The new container to use globally.

    Returns:
        The container that was set (for chaining).
    """
    global _container  # noqa: PLW0603
    _container = container
    return container


def reset_safety_container() -> None:
    """Reset the global container to None (for test teardown).

    Next call to get_safety_container() will create a fresh one.
    """
    global _container  # noqa: PLW0603
    _container = None
