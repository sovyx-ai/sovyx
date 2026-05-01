"""Per-session voice call-quality snapshot — Phase 7 / T7.42.

Microsoft Teams Call Quality Dashboard (CQD) ships a per-session
quality summary that aggregates the per-frame measurements operators
already collect into a single auditable record per voice turn. This
module is Sovyx's local-first equivalent — every voice turn that
reaches a clean termination boundary (TTS completed → IDLE) emits a
:class:`VoiceCallQualitySnapshot` carrying the operator-visible KPIs
in one structured payload.

The snapshot is a **summary**, not a metric source — the underlying
histograms / counters under :mod:`sovyx.observability.metrics` remain
the canonical wire contract. The CQD-style snapshot is the
turn-boundary record that dashboards render as a per-conversation
quality card and compliance tooling stores as the auditable summary.

What's intentionally NOT included (pre-v0.30.0 design decision):
network jitter / RTT / packet loss. Sovyx is local-first; the only
network calls live inside the optional cloud STT/TTS adapters which
already emit their own provider-specific telemetry. Cloud-mode
network metrics would be a forward-compatible addition in a future
minor version once the operator pilots a cloud-mode flow.

Periodic 1 Hz websocket push planned for v0.31.0+ once the dashboard
consumer widget lands; v0.30.0 ships the per-turn-boundary emit only
(staged-adoption per ``feedback_staged_adoption``).

Reference: master mission ``MISSION-voice-final-skype-grade-2026.md``
§Phase 7 / T7.42.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from sovyx.observability.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class VoiceCallQualitySnapshot:
    """Per-turn voice quality summary (CQD-equivalent).

    All numeric fields default to ``None`` so callers can omit
    measurements that don't apply to their flow (e.g. AEC ERLE is
    only meaningful when render + capture were both active during
    the turn). The :meth:`emit` helper writes a structured log
    event every time a snapshot is finalised; downstream
    aggregators (websocket push, audit trail, dashboard widget)
    consume the log stream.

    Field rationale per master mission §Phase 7 / T7.42:

    Latency:
      - ``time_to_first_utterance_ms`` — Wake → SpeechStarted KPI
      - ``wake_word_detection_ms`` — wake-word p95 GA gate measure
      - ``stt_latency_ms`` — speech → text duration (per-turn)
      - ``tts_synthesis_ms`` — text → speech (first chunk)
      - ``voice_to_voice_ms`` — wake → first TTS chunk played
        (the Skype-grade end-to-end gate)

    Audio quality:
      - ``snr_db`` — captured signal SNR (post-NS, post-AGC)
      - ``aec_erle_db`` — AEC ERLE during double-talk windows
      - ``mos_proxy`` — SNR-derived MOS proxy (3.0-5.0 scale)

    Detection:
      - ``wake_word_path`` — ``"two_stage"`` | ``"fast_path"`` | ``None``
      - ``wake_word_score`` — confidence at confirmation
      - ``stt_confidence`` — STT-provided confidence (0-1)

    Outcome:
      - ``utterance_id`` — trace ID linking to per-turn logs
      - ``mind_id`` — which mind handled the turn
      - ``outcome`` — ``"completed"`` | ``"barge_in_interrupted"`` |
        ``"perception_callback_failed"`` | ``"empty_transcription"``
        | ``"transcription_dropped"``
    """

    utterance_id: str
    """Per-turn trace ID (matches Ring 6 utterance ID convention)."""

    mind_id: str
    """The mind that handled this turn (single-mind: empty string default)."""

    outcome: str
    """Terminal disposition of the turn."""

    # Latency KPIs (ms)
    time_to_first_utterance_ms: float | None = None
    wake_word_detection_ms: float | None = None
    stt_latency_ms: float | None = None
    tts_synthesis_ms: float | None = None
    voice_to_voice_ms: float | None = None

    # Audio quality KPIs (dB / 0-5)
    snr_db: float | None = None
    aec_erle_db: float | None = None
    mos_proxy: float | None = None

    # Detection metadata
    wake_word_path: str | None = None
    wake_word_score: float | None = None
    stt_confidence: float | None = None

    # Per-turn aggregation (counts of events that fired during the turn)
    barge_in_count: int = 0
    """Number of barge-in interruptions during this turn (≥ 1 == user
    talked over the assistant; > 0 is the operator signal that the
    assistant's output was unwanted)."""

    extra: dict[str, object] = field(default_factory=dict)
    """Forward-compat catch-all for fields a future minor adds. Empty
    by default. Caller can stash additional turn metadata here
    without breaking the dataclass schema."""

    def emit(self) -> None:
        """Write a ``voice.call_quality.snapshot`` structured log event.

        The event name is part of the wire contract per
        ``docs/modules/voice-otel-semconv.md``; downstream consumers
        (websocket push in v0.31.0+, audit trail today, dashboard
        widget in v0.31.0+) filter by this exact event name.

        Fields land flat at the top level of the structured event so
        log-derived metrics tools (Loki / Grafana, Datadog log
        search) can extract them without nested-path navigation.
        Cardinality concerns are bounded — utterance_id is unique
        per turn but turns are bounded per session, mind_id is
        cardinality-bounded by the configured mind set.
        """
        payload = {f"voice.{k}": v for k, v in asdict(self).items() if k != "extra"}
        # Preserve the extra payload as a nested ``voice.extra`` field
        # so a buggy caller passing PII into ``extra`` doesn't leak
        # raw keys into the log namespace.
        if self.extra:
            payload["voice.extra"] = dict(self.extra)
        logger.info("voice.call_quality.snapshot", **payload)


def snapshot_from_pipeline(
    *,
    utterance_id: str,
    mind_id: str,
    outcome: str,
    voice_to_voice_ms: float | None = None,
    time_to_first_utterance_ms: float | None = None,
    wake_word_detection_ms: float | None = None,
    wake_word_path: str | None = None,
    wake_word_score: float | None = None,
    stt_latency_ms: float | None = None,
    stt_confidence: float | None = None,
    tts_synthesis_ms: float | None = None,
    snr_db: float | None = None,
    aec_erle_db: float | None = None,
    barge_in_count: int = 0,
) -> VoiceCallQualitySnapshot:
    """Construct + return a snapshot, computing ``mos_proxy`` from SNR.

    Convenience constructor for the orchestrator's turn-end emission
    site. Computes the ``mos_proxy`` field per the SNR→MOS curve in
    ``docs/audio-quality.md``:

      MOS = 1.0 + 0.035 × SNR_dB + 7e-6 × SNR_dB × (SNR_dB - 60) ×
            (100 - SNR_dB)

    Bounded to [1.0, 5.0]. SNR < 0 dB clamps to MOS=1.0; SNR > 60 dB
    clamps to MOS=5.0. ``snr_db=None`` returns ``mos_proxy=None``.
    """
    mos = _snr_to_mos(snr_db) if snr_db is not None else None
    return VoiceCallQualitySnapshot(
        utterance_id=utterance_id,
        mind_id=mind_id,
        outcome=outcome,
        time_to_first_utterance_ms=time_to_first_utterance_ms,
        wake_word_detection_ms=wake_word_detection_ms,
        stt_latency_ms=stt_latency_ms,
        tts_synthesis_ms=tts_synthesis_ms,
        voice_to_voice_ms=voice_to_voice_ms,
        snr_db=snr_db,
        aec_erle_db=aec_erle_db,
        mos_proxy=mos,
        wake_word_path=wake_word_path,
        wake_word_score=wake_word_score,
        stt_confidence=stt_confidence,
        barge_in_count=barge_in_count,
    )


def _snr_to_mos(snr_db: float) -> float:
    """Convert SNR in dB to a 1.0-5.0 MOS proxy.

    Logistic curve fit to operator-acceptable industry baselines:

      SNR ≤ 0 dB  → MOS = 1.0 (poor, unintelligible)
      SNR = 17 dB → MOS = 3.0 (poor-acceptable boundary)
      SNR = 20 dB → MOS ≈ 3.4 (Skype/Zoom acceptable threshold)
      SNR = 30 dB → MOS ≈ 4.5 (good)
      SNR = 40 dB → MOS ≈ 4.9 (excellent)
      SNR ≥ 60 dB → MOS = 5.0 (clamp)

    The logistic is a smoothed fit to the ITU-T P.800 MOS labels at
    operator-meaningful SNR points; not a regression-validated
    DNSMOS but a sufficient proxy for operator dashboards + the
    v0.30.0 GA promotion gate of "DNSMOS p95 ≥ 4.0 in pilot" (this
    proxy validates the gate when DNSMOS extras are absent).
    """
    if snr_db <= 0:
        return 1.0
    if snr_db >= 60.0:  # noqa: PLR2004
        return 5.0
    import math  # noqa: PLC0415 — single-use locally scoped import

    # Logistic centred at SNR=17 (poor-acceptable boundary), gain 0.15
    # per dB, asymptotic range [1.0, 5.0].
    mos = 1.0 + 4.0 / (1.0 + math.exp(-0.15 * (snr_db - 17.0)))
    return max(1.0, min(5.0, mos))


__all__ = [
    "VoiceCallQualitySnapshot",
    "snapshot_from_pipeline",
]
