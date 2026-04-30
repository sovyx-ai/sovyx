"""Resampler quality measurement helpers (Phase 4 / T4.41).

Diagnostic helpers that measure the polyphase resampler's spectral
quality via FFT analysis. Used by both the CI gate at
``tests/unit/voice/test_resampler_quality.py`` and the
``docs/audio-quality.md`` publication path (T4.47).

Three canonical tests per master mission §Phase 4 / T4.41:

* **Pure-tone THD** — 1 kHz sine through 44.1 → 16 kHz pipeline.
  THD = total power outside the fundamental ± neighboring bins,
  expressed in dB below the fundamental.
* **White-noise alias energy** — band-limited white noise through
  the resampler. Alias energy = power above Nyquist that "wrapped"
  back into the audible range, expressed in dB below the in-band
  signal.
* **Chirp SNR** — linear frequency sweep through the resampler.
  SNR compares the cleaned chirp's power to the residual after
  subtracting the ideal (analytic) chirp.

Promotion gate (master mission §Phase 4 / T4.42):

  Alias > -60 dBFS → upgrade to higher-order polyphase or sinc
  resampler.

The default scipy.signal.resample_poly with default Kaiser
window typically meets -60 dBFS for the Sovyx 44.1→16 kHz path;
the test gate pins this so a future scipy version drift surfaces
in CI before reaching production.

This module is pure NumPy + scipy — no new dependencies. The
helpers operate on float64 frequency-domain magnitude arrays so
numerical precision matches the FFT output.
"""

from __future__ import annotations

import numpy as np

from sovyx.observability.logging import get_logger

logger = get_logger(__name__)


_DBFS_FLOOR = -200.0
"""Lowest dB value returned — guards against ``log10(0)``."""


def _safe_db(magnitude: float) -> float:
    """Return ``20·log10(magnitude)`` with a sub-IEEE-754 floor."""
    if magnitude <= 0.0:
        return _DBFS_FLOOR
    return 20.0 * float(np.log10(magnitude))


def compute_thd_db(
    spectrum: np.ndarray,
    fundamental_bin: int,
    *,
    tolerance_bins: int = 2,
) -> float:
    """Return the THD of a near-sinusoidal spectrum in dB.

    THD = ``20 · log10(rms(non-fundamental) / rms(fundamental))``

    More-negative values mean cleaner output. -60 dB THD means
    the harmonic / noise residual is one-thousandth the
    fundamental's amplitude.

    Caller MUST apply a window function (Hann, Kaiser, etc.)
    before computing the FFT or the spectral leakage from a
    non-integer-bin fundamental will dominate the residual and
    produce optimistic-but-meaningless numbers (~ -45 dB instead
    of the true ~ -80 dB on a Sovyx 44.1→16 kHz path). The Hann
    window's leakage is what ``tolerance_bins=4`` was calibrated
    against.

    Args:
        spectrum: Complex FFT spectrum from ``np.fft.rfft`` of a
            *windowed* time-domain signal.
        fundamental_bin: Bin index of the test tone's
            fundamental frequency (round(f / fs · N)).
        tolerance_bins: Bins around the fundamental to exclude
            from the "residual" (covers spectral leakage from
            window choice). Default 2 covers rectangular-window
            leakage at ±1 bin plus a safety bin; bump to 4 when
            using Hann.

    Returns:
        THD in dB. Lower (more negative) = cleaner.
    """
    if spectrum.size == 0 or not (0 <= fundamental_bin < spectrum.size):
        return _DBFS_FLOOR

    magnitude = np.abs(spectrum)
    fundamental_mag = float(magnitude[fundamental_bin])
    if fundamental_mag <= 0.0:
        return _DBFS_FLOOR

    # Mask out the fundamental ± tolerance.
    residual_mask = np.ones(magnitude.shape, dtype=bool)
    lo = max(0, fundamental_bin - tolerance_bins)
    hi = min(magnitude.size, fundamental_bin + tolerance_bins + 1)
    residual_mask[lo:hi] = False

    residual_rms = float(np.sqrt(np.mean(np.square(magnitude[residual_mask]))))
    return _safe_db(residual_rms / fundamental_mag)


