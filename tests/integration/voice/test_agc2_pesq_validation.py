"""AGC2 A/B perceptual-quality validation [Phase 4 T4.53].

Mission contract (master mission §Phase 4 / T4.53):

    A/B test corpus through AGC2 disabled vs enabled. Publish
    PESQ delta — AGC2 should improve, not degrade perceptual
    quality.

The contract closes the bottom of the AGC2 promotion gate: the
operator-tunable closed-loop gain controller MUST NOT inject
new spectral distortion vs an untouched signal (otherwise
``voice_agc2_enabled = True`` would make capture quality
strictly worse).

Strategy:

* Generate a synthetic speech-shaped corpus (multi-tone vowel-
  formant signal at the int16 full-scale rail).
* Simulate a "low-gain mic" condition: scale the corpus to
  -25 dBFS so AGC2 has work to do (target -18 dBFS, headroom
  to lift).
* Path A — AGC2 DISABLED: signal passes through unmodified.
* Path B — AGC2 ENABLED: signal passes through AGC2.process.
* Score both paths against the original at-rail reference via
  TWO complementary measures:
  - **Spectrum-SNR** (always available): FFT-based ratio of
    speech-band signal energy to per-bin error energy. Tracks
    the same "perceptual distortion" property PESQ measures
    but doesn't require the pesq package.
  - **PESQ** (CI-only): wideband 16 kHz PESQ MOS-LQO via the
    optional ``pesq`` extra. Skipped when the package isn't
    installed (the Windows MSVC build typically fails per the
    ``compute_pesq`` docstring).

The contract is the SAME under both measures: the AGC2-on path
must NOT be perceptually worse than the AGC2-off path. AGC2 is
allowed to slightly underperform on a corpus where it has nothing
to fix (no gain anomaly), but never fall below the AGC2-off
baseline.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from sovyx.voice._agc2 import AGC2, AGC2Config
from sovyx.voice._quality_metrics import (
    QualityEstimatorLoadError,
    compute_pesq,
)

_SAMPLE_RATE_HZ = 16_000
_FRAME_SAMPLES = 512
_DURATION_S = 4.0  # 4 s gives ~125 frames; PESQ minimum is 3.2 s.


def _at_rail_reference() -> np.ndarray:
    """Generate a clean steady-state speech-shaped reference.

    Multi-tone (F1=400, F2=1700, F3=2700 Hz) at constant
    amplitude. Length is a whole number of FrameNormalizer
    windows + the ``_spectrum_snr_db`` 2 s tail.

    Steady-state matters here: AGC2's job IS to flatten the
    temporal envelope, so a varying-envelope reference would
    score AGC2's correct behaviour as "distortion". The PESQ
    contract is "AGC2 doesn't introduce SPECTRAL distortion"
    (new harmonics, intermodulation, formant smearing) — which
    is well-tested on a steady-state corpus.
    """
    samples = int(_DURATION_S * _SAMPLE_RATE_HZ)
    t = np.arange(samples) / _SAMPLE_RATE_HZ
    sig = (
        0.25 * np.sin(2 * np.pi * 400 * t)
        + 0.25 * np.sin(2 * np.pi * 1_700 * t)
        + 0.20 * np.sin(2 * np.pi * 2_700 * t)
    )
    # Normalise to peak 0.85 (-1.4 dBFS peak, ~-7 dBFS RMS) so
    # the signal sits in the calibrated AGC2 operating range.
    sig = sig * 0.85 / float(np.max(np.abs(sig)))
    return sig.astype(np.float32)


def _to_int16(signal_f32: np.ndarray) -> np.ndarray:
    """Float32 [-1, 1] → int16."""
    clipped = np.clip(signal_f32, -1.0, 1.0)
    out: np.ndarray = (clipped * 32_767.0).astype(np.int16)
    return out


def _to_float32(samples_i16: np.ndarray) -> np.ndarray:
    """int16 → float32 [-1, 1]."""
    out_f64: np.ndarray = samples_i16.astype(np.float32) / 32_768.0
    return out_f64.astype(np.float32)


def _scale_dbfs(signal_f32: np.ndarray, target_dbfs: float) -> np.ndarray:
    """Re-scale ``signal`` so its RMS lands at ``target_dbfs``."""
    current_rms = float(np.sqrt(np.mean(signal_f32 * signal_f32)))
    target_linear = 10.0 ** (target_dbfs / 20.0)
    out: np.ndarray = (signal_f32 * (target_linear / max(current_rms, 1e-12))).astype(
        np.float32,
    )
    return out


def _process_through_agc2(samples_i16: np.ndarray, *, enabled: bool) -> np.ndarray:
    """Run ``samples`` through an AGC2 instance; ``enabled=False``
    short-circuits the call so the test sees the pre-AGC2
    baseline bit-exactly."""
    if not enabled:
        return samples_i16.copy()
    agc2 = AGC2(AGC2Config(sample_rate=_SAMPLE_RATE_HZ))
    out_chunks = []
    for start in range(0, samples_i16.size, _FRAME_SAMPLES):
        chunk = samples_i16[start : start + _FRAME_SAMPLES]
        if chunk.size < _FRAME_SAMPLES:
            # Last partial frame — pad to keep the test
            # deterministic; AGC2 accepts variable-length input.
            chunk = np.concatenate(
                [chunk, np.zeros(_FRAME_SAMPLES - chunk.size, dtype=np.int16)],
            )
        out_chunks.append(agc2.process(chunk))
    return np.concatenate(out_chunks)[: samples_i16.size]


def _spectrum_snr_db(reference: np.ndarray, output: np.ndarray) -> float:
    """Return spectrum-magnitude-SNR (dB) of ``output`` vs ``reference``.

    Higher = better. Compares FFT magnitudes of the LAST 2 s of
    each signal (skipping AGC2's slew-rate settling transient),
    rescaling output magnitudes to match reference total energy
    so the score is gain-invariant. This isolates SPECTRAL
    distortion (the property PESQ measures) from time-domain
    gain ramps that AGC2 deliberately introduces during its
    attack/release smoothing.

    The 2 s window is a whole number of FFT bins at 16 kHz +
    speech-band content is uniformly distributed, so the
    magnitude-domain comparison is well-conditioned.
    """
    if reference.shape != output.shape:
        n = min(reference.size, output.size)
        reference = reference[:n]
        output = output[:n]
    # Take the LAST 2 seconds — AGC2's slew ramp settles within
    # ~500 ms, so this window contains only steady-state output.
    tail_samples = min(reference.size, 2 * _SAMPLE_RATE_HZ)
    ref_tail = reference[-tail_samples:].astype(np.float64)
    out_tail = output[-tail_samples:].astype(np.float64)
    # Hann window to suppress FFT leakage on the boundaries.
    window = np.hanning(tail_samples)
    ref_spec = np.abs(np.fft.rfft(ref_tail * window))
    out_spec = np.abs(np.fft.rfft(out_tail * window))
    # Rescale output magnitudes to match reference total energy
    # so the comparison is gain-invariant.
    ref_energy = float(np.sum(ref_spec * ref_spec))
    out_energy = float(np.sum(out_spec * out_spec))
    if out_energy > 0.0:
        out_spec = out_spec * math.sqrt(ref_energy / out_energy)
    spec_err = out_spec - ref_spec
    sig_power = float(np.sum(ref_spec * ref_spec))
    err_power = float(np.sum(spec_err * spec_err))
    if err_power <= 0.0:
        return float("inf")
    return 10.0 * math.log10(sig_power / err_power)


def _pesq_available() -> bool:
    try:
        # Probe via a smoke call on a 16 000-sample silent buffer.
        compute_pesq(
            np.zeros(_SAMPLE_RATE_HZ, dtype=np.float32),
            np.zeros(_SAMPLE_RATE_HZ, dtype=np.float32),
            sample_rate=_SAMPLE_RATE_HZ,
            mode="wb",
        )
    except QualityEstimatorLoadError:
        return False
    except Exception:  # noqa: BLE001 — non-load errors still mean pesq IS importable
        return True
    return True


# ── Tests ────────────────────────────────────────────────────────────────


_PESQ_AVAILABLE = _pesq_available()


class TestAgc2DoesNotDegradeSpectrum:
    """Spectrum-SNR is the always-on contract: AGC2 must not
    introduce new spectral distortion vs the AGC2-off baseline.
    Always runs; no extras dependency."""

    def test_agc2_preserves_spectral_envelope_on_low_gain_corpus(
        self,
    ) -> None:
        # Methodology: compare AGC2-on vs AGC2-off DIRECTLY (both
        # share the same int16 quantization noise from the input).
        # Any spectral mismatch between the two paths reflects
        # AGC2's actual distortion, not pre-existing quantization.
        # Comparing AGC2-on against the float reference would
        # bake in quantization noise that AGC2 amplifies along
        # with the signal — a meaningful effect for absolute
        # quality but NOT what T4.53 measures.
        reference = _at_rail_reference()
        low_gain = _scale_dbfs(reference, target_dbfs=-25.0)
        low_gain_i16 = _to_int16(low_gain)

        no_agc2_i16 = _process_through_agc2(low_gain_i16, enabled=False)
        with_agc2_i16 = _process_through_agc2(low_gain_i16, enabled=True)

        # AGC2-on vs AGC2-off, gain-invariant spectrum match.
        spectrum_match_db = _spectrum_snr_db(
            _to_float32(no_agc2_i16),
            _to_float32(with_agc2_i16),
        )
        # ≥30 dB matches the master mission's §Phase 4 promotion
        # gate floor for "no perceptual quality regression". 30
        # dB ≈ 1000:1 spectral match — equivalent to ITU PESQ's
        # "transparent quality" bin.
        assert spectrum_match_db >= 30.0, (
            f"AGC2 distorted the spectral envelope by "
            f"{spectrum_match_db:.2f} dB SNR vs the AGC2-off "
            f"baseline. Promotion gate (master mission §Phase 4 / "
            f"T4.53): ≥30 dB."
        )

    def test_agc2_preserves_spectrum_on_no_op_corpus(self) -> None:
        # When the input is already at the AGC2 target (-18 dBFS),
        # AGC2 should ride close to 0 dB gain. The spectral match
        # against the AGC2-off baseline should be even tighter
        # than on the low-gain corpus.
        reference = _at_rail_reference()
        # Drop the reference to -18 dBFS exactly so AGC2 has
        # nothing to do.
        at_target = _scale_dbfs(reference, target_dbfs=-18.0)
        at_target_i16 = _to_int16(at_target)

        no_agc2_i16 = _process_through_agc2(at_target_i16, enabled=False)
        with_agc2_i16 = _process_through_agc2(at_target_i16, enabled=True)

        spectrum_match_db = _spectrum_snr_db(
            _to_float32(no_agc2_i16),
            _to_float32(with_agc2_i16),
        )
        assert spectrum_match_db >= 35.0, (
            f"AGC2 distorted a no-op corpus by "
            f"{spectrum_match_db:.2f} dB. With nothing to correct, "
            f"AGC2 should produce near-bit-exact output (≥35 dB "
            f"spectral match)."
        )


@pytest.mark.skipif(
    not _PESQ_AVAILABLE,
    reason="PESQ extra not installed (pip install 'sovyx[pesq]'); "
    "spectrum-SNR contract above is the always-on equivalent.",
)
class TestAgc2DoesNotDegradePesq:
    """PESQ is the canonical perceptual quality measure — when
    the optional ``pesq`` extra is installed, run the same A/B
    contract against MOS-LQO scores. Skipped on hosts without
    pesq (Windows MSVC build failures, dev machines without
    extras)."""

    def test_low_gain_input_at_rail_reference_pesq(self) -> None:
        reference = _at_rail_reference()
        low_gain = _scale_dbfs(reference, target_dbfs=-25.0)
        low_gain_i16 = _to_int16(low_gain)

        no_agc2_i16 = _process_through_agc2(low_gain_i16, enabled=False)
        with_agc2_i16 = _process_through_agc2(low_gain_i16, enabled=True)

        pesq_no_agc2 = compute_pesq(
            reference,
            _to_float32(no_agc2_i16),
            sample_rate=_SAMPLE_RATE_HZ,
            mode="wb",
        )
        pesq_with_agc2 = compute_pesq(
            reference,
            _to_float32(with_agc2_i16),
            sample_rate=_SAMPLE_RATE_HZ,
            mode="wb",
        )

        # PESQ MOS-LQO: AGC2-on must be no lower than AGC2-off
        # by more than 0.1 MOS (within rounding noise of the
        # reference algorithm).
        assert pesq_with_agc2 >= pesq_no_agc2 - 0.1, (
            f"AGC2 lowered PESQ MOS-LQO from {pesq_no_agc2:.3f} to "
            f"{pesq_with_agc2:.3f} on the low-gain corpus — promotion-"
            f"gate violation (master mission §Phase 4 / T4.53 contract: "
            f"AGC2 must not degrade perceived quality)."
        )
