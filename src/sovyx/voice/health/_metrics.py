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
from sovyx.voice.health import _bypass_tier_state

if TYPE_CHECKING:
    from sovyx.voice.health.contract import (
        Diagnosis,
    )
    from sovyx.voice.health.preflight import PreflightStep, PreflightStepCode


# ── Stable name constants (test-friendly) ────────────────────────────────
# The OTel instrument names. Tests assert on these so a typo in the
# registry definition fails loudly.

# Phase 5.F.9 — METRIC_CASCADE_ATTEMPTS / _COMBO_STORE_HITS /
# _COMBO_STORE_INVALIDATIONS / _PROBE_DIAGNOSIS / _PROBE_DURATION
# moved to ``_metrics_cascade_probe.py`` (re-exported below).
METRIC_PREFLIGHT_FAILURES = "sovyx.voice.health.preflight.failures"
METRIC_RECOVERY_ATTEMPTS = "sovyx.voice.health.recovery.attempts"
METRIC_SELF_FEEDBACK_BLOCKS = "sovyx.voice.health.self_feedback.blocks"
METRIC_ACTIVE_ENDPOINT_CHANGES = "sovyx.voice.health.active_endpoint.changes"
METRIC_TIME_TO_FIRST_UTTERANCE = "sovyx.voice.health.time_to_first_utterance"
METRIC_KERNEL_INVALIDATED_EVENTS = "sovyx.voice.health.kernel_invalidated.events"
METRIC_PROBE_START_TIME_ERRORS = "sovyx.voice.health.probe.start_time_errors"
METRIC_APO_DEGRADED_EVENTS = "sovyx.voice.health.apo_degraded.events"
METRIC_BYPASS_STRATEGY_VERDICTS = "sovyx.voice.health.bypass_strategy.verdicts"
METRIC_CAPTURE_INTEGRITY_VERDICTS = "sovyx.voice.health.capture_integrity.verdicts"
METRIC_BYPASS_PROBE_WAIT_MS = "sovyx.voice.health.bypass.probe_wait_ms"
METRIC_BYPASS_PROBE_WINDOW_CONTAMINATED = "sovyx.voice.health.bypass.probe_window_contaminated"
METRIC_BYPASS_IMPROVEMENT_RESOLUTION = "sovyx.voice.health.bypass.improvement_resolution"

# Phase 5.F.6 god-file split — Voice Windows Paranoid Mission counter
# vocabulary (v0.24.0 → v0.26.0; cold-silence + Tier 1/2 bypass +
# opener alignment + hotplug listener) lives in
# :mod:`sovyx.voice.health._metrics_bypass_tier`. Re-exported below
# (after the function definitions block) so the original
# ``sovyx.voice.health._metrics.<name>`` import path stays stable.

# Phase 5.F.4 god-file split — Phase 4 audio-quality metrics
# (13 record helpers + 13 metric-name constants, ~300 LOC) live in
# :mod:`sovyx.voice.health._metrics_audio_quality`. Re-exported below
# (after function definitions) so all production callers + test
# references continue to resolve via the original
# ``sovyx.voice.health._metrics.<name>`` path.


# ── Label enums (closed sets for low-cardinality guarantees) ─────────────
# Using string literals here keeps the module dependency-free; the ADR
# table is the authoritative reference.

# Phase 5.F.9 — CascadeSource + ComboStoreResult moved to
# ``_metrics_cascade_probe.py`` (re-exported below).
RecoveryTrigger = str  # "deaf_backoff" | "hotplug" | "default_change" | "power" | "audio_service"
SelfFeedbackLayer = str  # "gate" | "duck" | "spectral"
EndpointChangeReason = str  # "hotplug" | "default" | "manual" | "recovery"


# ── Record helpers ───────────────────────────────────────────────────────


# Phase 5.F.9 god-file split — cascade + ComboStore + probe metrics
# (6 record helpers + 5 metric-name constants + 2 type aliases) extracted
# to _metrics_cascade_probe.py. Re-exported here so the original
# sovyx.voice.health._metrics.<name> import path stays stable.
from sovyx.voice.health._metrics_cascade_probe import (  # noqa: E402  F401
    METRIC_CASCADE_ATTEMPTS,
    METRIC_COMBO_STORE_HITS,
    METRIC_COMBO_STORE_INVALIDATIONS,
    METRIC_PROBE_DIAGNOSIS,
    METRIC_PROBE_DURATION,
    CascadeSource,
    ComboStoreResult,
    record_cascade_attempt,
    record_combo_store_hit,
    record_combo_store_invalidation,
    record_probe_diagnosis,
    record_probe_duration,
    record_probe_result,
)


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


