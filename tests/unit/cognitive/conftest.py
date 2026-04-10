"""Shared fixtures for cognitive tests."""

from __future__ import annotations

import pytest

from sovyx.cognitive.safety_audit import get_audit_trail
from sovyx.cognitive.safety_escalation import get_escalation_tracker


@pytest.fixture(autouse=True)
def _clear_safety_singletons() -> None:
    """Reset safety singletons between tests to prevent state leakage."""
    get_escalation_tracker().clear()
    get_audit_trail().clear()
