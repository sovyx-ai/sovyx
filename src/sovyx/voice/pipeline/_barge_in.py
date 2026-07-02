"""Barge-in sustain gating — real consecutive-speech-frame counter.

2026-07-02 audio-engine audit redesign (findings PIPELINE-2/3/4;
register: MISSION-AUDIO-ENGINE-CROSS-PLATFORM-AUDIT-2026-07-02):

* The original ``BargeInDetector`` held its own references to the
  shared :class:`~sovyx.voice.vad.SileroVAD` + output queue and re-ran
  ONNX inference per frame (``check_frame``) — a SECOND inference on
  the same stateful LSTM+FSM instance during SPEAKING, which
  double-advanced the shared hysteresis (offset window ~256 ms →
  ~128 ms of wall time) and bypassed the #50 VAD-stall timeout guard
  in ``feed_frame`` (PIPELINE-3).
* Its ``monitor()`` loop — the ONLY consumer of ``threshold_frames``
  — had zero production callers, so ``barge_in_threshold`` was dead
  code: barge-in fired on a single frame while the operator-facing
  WARN echoed the config value as a fabricated
  ``voice.frames_sustained`` (PIPELINE-2; anti-pattern #48/#70 class).

The detector is now a pure sustain counter. ``_handle_speaking``
feeds it the ``vad_event.is_speech`` verdict ALREADY computed by
``feed_frame``'s single (timeout-guarded) VAD inference — no second
inference exists anywhere on the barge-in path — and it reports a
sustained interruption only after ``threshold_frames`` CONSECUTIVE
speech frames (~32 ms each; reset on any non-speech frame). Holding
no VAD reference also closes PIPELINE-4 structurally: the C1 L2
``swap_vad`` recovery cannot leave a stale ONNX session captured on
the barge-in path, because no such capture exists.

The actual interruption is owned by the orchestrator's
``cancel_speech_chain`` (see ``_tts_cancel_chain_mixin``); this class
only answers "has the user spoken long enough for this to be a real
interruption rather than a blip or an echo tail?".
"""

from __future__ import annotations

from sovyx.voice.pipeline._constants import _BARGE_IN_THRESHOLD_FRAMES


class BargeInDetector:
    """Consecutive-speech-frame sustain counter for barge-in.

    ``_handle_speaking`` calls :meth:`observe` once per frame with the
    verdict from the pipeline's single per-frame VAD inference. The
    counter increments on speech, resets on non-speech, and reports
    ``True`` once ``threshold_frames`` consecutive speech frames have
    been observed (default 5 × 32 ms ≈ 160 ms of sustained speech, on
    top of the VAD FSM's own onset hysteresis). The orchestrator then
    runs the T1 cancellation chain and calls :meth:`reset`.

    Args:
        threshold_frames: Consecutive speech frames needed to report a
            sustained interruption. This is
            ``VoicePipelineConfig.barge_in_threshold`` — a REAL gate
            since the 2026-07-02 redesign (see module docstring).
    """

    def __init__(self, threshold_frames: int = _BARGE_IN_THRESHOLD_FRAMES) -> None:
        self._threshold = threshold_frames
        self._consecutive = 0

    @property
    def threshold_frames(self) -> int:
        """Configured consecutive-speech-frame firing threshold."""
        return self._threshold

    @property
    def frames_sustained(self) -> int:
        """Current run of consecutive speech frames (a real measurement).

        Read by the orchestrator at fire time so the
        ``voice.barge_in.detected`` WARN reports the measured sustain,
        not a config echo (anti-pattern #48 — falsifiability).
        """
        return self._consecutive

    def observe(self, *, is_speech: bool) -> bool:
        """Advance the counter with one frame's VAD verdict.

        Args:
            is_speech: The ``vad_event.is_speech`` verdict from
                ``feed_frame``'s single VAD inference for this frame.

        Returns:
            ``True`` when the consecutive-speech run has reached the
            threshold — the caller fires the barge-in chain and calls
            :meth:`reset`. ``False`` otherwise; a non-speech frame
            resets the run to zero.
        """
        if not is_speech:
            self._consecutive = 0
            return False
        self._consecutive += 1
        return self._consecutive >= self._threshold

    def reset(self) -> None:
        """Zero the consecutive counter (session boundary or post-fire).

        Called by the orchestrator after a barge-in fires and by the
        TTS-out surfaces when a new speech session opens, so a run
        accumulated in a previous SPEAKING window never leaks into the
        next turn's gate.
        """
        self._consecutive = 0


# ---------------------------------------------------------------------------
# JarvisIllusion — re-exported from jarvis.py (V05-24)
# ---------------------------------------------------------------------------


__all_jarvis__ = ["JarvisIllusion", "JarvisConfig", "split_at_boundaries"]


# ---------------------------------------------------------------------------
# VoicePipeline — main orchestrator
# ---------------------------------------------------------------------------
