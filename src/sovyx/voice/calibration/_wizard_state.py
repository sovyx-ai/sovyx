"""State types for the calibration wizard backend.

Pure-typing module: no I/O, no side effects. The orchestrator
(``_wizard_orchestrator.py``) uses these types as the canonical
state-machine surface.

State transitions (enforced by the orchestrator, NOT this module)::

    PENDING ─→ PROBING ─→ SLOW_PATH_DIAG ─→ SLOW_PATH_CALIBRATE ─→ SLOW_PATH_APPLY ─→ DONE
                                                                                  ├→ FAILED
                                                                                  ├→ CANCELLED
                                                                                  └→ FALLBACK

Terminal states (DONE / FAILED / CANCELLED / FALLBACK) accept no
further transitions.

History: introduced in v0.30.16 as T3.1 of mission
``MISSION-voice-self-calibrating-system-2026-05-05.md`` Layer 3.

FAST_PATH branches (``FAST_PATH_LOOKUP``, ``FAST_PATH_APPLY``) are
live: the orchestrator queries the local KB cache via
:func:`sovyx.voice.calibration._kb_cache.lookup_profile` after
PROBING, and on a cache hit replays the matched profile in ~5s
instead of running the slow path.

``FAST_PATH_VALIDATE`` is a **reserved enum member with no live
transition**: the orchestrator goes from ``FAST_PATH_APPLY`` directly
to ``DONE`` for cached profiles. The original plan was a 5s
mic-capture validation diag between APPLY and DONE, but the
validation gate would be an invasive addition (new bash ``--only``
invocation, new state-machine branch, new operator UX). The enum
member is kept in the closed set so a future profile that records
``FAST_PATH_VALIDATE`` doesn't break downstream auditors that
index on the WizardStatus closed enum. Future minor cycles MAY
wire a real transition.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class WizardStatus(StrEnum):
    """Lifecycle phases of a calibration wizard job.

    Stable wire format -- emitted to the JSONL progress log + dashboard
    websocket. Renaming a member is a breaking schema change for
    downstream auditors. Adding members is OK; the orchestrator uses
    a closed-set match so unknown values fail loudly.
    """

    PENDING = "pending"
    """Job created but no work has started yet."""

    PROBING = "probing"
    """Capturing hardware fingerprint via local probes (~1s)."""

    FAST_PATH_LOOKUP = "fast_path_lookup"
    """Looking up fingerprint hash in local KB cache."""

    FAST_PATH_APPLY = "fast_path_apply"
    """Applying KB-matched profile (~5s)."""

    FAST_PATH_VALIDATE = "fast_path_validate"
    """Reserved enum (v0.30.34): the orchestrator does NOT transition
    through this state in v0.30.x. Kept in the closed set so
    downstream auditors that index on the enum don't break if a
    future minor wires the validation step. See module docstring."""

    SLOW_PATH_DIAG = "slow_path_diag"
    """Running the full forensic bash diag toolkit (8-12 min)."""

    SLOW_PATH_CALIBRATE = "slow_path_calibrate"
    """Capturing measurements + evaluating the rule engine (~5s)."""

    SLOW_PATH_APPLY = "slow_path_apply"
    """Applying the produced CalibrationProfile + persisting (~1s)."""

    DONE = "done"
    """Calibration succeeded; ``profile_path`` points to the
    persisted ``calibration.json``."""

    FAILED = "failed"
    """Pipeline raised a non-cancellation exception. Inspect
    ``error_summary`` for the reason."""

    CANCELLED = "cancelled"
    """Operator cancelled via filesystem signal (presence of
    ``<job_dir>/.cancel`` file) or dashboard cancel call."""

    FALLBACK = "fallback"
    """Pipeline opted out (e.g. selftest aborted, hardware gap, no
    capture device detected) and the operator should fall back to the
    simple device-test wizard. The frontend renders this as a banner
    via ``_FallbackBanner.tsx`` explaining + offering the legacy path."""

    @property
    def is_terminal(self) -> bool:
        """``True`` when the state accepts no further transitions."""
        return self in (
            WizardStatus.DONE,
            WizardStatus.FAILED,
            WizardStatus.CANCELLED,
            WizardStatus.FALLBACK,
        )


@dataclass(frozen=True, slots=True)
class WizardJobState:
    """Snapshot of a calibration wizard job at one point in time.

    Frozen so concurrent observers (dashboard polling, WS subscribers,
    daemon's resume path) all see consistent data without locking.
    The orchestrator emits a new instance via the progress tracker on
    every state transition.

    Attributes:
        job_id: Stable identifier; mirrors the ``mind_id`` for
            single-mind operators or includes a UUID4 suffix for
            multi-mind concurrent calibration.
        mind_id: The mind whose calibration this job runs.
        status: Current lifecycle phase.
        progress: Fraction in [0.0, 1.0] for the dashboard progress
            bar. Coarsely mapped per stage (PROBING~0.05;
            SLOW_PATH_DIAG~0.10..0.85; CALIBRATE~0.92; APPLY~0.98;
            DONE=1.0). Frontend renders accordingly.
        current_stage_message: One-line operator-facing description
            of what the orchestrator is currently doing (e.g.
            ``"Running forensic diagnostic (8-12 min)"``). i18n at
            the frontend; this string is the english fallback.
        created_at_utc / updated_at_utc: ISO-8601 UTC timestamps.
        profile_path: Populated on DONE -- absolute path to the
            persisted CalibrationProfile JSON.
        triage_winner_hid: Populated post-TRIAGE -- the H1..H10
            short id when triage produced a high-confidence verdict.
            Used by the frontend to surface the verdict alongside
            the apply progress.
        error_summary: Populated on FAILED -- short operator-facing
            error message. Full traceback is in the orchestrator log.
        fallback_reason: Populated on FALLBACK -- reason the pipeline
            opted out (e.g. ``"diag_selftest_aborted"``).
    """

    job_id: str
    mind_id: str
    status: WizardStatus
    progress: float
    current_stage_message: str
    created_at_utc: str
    updated_at_utc: str
    profile_path: str | None = None
    triage_winner_hid: str | None = None
    error_summary: str | None = None
    fallback_reason: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-safe dict for the JSONL progress log."""
        return {
            "job_id": self.job_id,
            "mind_id": self.mind_id,
            "status": self.status.value,
            "progress": self.progress,
            "current_stage_message": self.current_stage_message,
            "created_at_utc": self.created_at_utc,
            "updated_at_utc": self.updated_at_utc,
            "profile_path": self.profile_path,
            "triage_winner_hid": self.triage_winner_hid,
            "error_summary": self.error_summary,
            "fallback_reason": self.fallback_reason,
            "extras": dict(self.extras),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> WizardJobState:
        """Deserialize from a JSONL progress-log entry.

        Raises:
            KeyError: a required field is missing.
            ValueError: ``status`` value is not a known WizardStatus.
        """
        return cls(
            job_id=d["job_id"],
            mind_id=d["mind_id"],
            status=WizardStatus(d["status"]),
            progress=float(d["progress"]),
            current_stage_message=d["current_stage_message"],
            created_at_utc=d["created_at_utc"],
            updated_at_utc=d["updated_at_utc"],
            profile_path=d.get("profile_path"),
            triage_winner_hid=d.get("triage_winner_hid"),
            error_summary=d.get("error_summary"),
            fallback_reason=d.get("fallback_reason"),
            extras=dict(d.get("extras") or {}),
        )
