"""Wiener entropy (spectral flatness) detector (Phase 4 / T4.44).

Wiener entropy — also called the spectral flatness measure (SFM)
— quantifies how "noise-like" vs "tone-like" a signal's power
spectrum is. Defined as the ratio of the geometric mean to the
arithmetic mean of the per-bin power spectrum:

    Wiener_entropy = geometric_mean(|S(f)|²) / arithmetic_mean(|S(f)|²)

Range: ``[0.0, 1.0]``.

* ``≈ 0.0`` — pure tone (one bin dominates, geo-mean → 0 because
  most bins have near-zero power).
* ``≈ 1.0`` — white noise (all bins have equal magnitude, so
  geo-mean equals arith-mean by definition).
* Speech typical values — 0.1 to 0.4 (formant structure produces
  multiple peaks but most spectrum is structured).

Phase 4 / T4.44 use case: signal-destruction detector. A frame
whose Wiener entropy exceeds a threshold (default 0.5) is too
noise-like for downstream DSP (resample, AEC, STT) to extract
meaningful content from — better to skip processing entirely than
waste CPU on lost-cause data. Master mission spec:

    Add Wiener entropy pre-check before resampling: skip resample
    if entropy > 0.5 (signal already destroyed; resampling won't
    help)

Foundation phase scope (this commit, T4.44 only):

* :func:`compute_wiener_entropy` — pure-DSP per-frame measurement.
* :func:`is_signal_destroyed` — predicate using a configurable
  threshold.
* :class:`WienerEntropyConfig` — tuning snapshot.

Out of scope (later commits):

* T4.44.b — wire-up into :class:`FrameNormalizer` resample stage
  to skip resample on destroyed signals.
* T4.44.c — observability: ``voice.audio.wiener_entropy``
  histogram + ``voice.audio.signal_destroyed`` counter.

The metric is bin-by-bin (all FFT bins included with an epsilon
floor so ``log(0)`` doesn't blow up). Computing on the FULL
spectrum (vs filtering near-zero bins) is the canonical
convention — a near-empty spectrum SHOULD report low entropy
(close to a pure tone), not 1.0 (which a filtered version would
incorrectly produce).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from sovyx.observability.logging import get_logger

logger = get_logger(__name__)


_DEFAULT_DESTRUCTION_THRESHOLD = 0.5
"""Master-mission spec default — entropy > 0.5 → skip processing."""

_LOG_FLOOR = 1e-10
"""Epsilon added to power values before ``log()`` to avoid -inf.

The geometric mean of a power spectrum with one or more
exact-zero bins would otherwise blow up (``exp(-inf)`` → 0
trivially). The epsilon shifts every bin into the strictly
positive range without measurably changing the entropy of
spectra well above the noise floor.
"""


@dataclass(frozen=True, slots=True)
class WienerEntropyConfig:
    """Tuning snapshot for the entropy detector.

    Foundation phase ships the threshold knob; future commits may
    add EWMA smoothing or per-band variants.
    """

    enabled: bool
    destruction_threshold: float


def compute_wiener_entropy(frame: np.ndarray) -> float:
    """Return the Wiener entropy (spectral flatness) of ``frame``.

    Args:
        frame: int16 PCM frame. Empty + all-zero inputs return
            ``0.0`` (treats true silence as "tone-like" / not
            destroyed by convention — there's no signal to call
            destroyed).

    Returns:
        Float in ``[0.0, 1.0]``. ``0.0`` = pure tone /
        deterministic. ``1.0`` = white noise / fully random.
    """
    if frame.dtype != np.int16:
        msg = f"frame dtype must be int16, got {frame.dtype}"
        raise ValueError(msg)
    if frame.size == 0:
        return 0.0

    # Real-FFT yields N//2+1 complex bins for an N-sample real input.
    # We work in float64 throughout — int16 squares overflow if
    # naively summed at large frame sizes.
    f64 = frame.astype(np.float64)
    spectrum = np.fft.rfft(f64)
    power = np.square(np.abs(spectrum))

    # Pure-silence shortcut: every bin is exact zero → entropy is
    # undefined. Returning 0.0 keeps "silence is tone-like" semantics
    # consistent with the destruction detector's purpose: silent
    # frames are NOT destroyed signals (no signal to destroy).
    if not np.any(power):
        return 0.0

    # Add the log floor to every bin BEFORE computing both means.
    # Using the same shifted distribution for numerator + denominator
    # is what keeps the ratio in ``[0, 1]``; mixing un-shifted arith
    # mean with floored geo mean would underestimate entropy on
    # peaky spectra.
    shifted = power + _LOG_FLOOR
    log_power = np.log(shifted)
    geo_mean = float(np.exp(log_power.mean()))
    arith_mean = float(shifted.mean())

    if arith_mean <= 0.0:
        return 0.0
    ratio = geo_mean / arith_mean
    # Numerical-stability clamp — the geo/arith ratio is provably
    # in [0, 1] for non-negative inputs but float rounding on
    # near-uniform spectra can produce ``1.0 + eps``.
    return max(0.0, min(1.0, ratio))


def is_signal_destroyed(
    frame: np.ndarray,
    *,
    threshold: float = _DEFAULT_DESTRUCTION_THRESHOLD,
) -> bool:
    """Predicate: True when ``frame`` looks too noise-like to process.

    Computed as ``compute_wiener_entropy(frame) > threshold``. The
    default 0.5 follows the master-mission spec — empirically
    chosen as the boundary above which downstream DSP (resample,
    AEC, STT) extracts no useful content from the signal.

    Args:
        frame: int16 PCM frame.
        threshold: Wiener-entropy boundary. Bounded ``[0.0, 1.0]``.

    Raises:
        ValueError: ``threshold`` outside the algebraic range.
    """
    if not (0.0 <= threshold <= 1.0):
        msg = f"threshold must be in [0.0, 1.0], got {threshold!r}"
        raise ValueError(msg)
    return compute_wiener_entropy(frame) > threshold


__all__ = [
    "WienerEntropyConfig",
    "compute_wiener_entropy",
    "is_signal_destroyed",
]
