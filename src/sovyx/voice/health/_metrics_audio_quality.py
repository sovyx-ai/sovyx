"""Phase 4 audio-quality metrics — AEC + NS + SNR + audio destruction recorders.

Phase 5.F.4 god-file extraction from ``voice/health/_metrics.py``
(anti-pattern #16). Owns the v0.24+ Phase 4 audio observability
surface — 13 record helpers + 13 metric-name constants spanning:

* **AEC** (Echo Return Loss Enhancement, double-talk, bypass combo) —
  T4.6 / T4.7 / T4.8 / T4.9.
* **NS** (Noise Suppression windows + suppression dB) — T4.16.
* **SNR** (per-window SNR + low-alert state transitions) — T4.33 / T4.35.
* **Audio destruction detectors** (Wiener-entropy, resample peak-clip,
  phase-inversion recovery) — T4.44.b / T4.45 / T4.46.
* **VAD quiet-signal gate** + **noise-floor drift alerts** — T4.38 / T4.39.

All recorders are thin façades over
:class:`sovyx.observability.metrics.MetricsRegistry` instruments —
they fire-and-forget, swallow ``None`` instruments (instrument not
yet wired), and never raise.

Anti-pattern #20 covered: parent module ``voice/health/_metrics.py``
re-exports every symbol so production callers + test references at
the original ``sovyx.voice.health._metrics.<name>`` path continue to
resolve via standard module-namespace lookup.
"""

from __future__ import annotations

from sovyx.observability.metrics import get_metrics


# ── Stable name constants (test-friendly) ───────────────────────────
# The OTel instrument names. Tests assert on these so a typo in the
# registry definition fails loudly. Names are immutable — any rename
# is a breaking contract change for downstream dashboards.

# Phase 4 — AEC observability (T4.7 + T4.8 + T4.9)
METRIC_AEC_ERLE_DB = "sovyx.voice.aec.erle_db"
METRIC_AEC_WINDOWS = "sovyx.voice.aec.windows"
METRIC_AEC_DOUBLE_TALK = "sovyx.voice.aec.double_talk"
METRIC_AEC_BYPASS_COMBO = "sovyx.voice.aec.bypass_combo"
METRIC_VAD_QUIET_SIGNAL_GATED = "sovyx.voice.vad.quiet_signal_gated"
METRIC_PIPELINE_SNR_LOW_ALERTS = "sovyx.voice.pipeline.snr_low_alerts"
METRIC_PIPELINE_NOISE_FLOOR_DRIFT_ALERTS = "sovyx.voice.pipeline.noise_floor_drift_alerts"

# Phase 4 — NS observability (T4.16)
METRIC_NS_WINDOWS = "sovyx.voice.ns.windows"
METRIC_NS_SUPPRESSION_DB = "sovyx.voice.ns.suppression_db"

# Phase 4 — SNR observability (T4.33)
METRIC_AUDIO_SNR_DB = "sovyx.voice.audio.snr_db"

# Phase 4 — Wiener entropy destruction detector (T4.44.b)
METRIC_AUDIO_SIGNAL_DESTROYED = "sovyx.voice.audio.signal_destroyed"

# Phase 4 — Resample peak-clip detector (T4.45)
METRIC_AUDIO_RESAMPLE_PEAK_CLIP = "sovyx.voice.audio.resample_peak_clip"

# Phase 4 — Phase-inversion auto-recovery (T4.46)
METRIC_AUDIO_PHASE_INVERSION_RECOVERY = "sovyx.voice.audio.phase_inversion_recovery"


# ── Record helpers ──────────────────────────────────────────────────


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
    "METRIC_AEC_BYPASS_COMBO",
    "METRIC_AEC_DOUBLE_TALK",
    "METRIC_AEC_ERLE_DB",
    "METRIC_AEC_WINDOWS",
    "METRIC_AUDIO_PHASE_INVERSION_RECOVERY",
    "METRIC_AUDIO_RESAMPLE_PEAK_CLIP",
    "METRIC_AUDIO_SIGNAL_DESTROYED",
    "METRIC_AUDIO_SNR_DB",
    "METRIC_NS_SUPPRESSION_DB",
    "METRIC_NS_WINDOWS",
    "METRIC_PIPELINE_NOISE_FLOOR_DRIFT_ALERTS",
    "METRIC_PIPELINE_SNR_LOW_ALERTS",
    "METRIC_VAD_QUIET_SIGNAL_GATED",
    "record_aec_bypass_combo",
    "record_aec_double_talk",
    "record_aec_erle",
    "record_aec_window",
    "record_audio_phase_inversion_recovery",
    "record_audio_resample_peak_clip",
    "record_audio_signal_destroyed",
    "record_audio_snr_db",
    "record_noise_floor_drift_alert",
    "record_ns_suppression_db",
    "record_ns_window",
    "record_snr_low_alert",
    "record_vad_quiet_signal_gated",
]
