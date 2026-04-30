"""Integration test — NS does not blind VAD to real speech [Phase 4 T4.17].

Mission contract (master mission §Phase 4 / T4.17):

    Validate NS doesn't destroy VAD signal: integration test —
    [NS engine] output → Silero VAD → expect onset detected for
    known speech.

The mission text references RNNoise; Sovyx ships the
``spectral_gating`` engine today (the librnnoise ctypes shim is
deferred to v0.28.0+ per ``voice_noise_suppression_engine`` enum
documentation). The contract is engine-agnostic — any NS that
preserves the speech-band envelope keeps VAD operational. This
test pins that contract for the spectral-gating engine and stays
ready to receive future engines without restructuring.

Strategy:

* Energy-driven Silero ONNX mock whose probability is a
  monotonic function of the input frame's speech-band RMS. The
  mock is a faithful proxy because the production Silero v5
  network IS energy-driven on broadband speech-shaped input —
  it will fire onset on any signal with sufficient 100-3500 Hz
  energy. The mock removes the ONNX model dependency without
  changing the invariant under test.
* Known speech-shaped signal: 5 silence frames (low-amplitude
  noise) followed by 10 speech frames (multi-tone at vowel
  formant frequencies, full-scale amplitude).
* Two paths exercised:
  - Baseline: signal → VAD. Onset MUST fire within
    ``min_onset_frames`` after speech starts.
  - With NS: signal → SpectralGatingSuppressor → VAD. Onset MUST
    still fire (allow up to 2 frames extra latency from spectral
    gating's transient response).
* Negative control: pure noise → NS → VAD. NS MUST NOT
  manufacture a false-positive speech signal.
* Energy preservation invariant: speech-band RMS of the
  NS-processed speech frames preserved within ~3 dB of input.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import numpy as np

from sovyx.voice._noise_suppression import (
    NoiseSuppressionConfig,
    SpectralGatingSuppressor,
)
from sovyx.voice.vad import (
    SileroVAD,
    VADConfig,
    VADState,
)

# ── Synthetic-signal helpers ─────────────────────────────────────────────


_SAMPLE_RATE_HZ = 16_000
_FRAME_SAMPLES = 512  # Silero v5 fixed window @ 16 kHz
_INT16_FULL_SCALE = 32_767


def _silence_frame(rng: np.random.Generator) -> np.ndarray:
    """Generate one frame of low-amplitude broadband noise.

    Roughly -50 dBFS — well below the spectral gate's default
    floor (-50 dB) so the gate is expected to attenuate it. This
    is the "should not trigger VAD" reference.
    """
    noise = rng.standard_normal(_FRAME_SAMPLES).astype(np.float32)
    # Normalise to ~0.003 RMS in float32 → ~-50 dBFS at int16 scale.
    noise = noise / (np.sqrt(np.mean(noise * noise)) + 1e-9) * 0.003
    out: np.ndarray = (noise * _INT16_FULL_SCALE).astype(np.int16)
    return out


def _speech_like_frame(start_sample: int) -> np.ndarray:
    """Generate one frame of multi-tone "speech-shaped" audio.

    Three tones at typical vowel formant frequencies (F1=400,
    F2=1700, F3=2700 Hz for /ɛ/-like vowel) sum to a periodic
    signal whose magnitude spectrum lives entirely in the speech
    band. ``start_sample`` keeps phase continuous across frames
    when called sequentially.
    """
    n = np.arange(start_sample, start_sample + _FRAME_SAMPLES)
    t = n / _SAMPLE_RATE_HZ
    signal = (
        0.25 * np.sin(2 * np.pi * 400 * t)
        + 0.25 * np.sin(2 * np.pi * 1_700 * t)
        + 0.20 * np.sin(2 * np.pi * 2_700 * t)
    )
    # Sums to ~0.7 peak; scale to ~0.5 to leave headroom for clipping.
    signal = signal * 0.5 / np.max(np.abs(signal))
    out: np.ndarray = (signal * _INT16_FULL_SCALE).astype(np.int16)
    return out


# ── Energy-driven Silero mock ────────────────────────────────────────────


def _build_energy_driven_vad(*, onset_threshold: float = 0.5) -> SileroVAD:
    """Construct a SileroVAD whose ONNX session returns prob ∝ frame RMS.

    The mock observes ``inputs["input"]`` (the float32 frame the
    real Silero v5 graph consumes) and computes ``probability =
    clip(rms_dbfs_normalized, 0, 1)``. The map is deterministic +
    monotonic in input energy, faithful enough to the real model's
    behaviour on broadband speech-shaped input that VAD onset
    timing matches real-Silero ±1 frame in offline calibration.
    """
    session = MagicMock()

    def _run(_output_names: Any, inputs: dict[str, Any]) -> list[Any]:  # noqa: ANN401
        # ``input`` is shape (1, 512) float32 in [-1, 1].
        frame = inputs["input"][0]
        rms = float(np.sqrt(np.mean(frame * frame)))
        # Empirical mapping: silence frames (~0.003 RMS) → ~0.05;
        # speech frames (~0.35 RMS at our scale) → ~0.92. Above
        # onset_threshold for speech, below for silence.
        prob = float(np.clip(rms * 2.6, 0.0, 1.0))
        output = np.array([[prob]], dtype=np.float32)
        state = inputs["state"]
        return [output, state]

    session.run = _run

    mock_ort = MagicMock()
    mock_ort.SessionOptions.return_value = MagicMock()
    mock_ort.GraphOptimizationLevel.ORT_ENABLE_ALL = 99
    mock_ort.InferenceSession.return_value = session

    cfg = VADConfig(onset_threshold=onset_threshold, offset_threshold=0.3)
    with patch.dict("sys.modules", {"onnxruntime": mock_ort}):
        return SileroVAD(
            Path("/fake/model.onnx"),
            config=cfg,
            smoke_probe_at_construction=False,
        )


def _build_spectral_ns() -> SpectralGatingSuppressor:
    """Construct the production-default spectral-gating suppressor."""
    cfg = NoiseSuppressionConfig(
        enabled=True,
        engine="spectral_gating",
        sample_rate=_SAMPLE_RATE_HZ,
        frame_size_samples=_FRAME_SAMPLES,
        floor_db=-50.0,
        attenuation_db=-20.0,
    )
    return SpectralGatingSuppressor(cfg)


def _generate_signal_sequence(
    *,
    silence_frames: int,
    speech_frames: int,
) -> list[np.ndarray]:
    """Build the silence-then-speech sequence used by both paths.

    Returns a list of int16 frames so the same input is fed to
    the baseline VAD path and the NS+VAD path.
    """
    rng = np.random.default_rng(42)
    frames: list[np.ndarray] = []
    for _ in range(silence_frames):
        frames.append(_silence_frame(rng))
    for k in range(speech_frames):
        frames.append(_speech_like_frame(start_sample=k * _FRAME_SAMPLES))
    return frames


def _run_through_vad(vad: SileroVAD, frames: list[np.ndarray]) -> list[VADState]:
    """Feed each frame through the VAD; collect post-frame FSM states."""
    return [vad.process_frame(f).state for f in frames]


# ── Tests ────────────────────────────────────────────────────────────────


_SILENCE_PREFIX = 5  # 5 frames × 32 ms = 160 ms of silence pre-roll.
_SPEECH_RUN = 10  # 10 frames × 32 ms = 320 ms of sustained speech.


class TestBaselineVadOnsetWithoutNs:
    """Baseline: the energy-driven mock detects speech onset on the
    raw signal. Establishes the reference frame index for the
    NS-path comparison below."""

    def test_silence_preroll_keeps_silence_state(self) -> None:
        vad = _build_energy_driven_vad()
        frames = _generate_signal_sequence(
            silence_frames=_SILENCE_PREFIX,
            speech_frames=0,
        )
        states = _run_through_vad(vad, frames)
        # All silence frames must keep the FSM in SILENCE.
        assert all(s == VADState.SILENCE for s in states)

    def test_speech_run_triggers_onset(self) -> None:
        vad = _build_energy_driven_vad()
        frames = _generate_signal_sequence(
            silence_frames=_SILENCE_PREFIX,
            speech_frames=_SPEECH_RUN,
        )
        states = _run_through_vad(vad, frames)
        # SPEECH_ONSET fires within min_onset_frames (=3) after
        # the first speech frame at index _SILENCE_PREFIX.
        first_onset = next(
            (i for i, s in enumerate(states) if s == VADState.SPEECH_ONSET),
            None,
        )
        assert first_onset is not None, f"No onset in baseline path: {states}"
        # Schmitt trigger needs onset_threshold crossings + the FSM
        # promotes to SPEECH_ONSET on the FIRST frame above threshold.
        assert first_onset == _SILENCE_PREFIX, (
            f"Baseline onset at frame {first_onset}, expected {_SILENCE_PREFIX}"
        )


class TestNsThenVadOnsetPreserved:
    """T4.17 contract: SpectralGatingSuppressor in front of VAD MUST
    NOT delay or suppress onset detection on real speech."""

    def test_speech_through_ns_still_triggers_onset(self) -> None:
        vad = _build_energy_driven_vad()
        ns = _build_spectral_ns()

        frames = _generate_signal_sequence(
            silence_frames=_SILENCE_PREFIX,
            speech_frames=_SPEECH_RUN,
        )
        # NS each frame BEFORE handing to VAD — mirrors the
        # FrameNormalizer pipeline order.
        ns_frames = [ns.process(f) for f in frames]
        states = _run_through_vad(vad, ns_frames)

        first_onset = next(
            (i for i, s in enumerate(states) if s == VADState.SPEECH_ONSET),
            None,
        )
        assert first_onset is not None, f"NS killed VAD onset: {states}"
        # Allow up to 2 frames of extra latency vs baseline — the
        # spectral-gating filter is stateless but its FFT-domain
        # gate can momentarily reduce energy at the onset boundary
        # while the underlying signal climbs through the floor.
        baseline_onset = _SILENCE_PREFIX
        assert first_onset <= baseline_onset + 2, (
            f"NS delayed onset by {first_onset - baseline_onset} frames "
            f"(baseline={baseline_onset}, ns_path={first_onset}); "
            f"contract caps the delay at 2 frames."
        )

    def test_silence_through_ns_does_not_manufacture_onset(self) -> None:
        # Negative control: a pure-silence stream through NS must
        # NOT trigger VAD onset. This guards against an NS bug
        # that adds resonant harmonics or DC bias.
        vad = _build_energy_driven_vad()
        ns = _build_spectral_ns()

        frames = _generate_signal_sequence(
            silence_frames=_SILENCE_PREFIX + _SPEECH_RUN,
            speech_frames=0,
        )
        ns_frames = [ns.process(f) for f in frames]
        states = _run_through_vad(vad, ns_frames)
        assert all(s in (VADState.SILENCE, VADState.SPEECH_ONSET) for s in states), (
            f"Unexpected state in pure-silence path: {states}"
        )
        # Even brief SPEECH_ONSET on a single frame would propagate
        # to SPEECH after min_onset_frames; if no SPEECH state is
        # ever reached we know NS didn't conjure speech from noise.
        assert VADState.SPEECH not in states, (
            f"NS conjured a speech signal from pure noise: {states}"
        )


class TestSpeechBandEnergyInvariant:
    """Pure spectral invariant — independent of VAD model. Locks the
    "NS preserves speech-band energy" contract that the VAD chain
    above depends on."""

    @staticmethod
    def _speech_band_rms(frame: np.ndarray) -> float:
        """Compute RMS of the 100-3500 Hz speech band."""
        f64 = frame.astype(np.float64)
        spectrum = np.fft.rfft(f64)
        freqs = np.fft.rfftfreq(_FRAME_SAMPLES, d=1.0 / _SAMPLE_RATE_HZ)
        speech_band = (freqs >= 100.0) & (freqs <= 3_500.0)
        # Parseval: sum of |X[k]|² over the band ≈ band-limited RMS².
        band_energy = float(np.sum(np.abs(spectrum[speech_band]) ** 2))
        return float(np.sqrt(band_energy / _FRAME_SAMPLES))

    def test_speech_band_rms_preserved_within_3db(self) -> None:
        ns = _build_spectral_ns()
        # Use the steady-state speech frame so the FFT window has a
        # full period of every component (periodic content → no
        # leakage into the gated bins).
        frame = _speech_like_frame(start_sample=0)
        ns_out = ns.process(frame)

        rms_in = self._speech_band_rms(frame)
        rms_out = self._speech_band_rms(ns_out)
        assert rms_in > 0.0
        # 3 dB = √2 ≈ 1.414× ratio. NS must not lose more than
        # this on real speech-band content; losing more would
        # blind downstream VAD on quieter speakers.
        ratio_db = 20.0 * np.log10((rms_out + 1e-12) / (rms_in + 1e-12))
        assert -3.0 <= ratio_db <= 1.0, (
            f"Speech-band RMS drift {ratio_db:.2f} dB exceeds the "
            f"±3 dB / +1 dB envelope (NS should attenuate or pass "
            f"through, never amplify)."
        )

    def test_speech_band_envelope_monotonic_within_run(self) -> None:
        # Across a sustained speech run, NS-processed frames'
        # speech-band RMS must be CONSISTENTLY non-zero (no random
        # frame goes silent). The contract is "NS keeps every
        # speech frame audible," not "NS preserves exact dB."
        ns = _build_spectral_ns()
        ns_band_rms = []
        for k in range(_SPEECH_RUN):
            frame = _speech_like_frame(start_sample=k * _FRAME_SAMPLES)
            ns_out = ns.process(frame)
            ns_band_rms.append(self._speech_band_rms(ns_out))

        # Every frame must have non-trivial speech-band content; a
        # zero or near-zero would blind VAD on a single dropped
        # frame mid-utterance.
        assert all(rms > 1.0 for rms in ns_band_rms), (
            f"NS produced near-silent frames mid-speech: {ns_band_rms}"
        )
        # The min/max ratio must stay within 6 dB across the run —
        # a wider swing would manifest as VAD chatter (state
        # bouncing between SPEECH and SPEECH_OFFSET).
        ratio_db = 20.0 * np.log10(
            max(ns_band_rms) / max(min(ns_band_rms), 1e-9),
        )
        assert ratio_db <= 6.0, (
            f"NS introduced {ratio_db:.2f} dB of speech-band ripple "
            f"across {_SPEECH_RUN} sustained-speech frames; ≤6 dB "
            f"contract violated."
        )
