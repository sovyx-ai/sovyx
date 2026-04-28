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

from sovyx.voice.capture._constants import (
    _CAPTURE_UNDERRUN_MIN_CALLBACKS,
    _CAPTURE_UNDERRUN_WARN_FRACTION,
    _CAPTURE_UNDERRUN_WARN_INTERVAL_S,
    _CAPTURE_UNDERRUN_WINDOW_S,
    _FRAME_SAMPLES,
    _HEARTBEAT_INTERVAL_S,
    _QUEUE_MAXSIZE,
    _RECONNECT_DELAY_S,
    _RING_EPOCH_SHIFT,
    _RING_SAMPLES_MASK,
    _SAMPLE_RATE,
    _VALIDATION_MIN_RMS_DB,
    _VALIDATION_S,
)
from sovyx.voice.capture._contention import (
    _SESSION_MANAGER_CONTENTION_ERROR_CODES,
    _is_session_manager_contention_pattern,
    _suggest_session_manager_alternatives,
)
from sovyx.voice.capture._epoch import EpochMixin
from sovyx.voice.capture._exceptions import (
    CaptureDeviceContendedError,
    CaptureError,
    CaptureInoperativeError,
    CaptureSilenceError,
)
from sovyx.voice.capture._helpers import (
    _PEAK_DB_RE,
    _RMS_FLOOR_DB,
    _extract_peak_db,
    _resolve_input_entry,
    _rms_db_int16,
)
from sovyx.voice.capture._lifecycle_mixin import LifecycleMixin
from sovyx.voice.capture._loop_mixin import LoopMixin
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
from sovyx.voice.capture._restart_mixin import RestartMixin
from sovyx.voice.capture._ring import RingMixin

__all__ = [
    "_LINUX_ALSA_HOST_API",
    "_LINUX_SESSION_MANAGER_HOST_APIS",
    "AlsaHwDirectRestartResult",
    "AlsaHwDirectRestartVerdict",
    "CaptureDeviceContendedError",
    "CaptureError",
    "CaptureInoperativeError",
    "CaptureSilenceError",
    "EpochMixin",
    "ExclusiveRestartResult",
    "ExclusiveRestartVerdict",
    "LifecycleMixin",
    "LoopMixin",
    "RestartMixin",
    "RingMixin",
    "SessionManagerRestartResult",
    "SessionManagerRestartVerdict",
    "SharedRestartResult",
    "SharedRestartVerdict",
    "_CAPTURE_UNDERRUN_MIN_CALLBACKS",
    "_CAPTURE_UNDERRUN_WARN_FRACTION",
    "_CAPTURE_UNDERRUN_WARN_INTERVAL_S",
    "_CAPTURE_UNDERRUN_WINDOW_S",
    "_FRAME_SAMPLES",
    "_HEARTBEAT_INTERVAL_S",
    "_PEAK_DB_RE",
    "_QUEUE_MAXSIZE",
    "_RECONNECT_DELAY_S",
    "_RING_EPOCH_SHIFT",
    "_RING_SAMPLES_MASK",
    "_RMS_FLOOR_DB",
    "_SAMPLE_RATE",
    "_SESSION_MANAGER_CONTENTION_ERROR_CODES",
    "_VALIDATION_MIN_RMS_DB",
    "_VALIDATION_S",
    "_emit_alsa_hw_direct_restart_metric",
    "_emit_exclusive_restart_metric",
    "_emit_session_manager_restart_metric",
    "_emit_shared_restart_metric",
    "_extract_peak_db",
    "_is_session_manager_contention_pattern",
    "_resolve_input_entry",
    "_rms_db_int16",
    "_suggest_session_manager_alternatives",
]
