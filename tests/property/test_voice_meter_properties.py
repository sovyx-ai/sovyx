"""Hypothesis property tests for :class:`PeakHoldMeter`.

These target invariants that should hold for *any* input tone / frame
sequence, not the hand-picked values in ``tests/unit/voice/device_test``.
"""

from __future__ import annotations

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from sovyx.voice.device_test._meter import _FLOOR_DB, PeakHoldMeter


def _tone(amp: float, n: int = 512) -> np.ndarray:
    return np.full(n, int(amp * 32_767), dtype=np.int16)


class TestDbRangeInvariants:
    """The meter must never emit values outside the documented dB range."""

    @settings(max_examples=50, deadline=None)
    @given(
        amp=st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
        n=st.integers(min_value=1, max_value=2_048),
    )
    def test_rms_always_in_floor_to_zero(self, amp: float, n: int) -> None:
        meter = PeakHoldMeter()
        reading = meter.process(_tone(amp, n), clock_s=0.0)
        assert _FLOOR_DB <= reading.rms_db <= 0.1

    @settings(max_examples=50, deadline=None)
    @given(
        amp=st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
        n=st.integers(min_value=1, max_value=2_048),
    )
    def test_peak_always_in_floor_to_zero(self, amp: float, n: int) -> None:
        meter = PeakHoldMeter()
        reading = meter.process(_tone(amp, n), clock_s=0.0)
        assert _FLOOR_DB <= reading.peak_db <= 0.1

    @settings(max_examples=50, deadline=None)
    @given(amp=st.floats(min_value=0.0, max_value=1.0, allow_nan=False))
    def test_peak_is_greater_or_equal_to_rms(self, amp: float) -> None:
        meter = PeakHoldMeter()
        reading = meter.process(_tone(amp), clock_s=0.0)
        # For a flat tone, peak == rms to within rounding.
        assert reading.peak_db + 0.5 >= reading.rms_db


class TestHoldInvariant:
    """The hold marker should always track the max peak seen so far."""

    @settings(max_examples=30, deadline=None)
    @given(
        amps=st.lists(
            st.floats(min_value=0.01, max_value=1.0, allow_nan=False),
            min_size=1,
            max_size=20,
        ),
    )
    def test_hold_never_below_latest_peak(self, amps: list[float]) -> None:
        # Long hold + slow decay so the marker doesn't move during the run.
        meter = PeakHoldMeter(hold_ms=10_000, decay_db_per_sec=1.0)
        clock = 0.0
        latest_peak: float | None = None
        for a in amps:
            r = meter.process(_tone(a), clock_s=clock)
            latest_peak = r.peak_db
            # Hold >= this frame's peak (either because this set it, or a
            # prior loud frame latched higher).
            assert r.hold_db + 1e-6 >= latest_peak
            clock += 0.01


class TestDecayMonotonicity:
    """With no new peaks, the hold marker must only decrease over time."""

    @settings(max_examples=30, deadline=None)
    @given(
        start_amp=st.floats(min_value=0.2, max_value=1.0, allow_nan=False),
        n_ticks=st.integers(min_value=2, max_value=10),
    )
    def test_hold_decreases_after_window(
        self,
        start_amp: float,
        n_ticks: int,
    ) -> None:
        meter = PeakHoldMeter(hold_ms=0, decay_db_per_sec=10.0)
        # Latch a loud peak at t=0.
        r0 = meter.process(_tone(start_amp), clock_s=0.0)
        prev_hold = r0.hold_db
        for i in range(1, n_ticks + 1):
            r = meter.process(_tone(0.0001), clock_s=float(i))
            # Hold is monotonically non-increasing while no new peaks arrive.
            assert r.hold_db <= prev_hold + 1e-6
            prev_hold = r.hold_db


class TestResetClearsHistory:
    """After :meth:`reset`, the meter behaves as fresh on next process."""

    @settings(max_examples=30, deadline=None)
    @given(
        loud=st.floats(min_value=0.5, max_value=1.0, allow_nan=False),
        soft=st.floats(min_value=0.001, max_value=0.05, allow_nan=False),
    )
    def test_reset_forgets_peak(self, loud: float, soft: float) -> None:
        meter = PeakHoldMeter(hold_ms=10_000)
        meter.process(_tone(loud), clock_s=0.0)
        meter.reset()
        r = meter.process(_tone(soft), clock_s=0.0)
        # After reset, hold snaps to the soft tone — no ghost of the loud one.
        # Soft = 0.05 → ~-26 dBFS, so hold_db must be below -10 at worst.
        assert r.hold_db < -10.0


class TestVADTriggerMonotonic:
    """Higher amplitudes must not clear the VAD trigger that lower ones set."""

    @settings(max_examples=50, deadline=None)
    @given(amp=st.floats(min_value=0.1, max_value=1.0, allow_nan=False))
    def test_loud_always_triggers_at_minus30(self, amp: float) -> None:
        meter = PeakHoldMeter(vad_trigger_db=-30.0)
        r = meter.process(_tone(amp), clock_s=0.0)
        # amp >= 0.1 → ~-20 dBFS, always above -30.
        assert r.vad_trigger is True


class TestPeakHoldStability:
    """Rapid-fire successive frames should never produce NaN/inf values."""

    @settings(max_examples=20, deadline=None)
    @given(
        amps=st.lists(
            st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
            min_size=5,
            max_size=50,
        ),
    )
    def test_all_values_are_finite(self, amps: list[float]) -> None:
        import math

        meter = PeakHoldMeter()
        clock = 0.0
        for a in amps:
            r = meter.process(_tone(a), clock_s=clock)
            assert math.isfinite(r.rms_db)
            assert math.isfinite(r.peak_db)
            assert math.isfinite(r.hold_db)
            clock += 0.01


# No async needed for these — the meter is sync.
pytestmark = pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning")
