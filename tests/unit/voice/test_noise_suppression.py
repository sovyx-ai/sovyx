"""Tests for :mod:`sovyx.voice._noise_suppression` [Phase 4 T4.11-T4.12].

Coverage:

* :class:`NoiseSuppressionConfig` linear-domain derivations from
  dB thresholds.
* :func:`build_noise_suppressor` factory matrix.
* :class:`NoOpNoiseSuppressor` pass-through identity.
* :class:`SpectralGatingSuppressor` algorithmic behaviour:
  - silent input passes (flat zero, no spurious noise injected),
  - loud broadband input passes (magnitudes well above floor),
  - low-energy non-silent input is attenuated,
  - shape / dtype contract enforcement.
* :func:`estimate_frame_dbfs` energy meter helper.
* :func:`build_frame_normalizer_noise_suppressor` pins
  the FrameNormalizer-mandated 16 kHz / 512-sample invariants.
"""

from __future__ import annotations

import dataclasses

import numpy as np
import pytest

from sovyx.voice._noise_suppression import (
    NoiseSuppressionConfig,
    NoiseSuppressor,
    NoOpNoiseSuppressor,
    SpectralGatingSuppressor,
    build_frame_normalizer_noise_suppressor,
    build_noise_suppressor,
    estimate_frame_dbfs,
)

# ── NoiseSuppressionConfig dB derivations ────────────────────────────────


class TestNoiseSuppressionConfig:
    def _cfg(self, **overrides: object) -> NoiseSuppressionConfig:
        base: dict[str, object] = {
            "enabled": True,
            "engine": "spectral_gating",
            "sample_rate": 16_000,
            "frame_size_samples": 512,
            "floor_db": -50.0,
            "attenuation_db": -20.0,
        }
        base.update(overrides)
        return NoiseSuppressionConfig(**base)  # type: ignore[arg-type]

    def test_floor_linear_at_minus_50_dbfs(self) -> None:
        # 32768 * 10^(-50/20) ≈ 32768 * 0.00316 ≈ 103.6
        cfg = self._cfg(floor_db=-50.0)
        assert 100 <= cfg.floor_linear <= 110

    def test_floor_linear_at_zero_dbfs_is_full_scale(self) -> None:
        cfg = self._cfg(floor_db=0.0)
        assert cfg.floor_linear == pytest.approx(32_768.0, abs=1.0)

    def test_attenuation_linear_at_minus_20_db(self) -> None:
        cfg = self._cfg(attenuation_db=-20.0)
        assert cfg.attenuation_linear == pytest.approx(0.1, abs=0.001)

    def test_attenuation_linear_at_zero_db(self) -> None:
        # 0 dB = passthrough (no NS effect).
        cfg = self._cfg(attenuation_db=0.0)
        assert cfg.attenuation_linear == pytest.approx(1.0, abs=1e-6)

    def test_attenuation_linear_clamped_to_one(self) -> None:
        # Positive attenuation_db (amplify) clamps to 1.0 — never
        # amplify noise. Defence-in-depth against a future config
        # validator regression.
        cfg = self._cfg(attenuation_db=6.0)  # would be ~2x
        assert cfg.attenuation_linear == 1.0


# ── NoOpNoiseSuppressor ─────────────────────────────────────────────────


class TestNoOpNoiseSuppressor:
    def test_implements_protocol(self) -> None:
        ns = NoOpNoiseSuppressor()
        assert isinstance(ns, NoiseSuppressor)

    def test_process_returns_input_unchanged(self) -> None:
        ns = NoOpNoiseSuppressor()
        frame = np.array([1, -2, 3, -4, 5], dtype=np.int16)
        out = ns.process(frame)
        np.testing.assert_array_equal(out, frame)

    def test_process_preserves_identity(self) -> None:
        ns = NoOpNoiseSuppressor()
        frame = np.zeros(512, dtype=np.int16)
        # Spec contract: identity preserved so the hot-path skips a
        # copy when NS is disabled.
        assert ns.process(frame) is frame

    def test_reset_does_not_raise(self) -> None:
        NoOpNoiseSuppressor().reset()


# ── SpectralGatingSuppressor — construction ──────────────────────────────


@pytest.fixture()
def spectral_config() -> NoiseSuppressionConfig:
    return NoiseSuppressionConfig(
        enabled=True,
        engine="spectral_gating",
        sample_rate=16_000,
        frame_size_samples=512,
        floor_db=-50.0,
        attenuation_db=-20.0,
    )


