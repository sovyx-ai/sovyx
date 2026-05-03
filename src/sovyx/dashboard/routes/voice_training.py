"""Voice wake-word training dashboard endpoints — Phase 8 / T8.13.

Endpoints (all under ``/api/voice/training``, auth required):

* ``POST /jobs/start`` — start a new training job (Mission
  ``MISSION-v0.30.0-single-mind-ga-2026-05-03.md`` §T1.1 D1). Returns
  HTTP 202 Accepted with ``{"job_id": ..., "stream_url": ...}``;
  spawns the orchestrator via ``sovyx.observability.tasks.spawn`` —
  the same fire-and-forget pattern ``brain/consolidation.py:550``
  uses for ``consolidation-scheduler``. Idempotency via slugified
  ``job_id`` (re-submit while a job is in flight returns HTTP 409
  Conflict). Fail-fast on missing trainer backend (HTTP 503).

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

Pre-v0.30.0 historical note (resolved by T1.1 above): an earlier
docstring explained "no POST /jobs" by claiming dashboard-side
creation needed a background-job queue (Celery / RQ / similar) that
"doesn't yet exist in Sovyx + isn't worth pulling in for one feature".
v0.30.0 closes that gap WITHOUT pulling in a job-queue framework —
``observability.tasks.spawn`` provides the fire-and-forget primitive
already proven by ConsolidationScheduler + DreamScheduler. Single-
process Sovyx; one async task per training job; cancellation via
the existing ``.cancel`` filesystem signal.

Reference: master mission ``MISSION-voice-final-skype-grade-2026.md``
§Phase 8 / T8.13 + T8.14.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from starlette.status import (
    HTTP_202_ACCEPTED,
    HTTP_400_BAD_REQUEST,
    HTTP_404_NOT_FOUND,
    HTTP_409_CONFLICT,
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


class StartTrainingRequest(BaseModel):
    """Body for ``POST /api/voice/training/jobs/start``.

    Mission ``MISSION-v0.30.0-single-mind-ga-2026-05-03.md`` §T1.1
    (D1). Mirrors the CLI's ``TrainingRequest`` shape so dashboard
    + CLI produce bit-exact training jobs for the same inputs.
    """

    wake_word: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description=(
            "The wake word to train. Diacritics preserved for audit "
            "logs; synthesizer + backend handle ASCII-folding. The "
            'slugified form derives the ``job_id`` (e.g., "Lúcia" → '
            '"lucia").'
        ),
    )
    mind_id: str = Field(
        ...,
        max_length=64,
        description=(
            "Mind that owns the resulting model. Empty string is "
            "permitted for unattached training. On COMPLETE the "
            "model is hot-reloaded into ``WakeWordRouter.register_mind"
            "(mind_id)`` if the daemon is running."
        ),
    )
    language: str = Field(
        default="en",
        max_length=16,
        description=(
            'BCP-47 language tag (e.g., ``"en"``, ``"pt-BR"``). '
            "Threaded through to Kokoro synthesizer + backend "
            "phoneme tables."
        ),
    )
    target_samples: int = Field(
        default=200,
        ge=100,
        le=10000,
        description=(
            "How many positive samples the synthesizer produces. "
            "200 is the conservative minimum per OpenWakeWord docs; "
            "scales linearly with training time (~30-60 min for 200, "
            "~3-5h for 1000+)."
        ),
    )
    voices: list[str] = Field(
        default_factory=list,
        description=(
            "Override Kokoro voice catalogue. Empty list uses the "
            "synthesizer's per-language defaults."
        ),
    )
    variants: list[str] = Field(
        default_factory=list,
        description=(
            "Phrases to render. Empty list uses the default variant "
            'set ``[wake_word, f"hey {wake_word}"]`` matching the '
            "CLI behavior."
        ),
    )
    negatives_dir: str = Field(
        ...,
        min_length=1,
        description=(
            "Filesystem path to operator-provided non-wake-word audio. "
            "Backend reads ``*.wav`` files from here. Operators MUST "
            "populate this before invoking; the orchestrator does NOT "
            "generate negative samples."
        ),
    )


class StartTrainingResponse(BaseModel):
    """Response for ``POST /api/voice/training/jobs/start``.

    HTTP 202 Accepted: the job has been spawned in the background;
    poll ``GET /jobs/{job_id}`` for state OR open the WebSocket at
    ``stream_url`` for live snapshots.
    """

    job_id: str = Field(
        ...,
        description=(
            "Filesystem-safe slug derived from ``wake_word``. "
            "Matches the directory name under "
            "``<data_dir>/wake_word_training/``."
        ),
    )
    stream_url: str = Field(
        ...,
        description=(
            "Relative path of the WebSocket stream for live progress "
            "(e.g., ``/api/voice/training/jobs/lucia/stream``). "
            "Frontend opens ``new WebSocket(host + stream_url + "
            '"?token=" + token)`` to subscribe.'
        ),
    )


# ── Helpers (T1.1 — D1) ─────────────────────────────────────────────


_TERMINAL_STATUSES = {"complete", "failed", "cancelled"}
"""Status values that indicate the orchestrator has exited the job
(no further state transitions). Used by T1.1's 409 Conflict check
to distinguish "job exists but ended" (re-submit OK) from "job in
flight" (reject with 409)."""


