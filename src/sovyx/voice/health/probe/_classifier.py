"""Pure signal-processing helpers + threshold constants used by the
cold and warm probe paths.

This module is dependency-light by design — only :mod:`numpy` (lazily)
and the engine config. Cold + warm probe modules import their
thresholds + helpers from here so the diagnosis tables stay coherent.

ADR §4.3 thresholds — every knob is overridable via
``SOVYX_TUNING__VOICE__PROBE_*`` env vars (CLAUDE.md anti-pattern #17).
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

from sovyx.engine.config import VoiceTuningConfig as _VoiceTuning

if TYPE_CHECKING:
    import numpy.typing as npt

    from sovyx.voice.health.contract import Combo


# ── Probe tuning thresholds ────────────────────────────────────────


_WARMUP_DISCARD_MS = _VoiceTuning().probe_warmup_discard_ms
"""Audio discarded at the start of every probe (VAD warmup + driver settle)."""

_RMS_DB_NO_SIGNAL_CEILING = _VoiceTuning().probe_rms_db_no_signal
"""Below this dBFS, warm-probe diagnosis is :attr:`Diagnosis.NO_SIGNAL`.

Voice Windows Paranoid Mission Furo W-1 — also gates the cold-probe
strict-rejection path in :func:`_diagnose_cold`."""

_RMS_DB_LOW_SIGNAL_CEILING = _VoiceTuning().probe_rms_db_low_signal
"""Between no_signal and low-signal, diagnosis is :attr:`Diagnosis.LOW_SIGNAL`."""

_VAD_APO_DEGRADED_CEILING = _VoiceTuning().probe_vad_apo_degraded_ceiling
"""Max VAD probability below which a healthy-RMS signal is diagnosed as APO-corrupted."""

_VAD_HEALTHY_FLOOR = _VoiceTuning().probe_vad_healthy_floor
"""Max VAD probability above which the warm probe is :attr:`Diagnosis.HEALTHY`."""

_TARGET_PIPELINE_RATE = 16_000
_TARGET_PIPELINE_WINDOW = 512


# ── Pure signal-processing helpers ────────────────────────────────


def _linear_to_db(linear: float) -> float:
    """Convert a linear amplitude to dBFS. Returns ``-inf`` for zero."""
    if linear <= 0.0:
        return float("-inf")
    return 20.0 * math.log10(linear)


def _compute_rms_db(block: npt.NDArray[Any], scale: float) -> float:
    """RMS of ``block`` expressed in dBFS.

    ``scale`` normalises the input to the ``[-1, 1]`` range the dBFS
    convention expects (``2**15`` for int16, ``2**23`` for int24,
    ``1.0`` for float32).

    Phase 6 / T6.34 chaos guard: when ``block`` contains NaN or Inf
    (production: a buggy upstream layer leaks float garbage; chaos
    test: ``np.array([np.nan, ...])``), ``np.mean`` produces a
    non-finite ``mean_sq``. The pre-T6.34 implementation propagated
    NaN through ``math.sqrt`` and ``math.log10``, returning NaN /
    +Inf — which silently broke downstream diagnosis logic
    (``rms < _RMS_DB_NO_SIGNAL_CEILING`` is ``False`` for NaN, so
    a NaN-RMS frame was misclassified as HEALTHY). The
    ``math.isfinite(mean_sq)`` guard collapses both cases to the
    canonical ``-inf`` no-signal sentinel — same convention the
    capture-integrity ``_compute_rms_db`` already enforces.
    """
    import numpy as np

    if block.size == 0:
        return float("-inf")
    arr = block.astype(np.float64) / scale
    mean_sq = float(np.mean(arr * arr))
    if mean_sq <= 0.0 or not math.isfinite(mean_sq):
        return float("-inf")
    rms_linear = math.sqrt(mean_sq)
    return _linear_to_db(rms_linear)


def _format_scale(sample_format: str) -> float:
    """Return the divisor that puts one sample in ``[-1, 1]``."""
    if sample_format == "int16":
        return float(1 << 15)
    if sample_format == "int24":
        return float(1 << 23)
    if sample_format == "float32":
        return 1.0
    msg = f"unexpected sample_format={sample_format!r}"  # pragma: no cover
    raise ValueError(msg)


def _warmup_samples(combo: Combo) -> int:
    """Count of source-rate samples to discard at probe start."""
    return int(combo.sample_rate * _WARMUP_DISCARD_MS / 1000.0)


__all__ = [
    "_RMS_DB_LOW_SIGNAL_CEILING",
    "_RMS_DB_NO_SIGNAL_CEILING",
    "_TARGET_PIPELINE_RATE",
    "_TARGET_PIPELINE_WINDOW",
    "_VAD_APO_DEGRADED_CEILING",
    "_VAD_HEALTHY_FLOOR",
    "_WARMUP_DISCARD_MS",
    "_compute_rms_db",
    "_format_scale",
    "_linear_to_db",
    "_warmup_samples",
]
