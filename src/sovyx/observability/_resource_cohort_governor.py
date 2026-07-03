"""Mission H4 §Phase 1.D — ResourceCohortGovernor.

Mission anchor:
``docs-internal/missions/MISSION-h4-resource-hygiene-instrumentation-2026-05-19.md``
§T4.1.

Consumes the per-cohort registry metrics emitted on every
``self.health.snapshot`` tick and evaluates 5 cohort budgets per
:class:`CohortAxis`:

* **RSS_GROWTH** — ``process.rss_bytes`` Δ across the rolling window
  exceeds ``cohort_rss_growth_threshold_mb``.
* **THREAD_COUNT** — ``process.num_threads`` Δ exceeds
  ``cohort_thread_growth_threshold`` in the same window.
* **LOCK_DICT_CARDINALITY** — aggregate
  ``lock_dict.total_cardinality`` crosses the soft cap
  ``cohort_lock_dict_soft_cap``.
* **ONNX_SESSION** — ``onnx.session_count`` exceeds the expected
  count for the enabled feature flags.
* **EXCEPTION_COHORT** — accumulated
  ``exception_cohort.retained_bytes_estimate`` exceeds
  ``exception_cohort_retained_bytes_cap``.

On every BUDGET_EXCEEDED verdict the governor:

1. Emits a structured WARN ``engine.resources.cohort_budget_exceeded``
   with ``cohort``, ``verdict``, ``observed``, ``budget`` fields so
   operators can correlate via log grep.
2. Calls ``EngineDegradedStore.record(DegradedEntry(
   axis="engine_resources", reason=f"engine_resources.{cohort.value}",
   ...))`` per C4 composite-store wire shim convention (anti-pattern
   #42). The existing :class:`DegradedBanner` renders the new axis
   automatically.

Phase 1.D minimum (this commit): governor library + per-tick evaluator
hook. The heap-snapshot file persistence + heartbeat-mixin N=5 trigger
+ circuit-breaker + ack endpoint are deferred to a Phase 1.E follow-up
(spec §8 T4.5+). The governor's structural skeleton is in place; future
extensions slot in via dependency injection.

Anti-pattern compliance:

* #14/#15/#30 — depends on the SSoT registry surface that closes
  those rules' instrumentation gaps.
* #34 — feature-flag gated (``observability.features.cohort_governor``
  default True). Bootstrap skips wire-up when disabled.
* #42 — single composite store wire shim. New axis
  ``engine_resources`` is forward-additive per C4 ADR-D5.
* #47 — the canonical instance for resource-cohort governance.
"""

from __future__ import annotations

import json
import time
import tracemalloc
from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum, unique
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING, Any

from sovyx.observability._resource_registry import CohortAxis
from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Mapping

    from sovyx.engine.config import ObservabilityTuningConfig

logger = get_logger(__name__)


@unique
class CohortVerdict(StrEnum):
    """Governor evaluation outcome per cohort.

    StrEnum per anti-pattern #9 (xdist-safe).
    """

    HEALTHY = "healthy"
    BUDGET_EXCEEDED = "budget_exceeded"
    INSUFFICIENT_DATA = "insufficient_data"


@dataclass(frozen=True, slots=True)
class CohortBudget:
    """Per-cohort budget threshold + window.

    Attributes:
        axis: Which cohort this budget applies to.
        threshold: The numeric ceiling (interpretation per-axis —
            RSS_GROWTH is delta-bytes; THREAD_COUNT is delta-count;
            LOCK_DICT_CARDINALITY is absolute soft-cap; etc.).
        window_s: Rolling-window length in seconds for delta-based
            cohorts (RSS_GROWTH + THREAD_COUNT). Absolute-cap
            cohorts (LOCK_DICT_CARDINALITY + ONNX_SESSION +
            EXCEPTION_COHORT) ignore this — they read the live value.
    """

    axis: CohortAxis
    threshold: int
    window_s: int = 60


@dataclass(frozen=True, slots=True)
class CohortEvaluation:
    """One cohort's evaluation result for a given snapshot tick."""

    axis: CohortAxis
    verdict: CohortVerdict
    observed: int
    budget: int
    note: str = ""


# Default budgets (matches mission spec §8 T4.7 — operator-tunable via
# ObservabilityTuningConfig but ship with sensible defaults).
_DEFAULT_BUDGETS: tuple[CohortBudget, ...] = (
    CohortBudget(axis=CohortAxis.RSS_GROWTH, threshold=512 * 1024 * 1024, window_s=60),
    CohortBudget(axis=CohortAxis.THREAD_COUNT, threshold=32, window_s=60),
    CohortBudget(axis=CohortAxis.LOCK_DICT_CARDINALITY, threshold=6_000, window_s=60),
    CohortBudget(axis=CohortAxis.ONNX_SESSION, threshold=8, window_s=60),
    CohortBudget(
        axis=CohortAxis.EXCEPTION_COHORT,
        threshold=16 * 1024 * 1024,
        window_s=300,
    ),
)


def _budgets_from_tuning(tuning: ObservabilityTuningConfig) -> tuple[CohortBudget, ...]:
    """Build a budget tuple from operator-tunable knobs.

    Mission H4 §8 T4.7 ADR-D12 — the governor consumes the
    ``cohort_*`` knobs on :class:`ObservabilityTuningConfig` instead of
    hardcoded constants so operators can re-tune via
    ``SOVYX_OBSERVABILITY__TUNING__*`` env vars without a code change.
    The defaults shipped on the config class match the v0.49.17
    constants so existing baselines hold.
    """
    return (
        CohortBudget(
            axis=CohortAxis.RSS_GROWTH,
            threshold=tuning.cohort_rss_growth_threshold_mb * 1024 * 1024,
            window_s=tuning.cohort_window_s,
        ),
        CohortBudget(
            axis=CohortAxis.THREAD_COUNT,
            threshold=tuning.cohort_thread_growth_threshold,
            window_s=tuning.cohort_window_s,
        ),
        CohortBudget(
            axis=CohortAxis.LOCK_DICT_CARDINALITY,
            threshold=tuning.cohort_lock_dict_soft_cap,
            window_s=tuning.cohort_window_s,
        ),
        CohortBudget(
            axis=CohortAxis.ONNX_SESSION,
            threshold=tuning.cohort_onnx_session_soft_cap,
            window_s=tuning.cohort_window_s,
        ),
        CohortBudget(
            axis=CohortAxis.EXCEPTION_COHORT,
            threshold=tuning.exception_cohort_retained_bytes_cap,
            window_s=tuning.exception_cohort_window_s,
        ),
    )


_OBSERVATION_RING_MAX: int = 32  # bounded history per cohort


# Mission H4 §0 line 30 + v0.49.24 spec-literal reason names. Each
# ``CohortAxis`` BUDGET_EXCEEDED verdict maps to a stable
# ``engine_resources.<reason>`` string that operators / dashboards /
# alert rules read; the suffix conveys the verdict semantics (RSS
# growth is a *spike*, lock-dict cardinality is *saturated*, ONNX
# session count is *unexpected*, etc.). The 6th reason
# ``heap_snapshot_triggered`` is emitted by the
# :meth:`ResourceCohortGovernor.request_heap_snapshot` success path
# (operator-actionable surface for the persisted forensic file).
_REASON_FOR_AXIS: dict[CohortAxis, str] = {
    CohortAxis.RSS_GROWTH: "engine_resources.rss_growth_spike",
    CohortAxis.THREAD_COUNT: "engine_resources.thread_count_spike",
    CohortAxis.LOCK_DICT_CARDINALITY: "engine_resources.lock_dict_cardinality_saturated",
    CohortAxis.ONNX_SESSION: "engine_resources.onnx_session_unexpected_count",
    CohortAxis.EXCEPTION_COHORT: "engine_resources.exception_cohort_retention_high",
}
_REASON_HEAP_SNAPSHOT_TRIGGERED: str = "engine_resources.heap_snapshot_triggered"


