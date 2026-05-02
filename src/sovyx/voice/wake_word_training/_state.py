"""Training-job state types — Phase 8 / T8.13.

Pure-typing module: no I/O, no side effects. The orchestrator (next
commit) uses these types as the canonical state-machine surface.

State transitions (enforced by the orchestrator, NOT this module):

  PENDING ─→ SYNTHESIZING ─→ TRAINING ─┬→ COMPLETE
                                      ├→ FAILED
                                      └→ CANCELLED

Terminal states (COMPLETE / FAILED / CANCELLED) accept no further
transitions. Resume logic loads the most-recent non-terminal state
+ replays from there.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum


class TrainingStatus(StrEnum):
    """Lifecycle phases of a wake-word training job.

    Stable wire format — emitted to the JSONL progress log + dashboard
    websocket. Renaming a member is a breaking schema change for
    downstream auditors. Adding members is OK; the orchestrator uses
    a closed-set match so unknown values fail loudly.
    """

    PENDING = "pending"
    """Job created but synthesis hasn't started yet."""

    SYNTHESIZING = "synthesizing"
    """Generating positive (TTS-augmented "wake word" utterances) +
    negative (non-wake utterances) sample sets. CPU-bound."""

    TRAINING = "training"
    """OpenWakeWord (or pluggable backend) training loop running.
    GPU-bound when CUDA available, otherwise CPU."""

    COMPLETE = "complete"
    """Training succeeded; ``output_path`` points to the trained
    ``.onnx``. Hot-reload into ``WakeWordRouter`` happens AFTER
    this state via ``register_mind``."""

    FAILED = "failed"
    """Training raised a non-cancellation exception. Inspect
    ``error_summary`` for the reason."""

    CANCELLED = "cancelled"
    """Operator cancelled via filesystem signal (presence of
    ``<job_dir>/.cancel`` file) or CLI/dashboard cancel call.
    Distinguished from FAILED so dashboards render differently."""

    @property
    def is_terminal(self) -> bool:
        """``True`` when the state accepts no further transitions."""
        return self in (
            TrainingStatus.COMPLETE,
            TrainingStatus.FAILED,
            TrainingStatus.CANCELLED,
        )


@dataclass(frozen=True, slots=True)
class TrainingJobState:
    """Snapshot of a training job at a point in time.

    Frozen so concurrent observers (dashboard polling, CLI status
    command, daemon's resume path) all see consistent data without
    locking. The orchestrator emits a new instance via the progress
    tracker on every state transition.

    Attributes:
        wake_word: The operator-configured wake word being trained
            (e.g. ``"Lúcia"``). Stored verbatim with diacritics so
            audit trails show the original input.
        mind_id: Mind that owns this training job. Empty string is
            permitted for global / unattached training (operator-
            initiated outside the per-mind flow); the orchestrator
            does NOT match the empty string against any
            ``register_mind`` call.
        language: BCP-47 language tag (``"pt-BR"``, etc.). Drives
            TTS voice selection during synthesis + phoneme tables
            during training.
        status: Current lifecycle phase.
        progress: 0.0 to 1.0 fractional progress within the current
            ``status`` phase. Resets to 0.0 on each phase
            transition. Operators see a continuous bar by stitching
            phase × progress (UI concern, not state-machine).
        message: Human-readable status line (e.g. "Generating sample
            123/200" or "Training epoch 4/10"). Localised at the
            UI layer; this field is the operator-language English
            default. Empty when no message applicable.
        started_at: ISO-8601 UTC timestamp at PENDING → SYNTHESIZING
            transition. Empty string before that.
        updated_at: ISO-8601 UTC timestamp of this snapshot.
        completed_at: ISO-8601 UTC timestamp at the terminal-state
            transition. Empty string for non-terminal snapshots.
        output_path: Filesystem path to the trained ``.onnx`` (only
            populated when ``status == COMPLETE``). Empty string
            otherwise.
        error_summary: One-line error description when
            ``status == FAILED``. Empty string otherwise.
        samples_generated: Number of synthesised positive samples
            so far (incremented during SYNTHESIZING).
        target_samples: Total positive samples the synthesizer
            aims to generate. Stable across the job's lifetime.
    """

    wake_word: str
    mind_id: str
    language: str
    status: TrainingStatus
    progress: float
    message: str
    started_at: str
    updated_at: str
    completed_at: str
    output_path: str
    error_summary: str
    samples_generated: int
    target_samples: int

    @classmethod
    def initial(
        cls,
        *,
        wake_word: str,
        mind_id: str = "",
        language: str = "en-US",
        target_samples: int = 200,
    ) -> TrainingJobState:
        """Construct a fresh PENDING-state job snapshot.

        ``updated_at`` is set to the current UTC time;
        ``started_at`` / ``completed_at`` / ``output_path`` /
        ``error_summary`` are empty.

        Args:
            wake_word: The wake word to train.
            mind_id: Owning mind (empty for global training).
            language: BCP-47 tag.
            target_samples: Positive sample target. Default 200 is
                the conservative minimum for reasonable accuracy
                per the OpenWakeWord training docs; operators with
                fewer compute hours can reduce, but accuracy
                degrades sub-100.

        Returns:
            A frozen :class:`TrainingJobState` ready to be written
            via :meth:`ProgressTracker.append`.
        """
        now_iso = datetime.now(UTC).isoformat()
        return cls(
            wake_word=wake_word,
            mind_id=mind_id,
            language=language,
            status=TrainingStatus.PENDING,
            progress=0.0,
            message="",
            started_at="",
            updated_at=now_iso,
            completed_at="",
            output_path="",
            error_summary="",
            samples_generated=0,
            target_samples=target_samples,
        )

    def with_status(
        self,
        new_status: TrainingStatus,
        *,
        progress: float | None = None,
        message: str | None = None,
        output_path: str | None = None,
        error_summary: str | None = None,
        samples_generated: int | None = None,
    ) -> TrainingJobState:
        """Return a NEW state with ``status`` updated + timestamps refreshed.

        Frozen-dataclass semantics: this method NEVER mutates self.
        Callers (the orchestrator) replace their snapshot reference
        with the return value.

        Updates ``updated_at`` to ``datetime.now(UTC)``; sets
        ``started_at`` on first non-PENDING transition; sets
        ``completed_at`` when the new status is terminal.
        """
        now_iso = datetime.now(UTC).isoformat()
        new_started_at = self.started_at
        if not new_started_at and new_status is not TrainingStatus.PENDING:
            new_started_at = now_iso
        new_completed_at = self.completed_at
        if new_status.is_terminal and not new_completed_at:
            new_completed_at = now_iso
        return TrainingJobState(
            wake_word=self.wake_word,
            mind_id=self.mind_id,
            language=self.language,
            status=new_status,
            progress=self.progress if progress is None else progress,
            message=self.message if message is None else message,
            started_at=new_started_at,
            updated_at=now_iso,
            completed_at=new_completed_at,
            output_path=self.output_path if output_path is None else output_path,
            error_summary=(self.error_summary if error_summary is None else error_summary),
            samples_generated=(
                self.samples_generated if samples_generated is None else samples_generated
            ),
            target_samples=self.target_samples,
        )

    def to_dict(self) -> dict[str, str | int | float]:
        """Serialise to a JSON-safe dict for the JSONL progress file
        + dashboard websocket. Every value is a primitive type."""
        return {
            "wake_word": self.wake_word,
            "mind_id": self.mind_id,
            "language": self.language,
            "status": self.status.value,
            "progress": self.progress,
            "message": self.message,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
            "output_path": self.output_path,
            "error_summary": self.error_summary,
            "samples_generated": self.samples_generated,
            "target_samples": self.target_samples,
        }


