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

# ── Phase 4 — AEC observability (T4.7 + T4.8 + T4.9) ────────────────────
METRIC_AEC_ERLE_DB = "sovyx.voice.aec.erle_db"
METRIC_AEC_WINDOWS = "sovyx.voice.aec.windows"
METRIC_AEC_DOUBLE_TALK = "sovyx.voice.aec.double_talk"
METRIC_AEC_BYPASS_COMBO = "sovyx.voice.aec.bypass_combo"
METRIC_VAD_QUIET_SIGNAL_GATED = "sovyx.voice.vad.quiet_signal_gated"
METRIC_PIPELINE_SNR_LOW_ALERTS = "sovyx.voice.pipeline.snr_low_alerts"
METRIC_PIPELINE_NOISE_FLOOR_DRIFT_ALERTS = "sovyx.voice.pipeline.noise_floor_drift_alerts"

# ── Phase 4 — NS observability (T4.16) ──────────────────────────────────
METRIC_NS_WINDOWS = "sovyx.voice.ns.windows"
METRIC_NS_SUPPRESSION_DB = "sovyx.voice.ns.suppression_db"

# ── Phase 4 — SNR observability (T4.33) ─────────────────────────────────
METRIC_AUDIO_SNR_DB = "sovyx.voice.audio.snr_db"

# ── Phase 4 — Wiener entropy destruction detector (T4.44.b) ─────────────
METRIC_AUDIO_SIGNAL_DESTROYED = "sovyx.voice.audio.signal_destroyed"

# ── Phase 4 — Resample peak-clip detector (T4.45) ───────────────────────
METRIC_AUDIO_RESAMPLE_PEAK_CLIP = "sovyx.voice.audio.resample_peak_clip"

# ── Phase 4 — Phase-inversion auto-recovery (T4.46) ─────────────────────
METRIC_AUDIO_PHASE_INVERSION_RECOVERY = "sovyx.voice.audio.phase_inversion_recovery"


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


# ── Phase 7 / T7.1 — wake-word latency profile ─────────────────────────


def record_wake_word_stage1_inference_ms(
    *,
    duration_ms: float,
    model_name: str,
) -> None:
    """Record per-frame stage-1 ONNX inference duration.

    Emitted from :meth:`WakeWordDetector._run_inference` for every
    audio frame fed into the detector — runs at the 80 ms frame
    cadence (12.5 Hz). Typical values: ~5 ms on Pi 5, ~1 ms on N100.
    Sustained p99 > 50 ms indicates host CPU saturation that will
    starve the audio callback before any wake-word user-experience
    impact.

    Args:
        duration_ms: Wall-clock duration of the ONNX session.run call.
        model_name: ONNX checkpoint file stem (e.g. ``"sovyx_v1"``).
            Cardinality-bounded by the small set of installed wake-
            word variants per the Phase 7 multi-language plan.
    """
    get_metrics().voice_wake_word_stage1_inference_latency.record(
        duration_ms,
        attributes={"model_name": model_name},
    )


def record_wake_word_stage2_collection_ms(
    *,
    duration_ms: float,
    outcome: str,
) -> None:
    """Record stage-2 audio-collection window duration.

    Emitted from :meth:`WakeWordDetector._evaluate_stage2` once per
    stage-2 evaluation, regardless of outcome. The wall-clock
    duration from ``STAGE1_TRIGGERED`` entry to evaluation should
    closely track the configured ``stage2_window_seconds`` (default
    1500 ms) plus per-frame jitter — T7.2 reduces this from 1500 ms
    → 500 ms; this histogram is the before/after measurement.

    Args:
        duration_ms: Wall-clock time from stage-1 trigger to evaluation.
        outcome: ``"confirmed"`` (verifier passed),
            ``"rejected_threshold"`` (peak_score < stage2_threshold),
            ``"rejected_verifier"`` (verifier failed).
    """
    get_metrics().voice_wake_word_stage2_collection_latency.record(
        duration_ms,
        attributes={"outcome": outcome},
    )


def record_wake_word_stage2_verifier_ms(
    *,
    duration_ms: float,
    outcome: str,
) -> None:
    """Record stage-2 verifier (STT) call duration.

    Emitted only on stage-2 evaluations where ``peak_score ≥
    stage2_threshold`` (the verifier never runs below threshold).
    T7.5 replaces STT with phoneme matching — this histogram is the
    before/after measurement.

    Args:
        duration_ms: Wall-clock duration of the verifier callable.
        outcome: ``"verified"`` (verifier returned True) or
            ``"rejected"`` (verifier returned False).
    """
    get_metrics().voice_wake_word_stage2_verifier_latency.record(
        duration_ms,
        attributes={"outcome": outcome},
    )


