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
METRIC_PROBE_START_TIME_ERRORS = "sovyx.voice.health.probe.start_time_errors"
METRIC_APO_DEGRADED_EVENTS = "sovyx.voice.health.apo_degraded.events"
METRIC_BYPASS_STRATEGY_VERDICTS = "sovyx.voice.health.bypass_strategy.verdicts"
METRIC_CAPTURE_INTEGRITY_VERDICTS = "sovyx.voice.health.capture_integrity.verdicts"
METRIC_BYPASS_PROBE_WAIT_MS = "sovyx.voice.health.bypass.probe_wait_ms"
METRIC_BYPASS_PROBE_WINDOW_CONTAMINATED = "sovyx.voice.health.bypass.probe_window_contaminated"
METRIC_BYPASS_IMPROVEMENT_RESOLUTION = "sovyx.voice.health.bypass.improvement_resolution"

# ── Voice Windows Paranoid Mission (v0.24.0 → v0.26.0) ──────────────
# New counter vocabulary for Furo W-1 (cold-probe silence rejection),
# Tier 1 RAW + Tier 2 host_api_rotate bypass strategies, opener
# alignment SLI, and IMMNotificationClient registration health. Names
# are stable wire contracts — downstream dashboards / Grafana queries /
# Prometheus alerts depend on them.

METRIC_PROBE_COLD_SILENCE_REJECTED = "sovyx.voice.health.probe.cold_silence_rejected"
METRIC_BYPASS_TIER1_RAW_ATTEMPTED = "sovyx.voice.health.bypass.tier1_raw.attempted"
METRIC_BYPASS_TIER1_RAW_OUTCOME = "sovyx.voice.health.bypass.tier1_raw.outcome"
METRIC_BYPASS_TIER2_HOST_API_ROTATE_ATTEMPTED = (
    "sovyx.voice.health.bypass.tier2_host_api_rotate.attempted"
)
METRIC_BYPASS_TIER2_HOST_API_ROTATE_OUTCOME = (
    "sovyx.voice.health.bypass.tier2_host_api_rotate.outcome"
)
METRIC_OPENER_HOST_API_ALIGNMENT = "sovyx.voice.opener.host_api_alignment"
METRIC_HOTPLUG_LISTENER_REGISTERED = "sovyx.voice.hotplug.listener.registered"

# ── Phase 4 — AEC observability (T4.7 + T4.8) ───────────────────────────
METRIC_AEC_ERLE_DB = "sovyx.voice.aec.erle_db"
METRIC_AEC_WINDOWS = "sovyx.voice.aec.windows"


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


# ── Voice Windows Paranoid Mission record helpers ──────────────────────


def record_cold_silence_rejected(*, mode: str, host_api: str) -> None:
    """Record a cold-probe silence-rejection event (Furo W-1 telemetry).

    Args:
        mode: ``"strict_reject"`` (post-fix path returning NO_SIGNAL) or
            ``"lenient_passthrough"`` (legacy v0.23.x acceptance kept for
            calibration during the foundation phase).
        host_api: combo's host_api (``"Windows WASAPI"``, ``"MME"``, etc).

    The lenient counter is the operator's calibration knob — when its rate
    matches the predicted silent-combo population on Voice Clarity rigs
    (mission §F1 promotion gate), flipping
    ``probe_cold_strict_validation_enabled`` to True is safe.
    """
    counter = getattr(get_metrics(), "voice_health_probe_cold_silence_rejected", None)
    if counter is None:
        return
    counter.add(
        1,
        attributes={
            "mode": mode,
            "host_api": host_api or "unknown",
        },
    )


