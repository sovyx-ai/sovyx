"""Deterministic synthetic audio fixtures for voice tests (TS1).

Pure-NumPy generators producing reproducible int16 PCM mono streams
for unit tests, property tests, and (future) integration tests
that replay through the voice pipeline without depending on a
real microphone or network.

Five families:

* :func:`silence` — exact-zero array (regression baseline for
  silence-floor gates).
* :func:`sine_tone` — single-frequency tone (the canonical AGC2 /
  resampler input).
* :func:`frequency_sweep` — exponential chirp across a frequency
  band (DSP linearity + resampler aliasing tests).
* :func:`white_noise` — band-flat random samples seeded from a
  caller-supplied integer (deterministic across runs).
* :func:`pink_noise` — 1/f-shaped noise via FFT spectral shaping
  (perceptually closer to room noise than white noise; exercises
  VAD + STT silence-vs-speech discrimination).
* :func:`speech_envelope_burst` — sinusoidal carrier modulated by
  a Hann envelope (synthetic "syllable" — deterministic stand-in
  for a real utterance when the test cares about energy
  envelope shape, not transcript content).
* :func:`clipping_burst` — int16 rail train (saturation-protector
  + R2 saturation-monitor stress).

Plus utilities:

* :func:`encode_wav_bytes` — wrap a sample buffer in a RIFF WAV
  container so tests can hand bytes directly to a STT mock or a
  WAV-file consumer.
* :func:`mix` — additive overlay of two equal-shape buffers with
  per-source gain (build "speech over noise" composites).
* :func:`apply_fade` — Hann fade-in / fade-out envelope (smooth
  test edges so a transient pop doesn't bias VAD).

All generators return ``np.ndarray[shape=(n_samples,), dtype=int16]``
so they drop directly into the existing voice helpers (FrameNormalizer
expects int16 mono unless ``source_format`` is overridden, AGC2
expects int16, the WAV encoder expects int16).

Reference: MISSION-voice-mixer-enterprise-refactor-2026-04-25 §6
TS1; FFT-shaped pink noise (canonical 1/sqrt(f) magnitude
weighting); WAV / RIFF format (docs.python.org/3/library/wave.html).
"""

from __future__ import annotations

import io
import math
import wave
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    import numpy.typing as npt


# ── Constants ──────────────────────────────────────────────────────


_INT16_MAX = 32767
_INT16_MIN = -32768
_DEFAULT_SAMPLE_RATE_HZ = 16_000
_NYQUIST_GUARD_HZ = 50.0
"""Don't generate tones within 50 Hz of either Nyquist edge — the
sine becomes indistinguishable from a square wave at that point and
the dB analysis tests get confused. Loud-fail above Nyquist."""


# ── Bound enforcement ─────────────────────────────────────────────


def _validate_sample_rate(sample_rate: int) -> None:
    if not (8_000 <= sample_rate <= 96_000):
        msg = (
            f"sample_rate must be in [8000, 96000], got {sample_rate} "
            f"(Sovyx voice pipeline operates in this range)"
        )
        raise ValueError(msg)


def _validate_duration(duration_s: float) -> None:
    if duration_s <= 0:
        msg = f"duration_s must be > 0, got {duration_s}"
        raise ValueError(msg)
    if duration_s > 600:
        # 10 minutes is a soft sanity ceiling — anything longer than
        # this in a unit/property test is almost certainly a bug
        # rather than a legitimate fixture (the soak suite uses a
        # different fixture path).
        msg = f"duration_s must be <= 600 (10 min), got {duration_s}"
        raise ValueError(msg)


def _validate_amplitude(amplitude: float) -> None:
    if not (0.0 <= amplitude <= 1.0):
        msg = f"amplitude must be in [0, 1] (normalised int16 fraction), got {amplitude}"
        raise ValueError(msg)


def _validate_frequency(freq_hz: float, sample_rate: int) -> None:
    nyquist = sample_rate / 2.0
    if freq_hz <= 0:
        msg = f"freq_hz must be > 0, got {freq_hz}"
        raise ValueError(msg)
    if freq_hz >= nyquist - _NYQUIST_GUARD_HZ:
        msg = (
            f"freq_hz must be < Nyquist - {_NYQUIST_GUARD_HZ} Hz "
            f"({nyquist - _NYQUIST_GUARD_HZ}), got {freq_hz}"
        )
        raise ValueError(msg)


def _n_samples(duration_s: float, sample_rate: int) -> int:
    return int(round(duration_s * sample_rate))


