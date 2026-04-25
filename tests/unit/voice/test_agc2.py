"""Tests for :class:`sovyx.voice._agc2.AGC2` (F5).

Covers the WebRTC-AGC2-inspired closed-loop digital gain controller:

* Convergence on attenuated input — drives the gain UP toward a
  level that lifts the speech RMS to the target.
* Convergence on hot input — drives the gain DOWN to bring loud
  input below the saturation rail.
* Asymmetric attack vs release — fast suppression of transients
  vs slow lift of quiet input (no noise-floor pumping).
* Slew-rate limiter — caps per-second gain change to the
  configured ceiling.
* Silence gate — RMS below ``silence_floor_dbfs`` does NOT
  update the speech-level estimate.
* Saturation protector — post-gain peak is always ≤ int16 rail
  (no overflow), counter increments on actual clamps.
* Configuration validation — rejects pathological configs at
  construction.
* Reset semantics — clears state without re-instantiating.

Reference: MISSION-voice-mixer-enterprise-refactor-2026-04-25 §4, F5.
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from sovyx.voice._agc2 import (
    _DEFAULT_MAX_GAIN_CHANGE_DB_PER_SECOND,
    _DEFAULT_MAX_GAIN_DB,
    _DEFAULT_MIN_GAIN_DB,
    _DEFAULT_RELEASE_TIME_S,
    _DEFAULT_SILENCE_FLOOR_DBFS,
    _DEFAULT_TARGET_DBFS,
    _INT16_FULL_SCALE,
    _INT16_RAIL,
    AGC2,
    AGC2Config,
)

_SAMPLE_RATE = 16_000
_FRAME_SAMPLES = 512  # 32 ms — matches Sovyx pipeline frame size


def _sine_int16(amplitude: float, samples: int = _FRAME_SAMPLES) -> np.ndarray:
    """Generate a 1 kHz int16 sine at the given peak amplitude (0..1)."""
    t = np.arange(samples, dtype=np.float64) / _SAMPLE_RATE
    sine = amplitude * np.sin(2 * np.pi * 1000 * t)
    scaled = np.clip(sine * _INT16_FULL_SCALE, -_INT16_FULL_SCALE, _INT16_RAIL)
    return scaled.astype(np.int16)


def _rms_dbfs(samples: np.ndarray) -> float:
    """RMS in dBFS for an int16 PCM frame."""
    if samples.size == 0:
        return float("-inf")
    arr = samples.astype(np.float64)
    rms = float(np.sqrt(np.mean(arr * arr)))
    if rms <= 0.0:
        return float("-inf")
    return 20.0 * math.log10(rms / _INT16_FULL_SCALE)


# ── Defaults sanity ─────────────────────────────────────────────────


class TestDefaults:
    def test_target_dbfs(self) -> None:
        # Public-surface tuning value — bumps must be deliberate.
        assert _DEFAULT_TARGET_DBFS == -18.0

    def test_max_gain_db(self) -> None:
        assert _DEFAULT_MAX_GAIN_DB == 30.0

    def test_min_gain_db(self) -> None:
        assert _DEFAULT_MIN_GAIN_DB == -10.0

    def test_silence_floor_dbfs(self) -> None:
        assert _DEFAULT_SILENCE_FLOOR_DBFS == -60.0

    def test_default_release_slower_than_attack(self) -> None:
        """Release MUST be slower than attack (asymmetric AGC contract)."""
        assert _DEFAULT_RELEASE_TIME_S > 0.010  # > attack

    def test_default_slew_rate(self) -> None:
        assert _DEFAULT_MAX_GAIN_CHANGE_DB_PER_SECOND == 6.0


# ── Config validation ───────────────────────────────────────────────


class TestConfigValidation:
    def test_default_config_valid(self) -> None:
        AGC2(AGC2Config())  # no exception

    def test_target_above_zero_rejected(self) -> None:
        with pytest.raises(ValueError, match="target_dbfs"):
            AGC2(AGC2Config(target_dbfs=10.0))

    def test_target_below_floor_rejected(self) -> None:
        with pytest.raises(ValueError, match="target_dbfs"):
            AGC2(AGC2Config(target_dbfs=-100.0))

    def test_negative_max_gain_rejected(self) -> None:
        with pytest.raises(ValueError, match="max_gain_db"):
            AGC2(AGC2Config(max_gain_db=-5.0))

    def test_positive_min_gain_rejected(self) -> None:
        with pytest.raises(ValueError, match="min_gain_db"):
            AGC2(AGC2Config(min_gain_db=5.0))

    def test_silence_floor_above_target_rejected(self) -> None:
        """Gating out the target itself would freeze the controller."""
        with pytest.raises(ValueError, match="silence_floor_dbfs"):
            AGC2(AGC2Config(target_dbfs=-30.0, silence_floor_dbfs=-10.0))

    def test_zero_attack_time_rejected(self) -> None:
        with pytest.raises(ValueError, match="attack_time_s"):
            AGC2(AGC2Config(attack_time_s=0.0))

    def test_zero_release_time_rejected(self) -> None:
        with pytest.raises(ValueError, match="release_time_s"):
            AGC2(AGC2Config(release_time_s=0.0))

    def test_release_faster_than_attack_rejected(self) -> None:
        """Release < attack pumps up the noise floor — band-aid pattern."""
        with pytest.raises(ValueError, match="release_time_s"):
            AGC2(
                AGC2Config(
                    attack_time_s=0.5,
                    release_time_s=0.1,
                ),
            )

    def test_zero_slew_rate_rejected(self) -> None:
        with pytest.raises(ValueError, match="max_gain_change_db_per_second"):
            AGC2(AGC2Config(max_gain_change_db_per_second=0.0))

    def test_sample_rate_below_floor_rejected(self) -> None:
        with pytest.raises(ValueError, match="sample_rate"):
            AGC2(AGC2Config(sample_rate=4_000))

    def test_sample_rate_above_ceiling_rejected(self) -> None:
        with pytest.raises(ValueError, match="sample_rate"):
            AGC2(AGC2Config(sample_rate=96_000))


# ── Initial state ───────────────────────────────────────────────────


class TestInitialState:
    def test_starts_at_zero_db_gain(self) -> None:
        agc = AGC2()
        assert agc.current_gain_db == 0.0

    def test_starts_at_target_speech_level(self) -> None:
        """Speech-level estimate starts at target so first frame
        error is zero — controller stays put until real RMS arrives."""
        agc = AGC2(AGC2Config(target_dbfs=-15.0))
        assert agc.speech_level_dbfs == -15.0

    def test_lifetime_counters_zero(self) -> None:
        agc = AGC2()
        assert agc.frames_processed == 0
        assert agc.frames_silenced == 0
        assert agc.frames_clipped == 0


# ── Process — happy path ────────────────────────────────────────────


class TestProcess:
    def test_empty_frame_passes_through(self) -> None:
        agc = AGC2()
        empty = np.zeros(0, dtype=np.int16)
        out = agc.process(empty)
        assert out.size == 0
        assert agc.frames_processed == 1
        assert agc.frames_silenced == 1

    def test_output_dtype_always_int16(self) -> None:
        agc = AGC2()
        out = agc.process(_sine_int16(0.5))
        assert out.dtype == np.int16

    def test_output_shape_matches_input(self) -> None:
        agc = AGC2()
        sine = _sine_int16(0.5, samples=1024)
        out = agc.process(sine)
        assert out.shape == sine.shape

    def test_frames_processed_increments(self) -> None:
        agc = AGC2()
        for _ in range(5):
            agc.process(_sine_int16(0.5))
        assert agc.frames_processed == 5

    def test_silence_gate_skips_estimator_update(self) -> None:
        """Frames below silence floor count as silenced + don't move
        the speech-level estimate."""
        agc = AGC2()
        initial_estimate = agc.speech_level_dbfs
        # Tiny amplitude → RMS well below -60 dBFS.
        quiet = _sine_int16(0.0001)
        for _ in range(10):
            agc.process(quiet)
        assert agc.frames_silenced == 10
        assert agc.speech_level_dbfs == initial_estimate


# ── Convergence behaviour ───────────────────────────────────────────


class TestConvergence:
    """The core enterprise contract: AGC2 brings RMS toward target
    over time, with slew-rate-limited adaptation."""

    def test_quiet_input_drives_gain_up(self) -> None:
        """Sustained -40 dBFS input should drive the gain up toward
        ``target - rms`` (positive)."""
        agc = AGC2()
        # 0.01 amplitude → RMS ~ -43 dBFS — below target -18 dBFS,
        # above -60 dBFS silence floor.
        quiet = _sine_int16(0.01)
        for _ in range(200):
            agc.process(quiet)
        # After convergence the gain should be POSITIVE (lifting up).
        assert agc.current_gain_db > 5.0

    def test_loud_input_drives_gain_down(self) -> None:
        """Sustained near-full-scale input should drive the gain down
        toward attenuation (or 0 dB if min_gain_db is -10)."""
        agc = AGC2()
        # 0.95 amplitude → RMS ~ -3 dBFS — well above target -18 dBFS.
        loud = _sine_int16(0.95)
        for _ in range(200):
            agc.process(loud)
        # Gain went NEGATIVE (suppressing) — at least toward target.
        assert agc.current_gain_db < 0.0

    def test_target_aligned_input_holds_gain_near_zero(self) -> None:
        """Input already at target should produce ~0 dB gain after
        convergence."""
        agc = AGC2()
        # Compute the amplitude that produces -18 dBFS RMS for a sine.
        # RMS of sine = peak / sqrt(2). dBFS = 20*log10(rms / 32768).
        # Solving for peak: peak = sqrt(2) * 10^(target/20) * 32768.
        target_peak_lin = math.sqrt(2) * 10 ** (_DEFAULT_TARGET_DBFS / 20.0)
        target_sine = _sine_int16(target_peak_lin)
        for _ in range(200):
            agc.process(target_sine)
        # Gain should be small (within ±2 dB of zero).
        assert abs(agc.current_gain_db) < 2.0

    def test_gain_clamped_to_max(self) -> None:
        """Even on a near-silent (above-floor) input, the gain
        cannot exceed ``max_gain_db``."""
        agc = AGC2(AGC2Config(max_gain_db=20.0, min_gain_db=-10.0))
        # Very quiet but above the silence floor.
        # RMS roughly -55 dBFS — would request ~37 dB of gain.
        very_quiet = _sine_int16(0.0025)
        for _ in range(500):
            agc.process(very_quiet)
        assert agc.current_gain_db <= 20.0  # noqa: PLR2004

    def test_gain_clamped_to_min(self) -> None:
        agc = AGC2(AGC2Config(min_gain_db=-5.0))
        loud = _sine_int16(0.99)  # ~ -3 dBFS RMS
        for _ in range(500):
            agc.process(loud)
        assert agc.current_gain_db >= -5.0  # noqa: PLR2004


# ── Slew-rate limiter ───────────────────────────────────────────────


class TestSlewRateLimit:
    def test_per_frame_change_capped_at_rate(self) -> None:
        """A single frame can only change the gain by
        ``rate × frame_duration`` dB."""
        # Use a tight slew rate so the cap is observable in one frame.
        agc = AGC2(AGC2Config(max_gain_change_db_per_second=1.0))
        # 32 ms frame at 1 dB/s rate → max 0.032 dB change per frame.
        quiet = _sine_int16(0.001)  # below floor — actually let's use audible
        audible_quiet = _sine_int16(0.005)  # ~-49 dBFS
        # First frame primes the estimator.
        agc.process(audible_quiet)
        gain_before = agc.current_gain_db
        # Drive a single frame — gain change ≤ 0.032 dB.
        agc.process(audible_quiet)
        gain_after = agc.current_gain_db
        assert abs(gain_after - gain_before) <= 0.05  # +1 dB headroom for FP


# ── Saturation protector ────────────────────────────────────────────


class TestSaturationProtector:
    def test_no_clip_on_quiet_input(self) -> None:
        agc = AGC2()
        out = agc.process(_sine_int16(0.1))
        # Output bounded by int16 rails.
        assert out.min() >= -_INT16_FULL_SCALE
        assert out.max() <= _INT16_RAIL

    def test_loud_input_with_high_gain_does_not_overflow(self) -> None:
        """A frame that would require gain × peak > rail must clamp."""
        agc = AGC2()
        # Manually set a large gain that would clip a near-full-scale input.
        agc._current_gain_db = 20.0
        loud = _sine_int16(0.9)
        out = agc.process(loud)
        # int16 dtype + clip means no values past the rails.
        assert out.min() >= -_INT16_FULL_SCALE
        assert out.max() <= _INT16_RAIL

    def test_clip_counter_increments_on_clamp(self) -> None:
        """A clamp event increments frames_clipped."""
        agc = AGC2()
        agc._current_gain_db = 30.0  # huge gain
        loud = _sine_int16(0.9)
        agc.process(loud)
        assert agc.frames_clipped >= 1


# ── Reset ────────────────────────────────────────────────────────────


class TestReset:
    def test_reset_clears_state(self) -> None:
        agc = AGC2()
        # Drive the controller into adapted state.
        for _ in range(50):
            agc.process(_sine_int16(0.5))
        assert agc.frames_processed > 0
        agc.reset()
        assert agc.current_gain_db == 0.0
        assert agc.speech_level_dbfs == _DEFAULT_TARGET_DBFS
        assert agc.frames_processed == 0
        assert agc.frames_silenced == 0
        assert agc.frames_clipped == 0


# ── Property-based invariants ───────────────────────────────────────


class TestPropertyInvariants:
    @settings(max_examples=30, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(amplitude=st.floats(min_value=0.001, max_value=0.99))
    def test_output_always_int16_bounded(self, amplitude: float) -> None:
        """For any input in the [-1, 1] range, output stays within
        int16 rails. Never overflows, regardless of the AGC's
        adaptation state."""
        agc = AGC2()
        sine = _sine_int16(amplitude)
        out = agc.process(sine)
        assert out.dtype == np.int16
        assert int(out.min()) >= -32768  # noqa: PLR2004
        assert int(out.max()) <= 32767  # noqa: PLR2004

    @settings(max_examples=20, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(amplitude=st.floats(min_value=0.005, max_value=0.5))
    def test_gain_always_within_configured_bounds(self, amplitude: float) -> None:
        """No matter how many frames are processed, gain stays
        between min_gain_db and max_gain_db."""
        cfg = AGC2Config(min_gain_db=-12.0, max_gain_db=24.0)
        agc = AGC2(cfg)
        sine = _sine_int16(amplitude)
        for _ in range(100):
            agc.process(sine)
            assert cfg.min_gain_db <= agc.current_gain_db <= cfg.max_gain_db

    @settings(max_examples=20, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(
        amplitudes=st.lists(
            st.floats(min_value=0.005, max_value=0.5),
            min_size=10,
            max_size=50,
        ),
    )
    def test_lifetime_counter_invariants(self, amplitudes: list[float]) -> None:
        agc = AGC2()
        for amp in amplitudes:
            agc.process(_sine_int16(amp))
        assert agc.frames_processed == len(amplitudes)
        # frames_silenced + processed-but-non-silenced == frames_processed.
        # We don't know the exact split because audibility depends on
        # amplitude; just assert no overflow.
        assert agc.frames_silenced <= agc.frames_processed
        assert agc.frames_clipped <= agc.frames_processed


# ── Module exports ──────────────────────────────────────────────────


class TestPublicSurface:
    def test_all_exports(self) -> None:
        from sovyx.voice import _agc2 as mod

        assert set(mod.__all__) == {"AGC2", "AGC2Config", "build_agc2_if_enabled"}


# ── F5/F6 promotion helper ────────────────────────────────────────


class TestBuildAgc2IfEnabled:
    """build_agc2_if_enabled honours the EngineConfig.tuning.voice.
    agc2_enabled flag — central factory for the F5→F6 promotion."""

    def test_enabled_returns_agc2_instance(self) -> None:
        from sovyx.voice._agc2 import AGC2, build_agc2_if_enabled

        agc = build_agc2_if_enabled(enabled=True)
        assert isinstance(agc, AGC2)

    def test_disabled_returns_none(self) -> None:
        from sovyx.voice._agc2 import build_agc2_if_enabled

        assert build_agc2_if_enabled(enabled=False) is None

    def test_sample_rate_threaded_into_config(self) -> None:
        """Sample rate flows into AGC2Config so the slew-rate
        calculations use the right frame-duration denominator."""
        from sovyx.voice._agc2 import build_agc2_if_enabled

        agc = build_agc2_if_enabled(enabled=True, sample_rate=48_000)
        assert agc is not None
        assert agc.config.sample_rate == 48_000

    def test_default_sample_rate_is_16k(self) -> None:
        """Default matches the pipeline's invariant 16 kHz target rate."""
        from sovyx.voice._agc2 import build_agc2_if_enabled

        agc = build_agc2_if_enabled(enabled=True)
        assert agc is not None
        assert agc.config.sample_rate == 16_000


# ── EngineConfig agc2_enabled default ─────────────────────────────


class TestVoiceTuningAgc2Default:
    """The promotion contract: agc2_enabled default flips from
    False (pre-F5/F6) to True (post-promotion). This test pins
    the default so a future regression downgrade surfaces as a
    test failure."""

    def test_default_is_true(self) -> None:
        from sovyx.engine.config import VoiceTuningConfig

        # Construct without env override.
        cfg = VoiceTuningConfig()
        assert cfg.agc2_enabled is True

    def test_env_override_disables(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from sovyx.engine.config import VoiceTuningConfig

        monkeypatch.setenv("SOVYX_TUNING__VOICE__AGC2_ENABLED", "false")
        cfg = VoiceTuningConfig()
        assert cfg.agc2_enabled is False
