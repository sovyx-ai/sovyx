"""WakeWordDetector with OpenWakeWord 2-stage verification.

Stage 1: OpenWakeWord ONNX model detects "hey sovyx" (fast, may false-positive).
Stage 2: Verify with STT transcription of the same audio segment.
Two-stage verification reduces false positives ~10x (IMPL-004 §ADR).

Architecture (3-layer ONNX):
    Audio → MelSpectrogram (ONNX) → Feature Embedding (ONNX) → Wake Word Model (ONNX)

Ref: SPE-010 §4, IMPL-004 §2.5 (OpenWakeWord 2-stage)
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import IntEnum, auto
from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from pathlib import Path

    import numpy as np
    import numpy.typing as npt

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SAMPLE_RATE = 16000
_FRAME_SAMPLES = 1280  # 80ms at 16kHz — OpenWakeWord input size
_DEFAULT_STAGE1_THRESHOLD = 0.5
_DEFAULT_STAGE2_THRESHOLD = 0.7
_DEFAULT_STAGE2_WINDOW_S = 1.5
_DEFAULT_COOLDOWN_S = 2.0

# Variants for STT verification (stage 2)
_WAKE_VARIANTS: frozenset[str] = frozenset(
    {
        "sovyx",
        "so vyx",
        "sovix",
        "hey sovyx",
        "hey so vyx",
        "hey sovix",
        "soyvix",
        "hey soyvix",
    }
)


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


class WakeWordState(IntEnum):
    """Detector state machine.

    IDLE: listening for wake word
    STAGE1_TRIGGERED: stage 1 fired, collecting audio for stage 2
    COOLDOWN: recently triggered, ignoring for cooldown period
    """

    IDLE = auto()
    STAGE1_TRIGGERED = auto()
    COOLDOWN = auto()


@dataclass(frozen=True, slots=True)
class WakeWordEvent:
    """Result of processing a single audio frame."""

    detected: bool
    """Whether a verified wake word was detected this frame."""

    score: float
    """Raw OpenWakeWord score for this frame (0.0–1.0)."""

    state: WakeWordState
    """Detector state after processing."""


@dataclass(frozen=True, slots=True)
class WakeWordConfig:
    """Configuration for the 2-stage wake word detector.

    Defaults calibrated per IMPL-004 §2.5.
    """

    stage1_threshold: float = _DEFAULT_STAGE1_THRESHOLD
    """Score above which stage 1 fires (fast detection)."""

    stage2_threshold: float = _DEFAULT_STAGE2_THRESHOLD
    """Score above which stage 2 confirms (higher bar)."""

    stage2_window_seconds: float = _DEFAULT_STAGE2_WINDOW_S
    """Seconds of audio to buffer for stage-2 STT verification."""

    cooldown_seconds: float = _DEFAULT_COOLDOWN_S
    """Seconds to ignore after a confirmed detection."""

    sample_rate: int = _SAMPLE_RATE
    """Audio sample rate — must be 16000."""

    wake_variants: frozenset[str] = _WAKE_VARIANTS
    """Acceptable transcription variants for STT verification."""

    @property
    def frame_samples(self) -> int:
        """Frame size in samples (1280 = 80ms at 16kHz)."""
        return _FRAME_SAMPLES

    @property
    def stage2_window_frames(self) -> int:
        """Number of frames in stage-2 collection window."""
        return int(self.stage2_window_seconds * self.sample_rate / _FRAME_SAMPLES)

    @property
    def cooldown_frames(self) -> int:
        """Number of frames for cooldown period."""
        return int(self.cooldown_seconds * self.sample_rate / _FRAME_SAMPLES)


def _validate_config(config: WakeWordConfig) -> None:
    """Raise ``ValueError`` for invalid config values."""
    if config.stage1_threshold <= 0.0 or config.stage1_threshold >= 1.0:
        msg = f"stage1_threshold must be in (0, 1), got {config.stage1_threshold}"
        raise ValueError(msg)
    if config.stage2_threshold <= 0.0 or config.stage2_threshold >= 1.0:
        msg = f"stage2_threshold must be in (0, 1), got {config.stage2_threshold}"
        raise ValueError(msg)
    if config.stage2_threshold < config.stage1_threshold:
        msg = (
            f"stage2_threshold ({config.stage2_threshold}) must be >= "
            f"stage1_threshold ({config.stage1_threshold}) for 2-stage verify"
        )
        raise ValueError(msg)
    if config.stage2_window_seconds <= 0:
        msg = f"stage2_window_seconds must be > 0, got {config.stage2_window_seconds}"
        raise ValueError(msg)
    if config.cooldown_seconds < 0:
        msg = f"cooldown_seconds must be >= 0, got {config.cooldown_seconds}"
        raise ValueError(msg)
    if config.sample_rate != _SAMPLE_RATE:
        msg = f"Only 16000 Hz supported, got {config.sample_rate}"
        raise ValueError(msg)


# ---------------------------------------------------------------------------
# Verifier protocol
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """Outcome of stage-2 STT verification."""

    verified: bool
    """Whether the transcription matches a wake variant."""

    transcription: str
    """What the STT returned."""


# Type for the stage-2 verifier callback
# Takes audio array, returns VerificationResult
VerifierFn = Callable[["npt.NDArray[np.float32]"], VerificationResult]


def default_verifier(
    wake_variants: frozenset[str],
) -> VerifierFn:
    """Create a verifier that always returns verified=True (no STT available).

    In production, replace with STT-backed verifier. This is the fallback
    when no STT engine is configured (stage 1 only).
    """

    def _verify(audio: npt.NDArray[np.float32]) -> VerificationResult:
        _ = audio
        _ = wake_variants
        return VerificationResult(verified=True, transcription="<no-stt>")

    return _verify


def create_stt_verifier(
    transcribe_fn: Callable[[npt.NDArray[np.float32]], str],
    wake_variants: frozenset[str],
) -> VerifierFn:
    """Create a verifier backed by an STT transcription function.

    Args:
        transcribe_fn: Function that takes float32 audio → text.
        wake_variants: Set of acceptable transcription substrings.

    Returns:
        A :data:`VerifierFn` for use with :class:`WakeWordDetector`.
    """

    def _verify(audio: npt.NDArray[np.float32]) -> VerificationResult:
        text = transcribe_fn(audio).lower()
        matched = any(variant in text for variant in wake_variants)
        return VerificationResult(verified=matched, transcription=text)

    return _verify


# ---------------------------------------------------------------------------
# WakeWordDetector
# ---------------------------------------------------------------------------


class WakeWordDetector:
    """OpenWakeWord detector with 2-stage verification.

    Stage 1: ONNX model scores each audio frame. If score > stage1_threshold
    AND peak score > stage2_threshold during collection window, proceed.
    Stage 2: Buffer audio during collection window, then verify via STT.

    The detector is CPU-efficient (~5ms/frame on Pi 5) and operates on
    1280-sample frames (80ms at 16kHz).

    Usage::

        detector = WakeWordDetector(model_path, verifier=my_stt_verifier)
        for frame in audio_stream:
            event = detector.process_frame(frame)
            if event.detected:
                # Wake word confirmed — start listening for command
                ...
    """

    def __init__(
        self,
        model_path: Path,
        config: WakeWordConfig | None = None,
        verifier: VerifierFn | None = None,
    ) -> None:
        import onnxruntime as ort

        self._config = config or WakeWordConfig()
        _validate_config(self._config)

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 2  # Pi5: 2 threads for wake word
        opts.inter_op_num_threads = 1
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

        self._session = ort.InferenceSession(
            str(model_path),
            sess_options=opts,
            providers=["CPUExecutionProvider"],
        )

        self._verifier: VerifierFn = verifier or default_verifier(self._config.wake_variants)

        # State machine
        self._state = WakeWordState.IDLE
        self._frame_counter = 0  # counts frames in current state
        self._peak_score = 0.0  # highest score during stage-1 collection

        # Audio buffer for stage-2 verification
        self._audio_buffer: list[npt.NDArray[np.float32]] = []

        logger.info(
            "WakeWordDetector initialised",
            model=str(model_path),
            stage1_threshold=self._config.stage1_threshold,
            stage2_threshold=self._config.stage2_threshold,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def process_frame(
        self,
        audio_frame: npt.NDArray[np.float32] | npt.NDArray[np.int16],
    ) -> WakeWordEvent:
        """Process a single audio frame (1280 samples at 16kHz).

        Args:
            audio_frame: Exactly 1280 samples — float32 [-1, 1] or int16.

        Returns:
            A :class:`WakeWordEvent` with detection flag, score, and state.

        Raises:
            ValueError: If frame has wrong length.
        """
        import numpy as np  # noqa: F811

        expected = self._config.frame_samples
        if audio_frame.shape != (expected,):
            msg = f"Expected frame of {expected} samples, got shape {audio_frame.shape}"
            raise ValueError(msg)

        # Normalise to float32
        if audio_frame.dtype == np.int16:
            audio = audio_frame.astype(np.float32) / 32768.0
        else:
            audio = audio_frame.astype(np.float32)

        # Get ONNX score
        score = self._run_inference(audio)

        # State machine transition
        detected = self._update_state(score, audio)

        return WakeWordEvent(
            detected=detected,
            score=score,
            state=self._state,
        )

    def reset(self) -> None:
        """Reset detector state (call between conversations)."""
        self._state = WakeWordState.IDLE
        self._frame_counter = 0
        self._peak_score = 0.0
        self._audio_buffer.clear()

    @property
    def state(self) -> WakeWordState:
        """Current detector state."""
        return self._state

    @property
    def config(self) -> WakeWordConfig:
        """Active configuration (read-only)."""
        return self._config

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _run_inference(self, audio: npt.NDArray[np.float32]) -> float:
        """Run ONNX model and return wake word score."""
        ort_inputs = {
            self._session.get_inputs()[0].name: audio.reshape(1, -1),
        }
        outputs = self._session.run(None, ort_inputs)
        return float(outputs[0][0][0]) if outputs[0].ndim > 1 else float(outputs[0][0])

    def _update_state(
        self,
        score: float,
        audio: npt.NDArray[np.float32],
    ) -> bool:
        """Advance the state machine. Returns True if wake word verified."""
        if self._state == WakeWordState.COOLDOWN:
            return self._handle_cooldown(score)

        if self._state == WakeWordState.IDLE:
            return self._handle_idle(score, audio)

        if self._state == WakeWordState.STAGE1_TRIGGERED:
            return self._handle_stage1(score, audio)

        return False  # pragma: no cover

    def _handle_idle(
        self,
        score: float,
        audio: npt.NDArray[np.float32],
    ) -> bool:
        """Handle IDLE state — watch for stage-1 trigger."""
        if score >= self._config.stage1_threshold:
            self._state = WakeWordState.STAGE1_TRIGGERED
            self._frame_counter = 1
            self._peak_score = score
            self._audio_buffer = [audio.copy()]
            logger.debug("Wake word stage-1 triggered", score=score)
            # If window is just 1 frame, evaluate immediately
            if self._frame_counter >= self._config.stage2_window_frames:
                return self._evaluate_stage2()
        return False

    def _handle_stage1(
        self,
        score: float,
        audio: npt.NDArray[np.float32],
    ) -> bool:
        """Handle STAGE1_TRIGGERED — collecting audio, tracking peak score."""
        self._frame_counter += 1
        self._audio_buffer.append(audio.copy())
        self._peak_score = max(self._peak_score, score)

        # Check if collection window expired
        if self._frame_counter >= self._config.stage2_window_frames:
            return self._evaluate_stage2()

        return False

    def _evaluate_stage2(self) -> bool:
        """Evaluate stage-2: check peak threshold + run verifier."""
        import numpy as np  # noqa: F811

        if self._peak_score >= self._config.stage2_threshold:
            combined_audio = np.concatenate(self._audio_buffer)
            result = self._verifier(combined_audio)

            if result.verified:
                logger.info(
                    "Wake word CONFIRMED (2-stage)",
                    peak_score=self._peak_score,
                    transcription=result.transcription,
                )
                self._enter_cooldown()
                return True

            logger.debug(
                "Wake word stage-2 REJECTED",
                peak_score=self._peak_score,
                transcription=result.transcription,
            )
        else:
            logger.debug(
                "Wake word stage-2 threshold not met",
                peak_score=self._peak_score,
                required=self._config.stage2_threshold,
            )

        # Reset to IDLE
        self._state = WakeWordState.IDLE
        self._frame_counter = 0
        self._peak_score = 0.0
        self._audio_buffer.clear()
        return False

    def _handle_cooldown(self, score: float) -> bool:
        """Handle COOLDOWN state — ignore detections for cooldown period."""
        _ = score  # Ignored during cooldown
        self._frame_counter += 1
        if self._frame_counter >= self._config.cooldown_frames:
            self._state = WakeWordState.IDLE
            self._frame_counter = 0
            logger.debug("Wake word cooldown ended")
        return False

    def _enter_cooldown(self) -> None:
        """Transition to COOLDOWN state."""
        self._state = WakeWordState.COOLDOWN
        self._frame_counter = 0
        self._peak_score = 0.0
        self._audio_buffer.clear()
