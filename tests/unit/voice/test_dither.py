"""Tests for :mod:`sovyx.voice._dither` [Phase 4 T4.43].

Coverage:

* :func:`tpdf_noise` algebraic identities (range, mean ≈ 0,
  amplitude scaling, empty/zero input).
* :func:`apply_tpdf_dither_int16`:
  - dtype/shape contract,
  - clipping to int16 rails,
  - silence preservation (zero in → zero ± 1 LSB out),
  - signal preservation (loud signal passes with sub-LSB noise),
  - decorrelation property (statistical: mean error → 0 across
    random signals).
"""

from __future__ import annotations

import numpy as np
import pytest

from sovyx.voice._dither import apply_tpdf_dither_int16, tpdf_noise

# ── tpdf_noise ───────────────────────────────────────────────────────────


class TestTpdfNoise:
    def test_default_amplitude_within_one_lsb_range(self) -> None:
        rng = np.random.default_rng(0)
        noise = tpdf_noise(10_000, rng=rng)
        # TPDF range: open (-1, +1). Floating-point random samples
        # never reach exactly ±1, but the upper bound IS 1.0 - eps.
        assert noise.max() < 1.0
        assert noise.min() > -1.0

    def test_amplitude_scaling(self) -> None:
        rng = np.random.default_rng(1)
        noise = tpdf_noise(10_000, rng=rng, amplitude_lsb=2.5)
        assert noise.max() < 2.5
        assert noise.min() > -2.5

    def test_mean_approaches_zero(self) -> None:
        rng = np.random.default_rng(2)
        noise = tpdf_noise(100_000, rng=rng)
        # 100 k samples → standard error ~ 1/√(N · 6) ≈ 0.0013.
        # Allow ±0.05 for cross-rng variance.
        assert abs(float(noise.mean())) < 0.05

    def test_pdf_is_triangular_not_rectangular(self) -> None:
        # Triangular PDF peaks at zero. We bin the histogram and
        # verify that the central bins concentrate mass much more
        # than the edges — clear signature of triangular vs uniform.
        #
        # Math: PDF(x) = 1 - |x| on [-1, 1]. Central 3 bins of 21
        # span [-0.143, +0.143], holding ∫(1-|x|)dx ≈ 0.266 of the
        # total mass (vs 3/21 ≈ 0.143 for a uniform distribution).
        # Edge bins hold ~0 (PDF goes to zero at ±1).
        rng = np.random.default_rng(3)
        noise = tpdf_noise(100_000, rng=rng)
        hist, _ = np.histogram(noise, bins=21, range=(-1.0, 1.0))
        center_mass = float(hist[9:12].sum() / hist.sum())
        edge_mass_left = float(hist[0] / hist.sum())
        edge_mass_right = float(hist[-1] / hist.sum())
        # Triangular: ~0.266; uniform would be ~0.143. 0.20 cleanly
        # separates the two distributions.
        assert center_mass > 0.20
        # Edge bins on triangular: PDF(±0.95) ≈ 0.05 → bin mass < 0.01.
        assert edge_mass_left < 0.02
        assert edge_mass_right < 0.02

    def test_zero_samples_returns_empty(self) -> None:
        rng = np.random.default_rng(4)
        out = tpdf_noise(0, rng=rng)
        assert out.shape == (0,)
        assert out.dtype == np.float64

    def test_negative_samples_rejected(self) -> None:
        rng = np.random.default_rng(5)
        with pytest.raises(ValueError, match=">=  *0"):
            tpdf_noise(-1, rng=rng)

    def test_amplitude_above_4_rejected(self) -> None:
        rng = np.random.default_rng(6)
        with pytest.raises(ValueError, match=r"\[0.0, 4.0\]"):
            tpdf_noise(100, rng=rng, amplitude_lsb=5.0)

    def test_amplitude_negative_rejected(self) -> None:
        rng = np.random.default_rng(7)
        with pytest.raises(ValueError, match=r"\[0.0, 4.0\]"):
            tpdf_noise(100, rng=rng, amplitude_lsb=-0.1)

    def test_seeded_determinism(self) -> None:
        # Same seed → same noise sequence (test reproducibility).
        a = tpdf_noise(100, rng=np.random.default_rng(42))
        b = tpdf_noise(100, rng=np.random.default_rng(42))
        np.testing.assert_array_equal(a, b)


# ── apply_tpdf_dither_int16 ──────────────────────────────────────────────


