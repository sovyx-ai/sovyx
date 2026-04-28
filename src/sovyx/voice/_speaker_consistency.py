"""Spectral-centroid drift detector — T1.39.

Catches the class of TTS regression where the synthesized voice
**shifts mid-session** without an explicit voice change. The
silence-class regression (synthesizer producing zero-energy
audio) is already covered by T1.36's zero-energy validation; this
module is its drift-class complement — the synthesizer keeps
producing audio but the timbre is wrong.

Failure modes the detector surfaces:

* **Voice file partial download** — a Piper ``*.onnx`` whose fetch
  was interrupted past the integrity-check window mid-session.
  Subsequent chunks render with truncated phoneme tables, producing
  a noticeably different timbre.
* **ONNX session corruption** — heap pressure mutating weight
  tensors. The pure-silence case is caught by T1.36; the still-
  producing-audio-but-wrong-voice case is caught here.
* **Mid-session voice swap by a buggy caller** — cognitive layer
  passes a different ``voice_id`` per chunk; the detector surfaces
  the symptom so operators can audit the call site.

Spectral centroid (the centre-of-mass of the FFT magnitude
spectrum) is one of the canonical features used in MFCC-based
speaker identification. It sits in the 1.0-3.0 kHz range for
human speech and is tightly clustered per speaker — a 5 % drift
on a 2.0 kHz centroid (= 100 Hz, ~ semitone) IS perceptible.

DSP cost is ~50 µs per chunk on a modern CPU (FFT is O(N log N);
a typical TTS chunk is 1-3 s of 22 kHz audio). Negligible
against the ~100 ms TTS synthesis cost itself, but the
orchestrator wraps the call in :func:`asyncio.to_thread` per
CLAUDE.md anti-pattern #14 to keep the async loop free of even
sub-millisecond CPU bursts.

Reference: ``docs-internal/T1.39-speaker-consistency-check-rfc.md``
for the full design rationale (per-session state, rolling-window
size, drift metric, alert action).
"""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    import numpy.typing as npt


__all__ = [
    "SpeakerConsistencyMonitor",
    "compute_spectral_centroid",
]


def compute_spectral_centroid(
    audio_int16: npt.NDArray[np.int16],
    sample_rate: int,
) -> float:
    """Return the spectral centroid (Hz) of an int16 PCM utterance.

    The centroid is the magnitude-weighted mean frequency of the
    rfft magnitude spectrum:

        centroid = sum(f[k] * |X[k]|) / sum(|X[k]|)

    where ``f[k]`` is the frequency at bin ``k`` and ``|X[k]|`` is
    the magnitude. Higher centroid → brighter / more high-frequency
    energy. Lower centroid → darker / more low-frequency.

    Args:
        audio_int16: int16 PCM samples (mono). Empty arrays return
            ``0.0`` so the caller can skip the drift check on
            degenerate input.
        sample_rate: Samples per second. Must be ``> 0``.

    Returns:
        The spectral centroid in Hz, or ``0.0`` if the input is
        empty / all-zero (the magnitude spectrum sums to zero, so
        the centroid is undefined; ``0.0`` signals the caller to
        skip the drift check).
    """
    if audio_int16.size == 0:
        return 0.0
    samples = audio_int16.astype(np.float64)
    spectrum = np.abs(np.fft.rfft(samples))
    spectrum_sum = spectrum.sum()
    if spectrum_sum <= 0.0:
        return 0.0
    freqs = np.fft.rfftfreq(samples.size, d=1.0 / sample_rate)
    return float((freqs * spectrum).sum() / spectrum_sum)