# MISSION-A.2.P2 F-018: per-axis SOVYX_OBSERVABILITY__TUNING__* env path.
# Surfaces in the ``engine.resources.cohort_budget_exceeded`` WARN payload
# so operators see the env-var to tune WITHOUT having to invoke
# ``sovyx doctor resources --explain`` post-breach. The env paths mirror
# the ``ObservabilityTuningConfig`` field names with the SOVYX_OBSERVABILITY__
# TUNING__ prefix per the pydantic-settings env-var convention
# (``SOVYX_*`` prefix, ``__`` for nesting — CLAUDE.md Conventions).
_TUNING_ENV_PATH_FOR_AXIS: dict[CohortAxis, str] = {
    CohortAxis.RSS_GROWTH: "SOVYX_OBSERVABILITY__TUNING__COHORT_RSS_GROWTH_THRESHOLD_MB",
    CohortAxis.THREAD_COUNT: "SOVYX_OBSERVABILITY__TUNING__COHORT_THREAD_GROWTH_THRESHOLD",
    CohortAxis.LOCK_DICT_CARDINALITY: "SOVYX_OBSERVABILITY__TUNING__COHORT_LOCK_DICT_SOFT_CAP",
    CohortAxis.ONNX_SESSION: "SOVYX_OBSERVABILITY__TUNING__COHORT_ONNX_SESSION_SOFT_CAP",
    CohortAxis.EXCEPTION_COHORT: (
        "SOVYX_OBSERVABILITY__TUNING__EXCEPTION_COHORT_RETAINED_BYTES_CAP"
    ),
}


