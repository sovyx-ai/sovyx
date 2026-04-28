"""Capture-task subpackage — extracted from the monolithic
``voice/_capture_task.py`` per master mission Phase 1 / T1.4.

Target final structure (per spec):

  * ``_restart.py`` — restart-verdict types + dataclasses + metric
    emitters (this module — landed first as the lowest-risk extraction).
  * ``_loop.py`` — consumer-loop logic (future commit).
  * ``_ring.py`` — ring-buffer state + access helpers (future commit).
  * ``_epoch.py`` — ring-buffer epoch packing (future commit).

The legacy stub at ``voice/_capture_task.py`` re-exports every public
name from this subpackage so existing imports + the 13 timing-
primitive test patches keep working without an import-path migration.
The split is structurally compatible with CLAUDE.md anti-pattern #16
(god-file split via subpackage with ``__init__.py`` re-exports).
"""

from __future__ import annotations

from sovyx.voice.capture._exceptions import (
    CaptureDeviceContendedError,
    CaptureError,
    CaptureInoperativeError,
    CaptureSilenceError,
)
from sovyx.voice.capture._restart import (
    _LINUX_ALSA_HOST_API,
    _LINUX_SESSION_MANAGER_HOST_APIS,
    AlsaHwDirectRestartResult,
    AlsaHwDirectRestartVerdict,
    ExclusiveRestartResult,
    ExclusiveRestartVerdict,
    SessionManagerRestartResult,
    SessionManagerRestartVerdict,
    SharedRestartResult,
    SharedRestartVerdict,
    _emit_alsa_hw_direct_restart_metric,
    _emit_exclusive_restart_metric,
    _emit_session_manager_restart_metric,
    _emit_shared_restart_metric,
)

__all__ = [
    "_LINUX_ALSA_HOST_API",
    "_LINUX_SESSION_MANAGER_HOST_APIS",
    "AlsaHwDirectRestartResult",
    "AlsaHwDirectRestartVerdict",
    "CaptureDeviceContendedError",
    "CaptureError",
    "CaptureInoperativeError",
    "CaptureSilenceError",
    "ExclusiveRestartResult",
    "ExclusiveRestartVerdict",
    "SessionManagerRestartResult",
    "SessionManagerRestartVerdict",
    "SharedRestartResult",
    "SharedRestartVerdict",
    "_emit_alsa_hw_direct_restart_metric",
    "_emit_exclusive_restart_metric",
    "_emit_session_manager_restart_metric",
    "_emit_shared_restart_metric",
]
