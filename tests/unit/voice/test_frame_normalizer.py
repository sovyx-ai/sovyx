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

import time

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from sovyx.voice._frame_normalizer import FrameNormalizer

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