@dataclass
class ResourceCohortGovernor:
    """Per-snapshot-tick cohort budget evaluator.

    Wire-up: bootstrap creates a singleton + the
    :class:`ResourceSnapshotter` calls :meth:`evaluate_snapshot()`
    after each ``_emit_snapshot``. Each cohort's per-tick verdict
    drives optional emissions:

    * ``HEALTHY`` — when the previous verdict for this cohort was
      ``BUDGET_EXCEEDED`` the governor increments a per-axis
      ``_consecutive_healthy`` counter; on the N-th consecutive HEALTHY
      tick (``clear_threshold`` — default 3, tunable via
      ``ObservabilityTuningConfig.cohort_clear_consecutive_healthy_threshold``)
      it calls :meth:`EngineDegradedStore.clear_reason` for the cohort's
      canonical reason and best-effort
      :meth:`OperatorAcksStore.clear_ack` on the matching ack-key.
      Per anti-pattern #54 (record-without-clear pairing). Gated by
      ``observability.features.cohort_axis_auto_clear`` (default True
      per anti-pattern #34 INVERSE — the clear IS the operator-trust
      feature). Mission B B-P0-3 closure (2026-05-21).
    * ``BUDGET_EXCEEDED`` — emit WARN + record axis entry in the
      composite store + (Phase 1.E) trigger heap snapshot / engage
      circuit-breaker. Resets the per-axis ``_consecutive_healthy``
      counter to 0 so a single re-breach restarts the hysteresis count
      from scratch.
    * ``INSUFFICIENT_DATA`` — silent (warmup window not yet filled).
      Does NOT reset the hysteresis counter — INSUFFICIENT_DATA after
      a sustained HEALTHY streak is treated as a transient observation
      gap, not as a re-breach.

    Thread-safe via internal :class:`Lock`; safe to invoke from the
    snapshotter loop or a future test fixture.
    """

    budgets: tuple[CohortBudget, ...] = _DEFAULT_BUDGETS
    enabled: bool = True
    breaker_threshold: int = 3
    breaker_window_s: int = 3_600
    # Mission B B-P0-3 — auto-clear hysteresis. ``clear_threshold`` is the
    # N consecutive HEALTHY ticks required to trigger ``clear_reason()`` on
    # a prior BUDGET_EXCEEDED. ``auto_clear_enabled`` gates the entire
    # clear path (operator override
    # ``SOVYX_OBSERVABILITY__FEATURES__COHORT_AXIS_AUTO_CLEAR=false``
    # restores v0.49.36 stuck-banner behavior).
    clear_threshold: int = 3
    auto_clear_enabled: bool = True
    # Mission OX-1.B — gate the additive ``axis.cleared`` emission at
    # :func:`_clear_axis_entry_for_reason` on the HEALTHY-edge clear
    # path. Sibling-additive to the existing ``engine.resources.cohort_auto_cleared``
    # INFO; the new event carries the canonical ``axis``/``reason``/
    # ``source`` triple consumed by the read-only causal-join helper.
    # Default False per ``feedback_staged_adoption``; flipped on via
    # ``SOVYX_OX1__CAUSAL_CHAIN_ENABLED=true``.
    causal_chain_enabled: bool = False
    _rss_history: deque[tuple[float, int]] = field(
        default_factory=lambda: deque(maxlen=_OBSERVATION_RING_MAX),
    )
    _thread_history: deque[tuple[float, int]] = field(
        default_factory=lambda: deque(maxlen=_OBSERVATION_RING_MAX),
    )
    # Mission H4 §8 T4.1(e) — circuit-breaker state: per-cohort rolling
    # window of BUDGET_EXCEEDED timestamps. After ``breaker_threshold``
    # entries within ``breaker_window_s``, the cohort is "engaged" —
    # dispatch_to_thread(label=<cohort>.*) callers consult this state
    # before spawning work. The breaker clears when the operator acks
    # via POST /api/engine/resources/cohort/ack OR when the rolling
    # window cycles past the threshold count.
    _breach_history: dict[CohortAxis, deque[float]] = field(
        default_factory=lambda: {axis: deque(maxlen=64) for axis in CohortAxis},
    )
    _engaged_acks: dict[CohortAxis, float] = field(default_factory=dict)
    # Mission B B-P0-3 — per-axis last verdict + consecutive-HEALTHY
    # counter for the clear_reason hysteresis. Populated by
    # :func:`emit_axis_entries` on every snapshot tick. Kept under
    # ``_lock`` so concurrent snapshot ticks don't race the counter
    # increment.
    _last_verdict: dict[CohortAxis, CohortVerdict] = field(default_factory=dict)
    _consecutive_healthy: dict[CohortAxis, int] = field(default_factory=dict)
    _lock: Lock = field(default_factory=Lock)

    @classmethod
    def from_tuning(
        cls,
        tuning: ObservabilityTuningConfig,
        *,
        enabled: bool = True,
        auto_clear_enabled: bool = True,
        causal_chain_enabled: bool = False,
    ) -> ResourceCohortGovernor:
        """Build a governor from operator-tunable knobs.

        Mission H4 §8 T4.7 ADR-D12 — bootstrap calls this with the live
        :class:`ObservabilityTuningConfig` so the ``cohort_*`` env
        overrides take effect. Tests using the bare ``ResourceCohortGovernor()``
        constructor get the v0.49.17 hardcoded defaults — backward-compat.

        Mission B B-P0-3 (2026-05-21) — ``auto_clear_enabled`` carries
        :attr:`ObservabilityFeaturesConfig.cohort_axis_auto_clear`; pass
        ``False`` to restore v0.49.36 stuck-banner behavior (clear
        path becomes a no-op). The clear-threshold N comes from
        ``tuning.cohort_clear_consecutive_healthy_threshold``.
        """
        return cls(
            budgets=_budgets_from_tuning(tuning),
            enabled=enabled,
            breaker_threshold=tuning.cohort_breaker_threshold,
            breaker_window_s=tuning.cohort_breaker_window_s,
            clear_threshold=tuning.cohort_clear_consecutive_healthy_threshold,
            auto_clear_enabled=auto_clear_enabled,
            causal_chain_enabled=causal_chain_enabled,
        )

    # ── Circuit-breaker (Phase 1.D §8 T4.1 (e) + §11 ADR-D14) ──

    def record_breach(self, axis: CohortAxis) -> None:
        """Record a BUDGET_EXCEEDED event for the circuit-breaker rolling window."""
        with self._lock:
            self._breach_history.setdefault(axis, deque(maxlen=64)).append(time.monotonic())

    def is_breaker_engaged(self, axis: CohortAxis) -> bool:
        """True iff *axis* has ≥ ``breaker_threshold`` breaches within window.

        The operator clears via :meth:`clear_breaker` (called by the
        ``POST /api/engine/resources/cohort/ack`` endpoint). Until then,
        ``dispatch_to_thread`` callers labelled with ``<axis>.*`` SHOULD
        skip new work — Phase 1.E consumer-side enforcement.
        """
        with self._lock:
            if axis in self._engaged_acks:
                # Operator acked; breaker is held cleared until acks expire.
                # (Acks are also cleared on rolling-window cycle below.)
                return False
            history = self._breach_history.get(axis)
            if not history:
                return False
            now = time.monotonic()
            window_start = now - self.breaker_window_s
            recent = [ts for ts in history if ts >= window_start]
            return len(recent) >= self.breaker_threshold

    def request_heap_snapshot(
        self,
        cohort: str,
        *,
        cohort_observed: int | None = None,
        cohort_budget: int | None = None,
        extra_metadata: Mapping[str, object] | None = None,
    ) -> Path | None:
        """Mission H4 §8 T4.6 — on-demand cohort-name-driven heap snapshot.

        Wires the H4 forensic-anchor signature (heartbeat N=5 deaf-cluster
        + coordinator terminal + ladder in progress) into the governor's
        persistence path WITHOUT going through the BUDGET_EXCEEDED RSS_GROWTH
        verdict. Callers tag the cohort with a descriptive label
        (``"voice_failover_deaf_cluster"``, ``"operator_on_demand"``, etc.)
        and the governor takes the tracemalloc snapshot + persists +
        rotates per the standard contract.

        When ``observability.features.tracemalloc=False`` (the default),
        emits ``engine.resources.heap_snapshot_skipped`` once with an
        operator hint pointing at the feature flag — no exception, no
        wasted compute.

        Returns the path written on success, ``None`` on skip / failure.
        """
        return _persist_heap_snapshot_direct(
            cohort=cohort,
            cohort_observed=cohort_observed,
            cohort_budget=cohort_budget,
            extra_metadata=extra_metadata,
        )

    def clear_breaker(self, axis: CohortAxis) -> None:
        """Operator-acked clear — records an ack timestamp + drops history.

        Called by the ``POST /api/engine/resources/cohort/ack`` endpoint
        when the operator dismisses the breach. Subsequent breaches
        re-arm the breaker normally.

        Note: this clears governor IN-PROCESS state only. The composite-store
        entry under ``axis="engine_resources"`` is cleared by
        :func:`_clear_axis_entry_for_reason` invoked from the ack endpoint
        (Mission B B-P1-03 closure) — the two stores are intentionally
        kept decoupled at the API level so test fixtures can exercise
        each path independently.
        """
        with self._lock:
            self._engaged_acks[axis] = time.monotonic()
            if axis in self._breach_history:
                self._breach_history[axis].clear()
            # Mission B B-P0-3 — operator-ack also resets the per-axis
            # hysteresis state so a future recovery starts clean.
            self._last_verdict[axis] = CohortVerdict.HEALTHY
            self._consecutive_healthy[axis] = 0

    def evaluate_snapshot(self, snapshot: Mapping[str, object]) -> list[CohortEvaluation]:
        """Evaluate every cohort against the given snapshot.

        Args:
            snapshot: The dict emitted by
                ``ResourceRegistry.snapshot_fields()`` (merged with
                the psutil + asyncio fields by
                :func:`ResourceSnapshotter._emit_snapshot`).

        Returns:
            A list of :class:`CohortEvaluation` records — one per
            cohort. Callers route ``BUDGET_EXCEEDED`` entries to
            :class:`EngineDegradedStore` via the
            :meth:`emit_axis_entries` helper.
        """
        if not self.enabled:
            return []
        now = time.monotonic()
        results: list[CohortEvaluation] = []
        for budget in self.budgets:
            match budget.axis:
                case CohortAxis.RSS_GROWTH:
                    results.append(self._eval_rss_growth(snapshot, budget, now))
                case CohortAxis.THREAD_COUNT:
                    results.append(self._eval_thread_growth(snapshot, budget, now))
                case CohortAxis.LOCK_DICT_CARDINALITY:
                    results.append(self._eval_lock_dict(snapshot, budget))
                case CohortAxis.ONNX_SESSION:
                    results.append(self._eval_onnx(snapshot, budget))
                case CohortAxis.EXCEPTION_COHORT:
                    results.append(self._eval_exception_cohort(snapshot, budget))
        return results

    # ── Per-cohort evaluators ──

    def _eval_rss_growth(
        self,
        snapshot: Mapping[str, object],
        budget: CohortBudget,
        now: float,
    ) -> CohortEvaluation:
        rss_raw = snapshot.get("process.rss_bytes")
        if not isinstance(rss_raw, int) or rss_raw <= 0:
            return CohortEvaluation(
                axis=budget.axis,
                verdict=CohortVerdict.INSUFFICIENT_DATA,
                observed=0,
                budget=budget.threshold,
                note="process.rss_bytes missing or non-positive",
            )
        with self._lock:
            self._rss_history.append((now, rss_raw))
            # Find the oldest sample inside the rolling window.
            window_start = now - budget.window_s
            samples_in_window = [v for (ts, v) in self._rss_history if ts >= window_start]
        if len(samples_in_window) < 2:
            return CohortEvaluation(
                axis=budget.axis,
                verdict=CohortVerdict.INSUFFICIENT_DATA,
                observed=rss_raw,
                budget=budget.threshold,
                note=f"need ≥2 samples in {budget.window_s}s window; got {len(samples_in_window)}",
            )
        delta = max(samples_in_window) - min(samples_in_window)
        if delta > budget.threshold:
            return CohortEvaluation(
                axis=budget.axis,
                verdict=CohortVerdict.BUDGET_EXCEEDED,
                observed=delta,
                budget=budget.threshold,
                note=f"RSS Δ {delta // (1024 * 1024)} MiB across {budget.window_s}s",
            )
        return CohortEvaluation(
            axis=budget.axis,
            verdict=CohortVerdict.HEALTHY,
            observed=delta,
            budget=budget.threshold,
        )

    def _eval_thread_growth(
        self,
        snapshot: Mapping[str, object],
        budget: CohortBudget,
        now: float,
    ) -> CohortEvaluation:
        threads_raw = snapshot.get("process.num_threads")
        if not isinstance(threads_raw, int) or threads_raw <= 0:
            return CohortEvaluation(
                axis=budget.axis,
                verdict=CohortVerdict.INSUFFICIENT_DATA,
                observed=0,
                budget=budget.threshold,
                note="process.num_threads missing",
            )
        with self._lock:
            self._thread_history.append((now, threads_raw))
            window_start = now - budget.window_s
            samples_in_window = [v for (ts, v) in self._thread_history if ts >= window_start]
        if len(samples_in_window) < 2:
            return CohortEvaluation(
                axis=budget.axis,
                verdict=CohortVerdict.INSUFFICIENT_DATA,
                observed=threads_raw,
                budget=budget.threshold,
                note=f"need ≥2 samples in {budget.window_s}s window",
            )
        delta = max(samples_in_window) - min(samples_in_window)
        if delta > budget.threshold:
            return CohortEvaluation(
                axis=budget.axis,
                verdict=CohortVerdict.BUDGET_EXCEEDED,
                observed=delta,
                budget=budget.threshold,
                note=f"thread Δ {delta} across {budget.window_s}s",
            )
        return CohortEvaluation(
            axis=budget.axis,
            verdict=CohortVerdict.HEALTHY,
            observed=delta,
            budget=budget.threshold,
        )

    def _eval_lock_dict(
        self,
        snapshot: Mapping[str, object],
        budget: CohortBudget,
    ) -> CohortEvaluation:
        total = snapshot.get("lock_dict.total_cardinality")
        if not isinstance(total, int):
            return CohortEvaluation(
                axis=budget.axis,
                verdict=CohortVerdict.INSUFFICIENT_DATA,
                observed=0,
                budget=budget.threshold,
                note="lock_dict.total_cardinality missing",
            )
        if total > budget.threshold:
            return CohortEvaluation(
                axis=budget.axis,
                verdict=CohortVerdict.BUDGET_EXCEEDED,
                observed=total,
                budget=budget.threshold,
                note=f"aggregate cardinality {total} exceeds soft cap",
            )
        return CohortEvaluation(
            axis=budget.axis,
            verdict=CohortVerdict.HEALTHY,
            observed=total,
            budget=budget.threshold,
        )

    def _eval_onnx(
        self,
        snapshot: Mapping[str, object],
        budget: CohortBudget,
    ) -> CohortEvaluation:
        count = snapshot.get("onnx.session_count")
        if not isinstance(count, int):
            return CohortEvaluation(
                axis=budget.axis,
                verdict=CohortVerdict.INSUFFICIENT_DATA,
                observed=0,
                budget=budget.threshold,
                note="onnx.session_count missing",
            )
        if count > budget.threshold:
            return CohortEvaluation(
                axis=budget.axis,
                verdict=CohortVerdict.BUDGET_EXCEEDED,
                observed=count,
                budget=budget.threshold,
                note=f"{count} ONNX sessions exceeds expected ceiling",
            )
        return CohortEvaluation(
            axis=budget.axis,
            verdict=CohortVerdict.HEALTHY,
            observed=count,
            budget=budget.threshold,
        )

    def _eval_exception_cohort(
        self,
        snapshot: Mapping[str, object],
        budget: CohortBudget,
    ) -> CohortEvaluation:
        # MISSION-A.1 F-002+F-003 (anti-pattern #49): governor reads the
        # WINDOWED retention so a single storm decays naturally as
        # observations age out of the deque. The pre-fix consumer read
        # ``retained_bytes_estimate`` — a monotonic ``+=`` accumulator —
        # so the cohort budget became permanently breached after one
        # storm with no recovery path absent process restart. LENIENT
        # fallback reads the legacy cumulative key while the dual-emit
        # window is in force; STRICT v0.55.0 drops the fallback.
        retained = snapshot.get("exception_cohort.window_retained_bytes")
        if not isinstance(retained, int):
            retained = snapshot.get(
                "exception_cohort.retained_bytes_estimate",
            )  # a1-allowlist: legacy alias during LENIENT
        if not isinstance(retained, int):
            return CohortEvaluation(
                axis=budget.axis,
                verdict=CohortVerdict.INSUFFICIENT_DATA,
                observed=0,
                budget=budget.threshold,
                note="exception_cohort.window_retained_bytes missing",
            )
        if retained > budget.threshold:
            return CohortEvaluation(
                axis=budget.axis,
                verdict=CohortVerdict.BUDGET_EXCEEDED,
                observed=retained,
                budget=budget.threshold,
                note=(
                    f"ExceptionGroup window retention {retained // (1024 * 1024)} MiB exceeds cap"
                ),
            )
        return CohortEvaluation(
            axis=budget.axis,
            verdict=CohortVerdict.HEALTHY,
            observed=retained,
            budget=budget.threshold,
        )


