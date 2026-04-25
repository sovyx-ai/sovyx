"""Tests for :mod:`tests._voice_audio_fixtures` (TS1).

Validates the synthetic-audio fixtures themselves so consumers
(TS4 property tests, TS2 integration tests, future soak tests) can
trust the corpus.

Reference: MISSION-voice-mixer-enterprise-refactor-2026-04-25 §6 TS1.
"""

from __future__ import annotations

import io
import math
import wave

import numpy as np
import pytest

from tests._voice_audio_fixtures import (
    apply_fade,
    clipping_burst,
    encode_wav_bytes,
    frequency_sweep,
    mix,
    pink_noise,
    silence,
    sine_tone,
    speech_envelope_burst,
    white_noise,
)

# ── silence ────────────────────────────────────────────────────────


class TestSilence:
    def test_returns_zero_array(self) -> None:
        s = silence(0.5, sample_rate=16_000)
        assert s.dtype == np.int16
        assert s.shape == (8_000,)
        assert np.all(s == 0)

    def test_invalid_duration_rejected(self) -> None:
        with pytest.raises(ValueError, match="duration_s must be > 0"):
            silence(0.0)

    def test_invalid_sample_rate_rejected(self) -> None:
        with pytest.raises(ValueError, match="sample_rate must be"):
            silence(0.5, sample_rate=1_000)


# ── sine_tone ──────────────────────────────────────────────────────


class TestSineTone:
    def test_correct_length(self) -> None:
        s = sine_tone(440.0, 1.0, sample_rate=16_000)
        assert s.shape == (16_000,)
        assert s.dtype == np.int16

    def test_amplitude_bounded(self) -> None:
        s = sine_tone(440.0, 0.1, amplitude=0.5)
        # Amplitude 0.5 → peak ≈ 16_383.5; allow rounding slack.
        peak = int(np.max(np.abs(s)))
        assert 16_000 <= peak <= 17_000

    def test_zero_dc_offset(self) -> None:
        s = sine_tone(100.0, 1.0, amplitude=0.5)
        # Mean of a full-period sine is zero (within int16 round-off).
        assert abs(int(np.mean(s.astype(np.int64)))) < 5

    def test_phase_changes_initial_value(self) -> None:
        a = sine_tone(440.0, 0.1, amplitude=0.5, phase_rad=0.0)
        b = sine_tone(440.0, 0.1, amplitude=0.5, phase_rad=math.pi / 2)
        # Phase π/2 starts at peak instead of zero.
        assert int(b[0]) > int(a[0])

    def test_freq_at_nyquist_rejected(self) -> None:
        with pytest.raises(ValueError, match="freq_hz must be"):
            sine_tone(8_000.0, 0.5, sample_rate=16_000)

    def test_amplitude_above_1_rejected(self) -> None:
        with pytest.raises(ValueError, match="amplitude must be"):
            sine_tone(440.0, 0.5, amplitude=1.5)


# ── frequency_sweep ────────────────────────────────────────────────


class TestFrequencySweep:
    def test_logarithmic_default(self) -> None:
        s = frequency_sweep(100.0, 4_000.0, 1.0, sample_rate=16_000)
        assert s.shape == (16_000,)
        assert s.dtype == np.int16

    def test_linear_method(self) -> None:
        s = frequency_sweep(100.0, 4_000.0, 1.0, method="linear")
        assert s.shape[0] == 16_000

    def test_invalid_method_rejected(self) -> None:
        with pytest.raises(ValueError, match="method must be"):
            frequency_sweep(100.0, 4_000.0, 1.0, method="quadratic")

    def test_equal_start_end_logarithmic(self) -> None:
        """Edge case — start == end should produce a constant tone."""
        s = frequency_sweep(440.0, 440.0, 0.5, method="logarithmic")
        # No log(1) divide-by-zero crash.
        assert s.shape[0] == 8_000


# ── white_noise ────────────────────────────────────────────────────


class TestWhiteNoise:
    def test_deterministic_for_same_seed(self) -> None:
        a = white_noise(0.5, seed=42)
        b = white_noise(0.5, seed=42)
        assert np.array_equal(a, b)

    def test_different_seed_different_output(self) -> None:
        a = white_noise(0.5, seed=1)
        b = white_noise(0.5, seed=2)
        assert not np.array_equal(a, b)

    def test_amplitude_bounded(self) -> None:
        s = white_noise(0.5, amplitude=0.1, seed=0)
        peak = int(np.max(np.abs(s)))
        # Amplitude 0.1 → max possible peak 3_276; allow rounding.
        assert peak <= 3_300


# ── pink_noise ────────────────────────────────────────────────────