def record_tier1_raw_attempted(*, host_api: str, raw_supported: bool) -> None:
    """Record a Tier 1 RAW + Communications bypass attempt (Windows only).

    Fired by ``WindowsRawCommunicationsBypass.apply`` before the
    ``IAudioClient3::SetClientProperties`` call. Pairs with
    :func:`record_tier1_raw_outcome` for an attempts-vs-success ratio.
    """
    _bypass_tier_state.mark_tier1_raw_attempted()
    counter = getattr(get_metrics(), "voice_health_bypass_tier1_raw_attempted", None)
    if counter is None:
        return
    counter.add(
        1,
        attributes={
            "host_api": host_api or "unknown",
            "raw_supported": "true" if raw_supported else "false",
        },
    )


def record_tier1_raw_outcome(*, verdict: str, host_api: str) -> None:
    """Record a Tier 1 RAW bypass outcome (Windows only).

    Args:
        verdict: ``RawCommunicationsRestartVerdict`` value
            (``raw_engaged``, ``property_rejected_by_driver``,
            ``open_failed_no_stream``, etc.).
        host_api: post-apply host_api (informational — the strategy
            doesn't mutate it; included for slice-by-host_api dashboards).
    """
    _bypass_tier_state.mark_tier1_raw_outcome(verdict)
    counter = getattr(get_metrics(), "voice_health_bypass_tier1_raw_outcome", None)
    if counter is None:
        return
    counter.add(
        1,
        attributes={
            "verdict": verdict,
            "host_api": host_api or "unknown",
        },
    )


def record_tier2_host_api_rotate_attempted(*, source_host_api: str, target_host_api: str) -> None:
    """Record a Tier 2 host-API rotate-then-exclusive attempt (Windows only).

    Fired by ``WindowsHostApiRotateThenExclusiveBypass.apply`` Phase A
    (rotate) before ``request_host_api_rotate``.
    """
    _bypass_tier_state.mark_tier2_host_api_rotate_attempted()
    counter = getattr(get_metrics(), "voice_health_bypass_tier2_host_api_rotate_attempted", None)
    if counter is None:
        return
    counter.add(
        1,
        attributes={
            "source_host_api": source_host_api or "unknown",
            "target_host_api": target_host_api or "unknown",
        },
    )


def record_tier2_host_api_rotate_outcome(
    *,
    phase_a_verdict: str,
    phase_b_verdict: str,
    resulting_host_api: str = "",
) -> None:
    """Record a Tier 2 rotate+exclusive outcome (Windows only).

    Args:
        phase_a_verdict: ``HostApiRotateVerdict`` value
            (``rotated_success``, ``no_target_sibling``, etc.).
        phase_b_verdict: ``ExclusiveRestartVerdict`` value when
            Phase A succeeded; ``"skipped"`` when Phase A failed.
        resulting_host_api: the host_api the stream actually ended on
            after both phases — may differ from the target if the
            opener pyramid drifted.
    """
    _bypass_tier_state.mark_tier2_host_api_rotate_outcome(
        phase_a_verdict=phase_a_verdict,
        phase_b_verdict=phase_b_verdict,
    )
    counter = getattr(get_metrics(), "voice_health_bypass_tier2_host_api_rotate_outcome", None)
    if counter is None:
        return
    counter.add(
        1,
        attributes={
            "phase_a_verdict": phase_a_verdict,
            "phase_b_verdict": phase_b_verdict,
            "resulting_host_api": resulting_host_api or "unknown",
        },
    )


def record_opener_host_api_alignment(
    *,
    aligned: bool,
    cascade_winner_host_api: str = "",
    runtime_chain_head_host_api: str = "",
) -> None:
    """Record a cascade-↔-runtime opener alignment SLI sample (Furo W-4).

    Fired on every ``_device_chain`` invocation. ``aligned=True`` when
    ``runtime_chain_head_host_api == cascade_winner_host_api``;
    ``aligned=False`` is the bug signature (the opener drifted off
    the cascade winner).

    Target SLI: 100 % aligned. Any drift is a bug, not a tunable.
    """
    counter = getattr(get_metrics(), "voice_opener_host_api_alignment", None)
    if counter is None:
        return
    counter.add(
        1,
        attributes={
            "aligned": "true" if aligned else "false",
            "cascade_winner_host_api": cascade_winner_host_api or "unknown",
            "runtime_chain_head_host_api": runtime_chain_head_host_api or "unknown",
        },
    )