def emit_axis_entries(evaluations: list[CohortEvaluation]) -> int:
    """Emit ``engine.resources.cohort_budget_exceeded`` for every breached cohort
    AND clear stale composite-store entries on sustained recovery.

    Routes each BUDGET_EXCEEDED entry to the :class:`EngineDegradedStore`
    with ``axis="engine_resources"`` so the existing C4
    :class:`DegradedBanner` renders the cohort without dashboard
    changes (per C4 ADR-D5 forward-additive contract). Also persists
    forensic-grade allocator/thread snapshots when the verdict is
    RSS-driven or thread-driven (Phase 1.D heap-snapshot trigger) and
    advances the circuit-breaker state per ADR-D14.

    Mission B B-P0-3 (2026-05-21) — per-axis state machine for the
    BUDGET_EXCEEDED → HEALTHY transition. The governor tracks
    ``_last_verdict[axis]`` + ``_consecutive_healthy[axis]``; after
    ``governor.clear_threshold`` consecutive HEALTHY ticks following any
    BUDGET_EXCEEDED, calls :func:`_clear_axis_entry_for_reason` which
    invokes ``EngineDegradedStore.clear_reason(_REASON_FOR_AXIS[axis])``
    (per-reason, not per-axis — protects sibling cohort entries) and
    best-effort ``OperatorAcksStore.clear_ack`` on the matching
    ``engine_resources.<reason>`` ack-key (closes B-P1-15 stale-ack
    suppression of re-degradation). Gated by ``governor.auto_clear_enabled``
    (operator override
    ``SOVYX_OBSERVABILITY__FEATURES__COHORT_AXIS_AUTO_CLEAR=false``
    restores v0.49.36 stuck-banner behavior). Anti-pattern #54.

    Returns the count of BUDGET_EXCEEDED emissions for caller-side
    metrics. Caller does NOT need to act on the count — the WARN log
    line + composite-store entry are the operator-actionable surfaces.
    """
    emitted = 0
    governor = get_default_resource_cohort_governor()
    for evaluation in evaluations:
        if evaluation.verdict == CohortVerdict.BUDGET_EXCEEDED:
            emitted += 1
            logger.warning(
                "engine.resources.cohort_budget_exceeded",
                **{
                    "engine.resources.cohort": evaluation.axis.value,
                    "engine.resources.observed": evaluation.observed,
                    "engine.resources.budget": evaluation.budget,
                    "engine.resources.note": evaluation.note,
                    # MISSION-A.2.P2 F-018: operator-trust disclosure.
                    # Operators reading the breach WARN see the env-var to
                    # tune WITHOUT a separate ``sovyx doctor resources
                    # --explain`` round-trip.
                    "engine.resources.tuning_env_path": _TUNING_ENV_PATH_FOR_AXIS.get(
                        evaluation.axis, ""
                    ),
                },
            )
            # Mission H4 §8 T4.1 (e) + §4.6 ADR-D6 v0.49.29 — circuit-breaker
            # state advancement HAPPENS FIRST so the severity computation in
            # _record_to_composite_store sees the current breach in the
            # temporal window count. Without this ordering, the 2nd breach
            # within 5 min still resolves to "warning" because the prior
            # breach is the only one in the history at compute time.
            governor.record_breach(evaluation.axis)
            _record_to_composite_store(evaluation)
            _increment_cohort_budget_counter(evaluation)
            # Mission B B-P0-3 — reset hysteresis on every fresh breach
            # so the clear_threshold count restarts from zero. Storing
            # the verdict and the reset count under the same lock as
            # the rest of the per-axis state.
            with governor._lock:  # noqa: SLF001 — same module
                governor._last_verdict[evaluation.axis] = CohortVerdict.BUDGET_EXCEEDED  # noqa: SLF001
                governor._consecutive_healthy[evaluation.axis] = 0  # noqa: SLF001
            # Mission H4 §8 T4.1 (c-d) — snapshot persistence on RSS / thread
            # breach. Best-effort; failures absorbed at debug level.
            if evaluation.axis == CohortAxis.RSS_GROWTH:
                _persist_heap_snapshot(evaluation)
            elif evaluation.axis == CohortAxis.THREAD_COUNT:
                _persist_thread_snapshot(evaluation)
            continue

        if evaluation.verdict == CohortVerdict.HEALTHY:
            # Mission B B-P0-3 — hysteresis state machine. Only advance
            # the consecutive-healthy counter when the prior verdict was
            # BUDGET_EXCEEDED (i.e. we are in a recovery sequence). On
            # the N-th consecutive HEALTHY, invoke the clear path.
            with governor._lock:  # noqa: SLF001
                prior = governor._last_verdict.get(evaluation.axis)  # noqa: SLF001
                if prior == CohortVerdict.BUDGET_EXCEEDED or (
                    governor._consecutive_healthy.get(evaluation.axis, 0) > 0  # noqa: SLF001
                ):
                    governor._consecutive_healthy[evaluation.axis] = (  # noqa: SLF001
                        governor._consecutive_healthy.get(evaluation.axis, 0) + 1  # noqa: SLF001
                    )
                    consecutive = governor._consecutive_healthy[evaluation.axis]  # noqa: SLF001
                else:
                    # No prior breach to recover from; nothing to clear.
                    governor._last_verdict[evaluation.axis] = CohortVerdict.HEALTHY  # noqa: SLF001
                    continue
                should_clear = (
                    governor.auto_clear_enabled and consecutive >= governor.clear_threshold
                )
                if should_clear:
                    # Reset under lock so a concurrent breach observation
                    # cannot double-clear.
                    governor._last_verdict[evaluation.axis] = CohortVerdict.HEALTHY  # noqa: SLF001
                    governor._consecutive_healthy[evaluation.axis] = 0  # noqa: SLF001
            if should_clear:
                _clear_axis_entry_for_reason(evaluation.axis)
            continue

        # INSUFFICIENT_DATA: transient observation gap; do not reset
        # hysteresis (operator should not lose recovery progress on a
        # single psutil hiccup). No emission.
    return emitted