def compute_alias_energy_db(
    spectrum_post: np.ndarray,
    spectrum_pre_inband: np.ndarray,
) -> float:
    """Return alias energy below the in-band signal level in dB.

    The classic resampler-quality test: drive band-limited white
    noise into the resampler. Energy in bands that the source
    didn't have (alias regions) is the resampler's fault.

    Args:
        spectrum_post: Complex FFT of the resampled output.
        spectrum_pre_inband: Complex FFT of the in-band portion
            of the source signal (must be the same length as
            ``spectrum_post``).

    Returns:
        Alias energy expressed in dB below the in-band reference.
        Lower (more negative) = less aliasing. The promotion
        gate is -60 dBFS per master mission §T4.42.
    """
    if spectrum_post.shape != spectrum_pre_inband.shape:
        msg = f"shape mismatch: post={spectrum_post.shape} vs pre={spectrum_pre_inband.shape}"
        raise ValueError(msg)

    inband_rms = float(np.sqrt(np.mean(np.square(np.abs(spectrum_pre_inband)))))
    if inband_rms <= 0.0:
        return _DBFS_FLOOR

    # Alias energy = power in the post spectrum at bins where the
    # source had near-zero energy. We approximate "source bins"
    # as those whose magnitude is > 1% of the in-band RMS, and
    # measure the residual elsewhere.
    source_threshold = 0.01 * inband_rms
    alias_mask = np.abs(spectrum_pre_inband) <= source_threshold
    if not np.any(alias_mask):
        return _DBFS_FLOOR
    alias_rms = float(np.sqrt(np.mean(np.square(np.abs(spectrum_post[alias_mask])))))
    return _safe_db(alias_rms / inband_rms)


def compute_spectrum_snr_db(
    measured: np.ndarray,
    reference: np.ndarray,
) -> float:
    """Return SNR comparing magnitude spectra (delay-invariant).

    Resamplers introduce group delay; a sample-by-sample
    comparison would penalize the resampler for shifting the
    signal in time even when the spectrum is bit-perfect. The
    spectrum-based SNR is delay-invariant — it measures whether
    the energy is distributed across frequency bins the same way
    as the reference, without caring about phase.

    Args:
        measured: Magnitude or complex spectrum of the resampler
            output. Use ``np.abs(np.fft.rfft(windowed_output))``.
        reference: Magnitude or complex spectrum of the ideal
            output. Same shape as ``measured``.

    Returns:
        SNR in dB. Higher = cleaner. Caps at +200 dB when
        spectra are identical.
    """
    if measured.shape != reference.shape:
        msg = f"shape mismatch: measured={measured.shape} vs reference={reference.shape}"
        raise ValueError(msg)
    mag_measured = np.abs(measured.astype(np.complex128))
    mag_reference = np.abs(reference.astype(np.complex128))
    diff = mag_measured - mag_reference
    ref_rms = float(np.sqrt(np.mean(np.square(mag_reference))))
    diff_rms = float(np.sqrt(np.mean(np.square(diff))))
    if diff_rms <= 0.0:
        return -_DBFS_FLOOR  # +200 dB cap
    if ref_rms <= 0.0:
        return _DBFS_FLOOR
    return _safe_db(ref_rms / diff_rms)