def record_hotplug_listener_registered(*, registered: bool, error: str = "") -> None:
    """Record an IMMNotificationClient registration health sample.

    Fired once at AudioCaptureTask.start when
    ``mm_notification_listener_enabled=True``. Promotion gate:
    registration success ≥99 % on Win10/11 is the threshold for
    flipping the listener flag default to True in v0.26.0.
    """
    counter = getattr(get_metrics(), "voice_hotplug_listener_registered", None)
    if counter is None:
        return
    counter.add(
        1,
        attributes={
            "registered": "true" if registered else "false",
            "error": error or "none",
        },
    )


# ── Phase 4 / T4.7-T4.8 — AEC observability ─────────────────────────────


def record_aec_erle(*, erle_db: float) -> None:
    """Record one Echo Return Loss Enhancement sample (Phase 4 / T4.7).

    Fires once per emitted 512-sample capture window when the AEC
    stage processed a non-silent render reference. Silent windows
    are NOT recorded — ERLE is undefined when there's no echo to
    cancel and a flat 0 dB sample would distort the histogram p50.

    Promotion gate (master mission §Phase 4): p50 ≥ 35 dB,
    p95 ≥ 30 dB sustained when render+capture both active.

    Args:
        erle_db: ERLE measurement in dB. Capped at +120 dB inside
            :func:`sovyx.voice._aec.compute_erle` to keep histogram
            buckets stable.
    """
    histogram = getattr(get_metrics(), "voice_aec_erle_db", None)
    if histogram is None:
        return
    histogram.record(float(erle_db))


def record_aec_window(*, state: str) -> None:
    """Record one AEC stage outcome (Phase 4 / T4.8).

    Fires once per emitted capture window when the AEC stage is
    wired (engine != "off"). The processed/total ratio reveals how
    often AEC actually had echo to cancel — a session with
    constant TTS playback approaches 100 % processed; a session
    with mostly silent listener runs approaches 0 %.

    Args:
        state: ``"processed"`` (AEC engaged on non-silent render) or
            ``"render_silent"`` (AEC short-circuited because the
            render reference was zero — see
            :class:`SpeexAecProcessor.process` early-return).
    """
    counter = getattr(get_metrics(), "voice_aec_windows", None)
    if counter is None:
        return
    counter.add(1, attributes={"state": state})


__all__ = [
    "METRIC_ACTIVE_ENDPOINT_CHANGES",
    "METRIC_AEC_ERLE_DB",
    "METRIC_AEC_WINDOWS",
    "METRIC_APO_DEGRADED_EVENTS",
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
    "METRIC_OPENER_HOST_API_ALIGNMENT",
    "METRIC_PREFLIGHT_FAILURES",
    "METRIC_PROBE_COLD_SILENCE_REJECTED",
    "METRIC_PROBE_DIAGNOSIS",
    "METRIC_PROBE_DURATION",
    "METRIC_PROBE_START_TIME_ERRORS",
    "METRIC_RECOVERY_ATTEMPTS",
    "METRIC_SELF_FEEDBACK_BLOCKS",
    "METRIC_TIME_TO_FIRST_UTTERANCE",
    "record_active_endpoint_change",
    "record_aec_erle",
    "record_aec_window",
    "record_apo_degraded_event",
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
    "record_opener_attempt",
    "record_opener_host_api_alignment",
    "record_preflight_failure",
    "record_probe_diagnosis",
    "record_probe_duration",
    "record_probe_result",
    "record_recovery_attempt",
    "record_self_feedback_block",
    "record_start_time_error",
    "record_tier1_raw_attempted",
    "record_tier1_raw_outcome",
    "record_tier2_host_api_rotate_attempted",
    "record_tier2_host_api_rotate_outcome",
    "record_time_to_first_utterance",
]