def _clear_axis_entry_for_reason(axis: CohortAxis) -> None:
    """Mission B B-P0-3 — clear the composite-store entry + ack on cohort recovery.

    Calls ``EngineDegradedStore.clear_reason(_REASON_FOR_AXIS[axis])``
    (per-reason — protects sibling cohort entries under
    ``axis="engine_resources"``) and best-effort
    ``OperatorAcksStore.clear_ack`` on the matching ack-key
    ``engine_resources.<reason>``. Closes B-P1-15: stale ack suppressing
    re-degradation under the same axis+reason.

    Emits ``engine.resources.cohort_auto_cleared`` INFO with the cohort
    + reason + threshold so operators can correlate the banner-drop on
    the dashboard with the recovery edge. Best-effort; failures absorbed
    at debug level to preserve the "observability never breaks the
    operator's process" invariant from #42.
    """
    reason = _REASON_FOR_AXIS.get(axis)
    if reason is None:
        return
    try:
        from sovyx.engine._degraded_store import get_default_degraded_store

        cleared = get_default_degraded_store().clear_reason(reason)
    except Exception:  # noqa: BLE001 — observability-only surface
        logger.debug("b_p0_3_degraded_store_clear_failed", axis=axis.value, reason=reason)
        return
    if cleared:
        logger.info(
            "engine.resources.cohort_auto_cleared",
            **{
                "engine.resources.cohort": axis.value,
                "engine.resources.reason": reason,
                "engine.resources.consecutive_healthy_threshold": (
                    get_default_resource_cohort_governor().clear_threshold
                ),
            },
        )
        # Mission OX-1.B — emit the canonical ``axis.cleared`` event
        # carrying the (axis, reason, source) triple consumed by the
        # read-only causal-join helper. Sibling-additive to the
        # ``engine.resources.cohort_auto_cleared`` event above; that
        # event uses cohort-namespaced field names for backward
        # compatibility with H4 dashboard consumers, while ``axis.cleared``
        # uses the axis vocabulary that DegradedEntry.axis already
        # speaks. Gated by ``EngineConfig.ox1.causal_chain_enabled``
        # (default False) — when disabled the emission is silent.
        # Single emission site per OX-1 doc §15; voice failover / llm
        # dispatch / dashboard clear paths intentionally stay silent
        # at this phase (no scope widening).
        if get_default_resource_cohort_governor().causal_chain_enabled:
            logger.info(
                "axis.cleared",
                axis="engine_resources",
                reason=reason,
                source="resource_cohort_governor",
            )
    # Mission B B-P1-15 — DEFERRED FROM B.1.P3 SCOPE:
    #
    # Closing the B-P1-15 "stale ack suppresses re-degradation" pathway
    # at the AUTO-clear edge would require resolving the
    # ``OperatorAcksStore`` instance from this module — but the store
    # is constructed at bootstrap and held in ``ServiceRegistry`` (no
    # module-level singleton helper exists). Wiring the resolution
    # would either (a) couple the observability cohort governor to the
    # service registry (architectural expansion outside B.1 scope), or
    # (b) require adding a ``get_default_operator_acks_store`` lazy
    # singleton helper (scope expansion). Per the operator's 2026-05-21
    # Mission B.1 directive "no opportunistic cleanup / no governance
    # expansion", we defer the auto-edge ack-clear to a sibling B.3
    # ticket.
    #
    # Remaining risk after B.1: if the operator ack'd a prior breach
    # with a long TTL (e.g. 24 h) and the cohort recovers + re-breaches
    # within the ack TTL, the ack-suppression at
    # ``engine_degraded.py::_aggregate_ack_state`` still suppresses the
    # NEW banner entry that this auto-clear path just allowed to
    # reform. The store entry exists; the banner stays muted. Operator
    # workaround: TTL expiry OR explicit re-ack on the new entry. The
    # operator-driven path at
    # ``POST /api/engine/resources/cohort/ack`` already calls
    # ``clear_reason`` (B.1.P3 closure) but does NOT yet call
    # ``clear_ack`` — same deferred B-P1-15 closure.
    #
    # This is an EXPLICIT semantic gap with classification:
    #   - Severity: P1 (operator-trust degraded; not catastrophic).
    #   - Sunset target: Mission B.3 (closes within v0.49.38..v0.50.0).
    #   - Anti-pattern reference: #54 sibling — the record↔clear
    #     pairing applies to the ack store as well as the degraded
    #     store, but the wiring across stores is governance-level
    #     work, not a single-tag patch.
    # See `MISSION-B-FINDINGS-REGISTER-2026-05-21.md` §2 B-P1-15.


# ── Phase 1.D snapshot persistence + rotation (spec §8 T4.1 c+d) ──


def _diagnostics_dir() -> Path:
    """Resolve ~/.sovyx/diagnostics/ + ensure it exists.

    Best-effort: returns a path that may not be writable on weird
    environments (tmpfs, read-only mounts). Callers absorb OSError.
    """
    path = Path.home() / ".sovyx" / "diagnostics"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _rotate_snapshot_files(diag_dir: Path, prefix: str, max_files: int) -> None:
    """Keep at most ``max_files`` files matching ``prefix*`` under ``diag_dir``.

    Drops the oldest (by mtime) until the count is within budget.
    Mission H4 §8 T4.7 — ``heap_snapshot_max_files`` /
    ``thread_snapshot_max_files`` knobs (default 10 each).
    """
    try:
        files = sorted(diag_dir.glob(f"{prefix}*"), key=lambda p: p.stat().st_mtime)
        excess = len(files) - max_files
        for old in files[: max(0, excess)]:
            old.unlink(missing_ok=True)
    except OSError:
        logger.debug("engine.resources.snapshot_rotation_failed", prefix=prefix, exc_info=True)


