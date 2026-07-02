"""Mission C1 §4.4 L5 — fallback energy-based VAD.

Last-resort recovery step in the
:class:`~sovyx.voice.health._vad_frontend_recovery.VADFrontendRecovery`
ladder: when L1 (Silero LSTM reset) + L2 (fresh ONNX session) + L3
(FrameNormalizer engage) + L4 (AGC2 floor lift) ALL fail to restore
VAD responsiveness, the pipeline swaps its :class:`SileroVAD` for a
:class:`FallbackEnergyVAD` for the remainder of the session.

**Operator-visible degradation:** VAD accuracy drops to RMS-threshold
semantics. Speech detection is binary on a fixed energy floor with
hysteresis (no spectral content awareness, no LSTM context). False
positives on loud non-speech transients (door slams, keyboard) are
expected; false negatives on quiet whispers are also expected.

**Why this is still worth shipping over outright failover:**
when Silero is wedged, the operator's mic IS delivering real audio
(otherwise we'd be in DRIVER_SILENT territory, not VAD_FRONTEND_DEAD).
A dumb energy gate keeps the speech-routing pipeline functional for
the rest of the session — barge-in, turn-taking, STT trigger — at
the cost of higher false-positive rate. Daemon restart picks Silero
again on the next boot; this is degraded-but-running, not broken.

Interface contract: :class:`FallbackEnergyVAD` is duck-typed against
the subset of :class:`SileroVAD` that :class:`VoicePipeline` reads
in production: ``process_frame(audio_frame) -> VADEvent``,
``reset() -> None``, plus the ``state`` / ``is_speaking`` /
``config`` / ``model_path`` properties. The pipeline's
:meth:`swap_vad` does Python attribute assignment so the duck-typed
interface is sufficient — no ABC inheritance needed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger
from sovyx.voice.vad import VADConfig, VADEvent, VADState

if TYPE_CHECKING:
    import numpy as np
    import numpy.typing as npt


logger = get_logger(__name__)


_FALLBACK_INT16_FULLSCALE = 32_768.0
"""int16 normalisation factor — matches the ``/ 32768.0`` divisor
inlined in :meth:`SileroVAD.process_frame`.

The fallback runs the same int16 → float32 normalisation path the
real Silero takes for input frames; matching the divisor keeps RMS
readings comparable across the swap boundary."""


_FALLBACK_RMS_FLOOR_DBFS = -120.0
"""dBFS floor for empty / silent frames.