class SpeakerConsistencyMonitor:
    """Per-session rolling-window spectral-centroid drift detector.

    The first ``window_size - 1`` observations build the baseline
    silently (no alert); from observation ``window_size`` onward
    each new centroid is compared to the mean of the prior
    ``window_size`` observations. When
    ``|centroid - baseline| / baseline`` exceeds
    ``drift_ratio_threshold`` the next call returns
    ``drift_detected=True``.

    The window is a :class:`collections.deque` with bounded
    ``maxlen``; old observations evict naturally as new ones
    arrive. A single anomalous chunk therefore falls out of the
    baseline after ``window_size`` further chunks — the detector
    is self-healing for transients.

    Lifecycle: instantiate per-pipeline (one monitor per
    :class:`VoicePipeline` instance), :meth:`reset` on session
    boundary (wake-word detection), :meth:`observe` per emitted
    TTS chunk.

    Thread-safety: the monitor is single-writer by contract — only
    the orchestrator's stream_text path calls :meth:`observe`.
    Cross-thread mutation requires external locking (none currently
    needed).
    """

    def __init__(
        self,
        *,
        window_size: int = 5,
        drift_ratio_threshold: float = 0.05,
    ) -> None:
        """Initialise the monitor.

        Args:
            window_size: Number of recent centroids that contribute
                to the rolling baseline. Floor 2 (single-sample
                baselines fire on any drift); ceiling 50 (a baseline
                that smooth takes minutes to detect a real drift).
            drift_ratio_threshold: Relative-drift threshold —
                ``|centroid - baseline| / baseline`` above this
                fraction triggers the alert. Default 0.05 (5 %).
        """
        if window_size < 2:
            msg = f"window_size must be >= 2, got {window_size}"
            raise ValueError(msg)
        if drift_ratio_threshold <= 0.0:
            msg = f"drift_ratio_threshold must be > 0, got {drift_ratio_threshold}"
            raise ValueError(msg)
        self._window: deque[float] = deque(maxlen=window_size)
        self._threshold = drift_ratio_threshold

    def observe(self, centroid_hz: float) -> tuple[bool, float, float]:
        """Record a new centroid; return ``(drift_detected, baseline, ratio)``.

        Returns ``(False, 0.0, 0.0)`` while the window is being
        filled (the first ``window_size - 1`` observations) — the
        baseline is undefined until ``window_size`` samples are
        present. Also returns ``(False, 0.0, 0.0)`` on
        non-positive ``centroid_hz`` so the caller can pass through
        :func:`compute_spectral_centroid`'s ``0.0`` "undefined"
        sentinel without a guard.

        Args:
            centroid_hz: The newest centroid measurement, in Hz.
                Non-positive values are treated as
                "skip — degenerate input" and don't update the
                window.

        Returns:
            ``(drift_detected, baseline_hz, drift_ratio)`` —
            ``drift_detected`` is ``True`` iff the relative drift
            exceeds the configured threshold. ``baseline_hz`` is
            the mean of the prior window; ``drift_ratio`` is
            ``|centroid - baseline| / baseline``. Both are ``0.0``
            during the warm-up phase.
        """
        if centroid_hz <= 0.0:
            return (False, 0.0, 0.0)
        # ``window_size - 1`` warm-up observations don't trigger;
        # they only populate the deque so the baseline mean is
        # well-defined when the next observation arrives.
        if self._window.maxlen is None or len(self._window) < self._window.maxlen:
            self._window.append(centroid_hz)
            return (False, 0.0, 0.0)
        baseline = sum(self._window) / len(self._window)
        ratio = abs(centroid_hz - baseline) / baseline if baseline > 0.0 else 0.0
        # Slide the window AFTER computing the baseline so the
        # newest centroid contributes to the NEXT baseline (not its
        # own).
        self._window.append(centroid_hz)
        return (ratio > self._threshold, baseline, ratio)

    def reset(self) -> None:
        """Clear the window — call on session boundary (new wake).

        A new session may legitimately use a different voice (the
        operator switched mind / the cognitive layer picked a
        different persona). Carrying the prior session's baseline
        across the boundary would surface a false-positive drift on
        the first chunk of the new session. :meth:`reset` is the
        per-session reset hook the orchestrator calls at
        ``WAKE_DETECTED``.
        """
        self._window.clear()