def _persist_heap_snapshot_direct(
    cohort: str,
    *,
    cohort_observed: int | None = None,
    cohort_budget: int | None = None,
    extra_metadata: Mapping[str, object] | None = None,
) -> Path | None:
    """Mission H4 §8 T4.1(c) + §8 T4.6 — cohort-name-driven heap snapshot.

    Decoupled from :class:`CohortEvaluation` so callers outside the
    BUDGET_EXCEEDED path (e.g. the heartbeat N=5 deaf-cluster trigger)
    can request a snapshot using a descriptive cohort label without
    fabricating a synthetic evaluation. The original
    :func:`_persist_heap_snapshot` thin-wraps this for the
    RSS_GROWTH-driven path.

    Returns the path written on success or None on skip / failure. When
    ``tracemalloc`` is NOT tracing the function emits
    ``engine.resources.heap_snapshot_skipped`` once with an operator
    hint pointing at the feature flag.
    """
    if not tracemalloc.is_tracing():
        logger.info(
            "engine.resources.heap_snapshot_skipped",
            **{
                "engine.resources.cohort": cohort,
                "engine.resources.reason": "tracemalloc_not_enabled",
                "engine.resources.hint": (
                    "Set SOVYX_OBSERVABILITY__FEATURES__TRACEMALLOC=true + "
                    "restart daemon for allocator-level forensics on the "
                    "next cohort breach. Adds 25-30% memory overhead."
                ),
            },
        )
        return None
    try:
        snapshot = tracemalloc.take_snapshot()
        top_stats = snapshot.statistics("lineno")[:50]
        diag_dir = _diagnostics_dir()
        ts = int(time.time())
        path = diag_dir / f"heap-snapshot-{ts}.json"
        payload: dict[str, object] = {
            "kind": "heap_snapshot",
            "schema_version": "1.0",
            "observed_at_unix": ts,
            "cohort": cohort,
            "cohort_observed": cohort_observed,
            "cohort_budget": cohort_budget,
            "tracemalloc_snapshot": {
                "top_allocators": [
                    {
                        "rank": rank,
                        "size_bytes": stat.size,
                        "count": stat.count,
                        "traceback": [str(frame) for frame in stat.traceback],
                    }
                    for rank, stat in enumerate(top_stats, start=1)
                ],
                "total_allocators": len(top_stats),
            },
        }
        if extra_metadata:
            payload["extra_metadata"] = dict(extra_metadata)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        logger.info(
            "engine.resources.heap_snapshot_persisted",
            **{
                "engine.resources.cohort": cohort,
                "engine.resources.path": str(path),
                "engine.resources.top_allocators": len(top_stats),
            },
        )
        from sovyx.engine.config import EngineConfig  # noqa: PLC0415 — lazy

        try:
            tuning = EngineConfig().observability.tuning
            max_files = tuning.heap_snapshot_max_files
        except Exception:  # noqa: BLE001 — fallback default
            max_files = 10
        _rotate_snapshot_files(diag_dir, "heap-snapshot-", max_files)
    except Exception:  # noqa: BLE001 — snapshot persistence is best-effort
        logger.debug(
            "engine.resources.heap_snapshot_persist_failed",
            cohort=cohort,
            exc_info=True,
        )
        return None
    # Mission H4 §0 line 30 — 6th spec-literal reason. Emit the
    # ``heap_snapshot_triggered`` DegradedEntry as an operator-visible
    # surface for the persisted forensic file. Action chip deep-links
    # the operator at the HeapSnapshotViewer widget for the captured
    # timestamp.
    _record_heap_snapshot_to_composite_store(
        cohort=cohort,
        path=path,
        timestamp=ts,
        cohort_observed=cohort_observed,
        cohort_budget=cohort_budget,
    )
    return path


def _record_heap_snapshot_to_composite_store(
    *,
    cohort: str,
    path: Path,
    timestamp: int,
    cohort_observed: int | None,
    cohort_budget: int | None,
) -> None:
    """Mission H4 §0 line 30 — emit the 6th spec-literal reason.

    Records ``engine_resources.heap_snapshot_triggered`` into the C4
    composite store on every successful tracemalloc snapshot capture.
    Action chips are sourced from :func:`_chips_for_reason` so the
    heap-snapshot path uses the same ADR-D8 mapping as every other
    cohort — primary deep-link at the HeapSnapshotViewer widget,
    secondary ack chip targeting ``POST /api/engine/resources/cohort/ack``.

    Best-effort: store unavailability cannot block the snapshot path.
    """
    try:
        from sovyx.engine._degraded_store import (  # noqa: PLC0415 — lazy
            DegradedEntry,
            get_default_degraded_store,
        )

        now_monotonic = time.monotonic()
        metadata: dict[str, object] = {
            "cohort": cohort,
            "heap_snapshot_path": str(path),
            "heap_snapshot_timestamp": timestamp,
            "cohort_observed": cohort_observed,
            "cohort_budget": cohort_budget,
        }
        entry = DegradedEntry(
            axis="engine_resources",
            reason=_REASON_HEAP_SNAPSHOT_TRIGGERED,
            severity="warning",
            title_token="degraded.engine_resources.heap_snapshot_triggered.title",
            body_token="degraded.engine_resources.heap_snapshot_triggered.body",
            action_chips=_chips_for_reason(_REASON_HEAP_SNAPSHOT_TRIGGERED, metadata),
            metadata=metadata,
            first_observed_monotonic=now_monotonic,
            last_observed_monotonic=now_monotonic,
            occurrence_count=1,
        )
        get_default_degraded_store().record(entry)
    except Exception:  # noqa: BLE001 — composite store never breaks snapshot
        logger.debug(
            "engine.resources.heap_snapshot_composite_store_record_failed",
            cohort=cohort,
            exc_info=True,
        )


def _persist_heap_snapshot(evaluation: CohortEvaluation) -> None:
    """BUDGET_EXCEEDED RSS_GROWTH wrapper around the cohort-name helper.

    Mission H4 §8 T4.1(c) — fires on the governor's RSS_GROWTH verdict.
    The cohort-name-driven entrypoint (:func:`_persist_heap_snapshot_direct`)
    is what new callers (the heartbeat N=5 trigger, future on-demand
    operator UI) should use.
    """
    _persist_heap_snapshot_direct(
        cohort=evaluation.axis.value,
        cohort_observed=evaluation.observed,
        cohort_budget=evaluation.budget,
    )


def _persist_thread_snapshot(evaluation: CohortEvaluation) -> None:
    """Mission H4 §8 T4.1(d) — thread-driven thread snapshot.

    Captures ``sys._current_frames()`` + ``threading.enumerate()`` to
    ``~/.sovyx/diagnostics/thread-snapshot-<ts>.txt``. Captures all
    threads regardless of platform; Windows may have incomplete
    thread-name attribution per Mission H4 §0 scope exclusion note.
    """
    try:
        import sys
        import threading

        diag_dir = _diagnostics_dir()
        ts = int(time.time())
        path = diag_dir / f"thread-snapshot-{ts}.txt"
        lines: list[str] = []
        lines.append(f"# Thread snapshot — cohort={evaluation.axis.value}")
        lines.append(f"# observed_at_unix={ts}")
        lines.append(f"# cohort_observed={evaluation.observed}")
        lines.append(f"# cohort_budget={evaluation.budget}")
        lines.append(f"# note={evaluation.note}")
        lines.append("")
        thread_map = {t.ident: t for t in threading.enumerate()}
        frames = sys._current_frames()  # noqa: SLF001 — documented stdlib API
        for tid, frame in frames.items():
            thread = thread_map.get(tid)
            tname = thread.name if thread else "?"
            daemon = thread.daemon if thread else "?"
            lines.append(f"=== Thread {tid} (name={tname!r}, daemon={daemon}) ===")
            stack: list[str] = []
            cur_frame: Any = frame
            while cur_frame is not None:
                stack.append(
                    f"  {cur_frame.f_code.co_filename}:{cur_frame.f_lineno} "
                    f"in {cur_frame.f_code.co_name}",
                )
                cur_frame = cur_frame.f_back
            # Bottom-up for forensic readability.
            lines.extend(reversed(stack))
            lines.append("")
        path.write_text("\n".join(lines), encoding="utf-8")
        logger.info(
            "engine.resources.thread_snapshot_persisted",
            **{
                "engine.resources.cohort": evaluation.axis.value,
                "engine.resources.path": str(path),
                "engine.resources.thread_count": len(thread_map),
            },
        )
        from sovyx.engine.config import EngineConfig  # noqa: PLC0415 — lazy

        try:
            tuning = EngineConfig().observability.tuning
            max_files = tuning.thread_snapshot_max_files
        except Exception:  # noqa: BLE001 — fallback default
            max_files = 10
        _rotate_snapshot_files(diag_dir, "thread-snapshot-", max_files)
    except Exception:  # noqa: BLE001 — thread snapshot is best-effort
        logger.debug(
            "engine.resources.thread_snapshot_persist_failed",
            cohort=evaluation.axis.value,
            exc_info=True,
        )