class TestSpectralGatingConstruction:
    def test_rejects_non_spectral_engine(
        self,
        spectral_config: NoiseSuppressionConfig,
    ) -> None:
        bad = dataclasses.replace(spectral_config, engine="off")
        with pytest.raises(ValueError, match="engine='spectral_gating'"):
            SpectralGatingSuppressor(bad)

    def test_implements_protocol(
        self,
        spectral_config: NoiseSuppressionConfig,
    ) -> None:
        ns = SpectralGatingSuppressor(spectral_config)
        assert isinstance(ns, NoiseSuppressor)


# ── SpectralGatingSuppressor — input validation ──────────────────────────


class TestSpectralGatingProcess:
    def test_rejects_non_int16(
        self,
        spectral_config: NoiseSuppressionConfig,
    ) -> None:
        ns = SpectralGatingSuppressor(spectral_config)
        with pytest.raises(ValueError, match="dtype"):
            ns.process(np.zeros(512, dtype=np.float32))

    def test_rejects_wrong_frame_size(
        self,
        spectral_config: NoiseSuppressionConfig,
    ) -> None:
        ns = SpectralGatingSuppressor(spectral_config)
        with pytest.raises(ValueError, match="frame size"):
            ns.process(np.zeros(256, dtype=np.int16))

    def test_returns_int16_same_length(
        self,
        spectral_config: NoiseSuppressionConfig,
    ) -> None:
        ns = SpectralGatingSuppressor(spectral_config)
        rng = np.random.default_rng(0)
        frame = rng.integers(-10_000, 10_000, 512).astype(np.int16)
        out = ns.process(frame)
        assert out.dtype == np.int16
        assert out.shape == frame.shape


# ── SpectralGatingSuppressor — algorithmic behaviour ────────────────────


class TestSpectralGatingBehaviour:
    """The gate suppresses low-energy bins, passes high-energy bins."""

    def test_silent_input_stays_silent(
        self,
        spectral_config: NoiseSuppressionConfig,
    ) -> None:
        # No spurious noise injection on zero input.
        ns = SpectralGatingSuppressor(spectral_config)
        out = ns.process(np.zeros(512, dtype=np.int16))
        # All-zero in → all-zero out (every bin's magnitude is 0,
        # well below floor; the gate scales 0 by attenuation = 0).
        assert np.max(np.abs(out)) == 0

    def test_loud_broadband_passes_with_low_distortion(
        self,
        spectral_config: NoiseSuppressionConfig,
    ) -> None:
        # Loud broadband signal: every bin's magnitude is well above
        # the floor → gain mask is all 1.0 → the output approximates
        # the input (with FFT/IFFT round-trip rounding). Validate
        # by RMS-dBFS difference.
        ns = SpectralGatingSuppressor(spectral_config)
        rng = np.random.default_rng(7)
        frame = (rng.standard_normal(512) * 8000).astype(np.int16)
        out = ns.process(frame)
        # FFT round-trip on int16 data introduces sub-LSB noise; the
        # output RMS should match input RMS within a fraction of a dB.
        in_db = estimate_frame_dbfs(frame)
        out_db = estimate_frame_dbfs(out)
        assert abs(in_db - out_db) < 0.5

    def test_below_floor_signal_is_attenuated(
        self,
        spectral_config: NoiseSuppressionConfig,
    ) -> None:
        # The gate operates per-FFT-bin, not on time-domain RMS, so
        # the test signal must spread its energy across the spectrum
        # such that EVERY bin sits below the magnitude floor. A
        # narrow-band tone concentrates energy into 1-2 bins whose
        # magnitudes are large even when the time-domain RMS is low.
        #
        # Solution: weak broadband noise. For std σ, the per-bin
        # magnitude ≈ σ·√(N/2) (white-noise FFT pdf). With σ=1,
        # N=512 → bin magnitude ≈ 16 → -66 dBFS, well below the
        # -50 dBFS floor. Every bin gets attenuated.
        ns = SpectralGatingSuppressor(spectral_config)
        rng = np.random.default_rng(11)
        weak = rng.standard_normal(512).astype(np.float64)
        weak_int16 = np.round(weak).astype(np.int16)  # amplitude ~ ±1
        out = ns.process(weak_int16)
        in_db = estimate_frame_dbfs(weak_int16)
        out_db = estimate_frame_dbfs(out)
        # The gate should attenuate by ~attenuation_db (-20 dB).
        # Allow generous slack for the FFT-bin variability of weak
        # noise (single realisation; sigma~1 gives noisy estimates).
        # Pin: at least 10 dB attenuation observed.
        assert (out_db - in_db) < -10, (
            f"in_db={in_db:.2f}, out_db={out_db:.2f}, expected ≥ 10 dB drop"
        )

    def test_reset_does_not_raise(
        self,
        spectral_config: NoiseSuppressionConfig,
    ) -> None:
        SpectralGatingSuppressor(spectral_config).reset()


