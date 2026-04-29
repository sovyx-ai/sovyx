"""TPDF dither for int16 quantization (Phase 4 / T4.43).

Quantizing a high-precision PCM signal (float64 / int24) down to
int16 by plain truncation injects non-linear distortion: the
quantization error is correlated with the signal, producing
spurious harmonics at exact-integer frequencies.

Mitigation: dither — adding a small random signal BEFORE
quantization to decorrelate the error from the input.

Algorithm: TPDF (Triangular Probability Density Function)

    dither[n] = U1[n] - U2[n]

Where ``U1`` and ``U2`` are independent uniform random variables in
``[0, 1)``. Their difference has a triangular PDF on ``[-1, 1]``,
peaked at zero. Scaled to one int16 LSB (= 1.0 in integer domain;
or 1/32768 in normalised float domain) the dither is at the
correct intensity for the quantization step it precedes.

TPDF is the textbook recommendation (Lipshitz, Wannamaker, Vanderkooy
1992 — "Quantization and Dither: A Theoretical Survey"):

* RPDF (rectangular, single uniform sample) eliminates the mean
  error but leaves a signal-correlated *variance*.
* TPDF (triangular, sum of two uniforms minus the bias) eliminates
  both the mean AND the variance modulation.
* Higher-order PDFs (Gaussian, etc.) achieve marginally better
  decorrelation at higher SNR cost — TPDF is the sweet spot for
  16-bit destination quantizers.

Quality envelope: TPDF at LSB-1 level (peak-to-peak 2 LSB)
adds about +4.77 dB of broadband noise to the noise floor (the
canonical "dither penalty"). The benefit is removal of the
quantization harmonics, which is audible on quiet sustained
tones (organ, sustained sax, room tone) and inaudible on dense
program material. For voice — the Sovyx use case — TPDF is
strictly an upgrade.

Foundation phase scope (T4.43, this commit):

* :func:`apply_tpdf_dither_int16` — pure-NumPy in-place dither
  + saturate from float64 to int16.
* :func:`tpdf_noise` — generator helper used by tests + the
  internal dither path.
* Configuration via ``VoiceTuningConfig.voice_dither_enabled`` +
  ``voice_dither_seed`` (deterministic for tests; system entropy
  in production).

Out of scope (later commits):

* T4.43.b — wire-up into :class:`FrameNormalizer._float_to_int16_saturate`.
* T4.44 — Wiener entropy pre-check (skip resample on already-
  destroyed signal).
* T4.45 — post-resample peak-clip assertion.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    import numpy.typing as npt

logger = get_logger(__name__)


_INT16_MIN = -(1 << 15)
"""int16 minimum (-32 768)."""

_INT16_MAX = (1 << 15) - 1
"""int16 maximum (+32 767)."""


def tpdf_noise(
    n_samples: int,
    *,
    rng: np.random.Generator,
    amplitude_lsb: float = 1.0,
) -> npt.NDArray[np.float64]:
    """Generate ``n_samples`` of TPDF dither at the requested LSB amplitude.

    The dither is the difference of two independent ``U[0, 1)``
    samples, scaled by ``amplitude_lsb``. Default ``amplitude_lsb=1.0``
    produces the textbook "1 LSB peak-to-peak" TPDF dither;
    operators may tune lower (less dither penalty, less effective
    decorrelation) or higher (more decorrelation but visible noise
    floor lift).

    Args:
        n_samples: Number of dither samples to generate.
        rng: NumPy random generator. Tests pass a seeded generator
            for reproducibility; production code uses
            ``np.random.default_rng()`` (system entropy).
        amplitude_lsb: Peak-to-peak dither amplitude in int16
            LSBs. Default 1.0 = textbook TPDF intensity. Bounded
            ``[0.0, 4.0]`` to prevent operator misconfiguration —
            > 4 LSB would be audible noise on the quiet path.

    Returns:
        ``float64`` array of length ``n_samples`` with values in
        ``(-amplitude_lsb, +amplitude_lsb)``.
    """
    if n_samples < 0:
        msg = f"n_samples must be >= 0, got {n_samples}"
        raise ValueError(msg)
    if not (0.0 <= amplitude_lsb <= 4.0):  # noqa: PLR2004 — bound documented
        msg = f"amplitude_lsb must be in [0.0, 4.0], got {amplitude_lsb}"
        raise ValueError(msg)
    if n_samples == 0:
        return np.zeros(0, dtype=np.float64)
    u1 = rng.random(n_samples, dtype=np.float64)
    u2 = rng.random(n_samples, dtype=np.float64)
    return ((u1 - u2) * amplitude_lsb).astype(np.float64)


def apply_tpdf_dither_int16(
    samples_f64: np.ndarray,
    *,
    rng: np.random.Generator,
    amplitude_lsb: float = 1.0,
) -> npt.NDArray[np.int16]:
    """Apply TPDF dither + saturate ``samples_f64`` to ``int16``.

    Pipeline:

    1. Generate TPDF noise of length ``samples_f64.size``.
    2. Add the noise to the input (still in float64).
    3. Round to nearest integer.
    4. Clip to ``[-32 768, +32 767]``.
    5. Cast to int16.

    The function expects ``samples_f64`` to already be at int16
    full-scale magnitude (i.e. range ``[-32 768, +32 768]`` —
    NOT the normalised ``[-1, 1]`` float convention used by
    PortAudio). The FrameNormalizer's ``_float_to_int16_saturate``
    multiplies the normalised signal by full-scale BEFORE clipping;
    this function plugs into the same point in the pipeline.

    Args:
        samples_f64: Input PCM as float64, scaled to int16 magnitude.
        rng: Random generator for the dither (see :func:`tpdf_noise`).
        amplitude_lsb: Peak-to-peak dither in int16 LSBs (default
            1.0 = textbook TPDF).

    Returns:
        Dithered + saturated int16 array, same shape as input.

    Raises:
        ValueError: ``samples_f64`` is not float64.
    """
    if samples_f64.dtype != np.float64:
        msg = f"samples_f64 dtype must be float64, got {samples_f64.dtype}"
        raise ValueError(msg)
    noise = tpdf_noise(samples_f64.size, rng=rng, amplitude_lsb=amplitude_lsb)
    dithered = samples_f64 + noise.reshape(samples_f64.shape)
    rounded = np.round(dithered)
    clipped = np.clip(rounded, _INT16_MIN, _INT16_MAX)
    return clipped.astype(np.int16)


__all__ = [
    "apply_tpdf_dither_int16",
    "tpdf_noise",
]