def record_wake_word_detection_ms(*, duration_ms: float) -> None:
    """Record end-to-end wake-word detection latency.

    Emitted only on CONFIRMED detections. Wall-clock from
    ``STAGE1_TRIGGERED`` entry to ``wake_word_detected`` event
    emission. The v0.30.0 GA promotion gate target is p95 ≤ 500 ms
    (Alexa / Google / Siri parity per master mission).

    Args:
        duration_ms: Wall-clock time from stage-1 trigger to confirm.
    """
    get_metrics().voice_wake_word_detection_latency.record(duration_ms)


def record_wake_word_confidence(
    *,
    score: float,
    detection_path: str,
) -> None:
    """Record the ONNX score at confirmed-detection time.

    Phase 7 / T7.6 — histogram captures the distribution of scores
    at which detections actually fire (distinct from the per-frame
    ``voice.wake_word.score`` log event which fires for EVERY frame).
    Dashboards render this distribution to surface the bimodal
    signature: a strong peak > 0.8 indicates T7.4 fast-path will
    be effective at the operator's pilot threshold.

    Args:
        score: The score at confirmation. For 2-stage, this is the
            peak across the collection window; for fast-path it's
            the trigger-frame score.
        detection_path: ``"two_stage"`` (legacy STT verifier path) or
            ``"fast_path"`` (T7.4 high-confidence skip).
    """
    get_metrics().voice_wake_word_confidence.record(
        score,
        attributes={"detection_path": detection_path},
    )


def record_wake_word_fast_path_engaged(*, score: float) -> None:
    """Increment the T7.4 fast-path engagement counter.

    Fires every time a wake-word detection skips stage-2 because the
    stage-1 score crossed ``stage1_high_confidence_threshold``.
    Pair with the standard wake-word detection total for the engage
    rate; pilot target per operator backlog is ~70-90% engage rate
    without elevating the false-fire rate.

    Args:
        score: The stage-1 ONNX score that triggered the fast path.
            Recorded as an attribute (bucketed via OTel Views in
            production) so dashboards can render the distribution.
    """
    get_metrics().voice_wake_word_fast_path_engaged.add(
        1,
        attributes={"score_bucket": _bucket_score(score)},
    )


def _bucket_score(score: float) -> str:
    """Bucket a wake-word score into operator-readable bands.

    Cardinality-bounded — 5 fixed buckets — so the counter doesn't
    blow the OTel cardinality budget regardless of how many wakes
    fire.
    """
    if score >= 0.95:  # noqa: PLR2004
        return "0.95-1.00"
    if score >= 0.90:  # noqa: PLR2004
        return "0.90-0.95"
    if score >= 0.85:  # noqa: PLR2004
        return "0.85-0.90"
    if score >= 0.80:  # noqa: PLR2004
        return "0.80-0.85"
    return "<0.80"


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


def record_ns_window(*, state: str) -> None:
    """Record one NS stage outcome (Phase 4 / T4.16).

    Fires once per emitted capture window when the NS stage is
    wired. The processed/total ratio reveals how often NS actually
    attenuated something — a session in a quiet room approaches
    0% processed; a session with HVAC running approaches 100%.

    Args:
        state: ``"processed"`` (NS attenuated the window — input
            dBFS > output dBFS by > 0.5 dB) or ``"passthrough"``
            (NS ran but found nothing to gate — every bin sat
            above the magnitude floor).
    """
    counter = getattr(get_metrics(), "voice_ns_windows", None)
    if counter is None:
        return
    counter.add(1, attributes={"state": state})


def record_audio_phase_inversion_recovery(*, state: str) -> None:
    """Record one phase-inversion recovery state transition (Phase 4 / T4.46).

    Fires only on engage/revert transitions (NOT once per block);
    the dashboard's transition rate is the primary signal for
    hardware-fault forensics.

    Args:
        state: ``"engaged"`` (L-only fallback latched in after
            sustained inversion) or ``"reverted"`` (downmix
            returned to L+R mean after sustained clean signal).
    """
    counter = getattr(get_metrics(), "voice_audio_phase_inversion_recovery", None)
    if counter is None:
        return
    counter.add(1, attributes={"state": state})