def _normalise_int16(samples: np.ndarray, amplitude: float) -> npt.NDArray[np.int16]:
    """Scale a normalised float32 buffer to int16, clipping to rails.

    ``samples`` must be in ``[-1.0, 1.0]``; ``amplitude`` scales it
    further. The clip is final defence against floating-point round
    pushing a value past the rail.
    """
    scaled = samples.astype(np.float32) * amplitude * _INT16_MAX
    clipped = np.clip(scaled, _INT16_MIN, _INT16_MAX)
    out: npt.NDArray[np.int16] = clipped.astype(np.int16)
    return out


# ── Generators ────────────────────────────────────────────────────


def silence(
    duration_s: float,
    sample_rate: int = _DEFAULT_SAMPLE_RATE_HZ,
) -> npt.NDArray[np.int16]:
    """Return a zero-filled int16 buffer.

    Used by tests that need a known-quiet baseline (silence-floor
    gates, VAD non-speech path, AGC2 estimator-freeze invariant).
    """
    _validate_sample_rate(sample_rate)
    _validate_duration(duration_s)
    n = _n_samples(duration_s, sample_rate)
    return np.zeros(n, dtype=np.int16)


def sine_tone(
    freq_hz: float,
    duration_s: float,
    *,
    amplitude: float = 0.5,
    sample_rate: int = _DEFAULT_SAMPLE_RATE_HZ,
    phase_rad: float = 0.0,
) -> npt.NDArray[np.int16]:
    """Pure sine wave at ``freq_hz`` for ``duration_s``.

    ``amplitude`` is the normalised int16 fraction (0–1); 0.5 is a
    comfortable test default well below the rail. ``phase_rad``
    seeds the starting phase — set to a non-zero value if you need
    two adjacent buffers to splice without a discontinuity.
    """
    _validate_sample_rate(sample_rate)
    _validate_duration(duration_s)
    _validate_amplitude(amplitude)
    _validate_frequency(freq_hz, sample_rate)
    n = _n_samples(duration_s, sample_rate)
    t = np.arange(n, dtype=np.float64) / sample_rate
    wave_norm = np.sin(2.0 * math.pi * freq_hz * t + phase_rad)
    return _normalise_int16(wave_norm, amplitude)


def frequency_sweep(
    start_hz: float,
    end_hz: float,
    duration_s: float,
    *,
    amplitude: float = 0.5,
    sample_rate: int = _DEFAULT_SAMPLE_RATE_HZ,
    method: str = "logarithmic",
) -> npt.NDArray[np.int16]:
    """Exponential or linear chirp from ``start_hz`` to ``end_hz``.

    ``method="logarithmic"`` (default) uses an exponential frequency
    sweep — the canonical DSP / resampler test signal because each
    octave gets equal time, exposing aliasing across the spectrum.
    ``method="linear"`` uses a linear frequency ramp (less common,
    sometimes useful for checking specific bandlimits).
    """
    _validate_sample_rate(sample_rate)
    _validate_duration(duration_s)
    _validate_amplitude(amplitude)
    _validate_frequency(start_hz, sample_rate)
    _validate_frequency(end_hz, sample_rate)
    if method not in {"logarithmic", "linear"}:
        msg = f"method must be 'logarithmic' or 'linear', got {method!r}"
        raise ValueError(msg)
    n = _n_samples(duration_s, sample_rate)
    t = np.arange(n, dtype=np.float64) / sample_rate
    if method == "linear":
        # Phase φ(t) = 2π·∫f(τ)dτ where f(τ) = start + k·τ.
        k = (end_hz - start_hz) / duration_s
        phase = 2.0 * math.pi * (start_hz * t + 0.5 * k * t * t)
    else:
        # Exponential: f(τ) = start·(end/start)^(τ/duration).
        # ∫f(τ)dτ from 0 to t = start·duration / ln(end/start) · ((end/start)^(t/duration) - 1).
        ratio = end_hz / start_hz
        if ratio == 1.0:
            phase = 2.0 * math.pi * start_hz * t
        else:
            ln_ratio = math.log(ratio)
            phase = (
                2.0
                * math.pi
                * start_hz
                * duration_s
                / ln_ratio
                * (np.power(ratio, t / duration_s) - 1.0)
            )
    wave_norm = np.sin(phase)
    return _normalise_int16(wave_norm, amplitude)


