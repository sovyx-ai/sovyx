"""Process-local cross-axis degraded-state ledger.

Mission anchor: ``docs-internal/missions/MISSION-c4-degraded-mode-banner-2026-05-17.md``
§T1.1.

Mirrors the C3 :mod:`sovyx.voice.health._failover_history` module-level
lazy-singleton pattern: no bootstrap dependency, easy test isolation
via :func:`reset_default_degraded_store`. Cardinality bounded by
``_MAX_ENTRIES`` (32) with deterministic-oldest eviction on overflow.

Populated by (representative, non-exhaustive — grep
``get_default_degraded_store`` for the full producer set):

* :mod:`sovyx.engine._llm_dispatch` for the LLM axis (provider
  availability / degraded-mode records; Mission C4 §T1.2 lineage,
  producer moved out of ``bootstrap`` during Mission C6).
* :mod:`sovyx.voice.factory._validate` on
  ``voice.factory.stt_language_unsupported`` (§T1.3).
* :mod:`sovyx.voice.health._runtime_failover` on
  ``voice.failover.ladder_complete{verdict=exhausted}`` (§T1.4) and
  cleared on ``verdict=succeeded`` per ADR-D5.
* :mod:`sovyx.dashboard.server` on ``dashboard.distribution.bundle_partial``
  / ``dashboard.distribution.bundle_missing`` AND cleared on
  ``dashboard.distribution.bundle_scanned{verdict=fully_present}``
  (Mission C5 §T2.1 / §T2.2). The ``axis="dashboard"`` axis
  proves the C4 forward-additive contract: a new operator-actionable
  degraded class slots into the same store + endpoint + banner with
  no schema migration.
* Later missions added further producers the same way, e.g.
  :mod:`sovyx.observability._resource_cohort_governor`
  (``axis="engine_resources"``, ADR-D5),
  :mod:`sovyx.voice.pipeline._heartbeat_mixin`, and
  :mod:`sovyx.voice.health.capture_integrity`.

Consumed by:

* :mod:`sovyx.dashboard.routes.engine_degraded` (composite endpoint,
  §T1.6).
* :mod:`sovyx.dashboard.voice_status` (populates
  ``VoiceStatusDegraded.composite_axes`` + ``composite_severity``,
  §T1.7).
* :mod:`sovyx.cli.commands.doctor` (CLI surface, Phase 3 §T3.6).

NOT persisted to disk — degraded state is *current* state, not
historical. On ``sovyx start`` the store is empty; the boot phase
populates it as warnings emit. Operator-acknowledgement state is
separate and DOES persist (Phase 3 ``operator_acks`` SQLite table).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Final

from sovyx.observability.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ActionChip:
    """Operator-actionable next-step rendered as a button chip.

    Attributes:
        label_token: i18n key for the chip label (e.g.
            ``"degraded.llm.noProvider.installOllama"``). Resolved by
            the frontend ``<DegradedBanner>`` via ``react-i18next``.
        action: One of:

            * ``"navigate"`` — react-router push to ``target`` path.
            * ``"dispatch"`` — POST to ``target`` API endpoint (legacy
              alias for ``api_post``; kept for back-compat with pre-H4
              chips emitted by C4/H5/H1/C6).
            * ``"external_link"`` — open ``target`` URL in a new tab.
            * ``"command_hint"`` — copy ``target`` (a CLI command
              string) to the operator's clipboard + show a success
              toast. Mission H4 §4.8 ADR-D8 v0.49.26.
            * ``"api_post"`` — ``apiFetch`` POST to ``target`` + show
              an ack toast on 2xx, error toast on failure. Mission H4
              §4.8 ADR-D8 v0.49.26.

            The zod twin at ``dashboard/src/types/schemas.ts``
            (``ActionChipSchema``) enforces this enum at the
            frontend boundary; emitting an unknown value rejects the
            whole DegradedEntry at parse time. New action types MUST
            land in BOTH this Python docstring AND the zod schema
            atomically.
        target: The route path / endpoint / URL / command string the
            action operates on. Semantics depend on ``action``.
        style: One of ``"default"``, ``"primary"``, or ``"danger"``;
            drives the chip's visual treatment.
    """

    label_token: str
    action: str
    target: str
    style: str = "default"


@dataclass(frozen=True, slots=True)
class DegradedEntry:
    """One degraded-axis ledger entry.

    Frozen + slotted so the store can safely return references in
    snapshots without consumers mutating the canonical state.

    Severity is per-entry because (a) a single axis MAY have multiple
    distinct degraded reasons live at the same time (e.g. a future
    LLM axis with both ``no_llm_provider`` and ``ollama_unreachable``
    — distinct remediation paths), and (b) the composite endpoint
    AGGREGATES per-entry severity into the operator-facing
    ``composite_severity`` per the amended ADR-D6 Hybrid rule
    (Mission D.1 / D-P0-1, 2026-05-21):
    ``composite = max(max(entry.severity), count_tier(distinct_axes))``
    under the ordering ``None < "warn" < "error" < "critical"``. A
    single axis emitting ``severity="critical"`` therefore propagates
    to the composite banner as ``"critical"`` rather than being
    collapsed to ``"warn"`` by the count-tier alone. See
    :func:`sovyx.dashboard.routes.engine_degraded._compute_composite_severity_hybrid`.
    """

    axis: str
    reason: str
    severity: str
    title_token: str
    body_token: str
    action_chips: tuple[ActionChip, ...] = field(default_factory=tuple)
    metadata: dict[str, Any] = field(default_factory=dict)
    first_observed_monotonic: float = 0.0
    last_observed_monotonic: float = 0.0
    occurrence_count: int = 1


class EngineDegradedStore:
    """Single-instance cross-axis degraded ledger.

    Thread-safe via a single ``threading.Lock``; reads return defensive
    copies so consumers may iterate without holding the lock.

    Cardinality bounded by :attr:`_MAX_ENTRIES` (32 — well above the
    realistic axis count of ≤ 8). On overflow the oldest entry by
    ``first_observed_monotonic`` is evicted; this should never trigger
    on real hardware but guards against bugs in producers that record
    repeatedly with fresh reasons.

    Anti-pattern compliance:

    * #5 — consumers MUST NOT call ``EngineDegradedStore()`` directly;
      use :func:`get_default_degraded_store`.
    * #15 — bounded cardinality with explicit eviction.
    * #34 — kill-switch flag (``degraded_banner_store_enabled`` future
      knob) NOT introduced; the store itself is always-on infrastructure.
    """

    _MAX_ENTRIES: Final[int] = 32

    def __init__(self) -> None:
        self._lock = Lock()
        self._by_reason: dict[str, DegradedEntry] = {}

    def record(self, entry: DegradedEntry) -> None:
        """Upsert: if ``reason`` already exists, preserve
        ``first_observed_monotonic`` + bump ``occurrence_count``;
        overwrite the rest of the fields with the new entry's values
        (latest severity / metadata wins).

        Else insert. On overflow (``len >= _MAX_ENTRIES``) the oldest
        entry by ``first_observed_monotonic`` is evicted before the
        insert.
        """
        with self._lock:
            existing = self._by_reason.get(entry.reason)
            if existing is not None:
                merged = DegradedEntry(
                    axis=existing.axis,
                    reason=existing.reason,
                    severity=entry.severity,
                    title_token=entry.title_token,
                    body_token=entry.body_token,
                    action_chips=entry.action_chips,
                    metadata=entry.metadata,
                    first_observed_monotonic=existing.first_observed_monotonic,
                    last_observed_monotonic=entry.last_observed_monotonic,
                    occurrence_count=existing.occurrence_count + 1,
                )
                self._by_reason[entry.reason] = merged
                return
            if len(self._by_reason) >= self._MAX_ENTRIES:
                oldest_key = min(
                    self._by_reason,
                    key=lambda r: self._by_reason[r].first_observed_monotonic,
                )
                del self._by_reason[oldest_key]
                logger.warning(
                    "engine.degraded_store.eviction",
                    evicted_reason=oldest_key,
                    cardinality=len(self._by_reason),
                    max_entries=self._MAX_ENTRIES,
                )
            self._by_reason[entry.reason] = entry

    def clear_axis(self, axis: str) -> int:
        """Remove all entries for ``axis``. Returns the count removed.

        Called by a producer when the underlying degraded condition
        resolves (e.g. :mod:`_runtime_failover` calls
        ``clear_axis("voice")`` on ``voice.failover.ladder_complete``
        with ``verdict=succeeded``).
        """
        with self._lock:
            removed = [r for r, e in self._by_reason.items() if e.axis == axis]
            for r in removed:
                del self._by_reason[r]
            return len(removed)

    def clear_reason(self, reason: str) -> bool:
        """Remove a single entry by ``reason``. Returns ``True`` iff
        an entry was removed."""
        with self._lock:
            return self._by_reason.pop(reason, None) is not None

    def snapshot(self) -> list[DegradedEntry]:
        """Read-only snapshot for cross-thread consumption.

        Returns a fresh list of references; entries are frozen
        dataclasses so consumers cannot mutate canonical state.
        """
        with self._lock:
            return list(self._by_reason.values())

    def distinct_axes(self) -> list[str]:
        """Sorted list of distinct ``axis`` values across all entries.

        Used by the composite-severity computation at
        :mod:`sovyx.dashboard.routes.engine_degraded` to determine the
        aggregate severity per ADR-D6.
        """
        with self._lock:
            return sorted({e.axis for e in self._by_reason.values()})

    def __len__(self) -> int:
        with self._lock:
            return len(self._by_reason)


_SINGLETON: EngineDegradedStore | None = None


def get_default_degraded_store() -> EngineDegradedStore:
    """Return (and lazily construct) the process-wide degraded store.

    Module-level lazy singleton — matches the C3
    :func:`sovyx.voice.health._failover_history.get_default_failover_history`
    pattern. Tests use :func:`reset_default_degraded_store` between
    cases for isolation.
    """
    global _SINGLETON  # noqa: PLW0603 — lazy singleton, not user-mutable state
    if _SINGLETON is None:
        _SINGLETON = EngineDegradedStore()
    return _SINGLETON


def reset_default_degraded_store() -> None:
    """Drop the singleton — tests use this between cases for isolation."""
    global _SINGLETON  # noqa: PLW0603
    _SINGLETON = None


def make_action_chip(
    label_token: str,
    action: str,
    target: str,
    *,
    style: str = "default",
) -> ActionChip:
    """Factory shim — keeps producer call sites compact + testable."""
    return ActionChip(
        label_token=label_token,
        action=action,
        target=target,
        style=style,
    )


def now_monotonic() -> float:
    """Thin alias — keeps test patching the monotonic-clock source easy."""
    return time.monotonic()


__all__ = [
    "ActionChip",
    "DegradedEntry",
    "EngineDegradedStore",
    "get_default_degraded_store",
    "make_action_chip",
    "now_monotonic",
    "reset_default_degraded_store",
]