def record_audio_resample_peak_clip(*, state: str) -> None:
    """Record one resample peak-clip verdict (Phase 4 / T4.45).

    Fires once per push() when the resample-peak detector is
    wired AND the FrameNormalizer is on the non-passthrough path
    (resampling actually ran). Distinct from the R2 saturation
    counter — this one isolates overshoot introduced by the
    resampler Gibbs phenomenon, while R2 counts post-multiply
    int16 rail hits (normal for hot inputs).

    Args:
        state: ``"clip"`` (post-resample peak ≥ 1.0) or
            ``"clean"`` (peak < 1.0).
    """
    counter = getattr(get_metrics(), "voice_audio_resample_peak_clip", None)
    if counter is None:
        return
    counter.add(1, attributes={"state": state})


def record_audio_signal_destroyed(*, state: str) -> None:
    """Record one Wiener-entropy destruction verdict (Phase 4 / T4.44.b).

    Fires once per processed frame (post-downmix, pre-resample)
    when the entropy detector is wired. The destroyed/total ratio
    is the primary destruction-rate signal for the dashboard.

    Args:
        state: ``"destroyed"`` (entropy > threshold) or
            ``"clean"`` (entropy ≤ threshold).
    """
    counter = getattr(get_metrics(), "voice_audio_signal_destroyed", None)
    if counter is None:
        return
    counter.add(1, attributes={"state": state})


def record_audio_snr_db(*, snr_db: float) -> None:
    """Record one per-window SNR estimate in dB (Phase 4 / T4.33).

    Fires when the SNR estimator returned a real measurement
    (i.e. the frame was above the silence floor and the noise
    tracker had at least one prior observation). Silent frames
    + degenerate first-frame zero-SNR samples should NOT be
    routed here — they distort the histogram p50 with floor
    noise.

    Promotion gate (master mission §Phase 4 / T4.35): alert when
    p50 drops below 9 dB (Moonshine STT degradation threshold).

    Args:
        snr_db: SNR measurement in dB. Capped at +120 dB inside
            :class:`SnrEstimator.estimate` to keep histogram
            buckets stable when the noise floor approaches the
            silence-floor limit.
    """
    histogram = getattr(get_metrics(), "voice_audio_snr_db", None)
    if histogram is None:
        return
    histogram.record(float(snr_db))


def record_ns_suppression_db(*, suppression_db: float) -> None:
    """Record one NS suppression sample in dB (Phase 4 / T4.16).

    Fires only on ``processed`` windows (passthrough windows have
    suppression ≈ 0 dB by definition and would distort the
    histogram p50 with a flat-zero spike).

    Args:
        suppression_db: ``input_dbfs - output_dbfs`` per window.
            Positive values mean NS reduced the frame energy
            (typical 5-20 dB range for spectral gating). Capped at
            +120 dB inside the FrameNormalizer's emission helper to
            keep histogram buckets stable when the gate produces a
            near-zero residual.
    """
    histogram = getattr(get_metrics(), "voice_ns_suppression_db", None)
    if histogram is None:
        return
    histogram.record(float(suppression_db))


def record_aec_double_talk(*, state: str) -> None:
    """Record one double-talk detector verdict (Phase 4 / T4.9).

    Fires once per emitted capture window when the double-talk
    detector is wired (``voice_double_talk_detection_enabled=True``).
    The detected/(detected+absent) ratio reveals how often the user
    was speaking during TTS playback — a calm dictation session
    approaches 0 %, an interruption-heavy conversation pushes it
    towards 100 %.

    Args:
        state: ``"detected"`` (NCC < threshold — user speaking
            during TTS), ``"absent"`` (NCC ≥ threshold — pure
            echo, filter can converge), or ``"undecided"`` (NCC
            undefined because either signal was silent — typically
            the same windows that fire ``voice.aec.windows{
            render_silent}``, kept separate for cardinality
            symmetry).
    """
    counter = getattr(get_metrics(), "voice_aec_double_talk", None)
    if counter is None:
        return
    counter.add(1, attributes={"state": state})