def white_noise(
    duration_s: float,
    *,
    amplitude: float = 0.1,
    sample_rate: int = _DEFAULT_SAMPLE_RATE_HZ,
    seed: int = 0,
) -> npt.NDArray[np.int16]:
    """Deterministic white noise seeded from ``seed``.

    Defaults to ``amplitude=0.1`` (≈ -20 dBFS RMS) — louder amplitudes
    exercise saturation/clipping paths but skew tests that assume the
    noise floor sits well below typical speech.
    """
    _validate_sample_rate(sample_rate)
    _validate_duration(duration_s)
    _validate_amplitude(amplitude)
    n = _n_samples(duration_s, sample_rate)
    rng = np.random.default_rng(seed)
    samples = rng.uniform(-1.0, 1.0, size=n)
    return _normalise_int16(samples, amplitude)


def pink_noise(
    duration_s: float,
    *,
    amplitude: float = 0.1,
    sample_rate: int = _DEFAULT_SAMPLE_RATE_HZ,
    seed: int = 0,
) -> npt.NDArray[np.int16]:
    """1/f noise via FFT spectral shaping.

    Generates white Gaussian noise, applies a 1/sqrt(f) magnitude
    response in the frequency domain, then inverse-transforms.
    Mathematically guaranteed to have a power spectrum proportional
    to 1/f (i.e. -3 dB per octave) — the canonical "pink" colour.

    More numerically faithful than the Voss-McCartney sample-and-hold
    approximation for spectral-content tests; both produce
    perceptually similar noise but the FFT-shaped variant has the
    cleaner spectral guarantee that downstream tests can rely on.
    """
    _validate_sample_rate(sample_rate)
    _validate_duration(duration_s)
    _validate_amplitude(amplitude)
    n = _n_samples(duration_s, sample_rate)
    rng = np.random.default_rng(seed)
    white = rng.standard_normal(n)
    spectrum = np.fft.rfft(white)
    freqs = np.fft.rfftfreq(n, d=1.0 / sample_rate)
    # DC bin avoids div-by-zero by clamping to the next bin's frequency
    # (preserves the bin's contribution; the resulting DC level is
    # negligible after peak normalisation).
    freqs_safe = freqs.copy()
    freqs_safe[0] = freqs[1] if freqs.size > 1 else 1.0
    pink_spectrum = spectrum / np.sqrt(freqs_safe)
    pink = np.fft.irfft(pink_spectrum, n=n)
    # Normalise to [-1, 1] before applying caller's amplitude.
    peak = float(np.max(np.abs(pink)))
    if peak > 0:
        pink = pink / peak
    return _normalise_int16(pink, amplitude)


def speech_envelope_burst(
    carrier_hz: float = 220.0,
    burst_duration_s: float = 0.4,
    *,
    amplitude: float = 0.5,
    sample_rate: int = _DEFAULT_SAMPLE_RATE_HZ,
    leading_silence_s: float = 0.1,
    trailing_silence_s: float = 0.1,
) -> npt.NDArray[np.int16]:
    """Sinusoidal carrier shaped by a Hann envelope (synthetic syllable).

    Useful when the test cares about energy envelope shape (VAD
    onset/offset, AGC2 attack/release behaviour) rather than
    transcript content. The default 220 Hz carrier sits in the male
    fundamental band; the Hann envelope ramps cleanly so VAD
    edge-detection has a real onset to chew on.
    """
    _validate_sample_rate(sample_rate)
    _validate_duration(burst_duration_s)
    _validate_amplitude(amplitude)
    _validate_frequency(carrier_hz, sample_rate)
    if leading_silence_s < 0 or trailing_silence_s < 0:
        msg = "leading/trailing silence must be >= 0"
        raise ValueError(msg)
    n_burst = _n_samples(burst_duration_s, sample_rate)
    t = np.arange(n_burst, dtype=np.float64) / sample_rate
    carrier = np.sin(2.0 * math.pi * carrier_hz * t)
    envelope = np.hanning(n_burst)
    burst = carrier * envelope
    burst_int16 = _normalise_int16(burst, amplitude)
    parts = []
    if leading_silence_s > 0:
        parts.append(silence(leading_silence_s, sample_rate))
    parts.append(burst_int16)
    if trailing_silence_s > 0:
        parts.append(silence(trailing_silence_s, sample_rate))
    return np.concatenate(parts)


