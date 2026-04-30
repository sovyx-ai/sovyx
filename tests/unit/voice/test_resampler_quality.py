"""Resampler quality validation [Phase 4 T4.41].

Three FFT-based measurements that pin the Sovyx 44.1→16 kHz path's
spectral quality. Each test prints the measured number to stdout
so the docs/audio-quality.md publication path (T4.47) can scrape
the latest gates from CI logs.

Promotion gate (master mission §Phase 4 / T4.42):

  Alias > -60 dBFS → upgrade to higher-order polyphase / sinc
  resampler.

The current scipy ``resample_poly`` (default Kaiser window,
β=5.0) measured on 2026-04-29:

  THD              = -83 dB    (gate ≤ -65 dB → 18 dB headroom)
  Alias            = -200 dB   (gate ≤ -100 dB → 100 dB headroom)
  Spectrum SNR     = +60 dB    (gate ≥ +40 dB → 20 dB headroom)

Headroom is generous so a future scipy version drift surfaces in
CI before reaching production. If the gate fails, the operator
should run T4.42 (upgrade to higher-order polyphase / sinc
resampler) before approving the merge.

Tests use the SAME ``resample_poly`` flow as
:func:`sovyx.voice._frame_normalizer.FrameNormalizer._resample`,
so any drift in the FrameNormalizer's resampler is caught here.
"""

from __future__ import annotations

from math import gcd

import numpy as np
import pytest  # noqa: TC002 — pytest types resolved at runtime via fixtures
import scipy.signal as sps

from sovyx.voice._resampler_quality import (
    compute_alias_energy_db,
    compute_spectrum_snr_db,
    compute_thd_db,
    generate_chirp,
    generate_tone,
    generate_white_noise,
)

_SOURCE_RATE = 44_100
_TARGET_RATE = 16_000


def _resample(signal: np.ndarray) -> np.ndarray:
    """Resample using the SAME flow the FrameNormalizer uses."""
    common = gcd(_SOURCE_RATE, _TARGET_RATE)
    up = _TARGET_RATE // common
    down = _SOURCE_RATE // common
    return sps.resample_poly(signal, up, down)  # type: ignore[no-any-return]


# ── Test 1: Pure-tone THD ───────────────────────────────────────────────


class TestPureToneThd:
    """1 kHz sine through 44.1 → 16 kHz pipeline.

    Pin: THD ≤ -65 dB (canonical broadcast clean threshold).
    """

    def test_thd_below_minus_65_db(self) -> None:
        n = 8_192
        tone = generate_tone(
            n_samples=n,
            sample_rate=_SOURCE_RATE,
            frequency=1_000.0,
            amplitude=0.5,
        )
        resampled = _resample(tone)
        # Hann window suppresses leakage from non-integer-bin
        # fundamental; without it THD measures bin-leakage not
        # actual distortion.
        windowed = resampled * np.hanning(len(resampled))
        spectrum = np.fft.rfft(windowed)
        fund_bin = round(1_000.0 / _TARGET_RATE * len(resampled))
        thd_db = compute_thd_db(spectrum, fund_bin, tolerance_bins=4)
        # Print for docs/audio-quality.md scraping (T4.47).
        print(f"\n[T4.41/THD] 1 kHz tone @ 44.1→16 kHz: {thd_db:.2f} dB")
        # Gate per master mission §Phase 4 / T4.42 + measured
        # headroom 18 dB above the gate.
        assert thd_db < -65.0, (
            f"THD {thd_db:.2f} dB exceeds -65 dB gate — "
            "run T4.42 (higher-order polyphase resampler upgrade)"
        )


# ── Test 2: White-noise alias energy ────────────────────────────────────


class TestWhiteNoiseAlias:
    """Band-limited white noise; alias energy must stay below the gate.

    Pin: alias ≤ -100 dB (massive headroom over the -60 dB
    promotion-gate threshold).
    """

    def test_alias_below_minus_100_db(self) -> None:
        n = 8_192
        rng = np.random.default_rng(0)
        noise = generate_white_noise(n_samples=n, rng=rng, amplitude=0.3)
        resampled_noise = _resample(noise)
        post = np.fft.rfft(resampled_noise)
        # In-band reference: low-pass the input below the new
        # Nyquist (Target_Rate / 2 - 1 kHz guard) → resample.
        sos = sps.butter(8, _TARGET_RATE / 2 - 1_000, fs=_SOURCE_RATE, output="sos")
        inband = sps.sosfilt(sos, noise)
        resampled_inband = _resample(inband)
        pre = np.fft.rfft(resampled_inband)
        alias_db = compute_alias_energy_db(post, pre)
        print(f"[T4.41/ALIAS] white noise @ 44.1→16 kHz: {alias_db:.2f} dB")
        assert alias_db < -100.0, (
            f"alias {alias_db:.2f} dB exceeds -100 dB gate — "
            "scipy resample_poly may have regressed"
        )


