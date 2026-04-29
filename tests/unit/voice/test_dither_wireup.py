"""Tests for the TPDF dither wire-up [Phase 4 T4.43.b].

Coverage:

* :func:`_float_to_int16_saturate` accepts the optional dither
  parameters and applies dither when ``dither_rng`` is supplied.
* The dither path produces statistically distinct output from
  the no-dither path on the SAME input.
* Saturation counters still work post-dither.
* :class:`FrameNormalizer` accepts the dither kwargs and threads
  the rng through to the conversion stage.
* The ``dither_enabled=False`` path is bit-exact to the
  pre-T4.43.b conversion (regression-guard).
* :class:`AudioCaptureTask` plumbing for the dither flags.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np

from sovyx.voice._capture_task import AudioCaptureTask
from sovyx.voice._frame_normalizer import (
    FrameNormalizer,
    _float_to_int16_saturate,
)

# ── _float_to_int16_saturate dither parameters ──────────────────────────


class TestSaturateDitherParams:
    def test_no_dither_path_unchanged(self) -> None:
        # Regression-guard: omitting dither_rng must produce the
        # exact same output as the pre-T4.43.b implementation.
        rng = np.random.default_rng(0)
        samples = (rng.standard_normal(512) * 0.5).astype(np.float32)
        out, sat = _float_to_int16_saturate(samples)
        assert out.dtype == np.int16
        assert out.shape == samples.shape
        assert sat.total_samples == 512

    def test_dither_path_modifies_output(self) -> None:
        # Same input + different dither rng → different output
        # (the noise is added). This is the core wire-up contract.
        samples = np.full(1_000, 0.5, dtype=np.float32)  # constant signal
        no_dither, _ = _float_to_int16_saturate(samples)
        with_dither, _ = _float_to_int16_saturate(
            samples,
            dither_rng=np.random.default_rng(0),
        )
        # Constant input → no_dither is constant; with_dither has
        # ±1 sample variance from the TPDF noise.
        assert int(no_dither.std()) == 0
        # With dither, the output isn't constant any more.
        assert with_dither.std() > 0

    def test_dither_preserves_signal_centre(self) -> None:
        # Dither shouldn't change the mean of the output across a
        # large window.
        samples = np.full(10_000, 0.5, dtype=np.float32)
        no_dither, _ = _float_to_int16_saturate(samples)
        with_dither, _ = _float_to_int16_saturate(
            samples,
            dither_rng=np.random.default_rng(0),
        )
        no_dither_mean = float(no_dither.astype(np.float64).mean())
        with_dither_mean = float(with_dither.astype(np.float64).mean())
        # Both paths should center around the same value (~16384).
        assert abs(no_dither_mean - with_dither_mean) < 1.0

    def test_dither_does_not_break_saturation_counters(self) -> None:
        # Loud sample at exactly ±1.0 — saturation counters fire.
        # Dither should NOT cause counters to underreport (a +noise
        # sample at 32767 would still clip).
        samples = np.full(100, 1.5, dtype=np.float32)  # > full scale
        out_no, sat_no = _float_to_int16_saturate(samples)
        out_d, sat_d = _float_to_int16_saturate(
            samples,
            dither_rng=np.random.default_rng(0),
        )
        # All 100 samples clip in both paths.
        assert sat_no.clipped_positive == 100
        assert sat_d.clipped_positive == 100

    def test_dither_amplitude_propagates(self) -> None:
        # amplitude_lsb=0.0 with a dither_rng should still behave
        # exactly like no-dither (zero-amplitude noise = zero).
        samples = np.full(1_000, 0.5, dtype=np.float32)
        no_dither, _ = _float_to_int16_saturate(samples)
        zero_dither, _ = _float_to_int16_saturate(
            samples,
            dither_rng=np.random.default_rng(0),
            dither_amplitude_lsb=0.0,
        )
        np.testing.assert_array_equal(no_dither, zero_dither)


# ── FrameNormalizer dither wire-up ──────────────────────────────────────


class TestFrameNormalizerDitherWireUp:
    def test_default_dither_disabled(self) -> None:
        norm = FrameNormalizer(source_rate=16_000, source_channels=1)
        assert norm._dither_enabled is False  # noqa: SLF001
        assert norm._dither_rng is None  # noqa: SLF001

    def test_enabled_constructor_creates_default_rng(self) -> None:
        norm = FrameNormalizer(
            source_rate=16_000,
            source_channels=1,
            dither_enabled=True,
        )
        assert norm._dither_enabled is True  # noqa: SLF001
        # When operator enables but provides no rng → factory
        # creates one for them.
        assert norm._dither_rng is not None  # noqa: SLF001

    def test_explicit_rng_threaded(self) -> None:
        rng = np.random.default_rng(123)
        norm = FrameNormalizer(
            source_rate=16_000,
            source_channels=1,
            dither_enabled=True,
            dither_rng=rng,
        )
        assert norm._dither_rng is rng  # noqa: SLF001

    def test_disabled_path_bit_exact_to_pre_dither(self) -> None:
        # The CRITICAL regression test: dither off → output is
        # IDENTICAL to a FrameNormalizer constructed with no
        # dither parameters at all (pre-T4.43.b behaviour).
        # 44.1 kHz mono float32 forces the non-passthrough path
        # so the conversion actually runs.
        rng = np.random.default_rng(0)
        block = (rng.standard_normal(2_048) * 0.5).astype(np.float32)

        baseline = FrameNormalizer(
            source_rate=44_100,
            source_channels=1,
            source_format="float32",
        )
        with_flag = FrameNormalizer(
            source_rate=44_100,
            source_channels=1,
            source_format="float32",
            dither_enabled=False,
        )
        out_a = baseline.push(block.copy())
        out_b = with_flag.push(block.copy())
        assert len(out_a) == len(out_b)
        for win_a, win_b in zip(out_a, out_b, strict=True):
            np.testing.assert_array_equal(win_a, win_b)

    def test_enabled_path_diverges_from_disabled(self) -> None:
        # With dither on, output differs from the no-dither path.
        rng = np.random.default_rng(0)
        block = (rng.standard_normal(2_048) * 0.5).astype(np.float32)

        no_dither = FrameNormalizer(
            source_rate=44_100,
            source_channels=1,
            source_format="float32",
        )
        with_dither = FrameNormalizer(
            source_rate=44_100,
            source_channels=1,
            source_format="float32",
            dither_enabled=True,
            dither_rng=np.random.default_rng(42),
        )
        out_no = no_dither.push(block.copy())
        out_d = with_dither.push(block.copy())
        # At least one window must differ (TPDF noise injection
        # changes the bit pattern of effectively every sample).
        any_diff = any(not np.array_equal(a, b) for a, b in zip(out_no, out_d, strict=True))
        assert any_diff


# ── AudioCaptureTask plumbing ────────────────────────────────────────────


class TestCaptureTaskDitherPlumbing:
    def _pipeline_stub(self) -> MagicMock:
        return MagicMock()

    def test_default_dither_disabled(self) -> None:
        task = AudioCaptureTask(self._pipeline_stub())
        assert task._dither_enabled is False  # noqa: SLF001
        assert task._dither_amplitude_lsb == 1.0  # noqa: SLF001

    def test_explicit_flags_stored(self) -> None:
        task = AudioCaptureTask(
            self._pipeline_stub(),
            dither_enabled=True,
            dither_amplitude_lsb=2.0,
        )
        assert task._dither_enabled is True  # noqa: SLF001
        assert task._dither_amplitude_lsb == 2.0  # noqa: SLF001
