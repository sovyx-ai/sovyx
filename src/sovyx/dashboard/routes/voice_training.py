"""Voice wake-word training dashboard endpoints — Phase 8 / T8.13.

Read-only observability + cancellation surface for the wake-word
training pipeline (the CLI ``sovyx voice train-wake-word`` runs the
actual training; this module surfaces job state to dashboards).

Endpoints (all under ``/api/voice/training``, auth required):

* ``GET /jobs`` — list every job in the training root with its
  latest state. Operators see all in-flight + historical jobs.

* ``GET /jobs/{job_id}`` — single job's latest state + recent
  history (last 200 progress events). Dashboards render a
  per-job timeline + current-status panel.

* ``POST /jobs/{job_id}/cancel`` — touch the ``<job_dir>/.cancel``
  file. The orchestrator polls this on every cycle and transitions
  to CANCELLED. Idempotent: cancelling an already-cancelled or
  already-complete job is a no-op (file creation is idempotent;
  terminal states ignore the signal).

Why no ``POST /jobs`` (start-from-dashboard):
  Training takes 30-60 minutes. A POST request that runs for that
  long would tie up the dashboard's worker, block the operator's
  UI from receiving updates, and blow past every reasonable HTTP
  timeout. The CLI ``sovyx voice train-wake-word`` is the
  job-creation surface — operators run it from a terminal session
  + observe via dashboard. Dashboard-side job-creation requires a
  background-job queue (Celery / RQ / similar) that doesn't yet
  exist in Sovyx + isn't worth pulling in for one feature.

Reference: master mission ``MISSION-voice-final-skype-grade-2026.md``
§Phase 8 / T8.13 + T8.14.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from starlette.status import (
    HTTP_400_BAD_REQUEST,
    HTTP_404_NOT_FOUND,
    HTTP_503_SERVICE_UNAVAILABLE,
)

from sovyx.dashboard.routes._deps import verify_token
from sovyx.observability.logging import get_logger
from sovyx.voice.wake_word_training._progress import ProgressTracker

if TYPE_CHECKING:
    from sovyx.engine.config import EngineConfig
    from sovyx.voice.wake_word_training._state import TrainingJobState


logger = get_logger(__name__)


router = APIRouter(prefix="/api/voice/training", dependencies=[Depends(verify_token)])


_HISTORY_DEFAULT_LIMIT = 200
"""Per-job history events returned by ``GET /jobs/{job_id}``. 200 is
generous enough to cover a typical 200-positive-sample run's
per-sample SYNTHESIZING events + training transitions, without
blowing out the JSON response size."""

_HISTORY_MAX_LIMIT = 1000


# ── Helpers ─────────────────────────────────────────────────────────


def _resolve_engine_config(request: Request) -> EngineConfig | None:
    return getattr(request.app.state, "engine_config", None)


def _resolve_training_root(request: Request) -> Path:
    """Return ``<data_dir>/wake_word_training/`` — the canonical
    location every training job lives under. CLI + dashboard agree
    on this path so both see the same set of jobs."""
    engine_config = _resolve_engine_config(request)
    if engine_config is not None:
        return engine_config.data_dir / "wake_word_training"
    return Path.home() / ".sovyx" / "wake_word_training"


def _list_job_dirs(training_root: Path) -> list[Path]:
    """Return every job directory under ``training_root``, sorted.

    A job directory is any subdirectory that contains a
    ``progress.jsonl`` file. Directories without that file are
    ignored (defensive — operators may create unrelated dirs by
    accident).

    Empty list when the training root doesn't exist (no jobs yet).
    """
    if not training_root.is_dir():
        return []
    out: list[Path] = []
    for entry in sorted(training_root.iterdir()):
        if entry.is_dir() and (entry / "progress.jsonl").is_file():
            out.append(entry)
    return out


def _state_to_dict(state: TrainingJobState) -> dict[str, str | int | float]:
    """Wrap :meth:`TrainingJobState.to_dict` so the route layer can
    decorate with computed fields without leaking serialisation
    concerns into the state module."""
    return state.to_dict()


# ── Request / response models ───────────────────────────────────────


class TrainingJobSummary(BaseModel):
    """One row in ``GET /jobs`` — most-recent state for a job."""

    job_id: str = Field(..., description="Filesystem-safe job identifier.")
    wake_word: str
    mind_id: str
    language: str
    status: str = Field(
        ...,
        description=(
            "``pending`` | ``synthesizing`` | ``training`` | "
            "``complete`` | ``failed`` | ``cancelled``."
        ),
    )
    progress: float = Field(..., ge=0.0, le=1.0)
    samples_generated: int = Field(..., ge=0)
    target_samples: int = Field(..., ge=0)
    started_at: str
    updated_at: str
    completed_at: str
    output_path: str
    error_summary: str
    cancelled_signalled: bool = Field(
        ...,
        description=(
            "True when ``<job_dir>/.cancel`` exists. Operators see "
            "this even before the orchestrator polls + writes the "
            "CANCELLED state to JSONL."
        ),
    )


class TrainingJobsResponse(BaseModel):
    jobs: list[TrainingJobSummary]
    total_count: int = Field(..., ge=0)


class TrainingJobDetailResponse(BaseModel):
    """Single-job view: latest state + recent history."""

    summary: TrainingJobSummary
    history: list[dict[str, str | int | float]] = Field(
        ...,
        description=(
            "Most-recent ``limit`` progress events (oldest-first). "
            "Each event is a JSON-serialisable state snapshot."
        ),
    )
    history_truncated: bool = Field(
        ...,
        description=(
            "True when the JSONL has more events than the response "
            "carries. Caller can bump ``limit`` (capped at 1000) "
            "to widen the window."
        ),
    )


class CancelJobResponse(BaseModel):
    job_id: str
    cancel_signal_written: bool = Field(
        ...,
        description=(
            "True when ``<job_dir>/.cancel`` was created (or already "
            "existed). The orchestrator polls this on every cycle + "
            "transitions to CANCELLED at the next checkpoint."
        ),
    )
    already_terminal: bool = Field(
        ...,
        description=(
            "True when the job's most-recent state is already "
            "terminal (COMPLETE / FAILED / CANCELLED). Cancel signal "
            "is still written for audit consistency, but it has no "
            "effect — orchestrator already exited."
        ),
    )


# ── Endpoints ───────────────────────────────────────────────────────


@router.get("/jobs", response_model=TrainingJobsResponse)
async def list_training_jobs(request: Request) -> TrainingJobsResponse:
    """List every wake-word training job under the data directory.

    Returns an empty list when the training root doesn't exist
    (no jobs have ever been created on this host). The dashboard's
    "Wake-word training" panel calls this on mount + polls every
    few seconds for live updates.
    """
    training_root = _resolve_training_root(request)
    job_dirs = _list_job_dirs(training_root)

    summaries: list[TrainingJobSummary] = []
    for job_dir in job_dirs:
        tracker = ProgressTracker(job_dir / "progress.jsonl")
        latest = tracker.latest()
        if latest is None:
            # ``progress.jsonl`` exists but has no parseable events —
            # treat as a "ghost" job with empty fields. Dashboard
            # surfaces it so the operator can manually clean up.
            continue
        summaries.append(
            TrainingJobSummary(
                job_id=job_dir.name,
                wake_word=latest.wake_word,
                mind_id=latest.mind_id,
                language=latest.language,
                status=latest.status.value,
                progress=latest.progress,
                samples_generated=latest.samples_generated,
                target_samples=latest.target_samples,
                started_at=latest.started_at,
                updated_at=latest.updated_at,
                completed_at=latest.completed_at,
                output_path=latest.output_path,
                error_summary=latest.error_summary,
                cancelled_signalled=(job_dir / ".cancel").exists(),
            )
        )

    return TrainingJobsResponse(jobs=summaries, total_count=len(summaries))


@router.get(
    "/jobs/{job_id}",
    response_model=TrainingJobDetailResponse,
)
async def get_training_job(
    request: Request,
    job_id: str,
    limit: int = _HISTORY_DEFAULT_LIMIT,
) -> TrainingJobDetailResponse:
    """Return one job's latest state + recent history.

    Args:
        job_id: Filesystem-safe job identifier (matches the directory
            name under ``<data_dir>/wake_word_training/``).
        limit: Most-recent N progress events to return (clamped to
            ``[1, 1000]``). Default 200.

    Raises:
        HTTPException 400: ``job_id`` is empty / contains path separators.
        HTTPException 404: Job directory or progress.jsonl missing.
    """
    if not job_id.strip():
        raise HTTPException(
            status_code=HTTP_400_BAD_REQUEST,
            detail="job_id must be a non-empty string",
        )
    # Path-traversal defence: refuse separators + parent refs even
    # though Path's ``/`` operator below would otherwise tolerate
    # them. Defensive in depth — operators on shared hosts shouldn't
    # be able to peek at sibling-tenant data via crafted job_ids.
    if "/" in job_id or "\\" in job_id or ".." in job_id:
        raise HTTPException(
            status_code=HTTP_400_BAD_REQUEST,
            detail="job_id may not contain path separators or '..'",
        )

    training_root = _resolve_training_root(request)
    job_dir = training_root / job_id
    progress_path = job_dir / "progress.jsonl"

    if not job_dir.is_dir() or not progress_path.is_file():
        raise HTTPException(
            status_code=HTTP_404_NOT_FOUND,
            detail=f"job not found: {job_id}",
        )

    bounded_limit = max(1, min(_HISTORY_MAX_LIMIT, limit))
    tracker = ProgressTracker(progress_path)
    events = tracker.read_all()
    if not events:
        raise HTTPException(
            status_code=HTTP_404_NOT_FOUND,
            detail=(
                f"job_id={job_id!r}'s progress.jsonl has no parseable "
                f"events. The job may have crashed before its first "
                f"PENDING write."
            ),
        )

    latest_state = events[-1].state
    history_states = events[-bounded_limit:]
    history_truncated = len(events) > bounded_limit

    summary = TrainingJobSummary(
        job_id=job_id,
        wake_word=latest_state.wake_word,
        mind_id=latest_state.mind_id,
        language=latest_state.language,
        status=latest_state.status.value,
        progress=latest_state.progress,
        samples_generated=latest_state.samples_generated,
        target_samples=latest_state.target_samples,
        started_at=latest_state.started_at,
        updated_at=latest_state.updated_at,
        completed_at=latest_state.completed_at,
        output_path=latest_state.output_path,
        error_summary=latest_state.error_summary,
        cancelled_signalled=(job_dir / ".cancel").exists(),
    )

    return TrainingJobDetailResponse(
        summary=summary,
        history=[_state_to_dict(e.state) for e in history_states],
        history_truncated=history_truncated,
    )


@router.post(
    "/jobs/{job_id}/cancel",
    response_model=CancelJobResponse,
)
async def cancel_training_job(
    request: Request,
    job_id: str,
) -> CancelJobResponse:
    """Signal cancellation by touching ``<job_dir>/.cancel``.

    Idempotent: re-cancelling an in-flight job is a no-op
    (filesystem ``touch`` is idempotent; orchestrator transitions
    to CANCELLED at the next checkpoint and writes the JSONL
    transition). Cancelling an already-terminal job creates the
    file for audit consistency but the orchestrator already exited
    + the file has no effect.

    Args:
        job_id: Filesystem-safe job identifier.

    Raises:
        HTTPException 400: ``job_id`` empty / contains path separators.
        HTTPException 404: Job directory doesn't exist.
        HTTPException 503: Engine config unavailable (daemon still
            booting).
    """
    if not job_id.strip():
        raise HTTPException(
            status_code=HTTP_400_BAD_REQUEST,
            detail="job_id must be a non-empty string",
        )
    if "/" in job_id or "\\" in job_id or ".." in job_id:
        raise HTTPException(
            status_code=HTTP_400_BAD_REQUEST,
            detail="job_id may not contain path separators or '..'",
        )

    if _resolve_engine_config(request) is None:
        # Refusing the cancel when EngineConfig isn't available
        # (very early boot) is safer than touching the home-dir
        # fallback which might point to a stale path.
        raise HTTPException(
            status_code=HTTP_503_SERVICE_UNAVAILABLE,
            detail="EngineConfig not available — daemon still booting",
        )

    training_root = _resolve_training_root(request)
    job_dir = training_root / job_id

    if not job_dir.is_dir():
        raise HTTPException(
            status_code=HTTP_404_NOT_FOUND,
            detail=f"job not found: {job_id}",
        )

    # Determine whether the job is already terminal so the response
    # tells the operator their cancel had no effect.
    progress_path = job_dir / "progress.jsonl"
    already_terminal = False
    if progress_path.is_file():
        tracker = ProgressTracker(progress_path)
        latest = tracker.latest()
        if latest is not None and latest.status.is_terminal:
            already_terminal = True

    cancel_path = job_dir / ".cancel"
    cancel_path.touch()
    logger.info(
        "voice.training.cancel_via_dashboard",
        **{
            "voice.job_id": job_id,
            "voice.already_terminal": already_terminal,
        },
    )

    return CancelJobResponse(
        job_id=job_id,
        cancel_signal_written=True,
        already_terminal=already_terminal,
    )


__all__ = [
    "CancelJobResponse",
    "TrainingJobDetailResponse",
    "TrainingJobSummary",
    "TrainingJobsResponse",
    "router",
]
