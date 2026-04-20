"""Voice Capture Health — §5.8 metrics facade.

The instrument definitions live on :class:`sovyx.observability.metrics.MetricsRegistry`
(single source of truth for all OpenTelemetry instruments). This module
provides ergonomic record helpers so call sites don't have to repeat
label dicts or remember the full instrument-attribute surface.

The stable metric name table (ADR §5.8):

.. list-table::
   :header-rows: 1

   * - Metric
     - Type
     - Labels
   * - ``sovyx.voice.health.cascade.attempts``
     - counter
     - platform, host_api, success, source
   * - ``sovyx.voice.health.combo_store.hits``
     - counter
     - endpoint_class, result
   * - ``sovyx.voice.health.combo_store.invalidations``
     - counter
     - reason
   * - ``sovyx.voice.health.probe.diagnosis``
     - counter
     - diagnosis, mode
   * - ``sovyx.voice.health.probe.duration``
     - histogram (ms)
     - mode
   * - ``sovyx.voice.health.preflight.failures``
     - counter
     - step, code
   * - ``sovyx.voice.health.recovery.attempts``
     - counter
     - trigger
   * - ``sovyx.voice.health.self_feedback.blocks``
     - counter
     - layer
   * - ``sovyx.voice.health.active_endpoint.changes``
     - counter
     - reason
   * - ``sovyx.voice.health.time_to_first_utterance``
     - histogram (ms)
     - —

Names are immutable — any rename is a breaking contract change for
downstream dashboards (Grafana, Loki queries, Prometheus alerts).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

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
    from sovyx.voice.health.preflight import PreflightStep, PreflightStepCode


# ── Stable name constants (test-friendly) ────────────────────────────────
# The OTel instrument names. Tests assert on these so a typo in the
# registry definition fails loudly.

METRIC_CASCADE_ATTEMPTS = "sovyx.voice.health.cascade.attempts"
METRIC_COMBO_STORE_HITS = "sovyx.voice.health.combo_store.hits"
METRIC_COMBO_STORE_INVALIDATIONS = "sovyx.voice.health.combo_store.invalidations"
METRIC_PROBE_DIAGNOSIS = "sovyx.voice.health.probe.diagnosis"
METRIC_PROBE_DURATION = "sovyx.voice.health.probe.duration"
METRIC_PREFLIGHT_FAILURES = "sovyx.voice.health.preflight.failures"
METRIC_RECOVERY_ATTEMPTS = "sovyx.voice.health.recovery.attempts"
METRIC_SELF_FEEDBACK_BLOCKS = "sovyx.voice.health.self_feedback.blocks"
METRIC_ACTIVE_ENDPOINT_CHANGES = "sovyx.voice.health.active_endpoint.changes"
METRIC_TIME_TO_FIRST_UTTERANCE = "sovyx.voice.health.time_to_first_utterance"
METRIC_KERNEL_INVALIDATED_EVENTS = "sovyx.voice.health.kernel_invalidated.events"
METRIC_PROBE_START_TIME_ERRORS = "sovyx.voice.probe.start_time_errors"


# ── Label enums (closed sets for low-cardinality guarantees) ─────────────
# Using string literals here keeps the module dependency-free; the ADR
# table is the authoritative reference.

CascadeSource = str  # "pinned" | "store" | "cascade"
ComboStoreResult = str  # "hit" | "miss" | "needs_revalidation"
RecoveryTrigger = str  # "deaf_backoff" | "hotplug" | "default_change" | "power" | "audio_service"
SelfFeedbackLayer = str  # "gate" | "duck" | "spectral"
EndpointChangeReason = str  # "hotplug" | "default" | "manual" | "recovery"


# ── Record helpers ───────────────────────────────────────────────────────


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


def record_preflight_failure(
    *,
    step: PreflightStep | str,
    code: PreflightStepCode | str,
) -> None:
    """Record a pre-flight step failure."""
    get_metrics().voice_health_preflight_failures.add(
        1,
        attributes={
            "step": str(step),
            "code": str(code),
        },
    )


def record_recovery_attempt(*, trigger: RecoveryTrigger) -> None:
    """Record a watchdog recovery trigger."""
    get_metrics().voice_health_recovery_attempts.add(
        1,
        attributes={"trigger": trigger},
    )


def record_self_feedback_block(*, layer: SelfFeedbackLayer) -> None:
    """Record a self-feedback isolation block."""
    get_metrics().voice_health_self_feedback_blocks.add(
        1,
        attributes={"layer": layer},
    )


def record_active_endpoint_change(*, reason: EndpointChangeReason) -> None:
    """Record an active-endpoint swap (ADR §5.8)."""
    get_metrics().voice_health_active_endpoint_changes.add(
        1,
        attributes={"reason": reason},
    )


def record_kernel_invalidated_event(
    *,
    platform: str,
    host_api: str,
    action: str,
) -> None:
    """Record a §4.4.7 kernel-invalidated lifecycle event.

    Args:
        platform: ``"win32"`` | ``"linux"`` | ``"darwin"``.
        host_api: Host API of the probe that hit the invalidated state.
            ``"unknown"`` when the event is not bound to a single combo
            (recheck loops, hot-plug clears).
        action: ``"quarantine"`` on initial add, ``"failover"`` when
            :mod:`sovyx.voice.health._factory_integration` picks a
            different endpoint, ``"recheck_recovered"`` when a periodic
            retry comes back healthy, ``"recheck_still_invalid"`` when
            the retry is still stuck, ``"hotplug_clear"`` when a replug
            clears quarantine.
    """
    counter = getattr(get_metrics(), "voice_health_kernel_invalidated_events", None)
    if counter is None:
        return
    counter.add(
        1,
        attributes={
            "platform": platform or "unknown",
            "host_api": host_api or "unknown",
            "action": action,
        },
    )


def record_start_time_error(
    *,
    diagnosis: Diagnosis | str,
    host_api: str,
    platform: str,
) -> None:
    """Record a probe ``stream.start()`` failure after a successful open.

    Before v0.20.2, start-time PortAudio errors (notably
    ``AUDCLNT_E_DEVICE_INVALIDATED`` and ``AUDCLNT_E_DEVICE_IN_USE``)
    propagated out of ``_run_probe`` and were synthesised into a
    generic ``DRIVER_ERROR`` at the cascade layer — fully bypassing the
    §4.4.7 fail-over decision. This counter is the telemetry signal we
    need to detect regressions of that gap.

    Args:
        diagnosis: The :class:`Diagnosis` the open-error classifier
            produced. Low-cardinality (closed enum).
        host_api: ``"WASAPI"`` | ``"WDM-KS"`` | ``"MME"`` | etc.
        platform: ``"win32"`` | ``"linux"`` | ``"darwin"``.
    """
    counter = getattr(get_metrics(), "voice_probe_start_time_errors", None)
    if counter is None:
        return
    counter.add(
        1,
        attributes={
            "diagnosis": str(diagnosis),
            "host_api": host_api or "unknown",
            "platform": platform or "unknown",
        },
    )


def record_time_to_first_utterance(*, duration_ms: float) -> None:
    """Record the user-perceived KPI (ADR §5.14).

    Latency from ``WakeWordDetectedEvent`` to ``SpeechStartedEvent``.
    Target p95 ≤ 200 ms; regression alert at > 500 ms.
    """
    get_metrics().voice_health_time_to_first_utterance.record(duration_ms)


__all__ = [
    "METRIC_ACTIVE_ENDPOINT_CHANGES",
    "METRIC_CASCADE_ATTEMPTS",
    "METRIC_COMBO_STORE_HITS",
    "METRIC_COMBO_STORE_INVALIDATIONS",
    "METRIC_PREFLIGHT_FAILURES",
    "METRIC_PROBE_DIAGNOSIS",
    "METRIC_PROBE_DURATION",
    "METRIC_RECOVERY_ATTEMPTS",
    "METRIC_SELF_FEEDBACK_BLOCKS",
    "METRIC_KERNEL_INVALIDATED_EVENTS",
    "METRIC_PROBE_START_TIME_ERRORS",
    "METRIC_TIME_TO_FIRST_UTTERANCE",
    "record_active_endpoint_change",
    "record_cascade_attempt",
    "record_combo_store_hit",
    "record_combo_store_invalidation",
    "record_kernel_invalidated_event",
    "record_preflight_failure",
    "record_probe_diagnosis",
    "record_probe_duration",
    "record_probe_result",
    "record_recovery_attempt",
    "record_self_feedback_block",
    "record_start_time_error",
    "record_time_to_first_utterance",
]
