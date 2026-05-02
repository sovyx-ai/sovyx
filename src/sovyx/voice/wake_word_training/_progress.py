"""JSONL progress writer / reader — Phase 8 / T8.13 + T8.14.

Persists every ``TrainingJobState`` snapshot to a JSONL file at
``<job_dir>/progress.jsonl`` so:

* The dashboard polls the file's tail to render live progress.
* A daemon restart can resume by reading the most-recent
  non-terminal snapshot and replaying from there (T8.14
  resume-from-checkpoint contract).
* A post-mortem of failed training can replay the full history
  via ``ProgressTracker.read_all`` for forensic reconstruction.

JSONL choice (vs. SQLite / msgpack / etc.):
* One record per line, easy for operators to ``grep`` / ``jq``.
* Append-only — no rewrite cost as the job runs.
* Survives crash mid-write (the partial line is silently skipped
  by ``read_all``; well-formed lines are intact via flush+fsync).

Concurrency model:
* ONE writer per job (the orchestrator). The tracker takes a
  threading.Lock to serialise writes from synthesis + training
  threads.
* MANY readers (dashboard polls, CLI status). Readers do NOT take
  the lock — JSONL append semantics + line-buffered fsync mean
  partial writes never appear at line boundaries.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path

from sovyx.observability.logging import get_logger
from sovyx.voice.wake_word_training._state import (
    TrainingJobState,
    TrainingStatus,
)

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    """One persisted progress line.

    Light wrapper around :class:`TrainingJobState` that captures the
    exact line bytes for forensic replay. Most callers consume the
    ``state`` field directly.
    """

    state: TrainingJobState
    """The persisted job snapshot."""

    line_no: int
    """1-indexed line number in the JSONL file. Useful for
    diff-against-previous-snapshot views in dashboards."""


class ProgressTracker:
    """Append-only progress log for one wake-word training job.

    Args:
        path: Absolute path to the JSONL file. Parent directory is
            created on first append. Reading from a missing path
            returns an empty list (the job hasn't started yet).

    Thread safety:
        Internal :class:`threading.Lock` serialises writes. Readers
        bypass the lock — JSONL line-buffered append guarantees
        atomicity at line boundaries on all POSIX-like filesystems
        and on Windows when writes are line-sized (≤ PIPE_BUF /
        page size, which our snapshots always are).
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        """Absolute path to the JSONL progress file."""
        return self._path

    def append(self, state: TrainingJobState) -> None:
        """Append one snapshot. fsynced before return.

        Durability is critical because a daemon crash mid-training
        must not lose the last status update — the resume path
        relies on the JSONL tail being trustworthy. Cost is one
        ``fsync`` per state transition, which is acceptable for
        the human-scale event rate (synthesis: ~1 update/sec;
        training: ~1 update per epoch).
        """
        line = json.dumps(state.to_dict(), ensure_ascii=False, sort_keys=True) + "\n"
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, "a", encoding="utf-8") as fh:  # noqa: PTH123, FURB101
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())

    def read_all(self) -> list[ProgressEvent]:
        """Read every persisted snapshot in order.

        Malformed lines (corrupt JSON, missing fields, unknown
        ``status`` values) are skipped + logged at WARN — a
        partial-write or schema-bump must not crash the dashboard
        replay.

        Returns:
            Empty list when the file doesn't exist (job not started).
        """
        if not self._path.exists():
            return []
        events: list[ProgressEvent] = []
        try:
            with open(self._path, encoding="utf-8") as fh:  # noqa: PTH123
                for idx, raw in enumerate(fh, start=1):
                    line = raw.strip()
                    if not line:
                        continue
                    state = self._parse_line(line, idx)
                    if state is not None:
                        events.append(ProgressEvent(state=state, line_no=idx))
        except OSError as exc:
            logger.warning(
                "voice.training.progress.read_failed",
                path=str(self._path),
                error=str(exc),
            )
            return []
        return events

    def latest(self) -> TrainingJobState | None:
        """Return the most-recent snapshot, or ``None`` when no events.

        Cheap helper for the dashboard's status poll + the resume
        path's "where did we leave off" check. Reads the whole file
        — JSONL files for a single training job are bounded
        (≤ ~5 MB even for verbose progress).
        """
        events = self.read_all()
        if not events:
            return None
        return events[-1].state

    def _parse_line(self, line: str, line_no: int) -> TrainingJobState | None:
        """Parse one JSONL line into a state. Returns ``None`` on
        malformed input + logs the prefix for forensics."""
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            logger.warning(
                "voice.training.progress.corrupt_line",
                path=str(self._path),
                line_no=line_no,
                prefix=line[:80],
            )
            return None
        if not isinstance(data, dict):
            return None
        try:
            status_str = str(data["status"])
            status = TrainingStatus(status_str)
        except (KeyError, ValueError):
            logger.warning(
                "voice.training.progress.unknown_status",
                path=str(self._path),
                line_no=line_no,
                status_value=data.get("status"),
            )
            return None
        try:
            return TrainingJobState(
                wake_word=str(data.get("wake_word", "")),
                mind_id=str(data.get("mind_id", "")),
                language=str(data.get("language", "")),
                status=status,
                progress=float(data.get("progress", 0.0)),
                message=str(data.get("message", "")),
                started_at=str(data.get("started_at", "")),
                updated_at=str(data.get("updated_at", "")),
                completed_at=str(data.get("completed_at", "")),
                output_path=str(data.get("output_path", "")),
                error_summary=str(data.get("error_summary", "")),
                samples_generated=int(data.get("samples_generated", 0)),
                target_samples=int(data.get("target_samples", 0)),
            )
        except (TypeError, ValueError):
            logger.warning(
                "voice.training.progress.malformed_state",
                path=str(self._path),
                line_no=line_no,
                prefix=line[:80],
            )
            return None


__all__ = [
    "ProgressEvent",
    "ProgressTracker",
]