def clipping_burst(
    duration_s: float,
    *,
    sample_rate: int = _DEFAULT_SAMPLE_RATE_HZ,
    polarity: str = "positive",
) -> npt.NDArray[np.int16]:
    """Constant-rail burst — int16_max / int16_min train.

    Used by saturation tests (R2 monitor, AGC2 anti-clip protector)
    that need to exercise the clip-counting path. ``polarity`` can
    be ``"positive"``, ``"negative"``, or ``"alternating"`` (one
    sample at each rail per cycle).
    """
    _validate_sample_rate(sample_rate)
    _validate_duration(duration_s)
    if polarity not in {"positive", "negative", "alternating"}:
        msg = f"polarity must be 'positive', 'negative', or 'alternating', got {polarity!r}"
        raise ValueError(msg)
    n = _n_samples(duration_s, sample_rate)
    if polarity == "positive":
        return np.full(n, _INT16_MAX, dtype=np.int16)
    if polarity == "negative":
        return np.full(n, _INT16_MIN, dtype=np.int16)
    out = np.empty(n, dtype=np.int16)
    out[0::2] = _INT16_MAX
    out[1::2] = _INT16_MIN
    return out


# ── Composition utilities ──────────────────────────────────────────


def mix(
    a: np.ndarray,
    b: np.ndarray,
    *,
    gain_a: float = 1.0,
    gain_b: float = 1.0,
) -> npt.NDArray[np.int16]:
    """Sample-additive mix of two equal-length int16 buffers.

    Output is clipped to int16 rails; both inputs must have the
    same length and dtype. Per-source gain lets callers build
    composites like "speech at 0 dB + noise at -20 dB" without
    re-rendering each source.
    """
    if a.shape != b.shape:
        msg = f"shapes must match, got a={a.shape} b={b.shape}"
        raise ValueError(msg)
    if a.dtype != np.int16 or b.dtype != np.int16:
        msg = f"both inputs must be int16, got a={a.dtype} b={b.dtype}"
        raise ValueError(msg)
    summed = a.astype(np.int32) * gain_a + b.astype(np.int32) * gain_b
    clipped = np.clip(summed, _INT16_MIN, _INT16_MAX)
    return clipped.astype(np.int16)


def apply_fade(
    samples: np.ndarray,
    *,
    fade_in_s: float = 0.0,
    fade_out_s: float = 0.0,
    sample_rate: int = _DEFAULT_SAMPLE_RATE_HZ,
) -> npt.NDArray[np.int16]:
    """Hann fade-in / fade-out envelope on the buffer edges.

    Smooths discontinuities so a clip at t=0 doesn't manifest as a
    DC pop in tests that pipe the buffer through a high-pass filter.
    """
    if samples.dtype != np.int16:
        msg = f"samples must be int16, got {samples.dtype}"
        raise ValueError(msg)
    if fade_in_s < 0 or fade_out_s < 0:
        msg = "fade durations must be >= 0"
        raise ValueError(msg)
    n_in = _n_samples(fade_in_s, sample_rate) if fade_in_s > 0 else 0
    n_out = _n_samples(fade_out_s, sample_rate) if fade_out_s > 0 else 0
    if n_in + n_out > samples.size:
        msg = (
            f"fade durations exceed buffer length: "
            f"n_in={n_in} + n_out={n_out} > samples.size={samples.size}"
        )
        raise ValueError(msg)
    out = samples.astype(np.float64).copy()
    if n_in > 0:
        # Hann-window leading half rises 0 → 1 over n_in samples.
        env = 0.5 * (1.0 - np.cos(np.pi * np.arange(n_in) / n_in))
        out[:n_in] *= env
    if n_out > 0:
        env = 0.5 * (1.0 - np.cos(np.pi * np.arange(n_out) / n_out))
        out[-n_out:] *= env[::-1]
    return np.clip(out, _INT16_MIN, _INT16_MAX).astype(np.int16)


def encode_wav_bytes(
    samples: np.ndarray,
    *,
    sample_rate: int = _DEFAULT_SAMPLE_RATE_HZ,
) -> bytes:
    """Wrap an int16 mono buffer in a RIFF WAV container.

    Used by tests that need actual on-the-wire WAV bytes — STT
    fakes, dashboard upload paths, regression of the WAV decoder.
    Pure stdlib (``wave`` module), no soundfile dependency.
    """
    _validate_sample_rate(sample_rate)
    if samples.dtype != np.int16:
        msg = f"samples must be int16, got {samples.dtype}"
        raise ValueError(msg)
    if samples.ndim != 1:
        msg = f"samples must be 1-D mono, got shape {samples.shape}"
        raise ValueError(msg)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)  # int16 = 2 bytes
        wav.setframerate(sample_rate)
        wav.writeframes(samples.tobytes())
    return buf.getvalue()


__all__ = [
    "apply_fade",
    "clipping_burst",
    "encode_wav_bytes",
    "frequency_sweep",
    "mix",
    "pink_noise",
    "silence",
    "sine_tone",
    "speech_envelope_burst",
    "white_noise",
]