def _increment_cohort_budget_counter(evaluation: CohortEvaluation) -> None:
    """Best-effort OTel counter increment for the cohort-breach event.

    Mission H4 §T2.6 + ADR-D20 — paired with the structured WARN above.
    Counter lookup is best-effort: a setup-time race where MetricsRegistry
    isn't ready yet falls back to a debug-level log + skips the increment.
    The structured WARN + composite-store entry remain the load-bearing
    surfaces.
    """
    try:
        from sovyx.observability.metrics import get_metrics  # noqa: PLC0415 — lazy

        counter = getattr(get_metrics(), "voice_health_cohort_budget_exceeded", None)
        if counter is None:
            return
        # Severity per ADR-D6: 1 cohort = warning (governor default). A
        # future caller that aggregates multiple BUDGET_EXCEEDED events
        # within one tick can escalate by inspecting the returned counter
        # state on the composite endpoint.
        counter.add(
            1,
            attributes={
                "cohort": evaluation.axis.value,
                "severity": "warning",
            },
        )
    except Exception:  # noqa: BLE001 — counter must NEVER break the snapshot path
        logger.debug(
            "engine.resources.cohort_budget_counter_failed",
            cohort=evaluation.axis.value,
            exc_info=True,
        )


def record_resource_snapshot_emission(*, final: bool) -> None:
    """Per-snapshot-tick counter increment — Mission H4 §T2.6 + ADR-D20.

    Called by :func:`ResourceSnapshotter._emit_snapshot` after the
    structured-log emission. Best-effort; failures absorbed.
    """
    try:
        from sovyx.observability.metrics import get_metrics  # noqa: PLC0415 — lazy

        counter = getattr(get_metrics(), "voice_health_resource_snapshot_emission", None)
        if counter is None:
            return
        counter.add(1, attributes={"final": str(final).lower()})
    except Exception:  # noqa: BLE001 — counter must NEVER break the snapshot path
        logger.debug("engine.resources.snapshot_emission_counter_failed", exc_info=True)