class TestPinkNoise:
    def test_deterministic_for_same_seed(self) -> None:
        a = pink_noise(0.5, seed=7)
        b = pink_noise(0.5, seed=7)
        assert np.array_equal(a, b)

    def test_correct_length(self) -> None:
        s = pink_noise(0.5, sample_rate=16_000)
        assert s.shape == (8_000,)

    def test_per_bin_power_density_decreases_with_frequency(self) -> None:
        """Pink-noise spectral test — per-bin POWER DENSITY should
        decrease with frequency (1/f shape).

        Comparing summed magnitude across bands is misleading: a
        wider high band can sum to more even for pink noise. The
        right comparison is *power density per bin* (mean(|X|²) per
        bin) — for pink noise this scales as 1/f.
        """
        s = pink_noise(2.0, amplitude=0.5, seed=0, sample_rate=16_000)
        spectrum = np.abs(np.fft.rfft(s.astype(np.float64)))
        power = spectrum**2
        bin_hz = 16_000 / s.size
        low_density = power[int(200 / bin_hz) : int(400 / bin_hz)].mean()
        high_density = power[int(2_000 / bin_hz) : int(4_000 / bin_hz)].mean()
        # Pink should have ≥ 5× more power-per-bin at low f than at high f
        # (theoretical ratio ~10×, allow slack for finite-length variance
        # and int16 quantisation).
        assert low_density >= 5.0 * high_density

    def test_pink_distinguishable_from_white(self) -> None:
        """Compare pink vs white noise spectra — pink must have a
        distinctly higher low/high ratio than white."""
        n_seconds = 2.0
        sr = 16_000

        def low_high_ratio(buf: np.ndarray) -> float:
            spectrum = np.abs(np.fft.rfft(buf.astype(np.float64)))
            power = spectrum**2
            bin_hz = sr / buf.size
            low = power[int(200 / bin_hz) : int(400 / bin_hz)].mean()
            high = power[int(2_000 / bin_hz) : int(4_000 / bin_hz)].mean()
            return float(low / high) if high > 0 else float("inf")

        from tests._voice_audio_fixtures import white_noise as wn

        pink = pink_noise(n_seconds, amplitude=0.5, seed=0, sample_rate=sr)
        white = wn(n_seconds, amplitude=0.5, seed=0, sample_rate=sr)
        # Pink ratio expected ~10, white ratio expected ~1.
        assert low_high_ratio(pink) > 3.0 * low_high_ratio(white)


# ── speech_envelope_burst ──────────────────────────────────────────


class TestSpeechEnvelopeBurst:
    def test_includes_silence_padding(self) -> None:
        s = speech_envelope_burst(
            burst_duration_s=0.2,
            leading_silence_s=0.1,
            trailing_silence_s=0.1,
            sample_rate=16_000,
        )
        # 0.1 + 0.2 + 0.1 = 0.4 s = 6_400 samples.
        assert s.shape == (6_400,)

    def test_envelope_starts_and_ends_at_zero(self) -> None:
        s = speech_envelope_burst(
            burst_duration_s=0.2,
            leading_silence_s=0.0,
            trailing_silence_s=0.0,
        )
        # Hann envelope → first + last sample == 0.
        assert s[0] == 0
        assert s[-1] == 0

    def test_negative_padding_rejected(self) -> None:
        with pytest.raises(ValueError, match="leading/trailing"):
            speech_envelope_burst(leading_silence_s=-0.1)


# ── clipping_burst ─────────────────────────────────────────────────


class TestClippingBurst:
    def test_positive_polarity(self) -> None:
        s = clipping_burst(0.1, polarity="positive")
        assert np.all(s == 32767)

    def test_negative_polarity(self) -> None:
        s = clipping_burst(0.1, polarity="negative")
        assert np.all(s == -32768)

    def test_alternating_polarity(self) -> None:
        s = clipping_burst(0.001, polarity="alternating", sample_rate=16_000)
        # Even indices = max, odd = min.
        assert s[0] == 32767
        assert s[1] == -32768

    def test_invalid_polarity_rejected(self) -> None:
        with pytest.raises(ValueError, match="polarity must be"):
            clipping_burst(0.1, polarity="random")


# ── mix ────────────────────────────────────────────────────────────


