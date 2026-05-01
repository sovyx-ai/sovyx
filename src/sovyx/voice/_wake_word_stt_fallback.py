"""STT-based fallback wake-word detector — Phase 8 / T8.17-T8.19.

Alternative wake-word detection path that runs STT over a rolling
audio buffer instead of an ONNX model. Used when a mind has no
trained ONNX wake-word checkpoint yet — e.g., immediately after
the operator names a new mind via the (future) ``sovyx voice
train-wake-word`` CLI, before the T8.13 custom training pipeline
finishes producing a checkpoint.

The detector implements the same duck-type surface as
:class:`~sovyx.voice.wake_word.WakeWordDetector` so the
:class:`~sovyx.voice._wake_word_router.WakeWordRouter` can register
+ fan-out frames to STT-backed detectors transparently. T8.18 hot-
swap from STT to ONNX is the natural consequence: when training
completes, the operator calls
:meth:`WakeWordRouter.register_mind(mind_id, model_path=<new ONNX>)`
which replaces the prior STTWakeWordDetector with a real
WakeWordDetector — operators see the per-mind detection latency
drop from ~500 ms to ~80 ms without daemon restart.

Latency contract:
  - STT call cost: ~500 ms typical (Moonshine on CPU; cloud STTs
    ~200-400 ms).
  - Frames between STT calls: free (just buffer management +
    cooldown counter).
  - Detection fires after the STT call returns a transcript that
    matches any ``wake_variants`` entry (case + diacritic
    insensitive comparison via ASCII-fold).

Performance pattern: STT runs at most every
``stt_call_interval_frames`` frames (default 25 ≈ 2 s at 80 ms
per frame). Between calls, process_frame returns near-instantly
(buffer append + counter check). The 500 ms STT call cost is
incurred on the audio thread per master mission §Phase 8 / T8.17;
the audio backlog impact is bounded because the inter-call
spacing exceeds the call duration (25 × 80 ms = 2 s ≫ 500 ms).

T8.19 telemetry: every confirmed detection emits the counter
``sovyx.voice.wake_word.detection_method`` with attribute
``method=stt_fallback`` so operator dashboards can split slow-path
vs fast-path detection rates.

Reference: master mission ``MISSION-voice-final-skype-grade-2026.md``
§Phase 8 / T8.17-T8.19.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger
from sovyx.voice.wake_word import WakeWordEvent, WakeWordState

if TYPE_CHECKING:
    from collections.abc import Callable

    import numpy as np
    import numpy.typing as npt

logger = get_logger(__name__)

# Type alias for the synchronous transcribe wrapper. The detector
# is synchronous by contract (matches WakeWordDetector duck-type);
# operators bridge from async STT engines via a sync wrapper at
# construction time:
#
#     def sync_transcribe(audio: np.ndarray) -> str:
#         return asyncio.run(engine.transcribe(audio)).text
#
# This keeps the detector free of asyncio concerns + lets tests
# inject deterministic transcript producers.
TranscribeFn = "Callable[[npt.NDArray[np.float32]], str]"


@dataclass(frozen=True, slots=True)
class STTWakeWordConfig:
    """STT-based fallback configuration.

    Defaults calibrated for a 16 kHz / 1280-sample / 80 ms-per-frame
    pipeline producing 12.5 frames/s. Operators tune
    ``stt_call_interval_frames`` for their STT engine's latency
    budget — slower engines (cloud STT with 1 s round-trip) want
    larger intervals.
    """

    wake_variants: tuple[str, ...]
    """Acceptable transcript values that count as a wake match.

    Compared case-insensitive after ASCII-folding (Latin-1
    diacritics dropped — ``"Lúcia"`` matches transcripts of
    ``"lucia"``, ``"Lúcia"``, ``"LUCIA"``). Empty tuple disables
    detection entirely (the detector is a no-op).
    """

    buffer_seconds: float = 2.0
    """Rolling window of audio fed to the STT call.

    Must be ≥ the longest expected wake-word phrase duration.
    Default 2 s covers all common wake words ("Sovyx", "Hey Sovyx",
    "Lúcia", etc.). Audio older than this falls off the buffer.
    """

    stt_call_interval_frames: int = 25
    """Frames between STT invocations.

    Default 25 = ~2 s at 80 ms per frame. Tuning rationale:
    - Smaller interval = lower detection latency (fires sooner
      after wake word said) but higher STT compute cost.
    - Larger interval = lower compute cost but the detection can
      lag the wake by up to interval × frame_ms.
    """

    cooldown_seconds: float = 2.0
    """Seconds to ignore detections after a confirmed match.

    Mirrors ``WakeWordConfig.cooldown_seconds`` so the router's
    cross-detector cooldown semantics stay uniform regardless of
    which detector class fired.
    """

    sample_rate: int = 16000
    """Audio sample rate. Must be 16000 (matches WakeWordDetector
    contract + STT engines). Validated at construction."""

    frame_samples: int = 1280
    """Frame size in samples. Must be 1280 (matches WakeWordDetector
    contract). Other sizes would break the router fan-out's shape
    assumption."""


def _validate_stt_config(config: STTWakeWordConfig) -> None:
    """Raise ``ValueError`` for invalid config values."""
    if config.buffer_seconds <= 0:
        msg = f"buffer_seconds must be > 0, got {config.buffer_seconds}"
        raise ValueError(msg)
    if config.stt_call_interval_frames < 1:
        msg = f"stt_call_interval_frames must be >= 1, got {config.stt_call_interval_frames}"
        raise ValueError(msg)
    if config.cooldown_seconds < 0:
        msg = f"cooldown_seconds must be >= 0, got {config.cooldown_seconds}"
        raise ValueError(msg)
    if config.sample_rate != 16000:  # noqa: PLR2004
        msg = f"sample_rate must be 16000, got {config.sample_rate}"
        raise ValueError(msg)
    if config.frame_samples != 1280:  # noqa: PLR2004
        msg = f"frame_samples must be 1280, got {config.frame_samples}"
        raise ValueError(msg)


def _ascii_fold(text: str) -> str:
    """Case-fold + Latin-1 diacritic-strip a transcript fragment.

    Mirrors :prop:`MindConfig.effective_wake_word_variants`'s
    diacritic stripping so the variants list and the STT
    transcripts match through the same canonicalisation. STT
    engines commonly drop diacritics (Moonshine returns "lucia"
    even when the speaker said "Lúcia"); the variants list often
    carries both forms but the comparison MUST be invariant under
    case + diacritic differences for predictable matching.
    """
    normalised = unicodedata.normalize("NFKD", text)
    ascii_text = "".join(c for c in normalised if not unicodedata.combining(c))
    return ascii_text.lower()


class STTWakeWordDetector:
    """STT-based wake-word detector — fallback when no ONNX model is trained.

    Implements the duck-type surface
    (``process_frame``, ``reset``, ``state``,
    ``note_false_fire``) that
    :class:`~sovyx.voice._wake_word_router.WakeWordRouter` expects
    so it slots into the router transparently.

    State machine (mirrors ONNX detector for uniform router
    semantics):
      IDLE → STT_PENDING → IDLE (on no-match)
                       → COOLDOWN → IDLE (on match)

    Note: STT_PENDING is conceptually equivalent to the ONNX
    detector's STAGE1_TRIGGERED but represents "STT call returned
    a transcript and we matched a variant" rather than "stage-1
    score crossed threshold". The post-match transition to
    COOLDOWN matches the ONNX detector exactly so cross-detector
    cooldown semantics are consistent.
    """

    def __init__(
        self,
        *,
        transcribe_fn: Callable[[npt.NDArray[np.float32]], str],
        config: STTWakeWordConfig,
    ) -> None:
        """Construct an STT-based fallback detector.

        Args:
            transcribe_fn: Synchronous callable that takes an audio
                buffer (float32, mono, 16 kHz) and returns the
                STT transcript. Operators bridge async STT engines
                via a sync wrapper at construction time (see
                module docstring).
            config: Detector configuration. Validated; raises
                ``ValueError`` on out-of-range values.
        """
        _validate_stt_config(config)
        self._transcribe = transcribe_fn
        self._config = config

        # Pre-compute fold-canonical variants once at construction
        # so process_frame's match path stays O(N variants) without
        # the per-call fold cost.
        self._folded_variants: frozenset[str] = frozenset(
            _ascii_fold(v) for v in config.wake_variants
        )

        # Audio buffer — list of float32 frames; trimmed each
        # process_frame to keep the rolling window bounded.
        self._buffer: list[npt.NDArray[np.float32]] = []
        self._max_buffer_frames = max(
            1,
            int(config.buffer_seconds * config.sample_rate / config.frame_samples),
        )
        # Cooldown is measured in frames at the ONNX detector's
        # per-frame rate; same conversion as WakeWordConfig.
        self._cooldown_frames = int(
            config.cooldown_seconds * config.sample_rate / config.frame_samples,
        )

        self._state = WakeWordState.IDLE
        self._frame_counter = 0  # frames since last STT call OR cooldown entry
        # Sliding window for adaptive cooldown — mirrors the field
        # name + semantics on WakeWordDetector so the router's
        # note_false_fire forwarding stays interface-compatible.
        # T8.9 attaches the per-mind label at the counter level;
        # this list is kept as a no-op store to satisfy the
        # interface contract (the STT detector doesn't currently
        # use adaptive cooldown — fixed-duration cooldown only).
        self._false_fire_monotonics: list[float] = []

        logger.info(
            "STTWakeWordDetector initialised",
            **{
                "voice.wake_variants_count": len(config.wake_variants),
                "voice.buffer_seconds": config.buffer_seconds,
                "voice.stt_call_interval_frames": config.stt_call_interval_frames,
                "voice.cooldown_seconds": config.cooldown_seconds,
            },
        )

    @property
    def state(self) -> WakeWordState:
        """Current detector state."""
        return self._state

    def process_frame(
        self,
        audio_frame: npt.NDArray[np.float32] | npt.NDArray[np.int16],
    ) -> WakeWordEvent:
        """Process one audio frame; periodically run STT.

        Returns a :class:`WakeWordEvent` with detection flag +
        score (always 0.0 for STT-path; the ONNX score field is
        repurposed to a boolean signal here) + state.

        Raises:
            ValueError: If frame has wrong length.
        """
        import numpy as np  # noqa: PLC0415 — same lazy-import pattern as WakeWordDetector

        expected = self._config.frame_samples
        if audio_frame.shape != (expected,):
            msg = f"Expected frame of {expected} samples, got shape {audio_frame.shape}"
            raise ValueError(msg)

        # Normalise to float32 so the STT engine sees a uniform
        # input regardless of whether the audio thread delivered
        # int16 (typical capture) or float32 (post-AGC pipelines).
        if audio_frame.dtype == np.int16:
            audio = audio_frame.astype(np.float32) / 32768.0
        else:
            audio = audio_frame.astype(np.float32)

        # Cooldown handling — same pattern as WakeWordDetector.
        if self._state == WakeWordState.COOLDOWN:
            self._frame_counter += 1
            if self._frame_counter >= self._cooldown_frames:
                self._state = WakeWordState.IDLE
                self._frame_counter = 0
                logger.debug("STT wake word cooldown ended")
            return WakeWordEvent(detected=False, score=0.0, state=self._state)

        # Empty variants set = detector disabled (no-op).
        if not self._folded_variants:
            return WakeWordEvent(detected=False, score=0.0, state=self._state)

        # Buffer the frame + trim to rolling window.
        self._buffer.append(audio)
        if len(self._buffer) > self._max_buffer_frames:
            # Drop oldest frame(s) to maintain bounded memory.
            self._buffer = self._buffer[-self._max_buffer_frames :]

        self._frame_counter += 1

        # Periodic STT call. Don't fire until we've buffered enough
        # audio for a meaningful transcript (≥ half the buffer
        # window) — short buffers produce noisy transcripts.
        if self._frame_counter < self._config.stt_call_interval_frames:
            return WakeWordEvent(detected=False, score=0.0, state=self._state)

        self._frame_counter = 0  # reset counter regardless of match
        if len(self._buffer) < self._max_buffer_frames // 2:
            # Not enough buffered audio yet; skip STT this cycle.
            return WakeWordEvent(detected=False, score=0.0, state=self._state)

        # Concatenate buffer + run STT. Failures on the STT side
        # are non-fatal — we log + treat as no-match. A buggy STT
        # backend must NOT deafen the entire wake-word path.
        combined = np.concatenate(self._buffer)
        try:
            transcript = self._transcribe(combined)
        except Exception:  # noqa: BLE001 — failure isolation
            logger.exception("STTWakeWordDetector.transcribe raised — treating as no-match")
            return WakeWordEvent(detected=False, score=0.0, state=self._state)

        # Match transcript against fold-canonical variants. The
        # transcript is fold-canonicalised once + checked for
        # substring-membership of any variant. Substring (not exact
        # equality) because STT typically returns a longer phrase
        # ("hey sovyx are you there") that contains the wake word
        # near the start.
        folded_transcript = _ascii_fold(transcript)
        matched = False
        matched_variant = ""
        for variant in self._folded_variants:
            if variant in folded_transcript:
                matched = True
                matched_variant = variant
                break

        if not matched:
            logger.debug(
                "STT wake-word no-match",
                **{
                    "voice.transcript_length": len(transcript),
                },
            )
            return WakeWordEvent(detected=False, score=0.0, state=self._state)

        # Match — transition to COOLDOWN, clear buffer, fire detection.
        self._state = WakeWordState.COOLDOWN
        self._frame_counter = 0
        self._buffer.clear()
        logger.info(
            "STT wake word CONFIRMED",
            **{
                "voice.matched_variant": matched_variant,
                "voice.transcript_preview": transcript[:64],  # bounded
            },
        )
        return WakeWordEvent(detected=True, score=1.0, state=self._state)

    def reset(self) -> None:
        """Reset detector state. Mirrors WakeWordDetector.reset."""
        self._state = WakeWordState.IDLE
        self._frame_counter = 0
        self._buffer.clear()

    def note_false_fire(self, *, monotonic_now: float | None = None) -> None:
        """Record a false-fire signal — interface-compatible no-op.

        The STT detector doesn't currently use adaptive cooldown
        (fixed cooldown_seconds only), but the method exists for
        router interface compatibility — the router calls
        ``note_false_fire`` uniformly across all registered
        detectors regardless of class. T7.8 adaptive cooldown is
        an ONNX-detector-only feature; STT path uses fixed cooldown.
        """
        del monotonic_now  # interface stub only


__all__ = [
    "STTWakeWordConfig",
    "STTWakeWordDetector",
    "TranscribeFn",
]
