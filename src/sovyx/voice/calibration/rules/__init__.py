"""Calibration rule registry + discovery helper.

Rules live as one file per rule in this package, named
``R<NN>_<short_slug>.py``. Each module exports a module-level
``rule: CalibrationRule`` singleton; :func:`iter_rules` walks the
package via :func:`pkgutil.iter_modules` and yields every singleton
it finds.

Adding a new rule (for L2 maintainers):

1. Create ``R<NN>_<slug>.py`` next to this file.
2. Implement a ``_Rule`` class satisfying :class:`CalibrationRule`.
3. Bind a module-level ``rule = _Rule()`` so discovery picks it up.
4. Add a unit test in ``tests/unit/voice/calibration/test_R<NN>_<slug>.py``.
5. Bump :data:`RULE_SET_VERSION` here.

Discovery is intentionally NOT entry-point-based -- rules are first-
party code only, never plugin-extensible, so the calibration engine
behaviour stays auditable from a single module tree.
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from sovyx.voice.calibration.rules._base import CalibrationRule

from sovyx.voice.calibration.rules._base import (
    CalibrationRule,
    RuleContext,
    RuleEvaluation,
)

# Bumped on every rule addition / removal / edit that changes the rule
# count or any rule's rule_version. Persisted in CalibrationProfile so
# the loader (T2.7) can detect drift between profile-time and runtime
# rule sets and warn / refuse to replay stale profiles.
RULE_SET_VERSION = 12

__all__ = [
    "RULE_SET_VERSION",
    "CalibrationRule",
    "RuleContext",
    "RuleEvaluation",
    "iter_rules",
    "iter_unreachable_rules",
]


def iter_unreachable_rules() -> Iterable[tuple[str, str]]:
    """Yield ``(rule_id, reason)`` for every discovered rule that declares
    itself UNREACHABLE at HEAD.

    W1.3 (anti-pattern #48) — a rule is "unreachable" when it exists and is
    unit-tested with synthetic inputs but cannot fire from real captured
    data because its ``applies()`` gate reads a field that has no producer
    yet (e.g. ``fingerprint.apo_active`` / ``measurements.echo_correlation_db``
    are hardcoded sentinels). Such a rule sets a module-level
    ``unreachable_reason`` string; this helper is the single source of truth
    that the ``--evaluate-rules`` preview discloses and the rule-honesty test
    enforces. Wiring the missing producers is tracked in W3/W4 of
    MISSION-VOICE-DEEP-INVESTIGATION-2026-06-01.md.
    """
    for rule in iter_rules():
        reason = getattr(rule, "unreachable_reason", None)
        if reason:
            yield rule.rule_id, str(reason)


def iter_rules() -> Iterable[CalibrationRule]:
    """Discover every ``rule: CalibrationRule`` singleton under this package.

    Walks the package via :func:`pkgutil.iter_modules`, imports each
    public ``R*`` module, and yields its ``rule`` attribute if it is
    a :class:`CalibrationRule` instance.

    Modules whose names start with an underscore (e.g. ``_base``) are
    skipped. Modules without a ``rule`` attribute or whose ``rule``
    fails the runtime-checkable Protocol check are also skipped --
    the discovery helper never raises, so a malformed rule never
    blocks engine instantiation. Operators inspecting the rule set
    use ``sovyx doctor voice --calibrate --show-rules`` (T2.9 surface).
    """
    package = __name__
    pkg_module = importlib.import_module(package)
    for info in pkgutil.iter_modules(pkg_module.__path__):
        if info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{package}.{info.name}")
        candidate = getattr(module, "rule", None)
        if candidate is None:
            continue
        if isinstance(candidate, CalibrationRule):
            yield candidate
