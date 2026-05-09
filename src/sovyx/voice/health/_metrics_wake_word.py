"""Phase 7 wake-word recorders — latency profile + verdict telemetry.

Phase 5.F.5 god-file extraction from ``voice/health/_metrics.py``
(anti-pattern #16). Owns the v0.27+ Phase 7 wake-word observability
surface — 10 record helpers spanning:

* **Stage-1 / stage-2 latency** (per-frame inference + collection +
  verifier durations).
* **Detection latency totals** + **confidence histograms**.
* **False-fire** counters (per-mind cardinality-bounded).
* **Detection method** (onnx / stt_fallback) + **resolution strategy**
  (the wake-word-router T8.6 verdict).
* **Fast-path engagement counter** (T7.4) + private
  :func:`_bucket_score` helper that buckets stage-1 scores into 5
  fixed bands for cardinality safety.

All recorders are thin façades over
:class:`sovyx.observability.metrics.MetricsRegistry` instruments.

Anti-pattern #20 covered: parent module ``voice/health/_metrics.py``
re-exports every symbol so production callers
(``WakeWordDetector``, ``WakeWordRouter``) and test references at
the original ``sovyx.voice.health._metrics.<name>`` path continue to
resolve via standard module-namespace lookup.
"""

from __future__ import annotations

from sovyx.observability.metrics import get_metrics


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


def record_wake_word_false_fire(*, reason: str, mind_id: str = "") -> None:
    """Increment the T7.7 false-fire counter.

    Fires when wake-word triggered but the resulting STT transcript
    was discarded. Operator pilot signal for the T7.4 fast-path
    threshold tuning + the v0.30.0 GA promotion gate "false-fire
    rate stays below v0.23.x baseline".

    Args:
        reason: One of ``"empty_transcription"`` (STT returned empty
            text — user never spoke), ``"rejected_transcription"``
            (STT engine rejected via hallucination filter / compression
            ratio / timeout), or ``"sub_confidence"`` (confidence below
            ``false_wake_min_confidence`` band-aid #46 gate).
        mind_id: Phase 8 / T8.9 — mind identifier for per-mind
            false-fire attribution. Empty string default preserves
            the v0.30.0 single-mind contract; multi-mind v0.31.0+
            deployments pass the routed mind ID so dashboards can
            split per-mind false-fire rates. Cardinality bounded
            by the operator's MindRegistry (typical 3-10 minds).
    """
    get_metrics().voice_wake_word_false_fire.add(
        1,
        attributes={"reason": reason, "mind_id": mind_id},
    )


def record_wake_word_detection_method(
    *,
    method: str,
    mind_id: str = "",
) -> None:
    """Increment the T8.19 detection-method counter.

    Fires at every confirmed wake-word detection, labeled by which
    detector class fired. Operator pilot signal for the T8.18
    hot-swap window — when a newly-named mind moves from
    ``stt_fallback`` to ``onnx``, the rate ratio shifts and
    operators see latency improve correspondingly in the
    ``voice.wake_word.detection_latency`` histogram.

    Args:
        method: ``"onnx"`` (standard WakeWordDetector via OpenWakeWord
            ONNX inference; ~5 ms/frame) or ``"stt_fallback"`` (STT-
            based fallback when no ONNX model trained yet for this
            mind; ~500 ms latency).
        mind_id: Phase 8 / T8.9 mind identifier for per-mind
            attribution. Empty default preserves the v0.30.0
            single-mind contract.
    """
    get_metrics().voice_wake_word_detection_method.add(
        1,
        attributes={"method": method, "mind_id": mind_id},
    )


def record_wake_word_resolution_strategy(
    *,
    strategy: str,
    mind_id: str = "",
) -> None:
    """Increment the T8.12 resolution-strategy counter.

    Fires once at mind boot when the WakeWordModelResolver picks a
    model path (or decides there's no usable pretrained model).
    Operator dashboards aggregate the ratio:

    * ``exact`` ratio high → operator's pretrained pool aligns
      with their mind names (deliberate naming).
    * ``phonetic`` ratio high → many minds use names without
      exact pretrained models — candidates for T8.13 custom
      training to drop ~80 ms ONNX vs. fall-back latency.
    * ``none`` ratio high → empty/sparse pool; either populate
      it (T8.11) or accept the STT-fallback latency (~500 ms).

    Args:
        strategy: ``"exact"``, ``"phonetic"``, or ``"none"``.
            Matches :class:`WakeWordResolutionStrategy` values.
        mind_id: Per-mind attribution.
    """
    get_metrics().voice_wake_word_resolution_strategy.add(
        1,
        attributes={"strategy": strategy, "mind_id": mind_id},
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


__all__ = [
    "_bucket_score",
    "record_wake_word_confidence",
    "record_wake_word_detection_method",
    "record_wake_word_detection_ms",
    "record_wake_word_false_fire",
    "record_wake_word_fast_path_engaged",
    "record_wake_word_resolution_strategy",
    "record_wake_word_stage1_inference_ms",
    "record_wake_word_stage2_collection_ms",
    "record_wake_word_stage2_verifier_ms",
]
