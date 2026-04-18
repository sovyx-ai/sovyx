"""Shared fixtures for voice unit tests.

Provides a session-scoped ``setup_logging`` invocation so structlog routes
through the stdlib logging pipeline. Voice telemetry tests
(``TestPipelineHeartbeat``, ``TestRecordingLifecycleLogs``,
``TestSTTAndPerceptionLogs``, ``TestStateTransitionTelemetry``) rely on
pytest's ``caplog`` fixture to observe structured events emitted by the
orchestrator and VAD — and ``caplog`` intercepts stdlib ``LogRecord``
instances, so the structlog chain must end at ``wrap_for_formatter``.

Why this matters
----------------
``capture_logs`` was the first-reach capture primitive, but it relies on
mutating the *current* ``_CONFIG.default_processors`` list in place; any
earlier ``structlog.configure`` call (``setup_logging`` installs a **new**
processors list on every invocation and ``tests/unit/observability`` runs
it dozens of times before the voice suite) orphans bound-logger references
to the previous list, and ``capture_logs`` silently yields an empty
sequence under full-suite CI ordering. The stdlib path is immune to that
because every ``LogRecord`` reaches the root logger regardless of which
processor list the BoundLogger holds.

Running the voice subset in isolation wouldn't hit the orphaning bug, but
``setup_logging`` also isn't invoked in that case so structlog still uses
its bootstrap defaults (pretty console renderer, no stdlib factory) and
``caplog`` captures nothing. Invoking ``setup_logging`` once here makes
the voice suite deterministic and order-independent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from sovyx.engine.config import LoggingConfig
from sovyx.observability.logging import setup_logging

if TYPE_CHECKING:
    from collections.abc import Generator


@pytest.fixture(scope="session", autouse=True)
def _voice_structlog_stdlib_routing() -> Generator[None, None, None]:
    setup_logging(LoggingConfig(level="DEBUG", console_format="json", log_file=None))
    yield
