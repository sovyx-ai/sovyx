"""Wake-word training orchestrator — Phase 8 / T8.13 + T8.14 + T8.15.

Composes the foundation primitives shipped in prior commits:

* :class:`KokoroSampleSynthesizer` (commit ``7e0548d``) — generates
  positive samples.
* :class:`TrainerBackend` Protocol (commit ``845e9cc``) — pluggable
  training backend.
* :class:`ProgressTracker` (commit ``845e9cc``) — JSONL progress log.
* :class:`TrainingJobState` + ``is_legal_transition`` (commit
  ``845e9cc``) — state machine.

The orchestrator drives the canonical
``PENDING → SYNTHESIZING → TRAINING → {COMPLETE | FAILED |
CANCELLED}`` transition graph. Each transition is persisted to the
JSONL log so a daemon restart can resume by reading the latest
non-terminal state. Cancellation is observable via either an
in-memory callable or a filesystem signal (touch
``<job_dir>/.cancel``).

Hot-reload integration (T8.15):
  On ``COMPLETE``, the orchestrator invokes the optional
  ``on_complete`` callback with the trained ``.onnx`` path. Callers
  wire this to ``WakeWordRouter.register_mind`` so the new model
  swaps in without a daemon restart. The router's ``register_mind``
  is idempotent — calling it with a new ``model_path`` for an
  already-registered ``mind_id`` replaces the detector cleanly.

Long-running design:
  ``run()`` is async + long-running (synthesis ~5 min, training
  ~30-60 min). Callers decide whether to ``await`` directly (CLI
  command ``sovyx voice train-wake-word``) or wrap in
  ``asyncio.create_task`` (dashboard 202-Accepted pattern). Tests
  await directly — the cancellation primitive lets them stop
  early.

Reference: master mission ``MISSION-voice-final-skype-grade-2026.md``
§Phase 8 / T8.13-T8.15. Operator debt:
``OPERATOR-DEBT-MASTER-2026-05-01.md`` D10 (training pipeline
mini-mission).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger
from sovyx.voice.wake_word_training._state import (
    TrainingJobState,
    TrainingStatus,
    is_legal_transition,
)
from sovyx.voice.wake_word_training._synthesizer import (
    SynthesisRequest,
)
from sovyx.voice.wake_word_training._trainer_protocol import (
    TrainingCancelledError,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from sovyx.voice.wake_word_training._progress import ProgressTracker
    from sovyx.voice.wake_word_training._synthesizer import (
        KokoroSampleSynthesizer,
    )
    from sovyx.voice.wake_word_training._trainer_protocol import (
        TrainerBackend,
    )


logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class TrainingRequest:
    """Spec for one end-to-end training job.

    Attributes:
        wake_word: The wake word to train (with diacritics intact for
            audit logs; synthesizer + backend handle ASCII-folding
            internally).
        mind_id: Mind that owns the resulting model. Empty string is
            permitted for global / unattached training. The
            orchestrator passes it to the ``on_complete`` callback so
            callers can route the trained ``.onnx`` to
            ``WakeWordRouter.register_mind(mind_id)``.
        language: BCP-47 tag passed to both synthesizer (Kokoro G2P)
            and backend (phoneme tables).
        target_positive_samples: How many positive samples the
            synthesizer should produce. 200 is the conservative
            minimum for reasonable accuracy per the OpenWakeWord
            training docs.
        synthesizer_voices: Override Kokoro voice catalogue. Empty
            tuple uses the synthesizer's defaults.
        synthesizer_variants: Phrases to render. Typically built
            from ``MindConfig.effective_wake_word_variants`` extended
            via ``expand_wake_word_variants`` (T8.16).
        negative_samples_dir: Filesystem path to operator-provided
            non-wake-word audio. The backend reads ``*.wav`` files
            from here. **The orchestrator does NOT generate negative
            samples** — that's the operator's responsibility (and
            future bundled-noise-pack work).
        output_path: Where the trained ``.onnx`` lands. Backend may
            return a different path (it MAY write to a temp location
            then rename); orchestrator preserves whatever the
            backend returns.
    """

    wake_word: str
    mind_id: str
    language: str
    target_positive_samples: int
    synthesizer_voices: tuple[str, ...]
    synthesizer_variants: tuple[str, ...]
    negative_samples_dir: Path
    output_path: Path


class TrainingOrchestrator:
    """Drive a wake-word training job through its state machine.

    Args:
        synthesizer: Positive-sample generator.
        backend: :class:`TrainerBackend`-shaped training backend.
            Production wires the operator-registered default backend
            via ``resolve_default_backend``; tests inject a stub.
        progress_tracker: JSONL progress log. The orchestrator
            appends a state snapshot at every transition.
        on_complete: Optional callback invoked when the job reaches
            ``COMPLETE``. Receives ``(mind_id, output_path)``.
            Production wires this to ``WakeWordRouter.register_mind``
            for hot-reload (T8.15). Tests inject a recording stub.

    Thread safety:
        Holds no mutable state; one orchestrator instance can drive
        many sequential jobs. Concurrent jobs need separate
        orchestrator instances (the progress tracker is per-job).
    """

    def __init__(
        self,
        *,
        synthesizer: KokoroSampleSynthesizer,
        backend: TrainerBackend,
        progress_tracker: ProgressTracker,
        on_complete: Callable[[str, Path], None] | None = None,
    ) -> None:
        self._synthesizer = synthesizer
        self._backend = backend
        self._progress = progress_tracker
        self._on_complete = on_complete

    async def run(
        self,
        request: TrainingRequest,
        *,
        job_dir: Path,
        cancel_check: Callable[[], bool] | None = None,
    ) -> TrainingJobState:
        """Execute the full training pipeline.

        Phase order:
        1. PENDING → SYNTHESIZING — start synthesis.
        2. SYNTHESIZING → TRAINING — positive samples written; load
           negative samples; call backend.train.
        3. TRAINING → COMPLETE — backend returned path; trigger
           ``on_complete`` for hot-reload.

        Any phase can transition to CANCELLED (via cancel_check) or
        FAILED (via exception). State transitions are persisted to
        the JSONL progress log; the operator's CLI/dashboard polls
        the tail.

        Args:
            request: The full training spec.
            job_dir: Working directory for this job.
                ``<job_dir>/positive/`` holds synthesized positives.
                ``<job_dir>/progress.jsonl`` is written by
                ``progress_tracker``. ``<job_dir>/.cancel`` is the
                filesystem cancellation signal (when no
                ``cancel_check`` is supplied).
            cancel_check: Optional cancellation poll. Default
                ``None`` uses the filesystem signal (presence of
                ``<job_dir>/.cancel``). Callers with their own
                cancellation token (dashboard "Cancel" button) pass
                a callable that consults their state.

        Returns:
            The final :class:`TrainingJobState` (always terminal).
        """
        job_dir.mkdir(parents=True, exist_ok=True)
        cancel_fn = cancel_check or _make_filesystem_cancel_check(job_dir)

        state = TrainingJobState.initial(
            wake_word=request.wake_word,
            mind_id=request.mind_id,
            language=request.language,
            target_samples=request.target_positive_samples,
        )
        self._progress.append(state)

        # Pre-flight cancellation check — operator may have
        # cancelled before the orchestrator started running.
        if cancel_fn():
            return self._terminal(
                state,
                TrainingStatus.CANCELLED,
                message="Cancelled before synthesis started.",
            )

        # ── SYNTHESIZING phase ───────────────────────────────────
        state = self._transition(
            state,
            TrainingStatus.SYNTHESIZING,
            message="Generating positive samples...",
        )

        synthesis_request = SynthesisRequest(
            wake_word=request.wake_word,
            variants=request.synthesizer_variants,
            language=request.language,
            target_count=request.target_positive_samples,
            voices=request.synthesizer_voices,
        )
        positive_dir = job_dir / "positive"

        try:
            synthesis_result = await self._synthesizer.synthesize(
                synthesis_request,
                positive_dir,
                on_progress=self._make_synthesis_progress_handler(state),
                cancel_check=cancel_fn,
            )
        except Exception as exc:  # noqa: BLE001 — orchestrator-level failure handler
            return self._terminal(
                state,
                TrainingStatus.FAILED,
                error_summary=f"Synthesis raised: {exc}",
            )

        if synthesis_result.cancelled:
            return self._terminal(
                state,
                TrainingStatus.CANCELLED,
                message=(
                    f"Cancelled during synthesis "
                    f"({synthesis_result.completed_count}"
                    f"/{request.target_positive_samples} samples)."
                ),
            )

        if synthesis_result.completed_count == 0:
            return self._terminal(
                state,
                TrainingStatus.FAILED,
                error_summary=(
                    "Synthesis produced 0 samples. Check the TTS "
                    "engine + voice catalogue + language tag."
                ),
            )

        state = state.with_status(
            TrainingStatus.SYNTHESIZING,
            progress=1.0,
            message=f"Synthesised {synthesis_result.completed_count} positive samples.",
            samples_generated=synthesis_result.completed_count,
        )
        self._progress.append(state)

        # ── TRAINING phase ──────────────────────────────────────
        state = self._transition(
            state,
            TrainingStatus.TRAINING,
            message="Loading negative samples...",
        )

        try:
            negative_paths = self._collect_negative_samples(request.negative_samples_dir)
        except FileNotFoundError as exc:
            return self._terminal(
                state,
                TrainingStatus.FAILED,
                error_summary=(
                    f"Negative samples directory not found: "
                    f"{request.negative_samples_dir}. "
                    f"Operator must provide non-wake-word audio "
                    f"(WAV files) before training. Original error: "
                    f"{exc}"
                ),
            )

        if not negative_paths:
            return self._terminal(
                state,
                TrainingStatus.FAILED,
                error_summary=(
                    f"Negative samples directory is empty: "
                    f"{request.negative_samples_dir}. "
                    f"Backend training requires at least one "
                    f"non-wake utterance to learn the discrimination "
                    f"boundary."
                ),
            )

        state = state.with_status(
            TrainingStatus.TRAINING,
            progress=0.0,
            message=f"Training: {len(negative_paths)} negative samples loaded.",
        )
        self._progress.append(state)

        try:
            output_path = await self._run_backend_train(
                request,
                positive_paths=list(synthesis_result.sample_paths),
                negative_paths=negative_paths,
                cancel_fn=cancel_fn,
                state=state,
            )
        except TrainingCancelledError as exc:
            return self._terminal(
                state,
                TrainingStatus.CANCELLED,
                message=f"Cancelled during training: {exc}",
            )
        except Exception as exc:  # noqa: BLE001 — orchestrator-level catch-all
            return self._terminal(
                state,
                TrainingStatus.FAILED,
                error_summary=f"Backend.train raised: {exc}",
            )

        # ── COMPLETE phase ──────────────────────────────────────
        terminal = self._terminal(
            state,
            TrainingStatus.COMPLETE,
            output_path=str(output_path),
            message=f"Training complete: {output_path}",
        )

        # Hot-reload trigger (T8.15) — best-effort: orchestrator
        # records COMPLETE regardless of whether the registration
        # succeeds, so a router-side regression doesn't roll back
        # the trained model.
        if self._on_complete is not None:
            try:
                self._on_complete(request.mind_id, output_path)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "voice.training.on_complete_failed",
                    mind_id=request.mind_id,
                    output_path=str(output_path),
                )
        return terminal

    # ── Internals ────────────────────────────────────────────────────

    def _transition(
        self,
        current: TrainingJobState,
        new_status: TrainingStatus,
        *,
        message: str = "",
    ) -> TrainingJobState:
        """Validate + apply a state transition + persist."""
        if not is_legal_transition(current.status, new_status):
            msg = (
                f"Illegal transition {current.status.value} → "
                f"{new_status.value}. Programmer error."
            )
            raise RuntimeError(msg)
        new_state = current.with_status(new_status, progress=0.0, message=message)
        self._progress.append(new_state)
        return new_state

    def _terminal(
        self,
        current: TrainingJobState,
        new_status: TrainingStatus,
        *,
        message: str = "",
        error_summary: str = "",
        output_path: str = "",
    ) -> TrainingJobState:
        """Apply a terminal-state transition + persist."""
        if not is_legal_transition(current.status, new_status):
            # Defensive: if we're already terminal somehow (e.g.,
            # cancelled at PENDING + caller still passed FAILED),
            # ignore the second transition + return the current
            # state unchanged. Avoids double-write to JSONL.
            logger.warning(
                "voice.training.illegal_terminal_transition_ignored",
                **{
                    "voice.from_status": current.status.value,
                    "voice.to_status": new_status.value,
                },
            )
            return current
        new_state = current.with_status(
            new_status,
            progress=1.0,
            message=message,
            error_summary=error_summary,
            output_path=output_path,
        )
        self._progress.append(new_state)
        return new_state

    def _make_synthesis_progress_handler(
        self,
        current_state: TrainingJobState,
    ) -> Callable[[int, int, str], None]:
        """Build a synthesis progress callback that writes JSONL.

        The synthesizer calls this once per generated sample. We
        update progress fraction + samples_generated + message + log.
        Closure-captured ``current_state`` is the SYNTHESIZING-state
        snapshot so we don't hammer the JSONL with redundant
        status fields.
        """
        progress_tracker = self._progress

        def _on_synth_progress(idx: int, total: int, msg: str) -> None:
            fraction = min(1.0, idx / max(1, total))
            updated = current_state.with_status(
                TrainingStatus.SYNTHESIZING,
                progress=fraction,
                message=f"Sample {idx}/{total}: {msg}",
                samples_generated=idx,
            )
            # The synthesizer fires per-sample which can be 100s of
            # writes for a typical 200-sample job. JSONL append +
            # fsync is on the order of 1 ms each, so even 200 writes
            # is ~200 ms — acceptable. Operators tailing the file
            # see live updates.
            progress_tracker.append(updated)

        return _on_synth_progress

    @staticmethod
    def _collect_negative_samples(directory: Path) -> list[Path]:
        """List ``*.wav`` files in ``directory``. Sorted for
        determinism."""
        if not directory.is_dir():
            msg = f"Not a directory: {directory}"
            raise FileNotFoundError(msg)
        return sorted(directory.glob("*.wav"))

    async def _run_backend_train(
        self,
        request: TrainingRequest,
        *,
        positive_paths: list[Path],
        negative_paths: list[Path],
        cancel_fn: Callable[[], bool],
        state: TrainingJobState,
    ) -> Path:
        """Call the backend's ``train`` method with progress + cancel.

        The backend is sync (``train`` is a regular method, not
        async) because most ML training libs expose sync APIs and
        wrapping in ``asyncio.to_thread`` keeps the orchestrator
        responsive without forcing every backend to be async-aware.
        """
        import asyncio  # noqa: PLC0415 — lazy

        progress_tracker = self._progress

        def on_progress(fraction: float, message: str) -> None:
            updated = state.with_status(
                TrainingStatus.TRAINING,
                progress=max(0.0, min(1.0, fraction)),
                message=message,
            )
            progress_tracker.append(updated)

        return await asyncio.to_thread(
            self._backend.train,
            wake_word=request.wake_word,
            language=request.language,
            positive_samples=positive_paths,
            negative_samples=negative_paths,
            output_path=request.output_path,
            on_progress=on_progress,
            cancel_check=cancel_fn,
        )


# ── Filesystem cancel signal ────────────────────────────────────────


def _make_filesystem_cancel_check(job_dir: Path) -> Callable[[], bool]:
    """Default cancellation poll — checks for ``<job_dir>/.cancel``.

    Operators (CLI / dashboard) signal cancellation by creating the
    file. The orchestrator's loops poll this on every iteration and
    transition to CANCELLED at the next checkpoint.

    Why filesystem signal:
    * Out-of-process: the CLI's ``sovyx voice training cancel <id>``
      runs as a separate Python process from the daemon's
      orchestrator task — file creation is the simplest cross-process
      signal that doesn't require IPC infrastructure.
    * Survives crashes: if the orchestrator crashed mid-job and is
      restarted by a daemon respawn, the ``.cancel`` file persists
      and the resumed orchestrator picks up the cancellation.
    * Easy to inspect: operators on a ssh session can ``ls`` the
      job dir to see whether cancellation was signalled.
    """
    cancel_path = job_dir / ".cancel"

    def _check() -> bool:
        return cancel_path.exists()

    return _check


__all__ = [
    "TrainingOrchestrator",
    "TrainingRequest",
]
