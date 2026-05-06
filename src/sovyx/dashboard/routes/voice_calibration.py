"""Voice calibration wizard dashboard endpoints (T3.2).

Endpoints (all under ``/api/voice/calibration``, auth required):

* ``POST /start`` -- launch a new calibration job. Returns HTTP 202
  Accepted with ``{"job_id": ..., "stream_url": ...}``; spawns the
  orchestrator via ``sovyx.observability.tasks.spawn`` (same fire-and-
  forget pattern as wake-word training). Idempotency: per ``mind_id``
  one job at a time -- re-submit while a job is in flight returns
  HTTP 409 Conflict.

* ``GET /jobs/{job_id}`` -- single job's most-recent snapshot. Returns
  HTTP 404 if the job directory or progress.jsonl does not exist.

* ``POST /jobs/{job_id}/cancel`` -- touch the ``<job_dir>/.cancel``
  file. The orchestrator polls this between every stage and transitions
  to CANCELLED at the next checkpoint. Idempotent: cancelling an
  already-terminal job is a no-op (file creation is idempotent;
  terminal states ignore the signal).

* ``GET /preview-fingerprint`` -- captures the host fingerprint
  (~1s) and returns a recommendation: ``"slow_path"`` always in
  v0.30.16 (FAST_PATH KB lookup wires up in v0.30.17+); the frontend
  uses this to decide which UX flow to render.

WebSocket (under ``/api/voice/calibration``):

* ``GET /jobs/{job_id}/stream?token=...`` -- live progress events
  from the JSONL tail. Auth via query-param token (FastAPI's
  ``Depends(verify_token)`` doesn't flow into WebSocket routes
  reliably -- same pattern as ``routes/logs.py:269`` and
  ``voice_training.py:78``). Emits one JSON message per state
  transition; closes cleanly once the job reaches a terminal state.

History: introduced in v0.30.16 as T3.2 of mission
``MISSION-voice-self-calibrating-system-2026-05-05.md`` Layer 3.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from pydantic import BaseModel, Field
from starlette.status import (
    HTTP_202_ACCEPTED,
    HTTP_404_NOT_FOUND,
    HTTP_409_CONFLICT,
)

from sovyx.dashboard.routes._deps import verify_token
from sovyx.observability.logging import get_logger
from sovyx.observability.privacy import short_hash
from sovyx.voice.calibration import (
    WizardJobState,
    WizardOrchestrator,
    WizardProgressTracker,
    capture_fingerprint,
)
from sovyx.voice.calibration._kb_cache import has_match

if TYPE_CHECKING:
    from pathlib import Path

    from sovyx.engine.config import EngineConfig

logger = get_logger(__name__)

router = APIRouter(
    prefix="/api/voice/calibration",
    dependencies=[Depends(verify_token)],
)
ws_router = APIRouter(prefix="/api/voice/calibration")


_WS_POLL_INTERVAL_S = 0.5
"""How often the WS handler polls the JSONL tail for new events.
500ms is a reasonable balance between responsiveness (operator sees
state transitions within half a second) and CPU cost (the tracker's
read_all is O(file size); per-job files stay small at <50 events)."""


# v0.30.30 (P2): per-job asyncio.Task registry so the cancel endpoint
# can call task.cancel() for mid-stage cancellation. The runner removes
# itself in a finally block, so the registry self-prunes once the
# orchestrator reaches a terminal state. Keyed by job_id; one entry
# per in-flight job (matched by the same per-mind 409 contract that
# /start enforces).
_active_jobs: dict[str, asyncio.Task[None]] = {}


# ====================================================================
# Helpers (resolve data_dir + orchestrator)
# ====================================================================


def _resolve_engine_config(request: Request) -> EngineConfig | None:
    return getattr(request.app.state, "engine_config", None)


def _resolve_data_dir(request: Request) -> Path:
    """Return the Sovyx data directory for the running daemon.

    Falls back to ``~/.sovyx`` when the daemon's EngineConfig is not
    registered (e.g. dashboard running standalone without a daemon).
    """
    engine_config = _resolve_engine_config(request)
    if engine_config is not None:
        return engine_config.data_dir
    from pathlib import Path

    return Path.home() / ".sovyx"


def _resolve_orchestrator(request: Request) -> WizardOrchestrator:
    return WizardOrchestrator(data_dir=_resolve_data_dir(request))


def _job_in_flight(orch: WizardOrchestrator, job_id: str) -> bool:
    """True when the most-recent snapshot exists + is non-terminal."""
    tracker = WizardProgressTracker(orch.progress_path(job_id))
    latest = tracker.latest()
    if latest is None:
        return False
    return not latest.status.is_terminal


# ====================================================================
# Pydantic schemas (request + response)
# ====================================================================


class StartCalibrationRequest(BaseModel):
    """Body for ``POST /api/voice/calibration/start``."""

    mind_id: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description=(
            "The mind whose calibration to compute. Profile lands at "
            "``<data_dir>/<mind_id>/calibration.json``. Per-mind "
            "isolation: concurrent jobs for distinct minds run in "
            "parallel; same-mind concurrent submission returns 409."
        ),
    )


class StartCalibrationResponse(BaseModel):
    """Response for ``POST /api/voice/calibration/start`` (HTTP 202)."""

    job_id: str = Field(
        ...,
        description=(
            "Stable identifier; equal to ``mind_id`` for v0.30.16 "
            "(one calibration in flight per mind). Multi-job per "
            "mind support lands when the operator-explicit retry "
            "pattern wires up."
        ),
    )
    stream_url: str = Field(
        ...,
        description=(
            "Relative URL of the WebSocket for live progress events. "
            "Frontend MUST append ``?token=<sessionStorage-token>`` "
            "before opening (auth via query-param)."
        ),
    )


class WizardJobSnapshotResponse(BaseModel):
    """Response for ``GET /api/voice/calibration/jobs/{id}``."""

    job_id: str
    mind_id: str
    status: str  # WizardStatus.value
    progress: float
    current_stage_message: str
    created_at_utc: str
    updated_at_utc: str
    profile_path: str | None = None
    triage_winner_hid: str | None = None
    error_summary: str | None = None
    fallback_reason: str | None = None

    @classmethod
    def from_state(cls, state: WizardJobState) -> WizardJobSnapshotResponse:
        return cls(
            job_id=state.job_id,
            mind_id=state.mind_id,
            status=state.status.value,
            progress=state.progress,
            current_stage_message=state.current_stage_message,
            created_at_utc=state.created_at_utc,
            updated_at_utc=state.updated_at_utc,
            profile_path=state.profile_path,
            triage_winner_hid=state.triage_winner_hid,
            error_summary=state.error_summary,
            fallback_reason=state.fallback_reason,
        )


class CancelCalibrationResponse(BaseModel):
    """Response for ``POST /api/voice/calibration/jobs/{id}/cancel``."""

    job_id: str
    cancel_signal_written: bool = Field(
        ...,
        description=(
            "True when the .cancel file was created (or already "
            "existed). The orchestrator picks it up at the next "
            "stage checkpoint."
        ),
    )
    already_terminal: bool = Field(
        ...,
        description=(
            "True when the most-recent state was already terminal. "
            "Cancel signal is still written for audit consistency."
        ),
    )


class FeatureFlagResponse(BaseModel):
    """Response for ``GET /api/voice/calibration/feature-flag``.

    Mirrors :attr:`EngineConfig.voice.calibration_wizard_enabled`
    + records whether the current value came from the original
    config (env / system.yaml) or from a runtime override applied
    via ``POST /feature-flag``. Frontend uses this on app load to
    decide whether to mount the calibration onboarding step.
    """

    enabled: bool
    runtime_override_active: bool = Field(
        default=False,
        description=(
            "True when the value differs from what was loaded at boot "
            "(via env/system.yaml). Operators flipping the toggle in "
            "Settings -> Voice -> Advanced see this flip as well; the "
            "flag is in-memory only -- restart picks up the persisted "
            "config value again."
        ),
    )


class FeatureFlagUpdateRequest(BaseModel):
    """Body for ``POST /api/voice/calibration/feature-flag``."""

    enabled: bool = Field(
        ...,
        description=(
            "New value for the calibration wizard mount flag. "
            "Mutates `app.state.engine_config.voice.calibration_wizard_enabled` "
            "in-memory; persistent change still requires editing "
            "`SOVYX_VOICE__CALIBRATION_WIZARD_ENABLED` in env or system.yaml + "
            "daemon restart."
        ),
    )


class PreviewFingerprintResponse(BaseModel):
    """Response for ``GET /api/voice/calibration/preview-fingerprint``."""

    fingerprint_hash: str = Field(
        ...,
        description="SHA256 of the host fingerprint (L4 KB lookup key).",
    )
    audio_stack: str
    system_vendor: str
    system_product: str
    recommendation: str = Field(
        ...,
        description=(
            "Recommended path: ``slow_path`` always in v0.30.16. "
            "v0.30.17+ may return ``fast_path`` when the local KB has "
            "a high-confidence match for the fingerprint hash."
        ),
    )


# ====================================================================
# Endpoints
# ====================================================================


@router.post(
    "/start",
    response_model=StartCalibrationResponse,
    status_code=HTTP_202_ACCEPTED,
)
async def start_calibration_job(
    request: Request,
    body: StartCalibrationRequest,
) -> StartCalibrationResponse:
    """Spawn a new calibration wizard job in the background.

    Idempotency: same-mind concurrent submission returns HTTP 409.
    Re-submitting after the prior job reaches a terminal state is
    permitted (operator's intent to recalibrate).
    """
    orch = _resolve_orchestrator(request)
    job_id = body.mind_id  # v0.30.16: one job per mind, job_id == mind_id

    if _job_in_flight(orch, job_id):
        raise HTTPException(
            status_code=HTTP_409_CONFLICT,
            detail=(
                f"A calibration job for mind '{body.mind_id}' is already "
                f"in flight. Cancel it via POST "
                f"/api/voice/calibration/jobs/{job_id}/cancel before "
                f"submitting a new one."
            ),
        )

    # Spawn the orchestrator as a fire-and-forget asyncio task. The
    # task runs concurrently with the dashboard request handler; the
    # dashboard only blocks on the spawn call.
    async def _runner() -> None:
        try:
            await orch.run(job_id=job_id, mind_id=body.mind_id)
        except asyncio.CancelledError:
            # Mid-stage cancellation flowed through. The orchestrator's
            # own CancelledError handler at run()'s top-level emits the
            # CANCELLED state; we just let the cancellation surface.
            raise
        except Exception:
            logger.exception(
                "voice.calibration.wizard.runner_failed",
                job_id_hash=short_hash(job_id),
                mind_id_hash=short_hash(body.mind_id),
            )
        finally:
            # Self-prune from the registry whether we exited by terminal
            # state, cancellation, or unhandled exception. Idempotent.
            _active_jobs.pop(job_id, None)

    # Use asyncio.ensure_future so the task is properly scheduled in
    # the running event loop.
    task: asyncio.Task[None] = asyncio.ensure_future(_runner())  # noqa: RUF006
    _active_jobs[job_id] = task

    logger.info(
        "voice.calibration.wizard.start",
        job_id_hash=short_hash(job_id),
        mind_id_hash=short_hash(body.mind_id),
    )

    return StartCalibrationResponse(
        job_id=job_id,
        stream_url=f"/api/voice/calibration/jobs/{job_id}/stream",
    )


@router.get(
    "/jobs/{job_id}",
    response_model=WizardJobSnapshotResponse,
)
async def get_calibration_job(
    request: Request,
    job_id: str,
) -> WizardJobSnapshotResponse:
    """Return the most-recent snapshot for a calibration job.

    Returns 404 when the job directory or its progress.jsonl does not
    exist (no such job, or the operator deleted the work directory).
    """
    orch = _resolve_orchestrator(request)
    tracker = WizardProgressTracker(orch.progress_path(job_id))
    latest = tracker.latest()
    if latest is None:
        raise HTTPException(
            status_code=HTTP_404_NOT_FOUND,
            detail=f"Calibration job '{job_id}' not found.",
        )
    return WizardJobSnapshotResponse.from_state(latest)


@router.post(
    "/jobs/{job_id}/cancel",
    response_model=CancelCalibrationResponse,
)
async def cancel_calibration_job(
    request: Request,
    job_id: str,
) -> CancelCalibrationResponse:
    """Cancel a running calibration job.

    Two cancellation paths fire in sequence:

    1. **Mid-stage** (v0.30.30+) — if the job's orchestrator task is
       still running, ``task.cancel()`` propagates :class:`asyncio.CancelledError`
       into the awaited subprocess. The async-native runner translates
       this into a SIGTERM on the bash process group with a 10s grace
       period for the trap-EXIT cleanup to run, then escalates to
       SIGKILL if needed. Operator sees CANCELLED within seconds.
    2. **Checkpoint** — the ``.cancel`` file remains the durable signal
       so a later stage that started AFTER this endpoint fired (e.g.
       race between cancel + stage transition) still observes the
       cancellation at the next ``_is_cancelled`` checkpoint. Both
       paths converge on the orchestrator's CANCELLED state emit.

    Idempotent: cancelling an already-terminal job is a no-op (file
    creation is idempotent; ``task.cancel()`` on a finished task
    returns False; both are silent).
    """
    orch = _resolve_orchestrator(request)
    tracker = WizardProgressTracker(orch.progress_path(job_id))
    latest = tracker.latest()
    already_terminal = latest is not None and latest.status.is_terminal

    cancel_path = orch.cancel_path(job_id)
    cancel_path.parent.mkdir(parents=True, exist_ok=True)
    cancel_path.touch(exist_ok=True)

    # Mid-stage cancellation: cancel the orchestrator task if still
    # registered. Idempotent on already-finished tasks.
    task_cancelled = False
    task = _active_jobs.get(job_id)
    if task is not None and not task.done():
        task_cancelled = task.cancel()

    logger.info(
        "voice.calibration.wizard.cancel_signaled",
        job_id_hash=short_hash(job_id),
        already_terminal=already_terminal,
        task_cancelled=task_cancelled,
    )

    return CancelCalibrationResponse(
        job_id=job_id,
        cancel_signal_written=True,
        already_terminal=already_terminal,
    )


@router.get(
    "/preview-fingerprint",
    response_model=PreviewFingerprintResponse,
)
async def preview_fingerprint(request: Request) -> PreviewFingerprintResponse:
    """Capture the host fingerprint (~1s) + return a path recommendation.

    v0.30.18+: returns ``recommendation="fast_path"`` when the local
    KB cache (``<data_dir>/voice_calibration/_kb/<hash>.json``) has
    an entry for the captured fingerprint hash; ``"slow_path"``
    otherwise. The cache is populated automatically by every
    successful slow-path run, so a returning operator on the same
    hardware sees fast_path on the second + subsequent calibrations.
    """
    fingerprint = await asyncio.to_thread(capture_fingerprint)
    data_dir = _resolve_data_dir(request)
    recommendation: str = (
        "fast_path"
        if has_match(data_dir=data_dir, fingerprint_hash=fingerprint.fingerprint_hash)
        else "slow_path"
    )
    return PreviewFingerprintResponse(
        fingerprint_hash=fingerprint.fingerprint_hash,
        audio_stack=fingerprint.audio_stack,
        system_vendor=fingerprint.system_vendor,
        system_product=fingerprint.system_product,
        recommendation=recommendation,
    )


# ────────────────────────────────────────────────────────────────────
# Feature-flag endpoints (T3.10 wire-up)
# ────────────────────────────────────────────────────────────────────


def _boot_value(request: Request) -> bool | None:
    """Return the calibration_wizard_enabled value as loaded at boot.

    Captured the first time ``GET /feature-flag`` is hit by stashing
    the boot value on ``app.state``. Subsequent reads compare against
    this snapshot to decide whether to surface a runtime-override
    notice in the response.
    """
    return getattr(request.app.state, "calibration_wizard_boot_value", None)


@router.get(
    "/feature-flag",
    response_model=FeatureFlagResponse,
)
async def get_calibration_feature_flag(request: Request) -> FeatureFlagResponse:
    """Return the current calibration-wizard mount flag.

    Reads :attr:`EngineConfig.voice.calibration_wizard_enabled` from
    the running daemon. Falls back to ``False`` (default) when the
    daemon's EngineConfig is not registered, mirroring fresh-install
    behaviour.

    The ``runtime_override_active`` field tells the frontend whether
    the operator (or another caller) has flipped the value in-memory
    via ``POST /feature-flag`` since boot.
    """
    config = _resolve_engine_config(request)
    if config is None:
        return FeatureFlagResponse(enabled=False, runtime_override_active=False)

    current = config.voice.calibration_wizard_enabled
    boot = _boot_value(request)
    if boot is None:
        # First read: stash boot value for future override-detection.
        request.app.state.calibration_wizard_boot_value = current
        boot = current
    return FeatureFlagResponse(
        enabled=current,
        runtime_override_active=current != boot,
    )


@router.post(
    "/feature-flag",
    response_model=FeatureFlagResponse,
)
async def set_calibration_feature_flag(
    request: Request,
    body: FeatureFlagUpdateRequest,
) -> FeatureFlagResponse:
    """Flip the calibration wizard mount flag in-memory.

    Mutates :attr:`EngineConfig.voice.calibration_wizard_enabled` on
    the running daemon. The change is NOT persisted -- a daemon
    restart reverts to the env / system.yaml value. For permanent
    changes, edit ``SOVYX_VOICE__CALIBRATION_WIZARD_ENABLED`` in
    your env or ``voice.calibration_wizard_enabled`` in system.yaml.

    Operator-visible toggle in Settings -> Voice -> Advanced calls
    this endpoint when the operator clicks the switch.
    """
    config = _resolve_engine_config(request)
    if config is None:
        raise HTTPException(
            status_code=HTTP_404_NOT_FOUND,
            detail=(
                "EngineConfig not registered on this dashboard -- the "
                "feature-flag toggle requires a running daemon. Run "
                "`sovyx start` first."
            ),
        )

    # Stash boot value the first time we see it so override-detection
    # works even when GET /feature-flag wasn't hit before this POST.
    if _boot_value(request) is None:
        request.app.state.calibration_wizard_boot_value = config.voice.calibration_wizard_enabled

    previous = config.voice.calibration_wizard_enabled
    config.voice.calibration_wizard_enabled = body.enabled

    logger.info(
        "voice.calibration.feature_flag.toggled",
        previous=previous,
        new=body.enabled,
        boot_value=_boot_value(request),
    )

    boot = _boot_value(request)
    return FeatureFlagResponse(
        enabled=body.enabled,
        runtime_override_active=body.enabled != boot,
    )


# ====================================================================
# WebSocket endpoint (live progress events)
# ====================================================================


@ws_router.websocket("/jobs/{job_id}/stream")
async def stream_calibration_job(
    websocket: WebSocket,
    job_id: str,
    token: str = "",
) -> None:
    """Stream live progress events for one calibration job.

    Auth via query-param ``token`` (FastAPI's ``Depends(verify_token)``
    doesn't flow into WebSocket routes reliably; same pattern as
    ``routes/logs.py:269`` and ``voice_training.py:78``). The token
    must match ``request.app.state.auth_token`` set by
    ``server.create_app``.

    Emits one JSON message per state transition; closes cleanly once
    the job reaches a terminal state.
    """
    expected_token = getattr(websocket.app.state, "auth_token", None)
    if expected_token is None or token != expected_token:
        logger.info(
            "voice.calibration.wizard.subscriber_rejected",
            job_id_hash=short_hash(job_id),
            reason="auth",
        )
        await websocket.close(code=1008, reason="auth")
        return

    await websocket.accept()
    logger.info(
        "voice.calibration.wizard.subscriber_connected",
        job_id_hash=short_hash(job_id),
    )

    data_dir = (
        websocket.app.state.engine_config.data_dir
        if hasattr(websocket.app.state, "engine_config")
        and websocket.app.state.engine_config is not None
        else None
    )
    if data_dir is None:
        from pathlib import Path

        data_dir = Path.home() / ".sovyx"

    orch = WizardOrchestrator(data_dir=data_dir)
    tracker = WizardProgressTracker(orch.progress_path(job_id))

    sent_line_no = 0
    try:
        # Initial snapshot of all events that already exist.
        events = tracker.read_all()
        for event in events:
            await websocket.send_json(event.state.to_dict())
            sent_line_no = event.line_no
            if event.state.status.is_terminal:
                # Job already completed before subscribe; close cleanly.
                await websocket.close()
                return

        # Live tail loop: poll for new events every _WS_POLL_INTERVAL_S.
        while True:
            await asyncio.sleep(_WS_POLL_INTERVAL_S)
            events = tracker.read_all()
            for event in events:
                if event.line_no <= sent_line_no:
                    continue
                await websocket.send_json(event.state.to_dict())
                sent_line_no = event.line_no
                if event.state.status.is_terminal:
                    await websocket.close()
                    return
    except WebSocketDisconnect:
        logger.info(
            "voice.calibration.wizard.subscriber_disconnected",
            job_id_hash=short_hash(job_id),
            reason="client_close",
        )
        return
    except Exception:
        logger.exception(
            "voice.calibration.wizard.ws_stream_failed",
            job_id_hash=short_hash(job_id),
        )
        with contextlib.suppress(Exception):
            await websocket.close(code=1011, reason="stream_error")
    finally:
        # Best-effort cleanup telemetry: emitted whether the loop
        # exited via terminal-close, client disconnect, or error
        # (the WebSocketDisconnect branch already emitted; this is
        # the catch-all for the terminal-close path so dashboards see
        # one disconnected event per connection).
        with contextlib.suppress(Exception):
            logger.debug(
                "voice.calibration.wizard.subscriber_loop_exited",
                job_id_hash=short_hash(job_id),
            )
