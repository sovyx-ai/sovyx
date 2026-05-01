"""Multi-mind wake-word router — Phase 8 / T8.6-T8.9.

The router holds N :class:`WakeWordDetector` instances keyed by mind
ID. Every audio frame is fanned out to the registered detectors;
the first detector that confirms a wake word wins, and the router
emits ``(mind_id, WakeWordEvent)`` so the orchestrator can dispatch
the matched mind in ≤ 50 ms (T8.10).

Architectural rationale per master mission §Phase 8:

  Sovyx already has multi-mind infrastructure in ``src/sovyx/mind/``
  (MindConfig per mind, MindRegistry capable of loading multiple
  minds, brain graph + episodic memory isolated per mind). What's
  missing is the **voice routing layer** that bridges wake-word
  detection to mind dispatch. v0.24.0-v0.30.0 ships single-mind
  voice (one shared wake word "Sovyx") because the wake-word engine
  was hardcoded. Phase 8 closes this conceptual inconsistency: if
  Sovyx is multi-mind by design, the wake word MUST be per-mind,
  not monolithic.

Performance contract: each detector runs ~5 ms ONNX inference per
frame on a Pi 5 / ~1 ms on N100. Sequential iteration over N
detectors costs N*inference_ms — comfortably under the 50 ms budget
for typical N=3-5 minds. For N ≥ 10 minds, future work moves the
fan-out onto :func:`asyncio.gather` + :func:`asyncio.to_thread` per
detector for parallelism; the foundation here is sequential since
the typical case doesn't need it.

Failure isolation: a detector that raises during ``process_frame``
is logged + skipped. Other detectors in the router continue to
fire. A buggy ONNX session in mind A doesn't deafen the entire
multi-mind installation to mind B.

T8.6: WakeWordRouter class
T8.7: Lazy registration — mind detectors are constructed only when
      :meth:`register_mind` is called, not when MindRegistry loads.
      Operators with one active mind don't pay the construction
      cost for inactive minds.
T8.8: Per-mind cooldown is independent by construction — each
      detector instance has its own state machine + cooldown
      counter; cooldown for mind A doesn't suppress mind B.
T8.9: Per-mind false-fire label. The router's
      :meth:`note_false_fire` accepts a mind_id and forwards to the
      matched detector; the underlying ``record_wake_word_false_fire``
      counter gains a ``mind_id`` attribute so dashboards split
      per-mind.

T8.10 (orchestrator dispatch ≤ 50 ms) is wired in a follow-up
commit — the router emits ``(mind_id, event)``; the orchestrator
side switches mind context.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from sovyx.observability.logging import get_logger
from sovyx.voice.wake_word import (
    WakeWordConfig,
    WakeWordDetector,
    WakeWordEvent,
    WakeWordState,
)


class _WakeWordDetectorLike(Protocol):
    """Duck-type interface the router fans frames to.

    Both :class:`WakeWordDetector` (ONNX) and
    :class:`STTWakeWordDetector` satisfy this Protocol structurally.
    The router holds a heterogeneous mix and dispatches uniformly.
    """

    @property
    def state(self) -> WakeWordState: ...

    def process_frame(
        self,
        audio_frame: npt.NDArray[np.float32] | npt.NDArray[np.int16],
    ) -> WakeWordEvent: ...

    def reset(self) -> None: ...

    def note_false_fire(self, *, monotonic_now: float | None = None) -> None: ...


if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from pathlib import Path

    import numpy as np
    import numpy.typing as npt

    from sovyx.engine.types import MindId
    from sovyx.voice._wake_word_stt_fallback import STTWakeWordConfig
    from sovyx.voice.wake_word import VerifierFn

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class WakeWordRouterEvent:
    """One detection from the router — the matched mind + event.

    The router emits this whenever any registered detector confirms
    a wake word for its mind. ``mind_id`` is the canonical lookup
    key the orchestrator uses to dispatch context (memory +
    personality + voice config) of the matched mind.

    Frozen + slotted to match :class:`WakeWordEvent` ergonomics.
    """

    mind_id: MindId
    """The mind whose wake word was detected."""

    event: WakeWordEvent
    """The underlying detector event (carries score + state)."""


class WakeWordRouter:
    """Multi-mind wake-word router.

    Holds N :class:`WakeWordDetector` instances; per-frame fan-out
    iterates them in registration order. The first detector whose
    ``process_frame`` returns ``detected=True`` wins; the router
    short-circuits the fan-out for that frame so subsequent
    detectors don't see audio post-confirmation (avoids spurious
    cross-mind detections on the same wake event).

    Thread-safety: NOT thread-safe. The router is designed to live
    on the orchestrator's event-loop thread, which is the same
    thread as the audio frame consumer. Calling ``process_frame``
    from multiple threads concurrently is undefined behaviour.

    Lazy registration: detectors are constructed only via
    :meth:`register_mind`. The MindRegistry's load-time iteration
    of mind configs MUST NOT eagerly call register_mind — the
    operator's voice pipeline calls it for each ENABLED mind at
    pipeline-start time so inactive minds don't pay the ONNX
    session construction cost (~50 MB resident per session).
    """

    def __init__(self) -> None:
        # Insertion order is detection priority. Python 3.7+ dict
        # preserves insertion order; the router's contract pins this
        # so operators get deterministic behaviour when multiple
        # minds share a wake word phonetic neighbourhood (e.g. two
        # minds named "Aria" and "Aria-2" — the first registered
        # wins).
        # The value type is the duck-type protocol so ONNX +
        # STT-fallback detectors interleave transparently
        # (T8.17-T8.18 hot-swap path).
        self._detectors: dict[MindId, _WakeWordDetectorLike] = {}

    @property
    def registered_minds(self) -> tuple[MindId, ...]:
        """Tuple of mind IDs in registration order (= detection priority)."""
        return tuple(self._detectors.keys())

    @property
    def is_empty(self) -> bool:
        """``True`` when no minds are registered. Useful for the
        orchestrator to skip the wake-word stage entirely when no
        detectors exist."""
        return not self._detectors

    def register_mind(
        self,
        mind_id: MindId,
        *,
        model_path: Path,
        config: WakeWordConfig | None = None,
        verifier: VerifierFn | None = None,
    ) -> None:
        """Construct + register a :class:`WakeWordDetector` for ``mind_id``.

        T8.7 lazy-load contract: each call constructs ONE detector
        for the named mind. Call this from the voice pipeline's
        start path for each ENABLED mind in the operator's
        MindRegistry.

        Idempotent: re-registering an already-registered mind_id
        replaces the prior detector (the prior ONNX session is
        garbage-collected normally; no manual close needed). Useful
        for hot-reload after T8.13 custom training completes
        (T8.15).

        Args:
            mind_id: Stable mind identifier (matches MindConfig.id).
            model_path: Path to the wake-word ONNX checkpoint for
                this mind.
            config: Per-mind WakeWordConfig (cooldown, thresholds,
                etc.). Default ``None`` constructs a default config
                — operator-overridable per mind.
            verifier: STT verifier callable. Default ``None`` uses
                the default verifier from ``wake_word.py``.

        Raises:
            ValueError: If ``mind_id`` is empty (would match every
                empty-id record at unregister/notify time).
        """
        if not mind_id:
            msg = "mind_id must be a non-empty string"
            raise ValueError(msg)
        detector = WakeWordDetector(
            model_path=model_path,
            config=config,
            verifier=verifier,
        )
        self._detectors[mind_id] = detector
        logger.info(
            "voice.wake_word.router.mind_registered",
            **{
                "voice.mind_id": str(mind_id),
                "voice.model_path": str(model_path),
                "voice.registered_count": len(self._detectors),
            },
        )

    def register_mind_stt_fallback(
        self,
        mind_id: MindId,
        *,
        transcribe_fn: Callable[[npt.NDArray[np.float32]], str],
        config: STTWakeWordConfig,
    ) -> None:
        """Register an STT-based fallback detector for ``mind_id``.

        T8.17-T8.18 hot-swap path: the operator initially registers
        a mind via ``register_mind_stt_fallback`` (slow path,
        ~500 ms latency) so wake-word detection works immediately
        for newly-named minds. When T8.13 custom training completes
        for that mind, the operator calls
        :meth:`register_mind` with the new ONNX checkpoint —
        re-registration replaces the STT detector with the
        ONNX-based :class:`WakeWordDetector` (T8.18 hot-swap),
        latency drops to ~80 ms, no daemon restart needed.

        Symmetric with :meth:`register_mind` — same idempotent
        replacement semantics, same empty-mind_id rejection, same
        registration-order priority. The router fans frames to
        BOTH ONNX and STT detectors uniformly via the duck-type
        ``process_frame`` interface; T8.19 telemetry differentiates
        which class fired via the ``method`` counter label.

        Args:
            mind_id: Stable mind identifier (matches MindConfig.id).
            transcribe_fn: Synchronous callable from float32 audio
                buffer → transcript text. Operators wire async STT
                engines via a sync bridge (see STTWakeWordDetector
                module docstring).
            config: STT detector configuration.

        Raises:
            ValueError: If ``mind_id`` is empty.
        """
        if not mind_id:
            msg = "mind_id must be a non-empty string"
            raise ValueError(msg)
        # Lazy import to keep STT module off the path of operators
        # who don't use the fallback feature.
        from sovyx.voice._wake_word_stt_fallback import (  # noqa: PLC0415
            STTWakeWordDetector,
        )

        detector = STTWakeWordDetector(transcribe_fn=transcribe_fn, config=config)
        self._detectors[mind_id] = detector
        logger.info(
            "voice.wake_word.router.mind_registered_stt_fallback",
            **{
                "voice.mind_id": str(mind_id),
                "voice.registered_count": len(self._detectors),
            },
        )

    def unregister_mind(self, mind_id: MindId) -> None:
        """Remove a mind's detector from the router.

        Idempotent: unregistering an unknown mind_id is a no-op.
        Use case: mind disabled at runtime (operator toggled it
        off in MindRegistry).
        """
        detector = self._detectors.pop(mind_id, None)
        if detector is not None:
            detector.reset()
            logger.info(
                "voice.wake_word.router.mind_unregistered",
                **{
                    "voice.mind_id": str(mind_id),
                    "voice.remaining_count": len(self._detectors),
                },
            )

    def reset_all(self) -> None:
        """Reset every registered detector to IDLE.

        Use case: pipeline restart between conversations / shutdown.
        Each detector's ``reset()`` clears its state machine + buffer
        + stage1_trigger anchor, but does NOT clear the false-fire
        sliding window (T7.8 adaptive cooldown's window is intentionally
        cross-conversation per the wake_word.py docstring).
        """
        for detector in self._detectors.values():
            detector.reset()

    def process_frame(
        self,
        audio_frame: npt.NDArray[np.float32] | npt.NDArray[np.int16],
    ) -> WakeWordRouterEvent | None:
        """Fan a frame out to every registered detector.

        Iterates detectors in registration order. First detector
        whose ``process_frame`` returns ``event.detected=True`` wins
        — the router short-circuits and emits the
        :class:`WakeWordRouterEvent`. Subsequent detectors are
        skipped for this frame to avoid spurious cross-mind
        detections on the same wake event.

        Returns ``None`` when no detector confirmed (the common
        case: every frame iterates without a wake). The router
        does NOT propagate :class:`WakeWordEvent` for non-detection
        frames; per-frame telemetry already lives on the individual
        detectors' ``voice.wake_word.score`` log events.

        Failure isolation: a detector that raises is logged at
        ERROR level + skipped; other detectors continue. Catching
        ``BaseException`` here is intentional — a bug in mind A's
        ONNX session must not deafen mind B.
        """
        for mind_id, detector in self._detectors.items():
            try:
                event = detector.process_frame(audio_frame)
            except BaseException as exc:  # noqa: BLE001 — failure isolation per docstring
                logger.exception(
                    "voice.wake_word.router.detector_raised",
                    **{
                        "voice.mind_id": str(mind_id),
                        "voice.error_type": type(exc).__name__,
                    },
                )
                continue
            if event.detected:
                # T8.19 — detection-method telemetry. Discriminate by
                # detector class so dashboards split slow-path
                # (stt_fallback) from fast-path (onnx) detection
                # rates per mind. Lazy-import to avoid the metrics
                # surface on non-voice daemons.
                from sovyx.voice._wake_word_stt_fallback import (  # noqa: PLC0415
                    STTWakeWordDetector,
                )
                from sovyx.voice.health._metrics import (  # noqa: PLC0415
                    record_wake_word_detection_method,
                )

                method = "stt_fallback" if isinstance(detector, STTWakeWordDetector) else "onnx"
                record_wake_word_detection_method(
                    method=method,
                    mind_id=str(mind_id),
                )
                logger.info(
                    "voice.wake_word.router.matched",
                    **{
                        "voice.mind_id": str(mind_id),
                        "voice.score": round(event.score, 4),
                        "voice.detection_method": method,
                    },
                )
                return WakeWordRouterEvent(mind_id=mind_id, event=event)
        return None

    def note_false_fire(self, mind_id: MindId) -> None:
        """Forward a false-fire signal to the named mind's detector.

        T8.9 per-mind false-fire wire-up. The orchestrator calls this
        after detecting the post-wake STT path discarded the
        transcript (the same 3 conditions as T7.7 — empty
        transcription, rejected transcription, sub-confidence).

        Idempotent on unknown mind_id: a stale signal targeting a
        recently-unregistered mind silently no-ops. The orchestrator
        side keeps the mind_id from the original
        :class:`WakeWordRouterEvent`; if the mind was unregistered
        between detection + STT, the signal is dropped.
        """
        detector = self._detectors.get(mind_id)
        if detector is None:
            return
        detector.note_false_fire()

    def state_for(self, mind_id: MindId) -> WakeWordState | None:
        """Return the named mind's detector state, or ``None`` if absent.

        Diagnostic accessor for the dashboard's per-mind
        wake-word-state widget. Returns the
        :class:`WakeWordState` enum value; ``None`` for unregistered
        minds is the documented sentinel.
        """
        detector = self._detectors.get(mind_id)
        if detector is None:
            return None
        return detector.state

    def __len__(self) -> int:
        """Number of registered minds."""
        return len(self._detectors)

    def __iter__(self) -> Iterable[MindId]:
        """Iterate registered mind IDs in registration order."""
        return iter(self._detectors.keys())

    def __contains__(self, mind_id: object) -> bool:
        """Check whether a mind is registered."""
        return mind_id in self._detectors


__all__ = [
    "WakeWordRouter",
    "WakeWordRouterEvent",
]