def _slugify_for_filesystem(text: str) -> str:
    """ASCII-fold + alnum-only normalisation for job-id derivation.

    Mirrors :func:`sovyx.cli.commands.voice._slugify_for_filesystem`
    bit-exactly so CLI and dashboard produce identical ``job_id`` for
    the same ``wake_word``. Defensive copy here (rather than import
    from CLI) keeps the dashboard route layer free of CLI-package
    coupling.
    """
    import unicodedata  # noqa: PLC0415

    decomposed = unicodedata.normalize("NFKD", text)
    folded = "".join(c for c in decomposed if not unicodedata.combining(c)).lower()
    return "".join(c if (c.isascii() and c.isalnum()) else "_" for c in folded)[:48]


def _job_in_flight(job_dir: Path) -> bool:
    """Return ``True`` when the job directory has a non-terminal
    most-recent state (orchestrator is still running OR the daemon
    crashed mid-training without writing a terminal state).

    ``False`` when:
    * The job directory does not exist (no prior job — fresh start).
    * ``progress.jsonl`` does not exist (incomplete artifact —
      caller can overwrite).
    * Most-recent state is in :data:`_TERMINAL_STATUSES` (job ended;
      caller may re-submit to retrain).
    """
    if not job_dir.is_dir():
        return False
    progress_path = job_dir / "progress.jsonl"
    if not progress_path.is_file():
        return False
    tracker = ProgressTracker(progress_path)
    latest = tracker.latest()
    if latest is None:
        return False
    return latest.status.value not in _TERMINAL_STATUSES


# ── Endpoints ───────────────────────────────────────────────────────