@dataclass(frozen=True)
class _StateTransitionRule:
    """Internal: which next-states are reachable from a current state.

    The orchestrator (future commit) consults this map to reject
    illegal transitions early. Encoded as a frozen dataclass so
    the rules are immutable across module reloads (defensive
    against hot-reload corruption — anti-pattern #2 patches).
    """

    allowed: frozenset[TrainingStatus] = field(default_factory=frozenset)


_TRANSITION_RULES: dict[TrainingStatus, _StateTransitionRule] = {
    TrainingStatus.PENDING: _StateTransitionRule(
        allowed=frozenset(
            {
                TrainingStatus.SYNTHESIZING,
                TrainingStatus.CANCELLED,
                TrainingStatus.FAILED,
            }
        ),
    ),
    TrainingStatus.SYNTHESIZING: _StateTransitionRule(
        allowed=frozenset(
            {
                TrainingStatus.TRAINING,
                TrainingStatus.CANCELLED,
                TrainingStatus.FAILED,
            }
        ),
    ),
    TrainingStatus.TRAINING: _StateTransitionRule(
        allowed=frozenset(
            {
                TrainingStatus.COMPLETE,
                TrainingStatus.CANCELLED,
                TrainingStatus.FAILED,
            }
        ),
    ),
    # Terminal states have empty allowed sets — any transition out
    # is rejected by ``is_legal_transition``.
    TrainingStatus.COMPLETE: _StateTransitionRule(),
    TrainingStatus.FAILED: _StateTransitionRule(),
    TrainingStatus.CANCELLED: _StateTransitionRule(),
}


def is_legal_transition(
    current: TrainingStatus,
    new: TrainingStatus,
) -> bool:
    """Return ``True`` when ``current → new`` is a permitted transition.

    Self-transitions are illegal (callers should use ``with_status``
    only on actual state changes; updating progress within the same
    state is :meth:`TrainingJobState.with_status` without a status
    parameter — a future overload). Terminal states reject all
    outgoing transitions.

    The orchestrator uses this guard to fail loudly on programmer
    errors rather than silently corrupting the JSONL log with
    invalid transitions.
    """
    rule = _TRANSITION_RULES.get(current)
    if rule is None:
        return False
    return new in rule.allowed


__all__ = [
    "TrainingJobState",
    "TrainingStatus",
    "is_legal_transition",
]
