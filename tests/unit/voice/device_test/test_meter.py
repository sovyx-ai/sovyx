"""Tests for :class:`PeakHoldMeter` — analogue-VU ballistics."""

from __future__ import annotations

import numpy as np
import pytest

from sovyx.voice.device_test._meter import (
    _FLOOR_DB,
    MeterReading,
    PeakHoldMeter,
    _lin_to_db,
)


def _tone(amplitude: float, n: int = 512) -> np.ndarray:
    """Generate a flat int16 tone at the given [-1.0, 1.0] amplitude."""
    return np.full(n, int(amplitude * 32_767), dtype=np.int16)


class TestConstructor:
    """Guard clauses on :meth:`PeakHoldMeter.__init__`."""

    def test_negative_hold_rejected(self) -> None:
        with pytest.raises(ValueError, match="hold_ms must be >= 0"):
            PeakHoldMeter(hold_ms=-1)

    def test_zero_decay_rejected(self) -> None:
        with pytest.raises(ValueError, match="decay_db_per_sec must be > 0"):
            PeakHoldMeter(decay_db_per_sec=0.0)

    def test_clipping_db_out_of_range_rejected(self) -> None:
        with pytest.raises(ValueError, match="clipping_db must be in"):
            PeakHoldMeter(clipping_db=1.0)
        with pytest.raises(ValueError, match="clipping_db must be in"):
            PeakHoldMeter(clipping_db=-200.0)


class TestRMSAndPeak:
    """Core dB math is sound."""

    def test_silent_frame_returns_floor(self) -> None:
        meter = PeakHoldMeter()
        reading = meter.process(np.zeros(512, dtype=np.int16), clock_s=0.0)
        assert reading.rms_db == _FLOOR_DB
        assert reading.peak_db == _FLOOR_DB
        assert reading.clipping is False
        assert reading.vad_trigger is False

    def test_full_scale_peak_is_zero_db(self) -> None:
        meter = PeakHoldMeter()
        # 32_767 / 32_768 ≈ -0.000265 dBFS — essentially 0.
        reading = meter.process(_tone(1.0), clock_s=0.0)
        assert reading.peak_db >= -0.01
        assert reading.peak_db <= 0.0
        assert reading.clipping is True  # -0.3 threshold

    def test_half_scale_is_minus_6_db(self) -> None:
        meter = PeakHoldMeter()
        # 0.5 amplitude → 20*log10(0.5) ≈ -6.02 dBFS.
        reading = meter.process(_tone(0.5), clock_s=0.0)
        assert abs(reading.peak_db - -6.02) < 0.1
        assert reading.clipping is False

    def test_empty_frame_is_floor(self) -> None:
        meter = PeakHoldMeter()
        reading = meter.process(np.zeros(0, dtype=np.int16), clock_s=0.0)
        assert reading.rms_db == _FLOOR_DB
        assert reading.peak_db == _FLOOR_DB


class TestVADTrigger:
    """VAD flag tracks the RMS threshold, not peak."""

    def test_trigger_at_threshold(self) -> None:
        meter = PeakHoldMeter(vad_trigger_db=-30.0)
        # -20 dBFS tone is well above -30.
        reading = meter.process(_tone(0.1), clock_s=0.0)
        assert reading.vad_trigger is True

    def test_silent_below_trigger(self) -> None:
        meter = PeakHoldMeter(vad_trigger_db=-30.0)
        reading = meter.process(_tone(0.001), clock_s=0.0)
        assert reading.vad_trigger is False


class TestPeakHoldBallistic:
    """The peak marker latches at a peak and decays after ``hold_ms``."""

    def test_hold_latches_then_decays(self) -> None:
        meter = PeakHoldMeter(
            hold_ms=1_000,
            decay_db_per_sec=20.0,
        )
        # Loud hit at t=0.
        r0 = meter.process(_tone(1.0), clock_s=0.0)
        assert r0.hold_db >= -0.1

        # During hold window — marker stays put.
        r_hold = meter.process(_tone(0.01), clock_s=0.5)
        assert r_hold.hold_db == pytest.approx(r0.hold_db, abs=0.01)

        # Still inside the 1 s window — no decay yet.
        r_edge = meter.process(_tone(0.01), clock_s=0.999)
        assert r_edge.hold_db == pytest.approx(r0.hold_db, abs=0.01)

        # After hold expires, decay kicks in. At 20 dB/s across ~1 s of
        # elapsed frame time we expect a clearly visible drop.
        r_decay = meter.process(_tone(0.01), clock_s=2.0)
        assert r_decay.hold_db < r0.hold_db - 10.0

    def test_new_peak_resets_hold_timer(self) -> None:
        meter = PeakHoldMeter(hold_ms=500)
        r0 = meter.process(_tone(0.1), clock_s=0.0)  # ~-20 dBFS
        # Bigger peak at 0.4 s — hold should latch to it.
        r1 = meter.process(_tone(1.0), clock_s=0.4)
        assert r1.hold_db > r0.hold_db
        # The new hold window starts at 0.4s, so at 0.8s (400 ms in) the
        # marker must still match the peak we just saw.
        r2 = meter.process(_tone(0.01), clock_s=0.8)
        assert r2.hold_db == pytest.approx(r1.hold_db, abs=0.01)

    def test_reset_clears_state(self) -> None:
        meter = PeakHoldMeter(hold_ms=1_000)
        meter.process(_tone(1.0), clock_s=0.0)
        meter.reset()
        r = meter.process(_tone(0.1), clock_s=0.0)
        # After reset the hold snaps to the new peak, not the old high.
        assert r.hold_db < -15.0


class TestLinToDbHelper:
    """Pure-function numerics for :func:`_lin_to_db`."""

    def test_zero_returns_floor(self) -> None:
        assert _lin_to_db(0.0) == _FLOOR_DB

    def test_nan_returns_floor(self) -> None:
        assert _lin_to_db(float("nan")) == _FLOOR_DB

    def test_subnormal_clamps_to_floor(self) -> None:
        assert _lin_to_db(1e-20) == _FLOOR_DB

    def test_full_scale(self) -> None:
        assert _lin_to_db(1.0) == pytest.approx(0.0, abs=1e-6)


class TestMeterReading:
    """:class:`MeterReading` is frozen + typed."""

    def test_is_frozen(self) -> None:
        reading = MeterReading(
            rms_db=-30.0,
            peak_db=-20.0,
            hold_db=-20.0,
            clipping=False,
            vad_trigger=False,
        )
        with pytest.raises(AttributeError):
            reading.rms_db = -40.0  # type: ignore[misc]