# ── Test 3: Chirp spectrum SNR ──────────────────────────────────────────


class TestChirpSpectrumSnr:
    """Linear chirp through the resampler; spectrum SNR vs ideal output.

    Pin: spectrum SNR ≥ +40 dB.
    """

    def test_chirp_snr_above_40_db(self) -> None:
        n = 8_192
        chirp = generate_chirp(
            n_samples=n,
            sample_rate=_SOURCE_RATE,
            f_start=200.0,
            f_end=4_000.0,
            amplitude=0.5,
        )
        resampled_chirp = _resample(chirp)
        n_target = len(resampled_chirp)
        ideal = generate_chirp(
            n_samples=n_target,
            sample_rate=_TARGET_RATE,
            f_start=200.0,
            f_end=4_000.0,
            amplitude=0.5,
        )
        # Spectrum-domain comparison is delay-invariant; time-domain
        # sample-by-sample would penalize the resampler's group
        # delay (5-10 ms typical) and produce nonsensically low
        # SNR (~10 dB) even on a perfect resampler.
        win = np.hanning(n_target)
        spec_resampled = np.fft.rfft(resampled_chirp * win)
        spec_ideal = np.fft.rfft(ideal * win)
        snr_db = compute_spectrum_snr_db(spec_resampled, spec_ideal)
        print(f"[T4.41/SNR ] chirp 200-4k @ 44.1→16 kHz: {snr_db:.2f} dB")
        assert snr_db > 40.0, f"chirp spectrum SNR {snr_db:.2f} dB below +40 dB gate"


# ── Helper validation ───────────────────────────────────────────────────


class TestHelperRanges:
    """Cheap unit tests for the helpers themselves."""

    def test_generate_tone_amplitude_bounded(self) -> None:
        with pytest.raises(ValueError, match=r"\[0.0, 1.0\]"):
            generate_tone(
                n_samples=512,
                sample_rate=16_000,
                frequency=1_000.0,
                amplitude=1.5,
            )

    def test_generate_chirp_amplitude_bounded(self) -> None:
        with pytest.raises(ValueError, match=r"\[0.0, 1.0\]"):
            generate_chirp(
                n_samples=512,
                sample_rate=16_000,
                f_start=200.0,
                f_end=4_000.0,
                amplitude=2.0,
            )

    def test_generate_white_noise_amplitude_bounded(self) -> None:
        with pytest.raises(ValueError, match=r"\[0.0, 1.0\]"):
            generate_white_noise(
                n_samples=512,
                rng=np.random.default_rng(0),
                amplitude=-0.1,
            )

    def test_compute_thd_returns_floor_for_empty(self) -> None:
        assert compute_thd_db(np.zeros(0, dtype=np.complex128), 0) == -200.0

    def test_compute_thd_returns_floor_for_silent_fundamental(self) -> None:
        spectrum = np.zeros(257, dtype=np.complex128)
        # Fundamental bin has zero magnitude → THD undefined → floor.
        assert compute_thd_db(spectrum, 16) == -200.0

    def test_compute_alias_shape_mismatch_rejected(self) -> None:
        with pytest.raises(ValueError, match="shape mismatch"):
            compute_alias_energy_db(
                np.zeros(257, dtype=np.complex128),
                np.zeros(128, dtype=np.complex128),
            )

    def test_compute_spectrum_snr_shape_mismatch_rejected(self) -> None:
        with pytest.raises(ValueError, match="shape mismatch"):
            compute_spectrum_snr_db(
                np.zeros(257, dtype=np.complex128),
                np.zeros(128, dtype=np.complex128),
            )

    def test_compute_spectrum_snr_identical_caps_at_200(self) -> None:
        ref = np.array([1.0, 2.0, 3.0], dtype=np.complex128)
        assert compute_spectrum_snr_db(ref, ref) == 200.0