def compute_snr_db(
    measured: np.ndarray,
    reference: np.ndarray,
) -> float:
    """Return SNR of ``measured`` vs ``reference`` (time-domain) in dB.

    Computes ``20·log10(rms(reference) / rms(measured - reference))``.
    The reference is the ideal output (e.g. the test signal
    resampled by an oracle); the measured signal is what the
    resampler actually produced.

    NOTE: this is sample-by-sample comparison and IS delay-
    sensitive — for resampler validation use
    :func:`compute_spectrum_snr_db` instead, which is delay-invariant.
    Time-domain SNR is appropriate for sample-aligned systems
    (e.g. AEC residual measurement).

    Args:
        measured: Resampler output. Length-aligned with reference.
        reference: Ideal output. Same shape + dtype as measured.

    Returns:
        SNR in dB. Higher = cleaner (more signal, less residual).
        ``+200 dB`` cap when measured == reference exactly.
    """
    if measured.shape != reference.shape:
        msg = f"shape mismatch: measured={measured.shape} vs reference={reference.shape}"
        raise ValueError(msg)
    measured_f64 = measured.astype(np.float64)
    reference_f64 = reference.astype(np.float64)
    residual = measured_f64 - reference_f64
    ref_rms = float(np.sqrt(np.mean(np.square(reference_f64))))
    res_rms = float(np.sqrt(np.mean(np.square(residual))))
    if res_rms <= 0.0:
        return -_DBFS_FLOOR  # +200 dB cap
    if ref_rms <= 0.0:
        return _DBFS_FLOOR
    return _safe_db(ref_rms / res_rms)


def generate_tone(
    *,
    n_samples: int,
    sample_rate: int,
    frequency: float,
    amplitude: float = 0.5,
) -> np.ndarray:
    """Generate a float32 sinusoid for the THD test.

    Amplitude bounded ``[0.0, 1.0]`` — the resampler-input
    convention is normalised audio in [-1, 1].
    """
    if not (0.0 <= amplitude <= 1.0):
        msg = f"amplitude must be in [0.0, 1.0], got {amplitude}"
        raise ValueError(msg)
    t = np.arange(n_samples) / float(sample_rate)
    return (np.sin(2 * np.pi * frequency * t) * amplitude).astype(np.float32)


def generate_chirp(
    *,
    n_samples: int,
    sample_rate: int,
    f_start: float,
    f_end: float,
    amplitude: float = 0.5,
) -> np.ndarray:
    """Generate a linear chirp from ``f_start`` to ``f_end`` Hz.

    Used by the SNR test — the chirp probes resampler behaviour
    across the full passband, which surfaces frequency-dependent
    distortion that a single tone would miss.
    """
    if not (0.0 <= amplitude <= 1.0):
        msg = f"amplitude must be in [0.0, 1.0], got {amplitude}"
        raise ValueError(msg)
    t = np.arange(n_samples) / float(sample_rate)
    duration = n_samples / float(sample_rate)
    # Linear-frequency-sweep formula: phase(t) = 2π·(f_start·t + (f_end-f_start)·t²/(2·duration))
    phase = 2 * np.pi * (f_start * t + (f_end - f_start) * t * t / (2.0 * duration))
    return (np.sin(phase) * amplitude).astype(np.float32)


def generate_white_noise(
    *,
    n_samples: int,
    rng: np.random.Generator,
    amplitude: float = 0.5,
) -> np.ndarray:
    """Generate Gaussian-white noise scaled to ``±amplitude``.

    Capped at ±1.0 (clip rather than wrap) so the resampler input
    is always within the normalised [-1, 1] convention. Used by
    the alias test — the source is approximately flat across the
    spectrum, so any non-flat region in the post-resample output
    is the resampler's contribution.
    """
    if not (0.0 <= amplitude <= 1.0):
        msg = f"amplitude must be in [0.0, 1.0], got {amplitude}"
        raise ValueError(msg)
    raw = rng.standard_normal(n_samples) * amplitude
    return np.clip(raw, -1.0, 1.0).astype(np.float32)


__all__ = [
    "compute_alias_energy_db",
    "compute_snr_db",
    "compute_spectrum_snr_db",
    "compute_thd_db",
    "generate_chirp",
    "generate_tone",
    "generate_white_noise",
]
