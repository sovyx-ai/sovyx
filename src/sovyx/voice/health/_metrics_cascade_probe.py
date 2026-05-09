"""Cascade + ComboStore + probe metrics — ADR §5.8 cascade-layer recorders.

Phase 5.F.9 god-file extraction from ``voice/health/_metrics.py``
(anti-pattern #16). Owns the cascade-execution observability surface
— 6 record helpers + 5 metric-name constants spanning:

* **Cascade attempts** — ADR §5.8: per-attempt outcome with
  pinned/store/cascade source attribution. Mirrors to the L9 anonymous
  opt-in telemetry rollup so fleet-wide cascade-success rates land
  on the same wire shape regardless of opt-in state.
* **ComboStore fast-path** — hit / miss / needs_revalidation
  resolution + per-rule invalidation tag.
* **Probe diagnosis + duration** — paired counter+histogram for
  every probe run, plus the convenience :func:`record_probe_result`
  wrapper that emits both from a single :class:`ProbeResult`.

All recorders are thin façades over
:class:`sovyx.observability.metrics.MetricsRegistry` instruments.
:func:`record_cascade_attempt` is the only one with a side-effect
beyond the OTel emit (the L9 telemetry mirror call).

Anti-pattern #20 covered: parent module
``voice/health/_metrics.py`` re-exports every symbol so production
callers (cascade executor + ComboStore + probe dispatch) and test
references at the original ``sovyx.voice.health._metrics.<name>``
path continue to resolve via standard module-namespace lookup.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, TypeAlias

from sovyx.observability.metrics import get_metrics
from sovyx.voice.health._telemetry import (
    record_cascade_outcome as _record_cascade_outcome_telemetry,
)

if TYPE_CHECKING:
    from sovyx.voice.health.contract import (
        Diagnosis,
        ProbeMode,
        ProbeResult,
    )


# ── Stable name constants (cascade + probe + ComboStore) ────────────
# OTel instrument names. Tests assert on these so a typo in the
# registry definition fails loudly. Names are immutable wire contracts
# — any rename is a breaking change for downstream dashboards.

METRIC_CASCADE_ATTEMPTS = "sovyx.voice.health.cascade.attempts"
METRIC_COMBO_STORE_HITS = "sovyx.voice.health.combo_store.hits"
METRIC_COMBO_STORE_INVALIDATIONS = "sovyx.voice.health.combo_store.invalidations"
METRIC_PROBE_DIAGNOSIS = "sovyx.voice.health.probe.diagnosis"
METRIC_PROBE_DURATION = "sovyx.voice.health.probe.duration"


# ── Label enums (closed sets for low-cardinality guarantees) ────────

CascadeSource: TypeAlias = str  # "pinned" | "store" | "cascade"
ComboStoreResult: TypeAlias = str  # "hit" | "miss" | "needs_revalidation"


# ── Record helpers ──────────────────────────────────────────────────


def record_cascade_attempt(
    *,
    platform: str,
    host_api: str,
    success: bool,
    source: CascadeSource,
) -> None:
    """Record a single cascade attempt outcome (ADR §5.8).

    Args:
        platform: "win32" | "linux" | "darwin" — the cascade being run.
        host_api: "WASAPI" | "WDM-KS" | "ALSA" | "CoreAudio" | ... — the
            host API of the attempted combo. Unknown/missing → "unknown".
        success: True iff the probe returned ``Diagnosis.HEALTHY``.
        source: "pinned" (user override), "store" (fast path),
            "cascade" (platform table walk).
    """
    get_metrics().voice_health_cascade_attempts.add(
        1,
        attributes={
            "platform": platform,
            "host_api": host_api or "unknown",
            "success": "true" if success else "false",
            "source": source,
        },
    )
    # L9 (ADR §4.9): forward to the anonymous opt-in rollup. The
    # telemetry facade is a no-op when ``EngineConfig.telemetry.enabled``
    # is False so this stays free on the hot path.
    _record_cascade_outcome_telemetry(
        platform=platform,
        host_api=host_api,
        success=success,
    )


def record_combo_store_hit(*, endpoint_class: str, result: ComboStoreResult) -> None:
    """Record a ComboStore fast-path resolution.

    Args:
        endpoint_class: Device class bucket — "Audio", "Headset",
            "USB", etc. Low cardinality by design.
        result: "hit" (fast path taken + HEALTHY), "miss" (no stored
            entry), "needs_revalidation" (entry present but stale).
    """
    get_metrics().voice_health_combo_store_hits.add(
        1,
        attributes={
            "endpoint_class": endpoint_class or "unknown",
            "result": result,
        },
    )


def record_combo_store_invalidation(*, reason: str) -> None:
    """Record a ComboStore invalidation event.

    Args:
        reason: A §4.1 rule tag — "fast_path_probe_failed",
            "fx_properties_mismatch", "fingerprint_drift", etc.
    """
    get_metrics().voice_health_combo_store_invalidations.add(
        1,
        attributes={"reason": reason or "unknown"},
    )


def record_probe_diagnosis(*, diagnosis: Diagnosis | str, mode: ProbeMode | str) -> None:
    """Record the final diagnosis of a probe run."""
    get_metrics().voice_health_probe_diagnosis.add(
        1,
        attributes={
            "diagnosis": str(diagnosis),
            "mode": str(mode),
        },
    )


def record_probe_duration(*, duration_ms: float, mode: ProbeMode | str) -> None:
    """Record the wall-clock duration of a probe run."""
    get_metrics().voice_health_probe_duration.record(
        duration_ms,
        attributes={"mode": str(mode)},
    )


def record_probe_result(probe_result: ProbeResult) -> None:
    """Convenience wrapper that emits both probe metrics from one result."""
    record_probe_diagnosis(diagnosis=probe_result.diagnosis, mode=probe_result.mode)
    record_probe_duration(duration_ms=probe_result.duration_ms, mode=probe_result.mode)


__all__ = [
    "METRIC_CASCADE_ATTEMPTS",
    "METRIC_COMBO_STORE_HITS",
    "METRIC_COMBO_STORE_INVALIDATIONS",
    "METRIC_PROBE_DIAGNOSIS",
    "METRIC_PROBE_DURATION",
    "CascadeSource",
    "ComboStoreResult",
    "record_cascade_attempt",
    "record_combo_store_hit",
    "record_combo_store_invalidation",
    "record_probe_diagnosis",
    "record_probe_duration",
    "record_probe_result",
]