# ── build_noise_suppressor factory ───────────────────────────────────────


class TestBuildNoiseSuppressor:
    def _cfg(self, **overrides: object) -> NoiseSuppressionConfig:
        base: dict[str, object] = {
            "enabled": True,
            "engine": "spectral_gating",
            "sample_rate": 16_000,
            "frame_size_samples": 512,
            "floor_db": -50.0,
            "attenuation_db": -20.0,
        }
        base.update(overrides)
        return NoiseSuppressionConfig(**base)  # type: ignore[arg-type]

    def test_disabled_returns_noop(self) -> None:
        ns = build_noise_suppressor(self._cfg(enabled=False))
        assert isinstance(ns, NoOpNoiseSuppressor)

    def test_engine_off_returns_noop(self) -> None:
        ns = build_noise_suppressor(self._cfg(engine="off"))
        assert isinstance(ns, NoOpNoiseSuppressor)

    def test_enabled_spectral_returns_concrete(self) -> None:
        ns = build_noise_suppressor(self._cfg())
        assert isinstance(ns, SpectralGatingSuppressor)

    def test_unknown_engine_raises_value_error(self) -> None:
        cfg = self._cfg()
        object.__setattr__(cfg, "engine", "bogus")  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="Unknown noise-suppression engine"):
            build_noise_suppressor(cfg)


# ── build_frame_normalizer_noise_suppressor helper ──────────────────────


class TestBuildFrameNormalizerNoiseSuppressor:
    """Pins the FrameNormalizer-mandated 16 kHz / 512-sample invariants."""

    def test_disabled_returns_noop(self) -> None:
        ns = build_frame_normalizer_noise_suppressor(
            enabled=False,
            engine="spectral_gating",
            floor_db=-50.0,
            attenuation_db=-20.0,
        )
        assert isinstance(ns, NoOpNoiseSuppressor)

    def test_engine_off_returns_noop(self) -> None:
        ns = build_frame_normalizer_noise_suppressor(
            enabled=True,
            engine="off",
            floor_db=-50.0,
            attenuation_db=-20.0,
        )
        assert isinstance(ns, NoOpNoiseSuppressor)

    def test_enabled_spectral_processes_512_samples(self) -> None:
        ns = build_frame_normalizer_noise_suppressor(
            enabled=True,
            engine="spectral_gating",
            floor_db=-50.0,
            attenuation_db=-20.0,
        )
        assert isinstance(ns, SpectralGatingSuppressor)
        out = ns.process(np.zeros(512, dtype=np.int16))
        assert out.shape == (512,)


# ── estimate_frame_dbfs helper ──────────────────────────────────────────


class TestEstimateFrameDbfs:
    def test_zero_returns_floor(self) -> None:
        assert estimate_frame_dbfs(np.zeros(512, dtype=np.int16)) == -120.0

    def test_full_scale_constant_yields_zero_dbfs(self) -> None:
        # Constant at int16 max = 0 dBFS RMS by definition.
        frame = np.full(512, 32_767, dtype=np.int16)
        assert estimate_frame_dbfs(frame) == pytest.approx(0.0, abs=0.01)

    def test_half_scale_yields_minus_six_dbfs(self) -> None:
        # 16384 / 32768 = 0.5 → 20*log10(0.5) ≈ -6.02 dB.
        frame = np.full(512, 16_384, dtype=np.int16)
        result = estimate_frame_dbfs(frame)
        assert -6.5 < result < -5.5

    def test_empty_frame_returns_floor(self) -> None:
        assert estimate_frame_dbfs(np.array([], dtype=np.int16)) == -120.0
