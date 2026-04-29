"""Double-talk detector for AEC reference protection (Phase 4 / T4.9).

When TTS is playing AND the user is speaking simultaneously
("double-talk"), the AEC's adaptive filter must NOT update — the
user's voice would be misinterpreted as echo and the filter would
diverge, distorting subsequent speech. Standard mitigation: detect
double-talk and freeze filter adaptation while it's active.

This module ships the *detector*. The freeze wire-up into the AEC
processor is staged for a follow-up commit per
``feedback_staged_adoption`` — foundation observes (telemetry +
debug log), the freeze action lands once we have production NCC
distribution data to calibrate the threshold.

Algorithm: Normalised Cross-Correlation (NCC)

    NCC(r, c) = Σ(r[n]·c[n]) / √(Σr[n]² · Σc[n]²)

Where:

* ``r`` = render reference PCM (echo source — TTS playback)
* ``c`` = capture PCM (mic input — possibly contaminated by user
  voice during double-talk)

Range: [-1.0, 1.0]. Interpretation:

* ``NCC ≈ 1.0`` — pure echo (capture is a near-linear function of
  render; AEC filter can converge cleanly).
* ``0.0 < NCC < 0.5`` — partial echo + significant non-echo
  content → double-talk likely.
* ``NCC ≈ 0.0`` — render and capture uncorrelated. Either:
    (a) the user is speaking with TTS silent (caller should never
        invoke us in this state — it's the ``render_silent`` branch),
    (b) double-talk dominates, OR
    (c) AEC delay alignment is broken (filter hasn't converged).

Threshold default 0.5 picks the boundary where standard NLMS
filters in the literature begin to diverge under double-talk
load. Operators tune via
:attr:`VoiceTuningConfig.voice_double_talk_ncc_threshold`.

Foundation phase scope (T4.9):

* :func:`compute_ncc` — pure-DSP NCC measurement.
* :class:`DoubleTalkDetector` — stateful wrapper with threshold
  + per-window decision.
* Configuration via ``VoiceTuningConfig.voice_double_talk_*``.

Out of scope (later commits):

* Filter-freeze wire-up — Speex AEC's ``pyaec`` binding does not
  expose adaptation control directly; T4.6+ revisits.
* Coherence-based detector (frequency-domain) — higher accuracy
  at higher cost; reserve for hardware where Geigel/NCC fails.
* Hangover timer — typical pro implementations hold the
  freeze for N frames after double-talk ends to avoid filter
  re-divergence on filter-resume transients. Foundation skips this
  since we're not wiring freeze yet.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from sovyx.observability.logging import get_logger

logger = get_logger(__name__)


_DEFAULT_NCC_THRESHOLD = 0.5
"""Standard NLMS-divergence threshold from AEC literature.

Below this value, the cross-correlation signal is too noisy to
trust the filter adaptation. Operators may tune up (more
permissive — fewer freezes, accept some divergence risk) or
down (more aggressive — frequent freezes, trade convergence
speed for stability).
"""

_SILENCE_FLOOR = 1.0
"""Power floor below which NCC is undefined.

Mean-square power of an int16 frame. Both render and capture
must clear this floor for the correlation denominator to be
non-zero. The floor is conservative — any frame with
``rms_db < ~-90 dBFS`` (real silence) sits below it.
"""


@dataclass(frozen=True, slots=True)
class DoubleTalkDecision:
    """Per-window double-talk verdict.

    Attributes:
        ncc: Normalised cross-correlation in ``[-1, 1]``, or
            ``None`` when either side is below the silence floor.
            ``None`` means "decision unavailable" — callers should
            treat as no-action (no freeze, no telemetry record).
        detected: ``True`` when ``ncc`` is computable AND below the
            threshold. False otherwise (including the
            decision-unavailable case).
    """

    ncc: float | None
    detected: bool


def compute_ncc(
    render: np.ndarray,
    capture: np.ndarray,
) -> float | None:
    """Compute the normalised cross-correlation of two PCM frames.

    Both arrays must be the same length and int16-dtype. Returns
    ``None`` when either array's mean-square power is below the
    silence floor — the correlation denominator is zero (or
    numerically unstable) in that case.

    Args:
        render: Far-end (TTS playback) int16 PCM. Length N.
        capture: Mic-side int16 PCM. Length N.

    Returns:
        Float in ``[-1.0, 1.0]`` when both signals are non-silent,
        ``None`` when one or both are silent.

    Raises:
        ValueError: shape / dtype mismatch.
    """
    if render.shape != capture.shape:
        msg = f"render/capture shape mismatch: {render.shape} vs {capture.shape}"
        raise ValueError(msg)
    if render.dtype != np.int16 or capture.dtype != np.int16:
        msg = (
            f"render and capture must be int16, got render={render.dtype} capture={capture.dtype}"
        )
        raise ValueError(msg)

    r_f64 = render.astype(np.float64)
    c_f64 = capture.astype(np.float64)

    r_power = float(np.mean(np.square(r_f64)))
    c_power = float(np.mean(np.square(c_f64)))

    if r_power < _SILENCE_FLOOR or c_power < _SILENCE_FLOOR:
        return None

    numerator = float(np.mean(r_f64 * c_f64))
    denominator = float(np.sqrt(r_power * c_power))
    return numerator / denominator


class DoubleTalkDetector:
    """Stateful wrapper that combines :func:`compute_ncc` with a threshold.

    Args:
        threshold: NCC value below which the detector declares
            double-talk. Default :data:`_DEFAULT_NCC_THRESHOLD`
            (0.5). Bounded ``[-1.0, 1.0]`` — values outside the
            valid NCC range raise ``ValueError`` at construction.

    Raises:
        ValueError: ``threshold`` outside ``[-1.0, 1.0]``.
    """

    def __init__(self, *, threshold: float = _DEFAULT_NCC_THRESHOLD) -> None:
        if not (-1.0 <= threshold <= 1.0):
            msg = f"threshold must be in [-1.0, 1.0], got {threshold!r}"
            raise ValueError(msg)
        self._threshold = float(threshold)

    @property
    def threshold(self) -> float:
        """Active NCC threshold."""
        return self._threshold

    def analyze(
        self,
        render: np.ndarray,
        capture: np.ndarray,
    ) -> DoubleTalkDecision:
        """Compute NCC + return the per-window decision.

        Args:
            render: int16 render PCM (echo reference).
            capture: int16 capture PCM (mic input).

        Returns:
            :class:`DoubleTalkDecision` carrying the raw NCC value
            (``None`` when silent) AND the threshold-vs-NCC verdict.
        """
        ncc = compute_ncc(render, capture)
        if ncc is None:
            return DoubleTalkDecision(ncc=None, detected=False)
        return DoubleTalkDecision(ncc=ncc, detected=ncc < self._threshold)


__all__ = [
    "DoubleTalkDecision",
    "DoubleTalkDetector",
    "compute_ncc",
]