class TestApplyTpdfDitherInt16:
    def test_returns_int16_same_shape(self) -> None:
        rng = np.random.default_rng(0)
        samples = np.full(512, 5_000.0, dtype=np.float64)
        out = apply_tpdf_dither_int16(samples, rng=rng)
        assert out.dtype == np.int16
        assert out.shape == samples.shape

    def test_rejects_non_float64(self) -> None:
        rng = np.random.default_rng(0)
        samples = np.full(512, 5_000, dtype=np.float32)
        with pytest.raises(ValueError, match="float64"):
            apply_tpdf_dither_int16(samples, rng=rng)

    def test_silence_stays_within_one_lsb(self) -> None:
        # Pure silence in → output is at most ±1 sample (the
        # quantized dither). This is the expected "dither floor"
        # contribution; operators tune amplitude_lsb if they want
        # less.
        rng = np.random.default_rng(0)
        samples = np.zeros(1_000, dtype=np.float64)
        out = apply_tpdf_dither_int16(samples, rng=rng)
        assert int(np.max(np.abs(out))) <= 1

    def test_loud_signal_preserved(self) -> None:
        # Strong sine in → strong sine out. The dither is
        # negligible relative to the signal energy.
        rng = np.random.default_rng(0)
        n = 1_024
        t = np.arange(n) / 16_000.0
        signal_f64 = np.sin(2 * np.pi * 1_000 * t) * 8_000
        out = apply_tpdf_dither_int16(signal_f64, rng=rng)
        # Reconstructed RMS should be within 0.5 dB of the input.
        in_rms = float(np.sqrt(np.mean(signal_f64**2)))
        out_rms = float(np.sqrt(np.mean(out.astype(np.float64) ** 2)))
        assert abs(20 * np.log10(out_rms / in_rms)) < 0.5

    def test_clipping_to_int16_rails(self) -> None:
        # Inputs above int16 max must clip (NOT wrap to negative
        # via int overflow).
        rng = np.random.default_rng(0)
        loud = np.full(100, 50_000.0, dtype=np.float64)
        out = apply_tpdf_dither_int16(loud, rng=rng)
        assert int(out.max()) == 32_767
        assert int(out.min()) == 32_767  # all samples saturated high

    def test_negative_clipping(self) -> None:
        rng = np.random.default_rng(0)
        very_quiet = np.full(100, -50_000.0, dtype=np.float64)
        out = apply_tpdf_dither_int16(very_quiet, rng=rng)
        assert int(out.min()) == -32_768
        assert int(out.max()) == -32_768

    def test_mean_quantization_error_decorrelated(self) -> None:
        # Dither's main job: make the mean rounding error → 0
        # ACROSS a random signal, vs naive truncation which has a
        # systematic bias (always rounds toward zero or toward
        # negative-infinity depending on cast direction).
        rng = np.random.default_rng(0)
        # Generate a sub-LSB signal: amplitude 0.3 LSB, all
        # samples at 0.3 (constant). Without dither: every sample
        # quantizes to 0 → mean error = -0.3. With TPDF dither:
        # mean error → 0 statistically.
        n = 100_000
        signal = np.full(n, 0.3, dtype=np.float64)
        out = apply_tpdf_dither_int16(signal, rng=rng)
        mean_int = float(out.astype(np.float64).mean())
        # With proper TPDF, output mean tracks the input mean.
        # Without dither it would be exactly 0.0 (round half to
        # even: 0.3 → 0). Allow ±0.05 for sample variance.
        assert 0.25 < mean_int < 0.35

    def test_amplitude_scaling_passes_through(self) -> None:
        rng = np.random.default_rng(0)
        # Dithered output amplitude tracks the dither setting.
        # Compare amplitude_lsb=0.0 (no dither, exact rounding)
        # vs amplitude_lsb=2.0 (more noise on silence input).
        zeros = np.zeros(10_000, dtype=np.float64)
        no_dither = apply_tpdf_dither_int16(
            zeros,
            rng=np.random.default_rng(0),
            amplitude_lsb=0.0,
        )
        much_dither = apply_tpdf_dither_int16(
            zeros,
            rng=rng,
            amplitude_lsb=2.0,
        )
        # No-dither: every sample is exactly 0.
        assert int(np.max(np.abs(no_dither))) == 0
        # Heavy dither: peak ≈ 2 LSB ± rounding.
        assert int(np.max(np.abs(much_dither))) >= 2

    def test_zero_amplitude_yields_pure_quantization(self) -> None:
        # amplitude_lsb=0 → no dither at all → behaves like
        # plain np.round + clip + cast.
        rng = np.random.default_rng(0)
        samples = np.array([100.4, 100.6, -100.4, -100.6], dtype=np.float64)
        out = apply_tpdf_dither_int16(samples, rng=rng, amplitude_lsb=0.0)
        # numpy rounds half-to-even: 100.4 → 100, 100.6 → 101,
        # -100.4 → -100, -100.6 → -101.
        np.testing.assert_array_equal(out, np.array([100, 101, -100, -101], dtype=np.int16))
