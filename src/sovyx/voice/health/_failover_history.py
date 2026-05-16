"""Failover ladder history ring buffer — observability surface for C3.

Mission anchor: ``docs-internal/missions/MISSION-c3-failover-ladder-iteration-2026-05-16.md``
§T2.9.

Stores the most recent N ladder runs in a bounded FIFO so the
dashboard's ``/api/voice/failover-history`` endpoint + the
``sovyx doctor voice`` CLI surface can render the operator-actionable
view without parsing log files.

Module-level lazy singleton mirroring
:func:`sovyx.voice.health._quarantine.get_default_quarantine` +
:func:`sovyx.voice.health._probe_result_cache.get_default_probe_result_cache`
patterns: no bootstrap dependency, easy test isolation via
:func:`reset_default_failover_history`. Cardinality bounded by the
ring capacity (default 32, configurable via
:attr:`VoiceTuningConfig.failover_history_ring_capacity`).

Population: :func:`sovyx.voice.health._runtime_failover._try_runtime_failover`
calls :meth:`FailoverHistoryRing.record_ladder` exactly once per
ladder completion (success OR exhausted). Per-candidate detail is
recorded inline via :meth:`FailoverLadderRunRecord.add_candidate`
during the loop body iteration.
"""

from __future__ import annotations

import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Final

from sovyx.observability.logging import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class FailoverCandidateRecord:
    """Per-candidate observability record within a ladder run.

    Attributes:
        index: Zero-based candidate index within the ladder.
        target_endpoint: Canonical endpoint identifier of the
            attempted candidate.
        target_friendly_name: Operator-facing device name.
        verdict: One of ``"succeeded"``, ``"failed"``, ``"skipped"``.
        error_class: :class:`FailoverErrorClass` member value when
            the candidate failed; empty string otherwise.
        error_detail: Free-text detail from the dispatch verdict.
        elapsed_ms: Wall-clock duration of the dispatch attempt.
        skipped_reason: When ``verdict="skipped"``, the reason token
            (e.g. ``"probe_cache_unopenable"``).
    """

    index: int
    target_endpoint: str
    target_friendly_name: str = ""
    verdict: str = ""
    error_class: str = ""
    error_detail: str = ""
    elapsed_ms: int | None = None
    skipped_reason: str | None = None


@dataclass(slots=True)
class FailoverLadderRunRecord:
    """One ladder invocation captured for observability.

    The ring buffer at :class:`FailoverHistoryRing` stores these in
    FIFO order; the dashboard widget renders the most recent N.
    """

    ladder_id: str
    started_monotonic: float
    completed_monotonic: float | None = None
    verdict: str = "in_progress"  # in_progress | succeeded | exhausted
    candidates_tried: int = 0
    succeeded_index: int | None = None
    candidates: list[FailoverCandidateRecord] = field(default_factory=list)
    from_endpoint: str = ""
    elapsed_ms: int | None = None
    derived_reason: str = ""
    mind_id: str = ""

    def add_candidate(self, record: FailoverCandidateRecord) -> None:
        """Append a per-candidate record. Called by the loop body."""
        self.candidates.append(record)


class FailoverHistoryRing:
    """Bounded FIFO of recent ladder runs.

    Default capacity 32 entries (configurable via the
    ``failover_history_ring_capacity`` tuning knob). Thread-safety
    notes mirror :class:`ProbeResultCache`: single-process + GIL-
    protected dict / deque operations; concurrent reads + writes
    coexist safely. The history is observability infrastructure;
    correctness does not depend on every write surviving contention.
    """

    _DEFAULT_CAPACITY: Final[int] = 32

    def __init__(self, *, capacity: int | None = None) -> None:
        # ``capacity=0`` should clamp to 1 (defensive); ``capacity=None``
        # falls back to the default. Disambiguates the ``capacity or default``
        # short-circuit which would otherwise treat ``0`` as falsy.
        resolved = self._DEFAULT_CAPACITY if capacity is None else capacity
        self._capacity = max(1, resolved)
        self._entries: deque[FailoverLadderRunRecord] = deque(maxlen=self._capacity)

    @property
    def capacity(self) -> int:
        return self._capacity

    def record_ladder(self, run: FailoverLadderRunRecord) -> None:
        """Append a completed (or in-flight) ladder run.

        When ``len(self._entries) == capacity``, the oldest entry is
        evicted by the underlying :class:`collections.deque` semantics.
        """
        self._entries.append(run)

    def update_in_progress(
        self,
        ladder_id: str,
        *,
        verdict: str,
        completed_monotonic: float,
        succeeded_index: int | None,
        candidates_tried: int,
        elapsed_ms: int,
    ) -> bool:
        """Finalize an in-flight record by ladder_id.

        Returns ``True`` when a matching record was found + updated,
        ``False`` when no match. Best-effort: if the record was
        evicted (the ring filled up between ladder_started and
        ladder_complete), the update is silently dropped.
        """
        for entry in reversed(self._entries):
            if entry.ladder_id == ladder_id:
                entry.verdict = verdict
                entry.completed_monotonic = completed_monotonic
                entry.succeeded_index = succeeded_index
                entry.candidates_tried = candidates_tried
                entry.elapsed_ms = elapsed_ms
                return True
        return False

    def entries(self) -> list[FailoverLadderRunRecord]:
        """Snapshot of all recorded ladder runs, newest first."""
        return list(reversed(self._entries))

    def __len__(self) -> int:
        return len(self._entries)


# Module-level lazy singleton.
_SINGLETON: FailoverHistoryRing | None = None


def get_default_failover_history(
    *,
    capacity: int | None = None,
) -> FailoverHistoryRing:
    """Return (and lazily construct) the process-wide history ring.

    Args:
        capacity: First-call-wins capacity override. Subsequent calls
            ignore this argument (matches
            :func:`sovyx.voice.health._quarantine.get_default_quarantine`
            semantics). Resolves the default from
            :attr:`VoiceTuningConfig.failover_history_ring_capacity`
            when the first call passes ``None``.

    Tests use :func:`reset_default_failover_history` between cases
    for isolation.
    """
    global _SINGLETON  # noqa: PLW0603 — lazy singleton, not user-mutable state
    if _SINGLETON is not None:
        if capacity is not None and capacity != _SINGLETON.capacity:
            logger.warning(
                "voice.failover_history.reinit_ignored",
                requested_capacity=capacity,
                active_capacity=_SINGLETON.capacity,
            )
        return _SINGLETON
    if capacity is None:
        try:
            from sovyx.engine.config import VoiceTuningConfig

            capacity = VoiceTuningConfig().failover_history_ring_capacity
        except Exception:  # noqa: BLE001
            capacity = FailoverHistoryRing._DEFAULT_CAPACITY  # noqa: SLF001
    _SINGLETON = FailoverHistoryRing(capacity=capacity)
    return _SINGLETON


def reset_default_failover_history() -> None:
    """Drop the singleton — tests use this between cases for isolation."""
    global _SINGLETON  # noqa: PLW0603
    _SINGLETON = None


def make_ladder_id() -> str:
    """Return a fresh ``uuid4().hex[:12]`` ladder identifier.

    Single canonical source so the runtime failover loop + the
    history ring share the same length / case convention.
    """
    return uuid.uuid4().hex[:12]


def now_monotonic() -> float:
    """Thin alias — keeps test patching the wall-clock source easy."""
    return time.monotonic()


__all__ = [
    "FailoverCandidateRecord",
    "FailoverHistoryRing",
    "FailoverLadderRunRecord",
    "get_default_failover_history",
    "make_ladder_id",
    "now_monotonic",
    "reset_default_failover_history",
]
