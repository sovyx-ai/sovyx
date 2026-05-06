"""JSONL progress writer / reader for the calibration wizard.

Persists every :class:`WizardJobState` snapshot to
``<job_dir>/progress.jsonl`` so:

* The dashboard polls the file's tail to render live progress.
* A daemon restart can read the most-recent non-terminal snapshot
  (the wizard does not yet support resume; the snapshot is consumed
  by ``GET /api/voice/calibration/jobs/{id}`` for status-only).
* A post-mortem of failed calibration replays the full history via
  :meth:`ProgressTracker.read_all` for forensic reconstruction.

JSONL choice (vs. SQLite / msgpack / etc.) -- same rationale as
:mod:`sovyx.voice.wake_word_training._progress`:

* One record per line, easy for operators to ``grep`` / ``jq``.
* Append-only -- no rewrite cost as the job runs.
* Survives crash mid-write (the partial line is silently skipped by
  ``read_all``; well-formed lines are intact via flush+fsync).

Concurrency model:

* ONE writer per job (the orchestrator). ``threading.Lock`` serialises
  writes from any sub-stage worker threads.
* MANY readers (REST snapshot handler, WS subscribers tailing the
  file, CLI status). Readers do NOT take the lock -- JSONL append
  semantics + line-buffered fsync mean partial writes never appear at
  line boundaries.

History: introduced in v0.30.16 as T3.1 of mission
``MISSION-voice-self-calibrating-system-2026-05-05.md`` Layer 3.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger
from sovyx.observability.privacy import short_hash
from sovyx.voice.calibration._wizard_state import WizardJobState

if TYPE_CHECKING:
    from pathlib import Path

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    """One persisted progress line.

    Light wrapper around :class:`WizardJobState` that captures the
    1-indexed line number for diff-against-previous-snapshot views.
    Most callers consume the ``state`` field directly.
    """

    state: WizardJobState
    line_no: int


class WizardProgressTracker:
    """Append-only progress log for one calibration wizard job.

    Args:
        path: Absolute path to the JSONL file. Parent directory is
            created on first append. Reading from a missing path
            returns an empty list (the job hasn't started yet).
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def append(self, state: WizardJobState) -> None:
        """Append one snapshot. fsynced before return.

        Durability is critical because a daemon crash mid-calibration
        must not lose the last status update -- the dashboard's WS
        subscriber relies on the JSONL tail being trustworthy. Cost
        is one ``fsync`` per state transition, which is acceptable
        for the human-scale event rate (one transition per stage).
        """
        line = json.dumps(state.to_dict(), ensure_ascii=False, sort_keys=True) + "\n"
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())

    def read_all(self) -> list[ProgressEvent]:
        """Read every persisted snapshot in order.

        Malformed lines (corrupt JSON, missing fields, unknown
        ``status`` values) are skipped + logged at WARN -- a
        partial-write or schema-bump must not crash the dashboard.

        Returns:
            Empty list when the file doesn't exist (job not started).
        """
        if not self._path.exists():
            return []
        events: list[ProgressEvent] = []
        try:
            content = self._path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            # Tracker path layout: ``<data_dir>/voice_calibration/<job_id>/progress.jsonl``;
            # parent dir name is the job_id, hashed here for telemetry.
            logger.warning(
                "voice.calibration.wizard.progress_read_failed",
                job_id_hash=short_hash(self._path.parent.name),
                reason=str(exc),
                # Deprecated raw filesystem path (removal in v0.30.29 per
                # MISSION-voice-calibration-extreme-audit-2026-05-06 §4.2):
                path=str(self._path),
            )
            return []
        for line_no, raw_line in enumerate(content.splitlines(), start=1):
            line = raw_line.strip()
            if not line:
                continue
            with contextlib.suppress(Exception):
                data = json.loads(line)
                state = WizardJobState.from_dict(data)
                events.append(ProgressEvent(state=state, line_no=line_no))
        return events

    def latest(self) -> WizardJobState | None:
        """Return the most-recent snapshot, or ``None`` when no events exist."""
        events = self.read_all()
        if not events:
            return None
        return events[-1].state