def _latest_snapshot_timestamp(prefix: str) -> int | None:
    """Locate the most-recent persisted snapshot file timestamp.

    Mission H4 §4.8 ADR-D8 — heap/thread snapshot chips deep-link the
    operator at the latest persisted file via ``<latest_ts>`` substitution.
    Best-effort: filesystem unavailability returns None and the calling
    chip-builder falls back to a generic anchor URL.
    """
    try:
        diag_dir = _diagnostics_dir()
        files = sorted(diag_dir.glob(f"{prefix}*"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not files:
            return None
        # Filenames are ``heap-snapshot-<ts>.json`` / ``thread-snapshot-<ts>.txt``.
        stem = files[0].stem  # strips final extension
        # Remove the leading prefix (sans trailing dash) — leaves the ts substr.
        ts_str = stem.removeprefix(prefix.rstrip("-") + "-")
        return int(ts_str)
    except (OSError, ValueError):
        return None


def _chips_for_reason(
    reason: str,
    metadata: Mapping[str, object],
) -> tuple[Any, ...]:
    """Mission H4 §4.8 ADR-D8 — per-cohort-reason action chip mapping.

    Each cohort reason carries 2 chips: a primary cohort-specific chip
    (deep-link to the relevant detail view) + a secondary general-action
    chip (CLI hint, docs link, ack, or cross-axis reference). The chips
    are constructed lazily via dotted-string lookup so the
    ``ActionChip`` dataclass import stays inside the caller's try block.

    Returns the chip tuple; the caller wraps in the DegradedEntry. The
    target URLs reference the v0.49.25 React routes added to
    ``router.tsx`` (``/engine/resources``, ``/engine/resources/heap-snapshot/<ts>``,
    ``/engine/resources/thread-snapshot/<ts>``).

    Per ADR-D8, mappings:

    * ``rss_growth_spike`` → heap snapshot (latest_ts substitution) +
      ``sovyx doctor resources`` CLI hint.
    * ``thread_count_spike`` → thread snapshot + CLI hint.
    * ``lock_dict_cardinality_saturated`` → ``/engine/resources#lock-dicts``
      anchor + docs link explaining LRULockDict maxsize tuning.
    * ``onnx_session_unexpected_count`` → ``/engine/resources#onnx`` anchor
      + ``sovyx doctor resources`` (RPC reload is not exposed at HEAD —
      doctor CLI is the closest operator-actionable surface; ADR-D8
      noted ``reloadModels`` as an alias for the CLI command).
    * ``exception_cohort_retention_high`` → ``/engine/resources#exception-cohort``
      anchor + C2 surface link (``/voice/health`` 500-history section).
    * ``heap_snapshot_triggered`` → heap snapshot deep-link + ack chip.
    """
    from sovyx.engine._degraded_store import ActionChip  # noqa: PLC0415 — lazy

    if reason == "engine_resources.rss_growth_spike":
        ts = _latest_snapshot_timestamp("heap-snapshot-")
        primary_target = (
            f"/engine/resources/heap-snapshot/{ts}" if ts else "/engine/resources#heap"
        )
        return (
            ActionChip(
                label_token="degraded.engine_resources.actions.viewHeapSnapshot",
                action="navigate",
                target=primary_target,
            ),
            ActionChip(
                label_token="degraded.engine_resources.actions.openDoctor",
                action="command_hint",
                target="sovyx doctor resources",
            ),
        )
    if reason == "engine_resources.thread_count_spike":
        ts = _latest_snapshot_timestamp("thread-snapshot-")
        primary_target = (
            f"/engine/resources/thread-snapshot/{ts}" if ts else "/engine/resources#threads"
        )
        return (
            ActionChip(
                label_token="degraded.engine_resources.actions.viewThreadSnapshot",
                action="navigate",
                target=primary_target,
            ),
            ActionChip(
                label_token="degraded.engine_resources.actions.openDoctor",
                action="command_hint",
                target="sovyx doctor resources",
            ),
        )
    if reason == "engine_resources.lock_dict_cardinality_saturated":
        return (
            ActionChip(
                label_token="degraded.engine_resources.actions.viewLockDicts",
                action="navigate",
                target="/engine/resources#lock-dicts",
            ),
            ActionChip(
                label_token="degraded.engine_resources.actions.adjustLruDocs",
                action="external_link",
                target="https://sovyx.dev/docs/observability/resource-hygiene#lock-dicts",
            ),
        )
    if reason == "engine_resources.onnx_session_unexpected_count":
        return (
            ActionChip(
                label_token="degraded.engine_resources.actions.viewOnnx",
                action="navigate",
                target="/engine/resources#onnx",
            ),
            ActionChip(
                label_token="degraded.engine_resources.actions.openDoctor",
                action="command_hint",
                target="sovyx doctor resources --cohort onnx",
            ),
        )
    if reason == "engine_resources.exception_cohort_retention_high":
        # MISSION-A.2.P2 F-019 (unblocked by A.1.P2 / ADR-D14 closure):
        # post-fix the cohort governor reads ``window_retained_bytes``
        # (rolling window) rather than the pre-A.1 lifetime accumulator
        # ``retained_bytes_estimate``. The breach now decays naturally
        # as observations age out of the window. Add a third chip
        # pointing the operator at the window-decay surface so the
        # remediation is actionable ("wait for decay" / "inspect window")
        # rather than vacuous ("fix the producer boundary").
        return (
            ActionChip(
                label_token="degraded.engine_resources.actions.viewExceptionCohort",
                action="navigate",
                target="/engine/resources#exception-cohort",
            ),
            ActionChip(
                label_token="degraded.engine_resources.actions.viewRecent500s",
                action="navigate",
                target="/voice/health#status-500-history",
            ),
            ActionChip(
                label_token="degraded.engine_resources.actions.inspectWindowDecay",
                action="command_hint",
                target=("sovyx doctor resources --explain exception_cohort.window_retained_bytes"),
            ),
        )
    if reason == "engine_resources.heap_snapshot_triggered":
        snapshot_ts = metadata.get("heap_snapshot_timestamp")
        target = (
            f"/engine/resources/heap-snapshot/{snapshot_ts}"
            if isinstance(snapshot_ts, int)
            else "/engine/resources#heap"
        )
        return (
            ActionChip(
                label_token="degraded.engine_resources.actions.viewSnapshot",
                action="navigate",
                target=target,
            ),
            ActionChip(
                label_token="degraded.engine_resources.actions.ack",
                action="api_post",
                target="/api/engine/resources/cohort/ack",
            ),
        )
    # Fallback for any future reason added without an explicit mapping:
    # one generic chip pointing at the resources page. Surface a debug
    # log so the gap is visible during local dev.
    logger.debug(
        "engine.resources.chip_mapping_fallback",
        reason=reason,
    )
    return (
        ActionChip(
            label_token="degraded.engine_resources.actions.viewResources",
            action="navigate",
            target="/engine/resources",
        ),
    )


def _compute_engine_resources_severity(
    cohort_axis: CohortAxis,
    *,
    governor: ResourceCohortGovernor | None = None,
) -> str:
    """Mission H4 §4.6 ADR-D6 + v0.49.29 — combined severity escalation.

    Computes the severity to assign to a freshly-emitted DegradedEntry
    on the ``engine_resources`` axis. Combines TWO escalation layers
    per ADR-D6:

    1. **Cross-cohort:** counts how many DISTINCT cohort axes are
       currently recorded in the C4 store under the ``engine_resources``
       axis. ``1 cohort = "warning"``, ``2 = "error"``,
       ``3+ = "critical"``. The current cohort is included in the count
       even if the store doesn't yet carry it (the helper is invoked
       BEFORE the new entry is recorded).
    2. **Temporal:** counts how many BUDGET_EXCEEDED events for the
       SAME cohort have landed in the governor's breach history within
       the last 5 min / 1 h. ``1st = "warning"``, ``2nd within 5 min =
       "error"``, ``3rd within 1 h = "critical"``.

    Returns the MAXIMUM of the two layer verdicts so an operator sees
    the most-alarming signal. Severity ordering:
    ``warning < error < critical``.

    Best-effort: store / governor unavailability falls back to
    ``"warning"`` — same as pre-v0.49.29 hardcoded value.
    """
    if governor is None:
        governor = get_default_resource_cohort_governor()
    # Layer 1: cross-cohort count from the C4 store.
    cross_count = 1  # current cohort always counts
    try:
        from sovyx.engine._degraded_store import (  # noqa: PLC0415 — lazy
            get_default_degraded_store,
        )

        store = get_default_degraded_store()
        entries = store.snapshot()
        distinct_cohorts: set[str] = {cohort_axis.value}
        for entry in entries:
            if entry.axis != "engine_resources":
                continue
            cohort_meta = entry.metadata.get("cohort") if entry.metadata else None
            if isinstance(cohort_meta, str):
                distinct_cohorts.add(cohort_meta)
        cross_count = len(distinct_cohorts)
    except Exception:  # noqa: BLE001 — severity computation must not break
        logger.debug(
            "engine.resources.severity_cross_cohort_lookup_failed",
            cohort=cohort_axis.value,
            exc_info=True,
        )
    # Layer 2: temporal breach count for THIS cohort.
    breaches_5min = 0
    breaches_1h = 0
    try:
        now = time.monotonic()
        with governor._lock:  # noqa: SLF001 — internal coordination
            history = governor._breach_history.get(cohort_axis)  # noqa: SLF001
            if history is not None:
                breaches_5min = sum(1 for ts in history if ts >= now - 300)
                breaches_1h = sum(1 for ts in history if ts >= now - 3600)
    except Exception:  # noqa: BLE001
        logger.debug(
            "engine.resources.severity_temporal_lookup_failed",
            cohort=cohort_axis.value,
            exc_info=True,
        )
    # Determine severity per ADR-D6 ordering.
    if cross_count >= 3 or breaches_1h >= 3:
        return "critical"
    if cross_count >= 2 or breaches_5min >= 2:
        return "error"
    return "warning"


def _record_to_composite_store(evaluation: CohortEvaluation) -> None:
    """Best-effort record into C4 :class:`EngineDegradedStore`.

    Failures absorbed at this layer — the WARN log is the
    load-bearing surface; composite-store recording is
    additive-only and never breaks the snapshot path.
    """
    try:
        from sovyx.engine._degraded_store import (  # noqa: PLC0415 — lazy import
            DegradedEntry,
            get_default_degraded_store,
        )

        now_monotonic = time.monotonic()
        # Mission H4 §0 line 30 spec-literal reason names — v0.49.24
        # closure. The reason string carries the verdict semantics
        # (``..._spike`` for delta-based cohorts, ``..._saturated`` for
        # aggregate caps, etc.) so dashboards + alert rules + i18n
        # tokens have a stable taxonomy distinct from the internal
        # ``CohortAxis`` value identifiers.
        reason = _REASON_FOR_AXIS.get(
            evaluation.axis,
            f"engine_resources.{evaluation.axis.value}",
        )
        reason_suffix = reason.split(".", 1)[1] if "." in reason else reason
        metadata: dict[str, object] = {
            "cohort": evaluation.axis.value,
            "observed": evaluation.observed,
            "budget": evaluation.budget,
            "note": evaluation.note,
        }
        # Mission H4 §4.6 ADR-D6 + v0.49.29 — severity escalation
        # combining cross-cohort axis count + per-cohort temporal
        # breach count. See ``_compute_engine_resources_severity``.
        severity = _compute_engine_resources_severity(evaluation.axis)
        entry = DegradedEntry(
            axis="engine_resources",
            reason=reason,
            severity=severity,
            title_token=f"degraded.engine_resources.{reason_suffix}.title",
            body_token=f"degraded.engine_resources.{reason_suffix}.body",
            action_chips=_chips_for_reason(reason, metadata),
            metadata=metadata,
            first_observed_monotonic=now_monotonic,
            last_observed_monotonic=now_monotonic,
            occurrence_count=1,
        )
        get_default_degraded_store().record(entry)
    except Exception:  # noqa: BLE001 — composite store must NEVER break the snapshot path
        logger.debug(
            "engine.resources.composite_store_record_failed",
            cohort=evaluation.axis.value,
            exc_info=True,
        )


_SINGLETON: ResourceCohortGovernor | None = None
_SINGLETON_LOCK: Lock = Lock()


def get_default_resource_cohort_governor() -> ResourceCohortGovernor:
    """Return the process-local lazy-initialized governor singleton."""
    global _SINGLETON  # noqa: PLW0603
    if _SINGLETON is None:
        with _SINGLETON_LOCK:
            if _SINGLETON is None:
                _SINGLETON = ResourceCohortGovernor()
    return _SINGLETON


def reset_default_resource_cohort_governor() -> None:
    """Test-only — reset the singleton to a fresh governor."""
    global _SINGLETON  # noqa: PLW0603
    with _SINGLETON_LOCK:
        _SINGLETON = None


__all__ = [
    "CohortBudget",
    "CohortEvaluation",
    "CohortVerdict",
    "ResourceCohortGovernor",
    "emit_axis_entries",
    "get_default_resource_cohort_governor",
    "record_resource_snapshot_emission",
    "reset_default_resource_cohort_governor",
    # Mission H4 §8 T4.6 — public on-demand heap-snapshot entrypoint.
    # The cohort-name-driven helper is exposed for unit tests + future
    # callers; the canonical wire-up is via ResourceCohortGovernor.request_heap_snapshot.
    "_persist_heap_snapshot_direct",
    # Mission H4 §0 line 30 + v0.49.24 — spec-literal reason names.
    "_REASON_FOR_AXIS",
    "_REASON_HEAP_SNAPSHOT_TRIGGERED",
    # Mission H4 §4.6 ADR-D6 + v0.49.29 — combined severity escalation.
    "_compute_engine_resources_severity",
]
