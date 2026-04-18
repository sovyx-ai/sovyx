"""Shared fixtures for voice unit tests.

Provides a fresh-proxy rebinding fixture so ``structlog.testing.capture_logs``
reliably intercepts events emitted by the ``_orchestrator`` / ``vad`` modules.

Root cause addressed
--------------------
``sovyx.observability.logging.setup_logging`` calls ``structlog.configure(...)``
with a freshly-constructed processors list and ``cache_logger_on_first_use=True``.
The observability test suite (``tests/unit/observability/test_logging.py``) invokes
``setup_logging`` dozens of times, each call replacing ``_CONFIG.default_processors``
with a new list object.

Any ``structlog.BoundLoggerLazyProxy`` that was first used **before** the final
``setup_logging`` call caches a :class:`structlog.stdlib.BoundLogger` holding a
reference to a now-stale processor list. ``capture_logs`` mutates the *current*
``_CONFIG.default_processors`` list, so cached loggers pointing at a previous
list never see the capturing ``LogCapture`` processor — events continue flowing
through the production chain, and ``capture_logs()`` yields an empty list.

Why this only surfaces in CI
----------------------------
Locally, running only the voice subset keeps the cache coherent; the failure is
deterministic in the full-suite CI run where observability tests execute before
voice tests (alphabetical collection).

Enterprise fix
--------------
This fixture rebinds the module-level ``logger`` attribute of the voice modules
to a fresh ``BoundLoggerLazyProxy`` before each test. The fresh proxy is not
bound yet, so its first log call inside the test — whether before or inside a
``capture_logs()`` block — binds against the *current* ``_CONFIG`` processors.
When ``capture_logs`` mutates that list in place, the proxy's cached
``BoundLogger`` observes the mutation, and events are captured correctly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import structlog

if TYPE_CHECKING:
    from collections.abc import Generator


@pytest.fixture(autouse=True)
def _rebind_voice_module_loggers() -> Generator[None, None, None]:
    from sovyx.voice import vad as _vad_mod
    from sovyx.voice.pipeline import _orchestrator as _orch_mod

    orch_original = _orch_mod.logger
    vad_original = _vad_mod.logger
    _orch_mod.logger = structlog.get_logger(_orch_mod.__name__)
    _vad_mod.logger = structlog.get_logger(_vad_mod.__name__)
    try:
        yield
    finally:
        _orch_mod.logger = orch_original
        _vad_mod.logger = vad_original