def record_opener_attempt(
    *,
    host_api: str,
    error_code: str | None,
) -> None:
    """Record one pyramid attempt in :func:`~sovyx.voice._stream_opener.open_input_stream`.

    Introduced by ``voice-linux-cascade-root-fix`` T1 so opener-level
    fallbacks are observable alongside cascade-level attempts. One call
    per ``_try_open_input`` invocation — emitted after the attempt is
    classified, regardless of whether it succeeded.

    Args:
        host_api: PortAudio host-API name of the attempted device. Empty
            / missing → ``"unknown"``.
        error_code: ``None`` for success; otherwise the
            :class:`~sovyx.voice.device_test._protocol.ErrorCode` string
            value (``"device_busy"``, ``"unsupported_samplerate"``, ...).
    """
    counter = getattr(get_metrics(), "voice_opener_attempts", None)
    if counter is None:
        return
    counter.add(
        1,
        attributes={
            "host_api": host_api or "unknown",
            "error_code": error_code or "none",
            "result": "ok" if error_code is None else "fail",
        },
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


def record_apo_degraded_event(
    *,
    platform: str,
    action: str,
) -> None:
    """Record an APO_DEGRADED lifecycle event (ADR §4.1 / §5.8.APO).

    Mirrors :func:`record_kernel_invalidated_event` but scoped to the
    OS-agnostic APO-cluster fail-over path (Windows Voice Clarity /
    VocaEffectPack, Linux ``module-echo-cancel``, CoreAudio VPIO, …).

    Args:
        platform: ``"win32"`` | ``"linux"`` | ``"darwin"``.
        action: ``"quarantine"`` on initial add,
            ``"failover"`` when :mod:`sovyx.voice.health._factory_integration`
            picks a different endpoint, ``"recheck_recovered"`` when the
            watchdog APO recheck loop clears quarantine on a HEALTHY
            re-probe, ``"recheck_still_invalid"`` when the recheck still
            fails, ``"hotplug_clear"`` when a replug retires the entry.
    """
    counter = getattr(get_metrics(), "voice_health_apo_degraded_events", None)
    if counter is None:
        return
    counter.add(
        1,
        attributes={
            "platform": platform or "unknown",
            "action": action,
        },
    )


def record_bypass_strategy_verdict(
    *,
    strategy: str,
    verdict: str,
    reason: str = "",
) -> None:
    """Record one per-strategy outcome from :class:`CaptureIntegrityCoordinator`.

    Args:
        strategy: Stable strategy identifier (``"win.wasapi_exclusive"``,
            ``"win.disable_sysfx"``, ``"linux.alsa_hw_direct"``,
            ``"macos.coreaudio_vpio_off"``).
        verdict: ``"applied_healthy"`` | ``"applied_still_dead"`` |
            ``"failed_to_apply"`` | ``"not_applicable"``. Matches the
            :class:`BypassVerdict` string values.
        reason: Optional low-cardinality tag — eligibility rejection
            reason (``"not_win32_platform"``) or apply-failure token
            (``"exclusive_downgraded_to_shared"``). Empty string when
            unset. Stable across minor versions so dashboards can
            key on it.
    """
    _bypass_tier_state.mark_strategy_verdict(strategy=strategy, verdict=verdict)
    counter = getattr(get_metrics(), "voice_health_bypass_strategy_verdicts", None)
    if counter is None:
        return
    counter.add(
        1,
        attributes={
            "strategy": strategy or "unknown",
            "verdict": verdict or "unknown",
            "reason": reason or "",
        },
    )


def record_bypass_probe_wait_ms(*, strategy: str, wait_ms: float) -> None:
    """Record the post-apply probe wait duration (v1.3 §14.E1).

    Fed from :meth:`CaptureIntegrityCoordinator.handle_deaf_signal` —
    measures the wall-clock span between ``strategy.apply()`` returning
    and :meth:`AudioCaptureTask.tap_frames_since_mark` yielding enough
    post-apply frames for classification.

    Args:
        strategy: Stable strategy identifier (same token used by
            :func:`record_bypass_strategy_verdict`).
        wait_ms: Observed duration in milliseconds. Clamped at zero so
            a negative clock skew does not poison the histogram.
    """
    histogram = getattr(get_metrics(), "voice_health_bypass_probe_wait_ms", None)
    if histogram is None:
        return
    histogram.record(
        max(0.0, float(wait_ms)),
        attributes={"strategy": strategy or "unknown"},
    )


def record_bypass_probe_window_contaminated(*, strategy: str) -> None:
    """Record a degraded-but-not-contaminated post-apply probe (v1.3 §14.E1).

    The tuple-mark design eliminates pre-apply frame leakage entirely;
    this counter instead fires when the coordinator had to classify
    *fewer* than ``min_samples`` post-apply frames because the tap
    timed out. Distinct signal from a failed apply — the fix was
    applied, but the verdict carries reduced statistical weight.
    """
    counter = getattr(get_metrics(), "voice_health_bypass_probe_window_contaminated", None)
    if counter is None:
        return
    counter.add(1, attributes={"strategy": strategy or "unknown"})


def record_bypass_improvement_resolution(*, strategy: str) -> None:
    """Record a v1.3 §14.E2 improvement-heuristic resolution.

    Fires when the post-apply verdict is ``VAD_MUTE`` yet the spectral
    rolloff improved by at least
    :attr:`VoiceTuningConfig.improvement_rolloff_factor` — the
    coordinator treats the attempt as resolved because the spectrum
    demonstrates the fix worked even though the user stopped speaking
    during settle. Label ``strategy`` matches
    :func:`record_bypass_strategy_verdict`.
    """
    counter = getattr(get_metrics(), "voice_health_bypass_improvement_resolution", None)
    if counter is None:
        return
    counter.add(1, attributes={"strategy": strategy or "unknown"})


def record_capture_integrity_verdict(
    *,
    verdict: str,
    phase: str,
) -> None:
    """Record a :class:`CaptureIntegrityProbe` classification.

    Args:
        verdict: :class:`IntegrityVerdict` value — ``"healthy"`` |
            ``"apo_degraded"`` | ``"driver_silent"`` | ``"vad_mute"`` |
            ``"inconclusive"``.
        phase: ``"pre_bypass"`` (coordinator probe before apply),
            ``"post_bypass"`` (coordinator probe after apply + settle),
            ``"recheck"`` (watchdog APO recheck loop). Low-cardinality.
    """
    counter = getattr(get_metrics(), "voice_health_capture_integrity_verdicts", None)
    if counter is None:
        return
    counter.add(
        1,
        attributes={
            "verdict": verdict or "unknown",
            "phase": phase or "unknown",
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
    counter = getattr(get_metrics(), "voice_health_probe_start_time_errors", None)
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


# Phase 5.F.5 — wake-word recorders + _bucket_score helper extracted to
# :mod:. Re-exported here so all
# production callers + tests at sovyx.voice.health._metrics.<name>
# continue to resolve via the parent module namespace.
from sovyx.voice.health._metrics_wake_word import (  # noqa: E402  F401
    _bucket_score,
    record_wake_word_confidence,
    record_wake_word_detection_method,
    record_wake_word_detection_ms,
    record_wake_word_false_fire,
    record_wake_word_fast_path_engaged,
    record_wake_word_resolution_strategy,
    record_wake_word_stage1_inference_ms,
    record_wake_word_stage2_collection_ms,
    record_wake_word_stage2_verifier_ms,
)

# ── Phase 7 / T7.27 / T7.28 — audio-error-translated counter ─────────


def record_audio_error_translated(*, error_class: str) -> None:
    """Increment the T7.27 / T7.28 audio-error-translated counter.

    Fires per call to
    :func:`sovyx.voice._error_messages.translate_audio_error`. The
    label is the ``AudioErrorClass`` value (closed-set, cardinality
    bounded by the enum).

    Operator dashboards aggregate the histogram for trend analysis:

    * ``permission_denied`` spike → TCC / Group Policy regression
      after an OS update.
    * ``device_in_use`` spike → operator installed a competing app
      (Zoom, Teams) that grabs exclusive mode.
    * Rising ``unknown`` ratio → translation table needs new
      entries for an OS update; surface as a tracked debt.

    Args:
        error_class: One of the :class:`AudioErrorClass` values
            (``device_not_found`` / ``device_in_use`` / ... /
            ``unknown``). Stable wire format.
    """
    get_metrics().voice_audio_error_translated.add(
        1,
        attributes={"class": error_class},
    )


# Phase 5.F.6 god-file split — bypass-tier recorders extracted to
# :mod:. Re-exported here so
# the original sovyx.voice.health._metrics.<name> import path
# stays stable.
from sovyx.voice.health._metrics_bypass_tier import (  # noqa: E402  F401
    METRIC_BYPASS_TIER1_RAW_ATTEMPTED,
    METRIC_BYPASS_TIER1_RAW_OUTCOME,
    METRIC_BYPASS_TIER2_HOST_API_ROTATE_ATTEMPTED,
    METRIC_BYPASS_TIER2_HOST_API_ROTATE_OUTCOME,
    METRIC_HOTPLUG_LISTENER_REGISTERED,
    METRIC_OPENER_HOST_API_ALIGNMENT,
    METRIC_PROBE_COLD_SILENCE_REJECTED,
    record_cold_silence_rejected,
    record_hotplug_listener_registered,
    record_opener_host_api_alignment,
    record_tier1_raw_attempted,
    record_tier1_raw_outcome,
    record_tier2_host_api_rotate_attempted,
    record_tier2_host_api_rotate_outcome,
)

# ── Phase 5 / T5.49 + new mission — driver-update detection ──────────


_DRIVER_UPDATE_ACTIONS: frozenset[str] = frozenset({"detected", "skipped", "triggered"})


def record_driver_update_detected(*, action: str) -> None:
    """Record an audio driver-modification event from the WMI listener.

    Counter dimension: ``action`` — one of ``detected`` (always
    emitted by the handler on first sight of the event),
    ``skipped`` (the recascade flag was False so the handler
    deliberately did NOT trigger a re-cascade), or ``triggered``
    (recascade flag was True; the handler emitted the would-
    trigger event — actual cascade re-run plumbing lives in a
    future commit per the new mission's Part 4.1).

    Operators correlate spikes in
    ``action=detected`` with ``voice.health.deaf.warnings_total``
    to attribute deaf-signal incidents to regressed driver
    releases (forensic value) rather than to Sovyx's bypass
    logic. ``action=skipped`` vs ``triggered`` lets dashboards
    track the operator's adoption of the recascade flag.

    Args:
        action: ``"detected"`` / ``"skipped"`` / ``"triggered"``.
            Other values are coerced to ``"unknown"`` to keep the
            label cardinality bounded.
    """
    if action not in _DRIVER_UPDATE_ACTIONS:
        action = "unknown"
    counter = getattr(get_metrics(), "voice_driver_update_detected", None)
    if counter is None:
        return
    counter.add(1, attributes={"action": action})


# Phase 5.F.4 — re-export the audio-quality recorders + constants here
# so the existing sovyx.voice.health._metrics.<name> import path stays
# stable for production callers + test references (anti-pattern #20).
from sovyx.voice.health._metrics_audio_quality import (  # noqa: E402  F401
    METRIC_AEC_BYPASS_COMBO,
    METRIC_AEC_DOUBLE_TALK,
    METRIC_AEC_ERLE_DB,
    METRIC_AEC_WINDOWS,
    METRIC_AUDIO_PHASE_INVERSION_RECOVERY,
    METRIC_AUDIO_RESAMPLE_PEAK_CLIP,
    METRIC_AUDIO_SIGNAL_DESTROYED,
    METRIC_AUDIO_SNR_DB,
    METRIC_NS_SUPPRESSION_DB,
    METRIC_NS_WINDOWS,
    METRIC_PIPELINE_NOISE_FLOOR_DRIFT_ALERTS,
    METRIC_PIPELINE_SNR_LOW_ALERTS,
    METRIC_VAD_QUIET_SIGNAL_GATED,
    record_aec_bypass_combo,
    record_aec_double_talk,
    record_aec_erle,
    record_aec_window,
    record_audio_phase_inversion_recovery,
    record_audio_resample_peak_clip,
    record_audio_signal_destroyed,
    record_audio_snr_db,
    record_noise_floor_drift_alert,
    record_ns_suppression_db,
    record_ns_window,
    record_snr_low_alert,
    record_vad_quiet_signal_gated,
)

__all__ = [
    "METRIC_ACTIVE_ENDPOINT_CHANGES",
    "METRIC_AEC_BYPASS_COMBO",
    "METRIC_AEC_DOUBLE_TALK",
    "METRIC_AEC_ERLE_DB",
    "METRIC_AEC_WINDOWS",
    "METRIC_APO_DEGRADED_EVENTS",
    "METRIC_AUDIO_PHASE_INVERSION_RECOVERY",
    "METRIC_AUDIO_RESAMPLE_PEAK_CLIP",
    "METRIC_AUDIO_SIGNAL_DESTROYED",
    "METRIC_AUDIO_SNR_DB",
    "METRIC_BYPASS_IMPROVEMENT_RESOLUTION",
    "METRIC_BYPASS_PROBE_WAIT_MS",
    "METRIC_BYPASS_PROBE_WINDOW_CONTAMINATED",
    "METRIC_BYPASS_STRATEGY_VERDICTS",
    "METRIC_BYPASS_TIER1_RAW_ATTEMPTED",
    "METRIC_BYPASS_TIER1_RAW_OUTCOME",
    "METRIC_BYPASS_TIER2_HOST_API_ROTATE_ATTEMPTED",
    "METRIC_BYPASS_TIER2_HOST_API_ROTATE_OUTCOME",
    "METRIC_CAPTURE_INTEGRITY_VERDICTS",
    "METRIC_CASCADE_ATTEMPTS",
    "METRIC_COMBO_STORE_HITS",
    "METRIC_COMBO_STORE_INVALIDATIONS",
    "METRIC_HOTPLUG_LISTENER_REGISTERED",
    "METRIC_KERNEL_INVALIDATED_EVENTS",
    "METRIC_NS_SUPPRESSION_DB",
    "METRIC_NS_WINDOWS",
    "METRIC_PIPELINE_NOISE_FLOOR_DRIFT_ALERTS",
    "METRIC_PIPELINE_SNR_LOW_ALERTS",
    "METRIC_OPENER_HOST_API_ALIGNMENT",
    "METRIC_PREFLIGHT_FAILURES",
    "METRIC_PROBE_COLD_SILENCE_REJECTED",
    "METRIC_PROBE_DIAGNOSIS",
    "METRIC_PROBE_DURATION",
    "METRIC_PROBE_START_TIME_ERRORS",
    "METRIC_RECOVERY_ATTEMPTS",
    "METRIC_SELF_FEEDBACK_BLOCKS",
    "METRIC_TIME_TO_FIRST_UTTERANCE",
    "METRIC_VAD_QUIET_SIGNAL_GATED",
    "record_active_endpoint_change",
    "record_aec_bypass_combo",
    "record_aec_double_talk",
    "record_aec_erle",
    "record_aec_window",
    "record_apo_degraded_event",
    "record_audio_phase_inversion_recovery",
    "record_audio_resample_peak_clip",
    "record_audio_signal_destroyed",
    "record_audio_snr_db",
    "record_bypass_improvement_resolution",
    "record_bypass_probe_wait_ms",
    "record_bypass_probe_window_contaminated",
    "record_bypass_strategy_verdict",
    "record_capture_integrity_verdict",
    "record_cascade_attempt",
    "record_cold_silence_rejected",
    "record_combo_store_hit",
    "record_combo_store_invalidation",
    "record_hotplug_listener_registered",
    "record_kernel_invalidated_event",
    "record_ns_suppression_db",
    "record_ns_window",
    "record_opener_attempt",
    "record_opener_host_api_alignment",
    "record_preflight_failure",
    "record_probe_diagnosis",
    "record_probe_duration",
    "record_probe_result",
    "record_recovery_attempt",
    "record_noise_floor_drift_alert",
    "record_self_feedback_block",
    "record_snr_low_alert",
    "record_start_time_error",
    "record_tier1_raw_attempted",
    "record_tier1_raw_outcome",
    "record_tier2_host_api_rotate_attempted",
    "record_tier2_host_api_rotate_outcome",
    "record_time_to_first_utterance",
    "record_vad_quiet_signal_gated",
    "record_wake_word_confidence",
    "record_audio_error_translated",
    "record_wake_word_detection_method",
    "record_wake_word_resolution_strategy",
    "record_wake_word_detection_ms",
    "record_wake_word_false_fire",
    "record_wake_word_fast_path_engaged",
    "record_wake_word_stage1_inference_ms",
    "record_wake_word_stage2_collection_ms",
    "record_wake_word_stage2_verifier_ms",
]
