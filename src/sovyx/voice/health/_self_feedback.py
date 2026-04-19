"""Self-feedback isolation (ADR §4.4.6).

When the OS echo-cancel APO is bypassed (Windows cascade attempts 1-4,
Linux 1-2, macOS 1-3) TTS playback leaks back into the microphone
stream and would otherwise trigger our own wake-word or barge-in
detector. The defense is a three-layer trio:

a. **Half-duplex gate** (structural, always on). Wake-word inference
   runs *only* in :attr:`~sovyx.voice.pipeline.VoicePipelineState.IDLE`
   and barge-in requires ≥ 5 sustained high-probability VAD frames —
   both encoded in the orchestrator and the
   :class:`~sovyx.voice.pipeline._barge_in.BargeInDetector`. This layer
   is not configurable; it's a property of the state machine.

b. **Mic ducking** (optional, default on when OS AEC is bypassed).
   During TTS playback we apply a -18 dB digital attenuation to the
   mic signal *before* it reaches the VAD. The attenuation is released
   within ``self_feedback_duck_release_ms`` of TTS-end. Implemented by
   calling :meth:`~sovyx.voice.FrameNormalizer.set_ducking_gain_db`
   through an ``apply_duck`` callback — the gate stays ignorant of the
   normalizer so unit tests and alternative pipelines can inject a
   bare ``lambda``.

c. **Spectral self-cancel** (Sprint 4 follow-up). Deferred.

The gate is a small state machine with three methods: ``on_tts_start``,
``on_tts_end``, and ``is_active``. Calling ``on_tts_start`` twice is
idempotent — the underlying duck callback is only invoked on the
rising edge. This matters because ``stream_text`` and ``speak`` both
transition to SPEAKING, and during streamed TTS the orchestrator may
enter SPEAKING once but call start handlers multiple times.

The ``apply_duck`` callback is wrapped so that exceptions from the
normalizer (e.g., capture task torn down mid-TTS) are logged and
swallowed. Losing a duck application is *preferable* to crashing the
voice loop mid-utterance.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from sovyx.engine.config import VoiceTuningConfig as _VoiceTuning
from sovyx.observability.logging import get_logger
from sovyx.voice.health._metrics import record_self_feedback_block

if TYPE_CHECKING:
    from collections.abc import Callable

logger = get_logger(__name__)


_DEFAULT_DUCK_GAIN_DB = _VoiceTuning().self_feedback_duck_gain_db
_DEFAULT_RELEASE_MS = _VoiceTuning().self_feedback_duck_release_ms
_DEFAULT_MODE = _VoiceTuning().self_feedback_isolation_mode


class SelfFeedbackMode(StrEnum):
    """Self-feedback isolation policy (ADR §4.4.6 / §5.9)."""

    OFF = "off"
    """No ducking and no gate-state logging. The structural half-duplex
    gate (state machine) remains active — it's a property of the
    orchestrator, not of this component."""

    GATE_ONLY = "gate-only"
    """Log gate engage/release transitions for observability but skip
    the ducking callback. Useful when OS echo-cancel is trusted
    (shared WASAPI, PulseAudio ``module-echo-cancel`` upstream)."""

    GATE_DUCK = "gate+duck"
    """Log gate transitions AND apply mic ducking via ``apply_duck``.
    Default when OS AEC is bypassed."""


class SelfFeedbackGate:
    """Orchestrates mic ducking around TTS playback.

    The gate does not know about ``FrameNormalizer`` — the caller
    supplies an ``apply_duck`` callable that accepts a single ``gain_db``
    float (``<= 0``). Factory code wires this to the capture task's
    normalizer; unit tests inject a lambda that records calls.

    Args:
        mode: Isolation policy. ``"off"`` disables duck *and* the
            engage/release observability logs. ``"gate-only"`` keeps
            the logs. ``"gate+duck"`` applies duck on the rising edge
            and releases on the falling edge.
        apply_duck: Callable invoked with the target gain in dB.
            ``None`` is treated as "no duck available" — gate degrades
            to ``gate-only`` semantics for the duration of the session
            and logs a one-shot WARNING. Passing an explicit ``None``
            is how the factory communicates "no capture normalizer
            reachable" (test harnesses, push-to-talk fallback).
        duck_gain_db: Attenuation applied on TTS-start. Must be ``<= 0``.
            Defaults to the tuning value. Rejected at construction.
        release_ms: Informational — recorded on logs and queryable via
            :attr:`release_ms`. The normalizer owns the actual ramp;
            this value lets the dashboard surface the policy.

    Raises:
        ValueError: If ``duck_gain_db > 0`` (the stage is an
            attenuator, never an amplifier).
    """

    def __init__(
        self,
        *,
        mode: SelfFeedbackMode | str = _DEFAULT_MODE,
        apply_duck: Callable[[float], None] | None = None,
        duck_gain_db: float | None = None,
        release_ms: float | None = None,
    ) -> None:
        resolved_gain = _DEFAULT_DUCK_GAIN_DB if duck_gain_db is None else duck_gain_db
        if resolved_gain > 0.0:
            msg = f"duck_gain_db must be <= 0, got {resolved_gain}"
            raise ValueError(msg)

        self._mode = SelfFeedbackMode(mode) if isinstance(mode, str) else mode
        self._apply_duck = apply_duck
        self._duck_gain_db = resolved_gain
        self._release_ms = _DEFAULT_RELEASE_MS if release_ms is None else release_ms
        self._active = False
        self._duck_unavailable_logged = False

    # -- Properties ----------------------------------------------------------

    @property
    def mode(self) -> SelfFeedbackMode:
        """Current isolation mode."""
        return self._mode

    @property
    def is_active(self) -> bool:
        """Whether the gate is currently engaged (TTS is playing)."""
        return self._active

    @property
    def duck_gain_db(self) -> float:
        """Target attenuation applied on engage (dB, ``<= 0``)."""
        return self._duck_gain_db

    @property
    def release_ms(self) -> float:
        """Informational release window. Normalizer owns the real ramp."""
        return self._release_ms

    # -- Transitions ---------------------------------------------------------

    def on_tts_start(self) -> None:
        """Engage the gate — TTS is about to play.

        Idempotent: calling while already active is a no-op. The duck
        callback runs once on the rising edge. ``OFF`` mode does
        nothing (not even a log entry).
        """
        if self._mode is SelfFeedbackMode.OFF:
            return
        if self._active:
            return
        self._active = True
        self._apply_mode_duck(self._duck_gain_db, phase="engage")
        record_self_feedback_block(layer="gate")
        if self._mode is SelfFeedbackMode.GATE_DUCK and self._apply_duck is not None:
            record_self_feedback_block(layer="duck")
        logger.info(
            "voice_self_feedback_gate_engaged",
            mode=self._mode.value,
            duck_gain_db=self._duck_gain_db,
            release_ms=self._release_ms,
        )

    def on_tts_end(self) -> None:
        """Release the gate — TTS finished or was interrupted.

        Idempotent: calling while inactive is a no-op. The duck is
        released to unity (``0 dB``) on the falling edge. ``OFF``
        mode does nothing.
        """
        if self._mode is SelfFeedbackMode.OFF:
            return
        if not self._active:
            return
        self._active = False
        self._apply_mode_duck(0.0, phase="release")
        logger.info(
            "voice_self_feedback_gate_released",
            mode=self._mode.value,
        )

    # -- Internals -----------------------------------------------------------

    def _apply_mode_duck(self, gain_db: float, *, phase: str) -> None:
        if self._mode is not SelfFeedbackMode.GATE_DUCK:
            return
        if self._apply_duck is None:
            if not self._duck_unavailable_logged:
                logger.warning(
                    "voice_self_feedback_duck_unavailable",
                    reason="no apply_duck callback wired",
                    mode=self._mode.value,
                )
                self._duck_unavailable_logged = True
            return
        try:
            self._apply_duck(gain_db)
        except Exception:  # noqa: BLE001 — duck failure must not crash voice loop
            logger.warning(
                "voice_self_feedback_duck_failed",
                phase=phase,
                gain_db=gain_db,
                exc_info=True,
            )


__all__ = [
    "SelfFeedbackGate",
    "SelfFeedbackMode",
]
