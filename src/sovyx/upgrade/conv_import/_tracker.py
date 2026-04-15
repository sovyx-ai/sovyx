"""In-memory progress tracker for ongoing conversation imports.

Process-local only — if the daemon restarts mid-import the client
must re-submit. v1 tradeoff: persistent job state would need a
dedicated table and resumable background workers, which is out of
scope for the first cut.

A single ``ImportProgressTracker`` instance lives on
``app.state.import_tracker`` (wired in ``server.create_app``) and is
shared between the POST endpoint that starts a job and the GET
endpoint that polls its progress.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4


class ImportState(StrEnum):
    """Lifecycle state of a conversation-import job.

    Progression is linear: ``PENDING`` → ``PARSING`` → ``PROCESSING``
    → ``COMPLETED`` (or → ``FAILED`` from any pre-completed state).
    ``PENDING`` is the brief window between ``start()`` and the
    background task picking up the job.
    """

    PENDING = "pending"
    PARSING = "parsing"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(slots=True)
class ImportJobStatus:
    """Observable state of a single import job.

    Fields update monotonically forward — ``conversations_processed``
    only grows, ``warnings`` only appends. The GET progress endpoint
    reads a snapshot copy so the client never sees a half-updated row.
    """

    job_id: str
    platform: str
    state: ImportState
    conversations_total: int = 0
    conversations_processed: int = 0
    conversations_skipped: int = 0
    episodes_created: int = 0
    concepts_learned: int = 0
    warnings: list[str] = field(default_factory=list)
    error: str | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None

    def elapsed_ms(self) -> int:
        """Milliseconds since the job started (or until it finished)."""
        end = self.finished_at or datetime.now(UTC)
        return int((end - self.started_at).total_seconds() * 1000)


class ImportProgressTracker:
    """Async-safe registry of active + recent import jobs.

    All mutations go through a single ``asyncio.Lock`` so concurrent
    ``update`` calls from the background worker and ``get`` calls from
    the polling endpoint don't race. Snapshots returned to callers are
    independent copies, safe to mutate.

    Jobs are kept forever in v1 — memory is bounded by human import
    volume (kilobytes per job). If that ever becomes a concern, add
    LRU eviction keyed by ``finished_at``.
    """

    def __init__(self) -> None:
        self._jobs: dict[str, ImportJobStatus] = {}
        self._lock = asyncio.Lock()

    async def start(self, platform: str) -> str:
        """Register a new job and return its ID.

        The returned ID is the one the client polls progress against.
        """
        job_id = uuid4().hex
        async with self._lock:
            self._jobs[job_id] = ImportJobStatus(
                job_id=job_id,
                platform=platform,
                state=ImportState.PENDING,
            )
        return job_id

    async def update(
        self,
        job_id: str,
        *,
        state: ImportState | None = None,
        conversations_total: int | None = None,
        conversations_processed_delta: int = 0,
        conversations_skipped_delta: int = 0,
        episodes_created_delta: int = 0,
        concepts_learned_delta: int = 0,
        warning: str | None = None,
    ) -> None:
        """Apply an atomic delta/set to a job's observable state.

        All deltas are additive so the caller doesn't need to read
        previous values. ``state`` is the only field that's replaced
        outright. Silently no-ops for unknown job_ids — callers don't
        need to special-case race conditions where a job was finalised
        before their update lands.
        """
        async with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            if state is not None:
                job.state = state
            if conversations_total is not None:
                job.conversations_total = conversations_total
            job.conversations_processed += conversations_processed_delta
            job.conversations_skipped += conversations_skipped_delta
            job.episodes_created += episodes_created_delta
            job.concepts_learned += concepts_learned_delta
            if warning is not None:
                job.warnings.append(warning)

    async def finish(
        self,
        job_id: str,
        *,
        error: str | None = None,
    ) -> None:
        """Mark a job as terminated.

        ``error=None`` → ``COMPLETED``; non-None → ``FAILED`` with the
        message attached. ``finished_at`` is set to "now" in both
        cases so the client can stop polling.
        """
        async with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.state = ImportState.FAILED if error else ImportState.COMPLETED
            job.error = error
            job.finished_at = datetime.now(UTC)

    async def get(self, job_id: str) -> ImportJobStatus | None:
        """Return a snapshot of a job's state, or ``None`` if unknown."""
        async with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            # Shallow-copy so the caller can mutate the snapshot
            # without affecting the canonical record.
            return ImportJobStatus(
                job_id=job.job_id,
                platform=job.platform,
                state=job.state,
                conversations_total=job.conversations_total,
                conversations_processed=job.conversations_processed,
                conversations_skipped=job.conversations_skipped,
                episodes_created=job.episodes_created,
                concepts_learned=job.concepts_learned,
                warnings=list(job.warnings),
                error=job.error,
                started_at=job.started_at,
                finished_at=job.finished_at,
            )
