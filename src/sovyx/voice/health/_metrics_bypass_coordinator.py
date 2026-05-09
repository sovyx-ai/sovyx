"""CaptureIntegrityCoordinator bypass + verdict metrics.

Phase 5.F.10 god-file extraction from ``voice/health/_metrics.py``
(anti-pattern #16). Owns the v1.3 §14 CaptureIntegrityCoordinator
observability surface — 5 record helpers + 4 metric-name constants
spanning:

* :func:`record_bypass_strategy_verdict` — per-strategy outcome
  classification (applied_healthy / applied_still_dead /
  failed_to_apply / not_applicable). Mirrors to ``_bypass_tier_state``
  for the dashboard's bypass-tier counter pair.
* :func:`record_bypass_probe_wait_ms` — post-apply probe wait
  duration (v1.3 §14.E1).
* :func:`record_bypass_probe_window_contaminated` — degraded but
  not-contaminated post-apply probe (v1.3 §14.E1 tuple-mark design).
* :func:`record_bypass_improvement_resolution` — v1.3 §14.E2
  improvement-heuristic resolution (VAD_MUTE + spectral rolloff
  improved).
* :func:`record_capture_integrity_verdict` — the underlying
  :class:`IntegrityVerdict` classifier outcome (pre_bypass /
  post_bypass / recheck phase).

Anti-pattern #20 covered: parent module ``voice/health/_metrics.py``
re-exports every symbol so production callers
(``CaptureIntegrityCoordinator``, ``CaptureIntegrityProbe``) and test
references at ``sovyx.voice.health._metrics.<name>`` continue to
resolve via standard module-namespace lookup.
"""

from __future__ import annotations

from sovyx.observability.metrics import get_metrics
from sovyx.voice.health import _bypass_tier_state

# ── Stable name constants (v1.3 §14 CaptureIntegrityCoordinator) ────

METRIC_BYPASS_STRATEGY_VERDICTS = "sovyx.voice.health.bypass_strategy.verdicts"
METRIC_BYPASS_PROBE_WAIT_MS = "sovyx.voice.health.bypass.probe_wait_ms"
METRIC_BYPASS_PROBE_WINDOW_CONTAMINATED = "sovyx.voice.health.bypass.probe_window_contaminated"
METRIC_BYPASS_IMPROVEMENT_RESOLUTION = "sovyx.voice.health.bypass.improvement_resolution"
METRIC_CAPTURE_INTEGRITY_VERDICTS = "sovyx.voice.health.capture_integrity.verdicts"


# ── Record helpers ──────────────────────────────────────────────────


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


__all__ = [
    "METRIC_BYPASS_IMPROVEMENT_RESOLUTION",
    "METRIC_BYPASS_PROBE_WAIT_MS",
    "METRIC_BYPASS_PROBE_WINDOW_CONTAMINATED",
    "METRIC_BYPASS_STRATEGY_VERDICTS",
    "METRIC_CAPTURE_INTEGRITY_VERDICTS",
    "record_bypass_improvement_resolution",
    "record_bypass_probe_wait_ms",
    "record_bypass_probe_window_contaminated",
    "record_bypass_strategy_verdict",
    "record_capture_integrity_verdict",
]