Mirrors :data:`voice.health.capture_integrity._RMS_FLOOR_DB` so
fallback RMS values plot on the same axis as the integrity probe's
RMS samples — operator dashboards see a continuous trace across the
Silero → fallback swap."""


@dataclass(frozen=True, slots=True)
class FallbackVADConfig:
    """Tuning for :class:`FallbackEnergyVAD`.

    Defaults chosen to favor FALSE POSITIVES over false negatives —
    the pipeline can still trigger STT on a non-speech transient and
    have STT return an empty transcript; missing a real speech turn
    is worse for the operator's UX.

    Attributes:
        sample_rate: Expected sample rate of incoming frames (Hz).
            Matched against :class:`VADConfig.sample_rate` so the
            swap is transparent to downstream consumers.
        window_size: Expected frame size in samples. Matched against
            :class:`VADConfig.window_size`.
        speech_rms_threshold_dbfs: Frame RMS above this level is
            classified as speech. Default -45 dBFS is a comfortable
            indoor speaking voice; quieter than this is treated as
            silence.
        onset_consecutive_frames: Speech FSM transitions
            ``SILENCE → SPEECH_ONSET → SPEECH`` after this many
            consecutive supra-threshold frames. Hysteresis against
            single-frame transients. At 32 ms per frame (Silero
            v5 default), 3 frames ≈ 96 ms.
        offset_consecutive_frames: ``SPEECH → SPEECH_OFFSET →
            SILENCE`` after this many consecutive sub-threshold
            frames. Longer than onset to keep brief speech pauses
            attached to the active turn (matches Silero's
            hysteresis preference).
    """

    sample_rate: int = 16_000
    window_size: int = 512
    speech_rms_threshold_dbfs: float = -45.0
    onset_consecutive_frames: int = 3
    offset_consecutive_frames: int = 8


class FallbackEnergyVAD:
    """Energy-based VAD with hysteresis FSM — duck-typed against
    :class:`SileroVAD` for :meth:`VoicePipeline.swap_vad` handoff.

    State machine mirrors Silero's:

    ::

        SILENCE → SPEECH_ONSET → SPEECH → SPEECH_OFFSET → SILENCE

    Transitions require N consecutive supra/sub-threshold frames per
    :class:`FallbackVADConfig` (anti-chatter hysteresis). Probability
    is the linear-scaled RMS proximity to the threshold (clamped to
    [0, 1]) — exposes a continuous-feeling signal for downstream
    consumers that read :attr:`VADEvent.probability`.

    Construction is cheap (no ONNX, no model file). Production
    callers use :meth:`VoicePipeline.swap_vad` to drop the
    fallback into the live pipeline at L5 of the recovery ladder.
    """

    # ``model_path`` semantics: the fallback has no model file — we
    # surface the sentinel path so :meth:`VoicePipeline.reinstantiate_vad`
    # never accidentally tries to rebuild a Silero session from it
    # (the recovery code reads ``model_path`` only on Silero
    # instances; the integrity probe never reinstantiates the
    # fallback). The empty path is unambiguous: tests can match on
    # ``str(fallback.model_path) == ""``.
    _SENTINEL_MODEL_PATH = Path("")

    def __init__(self, config: FallbackVADConfig | None = None) -> None:
        self._config = config or FallbackVADConfig()
        # Map fallback config onto a :class:`VADConfig` shape so the
        # pipeline-side consumers that read ``vad.config.sample_rate``
        # / ``vad.config.window_size`` see the same values they got
        # from Silero. ``VADConfig.window_size`` is a derived property
        # (16 kHz → 512 samples, 8 kHz → 256), so only ``sample_rate``
        # flows through the constructor; the fallback's own
        # ``window_size`` knob must match the Silero canonical mapping.
        self._vad_config = VADConfig(sample_rate=self._config.sample_rate)
        self._state = VADState.SILENCE
        # FSM counters — supra-threshold runs for onset, sub-threshold
        # runs for offset. Reset on every state transition.
        self._consecutive_supra = 0
        self._consecutive_sub = 0
        logger.info(
            "voice.fallback_vad.engaged",
            sample_rate=self._config.sample_rate,
            window_size=self._config.window_size,
            threshold_dbfs=self._config.speech_rms_threshold_dbfs,
        )

    # ---------------------------------------------------------------- API

    def process_frame(
        self,
        audio_frame: npt.NDArray[np.float32] | npt.NDArray[np.int16],
    ) -> VADEvent:
        """Classify one frame via RMS threshold + hysteresis FSM.

        Mirrors :meth:`SileroVAD.process_frame` shape: takes a numpy
        array of the configured ``window_size`` (float32 [-1, 1] OR
        int16 [-32768, 32767]), returns a :class:`VADEvent` carrying
        the FSM state + a 0..1 probability proxy.

        No ONNX, no GIL-blocking work — pure numpy + scalar math.
        Safe to call inline on the audio-callback hot path.
        """
        rms_dbfs = self._rms_dbfs(audio_frame)
        is_supra = rms_dbfs >= self._config.speech_rms_threshold_dbfs
        if is_supra:
            self._consecutive_supra += 1
            self._consecutive_sub = 0
        else:
            self._consecutive_sub += 1
            self._consecutive_supra = 0
        new_state = self._step_fsm(is_supra=is_supra)
        self._state = new_state
        is_speech = new_state in (VADState.SPEECH, VADState.SPEECH_OFFSET)
        probability = self._proximity_probability(rms_dbfs)
        return VADEvent(
            is_speech=is_speech,
            probability=probability,
            state=new_state,
        )

    def reset(self) -> None:
        """Zero the FSM + hysteresis counters. Mirrors
        :meth:`SileroVAD.reset` for L1 ladder compatibility (the
        recovery ladder calls ``reset_vad()`` on whatever VAD is
        currently mounted — the fallback honors the contract even
        though its own state is trivially small)."""
        self._state = VADState.SILENCE
        self._consecutive_supra = 0
        self._consecutive_sub = 0

    @property
    def state(self) -> VADState:
        """Current FSM state — matches :attr:`SileroVAD.state`."""
        return self._state

    @property
    def is_speaking(self) -> bool:
        """Whether the FSM considers speech ongoing — matches
        :attr:`SileroVAD.is_speaking`."""
        return self._state in (VADState.SPEECH, VADState.SPEECH_OFFSET)

    @property
    def config(self) -> VADConfig:
        """:class:`VADConfig`-shaped surface so downstream consumers
        reading ``vad.config.sample_rate`` etc. work unchanged after
        the swap."""
        return self._vad_config

    @property
    def model_path(self) -> Path:
        """Sentinel empty :class:`Path`. Fallback has no model
        artefact; the recovery ladder distinguishes Silero (real
        model) from fallback (empty path) by string comparison."""
        return self._SENTINEL_MODEL_PATH

    @property
    def fallback_config(self) -> FallbackVADConfig:
        """Expose the fallback-specific config for diagnostics +
        tests. Not part of the SileroVAD duck-type — only callers
        that KNOW they're looking at a fallback should read it."""
        return self._config

    # ----------------------------------------------------------- internals

    def _rms_dbfs(
        self,
        frame: npt.NDArray[np.float32] | npt.NDArray[np.int16],
    ) -> float:
        """Compute frame RMS in dBFS, normalised to [-120, 0]."""
        import numpy as np  # noqa: F811

        if frame.size == 0:
            return _FALLBACK_RMS_FLOOR_DBFS
        if frame.dtype == np.int16:
            samples = frame.astype(np.float32) / _FALLBACK_INT16_FULLSCALE
        else:
            samples = frame.astype(np.float32)
        sample_sq = float(np.mean(np.square(samples)))
        if sample_sq <= 0 or not math.isfinite(sample_sq):
            return _FALLBACK_RMS_FLOOR_DBFS
        return 10.0 * math.log10(sample_sq)

    def _step_fsm(self, *, is_supra: bool) -> VADState:
        """Advance the FSM one frame; return the next state."""
        current = self._state
        cfg = self._config
        if current == VADState.SILENCE:
            if is_supra and self._consecutive_supra >= cfg.onset_consecutive_frames:
                return VADState.SPEECH_ONSET
            return VADState.SILENCE
        if current == VADState.SPEECH_ONSET:
            if is_supra:
                return VADState.SPEECH
            return VADState.SILENCE
        if current == VADState.SPEECH:
            if not is_supra and self._consecutive_sub >= cfg.offset_consecutive_frames:
                return VADState.SPEECH_OFFSET
            return VADState.SPEECH
        if current == VADState.SPEECH_OFFSET:
            if is_supra:
                return VADState.SPEECH
            return VADState.SILENCE
        # Defensive fallback — unknown state shouldn't be reachable
        # because :class:`VADState` is a closed enum. Reset for safety.
        return VADState.SILENCE

    def _proximity_probability(self, rms_dbfs: float) -> float:
        """Map RMS dBFS proximity to the threshold into a 0..1 proxy.

        Not a real speech probability (the fallback doesn't have one)
        — just a continuous-feeling signal so downstream consumers
        that read :attr:`VADEvent.probability` see a smooth surface.
        Scale: ``-80 dBFS → 0.0``, threshold → ``0.5``,
        ``-10 dBFS → 1.0`` (linear in dB, clamped).
        """
        threshold = self._config.speech_rms_threshold_dbfs
        # Symmetric linear band ±35 dB around threshold spans 0..1.
        delta = rms_dbfs - threshold
        proximity = 0.5 + (delta / 70.0)
        return max(0.0, min(1.0, proximity))


__all__ = ["FallbackEnergyVAD", "FallbackVADConfig"]
