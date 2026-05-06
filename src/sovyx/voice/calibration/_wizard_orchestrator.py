"""Wizard orchestrator: state machine for calibration wizard jobs.

Drives one calibration job from PENDING through PROBING, SLOW_PATH_DIAG,
SLOW_PATH_CALIBRATE, SLOW_PATH_APPLY, to DONE / FAILED / CANCELLED.
Persists every state transition to ``<job_dir>/progress.jsonl`` via
:class:`WizardProgressTracker` so dashboard subscribers can tail the
file for live progress.

Composes the v0.30.15 calibration pipeline behind a job lifecycle:

* :func:`capture_fingerprint` -- PROBING stage
* :func:`run_full_diag` (with ``--non-interactive``) -- SLOW_PATH_DIAG
* :func:`triage_tarball` + :func:`capture_measurements` -- SLOW_PATH_CALIBRATE
* :class:`CalibrationEngine` + :class:`CalibrationApplier` -- SLOW_PATH_APPLY

Cancellation contract:
* The orchestrator polls for ``<job_dir>/.cancel`` between every
  stage transition. When found, the next state is :data:`WizardStatus.CANCELLED`
  and the orchestrator exits cleanly.
* Mid-stage cancellation (e.g. during the 8-12 min diag run) is
  not yet supported in v0.30.16 -- the diag is a blocking subprocess
  and we don't kill it. Operator who cancels mid-stage will see
  CANCELLED only after the current stage completes. v0.30.17+ adds
  subprocess cancellation via :func:`subprocess.Popen.terminate`.

FAST_PATH branches (FAST_PATH_LOOKUP / APPLY / VALIDATE) are deferred
to v0.30.17+ when the local KB lookup wires up. v0.30.16 always takes
the SLOW_PATH for every job.

History: introduced in v0.30.16 as T3.1 of mission
``MISSION-voice-self-calibrating-system-2026-05-05.md`` Layer 3.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sovyx.observability.logging import get_logger
from sovyx.observability.privacy import short_hash
from sovyx.voice.calibration._applier import ApplyError, CalibrationApplier
from sovyx.voice.calibration._fingerprint import capture_fingerprint
from sovyx.voice.calibration._kb_cache import lookup_profile, store_profile
from sovyx.voice.calibration._measurer import capture_measurements
from sovyx.voice.calibration._wizard_progress import WizardProgressTracker
from sovyx.voice.calibration._wizard_state import WizardJobState, WizardStatus
from sovyx.voice.calibration.engine import CalibrationEngine
from sovyx.voice.calibration.schema import CalibrationProfile
from sovyx.voice.diagnostics import (
    DiagPrerequisiteError,
    DiagRunError,
    run_full_diag_async,
    triage_tarball,
)

if TYPE_CHECKING:
    from pathlib import Path

logger = get_logger(__name__)


_CANCEL_FILE = ".cancel"
_PROGRESS_FILE = "progress.jsonl"
# v0.30.31 (P3) capture-prompt side-channel. Bash writes one JSON line per
# operator-facing prompt; orchestrator tails the file every
# _PROMPTS_POLL_INTERVAL_S seconds and updates state.extras["current_prompt"]
# so the dashboard renders the speak/silence card in real time.
_PROMPTS_FILE = "prompts.jsonl"
_PROMPTS_POLL_INTERVAL_S = 0.5

_PENDING_MSG = "Calibration job created"
_PROBING_MSG = "Capturing hardware fingerprint"
_FAST_PATH_LOOKUP_MSG = "Looking up matching profile in local KB"
_FAST_PATH_APPLY_MSG = "Applying matched profile (fast path)"
_SLOW_PATH_DIAG_MSG = "Running forensic diagnostic (8-12 min)"
_SLOW_PATH_CALIBRATE_MSG = "Triaging diagnostic + capturing measurements"
_SLOW_PATH_APPLY_MSG = "Applying calibration profile"
_DONE_MSG = "Calibration complete"
_CANCELLED_MSG = "Calibration cancelled by operator"

# Coarse per-stage progress fractions for the dashboard progress bar.
_PROGRESS_PENDING = 0.0
_PROGRESS_PROBING = 0.05
_PROGRESS_FAST_PATH_LOOKUP = 0.30
_PROGRESS_FAST_PATH_APPLY = 0.85
_PROGRESS_SLOW_PATH_DIAG = 0.10
_PROGRESS_SLOW_PATH_CALIBRATE = 0.85
_PROGRESS_SLOW_PATH_APPLY = 0.92
_PROGRESS_DONE = 1.0


# v0.30.24 T3.8: closed-enum mapping from internal WizardStatus to the
# spec §8.3 ``step_entered{step=...}`` label. Statuses not in the map
# do not emit a step_entered event (e.g. PENDING, intermediate
# fast_path_apply / slow_path_calibrate which are part of a parent
# step that already fired).
_STEP_BY_STATUS: dict[str, str] = {
    "probing": "probe",
    "fast_path_lookup": "fast_path",
    "slow_path_diag": "slow_path",
    "fallback": "fallback",
}


def _terminal_step_label(state: "WizardJobState") -> str:  # noqa: UP037 -- forward ref
    """Map a terminal WizardJobState to the spec §8.3 ``cancelled{step}`` label.

    Bounded set: probe | fast_path | slow_path | review | fallback |
    unknown. Used by the cancelled-event emitter to surface WHERE the
    cancel was honoured (not the terminal CANCELLED status itself).
    """
    return _STEP_BY_STATUS.get(state.status.value, "unknown")


class WizardOrchestrator:
    """Run one calibration wizard job end-to-end.

    Stateless across calls -- each :meth:`run` invocation builds a
    fresh job state + tracker, so the same orchestrator instance can
    serve multiple concurrent jobs (one async task per job).

    Args:
        data_dir: The Sovyx data directory; per-job work directories
            land at ``<data_dir>/voice_calibration/<job_id>/``.
    """

    __slots__ = ("_data_dir", "_path_by_job", "_started_mono_by_job")

    def __init__(self, *, data_dir: Path) -> None:
        self._data_dir = data_dir
        # v0.30.24 T3.8: per-job tracking for spec-aligned telemetry.
        # The orchestrator is multi-job-safe (one async task per job),
        # so distinct keys never collide. Cleanup happens in the
        # terminal-emit helper to avoid leaking entries on long-lived
        # daemon processes.
        self._path_by_job: dict[str, str] = {}
        self._started_mono_by_job: dict[str, float] = {}

    def job_dir(self, job_id: str) -> Path:
        """Return ``<data_dir>/voice_calibration/<job_id>/``."""
        return self._data_dir / "voice_calibration" / job_id

    def progress_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / _PROGRESS_FILE

    def cancel_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / _CANCEL_FILE

    async def run(self, *, job_id: str, mind_id: str) -> WizardJobState:
        """Run the slow-path calibration pipeline for one job.

        Returns the final :class:`WizardJobState` (terminal). Always
        returns -- exceptions are caught and surfaced as
        :data:`WizardStatus.FAILED` snapshots, never propagated.

        Args:
            job_id: Job identifier; per-job dir + progress file derived.
            mind_id: The mind whose calibration to compute. Required.

        Returns:
            The final terminal :class:`WizardJobState`.
        """
        job_dir = self.job_dir(job_id)
        job_dir.mkdir(parents=True, exist_ok=True)
        tracker = WizardProgressTracker(self.progress_path(job_id))

        now = self._now()
        state = WizardJobState(
            job_id=job_id,
            mind_id=mind_id,
            status=WizardStatus.PENDING,
            progress=_PROGRESS_PENDING,
            current_stage_message=_PENDING_MSG,
            created_at_utc=now,
            updated_at_utc=now,
        )
        self._emit_state(state, tracker)
        # v0.30.24 T3.8: stash job start time for the spec-aligned
        # `completed` event's duration_s field.
        self._started_mono_by_job[job_id] = time.monotonic()
        # Telemetry: job lifecycle start. Closed-enum cardinality: only
        # mind_id is high-cardinality and we hash it (mission spec D7
        # bounded telemetry). job_id == mind_id in v0.30.16+ so we
        # emit a single hashed pair.
        logger.info(
            "voice.calibration.wizard.job_started",
            job_id_hash=short_hash(job_id),
            mind_id_hash=short_hash(mind_id),
        )

        try:
            terminal = await self._run_inner(job_id=job_id, state=state, tracker=tracker)
            self._emit_terminal_telemetry(terminal)
            return terminal
        except asyncio.CancelledError:
            cancelled = self._transition(
                state,
                status=WizardStatus.CANCELLED,
                progress=state.progress,
                message=_CANCELLED_MSG,
            )
            self._emit_state(cancelled, tracker)
            self._emit_terminal_telemetry(cancelled)
            raise
        except Exception as exc:  # noqa: BLE001 -- last-resort safety net
            logger.exception(
                "voice.calibration.wizard.unhandled",
                job_id_hash=short_hash(job_id),
                mind_id_hash=short_hash(mind_id),
            )
            failed = self._transition(
                state,
                status=WizardStatus.FAILED,
                progress=state.progress,
                message=f"Unhandled error: {type(exc).__name__}",
                error_summary=str(exc),
            )
            self._emit_state(failed, tracker)
            self._emit_terminal_telemetry(failed)
            return failed

    def _emit_terminal_telemetry(self, state: WizardJobState) -> None:
        """Emit terminal telemetry for one job.

        Two emission layers:

        1. ``voice.calibration.wizard.terminal`` -- the legacy fine-
           grained one-line summary. Operators with v0.30.18+ dashboards
           keyed on this name keep working.
        2. The spec §8.3 dispatch: one of
           ``voice.calibration.wizard.completed`` /
           ``voice.calibration.wizard.cancelled`` /
           ``voice.calibration.wizard.fallback_triggered`` depending
           on the terminal status. Field shapes mirror the spec
           verbatim for cross-deployment OTel filtering.

        Closed-enum cardinality preserved on every dispatch: status,
        fallback_reason, triage_winner_hid, path are all from finite
        sets. error_summary is NOT included to avoid unbounded
        cardinality from arbitrary error text.
        """
        job_id_hash = short_hash(state.job_id)
        mind_id_hash = short_hash(state.mind_id)
        logger.info(
            "voice.calibration.wizard.terminal",
            job_id_hash=job_id_hash,
            mind_id_hash=mind_id_hash,
            status=state.status.value,
            triage_winner_hid=state.triage_winner_hid or "",
            fallback_reason=state.fallback_reason or "",
        )

        # v0.30.24 T3.8: spec §8.3 dispatch.
        path_label = self._path_by_job.pop(state.job_id, "unknown")
        started_mono = self._started_mono_by_job.pop(state.job_id, None)
        duration_s = round(time.monotonic() - started_mono, 3) if started_mono is not None else 0.0

        if state.status is WizardStatus.DONE:
            logger.info(
                "voice.calibration.wizard.completed",
                job_id_hash=job_id_hash,
                mind_id_hash=mind_id_hash,
                path=path_label,
                duration_s=duration_s,
                success=True,
                triage_winner_hid=state.triage_winner_hid or "",
            )
        elif state.status is WizardStatus.CANCELLED:
            logger.info(
                "voice.calibration.wizard.cancelled",
                job_id_hash=job_id_hash,
                mind_id_hash=mind_id_hash,
                path=path_label,
                duration_s=duration_s,
                # Step at which cancel was honoured -- the orchestrator
                # checkpoints + transitions to CANCELLED with the
                # current_stage_message preserving prior status info.
                step=_terminal_step_label(state),
            )
        elif state.status is WizardStatus.FALLBACK:
            logger.info(
                "voice.calibration.wizard.fallback_triggered",
                job_id_hash=job_id_hash,
                mind_id_hash=mind_id_hash,
                path=path_label,
                duration_s=duration_s,
                reason=state.fallback_reason or "unspecified",
            )
        elif state.status is WizardStatus.FAILED:
            # Spec §8.3 doesn't list a "failed" event but the operator
            # contract still benefits from the same shape -- treat
            # FAILED like a `completed{success=False}` for symmetry.
            logger.info(
                "voice.calibration.wizard.completed",
                job_id_hash=job_id_hash,
                mind_id_hash=mind_id_hash,
                path=path_label,
                duration_s=duration_s,
                success=False,
                triage_winner_hid=state.triage_winner_hid or "",
            )

    def _emit_step_entered(self, state: WizardJobState, *, step: str) -> None:
        """Emit ``voice.calibration.wizard.step_entered`` per spec §8.3.

        Closed enum for ``step``: probe | fast_path | slow_path |
        review | fallback. Fired at the FIRST transition into each
        major step (not on intermediate sub-states like
        FAST_PATH_VALIDATE which are part of the same step).
        """
        logger.info(
            "voice.calibration.wizard.step_entered",
            job_id_hash=short_hash(state.job_id),
            mind_id_hash=short_hash(state.mind_id),
            step=step,
        )

    def _emit_path_chosen(self, state: WizardJobState, *, path: str) -> None:
        """Emit ``voice.calibration.wizard.path_chosen`` per spec §8.3.

        Closed enum for ``path``: fast | slow | fallback. Fired
        exactly once per job, immediately after the orchestrator
        decides which branch to take (after fingerprint + KB lookup).
        """
        self._path_by_job[state.job_id] = path
        logger.info(
            "voice.calibration.wizard.path_chosen",
            job_id_hash=short_hash(state.job_id),
            mind_id_hash=short_hash(state.mind_id),
            path=path,
        )

    def _emit_state(self, state: WizardJobState, tracker: WizardProgressTracker) -> None:
        """Persist the snapshot AND emit a stage-transition telemetry event.

        One call site per state mutation -- callers replace
        ``tracker.append(state)`` with ``self._emit_state(state, tracker)``
        so JSONL persistence + structured telemetry stay synchronized.
        Closed-enum cardinality: status is from the 12-value
        WizardStatus enum; progress is bucketed for OTel histograms;
        no per-event message string (operator-facing strings live in
        the JSONL only).
        """
        tracker.append(state)
        logger.info(
            "voice.calibration.wizard.stage_transition",
            job_id_hash=short_hash(state.job_id),
            mind_id_hash=short_hash(state.mind_id),
            status=state.status.value,
            progress=state.progress,
        )

    async def _tail_prompts_file(
        self,
        *,
        prompts_file: Path,
        state_holder: dict[str, WizardJobState],
        tracker: WizardProgressTracker,
    ) -> None:
        """Tail the bash side-channel prompts file every 500 ms.

        Bash writes one JSON line per operator-facing prompt
        (``prompt_emit_structured`` in ``common.sh``). The orchestrator
        reads new lines on each poll, parses them, and updates
        ``state.extras["current_prompt"]`` so the dashboard's
        ``<CapturePrompt>`` component renders the active prompt in
        real time. One ``voice.calibration.wizard.capture_prompt``
        telemetry event fires per parsed prompt.

        Failure-mode contract:

        * File never materializes (CLI-only operator who didn't set the
          env var, or bash diag failed before writing) → loop polls
          harmlessly until cancelled.
        * Malformed JSON line → skipped + DEBUG log; subsequent lines
          processed.
        * OSError on read (transient) → suppressed; retried on next poll.
        * The task is spawned by the slow-path-diag stage and cancelled
          in its ``finally:`` block. CancelledError is the expected
          terminator and exits cleanly.
        """
        sent_offset = 0
        while True:
            try:
                if prompts_file.exists():
                    raw = prompts_file.read_text(encoding="utf-8", errors="replace")
                    lines = raw.splitlines()
                    for line in lines[sent_offset:]:
                        stripped = line.strip()
                        if not stripped:
                            continue
                        try:
                            prompt_data: dict[str, Any] = json.loads(stripped)
                        except json.JSONDecodeError:
                            logger.debug(
                                "voice.calibration.wizard.capture_prompt_malformed",
                                preview=stripped[:120],
                            )
                            continue
                        current = state_holder["state"]
                        # Build a fresh state with current_prompt in extras
                        # so subscribers see the per-prompt update.
                        new_extras = dict(current.extras)
                        new_extras["current_prompt"] = prompt_data
                        new_state = WizardJobState(
                            job_id=current.job_id,
                            mind_id=current.mind_id,
                            status=current.status,
                            progress=current.progress,
                            current_stage_message=current.current_stage_message,
                            created_at_utc=current.created_at_utc,
                            updated_at_utc=self._now(),
                            profile_path=current.profile_path,
                            triage_winner_hid=current.triage_winner_hid,
                            error_summary=current.error_summary,
                            fallback_reason=current.fallback_reason,
                            extras=new_extras,
                        )
                        state_holder["state"] = new_state
                        self._emit_state(new_state, tracker)
                        prompt_type = prompt_data.get("type", "")
                        # Closed-enum guard: only emit telemetry for the
                        # documented prompt types so OTel cardinality
                        # stays bounded.
                        if prompt_type in ("speak", "silence"):
                            logger.info(
                                "voice.calibration.wizard.capture_prompt",
                                job_id_hash=short_hash(current.job_id),
                                mind_id_hash=short_hash(current.mind_id),
                                prompt_type=prompt_type,
                                phrase=str(prompt_data.get("phrase") or ""),
                            )
                    sent_offset = len(lines)
            except OSError as exc:
                logger.debug(
                    "voice.calibration.wizard.capture_prompt_read_failed",
                    reason=str(exc),
                )
            await asyncio.sleep(_PROMPTS_POLL_INTERVAL_S)

    # ====================================================================
    # Internals
    # ====================================================================

    async def _run_inner(
        self,
        *,
        job_id: str,
        state: WizardJobState,
        tracker: WizardProgressTracker,
    ) -> WizardJobState:
        if self._is_cancelled(job_id):
            return self._emit_cancelled(state, tracker)

        # Stage 1: PROBING -- capture fingerprint.
        state = self._transition(
            state,
            status=WizardStatus.PROBING,
            progress=_PROGRESS_PROBING,
            message=_PROBING_MSG,
        )
        self._emit_state(state, tracker)
        self._emit_step_entered(state, step="probe")
        fingerprint = await asyncio.to_thread(capture_fingerprint)

        if self._is_cancelled(job_id):
            return self._emit_cancelled(state, tracker)

        # Stage 2 (fast path): KB lookup. If the local cache has a
        # profile for this fingerprint, replay it (~5s) instead of
        # running the full 8-12 min slow path. Cache miss falls
        # through to SLOW_PATH below.
        cached_profile = await asyncio.to_thread(
            lookup_profile,
            data_dir=self._data_dir,
            fingerprint_hash=fingerprint.fingerprint_hash,
        )
        if cached_profile is not None:
            self._emit_path_chosen(state, path="fast")
            return await self._run_fast_path(
                job_id=job_id,
                state=state,
                tracker=tracker,
                cached=cached_profile,
                mind_id=state.mind_id,
            )

        # Stage 2: SLOW_PATH_DIAG -- run full diag (--non-interactive).
        self._emit_path_chosen(state, path="slow")
        state = self._transition(
            state,
            status=WizardStatus.SLOW_PATH_DIAG,
            progress=_PROGRESS_SLOW_PATH_DIAG,
            message=_SLOW_PATH_DIAG_MSG,
        )
        self._emit_state(state, tracker)
        self._emit_step_entered(state, step="slow_path")
        # v0.30.31 (P3) capture-prompt protocol: bash writes structured
        # prompts to <job_dir>/prompts.jsonl (only when SOVYX_DIAG_PROMPTS_FILE
        # env is set, so CLI operators are unaffected); the orchestrator
        # tails the file every 500 ms and pushes each line into
        # state.extras["current_prompt"] so the dashboard renders the
        # "say X" / "stay silent for Y" cards in real time.
        prompts_file = self.job_dir(job_id) / _PROMPTS_FILE
        prompts_file.parent.mkdir(parents=True, exist_ok=True)
        # Drop any stale prompts.jsonl from a prior run on the same
        # job_id so the tail starts at a clean offset.
        with contextlib.suppress(OSError):
            prompts_file.unlink(missing_ok=True)
        # Holder lets the tail loop see the current state without
        # passing it explicitly (the orchestrator mutates state inside
        # the loop's lifetime as it builds new transitions).
        prompt_state_holder: dict[str, WizardJobState] = {"state": state}
        tail_task = asyncio.create_task(
            self._tail_prompts_file(
                prompts_file=prompts_file,
                state_holder=prompt_state_holder,
                tracker=tracker,
            )
        )
        try:
            # v0.30.26: pass `trigger="wizard"` per spec §8.3 so the
            # voice.diagnostics.full_diag_started telemetry attributes
            # the call to the dashboard wizard rather than the CLI.
            # v0.30.30 (P2): use the async-native runner so
            # operator-initiated CancelledError propagates into the
            # bash subprocess via SIGTERM → grace → SIGKILL escalation.
            # v0.30.31 (P3): pass SOVYX_DIAG_PROMPTS_FILE so bash emits
            # structured prompts the tail loop forwards to the WS.
            diag_result = await run_full_diag_async(
                extra_args=("--non-interactive",),
                trigger="wizard",
                env_overrides={"SOVYX_DIAG_PROMPTS_FILE": str(prompts_file)},
            )
        except DiagPrerequisiteError as exc:
            return self._emit_fallback(
                state,
                tracker,
                reason="diag_prerequisite_unmet",
                summary=str(exc),
            )
        except DiagRunError as exc:
            return self._emit_fallback(
                state,
                tracker,
                reason="diag_run_failed",
                summary=str(exc),
            )
        finally:
            tail_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await tail_task

        # Pick up any state mutations the tail loop made (extras updates).
        state = prompt_state_holder["state"]

        if self._is_cancelled(job_id):
            return self._emit_cancelled(state, tracker)

        # Stage 3: SLOW_PATH_CALIBRATE -- triage + measurements + engine.
        # Drop the now-stale current_prompt from extras as we leave the
        # SLOW_PATH_DIAG stage; the dashboard reads its absence as
        # "prompt cleared" and the CapturePrompt component unmounts.
        cleared_extras = dict(state.extras)
        cleared_extras.pop("current_prompt", None)
        state = WizardJobState(
            job_id=state.job_id,
            mind_id=state.mind_id,
            status=state.status,
            progress=state.progress,
            current_stage_message=state.current_stage_message,
            created_at_utc=state.created_at_utc,
            updated_at_utc=state.updated_at_utc,
            profile_path=state.profile_path,
            triage_winner_hid=state.triage_winner_hid,
            error_summary=state.error_summary,
            fallback_reason=state.fallback_reason,
            extras=cleared_extras,
        )
        state = self._transition(
            state,
            status=WizardStatus.SLOW_PATH_CALIBRATE,
            progress=_PROGRESS_SLOW_PATH_CALIBRATE,
            message=_SLOW_PATH_CALIBRATE_MSG,
        )
        self._emit_state(state, tracker)
        try:
            triage = await asyncio.to_thread(triage_tarball, diag_result.tarball_path)
        except (FileNotFoundError, ValueError) as exc:
            return self._emit_fallback(
                state,
                tracker,
                reason="triage_failed",
                summary=str(exc),
            )

        measurements = capture_measurements(
            diag_tarball_root=triage.tarball_root,
            triage_result=triage,
            duration_s=diag_result.duration_s,
        )

        engine = CalibrationEngine()
        profile = engine.evaluate(
            mind_id=state.mind_id,
            fingerprint=fingerprint,
            measurements=measurements,
            triage_result=triage,
        )

        triage_winner_hid = triage.winner.hid.value if triage.winner is not None else None

        if self._is_cancelled(job_id):
            return self._emit_cancelled(state, tracker)

        # Stage 4: SLOW_PATH_APPLY -- persist profile + render advice.
        state = self._transition(
            state,
            status=WizardStatus.SLOW_PATH_APPLY,
            progress=_PROGRESS_SLOW_PATH_APPLY,
            message=_SLOW_PATH_APPLY_MSG,
            triage_winner_hid=triage_winner_hid,
        )
        self._emit_state(state, tracker)
        applier = CalibrationApplier(
            data_dir=self._data_dir,
            mind_yaml_path=self._data_dir / state.mind_id / "mind.yaml",
        )
        try:
            apply_result = await applier.apply(profile, dry_run=False)
        except ApplyError as exc:
            return self._emit_failed(
                state,
                tracker,
                summary=str(exc),
            )

        # Stage 5: DONE -- and store the profile in the local KB
        # cache so the next run on the same hardware takes the fast
        # path (~5s instead of ~10 min). Cache failures are logged
        # but do NOT fail the run -- the operator already has a
        # successful calibration; cache miss next time is harmless.
        with contextlib.suppress(Exception):
            await asyncio.to_thread(store_profile, profile, data_dir=self._data_dir)
        done = self._transition(
            state,
            status=WizardStatus.DONE,
            progress=_PROGRESS_DONE,
            message=_DONE_MSG,
            profile_path=str(apply_result.profile_path),
            triage_winner_hid=triage_winner_hid,
        )
        self._emit_state(done, tracker)
        # v0.30.26 spec §8.3: emit step_entered{step=review} on DONE
        # because the operator's UX surface flips to ProfileReview
        # the moment the orchestrator reports DONE. Mirrors the
        # frontend's _ProfileReview component activation.
        self._emit_step_entered(done, step="review")
        return done

    async def _run_fast_path(
        self,
        *,
        job_id: str,
        state: WizardJobState,
        tracker: WizardProgressTracker,
        cached: CalibrationProfile,
        mind_id: str,
    ) -> WizardJobState:
        """FAST_PATH branch: replay a cached profile (~5s).

        Bypasses the 8-12 min full diag; the cached CalibrationProfile
        was produced by a prior successful slow-path run on the same
        hardware (matched by fingerprint_hash). The applier persists
        the profile under the CURRENT mind_id (not the cached one),
        so per-mind isolation is preserved -- one host, multiple minds
        share the same calibration but persist independently.

        v0.30.18 alpha: validation capture (5s mic recording to
        confirm the cached profile still works) is intentionally
        skipped. v0.30.19+ adds it; for now we trust the fingerprint
        match.
        """
        if self._is_cancelled(job_id):
            return self._emit_cancelled(state, tracker)

        state = self._transition(
            state,
            status=WizardStatus.FAST_PATH_LOOKUP,
            progress=_PROGRESS_FAST_PATH_LOOKUP,
            message=_FAST_PATH_LOOKUP_MSG,
        )
        self._emit_state(state, tracker)
        self._emit_step_entered(state, step="fast_path")

        if self._is_cancelled(job_id):
            return self._emit_cancelled(state, tracker)

        # Re-issue the cached profile under the current mind_id.
        # Reuses every field from the cached profile so the rule
        # trace + decisions + provenance survive replay; only mind_id
        # is rewritten because the cache key is hardware-keyed, not
        # mind-keyed.
        replayed = CalibrationProfile(
            schema_version=cached.schema_version,
            profile_id=cached.profile_id,
            mind_id=mind_id,
            fingerprint=cached.fingerprint,
            measurements=cached.measurements,
            decisions=cached.decisions,
            provenance=cached.provenance,
            generated_by_engine_version=cached.generated_by_engine_version,
            generated_by_rule_set_version=cached.generated_by_rule_set_version,
            generated_at_utc=cached.generated_at_utc,
            signature=cached.signature,
        )

        state = self._transition(
            state,
            status=WizardStatus.FAST_PATH_APPLY,
            progress=_PROGRESS_FAST_PATH_APPLY,
            message=_FAST_PATH_APPLY_MSG,
        )
        self._emit_state(state, tracker)

        applier = CalibrationApplier(
            data_dir=self._data_dir,
            mind_yaml_path=self._data_dir / mind_id / "mind.yaml",
        )
        try:
            apply_result = await applier.apply(replayed, dry_run=False)
        except ApplyError as exc:
            return self._emit_failed(state, tracker, summary=str(exc))

        triage_winner_hid = (
            replayed.measurements.triage_winner_hid
            if replayed.measurements.triage_winner_hid is not None
            else None
        )
        done = self._transition(
            state,
            status=WizardStatus.DONE,
            progress=_PROGRESS_DONE,
            message=_DONE_MSG,
            profile_path=str(apply_result.profile_path),
            triage_winner_hid=triage_winner_hid,
        )
        self._emit_state(done, tracker)
        # v0.30.26 spec §8.3: emit step_entered{step=review} on DONE
        # because the operator's UX surface flips to ProfileReview
        # the moment the orchestrator reports DONE. Mirrors the
        # frontend's _ProfileReview component activation.
        self._emit_step_entered(done, step="review")
        return done

    def _is_cancelled(self, job_id: str) -> bool:
        return self.cancel_path(job_id).exists()

    def _emit_cancelled(
        self, state: WizardJobState, tracker: WizardProgressTracker
    ) -> WizardJobState:
        cancelled = self._transition(
            state,
            status=WizardStatus.CANCELLED,
            progress=state.progress,
            message=_CANCELLED_MSG,
        )
        self._emit_state(cancelled, tracker)
        return cancelled

    def _emit_failed(
        self,
        state: WizardJobState,
        tracker: WizardProgressTracker,
        *,
        summary: str,
    ) -> WizardJobState:
        failed = self._transition(
            state,
            status=WizardStatus.FAILED,
            progress=state.progress,
            message=f"Calibration failed: {summary[:200]}",
            error_summary=summary,
        )
        self._emit_state(failed, tracker)
        return failed

    def _emit_fallback(
        self,
        state: WizardJobState,
        tracker: WizardProgressTracker,
        *,
        reason: str,
        summary: str,
    ) -> WizardJobState:
        # v0.30.24 T3.8: spec §8.3 reclassifies the chosen path as
        # "fallback" since the operator's UX surface flips to the
        # legacy wizard. Override any prior path_chosen=fast/slow.
        self._path_by_job[state.job_id] = "fallback"
        fallback = self._transition(
            state,
            status=WizardStatus.FALLBACK,
            progress=state.progress,
            message=f"Falling back to simple setup: {reason}",
            fallback_reason=reason,
            error_summary=summary,
        )
        self._emit_state(fallback, tracker)
        self._emit_step_entered(fallback, step="fallback")
        return fallback

    def _transition(
        self,
        prev: WizardJobState,
        *,
        status: WizardStatus,
        progress: float,
        message: str,
        profile_path: str | None = None,
        triage_winner_hid: str | None = None,
        error_summary: str | None = None,
        fallback_reason: str | None = None,
    ) -> WizardJobState:
        """Build a new frozen WizardJobState reflecting one transition."""
        return WizardJobState(
            job_id=prev.job_id,
            mind_id=prev.mind_id,
            status=status,
            progress=progress,
            current_stage_message=message,
            created_at_utc=prev.created_at_utc,
            updated_at_utc=self._now(),
            profile_path=profile_path if profile_path is not None else prev.profile_path,
            triage_winner_hid=(
                triage_winner_hid if triage_winner_hid is not None else prev.triage_winner_hid
            ),
            error_summary=error_summary if error_summary is not None else prev.error_summary,
            fallback_reason=(
                fallback_reason if fallback_reason is not None else prev.fallback_reason
            ),
            extras=dict(prev.extras),
        )

    @staticmethod
    def _now() -> str:
        return datetime.now(tz=UTC).isoformat(timespec="seconds")
