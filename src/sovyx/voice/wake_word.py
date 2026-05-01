"""WakeWordDetector with OpenWakeWord 2-stage verification.

Stage 1: OpenWakeWord ONNX model detects "hey sovyx" (fast, may false-positive).
Stage 2: Verify with STT transcription of the same audio segment.
Two-stage verification reduces false positives ~10x (IMPL-004 §ADR).

Architecture (3-layer ONNX):
    Audio → MelSpectrogram (ONNX) → Feature Embedding (ONNX) → Wake Word Model (ONNX)

Ref: SPE-010 §4, IMPL-004 §2.5 (OpenWakeWord 2-stage)
"""

from __future__ import annotations

import time
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
# Phase 7 / T7.8 — adaptive cooldown. When enabled, the cooldown
# duration adjusts based on recent false-fire density: if the
# orchestrator reported ≥ ``cooldown_adaptive_threshold`` false-fires
# within ``cooldown_adaptive_window_s``, use ``cooldown_max_seconds``;
# otherwise use ``cooldown_min_seconds``. Default disabled
# (``cooldown_adaptive_enabled=False``) preserves the legacy fixed
# 2 s cooldown. Operator opt-in via explicit WakeWordConfig
# construction; default-flip planned post-T7.7 pilot data.
_DEFAULT_COOLDOWN_MIN_S = 2.0
_DEFAULT_COOLDOWN_MAX_S = 5.0
_DEFAULT_COOLDOWN_ADAPTIVE_WINDOW_S = 60.0
_DEFAULT_COOLDOWN_ADAPTIVE_THRESHOLD = 2
# Phase 7 / T7.4 — fast-path threshold. When stage-1 score crosses
# this value, skip stage-2 entirely and emit ``WakeWordDetectedEvent``
# on the same frame. Default 1.0 = DISABLED (no real OpenWakeWord
# score reaches 1.0 in practice; max ~0.99). Operators opt-in by
# constructing ``WakeWordConfig(stage1_high_confidence_threshold=0.8)``
# after piloting the false-fire rate per the backlog. Default-flip
# to 0.8 planned for v0.30.0 after one minor cycle of operator
# pilot data validates the false-fire rate stays below the v0.23.x
# baseline (per ``feedback_staged_adoption``).
_DEFAULT_STAGE1_HIGH_CONFIDENCE_THRESHOLD = 1.0

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

    stage1_high_confidence_threshold: float = _DEFAULT_STAGE1_HIGH_CONFIDENCE_THRESHOLD
    """Phase 7 / T7.4 — score above which stage-2 is skipped entirely.

    When a frame scores >= this threshold AND >= ``stage1_threshold``,
    the detector emits a confirmed ``WakeWordDetectedEvent`` on the
    SAME frame, bypassing the stage-2 collection window + STT verifier
    call. Cuts end-to-end latency from ~1500 ms (collection) + verifier
    to ~5 ms (single ONNX inference) for high-confidence detections.

    Default 1.0 = DISABLED (no OpenWakeWord score reaches 1.0 in
    practice; max ~0.99). Operators pilot via explicit construction:
    ``WakeWordConfig(stage1_high_confidence_threshold=0.8)``. Pilot
    target: false-fire rate stays below the v0.23.x 2-stage baseline
    while p95 detection_latency drops from ~1700 ms to ~80 ms (one
    frame at 80 ms).

    Constraint: must be > ``stage1_threshold`` (else fast-path would
    fire on every stage-1 trigger, defeating the high-confidence
    contract). Validation enforces this at construction time.
    """

    cooldown_seconds: float = _DEFAULT_COOLDOWN_S
    """Seconds to ignore after a confirmed detection.

    Used as the static cooldown when ``cooldown_adaptive_enabled``
    is False (the default — preserves legacy behaviour). When
    adaptive is True, this field is ignored in favour of the
    ``cooldown_min_seconds`` / ``cooldown_max_seconds`` pair.
    """

    cooldown_adaptive_enabled: bool = False
    """Phase 7 / T7.8 — gate the adaptive-cooldown behaviour.

    When False (default): legacy fixed cooldown via ``cooldown_seconds``.
    When True: cooldown duration shifts between
    ``cooldown_min_seconds`` (clean recent history) and
    ``cooldown_max_seconds`` (dense recent false-fires) based on
    the rolling window the orchestrator drives via
    :meth:`WakeWordDetector.note_false_fire`. Default disabled per
    ``feedback_staged_adoption`` — operators opt in via explicit
    construction after piloting T7.7's false-fire counter to confirm
    the threshold is calibrated for their environment.
    """

    cooldown_min_seconds: float = _DEFAULT_COOLDOWN_MIN_S
    """Adaptive cooldown floor — used when recent false-fires < threshold."""

    cooldown_max_seconds: float = _DEFAULT_COOLDOWN_MAX_S
    """Adaptive cooldown ceiling — used when recent false-fires ≥ threshold."""

    cooldown_adaptive_window_seconds: float = _DEFAULT_COOLDOWN_ADAPTIVE_WINDOW_S
    """Sliding-window length over which false-fires are counted."""

    cooldown_adaptive_threshold: int = _DEFAULT_COOLDOWN_ADAPTIVE_THRESHOLD
    """Number of false-fires within the window to switch to max cooldown."""

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
    # T7.4 — fast-path threshold must lie strictly above stage1_threshold
    # (else fast-path fires on every stage-1 trigger, defeating the
    # high-confidence contract) and at most 1.0 (the OpenWakeWord
    # score domain). Default 1.0 = disabled is permitted as the
    # upper-bound sentinel.
    if not (
        config.stage1_threshold < config.stage1_high_confidence_threshold <= 1.0  # noqa: PLR2004 — the sigmoid output domain ceiling
    ):
        msg = (
            f"stage1_high_confidence_threshold must be in "
            f"(stage1_threshold={config.stage1_threshold}, 1.0], got "
            f"{config.stage1_high_confidence_threshold}"
        )
        raise ValueError(msg)
    # T7.8 — adaptive cooldown bounds + window must be sensible.
    # Only validated when adaptive is enabled (the static path doesn't
    # consult these fields).
    if config.cooldown_adaptive_enabled:
        if config.cooldown_min_seconds < 0:
            msg = f"cooldown_min_seconds must be >= 0, got {config.cooldown_min_seconds}"
            raise ValueError(msg)
        if config.cooldown_max_seconds < config.cooldown_min_seconds:
            msg = (
                f"cooldown_max_seconds ({config.cooldown_max_seconds}) "
                f"must be >= cooldown_min_seconds "
                f"({config.cooldown_min_seconds})"
            )
            raise ValueError(msg)
        if config.cooldown_adaptive_window_seconds <= 0:
            msg = (
                f"cooldown_adaptive_window_seconds must be > 0, got "
                f"{config.cooldown_adaptive_window_seconds}"
            )
            raise ValueError(msg)
        if config.cooldown_adaptive_threshold < 1:
            msg = (
                f"cooldown_adaptive_threshold must be >= 1, got "
                f"{config.cooldown_adaptive_threshold}"
            )
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

        # T7.1 — wake-word latency profile. Records when STAGE1_TRIGGERED
        # was entered so _evaluate_stage2 can compute the wall-clock
        # collection-window duration + the end-to-end stage-1-trigger
        # → confirmed-detection latency. ``None`` between detections.
        self._stage1_trigger_monotonic: float | None = None

        # T7.8 — false-fire timestamps for the adaptive-cooldown
        # sliding window. Orchestrator calls
        # :meth:`note_false_fire` from each false-fire site;
        # _enter_cooldown reads + prunes this list to compute the
        # effective cooldown duration. List is bounded by the
        # window-length pruning in _prune_false_fires so it can't
        # grow without bound on a daemon left running for days.
        self._false_fire_monotonics: list[float] = []
        # T7.8 — current cycle's effective cooldown in frames.
        # Initialised to the static ``cooldown_frames`` so
        # ``_handle_cooldown`` works before any detection lands;
        # _enter_cooldown overrides this on every transition.
        self._effective_cooldown_frames = self._config.cooldown_frames

        # Audio buffer for stage-2 verification
        self._audio_buffer: list[npt.NDArray[np.float32]] = []

        # Telemetry attribution: model file stem identifies the wake-word
        # variant in dashboards even if the same detector is reused with
        # multiple ONNX checkpoints across boots.
        self._model_path = str(model_path)
        self._model_name = model_path.stem
        self._frame_ms = int(self._config.frame_samples * 1000 / self._config.sample_rate)

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

        # Per-frame telemetry (sampled by SamplingProcessor at the rate
        # set in ObservabilitySamplingConfig.wake_word_score_rate). The
        # cooldown_ms_remaining field is non-zero only while we're
        # actively suppressing — every other frame reports 0 so the
        # dashboard can render cooldown windows as solid bands.
        cooldown_ms_remaining = 0
        if self._state == WakeWordState.COOLDOWN:
            remaining_frames = max(0, self._config.cooldown_frames - self._frame_counter)
            cooldown_ms_remaining = remaining_frames * self._frame_ms
        logger.info(
            "voice.wake_word.score",
            **{
                "voice.score": round(score, 4),
                "voice.threshold": self._config.stage1_threshold,
                "voice.stage2_threshold": self._config.stage2_threshold,
                "voice.cooldown_ms_remaining": cooldown_ms_remaining,
                "voice.state": self._state.name,
                "voice.model_name": self._model_name,
            },
        )

        # State machine transition
        detected = self._update_state(score, audio)

        return WakeWordEvent(
            detected=detected,
            score=score,
            state=self._state,
        )

    def reset(self) -> None:
        """Reset detector state (call between conversations).

        Note: ``_false_fire_monotonics`` is intentionally NOT cleared
        on reset. Adaptive cooldown's sliding window spans
        cross-detection history (the whole point: "have we had
        recent false-fires?") so a reset between turns must preserve
        the rolling state. Pruning happens age-based in
        :meth:`_prune_false_fires`.
        """
        self._state = WakeWordState.IDLE
        self._frame_counter = 0
        self._peak_score = 0.0
        self._audio_buffer.clear()
        self._stage1_trigger_monotonic = None

    def note_false_fire(self, *, monotonic_now: float | None = None) -> None:
        """Record a false-fire signal from the orchestrator.

        Phase 7 / T7.8 — orchestrator calls this from each of its 3
        false-fire emission sites (empty_transcription /
        rejected_transcription / sub_confidence). The detector uses
        the timestamp to drive adaptive cooldown: dense recent
        false-fires push the cooldown to ``cooldown_max_seconds``,
        clean history keeps it at ``cooldown_min_seconds``. When
        ``cooldown_adaptive_enabled=False`` the call is a no-op
        (signals are recorded but never consulted) — this is
        intentional so the orchestrator can call unconditionally
        without checking the flag.

        Args:
            monotonic_now: Override timestamp (tests). Defaults to
                ``time.monotonic()``.
        """
        ts = monotonic_now if monotonic_now is not None else time.monotonic()
        self._false_fire_monotonics.append(ts)
        # Prune-on-add bounds memory regardless of the daemon's
        # uptime. The window shifts forward continuously so any
        # entry past the configured horizon is dead weight.
        self._prune_false_fires(now=ts)

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
        """Run ONNX model and return wake word score.

        T7.1 latency profile: every call records the ONNX inference
        duration to ``sovyx.voice.wake_word.stage1_inference_latency``
        keyed by ``model_name``. Per-frame at the 80 ms frame cadence
        (~12.5 Hz) — the measured budget against which the v0.30.0 GA
        target ``wake-word p95 ≤ 500 ms end-to-end`` is built.
        """
        from sovyx.voice.health._metrics import (  # noqa: PLC0415 — metrics import is hot-path; lazy keeps module-load cost off non-voice daemons
            record_wake_word_stage1_inference_ms,
        )

        t0 = time.monotonic()
        ort_inputs = {
            self._session.get_inputs()[0].name: audio.reshape(1, -1),
        }
        outputs = self._session.run(None, ort_inputs)
        score = float(outputs[0][0][0]) if outputs[0].ndim > 1 else float(outputs[0][0])
        record_wake_word_stage1_inference_ms(
            duration_ms=(time.monotonic() - t0) * 1000.0,
            model_name=self._model_name,
        )
        return score

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
        """Handle IDLE state — watch for stage-1 trigger.

        T7.4 fast-path: when ``score >= stage1_high_confidence_threshold``
        the detector skips stage-2 entirely and emits a confirmed
        detection on the same frame. The fast path is GATED on
        ``stage1_high_confidence_threshold < 1.0`` (the default 1.0
        is the disabled sentinel — no OpenWakeWord score reaches it
        in practice). Operators opt in via explicit
        ``WakeWordConfig(stage1_high_confidence_threshold=0.8)``.
        """
        if score >= self._config.stage1_threshold:
            # T7.4 — fast-path: skip stage-2 when the score is high enough
            # that the false-positive rate is already acceptable without
            # STT verification. Saves ~1500 ms of collection + verifier
            # latency for high-confidence detections.
            if score >= self._config.stage1_high_confidence_threshold:
                from sovyx.voice.health._metrics import (  # noqa: PLC0415
                    record_wake_word_confidence,
                    record_wake_word_detection_ms,
                    record_wake_word_fast_path_engaged,
                )

                self._stage1_trigger_monotonic = time.monotonic()
                self._peak_score = score
                # End-to-end latency for the fast path is essentially
                # the single ONNX frame inference time — record 0.0
                # for the post-trigger overhead so the histogram
                # captures this path correctly. Real wall-clock
                # contribution from this method is negligible.
                record_wake_word_detection_ms(duration_ms=0.0)
                record_wake_word_fast_path_engaged(score=score)
                record_wake_word_confidence(
                    score=score,
                    detection_path="fast_path",
                )
                logger.info(
                    "Wake word CONFIRMED (T7.4 fast-path)",
                    score=score,
                    threshold=self._config.stage1_high_confidence_threshold,
                )
                logger.info(
                    "voice.wake_word.detected",
                    **{
                        "voice.score": round(score, 4),
                        "voice.model_name": self._model_name,
                        "voice.stage1_threshold": self._config.stage1_threshold,
                        "voice.stage2_threshold": self._config.stage2_threshold,
                        "voice.transcription": "<fast-path>",
                        "voice.window_frames": 1,
                        "voice.stage2_collection_ms": 0.0,
                        "voice.stage2_verifier_ms": 0.0,
                        "voice.detection_ms": 0.0,
                        "voice.fast_path": True,
                    },
                )
                self._enter_cooldown()
                return True

            self._state = WakeWordState.STAGE1_TRIGGERED
            self._frame_counter = 1
            self._peak_score = score
            self._audio_buffer = [audio.copy()]
            # T7.1 — anchor stage-1 trigger time for the latency
            # histograms recorded in _evaluate_stage2.
            self._stage1_trigger_monotonic = time.monotonic()
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
        """Evaluate stage-2: check peak threshold + run verifier.

        T7.1 latency profile: emits 3 histograms per evaluation.
        ``stage2_collection_latency`` records wall-clock from
        STAGE1_TRIGGERED entry to this call (every evaluation,
        keyed by outcome). ``stage2_verifier_latency`` records the
        verifier callable duration (only when peak_score crosses
        the threshold). ``detection_latency`` records the end-to-
        end stage-1-trigger → confirmed-detection time (only on
        ``verified=True`` returns).
        """
        import numpy as np  # noqa: F811

        from sovyx.voice.health._metrics import (  # noqa: PLC0415 — keep voice metrics off non-voice daemon import path
            record_wake_word_confidence,
            record_wake_word_detection_ms,
            record_wake_word_stage2_collection_ms,
            record_wake_word_stage2_verifier_ms,
        )

        # T7.1 — collection-latency wall clock. ``_stage1_trigger_monotonic``
        # is set in _handle_idle when STAGE1_TRIGGERED entered; if it's
        # None (defensive — _evaluate_stage2 should never be called
        # without a prior _handle_idle), default to 0 ms so the metric
        # records but is obvious as a calibration artifact.
        now = time.monotonic()
        collection_ms = (
            (now - self._stage1_trigger_monotonic) * 1000.0
            if self._stage1_trigger_monotonic is not None
            else 0.0
        )

        if self._peak_score >= self._config.stage2_threshold:
            combined_audio = np.concatenate(self._audio_buffer)
            verifier_t0 = time.monotonic()
            result = self._verifier(combined_audio)
            verifier_ms = (time.monotonic() - verifier_t0) * 1000.0
            record_wake_word_stage2_verifier_ms(
                duration_ms=verifier_ms,
                outcome="verified" if result.verified else "rejected",
            )

            if result.verified:
                detection_ms = (
                    (time.monotonic() - self._stage1_trigger_monotonic) * 1000.0
                    if self._stage1_trigger_monotonic is not None
                    else 0.0
                )
                record_wake_word_stage2_collection_ms(
                    duration_ms=collection_ms,
                    outcome="confirmed",
                )
                record_wake_word_detection_ms(duration_ms=detection_ms)
                record_wake_word_confidence(
                    score=self._peak_score,
                    detection_path="two_stage",
                )
                logger.info(
                    "Wake word CONFIRMED (2-stage)",
                    peak_score=self._peak_score,
                    transcription=result.transcription,
                )
                logger.info(
                    "voice.wake_word.detected",
                    **{
                        "voice.score": round(self._peak_score, 4),
                        "voice.model_name": self._model_name,
                        "voice.stage1_threshold": self._config.stage1_threshold,
                        "voice.stage2_threshold": self._config.stage2_threshold,
                        "voice.transcription": result.transcription,
                        "voice.window_frames": self._frame_counter,
                        # T7.1 — surface the same numbers on the
                        # structured event so dashboards can render the
                        # per-detection breakdown without scraping the
                        # OTel histograms.
                        "voice.stage2_collection_ms": round(collection_ms, 2),
                        "voice.stage2_verifier_ms": round(verifier_ms, 2),
                        "voice.detection_ms": round(detection_ms, 2),
                    },
                )
                self._enter_cooldown()
                return True

            record_wake_word_stage2_collection_ms(
                duration_ms=collection_ms,
                outcome="rejected_verifier",
            )
            logger.debug(
                "Wake word stage-2 REJECTED",
                peak_score=self._peak_score,
                transcription=result.transcription,
            )
        else:
            record_wake_word_stage2_collection_ms(
                duration_ms=collection_ms,
                outcome="rejected_threshold",
            )
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
        self._stage1_trigger_monotonic = None
        return False

    def _handle_cooldown(self, score: float) -> bool:
        """Handle COOLDOWN state — ignore detections for cooldown period.

        T7.8: consults ``_effective_cooldown_frames`` (set by
        :meth:`_enter_cooldown` from the adaptive computation) instead
        of the static ``self._config.cooldown_frames`` so the
        recently-elevated cooldown stays in effect for the full
        adaptive window.
        """
        _ = score  # Ignored during cooldown
        self._frame_counter += 1
        if self._frame_counter >= self._effective_cooldown_frames:
            self._state = WakeWordState.IDLE
            self._frame_counter = 0
            logger.debug("Wake word cooldown ended")
        return False

    def _prune_false_fires(self, *, now: float) -> None:
        """Drop false-fire timestamps older than the adaptive window.

        Constant-time amortised: the list is append-only in entry
        order, so old entries cluster at the front. We could use a
        deque for O(1) popleft, but for the practical density (a few
        false-fires per minute over a 60 s window = ~5 entries) a
        list with slicing is simpler and faster.
        """
        cutoff = now - self._config.cooldown_adaptive_window_seconds
        # Find first index whose timestamp is within the window.
        kept_start = 0
        for ts in self._false_fire_monotonics:
            if ts >= cutoff:
                break
            kept_start += 1
        if kept_start > 0:
            self._false_fire_monotonics = self._false_fire_monotonics[kept_start:]

    def _adaptive_cooldown_seconds(self) -> float:
        """Compute the effective cooldown duration for the current cycle.

        Static path (``cooldown_adaptive_enabled=False``): returns the
        configured ``cooldown_seconds`` unchanged — legacy behaviour.

        Adaptive path: prunes the false-fire window then picks
        ``cooldown_max_seconds`` if the window has ≥ threshold
        entries, otherwise ``cooldown_min_seconds``.
        """
        if not self._config.cooldown_adaptive_enabled:
            return self._config.cooldown_seconds
        now = time.monotonic()
        self._prune_false_fires(now=now)
        if len(self._false_fire_monotonics) >= self._config.cooldown_adaptive_threshold:
            return self._config.cooldown_max_seconds
        return self._config.cooldown_min_seconds

    def _enter_cooldown(self) -> None:
        """Transition to COOLDOWN state.

        T7.8: when ``cooldown_adaptive_enabled``, the cooldown
        duration is recomputed per entry from the recent false-fire
        density. Stored on ``self._effective_cooldown_frames`` so
        ``_handle_cooldown`` consults it instead of the static
        ``cooldown_frames`` property. The static path keeps the
        legacy field-only behaviour for back-compat.
        """
        cooldown_s = self._adaptive_cooldown_seconds()
        self._effective_cooldown_frames = int(
            cooldown_s * self._config.sample_rate / _FRAME_SAMPLES,
        )
        self._state = WakeWordState.COOLDOWN
        self._frame_counter = 0
        self._peak_score = 0.0
        self._audio_buffer.clear()
        self._stage1_trigger_monotonic = None