@router.post(
    "/jobs/start",
    response_model=StartTrainingResponse,
    status_code=HTTP_202_ACCEPTED,
)
async def start_training_job(
    request: Request,
    body: StartTrainingRequest,
) -> StartTrainingResponse:
    """Spawn a new wake-word training job in the background.

    Mission ``MISSION-v0.30.0-single-mind-ga-2026-05-03.md`` §T1.1
    (D1). Returns HTTP 202 Accepted with the ``job_id`` + WebSocket
    ``stream_url`` for live progress; the orchestrator runs as an
    async task via :func:`sovyx.observability.tasks.spawn` (the same
    fire-and-forget primitive ``brain/consolidation.py:550`` uses).

    Idempotency contract:
      * Slugified ``wake_word`` derives the ``job_id`` (matches the
        CLI's :func:`_slugify_for_filesystem` 1:1).
      * Re-submitting while a job with the same ``job_id`` is in
        flight (most-recent state non-terminal) returns HTTP 409
        Conflict. Operator must explicitly cancel + retry.
      * Re-submitting after a job completes / fails / cancels is
        permitted and overwrites the prior ``progress.jsonl`` —
        operator's intent to retrain is the signal.

    Failure modes:
      * 409 Conflict — job_id already in flight (see idempotency).
      * 422 Unprocessable Content — body validation (pydantic).
      * 503 Service Unavailable — trainer backend not registered
        (operator hasn't installed the extras OR called
        :func:`register_default_backend`). Detail carries the
        registration command.
      * 500 Internal Server Error — orchestrator construction failed
        (Kokoro model missing, etc.). Detail carries the underlying
        exception text.

    Returns:
        :class:`StartTrainingResponse` with ``job_id`` + ``stream_url``.

    Raises:
        HTTPException: see failure modes above.
    """
    # ── 1. Resolve trainer backend (fail-fast UX — same as CLI 384-389)
    try:
        from sovyx.voice.wake_word_training import (  # noqa: PLC0415
            resolve_default_backend,
        )

        backend = resolve_default_backend()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "voice.training.start.backend_unavailable",
            **{"voice.training.error": str(exc)},
        )
        raise HTTPException(
            status_code=HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"Trainer backend unavailable: {exc}. Install the "
                f"trainer extras + register a backend via "
                f"``register_default_backend()``. See Phase 8 / T8.13 "
                f"docs for the pluggable Protocol contract."
            ),
        ) from None

    # ── 2. Resolve job_dir + idempotency check
    training_root = _resolve_training_root(request)
    job_id = _slugify_for_filesystem(body.wake_word)
    if not any(c.isascii() and c.isalnum() for c in job_id):
        raise HTTPException(
            status_code=HTTP_400_BAD_REQUEST,
            detail=(
                "wake_word produced no ASCII alphanumeric characters "
                "after fold (e.g. Chinese-only / Cyrillic-only input). "
                "Use ASCII characters or romanise the name "
                "(e.g. 'Ni hao' instead of '你好')."
            ),
        )

    job_dir = training_root / job_id
    if _job_in_flight(job_dir):
        raise HTTPException(
            status_code=HTTP_409_CONFLICT,
            detail=(
                f"A training job for '{body.wake_word}' (job_id='{job_id}') "
                f"is already in flight. Cancel it via "
                f"``POST /api/voice/training/jobs/{job_id}/cancel`` before "
                f"submitting a new one, OR wait for it to finish."
            ),
        )

    # ── 3. Validate negatives_dir exists (operator-actionable error
    # at 400 rather than failing 5 minutes into synthesis)
    negatives_path = Path(body.negatives_dir)
    if not negatives_path.is_dir():
        raise HTTPException(
            status_code=HTTP_400_BAD_REQUEST,
            detail=(
                f"negatives_dir does not exist or is not a directory: "
                f"{negatives_path}. Provide a directory containing "
                f"``*.wav`` files of non-wake-word audio (your own "
                f"speech recordings, common-voice samples, ambient "
                f"audio)."
            ),
        )

    # ── 4. Build TrainingRequest (mirrors CLI lines 440-449)
    from sovyx.voice.wake_word_training import (  # noqa: PLC0415
        TrainingRequest,
    )

    voice_tuple = tuple(body.voices)
    if body.variants:
        variant_tuple = tuple(body.variants)
    else:
        variant_tuple = (body.wake_word, f"hey {body.wake_word}")

    # Default output path mirrors the CLI: writes to the pretrained
    # pool so the next pipeline boot picks it up via PretrainedModelRegistry
    # (or hot-reloads via on_complete callback when the daemon is running).
    engine_config = _resolve_engine_config(request)
    data_dir = engine_config.data_dir if engine_config is not None else Path.home() / ".sovyx"
    output_path = data_dir / "wake_word_models" / "pretrained" / f"{job_id}.onnx"

    training_req = TrainingRequest(
        wake_word=body.wake_word,
        mind_id=body.mind_id,
        language=body.language,
        target_positive_samples=body.target_samples,
        synthesizer_voices=voice_tuple,
        synthesizer_variants=variant_tuple,
        negative_samples_dir=negatives_path,
        output_path=output_path,
    )

    # ── 5. Build orchestrator (mirrors CLI lines 463-488)
    job_dir.mkdir(parents=True, exist_ok=True)
    try:
        from sovyx.voice.tts_kokoro import KokoroTTS  # noqa: PLC0415
        from sovyx.voice.wake_word_training import (  # noqa: PLC0415
            KokoroSampleSynthesizer,
            TrainingOrchestrator,
        )

        kokoro_model_dir = data_dir / "models" / "voice"
        kokoro = KokoroTTS(model_dir=kokoro_model_dir)
        synthesizer = KokoroSampleSynthesizer(tts=kokoro)
        progress_tracker = ProgressTracker(job_dir / "progress.jsonl")
        orchestrator = TrainingOrchestrator(
            synthesizer=synthesizer,
            backend=backend,
            progress_tracker=progress_tracker,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "voice.training.start.orchestrator_init_failed",
            **{"voice.training.job_id": job_id},
        )
        raise HTTPException(
            status_code=500,
            detail=f"orchestrator construction failed: {exc}",
        ) from exc

    # ── 6. Spawn fire-and-forget task (the consolidation-scheduler
    # pattern; per D1, the right shape for single-process Sovyx).
    # The orchestrator handles cancellation via the existing
    # ``<job_dir>/.cancel`` filesystem-signal path; no need for a
    # separate cancel_check callable.
    from sovyx.observability.tasks import spawn  # noqa: PLC0415

    spawn(
        orchestrator.run(training_req, job_dir=job_dir),
        name=f"training-{job_id}",
    )

    logger.info(
        "voice.training.start.spawned",
        **{
            "voice.training.job_id": job_id,
            "voice.training.wake_word": body.wake_word,
            "voice.training.mind_id": body.mind_id,
            "voice.training.language": body.language,
            "voice.training.target_samples": body.target_samples,
            "voice.training.backend": backend.name,
        },
    )

    return StartTrainingResponse(
        job_id=job_id,
        stream_url=f"/api/voice/training/jobs/{job_id}/stream",
    )


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
    "StartTrainingRequest",
    "StartTrainingResponse",
    "TrainingJobDetailResponse",
    "TrainingJobSummary",
    "TrainingJobsResponse",
    "router",
]
