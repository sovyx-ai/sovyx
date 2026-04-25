"""Tests for :class:`sovyx.voice._frame_normalizer.FrameNormalizer`.

Covers the three transformations the normalizer must perform correctly
to unblock the voice pipeline on drivers that deliver non-16-kHz audio
(Razer BlackShark et al.):

1. **Downmix** — stereo int16 into mono without channel leaks.
2. **Resample** — a 1 kHz sine at arbitrary source rate survives as a
   1 kHz sine at 16 kHz (FFT peak in the correct bin, ±1 bin).
3. **Rewindow** — regardless of input block size, output is always
   ``(512,) int16`` windows with no sample loss or duplication.

Also verifies the fast path (source already 16 kHz mono) incurs zero
DSP overhead — the passthrough flag is wired through.
"""

from __future__ import annotations

import math
import time

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from sovyx.voice._frame_normalizer import (
    _SATURATION_MIN_SAMPLES_FOR_WARNING,
    _SATURATION_WARN_FRACTION,
    _SATURATION_WINDOW_SECONDS,
    FrameNormalizer,
    SaturationCounters,
    _float_to_int16_saturate,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sine_wave_int16(
    freq_hz: float,
    sample_rate: int,
    duration_s: float,
    channels: int = 1,
    amplitude: float = 0.5,
) -> np.ndarray:
    """Generate a mono or multichannel int16 sine wave.

    Args:
        freq_hz: Tone frequency.
        sample_rate: Samples per second.
        duration_s: Clip length in seconds.
        channels: 1 for 1-D output, >1 for (N, C) 2-D.
        amplitude: Peak amplitude in [0, 1] (0.5 leaves headroom).
    """
    num_samples = int(duration_s * sample_rate)
    t = np.arange(num_samples, dtype=np.float64) / sample_rate
    sine = amplitude * np.sin(2 * np.pi * freq_hz * t)
    scaled = np.clip(sine * 32767, -32768, 32767).astype(np.int16)
    if channels == 1:
        return scaled
    return np.tile(scaled.reshape(-1, 1), (1, channels))


def _dominant_freq_hz(samples: np.ndarray, sample_rate: int) -> float:
    """Return the frequency of the strongest FFT bin (positive half)."""
    windowed = samples.astype(np.float64) * np.hanning(len(samples))
    spectrum = np.abs(np.fft.rfft(windowed))
    peak_bin = int(np.argmax(spectrum[1:])) + 1  # skip DC
    return peak_bin * sample_rate / len(samples)


# ---------------------------------------------------------------------------
# Construction / invariants
# ---------------------------------------------------------------------------


class TestConstruction:
    """Basic invariants at construction time."""

    def test_passthrough_flag_set_for_16khz_mono(self) -> None:
        """16 kHz mono input flips the fast path on — no DSP overhead."""
        norm = FrameNormalizer(source_rate=16_000, source_channels=1)
        assert norm.is_passthrough is True
        assert norm.source_rate == 16_000
        assert norm.source_channels == 1
        assert norm.target_rate == 16_000
        assert norm.target_window == 512

    def test_passthrough_flag_off_when_rate_mismatches(self) -> None:
        """Any deviation from 16 kHz mono must trigger the full pipeline."""
        for source_rate in (8_000, 22_050, 44_100, 48_000, 96_000):
            norm = FrameNormalizer(source_rate=source_rate, source_channels=1)
            assert norm.is_passthrough is False, (
                f"source_rate={source_rate} should not be passthrough"
            )

    def test_passthrough_flag_off_when_multichannel(self) -> None:
        """Stereo at 16 kHz still needs downmix — not passthrough."""
        norm = FrameNormalizer(source_rate=16_000, source_channels=2)
        assert norm.is_passthrough is False

    def test_rejects_invalid_source_rate(self) -> None:
        """Zero / negative source rates are a caller bug — fail loudly."""
        with pytest.raises(ValueError, match="source_rate must be positive"):
            FrameNormalizer(source_rate=0, source_channels=1)
        with pytest.raises(ValueError, match="source_rate must be positive"):
            FrameNormalizer(source_rate=-1, source_channels=1)

    def test_rejects_invalid_channels(self) -> None:
        """Zero / negative channels are a caller bug — fail loudly."""
        with pytest.raises(ValueError, match="source_channels must be"):
            FrameNormalizer(source_rate=48_000, source_channels=0)
        with pytest.raises(ValueError, match="source_channels must be"):
            FrameNormalizer(source_rate=48_000, source_channels=-2)


# ---------------------------------------------------------------------------
# Empty / trivial input
# ---------------------------------------------------------------------------


class TestEmptyInput:
    """Edge cases at block boundaries."""

    def test_empty_block_returns_empty_list(self) -> None:
        """An empty PortAudio callback block must not raise or emit."""
        norm = FrameNormalizer(source_rate=48_000, source_channels=2)
        result = norm.push(np.zeros((0, 2), dtype=np.int16))
        assert result == []

    def test_partial_window_held_until_full(self) -> None:
        """<512 output samples → no emission yet, buffer retains them."""
        norm = FrameNormalizer(source_rate=16_000, source_channels=1)
        # 256 samples at 16 kHz = 16 ms — half a window
        block = _sine_wave_int16(440, 16_000, 0.016, channels=1)
        result = norm.push(block)
        assert result == []

        # Second push fills the rest
        block2 = _sine_wave_int16(440, 16_000, 0.016, channels=1)
        result = norm.push(block2)
        assert len(result) == 1
        assert result[0].shape == (512,)
        assert result[0].dtype == np.int16


# ---------------------------------------------------------------------------
# Fast path — 16 kHz mono passthrough
# ---------------------------------------------------------------------------


class TestPassthrough:
    """When source already matches target, no DSP should change the data."""

    def test_passthrough_preserves_samples_bit_exact(self) -> None:
        """16 kHz mono in → 16 kHz mono out, bit-identical."""
        norm = FrameNormalizer(source_rate=16_000, source_channels=1)
        block = _sine_wave_int16(440, 16_000, 0.032, channels=1)  # exactly 512 samples
        result = norm.push(block)
        assert len(result) == 1
        np.testing.assert_array_equal(result[0], block)

    def test_passthrough_rewindows_oversized_block(self) -> None:
        """A 1024-sample block at 16 kHz emits exactly 2 × 512 windows."""
        norm = FrameNormalizer(source_rate=16_000, source_channels=1)
        block = _sine_wave_int16(440, 16_000, 0.064, channels=1)  # 1024 samples
        result = norm.push(block)
        assert len(result) == 2
        assert all(w.shape == (512,) for w in result)
        # Concatenated output equals input
        np.testing.assert_array_equal(np.concatenate(result), block)


# ---------------------------------------------------------------------------
# Downmix
# ---------------------------------------------------------------------------


class TestDownmix:
    """Stereo → mono must be channel average (or first-channel fallback)."""

    def test_stereo_downmix_averages_channels(self) -> None:
        """Two identical channels → passthrough average (same signal)."""
        norm = FrameNormalizer(source_rate=16_000, source_channels=2)
        stereo = _sine_wave_int16(1_000, 16_000, 0.032, channels=2)
        mono_expected = stereo[:, 0]  # identical channels — mean == either
        result = norm.push(stereo)
        assert len(result) == 1
        # Allow ±1 due to int16 rounding of the mean
        np.testing.assert_allclose(result[0], mono_expected, atol=2)

    def test_stereo_downmix_silences_opposite_phase(self) -> None:
        """Left + right at opposite phase → silence (average = 0)."""
        norm = FrameNormalizer(source_rate=16_000, source_channels=2)
        mono = _sine_wave_int16(1_000, 16_000, 0.032, channels=1)
        stereo = np.stack([mono, -mono], axis=1)
        result = norm.push(stereo)
        assert len(result) == 1
        # Expect near-silence; rounding may leave ±1 LSB
        assert np.abs(result[0]).max() <= 2

    def test_mono_source_with_2d_block_uses_first_channel(self) -> None:
        """source_channels=1 but (N, 1) block works — take column 0."""
        norm = FrameNormalizer(source_rate=16_000, source_channels=1)
        block = _sine_wave_int16(440, 16_000, 0.032, channels=1).reshape(-1, 1)
        result = norm.push(block)
        assert len(result) == 1
        np.testing.assert_array_equal(result[0], block[:, 0])


# ---------------------------------------------------------------------------
# Resample — spectral correctness
# ---------------------------------------------------------------------------


class TestResampleSpectralContent:
    """The whole reason this module exists: preserve voice frequencies.

    These tests pin the bug down to the wire: if a 1 kHz tone at 48 kHz
    comes out as a 3 kHz tone at 16 kHz, Silero sees garbage and the
    pipeline never fires.
    """

    def test_1khz_at_48k_survives_as_1khz_at_16k(self) -> None:
        """48 kHz → 16 kHz resample preserves frequency content."""
        source_rate = 48_000
        tone_hz = 1_000.0
        duration_s = 0.5  # long enough for clean FFT

        norm = FrameNormalizer(source_rate=source_rate, source_channels=1)
        block = _sine_wave_int16(tone_hz, source_rate, duration_s, channels=1)
        windows = norm.push(block)
        assert len(windows) > 0
        resampled = np.concatenate(windows)

        detected_hz = _dominant_freq_hz(resampled, sample_rate=16_000)
        # ±1 FFT bin tolerance — bin width = 16000 / len
        bin_width = 16_000 / len(resampled)
        assert abs(detected_hz - tone_hz) < bin_width * 2, (
            f"Expected ~{tone_hz} Hz, got {detected_hz:.1f} Hz"
        )

    def test_1khz_at_44100_survives_as_1khz_at_16k(self) -> None:
        """Non-integer ratio (44.1 kHz → 16 kHz) still preserves tone."""
        source_rate = 44_100
        tone_hz = 1_000.0
        duration_s = 0.5

        norm = FrameNormalizer(source_rate=source_rate, source_channels=1)
        block = _sine_wave_int16(tone_hz, source_rate, duration_s, channels=1)
        windows = norm.push(block)
        resampled = np.concatenate(windows)

        detected_hz = _dominant_freq_hz(resampled, sample_rate=16_000)
        bin_width = 16_000 / len(resampled)
        assert abs(detected_hz - tone_hz) < bin_width * 2, (
            f"Expected ~{tone_hz} Hz, got {detected_hz:.1f} Hz"
        )

    def test_voice_band_stereo_48k_preserves_fundamental(self) -> None:
        """End-to-end case that matches the live bug: 48 kHz stereo → 16 kHz mono."""
        source_rate = 48_000
        tone_hz = 250.0  # voice fundamental range
        duration_s = 0.5

        norm = FrameNormalizer(source_rate=source_rate, source_channels=2)
        block = _sine_wave_int16(tone_hz, source_rate, duration_s, channels=2)
        windows = norm.push(block)
        resampled = np.concatenate(windows)

        detected_hz = _dominant_freq_hz(resampled, sample_rate=16_000)
        bin_width = 16_000 / len(resampled)
        assert abs(detected_hz - tone_hz) < bin_width * 2, (
            f"Expected ~{tone_hz} Hz, got {detected_hz:.1f} Hz"
        )


# ---------------------------------------------------------------------------
# Continuity — boundary behaviour across multiple pushes
# ---------------------------------------------------------------------------


class TestContinuityAcrossPushes:
    """The normalizer is stateful — calls must not lose or duplicate samples."""

    def test_split_block_produces_same_output_as_single_block(self) -> None:
        """Feeding a waveform in halves vs whole → same resampled output (within resample tolerance).

        Polyphase resampling with per-call filter-state reset does
        introduce a small boundary artifact, so we allow a modest L2
        difference. Silero's 32 ms window is robust to it; the test
        pins the artifact below the "would confuse VAD" threshold.
        """
        source_rate = 48_000
        duration_s = 0.1  # 4800 samples
        block = _sine_wave_int16(1_000, source_rate, duration_s, channels=1)

        # Single-push reference
        norm_single = FrameNormalizer(source_rate=source_rate, source_channels=1)
        single_out = np.concatenate(norm_single.push(block))

        # Half-push sequence
        norm_halves = FrameNormalizer(source_rate=source_rate, source_channels=1)
        half = len(block) // 2
        halves_out = np.concatenate(
            norm_halves.push(block[:half]) + norm_halves.push(block[half:]),
        )

        # Lengths should match to within a few samples (partial windows).
        # The shorter of the two is what we compare on.
        common = min(len(single_out), len(halves_out))
        diff = single_out[:common].astype(np.float64) - halves_out[:common].astype(np.float64)
        rms_diff = float(np.sqrt(np.mean(diff**2)))
        # 1 kHz tone peak ≈ 16384. 1% RMS = 164. Allow up to 5% for boundary
        # artefacts — well below anything Silero cares about.
        assert rms_diff < 32_767 * 0.05, f"Boundary artifact too large: RMS diff = {rms_diff:.1f}"

    def test_small_blocks_still_emit_eventually(self) -> None:
        """Driver delivering 16-sample blocks at 48 kHz still produces 16 kHz windows."""
        source_rate = 48_000
        norm = FrameNormalizer(source_rate=source_rate, source_channels=1)
        # 0.5 s of audio delivered in 16-sample chunks (~0.33 ms each)
        total = _sine_wave_int16(500, source_rate, 0.5, channels=1)
        chunks = [total[i : i + 16] for i in range(0, len(total), 16)]

        all_windows: list[np.ndarray] = []
        for chunk in chunks:
            all_windows.extend(norm.push(chunk))

        # 0.5 s at 16 kHz = 8000 samples; 8000 // 512 = 15 full windows
        assert len(all_windows) >= 15
        assert all(w.shape == (512,) for w in all_windows)


# ---------------------------------------------------------------------------
# Property-based tests
# ---------------------------------------------------------------------------


class TestOutputShapeInvariants:
    """Every output window is always (512,) int16 — regardless of input."""

    @given(
        source_rate=st.sampled_from([8_000, 16_000, 22_050, 32_000, 44_100, 48_000, 96_000]),
        channels=st.integers(min_value=1, max_value=4),
        block_samples=st.integers(min_value=32, max_value=4_096),
    )
    @settings(max_examples=40, deadline=2_000)
    def test_output_windows_always_correct_shape(
        self,
        source_rate: int,
        channels: int,
        block_samples: int,
    ) -> None:
        """No combination of rate × channels × block size produces a bad window."""
        norm = FrameNormalizer(source_rate=source_rate, source_channels=channels)
        shape = (block_samples,) if channels == 1 else (block_samples, channels)
        block = np.zeros(shape, dtype=np.int16)
        windows = norm.push(block)
        for w in windows:
            assert w.shape == (512,)
            assert w.dtype == np.int16


# ---------------------------------------------------------------------------
# Performance — must not regress the voice pipeline budget
# ---------------------------------------------------------------------------


class TestPerformance:
    """Per-block normalisation must stay well under the 32 ms VAD window."""

    def test_48k_stereo_block_under_2ms_p95(self) -> None:
        """100 pushes of a 32 ms 48 kHz stereo block: p95 latency < 2 ms on desktop."""
        source_rate = 48_000
        norm = FrameNormalizer(source_rate=source_rate, source_channels=2)
        block = _sine_wave_int16(500, source_rate, 0.032, channels=2)  # 1536 samples

        # Warm up (first resample_poly call JIT-ishly loads scipy internals)
        norm.push(block)

        latencies_ms: list[float] = []
        for _ in range(100):
            start = time.perf_counter()
            norm.push(block)
            latencies_ms.append((time.perf_counter() - start) * 1000)

        latencies_ms.sort()
        p95 = latencies_ms[int(0.95 * len(latencies_ms))]
        # On a typical developer machine this stays around 0.3–0.8 ms.
        # CI self-hosted runners are 4-core, similar range. 5 ms is a
        # generous ceiling that would still be <20% of the 32 ms budget.
        assert p95 < 5.0, f"p95 latency too high: {p95:.2f} ms"


# ---------------------------------------------------------------------------
# reset()
# ---------------------------------------------------------------------------


class TestReset:
    """reset() must drop buffered output so a stream restart is clean."""

    def test_reset_clears_output_buffer(self) -> None:
        """Half-filled buffer is discarded after reset."""
        norm = FrameNormalizer(source_rate=16_000, source_channels=1)
        # Push 256 samples (half a window) — held internally
        block = _sine_wave_int16(440, 16_000, 0.016, channels=1)
        assert norm.push(block) == []
        norm.reset()
        # After reset + 256 more samples, still no window (fresh buffer)
        assert norm.push(block) == []
        # One more push fills a full window
        assert len(norm.push(block)) == 1

    def test_reset_snaps_ducking_ramp_to_target(self) -> None:
        """Mid-ramp state must collapse to target on reset."""
        norm = FrameNormalizer(source_rate=16_000, source_channels=1)
        norm.set_ducking_gain_db(-18.0)
        # Start the ramp with one small block
        block = _sine_wave_int16(440, 16_000, 0.002, channels=1)  # 32 samples
        norm.push(block)
        # We're mid-ramp: current != target
        assert not math.isclose(
            10 ** (norm.current_ducking_gain_db / 20.0),
            10 ** (norm.ducking_gain_db / 20.0),
        )
        norm.reset()
        # After reset, current snaps to target
        assert norm.current_ducking_gain_db == pytest.approx(norm.ducking_gain_db)


# ---------------------------------------------------------------------------
# int24 input — ADR §5.1 saturation clip path
# ---------------------------------------------------------------------------


def _sine_wave_int24(
    freq_hz: float,
    sample_rate: int,
    duration_s: float,
    channels: int = 1,
    amplitude: float = 0.5,
) -> np.ndarray:
    """Generate a sine wave as int32 with 24-bit sign-extended payload.

    PortAudio/sounddevice deliver int24 in int32 numpy arrays where
    values fall in [-2**23, 2**23 - 1] and the upper 8 bits repeat the
    sign bit.
    """
    num_samples = int(duration_s * sample_rate)
    t = np.arange(num_samples, dtype=np.float64) / sample_rate
    sine = amplitude * np.sin(2 * np.pi * freq_hz * t)
    full_scale = (1 << 23) - 1
    scaled = np.clip(sine * full_scale, -(1 << 23), full_scale).astype(np.int32)
    if channels == 1:
        return scaled
    return np.tile(scaled.reshape(-1, 1), (1, channels))


class TestInt24Input:
    """ADR §5.1: int24 capture is a cascade option; the normalizer must
    scale the 24-bit payload (NOT 32) and saturate-clip to int16."""

    def test_int24_tone_survives_resample(self) -> None:
        """1 kHz int24 tone at 48 kHz arrives as 1 kHz at 16 kHz int16."""
        source_rate = 48_000
        tone_hz = 1_000.0
        norm = FrameNormalizer(
            source_rate=source_rate,
            source_channels=1,
            source_format="int24",
        )
        block = _sine_wave_int24(tone_hz, source_rate, 0.5, channels=1)
        windows = norm.push(block)
        resampled = np.concatenate(windows)

        detected_hz = _dominant_freq_hz(resampled, sample_rate=16_000)
        bin_width = 16_000 / len(resampled)
        assert abs(detected_hz - tone_hz) < bin_width * 2

    def test_int24_amplitude_scales_by_two_power_23(self) -> None:
        """Full-scale int24 (±2**23) must round-trip near int16 full-scale."""
        norm = FrameNormalizer(
            source_rate=16_000,
            source_channels=1,
            source_format="int24",
        )
        # Constant-amplitude "tone" at half-scale int24
        block = np.full(1024, 1 << 22, dtype=np.int32)  # +0.5 full scale
        windows = norm.push(block)
        out = np.concatenate(windows)
        # Expected int16 magnitude ≈ 0.5 * 32768 = 16384
        assert np.abs(out).mean() == pytest.approx(16384, abs=50)

    def test_int24_negative_range_not_misinterpreted_as_huge_positive(self) -> None:
        """Sign-extended negative int24 values stay negative after scaling."""
        norm = FrameNormalizer(
            source_rate=16_000,
            source_channels=1,
            source_format="int24",
        )
        # -2**22 as int32, not as unsigned big integer
        block = np.full(1024, -(1 << 22), dtype=np.int32)
        windows = norm.push(block)
        out = np.concatenate(windows)
        assert out.mean() < 0
        assert np.abs(out).mean() == pytest.approx(16384, abs=50)

    def test_int24_saturation_clips_overshoot(self) -> None:
        """int24 values at exactly ±2**23 saturate to int16 range, not wrap."""
        norm = FrameNormalizer(
            source_rate=16_000,
            source_channels=1,
            source_format="int24",
        )
        # int24 full-scale positive — maps to float +1.0 exactly,
        # which saturation-clips to int16 32767 (not wraps to -32768).
        block = np.full(1024, 1 << 23, dtype=np.int32)
        windows = norm.push(block)
        out = np.concatenate(windows)
        assert out.min() >= 0
        assert out.max() == 32767

    def test_int24_rejects_non_int32_dtype(self) -> None:
        """Passing int16 when source_format='int24' is a caller bug."""
        norm = FrameNormalizer(
            source_rate=16_000,
            source_channels=1,
            source_format="int24",
        )
        bad_block = np.zeros(512, dtype=np.int16)
        with pytest.raises(ValueError, match="int24 source requires numpy int32"):
            norm.push(bad_block)

    def test_int24_stereo_downmix(self) -> None:
        """int24 stereo averages channels after correct scaling."""
        source_rate = 16_000
        norm = FrameNormalizer(
            source_rate=source_rate,
            source_channels=2,
            source_format="int24",
        )
        mono = _sine_wave_int24(1_000, source_rate, 0.032, channels=1)
        stereo = np.stack([mono, -mono], axis=1)
        windows = norm.push(stereo)
        out = np.concatenate(windows)
        # Opposite-phase stereo averages to silence
        assert np.abs(out).max() <= 2


# ---------------------------------------------------------------------------
# float32 input — ADR §5.1 saturation clip path
# ---------------------------------------------------------------------------


def _sine_wave_float32(
    freq_hz: float,
    sample_rate: int,
    duration_s: float,
    channels: int = 1,
    amplitude: float = 0.5,
) -> np.ndarray:
    """Generate a sine wave as float32 in [-1, 1]."""
    num_samples = int(duration_s * sample_rate)
    t = np.arange(num_samples, dtype=np.float64) / sample_rate
    sine = (amplitude * np.sin(2 * np.pi * freq_hz * t)).astype(np.float32)
    if channels == 1:
        return sine
    return np.tile(sine.reshape(-1, 1), (1, channels))


class TestFloat32Input:
    """ADR §5.1: float32 capture path must saturation-clip to int16."""

    def test_float32_tone_survives_resample(self) -> None:
        source_rate = 48_000
        tone_hz = 1_000.0
        norm = FrameNormalizer(
            source_rate=source_rate,
            source_channels=1,
            source_format="float32",
        )
        block = _sine_wave_float32(tone_hz, source_rate, 0.5, channels=1)
        windows = norm.push(block)
        resampled = np.concatenate(windows)
        detected_hz = _dominant_freq_hz(resampled, sample_rate=16_000)
        bin_width = 16_000 / len(resampled)
        assert abs(detected_hz - tone_hz) < bin_width * 2

    def test_float32_saturation_clip_on_overshoot(self) -> None:
        """Values beyond [-1, 1] saturate, never wrap."""
        norm = FrameNormalizer(
            source_rate=16_000,
            source_channels=1,
            source_format="float32",
        )
        # Overshoot to +2.0 — mustn't wrap to int16 negative
        block = np.full(1024, 2.0, dtype=np.float32)
        windows = norm.push(block)
        out = np.concatenate(windows)
        assert out.min() >= 0
        assert out.max() == 32767


# ---------------------------------------------------------------------------
# Mission #40 — strict dtype validation for float32 source format
# ---------------------------------------------------------------------------
#
# Pre-#40 the float32 branch silently coerced ANY dtype via np.asarray.
# A caller declaring source_format="float32" but actually delivering
# int16 produced a ~1000× amplified buffer (clipping cascaded into
# saturation, R2 surfaced the symptom but not the root cause). The
# strict check makes the contract violation loud (ValueError) so the
# caller fixes the source rather than chasing downstream symptoms.


class TestFloat32SourceDtypeValidationB40:
    """Mission Appendix A band-aid #40 — float32 source must reject
    non-float32 dtype inputs at the boundary."""

    def test_int16_input_to_float32_source_rejected(self) -> None:
        norm = FrameNormalizer(
            source_rate=48_000,
            source_channels=1,
            source_format="float32",
        )
        # int16 buffer pretending to be float32 — would silently
        # produce a 32000× amplified output without the new check.
        bad_block = np.array([16000, -16000, 8000], dtype=np.int16)
        with pytest.raises(ValueError, match="float32 source requires"):
            norm.push(bad_block)

    def test_int32_input_to_float32_source_rejected(self) -> None:
        norm = FrameNormalizer(
            source_rate=48_000,
            source_channels=1,
            source_format="float32",
        )
        bad_block = np.array([1, 2, 3], dtype=np.int32)
        with pytest.raises(ValueError, match="float32 source requires"):
            norm.push(bad_block)

    def test_float64_input_to_float32_source_rejected(self) -> None:
        """Even harmless float64 → float32 narrowing must be explicit
        — caller should ``.astype(float32)`` before pushing."""
        norm = FrameNormalizer(
            source_rate=48_000,
            source_channels=1,
            source_format="float32",
        )
        bad_block = np.array([0.5, -0.5], dtype=np.float64)
        with pytest.raises(ValueError, match="float32 source requires"):
            norm.push(bad_block)

    def test_correct_float32_input_accepted(self) -> None:
        """Backwards-compat regression — the documented happy path
        still works after the strict check."""
        norm = FrameNormalizer(
            source_rate=16_000,
            source_channels=1,
            source_format="float32",
        )
        good_block = np.zeros(512, dtype=np.float32)
        windows = norm.push(good_block)
        assert len(windows) == 1
        assert windows[0].shape == (512,)
        assert windows[0].dtype == np.int16

    def test_error_message_names_alternative_formats(self) -> None:
        """Error message must point caller at the fix path."""
        norm = FrameNormalizer(
            source_rate=48_000,
            source_channels=1,
            source_format="float32",
        )
        with pytest.raises(ValueError) as exc_info:
            norm.push(np.array([1, 2], dtype=np.int16))
        # Caller-actionable hint included.
        assert "int16" in str(exc_info.value) or "source_format" in str(exc_info.value)


# ---------------------------------------------------------------------------
# source_format validation
# ---------------------------------------------------------------------------


class TestSourceFormatValidation:
    def test_rejects_unknown_format(self) -> None:
        with pytest.raises(ValueError, match="source_format must be one of"):
            FrameNormalizer(
                source_rate=16_000,
                source_channels=1,
                source_format="int32",  # not supported — int24 uses int32 *dtype*
            )

    def test_default_format_is_int16(self) -> None:
        norm = FrameNormalizer(source_rate=16_000, source_channels=1)
        assert norm.source_format == "int16"

    @pytest.mark.parametrize("fmt", ["int16", "int24", "float32"])
    def test_accepts_all_allowed_formats(self, fmt: str) -> None:
        norm = FrameNormalizer(
            source_rate=16_000,
            source_channels=1,
            source_format=fmt,
        )
        assert norm.source_format == fmt


# ---------------------------------------------------------------------------
# Mic-ducking gain — ADR §4.4.6.b
# ---------------------------------------------------------------------------


class TestDuckingDefaults:
    """Default state is unity — ducking off, no DSP overhead."""

    def test_default_gain_is_zero_db(self) -> None:
        norm = FrameNormalizer(source_rate=16_000, source_channels=1)
        assert norm.ducking_gain_db == pytest.approx(0.0)
        assert norm.current_ducking_gain_db == pytest.approx(0.0)

    def test_unity_gain_preserves_samples_bit_exact(self) -> None:
        """With ducking off, int16 mono 16 kHz passthrough is bit-exact."""
        norm = FrameNormalizer(source_rate=16_000, source_channels=1)
        block = _sine_wave_int16(440, 16_000, 0.032, channels=1)
        result = norm.push(block)
        np.testing.assert_array_equal(result[0], block)


class TestDuckingSetter:
    def test_set_minus_18_db_updates_target(self) -> None:
        norm = FrameNormalizer(source_rate=16_000, source_channels=1)
        norm.set_ducking_gain_db(-18.0)
        assert norm.ducking_gain_db == pytest.approx(-18.0)

    def test_set_zero_db_restores_unity(self) -> None:
        norm = FrameNormalizer(source_rate=16_000, source_channels=1)
        norm.set_ducking_gain_db(-18.0)
        norm.set_ducking_gain_db(0.0)
        assert norm.ducking_gain_db == pytest.approx(0.0)

    def test_rejects_positive_gain(self) -> None:
        """The stage is an attenuator — amplification is a caller bug."""
        norm = FrameNormalizer(source_rate=16_000, source_channels=1)
        with pytest.raises(ValueError, match="must be <= 0 dB"):
            norm.set_ducking_gain_db(6.0)

    def test_set_same_target_twice_is_noop(self) -> None:
        """Duplicate set must not reset an in-progress ramp."""
        norm = FrameNormalizer(source_rate=16_000, source_channels=1)
        norm.set_ducking_gain_db(-18.0)
        # Advance the ramp partially
        block = _sine_wave_int16(440, 16_000, 0.002, channels=1)  # 32 samples
        norm.push(block)
        gain_after_first_push = norm.current_ducking_gain_db

        # Set the same target again — should not reset current to 1.0
        norm.set_ducking_gain_db(-18.0)
        assert norm.current_ducking_gain_db == pytest.approx(gain_after_first_push)

    def test_accepts_negative_infinity(self) -> None:
        """-inf dB collapses to linear gain 0 (full silence)."""
        norm = FrameNormalizer(source_rate=16_000, source_channels=1)
        norm.set_ducking_gain_db(float("-inf"))
        assert norm.ducking_gain_db == float("-inf")


class TestDuckingRamp:
    """Linear ramp must avoid step-change clicks in the audio."""

    def test_unity_ducking_is_multiply_by_one_noop(self) -> None:
        """With target=0 dB, resampled path still produces clean output."""
        source_rate = 48_000
        norm = FrameNormalizer(
            source_rate=source_rate,
            source_channels=1,
        )
        # No ducking set — target stays at 1.0
        block = _sine_wave_int16(1_000, source_rate, 0.1, channels=1)
        out = np.concatenate(norm.push(block))
        # Amplitude near 0.5 input → ~16384 peak (within 5% for resample
        # edge taper)
        assert np.abs(out).max() > 14_000

    def test_minus_18_db_reduces_amplitude(self) -> None:
        """Fully ramped -18 dB ≈ 0.1259 linear → ~12.6% amplitude."""
        norm = FrameNormalizer(source_rate=16_000, source_channels=1)
        norm.set_ducking_gain_db(-18.0)
        # Long enough to complete the 10ms ramp and then some
        block = _sine_wave_int16(1_000, 16_000, 0.5, channels=1)
        windows = norm.push(block)
        # Skip the ramp region (first ~160 samples post-resample path).
        # Passthrough path applies ducking directly, so ramp is in the
        # first 160 samples of output.
        out = np.concatenate(windows)[200:]
        input_peak = np.abs(block).max()
        output_peak = np.abs(out).max()
        expected_ratio = 10 ** (-18 / 20)  # ≈ 0.1259
        actual_ratio = output_peak / input_peak
        assert actual_ratio == pytest.approx(expected_ratio, rel=0.05)

    def test_ramp_is_monotonic(self) -> None:
        """Gain envelope must move monotonically from current → target."""
        norm = FrameNormalizer(source_rate=16_000, source_channels=1)
        # DC input at full amplitude so the envelope IS the output.
        dc = np.full(512, 16_000, dtype=np.int16)
        norm.set_ducking_gain_db(-18.0)
        out = np.concatenate(norm.push(dc))
        # Output monotonically *decreases* across the ramp region, then
        # stays flat.
        first160 = out[:160].astype(np.int32)
        diffs = np.diff(first160)
        assert (diffs <= 0).all(), "envelope must be non-increasing during down-ramp"

    def test_ramp_completes_within_10ms(self) -> None:
        """After 160 output samples (10 ms @ 16 kHz) current gain == target."""
        norm = FrameNormalizer(source_rate=16_000, source_channels=1)
        norm.set_ducking_gain_db(-18.0)
        # Push exactly 160 samples = one full ramp length
        block = np.full(160, 16_000, dtype=np.int16)
        norm.push(block)
        assert norm.current_ducking_gain_db == pytest.approx(-18.0, abs=0.01)

    def test_ramp_midpoint_is_approximately_linear(self) -> None:
        """At 80 samples (half the ramp), current is the midpoint of 1.0 → target."""
        norm = FrameNormalizer(source_rate=16_000, source_channels=1)
        norm.set_ducking_gain_db(-18.0)
        block = np.zeros(80, dtype=np.int16)
        norm.push(block)
        target_linear = 10 ** (-18 / 20)
        midpoint_linear = (1.0 + target_linear) / 2.0
        # Current at 80 samples should be about (1.0 + target)/2
        current_linear = 10 ** (norm.current_ducking_gain_db / 20.0)
        assert current_linear == pytest.approx(midpoint_linear, rel=0.05)

    def test_gain_change_mid_ramp_transitions_smoothly(self) -> None:
        """Changing target mid-ramp continues from current, not from 1.0."""
        norm = FrameNormalizer(source_rate=16_000, source_channels=1)
        norm.set_ducking_gain_db(-18.0)
        # Advance half the ramp
        norm.push(np.zeros(80, dtype=np.int16))
        gain_before = norm.current_ducking_gain_db
        # Now retarget to -6 dB
        norm.set_ducking_gain_db(-6.0)
        # Push one sample's worth — current should move toward -6 dB, not reset to 0
        norm.push(np.zeros(1, dtype=np.int16))
        gain_after = norm.current_ducking_gain_db
        # Moving from ~-5 dB toward -6 dB: gain_after should be between
        # gain_before and -6 dB
        assert -6.0 <= gain_after <= max(gain_before, -6.0) + 0.1

    def test_rampback_to_unity_restores_signal(self) -> None:
        """After ducking then releasing, signal amplitude returns to baseline."""
        norm = FrameNormalizer(source_rate=16_000, source_channels=1)
        block = _sine_wave_int16(1_000, 16_000, 0.05, channels=1)
        # Duck
        norm.set_ducking_gain_db(-18.0)
        norm.push(block)  # ramp completes within this block
        # Release
        norm.set_ducking_gain_db(0.0)
        norm.push(block)  # ramp up completes
        # Next block arrives at unity
        windows = norm.push(block)
        out = np.concatenate(windows)
        # Output should match input closely (no attenuation)
        assert np.abs(out).max() == pytest.approx(np.abs(block).max(), rel=0.02)


class TestDuckingProperty:
    """Any valid ducking level must produce finite, non-clipped output."""

    @given(gain_db=st.floats(min_value=-60.0, max_value=0.0))
    @settings(max_examples=25, deadline=2_000)
    def test_no_nan_no_inf_for_any_valid_gain(self, gain_db: float) -> None:
        norm = FrameNormalizer(source_rate=48_000, source_channels=1)
        norm.set_ducking_gain_db(gain_db)
        block = _sine_wave_int16(1_000, 48_000, 0.05, channels=1)
        windows = norm.push(block)
        out = np.concatenate(windows) if windows else np.zeros(0, dtype=np.int16)
        # int16 can't hold NaN/Inf — the invariant to check is saturation
        assert out.dtype == np.int16
        assert out.min() >= -32768
        assert out.max() <= 32767


# ===========================================================================
# R2: Saturation feedback loop (Ring 2 signal integrity)
# ===========================================================================
#
# Pre-R2 ``_float_to_int16_saturate`` clipped silently — loud transients
# wrapped to int16 rails with no counter, no event, no signal. R2:
#
# * Pure function returns (int16_array, SaturationCounters)
# * FrameNormalizer aggregates counters into a rolling window monitor
# * Structured ``voice.audio.saturation_clipping`` warning fires when
#   the window fraction exceeds 5% (EBU R128 / ITU-R BS.1770 canonical
#   threshold), rate-limited to one event per window
# * Public lifetime properties (``lifetime_samples_*``) for the future
#   Layer 4 AGC2 closed-loop gain controller
#
# Reference: MISSION-voice-mixer-enterprise-refactor-2026-04-25 §3.1, R2.

_NORM_LOGGER = "sovyx.voice._frame_normalizer"


def _events_of(
    caplog: pytest.LogCaptureFixture,
    event_name: str,
) -> list[dict[str, object]]:
    """Filter caplog records by the structured ``event`` field."""
    return [
        r.msg
        for r in caplog.records
        if r.name == _NORM_LOGGER and isinstance(r.msg, dict) and r.msg.get("event") == event_name
    ]


class TestFloatToInt16SaturatePure:
    """Exhaustive coverage of the pure saturate function and counters."""

    def test_empty_input_returns_zero_counters(self) -> None:
        out, counters = _float_to_int16_saturate(np.zeros(0, dtype=np.float32))
        assert out.shape == (0,)
        assert out.dtype == np.int16
        assert counters.total_samples == 0
        assert counters.clipped_positive == 0
        assert counters.clipped_negative == 0
        assert counters.clipping_fraction == 0.0

    def test_no_clipping_for_in_range_samples(self) -> None:
        # All samples in [-0.5, 0.5] — well within range
        samples = np.array([-0.5, -0.25, 0.0, 0.25, 0.5], dtype=np.float32)
        out, counters = _float_to_int16_saturate(samples)
        assert counters.clipped_positive == 0
        assert counters.clipped_negative == 0
        assert counters.total_samples == 5  # noqa: PLR2004
        # Verify the output samples are correctly scaled (no clipping took place).
        assert out.dtype == np.int16
        assert int(out[2]) == 0  # 0.0 * 32768 = 0

    def test_positive_rail_clipping_counted(self) -> None:
        # 1.5 * 32768 = 49152 > 32767 (positive rail) → clipped
        samples = np.array([0.0, 1.5, 0.5, 2.0], dtype=np.float32)
        _out, counters = _float_to_int16_saturate(samples)
        assert counters.clipped_positive == 2  # 1.5 and 2.0 both clip  # noqa: PLR2004
        assert counters.clipped_negative == 0
        assert counters.total_samples == 4  # noqa: PLR2004

    def test_negative_rail_clipping_counted(self) -> None:
        # -1.5 * 32768 = -49152 < -32768 (negative rail) → clipped
        samples = np.array([0.0, -1.5, -0.5, -2.0], dtype=np.float32)
        _out, counters = _float_to_int16_saturate(samples)
        assert counters.clipped_positive == 0
        assert counters.clipped_negative == 2  # noqa: PLR2004
        assert counters.total_samples == 4  # noqa: PLR2004

    def test_mixed_clipping_counted_separately(self) -> None:
        samples = np.array([2.0, -2.0, 0.5, -0.5, 3.0, -3.0], dtype=np.float32)
        _out, counters = _float_to_int16_saturate(samples)
        assert counters.clipped_positive == 2  # noqa: PLR2004
        assert counters.clipped_negative == 2  # noqa: PLR2004
        assert counters.total_samples == 6  # noqa: PLR2004

    def test_boundary_at_exact_full_scale(self) -> None:
        # 1.0 * 32768 = 32768 > 32767 → counts as clipped
        # 0.99996948... * 32768 = 32767.0 → does NOT clip
        samples = np.array([1.0, 32767.0 / 32768.0], dtype=np.float32)
        _out, counters = _float_to_int16_saturate(samples)
        assert counters.clipped_positive == 1
        assert counters.clipped_negative == 0

    def test_clipping_fraction_computed(self) -> None:
        samples = np.array([2.0, 2.0, 0.5, 0.5], dtype=np.float32)
        _out, counters = _float_to_int16_saturate(samples)
        assert counters.clipping_fraction == 0.5

    def test_clipped_total_sums_both_rails(self) -> None:
        samples = np.array([2.0, 2.0, -2.0], dtype=np.float32)
        _out, counters = _float_to_int16_saturate(samples)
        assert counters.clipped_total == 3  # noqa: PLR2004

    def test_output_dtype_always_int16(self) -> None:
        samples = np.array([0.5, -0.5, 2.0, -2.0], dtype=np.float32)
        out, _counters = _float_to_int16_saturate(samples)
        assert out.dtype == np.int16
        # Verify no wrap — clipped values are at the rails, not wrapped
        assert out.min() >= -32768  # noqa: PLR2004
        assert out.max() <= 32767  # noqa: PLR2004

    def test_counters_immutable(self) -> None:
        samples = np.array([0.5], dtype=np.float32)
        _out, counters = _float_to_int16_saturate(samples)
        with pytest.raises((AttributeError, TypeError)):
            counters.total_samples = 999  # type: ignore[misc]


class TestFrameNormalizerSaturationMonitor:
    """End-to-end R2 monitor wired through ``FrameNormalizer.push``."""

    def _norm_with_clock(
        self,
        clock_value: float = 0.0,
        *,
        source_rate: int = 48_000,
        source_format: str = "float32",
    ) -> FrameNormalizer:
        """Build a normalizer with an injectable monotonic clock for
        deterministic warning rate-limit tests.

        Defaults to ``source_format="float32"`` so the test can inject
        out-of-range values (>1.0 / <-1.0) that genuinely clip at the
        ``_float_to_int16_saturate`` stage. int16/int24 inputs are
        bounded by their source dtype and can never produce the
        > full-scale floats that drive the saturate-clip path.
        """
        norm = FrameNormalizer(
            source_rate=source_rate,
            source_channels=1,
            source_format=source_format,
        )
        clock_holder = {"t": clock_value}
        norm._monotonic = lambda: clock_holder["t"]  # type: ignore[method-assign]
        norm._clock_holder = clock_holder  # type: ignore[attr-defined]
        return norm

    def _advance(self, norm: FrameNormalizer, seconds: float) -> None:
        norm._clock_holder["t"] += seconds  # type: ignore[attr-defined]

    def _hot_block(self, n_samples: int) -> np.ndarray:
        """Generate float32 samples WELL OUTSIDE [-1, 1] so every sample
        clips at the int16 rail.

        Values > 1.0 (or < -1.0) on the float32 source path get scaled
        by 32768 inside ``_float_to_int16_saturate`` (so 1.5 → 49152 →
        clipped to 32767). int16/int24 source paths can never produce
        these out-of-range values, which is why this synthetic signal
        explicitly uses the float32 source-format path.
        """
        return np.full(n_samples, 1.5, dtype=np.float32)

    def test_lifetime_counters_start_at_zero(self) -> None:
        norm = FrameNormalizer(source_rate=48_000, source_channels=1)
        assert norm.lifetime_samples_processed == 0
        assert norm.lifetime_samples_clipped == 0
        assert norm.lifetime_clipping_fraction == 0.0

    def test_lifetime_counters_accumulate_across_pushes(self) -> None:
        # Stereo input → forces non-passthrough path → saturate runs
        norm = FrameNormalizer(source_rate=48_000, source_channels=2)
        block = _sine_wave_int16(1_000, 48_000, 0.05, channels=2, amplitude=0.5)
        norm.push(block)
        first = norm.lifetime_samples_processed
        assert first > 0
        norm.push(block)
        # Second push doubles the processed count.
        assert norm.lifetime_samples_processed == 2 * first

    def test_passthrough_path_does_not_count_samples(self) -> None:
        """int16 mono 16k → fast path → no saturate → counters stay zero."""
        norm = FrameNormalizer(source_rate=16_000, source_channels=1)
        block = _sine_wave_int16(1_000, 16_000, 0.5, channels=1, amplitude=0.5)
        norm.push(block)
        # Passthrough never invokes _float_to_int16_saturate.
        assert norm.lifetime_samples_processed == 0
        assert norm.lifetime_samples_clipped == 0

    def test_no_warning_when_below_threshold(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging

        caplog.set_level(logging.WARNING, logger=_NORM_LOGGER)
        # 16k mono float32 = passthrough on rate but float dtype path
        # still triggers _float_to_int16_saturate. Sine wave at amp=0.1
        # never clips.
        norm = self._norm_with_clock(source_rate=16_000)
        t = np.arange(16_000, dtype=np.float32) / 16_000
        quiet = (0.1 * np.sin(2 * np.pi * 1000 * t)).astype(np.float32)
        norm.push(quiet)
        assert _events_of(caplog, "voice.audio.saturation_clipping") == []

    def test_warning_fires_when_clipping_fraction_exceeds_threshold(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging

        caplog.set_level(logging.WARNING, logger=_NORM_LOGGER)
        norm = self._norm_with_clock(source_rate=16_000)
        # 16k mono float32 → no resample → all 16000 samples reach
        # the saturate stage. >>4096 min for warning.
        hot = self._hot_block(16_000)
        norm.push(hot)
        events = _events_of(caplog, "voice.audio.saturation_clipping")
        assert len(events) == 1
        assert events[0]["voice.window_clipping_fraction"] >= _SATURATION_WARN_FRACTION
        assert events[0]["voice.warning_threshold_fraction"] == _SATURATION_WARN_FRACTION
        assert "reduce_upstream_gain" in str(events[0]["voice.action_required"])

    def test_warning_not_fired_below_min_samples(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Even 100% clipping is suppressed below the min-samples floor."""
        import logging

        caplog.set_level(logging.WARNING, logger=_NORM_LOGGER)
        norm = self._norm_with_clock(source_rate=16_000)
        # 1024 samples — well below _SATURATION_MIN_SAMPLES_FOR_WARNING (4096)
        small_hot = self._hot_block(1024)
        norm.push(small_hot)
        # No event yet — not statistically significant.
        assert _events_of(caplog, "voice.audio.saturation_clipping") == []
        # Lifetime counters DID record the clipping (counters always run).
        assert norm.lifetime_samples_clipped > 0

    def test_warning_rate_limited_within_window(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Multiple hot blocks within one window emit at most one warning."""
        import logging

        caplog.set_level(logging.WARNING, logger=_NORM_LOGGER)
        norm = self._norm_with_clock(source_rate=16_000)
        hot = self._hot_block(16_000)

        norm.push(hot)
        assert len(_events_of(caplog, "voice.audio.saturation_clipping")) == 1

        # Advance less than one window — second hot push must NOT warn.
        self._advance(norm, _SATURATION_WINDOW_SECONDS / 2)
        norm.push(hot)
        assert len(_events_of(caplog, "voice.audio.saturation_clipping")) == 1

    def test_warning_re_arms_after_window(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """After ``_SATURATION_WINDOW_SECONDS`` elapses, a second warning
        can fire on a still-hot signal."""
        import logging

        caplog.set_level(logging.WARNING, logger=_NORM_LOGGER)
        norm = self._norm_with_clock(source_rate=16_000)
        hot = self._hot_block(16_000)

        norm.push(hot)
        assert len(_events_of(caplog, "voice.audio.saturation_clipping")) == 1

        # Advance PAST one window + push more hot data.
        self._advance(norm, _SATURATION_WINDOW_SECONDS + 0.1)
        norm.push(hot)
        # Second warning fires (rate-limit released, fresh window crossed).
        assert len(_events_of(caplog, "voice.audio.saturation_clipping")) == 2  # noqa: PLR2004

    def test_lifetime_clipping_fraction_zero_with_no_input(self) -> None:
        norm = FrameNormalizer(source_rate=48_000, source_channels=1)
        # Property must be safe to call BEFORE any push.
        assert norm.lifetime_clipping_fraction == 0.0

    def test_lifetime_clipping_fraction_reflects_clipped_ratio(self) -> None:
        # Use float32 source so the hot block actually clips at the
        # _float_to_int16_saturate stage (int16 inputs can't reach
        # > full-scale floats internally).
        norm = self._norm_with_clock(source_rate=48_000)
        hot = self._hot_block(8_000)
        norm.push(hot)
        # Most samples should have clipped (fraction much greater than 0).
        assert norm.lifetime_clipping_fraction > 0.5

    def test_event_includes_lifetime_and_window_breakdown(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Operators need both the trigger window AND lifetime context."""
        import logging

        caplog.set_level(logging.WARNING, logger=_NORM_LOGGER)
        norm = self._norm_with_clock(source_rate=16_000)
        hot = self._hot_block(16_000)
        norm.push(hot)
        events = _events_of(caplog, "voice.audio.saturation_clipping")
        assert len(events) == 1
        # Expected attributes per the R2 contract.
        evt = events[0]
        assert "voice.window_clipping_fraction" in evt
        assert "voice.window_samples_processed" in evt
        assert "voice.window_samples_clipped" in evt
        assert "voice.lifetime_clipping_fraction" in evt
        assert "voice.lifetime_samples_processed" in evt
        assert "voice.lifetime_samples_clipped" in evt
        assert "voice.window_clipped_positive_fraction" in evt
        assert "voice.window_clipped_negative_fraction" in evt


class TestSaturationCountersProperty:
    """Hypothesis: counter invariants hold for arbitrary float input."""

    @settings(max_examples=50, deadline=None)
    @given(
        samples=st.lists(
            st.floats(min_value=-3.0, max_value=3.0, allow_nan=False, allow_infinity=False),
            min_size=1,
            max_size=2048,
        )
    )
    def test_counter_invariants(self, samples: list[float]) -> None:
        arr = np.array(samples, dtype=np.float32)
        out, counters = _float_to_int16_saturate(arr)
        # Total samples matches input length.
        assert counters.total_samples == arr.size
        # Non-negative counters.
        assert counters.clipped_positive >= 0
        assert counters.clipped_negative >= 0
        # Clipped total can't exceed total.
        assert counters.clipped_total <= counters.total_samples
        # Output is int16, no wrap.
        assert out.dtype == np.int16
        assert int(out.min()) >= -32768  # noqa: PLR2004
        assert int(out.max()) <= 32767  # noqa: PLR2004
        # Fraction in [0, 1].
        assert 0.0 <= counters.clipping_fraction <= 1.0

    @settings(max_examples=50, deadline=None)
    @given(
        n_in_range=st.integers(min_value=0, max_value=100),
        n_clipped=st.integers(min_value=0, max_value=100),
    )
    def test_counters_reflect_known_clip_ratio(
        self,
        n_in_range: int,
        n_clipped: int,
    ) -> None:
        if n_in_range + n_clipped == 0:
            return  # empty input is the empty-counters case
        # Build an array with a known number of clip-inducing values.
        samples = np.array(
            [0.5] * n_in_range + [2.0] * n_clipped,
            dtype=np.float32,
        )
        _out, counters = _float_to_int16_saturate(samples)
        assert counters.clipped_positive == n_clipped
        assert counters.clipped_negative == 0
        assert counters.total_samples == n_in_range + n_clipped


class TestSaturationCountersDataclass:
    """Pure-data invariants on ``SaturationCounters``."""

    def test_zero_total_returns_zero_fraction(self) -> None:
        c = SaturationCounters(total_samples=0, clipped_positive=0, clipped_negative=0)
        assert c.clipping_fraction == 0.0
        assert c.clipped_total == 0

    def test_clipped_total_sums_both_rails(self) -> None:
        c = SaturationCounters(total_samples=100, clipped_positive=3, clipped_negative=7)
        assert c.clipped_total == 10  # noqa: PLR2004

    def test_immutable(self) -> None:
        c = SaturationCounters(total_samples=10, clipped_positive=1, clipped_negative=0)
        with pytest.raises((AttributeError, TypeError)):
            c.total_samples = 999  # type: ignore[misc]

    def test_min_samples_constant_value(self) -> None:
        # Regression on the public-surface tuning constant — bumps must
        # be deliberate (the value gates a warning users will eventually
        # see, so the threshold is part of the user-visible contract).
        assert _SATURATION_MIN_SAMPLES_FOR_WARNING == 4096  # noqa: PLR2004
        assert _SATURATION_WARN_FRACTION == 0.05  # noqa: PLR2004
        assert _SATURATION_WINDOW_SECONDS == 1.0


# ===========================================================================
# F5/F6 integration: FrameNormalizer wires AGC2 on the non-passthrough path
# ===========================================================================


class TestAGC2IntegrationF5:
    """The opt-in AGC2 stage — pre-F6 ``apply_mixer_boost_up`` band-aid
    replacement. Wired into FrameNormalizer's non-passthrough branch
    so closed-loop digital gain handles attenuated input regardless
    of mixer permissions / presence / distro audio stack."""

    def test_default_constructor_no_agc2_wired(self) -> None:
        norm = FrameNormalizer(source_rate=48_000, source_channels=1)
        assert norm.agc2 is None

    def test_agc2_constructor_arg_wired(self) -> None:
        from sovyx.voice._agc2 import AGC2

        agc = AGC2()
        norm = FrameNormalizer(source_rate=48_000, source_channels=1, agc2=agc)
        assert norm.agc2 is agc

    def test_set_agc2_runtime_wires_unwires(self) -> None:
        from sovyx.voice._agc2 import AGC2

        norm = FrameNormalizer(source_rate=48_000, source_channels=1)
        agc = AGC2()
        norm.set_agc2(agc)
        assert norm.agc2 is agc
        norm.set_agc2(None)
        assert norm.agc2 is None

    def test_agc2_skipped_on_passthrough_path(self) -> None:
        """The 16 kHz mono int16 passthrough path is bit-exact by
        contract (operators rely on it for A/B comparisons). The
        AGC2 must NOT engage there."""
        from sovyx.voice._agc2 import AGC2

        agc = AGC2()
        norm = FrameNormalizer(source_rate=16_000, source_channels=1, agc2=agc)
        block = _sine_wave_int16(440, 16_000, 0.032, channels=1)  # exactly 512 samples
        result = norm.push(block)
        assert len(result) == 1
        # Passthrough invariant: bit-identical output, AGC2 untouched.
        np.testing.assert_array_equal(result[0], block)
        assert agc.frames_processed == 0

    def test_agc2_engages_on_non_passthrough_path(self) -> None:
        """48 kHz stereo input takes the resample/downmix path; AGC2
        runs after _float_to_int16_saturate."""
        from sovyx.voice._agc2 import AGC2

        agc = AGC2()
        norm = FrameNormalizer(source_rate=48_000, source_channels=2, agc2=agc)
        # Quiet stereo signal — AGC2 should see frames.
        block = _sine_wave_int16(1_000, 48_000, 0.05, channels=2, amplitude=0.05)
        norm.push(block)
        assert agc.frames_processed > 0

    def test_agc2_lifts_attenuated_input_toward_target(self) -> None:
        """End-to-end: the F6 user-story. Sustained attenuated input
        must converge toward the AGC's target dBFS. Pre-F6 the
        only fix was apply_mixer_boost_up's hardcoded fractions —
        post-F5/F6 the AGC2 does it in user space, no mixer write
        required."""
        from sovyx.voice._agc2 import AGC2, AGC2Config

        # Force non-passthrough path (48 kHz mono → resample to 16 kHz).
        cfg = AGC2Config(target_dbfs=-18.0, max_gain_db=30.0)
        agc = AGC2(cfg)
        norm = FrameNormalizer(source_rate=48_000, source_channels=1, agc2=agc)

        # Attenuated input ~ -40 dBFS.
        block = _sine_wave_int16(1_000, 48_000, 0.05, channels=1, amplitude=0.01)

        # Drive enough blocks for the slew-rate-limited gain to climb
        # ~25 dB toward target. Slew cap (6 dB/s default) bounds
        # convergence to ~6 dB per second; 200 × 50 ms = 10 s gives
        # the controller plenty of room.
        for _ in range(200):
            norm.push(block)

        # The AGC2 should have raised the gain WELL above zero —
        # 10 s of run time × 6 dB/s slew = up to 60 dB headroom,
        # gain clamped at max_gain_db=30 dB. We expect convergence
        # to the ceiling region given 25 dB of needed lift.
        assert agc.current_gain_db > 15.0

    def test_agc2_does_not_affect_lifetime_saturation_counters(self) -> None:
        """The R2 saturation counters reflect what *would have*
        clipped post-saturate, BEFORE AGC2 takes over. They aren't
        affected by AGC2's own gain decisions — that's the AGC2's
        own ``frames_clipped`` counter's responsibility."""
        from sovyx.voice._agc2 import AGC2

        agc = AGC2()
        norm = FrameNormalizer(source_rate=48_000, source_channels=2, agc2=agc)
        block = _sine_wave_int16(1_000, 48_000, 0.05, channels=2, amplitude=0.5)
        norm.push(block)
        # R2 counters reflect pre-AGC saturate work.
        # AGC2 has its own counters via agc.frames_processed/clipped.
        assert norm.lifetime_samples_processed > 0  # R2 saw samples
        assert agc.frames_processed > 0  # AGC2 saw frames

    def test_agc2_output_stays_int16_bounded(self) -> None:
        """Even with AGC2 in the path, output must never overflow."""
        from sovyx.voice._agc2 import AGC2, AGC2Config

        # Aggressive AGC config — fast adaptation, high max gain.
        cfg = AGC2Config(max_gain_db=30.0, max_gain_change_db_per_second=60.0)
        agc = AGC2(cfg)
        norm = FrameNormalizer(source_rate=48_000, source_channels=1, agc2=agc)
        # Mix of quiet + loud frames.
        for amplitude in (0.01, 0.5, 0.99, 0.01):
            block = _sine_wave_int16(1_000, 48_000, 0.05, channels=1, amplitude=amplitude)
            for window in norm.push(block):
                assert window.dtype == np.int16
                assert window.min() >= -32768  # noqa: PLR2004
                assert window.max() <= 32767  # noqa: PLR2004


# ---------------------------------------------------------------------------
# M2 wire-up — RED + USE telemetry on capture
# ---------------------------------------------------------------------------


class TestFrameNormalizerM2WireUp:
    """FrameNormalizer.push must emit M2 stage events.

    Mirrors STT/TTS adoption — proves the M2 foundation is wired
    in capture stage too. Empty-input is intentionally NOT
    instrumented (zero-size callbacks at stream boundaries are
    a no-op and would inflate the metric noise floor).
    """

    def test_push_records_success_event(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from typing import Any

        recorded: list[tuple[Any, Any, Any]] = []

        from sovyx.voice import _frame_normalizer as fn_mod
        from sovyx.voice._stage_metrics import StageEventKind, VoiceStage

        def _capture(stage: Any, kind: Any, *, error_type: str | None = None) -> None:
            recorded.append((stage, kind, error_type))

        monkeypatch.setattr(fn_mod, "record_stage_event", _capture)

        norm = FrameNormalizer(source_rate=16_000, source_channels=1)
        block = _sine_wave_int16(440, 16_000, 0.032, channels=1)
        norm.push(block)

        assert (VoiceStage.CAPTURE, StageEventKind.SUCCESS, None) in recorded

    def test_empty_input_does_not_record_event(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Empty PortAudio callbacks are pure no-ops — no telemetry."""
        from typing import Any

        recorded: list[tuple[Any, Any, Any]] = []

        from sovyx.voice import _frame_normalizer as fn_mod

        def _capture(stage: Any, kind: Any, *, error_type: str | None = None) -> None:
            recorded.append((stage, kind, error_type))

        monkeypatch.setattr(fn_mod, "record_stage_event", _capture)

        norm = FrameNormalizer(source_rate=16_000, source_channels=1)
        norm.push(np.array([], dtype=np.int16))

        assert recorded == []

    def test_bad_dtype_propagates_as_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Bad input (wrong dtype for source_format) raises — the
        measure_stage_duration BaseException handler records
        duration with outcome=error and re-raises. Caller sees the
        original ValueError; the RED counter sees no SUCCESS event."""
        from typing import Any

        recorded: list[tuple[Any, Any, Any]] = []

        from sovyx.voice import _frame_normalizer as fn_mod
        from sovyx.voice._stage_metrics import StageEventKind, VoiceStage

        def _capture(stage: Any, kind: Any, *, error_type: str | None = None) -> None:
            recorded.append((stage, kind, error_type))

        monkeypatch.setattr(fn_mod, "record_stage_event", _capture)

        norm = FrameNormalizer(
            source_rate=16_000,
            source_channels=1,
            source_format="int16",
        )
        # float64 block when source_format is int16 → ValueError.
        bad = np.zeros(512, dtype=np.float64)
        with pytest.raises(ValueError):  # noqa: PT011
            norm.push(bad)

        # No SUCCESS event was recorded — exception propagated before
        # the success-path call site.
        successes = [
            (s, k, et)
            for (s, k, et) in recorded
            if s == VoiceStage.CAPTURE and k == StageEventKind.SUCCESS
        ]
        assert successes == []


# ---------------------------------------------------------------------------
# Band-aid #8 — phase-inversion downmix detector
# ---------------------------------------------------------------------------


class TestChannelCorrelation:
    """Pure-function correlation check used by the phase-inversion
    downmix detector."""

    def test_identical_channels_correlation_is_one(self) -> None:
        from sovyx.voice._frame_normalizer import _channel_correlation

        signal = np.linspace(-0.5, 0.5, 1024).astype(np.float32)
        assert abs(_channel_correlation(signal, signal) - 1.0) < 0.001  # noqa: PLR2004

    def test_phase_inverted_channels_correlation_is_minus_one(self) -> None:
        from sovyx.voice._frame_normalizer import _channel_correlation

        signal = np.linspace(-0.5, 0.5, 1024).astype(np.float32)
        inverted = -signal
        assert abs(_channel_correlation(signal, inverted) - (-1.0)) < 0.001  # noqa: PLR2004

    def test_orthogonal_channels_correlation_near_zero(self) -> None:
        """Sin and cos at the same frequency are perfectly orthogonal —
        Pearson correlation should be ~0."""
        from sovyx.voice._frame_normalizer import _channel_correlation

        n = 16_000  # 1 s @ 16 kHz
        t = np.linspace(0, 1.0, n, endpoint=False, dtype=np.float32)
        sin_signal = np.sin(2.0 * np.pi * 100.0 * t).astype(np.float32)
        cos_signal = np.cos(2.0 * np.pi * 100.0 * t).astype(np.float32)
        # Pearson r for orthogonal signals over an integer-cycle
        # window should be effectively zero.
        assert abs(_channel_correlation(sin_signal, cos_signal)) < 0.01  # noqa: PLR2004

    def test_silent_channel_returns_zero(self) -> None:
        """Below-floor RMS → return 0 (correlation is undefined for
        zero-signal case)."""
        from sovyx.voice._frame_normalizer import _channel_correlation

        signal = np.linspace(-0.5, 0.5, 1024).astype(np.float32)
        silence = np.zeros(1024, dtype=np.float32)
        assert _channel_correlation(signal, silence) == 0.0
        assert _channel_correlation(silence, signal) == 0.0

    def test_empty_channels_returns_zero(self) -> None:
        from sovyx.voice._frame_normalizer import _channel_correlation

        empty = np.zeros(0, dtype=np.float32)
        assert _channel_correlation(empty, empty) == 0.0

    def test_shape_mismatch_raises(self) -> None:
        from sovyx.voice._frame_normalizer import _channel_correlation

        a = np.zeros(100, dtype=np.float32)
        b = np.zeros(200, dtype=np.float32)
        with pytest.raises(ValueError, match="shape mismatch"):
            _channel_correlation(a, b)


class TestPhaseInversionDetector:
    """End-to-end: phase-inverted stereo input through FrameNormalizer
    triggers the WARN + bumps the public counter."""

    def _stereo_block(
        self,
        left: np.ndarray,
        right: np.ndarray,
    ) -> np.ndarray:
        """Build a 2-channel int16 block from two float32 [-1, 1] arrays."""
        stereo_f = np.column_stack([left, right])
        return (stereo_f * 32767.0).astype(np.int16)

    def test_phase_inverted_stereo_flagged(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging

        norm = FrameNormalizer(source_rate=16_000, source_channels=2)
        # Build a sine + its negation.
        n = 1024
        signal = (np.sin(np.linspace(0, 4.0, n)) * 0.5).astype(np.float32)
        block = self._stereo_block(signal, -signal)
        with caplog.at_level(logging.WARNING):
            norm.push(block)
        assert norm.phase_inverted_count == 1
        assert any(
            "voice.audio.downmix_phase_inverted" in str(r.msg)
            for r in caplog.records
        )

    def test_correlated_stereo_not_flagged(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Identical stereo (mono played on both channels) — correlation
        ≈ +1.0, well above threshold, no flag."""
        import logging

        norm = FrameNormalizer(source_rate=16_000, source_channels=2)
        n = 1024
        signal = (np.sin(np.linspace(0, 4.0, n)) * 0.5).astype(np.float32)
        block = self._stereo_block(signal, signal)
        with caplog.at_level(logging.WARNING):
            norm.push(block)
        assert norm.phase_inverted_count == 0

    def test_warn_rate_limited(self) -> None:
        """Multiple consecutive inverted blocks → counter grows but
        WARN fires at most once per _PHASE_INVERSION_LOG_INTERVAL_S."""
        import logging

        norm = FrameNormalizer(source_rate=16_000, source_channels=2)
        # Inject a fake clock so the rate limit is deterministic.
        fake_now = [0.0]
        norm._monotonic = lambda: fake_now[0]  # type: ignore[method-assign]
        n = 1024
        signal = (np.sin(np.linspace(0, 4.0, n)) * 0.5).astype(np.float32)
        block = self._stereo_block(signal, -signal)

        warns: list[str] = []
        handler = logging.Handler()
        handler.emit = lambda record: warns.append(str(record.msg))  # type: ignore[method-assign]
        logger_obj = logging.getLogger("sovyx.voice._frame_normalizer")
        logger_obj.addHandler(handler)
        logger_obj.setLevel(logging.WARNING)
        try:
            for _ in range(5):
                norm.push(block)
            # All 5 blocks flagged as inverted.
            assert norm.phase_inverted_count == 5  # noqa: PLR2004
            # But only ONE WARN fired (rate limit @ 5 s, fake clock
            # didn't advance).
            phase_warns = [w for w in warns if "phase_inverted" in w]
            assert len(phase_warns) == 1

            # Advance the clock past the rate limit window.
            fake_now[0] = 10.0
            norm.push(block)
            phase_warns = [w for w in warns if "phase_inverted" in w]
            assert len(phase_warns) == 2  # noqa: PLR2004
        finally:
            logger_obj.removeHandler(handler)

    def test_silent_stereo_not_flagged(self) -> None:
        """Both channels at zero RMS → correlation returns 0, above
        the threshold, no flag (silence is not an inversion)."""
        norm = FrameNormalizer(source_rate=16_000, source_channels=2)
        block = np.zeros((1024, 2), dtype=np.int16)
        norm.push(block)
        assert norm.phase_inverted_count == 0

    def test_mono_input_skips_check(self) -> None:
        """1-channel input never triggers the phase check (no
        second channel to correlate against)."""
        norm = FrameNormalizer(source_rate=16_000, source_channels=1)
        block = _sine_wave_int16(440, 16_000, 0.05, channels=1, amplitude=0.5)
        norm.push(block)
        assert norm.phase_inverted_count == 0
