"""Pure helper functions used by the capture task — RMS, dBFS parsing,
device-entry resolution.

Extracted from ``voice/_capture_task.py`` (lines 251-265 +
2241-2313 pre-split) per master mission Phase 1 / T1.4 step 4.
Pure functions + a regex constant. No class state coupling, no I/O
on the hot path. Numpy is lazy-imported inside ``_rms_db_int16`` so
the test suite can stub the helpers without forcing numpy at module
import.

Public surface:

  * ``_RMS_FLOOR_DB`` — silence-floor constant. Returned by
    :func:`_rms_db_int16` for empty / all-zero buffers and by
    :func:`_extract_peak_db` for malformed silence-attempt details.
  * ``_rms_db_int16`` — dBFS RMS of an int16 buffer.
  * ``_PEAK_DB_RE`` — regex matching the opener's silence-attempt
    detail format ``"peak -XX.X dBFS"``.
  * ``_extract_peak_db`` — parse the peak-db value from a detail
    string; returns ``_RMS_FLOOR_DB`` on no-match.
  * ``_resolve_input_entry`` — selector → :class:`DeviceEntry`
    resolver (int / str / None, with optional host_api refinement).

Legacy import surface preserved: ``voice/_capture_task.py``
re-exports every name in ``__all__`` so existing imports
(``from sovyx.voice._capture_task import _resolve_input_entry``
in tests and downstream code) keep working.
"""

from __future__ import annotations

import math
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from sovyx.voice.device_enum import DeviceEntry


__all__ = [
    "_PEAK_DB_RE",
    "_RMS_FLOOR_DB",
    "_extract_peak_db",
    "_resolve_input_entry",
    "_rms_db_int16",
]


_RMS_FLOOR_DB = -120.0
"""Silence-floor dBFS used as the safe ``no signal`` sentinel.

Returned by :func:`_rms_db_int16` for empty / all-zero / numerically
degenerate buffers, and by :func:`_extract_peak_db` when the
opener's silence-attempt detail is missing the expected
``peak -XX.X dBFS`` token. Callers that need to differentiate
"genuinely -120 dB" from "no measurement" should track that
distinction at the call site — both functions intentionally return
the floor rather than ``None`` so callers can aggregate via
``min(...)`` / ``max(...)`` without nullable-handling boilerplate.
"""


def _rms_db_int16(frame: Any) -> float:  # noqa: ANN401 — numpy int16 array; Any keeps numpy lazy-imported
    """Compute dBFS RMS of an int16 buffer — safe for silent / empty buffers.

    Returns :data:`_RMS_FLOOR_DB` for empty or all-zero frames to keep
    the output finite.
    """
    import numpy as np

    if frame is None or len(frame) == 0:
        return _RMS_FLOOR_DB
    # int16 max magnitude = 32767 — normalise to [-1, 1] to get dBFS.
    sample_sq = np.mean(np.square(frame.astype(np.float32) / 32768.0))
    if sample_sq <= 0 or not math.isfinite(float(sample_sq)):
        return _RMS_FLOOR_DB
    return float(10.0 * math.log10(float(sample_sq)))


_PEAK_DB_RE = re.compile(r"peak\s+(-?\d+(?:\.\d+)?)\s*dBFS", re.IGNORECASE)


def _extract_peak_db(detail: str | None) -> float:
    """Parse ``peak -XX.X dBFS`` out of an opener silence-attempt detail.

    The opener formats silence attempts as
    ``"silent stream (peak -96.0 dBFS < threshold -80.0 dBFS)"``.
    Returns :data:`_RMS_FLOOR_DB` when the pattern is absent so callers
    can still aggregate a worst-case peak across attempts.
    """
    if not detail:
        return _RMS_FLOOR_DB
    match = _PEAK_DB_RE.search(detail)
    if match is None:
        return _RMS_FLOOR_DB
    try:
        return float(match.group(1))
    except ValueError:
        return _RMS_FLOOR_DB


def _resolve_input_entry(
    *,
    input_device: int | str | None,
    enumerate_fn: Callable[[], list[DeviceEntry]] | None,
    host_api_name: str | None,
) -> DeviceEntry:
    """Resolve a capture-task input selector to a live :class:`DeviceEntry`.

    Matching order:

    1. Exact PortAudio index (``int``) when provided.
    2. Canonical device name (``str``) optionally refined by
       ``host_api_name`` — lets the wizard persist a stable identifier
       across reboots where indices shuffle.
    3. First OS-default input, or the first available input entry.

    Raises :class:`RuntimeError` when the host exposes no input devices
    at all so :meth:`AudioCaptureTask.start` can fail loudly instead of
    silently opening the OS default.
    """
    if enumerate_fn is not None:
        entries = enumerate_fn()
    else:
        from sovyx.voice.device_enum import enumerate_devices

        entries = enumerate_devices()

    candidates = [e for e in entries if e.max_input_channels > 0]
    if not candidates:
        msg = "No audio input devices available"
        raise RuntimeError(msg)

    if isinstance(input_device, int):
        for entry in candidates:
            if entry.index == input_device:
                return entry

    if isinstance(input_device, str) and input_device:
        from sovyx.voice.device_enum import _canonicalise

        canonical = _canonicalise(input_device)
        matches = [e for e in candidates if e.canonical_name == canonical]
        if host_api_name:
            for entry in matches:
                if entry.host_api_name == host_api_name:
                    return entry
        if matches:
            return matches[0]

    defaults = [e for e in candidates if e.is_os_default]
    return defaults[0] if defaults else candidates[0]
