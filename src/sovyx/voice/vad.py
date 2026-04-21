"""SileroVAD v5 with hysteresis state machine.

Onset/offset FSM with configurable thresholds and 512-sample window (32ms @16kHz).
Prevents rapid on/off switching via consecutive-frame gating.

Ref: SPE-010 §3 (VAD), IMPL-004 §2.4 (SileroVAD v5 code)
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import IntEnum, auto
from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from pathlib import Path

    import numpy as np
    import numpy.typing as npt

logger = get_logger(__name__)

_WINDOW_HISTORY = 5
"""Rolling-window depth carried on ``voice.vad.state_changed`` so the
dashboard timeline can render the probability / RMS build-up that led
to each FSM transition instead of a single point estimate."""

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SAMPLE_RATE_16K = 16000
_SAMPLE_RATE_8K = 8000
_WINDOW_16K = 512
_WINDOW_8K = 256
_LSTM_STATE_SHAPE = (2, 1, 128)


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


class VADState(IntEnum):
    """Hysteresis state machine states.

    Transitions: SILENCE → SPEECH_ONSET → SPEECH → SPEECH_OFFSET → SILENCE
    Each transition requires *consecutive* frames above/below threshold.
    """

    SILENCE = auto()
    SPEECH_ONSET = auto()
    SPEECH = auto()
    SPEECH_OFFSET = auto()


@dataclass(frozen=True, slots=True)
class VADEvent:
    """Result of processing a single audio frame."""

    is_speech: bool
    """Whether the FSM considers the current frame as speech."""

    probability: float
    """Raw speech probability from the model (0.0–1.0)."""

    state: VADState
    """FSM state after processing this frame."""


@dataclass(frozen=True, slots=True)
class VADConfig:
    """Calibrated parameters for SileroVAD v5 (IMPL-004 §5).

    Defaults tuned for 16 kHz input on Pi 5 (Cortex-A76).
    """

    onset_threshold: float = 0.5
    """Probability above which a frame is considered speech-likely."""

    offset_threshold: float = 0.3
    """Probability below which a frame is considered silence-likely."""

    min_onset_frames: int = 3
    """Consecutive frames (≈96 ms) above onset to confirm speech start."""

    min_offset_frames: int = 8
    """Consecutive frames (≈256 ms) below offset to confirm speech end."""

    sample_rate: int = _SAMPLE_RATE_16K
    """Audio sample rate — only 8000 or 16000 supported."""

    @property
    def window_size(self) -> int:
        """Frame size in samples (fixed per sample rate)."""
        if self.sample_rate == _SAMPLE_RATE_16K:
            return _WINDOW_16K
        if self.sample_rate == _SAMPLE_RATE_8K:
            return _WINDOW_8K
        msg = f"Unsupported sample rate: {self.sample_rate}. Use 8000 or 16000."
        raise ValueError(msg)


def _validate_config(config: VADConfig) -> None:
    """Raise ``ValueError`` for obviously bad config values."""
    if config.onset_threshold <= 0.0 or config.onset_threshold >= 1.0:
        msg = f"onset_threshold must be in (0, 1), got {config.onset_threshold}"
        raise ValueError(msg)
    if config.offset_threshold <= 0.0 or config.offset_threshold >= 1.0:
        msg = f"offset_threshold must be in (0, 1), got {config.offset_threshold}"
        raise ValueError(msg)
    if config.offset_threshold >= config.onset_threshold:
        msg = (
            f"offset_threshold ({config.offset_threshold}) must be < "
            f"onset_threshold ({config.onset_threshold}) for hysteresis"
        )
        raise ValueError(msg)
    if config.min_onset_frames < 1:
        msg = f"min_onset_frames must be >= 1, got {config.min_onset_frames}"
        raise ValueError(msg)
    if config.min_offset_frames < 1:
        msg = f"min_offset_frames must be >= 1, got {config.min_offset_frames}"
        raise ValueError(msg)
    # Validate sample rate eagerly
    _ = config.window_size


# ---------------------------------------------------------------------------
# SileroVAD
# ---------------------------------------------------------------------------


class SileroVAD:
    """SileroVAD v5 with ONNX inference and hysteresis state machine.

    V5 improvements over V4:
    - 3× faster TorchScript, 10 % faster ONNX
    - Fixed window: 512 samples at 16 kHz (32 ms)
    - 6000+ languages supported
    - Smaller model (≈2 MB ONNX)

    State machine prevents rapid on/off switching::

        SILENCE → SPEECH_ONSET → SPEECH → SPEECH_OFFSET → SILENCE

    Each transition requires consecutive frames above/below threshold,
    preventing cut-off during natural pauses.

    Performance: <1 ms per frame on Pi 5 (Cortex-A76).
    """

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def __init__(
        self,
        model_path: Path,
        config: VADConfig | None = None,
    ) -> None:
        import numpy as np  # noqa: F811
        import onnxruntime as ort

        self._config = config or VADConfig()
        _validate_config(self._config)

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 1  # VAD is tiny — 1 thread is optimal
        opts.inter_op_num_threads = 1
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

        self._session = ort.InferenceSession(
            str(model_path),
            sess_options=opts,
            providers=["CPUExecutionProvider"],
        )

        # Persistent LSTM state (h0, c0) — survives between frames
        self._state: npt.NDArray[np.float32] = np.zeros(_LSTM_STATE_SHAPE, dtype=np.float32)
        self._sr: npt.NDArray[np.int64] = np.array([self._config.sample_rate], dtype=np.int64)

        # FSM bookkeeping
        self._vad_state = VADState.SILENCE
        self._consecutive_count = 0

        # Rolling windows for state_changed enrichment. Bounded so the
        # memory footprint is constant regardless of session length.
        self._prob_history: deque[float] = deque(maxlen=_WINDOW_HISTORY)
        self._rms_history: deque[float] = deque(maxlen=_WINDOW_HISTORY)

        logger.info(
            "SileroVAD initialised",
            model=str(model_path),
            sample_rate=self._config.sample_rate,
            window_size=self._config.window_size,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def process_frame(
        self,
        audio_frame: npt.NDArray[np.float32] | npt.NDArray[np.int16],
    ) -> VADEvent:
        """Process a single audio frame through ONNX model + hysteresis FSM.

        Args:
            audio_frame: Exactly ``config.window_size`` samples —
                float32 normalised [-1, 1] **or** int16 [-32768, 32767].

        Returns:
            A :class:`VADEvent` with speech flag, probability, and FSM state.

        Raises:
            ValueError: If frame has wrong length.
        """
        import numpy as np  # noqa: F811

        expected = self._config.window_size
        if audio_frame.shape != (expected,):
            msg = f"Expected frame of {expected} samples, got shape {audio_frame.shape}"
            raise ValueError(msg)

        # Normalise to float32 [-1, 1]
        if audio_frame.dtype == np.int16:
            audio = audio_frame.astype(np.float32) / 32768.0
        else:
            audio = audio_frame.astype(np.float32)

        # ONNX inference
        ort_inputs = {
            "input": audio.reshape(1, -1),
            "state": self._state,
            "sr": self._sr,
        }
        output, self._state = self._session.run(None, ort_inputs)[:2]
        probability = float(output[0][0])

        # Rolling window — append before the FSM tick so the enrichment
        # on a transition reflects the probabilities that *led to* it.
        rms = float(np.sqrt(np.mean(audio * audio))) if audio.size else 0.0
        self._prob_history.append(probability)
        self._rms_history.append(rms)

        # Per-frame telemetry (sampled by SamplingProcessor at the rate
        # set in ObservabilitySamplingConfig.vad_frame_rate). Operators
        # can disable sampling for live-debug by setting the rate to 0.
        logger.info(
            "voice.vad.frame",
            **{
                "voice.probability": round(probability, 4),
                "voice.rms": round(rms, 4),
                "voice.state": self._vad_state.name,
                "voice.onset_threshold": self._config.onset_threshold,
                "voice.offset_threshold": self._config.offset_threshold,
            },
        )

        # FSM transition — log every state change so operators can see
        # exactly when/why the orchestrator moved between silence and speech
        # without guessing from the absence of downstream events.
        prev_state = self._vad_state
        is_speech = self._update_state(probability)
        if self._vad_state != prev_state:
            logger.info(
                "vad_state_transition",
                from_state=prev_state.name,
                to_state=self._vad_state.name,
                probability=round(probability, 3),
            )
            logger.info(
                "voice.vad.state_changed",
                **{
                    "voice.from_state": prev_state.name,
                    "voice.to_state": self._vad_state.name,
                    "voice.probability": round(probability, 4),
                    "voice.rms": round(rms, 4),
                    "voice.onset_threshold": self._config.onset_threshold,
                    "voice.offset_threshold": self._config.offset_threshold,
                    "voice.prob_window": [round(p, 4) for p in self._prob_history],
                    "voice.rms_window": [round(r, 4) for r in self._rms_history],
                },
            )

        return VADEvent(
            is_speech=is_speech,
            probability=probability,
            state=self._vad_state,
        )

    def reset(self) -> None:
        """Reset LSTM and FSM state (call between conversations)."""
        import numpy as np  # noqa: F811

        self._state = np.zeros(_LSTM_STATE_SHAPE, dtype=np.float32)
        self._vad_state = VADState.SILENCE
        self._consecutive_count = 0
        self._prob_history.clear()
        self._rms_history.clear()

    @property
    def state(self) -> VADState:
        """Current FSM state."""
        return self._vad_state

    @property
    def is_speaking(self) -> bool:
        """Whether the FSM considers speech ongoing."""
        return self._vad_state in (VADState.SPEECH, VADState.SPEECH_OFFSET)

    @property
    def config(self) -> VADConfig:
        """Active configuration (read-only)."""
        return self._config

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _update_state(self, probability: float) -> bool:  # noqa: PLR0911
        """Advance the hysteresis FSM and return whether speech is active."""
        if self._vad_state == VADState.SILENCE:
            if probability > self._config.onset_threshold:
                self._consecutive_count = 1
                if self._consecutive_count >= self._config.min_onset_frames:
                    self._vad_state = VADState.SPEECH
                    self._consecutive_count = 0
                    return True
                self._vad_state = VADState.SPEECH_ONSET
            return False

        if self._vad_state == VADState.SPEECH_ONSET:
            if probability > self._config.onset_threshold:
                self._consecutive_count += 1
                if self._consecutive_count >= self._config.min_onset_frames:
                    self._vad_state = VADState.SPEECH
                    self._consecutive_count = 0
                    return True
            else:
                # False alarm — back to silence
                self._vad_state = VADState.SILENCE
                self._consecutive_count = 0
            return False

        if self._vad_state == VADState.SPEECH:
            if probability < self._config.offset_threshold:
                self._consecutive_count = 1
                if self._consecutive_count >= self._config.min_offset_frames:
                    self._vad_state = VADState.SILENCE
                    self._consecutive_count = 0
                    return False
                self._vad_state = VADState.SPEECH_OFFSET
            return True

        if self._vad_state == VADState.SPEECH_OFFSET:
            if probability < self._config.offset_threshold:
                self._consecutive_count += 1
                if self._consecutive_count >= self._config.min_offset_frames:
                    self._vad_state = VADState.SILENCE
                    self._consecutive_count = 0
                    return False
            else:
                # Speech resumed
                self._vad_state = VADState.SPEECH
                self._consecutive_count = 0
            return True

        return False  # pragma: no cover — unreachable with current enum
