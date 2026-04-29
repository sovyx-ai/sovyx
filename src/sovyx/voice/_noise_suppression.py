"""Noise suppression foundation (Phase 4 / T4.11-T4.12).

Foundation module for in-process noise suppression. Mirrors the AEC
foundation surface — abstract Protocol + NoOp default + a working
concrete implementation that ships zero new native dependencies.

Engine choice rationale (documented per ``feedback_no_speculation``):

The mission spec's preferred ``rnnoise-python`` (``pyrnnoise``) was
investigated 2026-04-29 and rejected for v0.27.0 foundation:

* Ships its DSP graph atop PyAV (ffmpeg bindings) which transitively
  pulls matplotlib + pillow + soundfile (50+ MB) onto every voice
  install — disproportionate cost for a NS feature.
* ``denoise_chunk`` yields variable-size frame batches that don't
  align with the FrameNormalizer's 512-sample emission window, so
  the wire-up would need a re-buffering layer.
* PyAV memory-allocation errors observed at ``denoise_chunk``
  initialization on Windows — non-trivial integration risk.

Foundation engine: **spectral gating** in pure NumPy + scipy (already
shipped). Algorithm:

1. Real-FFT the 512-sample int16 window into 257 complex bins.
2. Compute per-bin magnitude.
3. For each bin: ``gain = 1.0`` when magnitude exceeds the configured
   floor threshold, ``gain = attenuation`` otherwise. The threshold
   is operator-tunable via
   :attr:`VoiceTuningConfig.voice_noise_suppression_floor_db`.
4. Apply the gain mask, IRFFT back to 512 real samples, clip into
   int16 with the same saturation logic as the FrameNormalizer's
   downstream stages.

Quality envelope: spectral gating delivers ~5-10 dB SNR improvement
on stationary background noise (HVAC, fan noise) — below RNNoise's
~15-20 dB on non-stationary noise (keyboard, traffic) but cross-
platform shippable today. Adaptive floor estimation + per-bin RNN
inference (RNNoise) are reserved for v0.28.0+ via a custom
``librnnoise`` ctypes shim once production telemetry validates the
SNR gap.

Foundation phase scope (this commit, T4.11-T4.12 only):

* :class:`NoiseSuppressor` Protocol — interface contract.
* :class:`NoiseSuppressionConfig` — tuning knobs.
* :class:`NoOpNoiseSuppressor` — engine="off" / disabled-default.
* :class:`SpectralGatingSuppressor` — engine="spectral_gating".
* :func:`build_noise_suppressor` — factory keyed by config.

Out of scope (later commits per ``feedback_staged_adoption``):

* T4.13 wire-up into :mod:`sovyx.voice._frame_normalizer`.
* T4.14 default-flag flip planning (foundation default stays False).
* T4.15 ``voice_noise_suppression_engine`` future-flag for Krisp.
* T4.16 OTel ``voice.ns.engaged`` counter + suppression-dB
  histogram.
* T4.17 NS-vs-VAD integration test (RNNoise output → Silero VAD).
* T4.18 OS-NS detection + auto-disable.
* T4.19 Trade-off documentation.
* T4.20 Operator opt-in flag for OS DSP deference.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

import numpy as np

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    import numpy.typing as npt

logger = get_logger(__name__)


_INT16_FULL_SCALE = float(1 << 15)
"""Full-scale magnitude of int16 PCM."""

_DBFS_FLOOR = -120.0
"""Lowest dBFS a magnitude can report — guards against log10(0)."""


# ── Configuration ────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class NoiseSuppressionConfig:
    """Immutable NS tuning snapshot.

    Constructed once per pipeline lifetime. Operators rebuild the
    suppressor (via :func:`build_noise_suppressor`) after a config
    reload.
    """

    enabled: bool
    engine: Literal["off", "spectral_gating"]
    sample_rate: int
    frame_size_samples: int
    floor_db: float
    attenuation_db: float

    @property
    def floor_linear(self) -> float:
        """Linear-domain threshold derived from ``floor_db``.

        Bins whose magnitude (in dBFS) sits below this floor are
        multiplied by :attr:`attenuation_linear`. The floor is
        expressed against the int16 full-scale rail (32 768).
        """
        return float(_INT16_FULL_SCALE) * float(10.0 ** (self.floor_db / 20.0))

    @property
    def attenuation_linear(self) -> float:
        """Linear attenuation factor derived from ``attenuation_db``.

        ``attenuation_db = -20`` → ``attenuation_linear = 0.1``
        (-20 dB = 10× quieter). Clamped to ``[0.0, 1.0]`` since
        amplifying noise is never the right action.
        """
        gain = float(10.0 ** (self.attenuation_db / 20.0))
        return max(0.0, min(1.0, gain))


# ── Protocol ─────────────────────────────────────────────────────────────


@runtime_checkable
class NoiseSuppressor(Protocol):
    """Minimal noise suppression interface.

    Implementations process one PCM frame at a time. Input is int16
    PCM at the configured ``sample_rate``; the length must match
    the configured ``frame_size_samples``. Implementations are
    responsible for any internal buffering / state.

    Stateless contract: each :meth:`process` call is independent.
    Spectral-gating subclasses MAY hold a per-bin noise floor
    estimate across calls; the Protocol does not require it.
    """

    def process(self, frame: np.ndarray) -> np.ndarray:
        """Suppress noise in ``frame`` and return the cleaned output.

        Args:
            frame: Mic-side int16 PCM, length == frame_size_samples.

        Returns:
            Cleaned int16 PCM of identical shape. For
            :class:`NoOpNoiseSuppressor` this is ``frame`` verbatim.
        """
        ...

    def reset(self) -> None:
        """Reset any internal state (noise floor estimate, etc).

        Called on device change / pipeline restart so a stale
        estimator doesn't poison the next session.
        """
        ...


# ── Concrete: no-op (engine="off") ───────────────────────────────────────


class NoOpNoiseSuppressor:
    """Pass-through NS for ``engine="off"`` / disabled foundation.

    Mirrors :class:`NoOpAec` — kept as an explicit class so wire-up
    sites can call :meth:`process` unconditionally without a None
    check on every audio frame.
    """

    def process(self, frame: np.ndarray) -> np.ndarray:
        """Return ``frame`` unchanged."""
        return frame

    def reset(self) -> None:
        """No state — no-op."""


# ── Concrete: spectral gating (engine="spectral_gating") ─────────────────


class SpectralGatingSuppressor:
    """Frequency-domain magnitude gate.

    Each call:

    1. Real-FFTs the 512-sample int16 window → 257 complex bins.
    2. Computes per-bin magnitude.
    3. For bins below ``floor_linear`` magnitude → multiplies by
       ``attenuation_linear``. Above-threshold bins pass through
       unchanged.
    4. IRFFTs back to 512 real samples, clamps to int16 rails.

    Stateless: every call is independent. The next foundation step
    (T4.14+) layers an adaptive per-bin noise floor estimator on
    top so the floor tracks actual ambient noise. Foundation-T4.11
    keeps the threshold static so the algorithm is auditable in
    isolation.
    """

    def __init__(self, config: NoiseSuppressionConfig) -> None:
        if config.engine != "spectral_gating":
            raise ValueError(
                f"SpectralGatingSuppressor requires engine='spectral_gating', "
                f"got {config.engine!r}",
            )
        self._config = config
        self._floor_linear = config.floor_linear
        self._attenuation_linear = config.attenuation_linear

    def process(self, frame: np.ndarray) -> np.ndarray:
        """Apply the spectral gate to one frame."""
        if frame.dtype != np.int16:
            raise ValueError(f"frame dtype must be int16, got {frame.dtype}")
        if frame.size != self._config.frame_size_samples:
            raise ValueError(
                f"frame size mismatch: got {frame.size}, expected "
                f"{self._config.frame_size_samples}",
            )

        # int16 → float64 for FFT precision. Avoids accumulating
        # quantization noise in the magnitude / phase reconstruction.
        f64 = frame.astype(np.float64)
        spectrum = np.fft.rfft(f64)
        magnitudes = np.abs(spectrum)

        # Build the per-bin gain mask. Bins above the floor pass
        # through (gain=1); bins at-or-below get attenuated.
        gain_mask: npt.NDArray[np.float64] = np.where(
            magnitudes > self._floor_linear,
            1.0,
            self._attenuation_linear,
        ).astype(np.float64)

        gated_spectrum = spectrum * gain_mask
        cleaned_f64 = np.fft.irfft(gated_spectrum, n=frame.size)
        # Clip to int16 rails before casting — float→int conversion
        # would wrap silently on overflow without the explicit clip.
        clipped = np.clip(cleaned_f64, -(1 << 15), (1 << 15) - 1)
        return clipped.astype(np.int16)

    def reset(self) -> None:
        """No persistent state in the static-threshold variant."""


# ── Factory ──────────────────────────────────────────────────────────────


def build_noise_suppressor(config: NoiseSuppressionConfig) -> NoiseSuppressor:
    """Construct the concrete suppressor for the given config.

    Matrix:

    * ``enabled=False`` OR ``engine="off"`` →
      :class:`NoOpNoiseSuppressor`
    * ``enabled=True`` AND ``engine="spectral_gating"`` →
      :class:`SpectralGatingSuppressor`

    Raises :class:`ValueError` for unknown engines so a future
    refactor that adds an engine without updating the factory fails
    loudly.
    """
    if not config.enabled or config.engine == "off":
        return NoOpNoiseSuppressor()
    if config.engine == "spectral_gating":
        return SpectralGatingSuppressor(config)
    raise ValueError(f"Unknown noise-suppression engine: {config.engine!r}")


def build_frame_normalizer_noise_suppressor(
    *,
    enabled: bool,
    engine: Literal["off", "spectral_gating"],
    floor_db: float,
    attenuation_db: float,
) -> NoiseSuppressor:
    """Build a NoiseSuppressor pinned to the FrameNormalizer invariants.

    Convenience helper mirroring
    :func:`sovyx.voice._aec.build_frame_normalizer_aec`. Pins
    sample_rate=16000, frame_size_samples=512 so call sites only
    forward the operator-tunable knobs.
    """
    config = NoiseSuppressionConfig(
        enabled=enabled,
        engine=engine,
        sample_rate=16_000,
        frame_size_samples=512,
        floor_db=floor_db,
        attenuation_db=attenuation_db,
    )
    return build_noise_suppressor(config)


# ── Helpers ──────────────────────────────────────────────────────────────


def estimate_frame_dbfs(frame: np.ndarray) -> float:
    """Return the RMS dBFS of an int16 PCM frame.

    Used by tests to verify the gate's energy-based behaviour and
    by the future T4.16 ``voice.ns.suppression_db`` histogram once
    the metric lands.
    """
    if frame.size == 0:
        return _DBFS_FLOOR
    rms = float(np.sqrt(np.mean(np.square(frame.astype(np.float64)))))
    if rms < 1.0:
        return _DBFS_FLOOR
    return 20.0 * float(np.log10(rms / _INT16_FULL_SCALE))


__all__ = [
    "NoOpNoiseSuppressor",
    "NoiseSuppressionConfig",
    "NoiseSuppressor",
    "SpectralGatingSuppressor",
    "build_frame_normalizer_noise_suppressor",
    "build_noise_suppressor",
    "estimate_frame_dbfs",
]