def record_noise_floor_drift_alert(*, state: str) -> None:
    """Record one noise-floor drift alert state transition (T4.38).

    Fires from :meth:`VoicePipeline._track_vad_for_heartbeat`
    once per state transition (open ⟶ alerted, alerted ⟶
    resolved). Cardinality bounded to two events per incident.

    Args:
        state: One of:
            * ``"warned"`` — consecutive heartbeats with rolling
              noise-floor short-window average exceeding the
              long-window baseline by the threshold (default
              10 dB) hit the de-flap count. Orchestrator fired
              ``voice_pipeline_noise_floor_drift_warning``.
            * ``"cleared"`` — the next clean heartbeat resolved
              the drift. Orchestrator fired
              ``voice_pipeline_noise_floor_drift_cleared``.
    """
    counter = getattr(get_metrics(), "voice_pipeline_noise_floor_drift_alerts", None)
    if counter is None:
        return
    counter.add(1, attributes={"state": state})


def record_snr_low_alert(*, state: str) -> None:
    """Record one SNR low-alert state transition (Phase 4 / T4.35).

    Fires from :meth:`VoicePipeline._track_vad_for_heartbeat` once
    per state transition (open ⟶ alerted, alerted ⟶ resolved).
    Cardinality is bounded to two events per incident so the
    counter pair tracks open incidents over time as
    ``warned − cleared``.

    Args:
        state: One of:
            * ``"warned"`` — consecutive low-SNR heartbeats hit the
              de-flap threshold. The orchestrator fired
              ``voice_pipeline_snr_low_alert`` and latched the
              alert as active.
            * ``"cleared"`` — the next clean heartbeat resolved
              the incident. The orchestrator fired
              ``voice_pipeline_snr_low_alert_cleared`` and reset
              the latch.
    """
    counter = getattr(get_metrics(), "voice_pipeline_snr_low_alerts", None)
    if counter is None:
        return
    counter.add(1, attributes={"state": state})


def record_vad_quiet_signal_gated(*, state: str) -> None:
    """Record one VAD quiet-signal gate verdict (Phase 4 / T4.39).

    Fires from inside :meth:`SileroVAD.process_frame` whenever the
    paradox detector observes a frame with RMS below the configured
    dBFS gate AND VAD probability above the configured threshold.
    The detector ALWAYS observes — the action (force probability
    to 0.0) only fires when ``VADConfig.quiet_signal_gate_enabled``
    is True.

    Args:
        state: One of:
            * ``"gated"`` — the action engaged: probability was
              force-clamped to 0.0 before the FSM read it. Implies
              the operator has flipped ``quiet_signal_gate_enabled``
              to True.
            * ``"would_gate"`` — the detector saw the paradox but
              the action is disabled (foundation default). The
              counter still fires so operators can measure the
              base rate before opting in.
    """
    counter = getattr(get_metrics(), "voice_vad_quiet_signal_gated", None)
    if counter is None:
        return
    counter.add(1, attributes={"state": state})


def record_aec_bypass_combo(*, state: str) -> None:
    """Record one boot-time AEC + WASAPI-exclusive combo verdict (T4.6).

    Fires once per voice pipeline construction in
    :func:`sovyx.voice.factory._build_aec_wiring`. Cardinality is
    bounded to one event per process boot, so the counter doubles
    as a feature-flag-state ledger across the fleet — useful when
    rolling out the auto-engage default.

    Args:
        state: One of:
            * ``"safe_shared"`` — exclusive=False, AEC=False. OS
              AEC is in the chain; in-process AEC is the operator's
              choice.
            * ``"safe_engaged"`` — exclusive=True, AEC=True. OS
              AEC bypassed but in-process AEC active. Recommended
              configuration for WASAPI-exclusive deployments.
            * ``"safe_belt_and_suspenders"`` — exclusive=False,
              AEC=True. Both layers active. Redundant but safe;
              minor CPU cost from double processing.
            * ``"dangerous"`` — exclusive=True, AEC=False, auto-
              engage=False. OS AEC bypassed AND in-process AEC
              off. TTS leaks into ASR. WARN-level log fires
              alongside this metric.
            * ``"auto_engaged"`` — exclusive=True, AEC=False,
              auto-engage=True. Same dangerous combo, but the
              operator opted into ``voice_aec_auto_engage_on_
              exclusive=True`` so the factory force-engaged AEC
              with the configured engine.
    """
    counter = getattr(get_metrics(), "voice_aec_bypass_combo", None)
    if counter is None:
        return
    counter.add(1, attributes={"state": state})


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
    "record_wake_word_detection_ms",
    "record_wake_word_fast_path_engaged",
    "record_wake_word_stage1_inference_ms",
    "record_wake_word_stage2_collection_ms",
    "record_wake_word_stage2_verifier_ms",
]