class TestMix:
    def test_additive_overlay(self) -> None:
        a = sine_tone(440.0, 0.1, amplitude=0.2)
        b = sine_tone(880.0, 0.1, amplitude=0.2)
        out = mix(a, b)
        assert out.shape == a.shape
        assert out.dtype == np.int16

    def test_gain_attenuates(self) -> None:
        a = sine_tone(440.0, 0.1, amplitude=0.5)
        b = silence(0.1)
        out = mix(a, b, gain_a=0.5)
        # Output peak ≈ half input peak.
        assert int(np.max(np.abs(out))) <= int(np.max(np.abs(a))) // 2 + 5

    def test_clip_to_int16_rails(self) -> None:
        """When sum exceeds rail, clip — don't wrap."""
        a = clipping_burst(0.01, polarity="positive")
        b = clipping_burst(0.01, polarity="positive")
        out = mix(a, b)
        # 32767 + 32767 = 65534 → clipped to 32767, not int16-overflow-wrapped.
        assert int(np.max(out)) == 32767

    def test_shape_mismatch_rejected(self) -> None:
        a = sine_tone(440.0, 0.1)
        b = sine_tone(440.0, 0.2)
        with pytest.raises(ValueError, match="shapes must match"):
            mix(a, b)

    def test_dtype_mismatch_rejected(self) -> None:
        a = sine_tone(440.0, 0.1)
        b = a.astype(np.int32)
        with pytest.raises(ValueError, match="must be int16"):
            mix(a, b)


# ── apply_fade ─────────────────────────────────────────────────────


class TestApplyFade:
    def test_fade_in_starts_at_zero(self) -> None:
        s = sine_tone(440.0, 0.5, amplitude=0.5)
        out = apply_fade(s, fade_in_s=0.05, sample_rate=16_000)
        assert out[0] == 0

    def test_fade_out_ends_at_zero(self) -> None:
        s = sine_tone(440.0, 0.5, amplitude=0.5)
        out = apply_fade(s, fade_out_s=0.05, sample_rate=16_000)
        assert out[-1] == 0

    def test_fade_longer_than_buffer_rejected(self) -> None:
        s = sine_tone(440.0, 0.1, sample_rate=16_000)
        with pytest.raises(ValueError, match="fade durations exceed"):
            apply_fade(s, fade_in_s=0.1, fade_out_s=0.1)

    def test_dtype_validation(self) -> None:
        s = np.zeros(1_000, dtype=np.int32)
        with pytest.raises(ValueError, match="must be int16"):
            apply_fade(s)


# ── encode_wav_bytes ───────────────────────────────────────────────


class TestEncodeWavBytes:
    def test_produces_valid_wav(self) -> None:
        s = sine_tone(440.0, 0.1, sample_rate=16_000)
        b = encode_wav_bytes(s, sample_rate=16_000)
        # Round-trip via stdlib wave.
        with wave.open(io.BytesIO(b), "rb") as wav:
            assert wav.getnchannels() == 1
            assert wav.getsampwidth() == 2
            assert wav.getframerate() == 16_000
            assert wav.getnframes() == s.size
            frames = wav.readframes(wav.getnframes())
            decoded = np.frombuffer(frames, dtype=np.int16)
            assert np.array_equal(decoded, s)

    def test_riff_header(self) -> None:
        s = sine_tone(440.0, 0.05)
        b = encode_wav_bytes(s)
        assert b[:4] == b"RIFF"
        assert b[8:12] == b"WAVE"

    def test_non_int16_rejected(self) -> None:
        s = np.zeros(100, dtype=np.float32)
        with pytest.raises(ValueError, match="must be int16"):
            encode_wav_bytes(s)

    def test_multi_dim_rejected(self) -> None:
        s = np.zeros((100, 2), dtype=np.int16)
        with pytest.raises(ValueError, match="must be 1-D mono"):
            encode_wav_bytes(s)


# ── Integration smoke ──────────────────────────────────────────────


class TestComposability:
    """Fixtures should compose into realistic test signals."""

    def test_speech_over_noise_composite(self) -> None:
        """Build the canonical 'speech at 0 dB + noise at -20 dB'."""
        speech = speech_envelope_burst(
            carrier_hz=220.0,
            burst_duration_s=0.5,
            amplitude=0.5,
            leading_silence_s=0.0,
            trailing_silence_s=0.0,
            sample_rate=16_000,
        )
        noise = white_noise(
            duration_s=0.5,
            amplitude=0.05,
            sample_rate=16_000,
            seed=42,
        )
        composite = mix(speech, noise)
        assert composite.shape == speech.shape
        assert composite.dtype == np.int16
        # Composite RMS ≥ speech RMS (additive energy).
        speech_rms = float(np.sqrt(np.mean(speech.astype(np.float64) ** 2)))
        composite_rms = float(np.sqrt(np.mean(composite.astype(np.float64) ** 2)))
        assert composite_rms >= speech_rms * 0.99  # tolerance for cancellation
