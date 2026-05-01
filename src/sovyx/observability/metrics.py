"""Sovyx metrics — counters, histograms, and gauges via OpenTelemetry.

Provides a thin, Sovyx-specific wrapper around the OpenTelemetry Metrics API.
All instruments are lazily created from a shared :class:`MetricsRegistry` and
can be exported via any OTel-compatible backend (Prometheus, OTLP, JSON, etc.).

Usage::

    from sovyx.observability.metrics import get_metrics

    m = get_metrics()
    m.messages_received.add(1, {"channel": "telegram"})
    m.messages_processed.add(1, {"mind_id": "nyx"})

    with m.measure_latency(m.llm_response_latency):
        response = await provider.generate(...)

Design decisions:

- **No global singletons** — :func:`setup_metrics` creates and returns
  a :class:`MetricsRegistry`.  :func:`get_metrics` retrieves the active
  instance (or a no-op stub if metrics are disabled).
- **Attribute cardinality** — all instruments accept optional attribute
  dicts.  Keep cardinality low (channel, provider, model, mind_id).
- **Unit conventions** — latencies in milliseconds (``ms``), costs in
  USD, sizes in bytes.
"""

from __future__ import annotations

import contextlib
import time
from typing import TYPE_CHECKING, Any

from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    InMemoryMetricReader,
    MetricReader,
)

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Generator


logger = get_logger(__name__)


# ── Metrics Registry ────────────────────────────────────────────────────────

_METER_NAME = "sovyx"
# Tracks the observability API version (not the package version in pyproject.toml).
# Bump when instrument names, units, or attribute schemas change.
_METER_VERSION = "0.2.0"

# Phase 11+ §22.7 — global cardinality budget. Operators override via
# SOVYX_OBSERVABILITY__METRICS_MAX_SERIES. Once total (metric, label-tuple)
# pairs reach this number, new label combinations are folded into a
# single ``_overflow=true`` series per metric and a one-shot WARNING is
# emitted (``metrics.cardinality.exceeded``). Existing series keep
# updating normally, and the overflow series itself is one extra
# (label_tuple = (("_overflow", "true"),)) per metric.
DEFAULT_MAX_SERIES: int = 10_000

# Single attribute swap used when a metric blows past the budget. Picked
# to be obviously synthetic so dashboards don't mistake it for a real
# label and Prometheus queries can ``{_overflow="true"}`` to find the
# blown counters.
_OVERFLOW_ATTRS: dict[str, str] = {"_overflow": "true"}
_OVERFLOW_KEY: tuple[tuple[str, str], ...] = (("_overflow", "true"),)


def _attr_key(attributes: dict[str, str] | None) -> tuple[tuple[str, str], ...]:
    """Canonical tuple key for a label set — sorted for deterministic identity.

    OTel allows attribute dicts with identical contents in any order;
    Prometheus treats them as the same series. We mirror that by
    sorting the (key, value) pairs before hashing them into the
    ``CardinalityBudget._seen`` set.
    """
    if not attributes:
        return ()
    # str() the value so int / bool labels collapse to the same series
    # the OTel SDK would generate when it stringifies them downstream.
    return tuple(sorted((str(k), str(v)) for k, v in attributes.items()))


class CardinalityBudget:
    """Tracks per-metric label-tuple usage against a global series budget.

    Sovyx uses a *global* budget rather than per-metric quotas because
    operational reality is one badly-instrumented label (e.g. a user_id
    label on a counter) can exhaust an entire fleet's scrape memory.
    A single ceiling makes the failure mode loud and bounded — once
    reached, new label combinations are silently folded into an
    ``_overflow=true`` series per metric, and a WARNING fires once
    per metric so the operator knows where to look.

    Thread-safety: dict / set mutations under CPython's GIL are atomic
    for single-key operations; each ``check()`` call performs at most
    one ``set.add`` and one ``dict[k] = set`` reassignment, so no lock
    is needed in the hot path.

    Args:
        max_series: Hard ceiling on total ``(metric, label-tuple)``
            pairs across the entire registry. Defaults to
            :data:`DEFAULT_MAX_SERIES` (10 000).
    """

    __slots__ = ("_max_series", "_overflow_warned", "_seen")

    def __init__(self, max_series: int = DEFAULT_MAX_SERIES) -> None:
        self._max_series = max_series
        self._seen: dict[str, set[tuple[tuple[str, str], ...]]] = {}
        self._overflow_warned: set[str] = set()

    @property
    def max_series(self) -> int:
        """The configured global ceiling — read-only after construction."""
        return self._max_series

    @property
    def total_series(self) -> int:
        """Sum of distinct label-tuples across every tracked metric."""
        return sum(len(s) for s in self._seen.values())

    def check(
        self,
        metric_name: str,
        attributes: dict[str, str] | None,
    ) -> dict[str, str] | None:
        """Decide whether ``attributes`` is admissible for ``metric_name``.

        - If ``attributes`` is already known for this metric, return it
          unchanged (cheap path — one set membership check).
        - If new but the budget is not yet exhausted, record it and
          return it unchanged.
        - If new *and* the budget is exhausted, emit the
          ``metrics.cardinality.exceeded`` WARNING (once per metric)
          and return :data:`_OVERFLOW_ATTRS` so the underlying
          instrument folds the data point into a single overflow
          series.

        Returns:
            The attributes dict to forward to the OTel instrument.
            Either the original (admitted) or the overflow stub
            (rejected, folded).
        """
        key = _attr_key(attributes)
        seen = self._seen.setdefault(metric_name, set())

        if key in seen:
            return attributes
        # New label tuple — admit only if there's headroom.
        if self.total_series < self._max_series:
            seen.add(key)
            return attributes

        # Budget exhausted. Make sure the overflow stub itself is
        # tracked exactly once per metric (so the report counts it
        # as 1 series, not N) and warn once per metric.
        if _OVERFLOW_KEY not in seen:
            seen.add(_OVERFLOW_KEY)
        if metric_name not in self._overflow_warned:
            self._overflow_warned.add(metric_name)
            logger.warning(
                "metrics.cardinality.exceeded",
                metric=metric_name,
                max_series=self._max_series,
                total_series=self.total_series,
                dropped_attributes=dict(key) if key else {},
            )
        return _OVERFLOW_ATTRS

    def report(self, top_n: int = 20) -> list[dict[str, Any]]:
        """Return the top-``top_n`` metrics ranked by series count.

        Used by ``GET /api/observability/metrics/cardinality`` so an
        operator can see which metric is on track to blow the budget
        before the budget fires. Each entry is a dict with ``metric``,
        ``series_count``, and ``overflow`` (true if the metric has
        already been folded into the overflow series at least once).
        """
        ranked = sorted(
            self._seen.items(),
            key=lambda kv: len(kv[1]),
            reverse=True,
        )
        return [
            {
                "metric": name,
                "series_count": len(label_set),
                "overflow": name in self._overflow_warned,
            }
            for name, label_set in ranked[:top_n]
        ]


class _BudgetedInstrument:
    """Proxy around an OTel instrument that gates writes through the budget.

    Exposes both ``add`` (counter contract) and ``record`` (histogram
    contract) — call sites already pick the right one for their
    instrument type, and the unused method on the wrong instrument
    type would raise ``AttributeError`` from the underlying OTel
    instrument exactly as before.

    Attributes are filtered through :meth:`CardinalityBudget.check`
    on every call. The hot path is one set membership test for
    already-known label tuples (the steady-state case).
    """

    __slots__ = ("_budget", "_instrument", "_name")

    def __init__(
        self,
        *,
        name: str,
        instrument: Any,  # noqa: ANN401 — opaque OTel Counter/Histogram/Gauge.
        budget: CardinalityBudget,
    ) -> None:
        self._name = name
        self._instrument = instrument
        self._budget = budget

    def add(
        self,
        amount: float,
        attributes: dict[str, str] | None = None,
    ) -> None:
        """Forward an ``add()`` call after enforcing the cardinality budget."""
        gated = self._budget.check(self._name, attributes)
        self._instrument.add(amount, attributes=gated)

    def record(
        self,
        value: float,
        attributes: dict[str, str] | None = None,
    ) -> None:
        """Forward a ``record()`` call after enforcing the cardinality budget."""
        gated = self._budget.check(self._name, attributes)
        self._instrument.record(value, attributes=gated)

    @property
    def name(self) -> str:
        """The metric name (matches the OTel instrument's ``name`` attribute)."""
        return self._name


# Module-level reference set by setup_metrics / reset by teardown_metrics.
_active_registry: MetricsRegistry | None = None


class MetricsRegistry:
    """Central registry of all Sovyx metrics instruments.

    All counters, histograms, and gauges are created once in ``__init__``
    and reused for the lifetime of the application.

    Attributes (Counters):
        messages_received: Messages received from channels (label: channel).
        messages_processed: Messages fully processed through cognitive loop (label: mind_id).
        llm_calls: Total LLM provider calls (label: provider, model).
        errors: Total errors by category (label: error_type, module).
        tokens_used: Total tokens consumed (label: direction=in|out, provider).

    Attributes (Histograms):
        llm_response_latency: LLM call latency in ms.
        cognitive_loop_latency: Full cognitive loop latency in ms.
        brain_search_latency: Brain semantic search latency in ms.
        context_assembly_latency: Context assembly latency in ms.

    Attributes (Counters — cost):
        llm_cost: Cumulative LLM cost in USD.
    """

    def __init__(
        self,
        meter: metrics.Meter,
        *,
        max_series: int = DEFAULT_MAX_SERIES,
    ) -> None:
        self._meter = meter
        self._budget = CardinalityBudget(max_series=max_series)

        # ── Counters ────────────────────────────────────────────────
        self.messages_received = self._counter(
            "sovyx.messages.received",
            "Total messages received from channels (inbound)",
        )

        self.messages_processed = self._counter(
            "sovyx.messages.processed",
            "Total messages fully processed through the cognitive loop",
        )

        self.llm_calls = self._counter(
            "sovyx.llm.calls",
            "Total LLM provider calls",
        )

        self.errors = self._counter(
            "sovyx.errors",
            "Total errors by category",
        )

        self.tokens_used = self._counter(
            "sovyx.llm.tokens",
            "Total tokens consumed",
        )

        self.llm_cost = self._counter(
            "sovyx.llm.cost",
            "Cumulative LLM cost",
            unit="USD",
        )

        self.concepts_created = self._counter(
            "sovyx.brain.concepts.created",
            "Total concepts created in brain memory",
        )

        self.episodes_encoded = self._counter(
            "sovyx.brain.episodes.encoded",
            "Total episodes encoded into brain memory",
        )

        self.safety_blocks = self._counter(
            "sovyx.safety.blocks",
            "Total safety filter blocks (label: direction, tier, category)",
        )

        self.safety_pii_redacted = self._counter(
            "sovyx.safety.pii.redacted",
            "Total PII redactions in output (label: type)",
        )

        self.safety_llm_classifications = self._counter(
            "sovyx.safety.llm.classifications",
            "Total LLM safety classifications (label: result, method, category)",
        )

        # ── Histograms ─────────────────────────────────────────────
        self.safety_filter_latency = self._histogram(
            "sovyx.safety.filter.latency",
            "Safety filter latency per direction (label: direction)",
        )

        self.llm_response_latency = self._histogram(
            "sovyx.llm.latency",
            "LLM call latency",
        )

        self.cognitive_loop_latency = self._histogram(
            "sovyx.cognitive.latency",
            "Full cognitive loop latency (perceive to act)",
        )

        self.brain_search_latency = self._histogram(
            "sovyx.brain.search.latency",
            "Brain semantic search latency",
        )

        self.context_assembly_latency = self._histogram(
            "sovyx.context.assembly.latency",
            "Context assembly latency",
        )

        self.safety_llm_classify_latency = self._histogram(
            "sovyx.safety.llm.classify.latency",
            "LLM safety classifier latency per call",
        )

        # ── Voice device test (setup-wizard meter + TTS playback) ─
        self.voice_test_sessions = self._counter(
            "sovyx.voice.test.sessions",
            "Total voice-test meter sessions (label: result)",
        )

        self.voice_test_clipping_events = self._counter(
            "sovyx.voice.test.clipping.events",
            "Voice-test meter frames flagged as clipping",
        )

        self.voice_test_stream_open_latency = self._histogram(
            "sovyx.voice.test.stream.open.latency",
            "Latency from WS accept to first LevelFrame emitted",
        )

        self.voice_test_output_synthesis_ms = self._histogram(
            "sovyx.voice.test.output.synthesis.latency",
            "TTS synthesis latency for the voice-test playback job",
        )

        self.voice_test_output_playback_ms = self._histogram(
            "sovyx.voice.test.output.playback.latency",
            "Sink playback latency for the voice-test job",
        )

        # Unified stream opener — one counter increment per attempted
        # (host_api, auto_convert, result) triple so field debugging can
        # answer "which host API + auto_convert combination is the mic
        # actually landing on in the wild?" without parsing logs.
        self.voice_stream_open_attempts = self._counter(
            "sovyx.voice.stream.open.attempts",
            "Stream-opener attempts (labels: host_api, auto_convert, "
            "kind=input|output, result=ok|silent|error, error_code)",
        )

        # ── Model downloader telemetry ──────────────────────────────
        # Answers: "did the primary URL fail often enough that our
        # mirrors were actually useful?" and "how often are users
        # hitting cooldowns / checksum drift?" without log scraping.
        # Labels are intentionally low-cardinality:
        #   - model: silero_vad.onnx, e5-small-v2.onnx, kokoro-v1.0.int8.onnx, ...
        #   - source: primary | mirror-1 | mirror-2 | ...
        #   - result: ok | transient | permanent
        #   - error_type: exception class name (HTTPStatusError, ChecksumMismatch, ...)
        self.model_download_attempts = self._counter(
            "sovyx.model.download.attempts",
            "Model download attempts (labels: model, source=primary|mirror-N, "
            "result=ok|transient|permanent, error_type)",
        )

        # ── Voice Capture Health Lifecycle (ADR §5.8) ──────────────
        # Stable metric names — see docs-internal/ADR-voice-capture-health-lifecycle.md.
        # Labels are kept low-cardinality so Prometheus series counts stay bounded
        # even in long-running daemons with many hot-plug events.
        self.voice_health_cascade_attempts = self._counter(
            "sovyx.voice.health.cascade.attempts",
            "Cascade attempts against an endpoint (labels: platform, "
            "host_api, success=true|false, source=pinned|store|cascade)",
        )
        self.voice_health_combo_store_hits = self._counter(
            "sovyx.voice.health.combo_store.hits",
            "ComboStore fast-path resolutions (labels: endpoint_class, "
            "result=hit|miss|needs_revalidation)",
        )
        self.voice_health_combo_store_invalidations = self._counter(
            "sovyx.voice.health.combo_store.invalidations",
            "ComboStore invalidations (labels: reason — §4.1 rule tags)",
        )
        self.voice_health_probe_diagnosis = self._counter(
            "sovyx.voice.health.probe.diagnosis",
            "Probe outcomes (labels: diagnosis — Diagnosis enum value, mode=cold|warm)",
        )
        self.voice_health_probe_duration = self._histogram(
            "sovyx.voice.health.probe.duration",
            "Probe wall-clock duration (label: mode=cold|warm)",
        )
        self.voice_health_preflight_failures = self._counter(
            "sovyx.voice.health.preflight.failures",
            "Pre-flight step failures (labels: step — PreflightStep value, "
            "code — PreflightStepCode value)",
        )
        self.voice_health_recovery_attempts = self._counter(
            "sovyx.voice.health.recovery.attempts",
            "Watchdog recovery triggers (labels: trigger=deaf_backoff|"
            "hotplug|default_change|power|audio_service)",
        )
        self.voice_health_self_feedback_blocks = self._counter(
            "sovyx.voice.health.self_feedback.blocks",
            "Self-feedback isolation blocks (label: layer=gate|duck|spectral)",
        )
        self.voice_health_active_endpoint_changes = self._counter(
            "sovyx.voice.health.active_endpoint.changes",
            "Active endpoint swaps (label: reason=hotplug|default|manual|recovery)",
        )
        self.voice_health_kernel_invalidated_events = self._counter(
            "sovyx.voice.health.kernel_invalidated.events",
            "Kernel-side IAudioClient invalidation events detected by "
            "the probe (labels: platform, host_api, action=quarantine|"
            "failover|recheck_recovered|recheck_still_invalid)",
        )
        self.voice_health_probe_start_time_errors = self._counter(
            "sovyx.voice.health.probe.start_time_errors",
            "Probe stream.start() failures classified into Diagnosis "
            "values (labels: diagnosis, host_api, platform). Before "
            "v0.20.2 these bypassed the probe classifier and appeared "
            "as generic DRIVER_ERROR in cascade logs.",
        )
        self.voice_capture_exclusive_restart_verdicts = self._counter(
            "sovyx.voice.capture.exclusive_restart.verdicts",
            "request_exclusive_restart() outcomes classified into "
            "ExclusiveRestartVerdict values (labels: verdict, host_api, "
            "platform). v0.20.2 / Bug C — before this metric the "
            "restart was opaque: the dashboard logged 'ok' even when "
            "WASAPI silently downgraded the stream to shared mode "
            "with the APO chain still active.",
        )
        self.voice_capture_shared_restart_verdicts = self._counter(
            "sovyx.voice.capture.shared_restart.verdicts",
            "request_shared_restart() outcomes classified into "
            "SharedRestartVerdict values (labels: verdict, host_api, "
            "platform). Symmetric twin of exclusive_restart.verdicts — "
            "the revert path used when a PlatformBypassStrategy rolls "
            "back an exclusive-mode bypass (APPLIED_STILL_DEAD, revert "
            "on coordinator teardown).",
        )
        self.voice_health_apo_degraded_events = self._counter(
            "sovyx.voice.health.apo_degraded.events",
            "APO-degraded endpoint lifecycle events emitted by the "
            "CaptureIntegrityCoordinator + watchdog recheck loop "
            "(labels: platform, action=quarantine|failover|"
            "recheck_recovered|recheck_still_invalid|hotplug_clear). "
            "Mirrors the kernel_invalidated.events metric but scoped "
            "to the Windows Voice Clarity / VocaEffectPack APO cluster "
            "and any future user-mode DSP failure mode.",
        )
        self.voice_health_bypass_strategy_verdicts = self._counter(
            "sovyx.voice.health.bypass_strategy.verdicts",
            "Per-strategy outcomes from CaptureIntegrityCoordinator "
            "(labels: strategy, verdict=applied_healthy|"
            "applied_still_dead|failed_to_apply|not_applicable, "
            "reason). Operators read this to tell which bypass path "
            "actually cures a given endpoint fleet-wide — feeds the "
            "Phase 4 hardware-fingerprint catalog confidence gate.",
        )
        self.voice_health_capture_integrity_verdicts = self._counter(
            "sovyx.voice.health.capture_integrity.verdicts",
            "Warm integrity-probe classifications (labels: verdict="
            "healthy|apo_degraded|driver_silent|vad_mute|inconclusive, "
            "phase=pre_bypass|post_bypass|recheck). OS-agnostic "
            "degradation signal derived from RMS + spectral flatness "
            "+ energy rolloff + Silero VAD max probability.",
        )
        self.voice_health_bypass_probe_wait_ms = self._histogram(
            "sovyx.voice.health.bypass.probe_wait_ms",
            "Wall-clock time the CaptureIntegrityCoordinator waited in "
            "tap_frames_since_mark between strategy.apply() returning "
            "and post-apply frames being ready for classification "
            "(labels: strategy). Introduced by v1.3 §14.E1 as the "
            "primary telemetry feeding the probe_jitter_margin_s review "
            "gate — p95 rising above "
            "(integrity_probe_duration_s + probe_jitter_margin_s) * 1000 "
            "signals an under-provisioned jitter margin.",
        )
        self.voice_health_bypass_probe_window_contaminated = self._counter(
            "sovyx.voice.health.bypass.probe_window_contaminated",
            "Fires when tap_frames_since_mark returned fewer than "
            "min_samples post-apply frames within max_wait_s (labels: "
            "strategy). A non-zero count means the coordinator fell back "
            "to classifying an undersized sample — the tuple mark still "
            "prevented pre-apply contamination, but the verdict carries "
            "reduced statistical weight.",
        )
        self.voice_health_bypass_improvement_resolution = self._counter(
            "sovyx.voice.health.bypass.improvement_resolution",
            "Counts APPLIED_HEALTHY verdicts reached via the v1.3 §14.E2 "
            "improvement heuristic — post-apply verdict is VAD_MUTE but "
            "spectral rolloff improved by >= improvement_rolloff_factor. "
            "Paired with a follow-up deaf-within-60s counter (future) to "
            "compute Type I/II error rates of the heuristic.",
        )
        self.voice_health_time_to_first_utterance = self._histogram(
            "sovyx.voice.health.time_to_first_utterance",
            "User-perceived KPI — latency from WakeWordDetectedEvent to "
            "SpeechStartedEvent. ADR §5.14 target p95 ≤ 200 ms.",
        )
        # Phase 7 / T7.1 — wake-word stage-by-stage latency profile.
        # The 4 histograms together decompose the end-to-end "user spoke
        # the wake word" → "pipeline observed WakeWordDetectedEvent"
        # latency into operator-actionable stages so the v0.30.0 GA
        # promotion gate (wake-word p95 ≤ 500 ms — matches Alexa /
        # Google / Siri industry benchmark) can be tracked directly
        # against each stage's contribution. Master mission
        # MISSION-voice-final-skype-grade-2026.md §Phase 7 / T7.1.
        self.voice_wake_word_stage1_inference_latency = self._histogram(
            "sovyx.voice.wake_word.stage1_inference_latency",
            "Wake-word stage-1 ONNX inference duration in ms. Per-frame "
            "(80 ms at 16 kHz) — typically ~5 ms on Pi 5 / ~1 ms on N100. "
            "Sustained p99 > 50 ms indicates host CPU saturation that "
            "will starve the audio callback. Labels: model_name (the "
            "ONNX checkpoint stem; cardinality-bounded by the small "
            "set of installed wake-word variants).",
        )
        self.voice_wake_word_stage2_collection_latency = self._histogram(
            "sovyx.voice.wake_word.stage2_collection_latency",
            "Wake-word stage-2 audio-collection window duration in ms. "
            "Wall-clock from STAGE1_TRIGGERED entry to _evaluate_stage2 "
            "call — typically the configured stage2_window_seconds "
            "(default 1500 ms) plus per-frame jitter. T7.2 reduces "
            "the window from 1500 ms → 500 ms; this histogram is the "
            "before/after measurement. Labels: outcome=confirmed|"
            "rejected_threshold|rejected_verifier.",
        )
        self.voice_wake_word_stage2_verifier_latency = self._histogram(
            "sovyx.voice.wake_word.stage2_verifier_latency",
            "Wake-word stage-2 verifier (STT call) duration in ms. "
            "Only emitted on stage-2 evaluations where peak_score ≥ "
            "stage2_threshold (otherwise verifier never runs). "
            "T7.5 replaces STT with phoneme matching; this histogram "
            "is the before/after measurement. Labels: outcome="
            "verified|rejected.",
        )
        self.voice_wake_word_detection_latency = self._histogram(
            "sovyx.voice.wake_word.detection_latency",
            "Wake-word end-to-end detection latency in ms. Wall-clock "
            "from STAGE1_TRIGGERED entry to wake_word_detected event "
            "emission (only emitted on confirmed detections). Sums "
            "stage-2 collection + verifier durations + state-machine "
            "overhead. The v0.30.0 GA promotion gate target is p95 "
            "≤ 500 ms (Alexa/Google/Siri parity).",
        )
        self.voice_opener_attempts = self._counter(
            "sovyx.voice.opener.attempts",
            "Per-attempt outcomes of open_input_stream pyramid (labels: "
            "host_api, error_code=none|device_busy|unsupported_samplerate|"
            "device_not_found|permission_denied|internal_error|..., "
            "result=ok|fail). Introduced by voice-linux-cascade-root-fix "
            "T1 to make opener fallbacks queryable alongside cascade "
            "attempts.",
        )

        # ── Voice Windows Paranoid Mission (v0.24.0 → v0.26.0) ─────
        # See docs-internal/missions/MISSION-voice-windows-paranoid-2026-04-26.md
        # and docs-internal/ADR-voice-bypass-tier-system.md +
        # ADR-voice-cascade-runtime-alignment.md +
        # ADR-voice-imm-notification-recovery.md.
        # Names are stable wire contracts — downstream dashboards /
        # Grafana queries / Prometheus alerts depend on them.
        self.voice_health_probe_cold_silence_rejected = self._counter(
            "sovyx.voice.health.probe.cold_silence_rejected",
            "Cold-probe silence-rejection events (labels: mode="
            "strict_reject|lenient_passthrough, host_api). Furo W-1 "
            "telemetry — when the lenient counter rate matches the "
            "predicted silent-combo population on Voice Clarity rigs "
            "the operator can flip "
            "probe_cold_strict_validation_enabled=true safely.",
        )
        self.voice_health_bypass_tier1_raw_attempted = self._counter(
            "sovyx.voice.health.bypass.tier1_raw.attempted",
            "Tier 1 RAW + Communications bypass attempts on Windows "
            "(labels: host_api, raw_supported=true|false). Pairs with "
            "voice.health.bypass.tier1_raw.outcome for the Phase 2 → "
            "Phase 3 promotion gate (Tier 1 success ≥60% on VC machines).",
        )
        self.voice_health_bypass_tier1_raw_outcome = self._counter(
            "sovyx.voice.health.bypass.tier1_raw.outcome",
            "Tier 1 RAW bypass outcomes on Windows (labels: verdict="
            "raw_engaged|property_rejected_by_driver|"
            "open_failed_no_stream|open_failed_fallback_to_plain|"
            "not_running|not_win32, host_api). raw_engaged is the "
            "success terminal state.",
        )
        self.voice_health_bypass_tier2_host_api_rotate_attempted = self._counter(
            "sovyx.voice.health.bypass.tier2_host_api_rotate.attempted",
            "Tier 2 host_api_rotate bypass Phase A (rotate) attempts "
            "on Windows (labels: source_host_api, target_host_api). "
            "Fires from WindowsHostApiRotateThenExclusiveBypass.apply "
            "when source_host_api ∈ {MME, Windows DirectSound, Windows "
            "WDM-KS}.",
        )
        self.voice_health_bypass_tier2_host_api_rotate_outcome = self._counter(
            "sovyx.voice.health.bypass.tier2_host_api_rotate.outcome",
            "Tier 2 host_api_rotate combined outcomes on Windows "
            "(labels: phase_a_verdict — HostApiRotateVerdict, "
            "phase_b_verdict — ExclusiveRestartVerdict or 'skipped' "
            "when Phase A failed, resulting_host_api). Promotion gate: "
            "rotated_then_exclusive_engaged ≥70% on machines where "
            "Tier 1 failed.",
        )
        self.voice_opener_host_api_alignment = self._counter(
            "sovyx.voice.opener.host_api_alignment",
            "Cascade ↔ runtime opener alignment SLI (labels: aligned="
            "true|false, cascade_winner_host_api, "
            "runtime_chain_head_host_api). Furo W-4 telemetry — target "
            "is 100% aligned; any drift signals the bug. NOT a "
            "tunable — drift is a bug.",
        )
        self.voice_hotplug_listener_registered = self._counter(
            "sovyx.voice.hotplug.listener.registered",
            "IMMNotificationClient registration health (labels: "
            "registered=true|false, error). Fires once at "
            "AudioCaptureTask.start when "
            "mm_notification_listener_enabled=True. Promotion gate: "
            "registration success ≥99% on Win10/11 before flipping "
            "the listener flag default to True in v0.26.0.",
        )

        # ── Phase 5 / T5.49 + new mission Part 6 — driver-update detection ──
        # Counter that fires every time the WMI listener (T5.49,
        # `fb815a3`) detects an audio driver modification. Labels
        # split the counter by what the handler decided to do:
        #   action=detected  — the always-emit "saw the event" baseline
        #   action=skipped   — recascade flag disabled (lenient mode)
        #   action=triggered — recascade flag enabled (would-trigger today;
        #                      future commits wire actual cascade re-run)
        # Operators correlate this counter against
        # `voice.health.deaf.warnings_total` for forensic
        # attribution: a deaf-signal spike co-occurring with an
        # ``action=detected`` event points at a regressed driver
        # release rather than at Sovyx's bypass logic.
        self.voice_driver_update_detected = self._counter(
            "sovyx.voice.driver_update.detected",
            "Audio driver modification events from the WMI listener "
            "(labels: action=detected|skipped|triggered). Operators "
            "correlate spikes with deaf-signal incidents to attribute "
            "them to driver releases. Fires only on Windows when "
            "audio_driver_update_listener_enabled=True.",
        )

        # ── Phase 4 / T4.7-T4.8 — AEC observability ─────────────────
        # ERLE histogram + windows counter give the dashboard a real
        # picture of acoustic-echo cancellation quality in production.
        # Promotion gate (master mission §Phase 4): ERLE ≥ 30 dB
        # sustained when render+capture both active. The histogram's
        # p50 / p95 surfaces this directly; the counter shows what
        # fraction of capture windows actually had echo to cancel.
        self.voice_aec_erle_db = self._histogram(
            "sovyx.voice.aec.erle_db",
            "Per-window Echo Return Loss Enhancement in dB (ITU-T "
            "G.168 §6.2). Fires once per emitted 512-sample capture "
            "window when AEC is wired AND the render reference is "
            "non-silent (silent windows skip ERLE since echo is "
            "undefined when there's no reference signal). Promotion "
            "gate: p50 ≥ 35 dB, p95 ≥ 30 dB sustained.",
            unit="dB",
        )
        self.voice_aec_windows = self._counter(
            "sovyx.voice.aec.windows",
            "Per-window AEC processing counter (labels: state="
            "processed|render_silent). 'processed' fires when AEC "
            "engaged (render non-silent + filter ran); "
            "'render_silent' fires when AEC short-circuited "
            "(SpeexAecProcessor early-returns on zero-render to "
            "avoid filter drift). The processed/total ratio reveals "
            "how often the AEC stage actually had echo to cancel.",
        )
        self.voice_aec_double_talk = self._counter(
            "sovyx.voice.aec.double_talk",
            "Per-window double-talk detector verdict (labels: state="
            "detected|absent|undecided). 'detected' fires when the "
            "DoubleTalkDetector's NCC < threshold (user speaking "
            "during TTS); 'absent' fires when NCC ≥ threshold (pure "
            "echo, AEC filter can converge cleanly); 'undecided' "
            "fires when either signal is silent (NCC undefined).",
        )
        self.voice_pipeline_noise_floor_drift_alerts = self._counter(
            "sovyx.voice.pipeline.noise_floor_drift_alerts",
            "Per-mind orchestrator noise-floor drift alert state-"
            "transition counter (Phase 4 T4.38 — labels: state="
            "warned|cleared). 'warned' fires once when consecutive "
            "heartbeats with rolling-window noise-floor rise above "
            "voice_noise_floor_drift_threshold_db reaches "
            "voice_noise_floor_drift_consecutive_heartbeats; "
            "'cleared' fires once when the next clean heartbeat "
            "resolves the incident. The pair is symmetric so "
            "dashboards graph open drift incidents as warned − "
            "cleared.",
        )
        self.voice_pipeline_snr_low_alerts = self._counter(
            "sovyx.voice.pipeline.snr_low_alerts",
            "Per-mind orchestrator SNR low-alert state-transition "
            "counter (Phase 4 T4.35 — labels: state=warned|cleared). "
            "'warned' fires once when consecutive heartbeats with "
            "SNR p50 below voice_snr_low_alert_threshold_db reaches "
            "voice_snr_low_alert_consecutive_heartbeats; 'cleared' "
            "fires once when the next clean heartbeat resolves the "
            "incident. The pair is symmetric (warned == cleared "
            "in steady state) so dashboards graph open SNR-low "
            "incidents as warned − cleared.",
        )
        self.voice_vad_quiet_signal_gated = self._counter(
            "sovyx.voice.vad.quiet_signal_gated",
            "Per-frame VAD quiet-signal gate verdict (Phase 4 T4.39 — "
            "labels: state=gated|would_gate). 'gated' fires when the "
            "anti-hallucination gate clamped probability to 0.0 "
            "because the frame paradoxically combined low RMS "
            "(<gate_rms_dbfs) with high speech probability "
            "(>gate_prob_threshold). 'would_gate' fires on the same "
            "paradox when quiet_signal_gate_enabled=False so the "
            "rate is observable before flipping the action.",
        )
        self.voice_aec_bypass_combo = self._counter(
            "sovyx.voice.aec.bypass_combo",
            "Boot-time AEC + WASAPI-exclusive combo detector "
            "(Phase 4 T4.6 — labels: state=safe_shared|"
            "safe_engaged|safe_belt_and_suspenders|dangerous|"
            "auto_engaged). Fires once per voice pipeline "
            "construction. 'dangerous' = exclusive bypasses OS "
            "AEC AND in-process AEC is off — TTS leaks into ASR. "
            "'auto_engaged' = same combo but voice_aec_auto_engage_"
            "on_exclusive flipped True so the factory force-"
            "engaged AEC.",
        )

        # ── Phase 4 / T4.16 — NS observability ──────────────────────
        # Mirrors voice.aec.windows + voice.aec.erle_db for the
        # noise-suppression stage. The pair gives the dashboard
        # symmetric AEC + NS quality panels.
        self.voice_ns_windows = self._counter(
            "sovyx.voice.ns.windows",
            "Per-window NS processing counter (labels: state="
            "processed|passthrough). 'processed' fires when NS "
            "actually attenuated the window (suppression > 0.5 dB); "
            "'passthrough' fires when NS ran but the gate found "
            "nothing to attenuate (every bin above floor). The "
            "processed/total ratio reveals how often NS engaged.",
        )
        self.voice_ns_suppression_db = self._histogram(
            "sovyx.voice.ns.suppression_db",
            "Per-window NS suppression in dB — positive values mean "
            "NS reduced the frame energy. Computed as (input_dbfs - "
            "output_dbfs) per emitted 512-sample window when NS is "
            "wired. Promotion gate (master mission §Phase 4 / T4.16): "
            "p50 ≥ 5 dB on stationary background noise.",
            unit="dB",
        )

        # ── Phase 4 / T4.33 — SNR observability ─────────────────────
        # Per-window SNR estimate from the SnrEstimator's
        # spectral-subtraction formula. Gives the dashboard a real
        # signal-vs-noise distribution per session. Promotion gate
        # (master mission §Phase 4 / T4.35): alert when p50 < 9 dB
        # (Moonshine STT degradation threshold).
        self.voice_audio_snr_db = self._histogram(
            "sovyx.voice.audio.snr_db",
            "Per-window SNR estimate in dB. Computed by the "
            "SnrEstimator's sliding-window minimum noise tracker "
            "per emitted 512-sample window when SNR estimation is "
            "wired. Excludes silent frames (those carry no signal "
            "to ratio). Bucketing tracks: 17 dB+ excellent, 9-17 dB "
            "good, <9 dB degraded (per T4.40 documentation).",
            unit="dB",
        )

        # ── Phase 4 / T4.44.b — Wiener entropy observability ─────
        # Per-frame signal-destruction verdict from the Wiener
        # entropy detector. The destroyed/total ratio reveals how
        # often the operator's hardware delivers usable audio vs
        # noise-dominated garbage; sustained > 0.5 ratio signals
        # a hardware / driver problem upstream of Sovyx.
        self.voice_audio_signal_destroyed = self._counter(
            "sovyx.voice.audio.signal_destroyed",
            "Per-frame Wiener-entropy destruction verdict (labels: "
            "state=destroyed|clean). 'destroyed' fires when frame "
            "entropy exceeds the threshold (default 0.5 — signal "
            "too noise-like for downstream DSP). 'clean' fires "
            "otherwise. Foundation: observability-only — the "
            "destroyed signal still flows through the pipeline. "
            "Future commit may add skip-action when sustained ratio "
            "indicates the operator should investigate hardware.",
        )

        # ── Phase 4 / T4.45 — resample peak-clip detector ────────
        # Counts frames where the polyphase resample stage produced
        # values ≥ ±1.0 (i.e. introduced overshoot the original
        # signal didn't have). Distinct from the R2 saturation
        # counter which counts post-multiply int16 rail hits — a
        # hot input that clipped at int16 conversion is normal,
        # but the resampler adding overshoot to a sub-full-scale
        # input is the canonical Gibbs-phenomenon signature that
        # warrants T4.42 (higher-order polyphase / sinc resampler).
        self.voice_audio_resample_peak_clip = self._counter(
            "sovyx.voice.audio.resample_peak_clip",
            "Per-frame resample peak-clip counter (labels: "
            "state=clip|clean). 'clip' fires when post-resample "
            "peak ≥ 1.0 (overshoot introduced by the resampler "
            "Gibbs phenomenon). Sustained > 1% rate signals the "
            "operator should escalate to T4.42 — upgrade to "
            "higher-order polyphase or sinc resampler.",
        )

        # ── Phase 4 / T4.46 — phase-inversion auto-recovery ─────
        # State-transition events when the FrameNormalizer's
        # phase-inversion detector latches L-only mode (after N
        # consecutive inverted blocks) or reverts to mean-downmix
        # (after M consecutive clean blocks). Operators see the
        # transition events on the dashboard for hardware-fault
        # forensics — repeated engage/revert cycles signal a
        # genuinely unstable mic that needs replacement.
        self.voice_audio_phase_inversion_recovery = self._counter(
            "sovyx.voice.audio.phase_inversion_recovery",
            "Phase-inversion auto-recovery state-transition events "
            "(labels: state=engaged|reverted). 'engaged' fires when "
            "the L-only fallback latches in after sustained L/R "
            "destructive correlation. 'reverted' fires when the "
            "downmix returns to L+R mean after sustained clean "
            "signal. Each transition pair represents one detected + "
            "recovered hardware fault.",
        )

        # ── Voice pipeline RED + USE (Ring 6 — M2) ──────────────────
        # Per-stage Rate / Errors / Duration plus Utilisation /
        # Saturation / Errors for every async queue between stages.
        # Single instrument per RED component with a closed-set
        # ``stage`` label keeps cardinality bounded (capture, vad,
        # stt, tts, output × success/error × top-N error_type via
        # _stage_metrics' BoundedCardinalityBucket).
        # Per-utterance trace IDs go on STRUCTURED LOG ATTRIBUTES, not
        # on metric labels — that's the canonical OTel cardinality
        # discipline and the M1 trace-ID contract assumes it.
        self.voice_stage_events = self._counter(
            "sovyx.voice.stage.events",
            "Per-stage RED Rate + Errors counter (labels: stage="
            "capture|vad|stt|tts|output, kind=success|error|drop, "
            "error_type=<top-N bucketed>). One increment per stage "
            "completion regardless of outcome — stage rate is the "
            "sum across kinds, error rate is kind=error / total, drop "
            "rate is kind=drop / total.",
        )
        self.voice_stage_duration = self._histogram(
            "sovyx.voice.stage.duration",
            "Per-stage RED Duration histogram in ms (labels: stage, "
            "outcome=success|error). Records wall-clock latency per "
            "stage invocation including failed paths so the dashboard "
            "can compare success-path vs error-path tail latency.",
        )
        # T1.37 — TTS synthesis latency bucketed by engine_family
        # (e.g. "kokoro:af", "piper:en_US"). Cardinality-bounded ~25
        # series via the family bucket; per-voice detail lives on the
        # ``voice.tts.chunk_emitted`` structured log event (carries
        # ``voice.voice`` per chunk + ``voice.synthesis_latency_ms``
        # since T1.37). The naive per-voice label was rejected
        # because Piper voices are operator-installable and would
        # blow the cardinality budget. See
        # ``docs-internal/T1.37-tts-latency-histogram-rfc.md`` Option
        # B for the rationale + bucket function contract.
        self.voice_tts_synthesis_latency = self._histogram(
            "sovyx.voice.tts.synthesis_latency",
            "Per-engine-family TTS synthesis duration in ms (labels: "
            "engine_family=kokoro:<lang>|piper:<lang>, outcome="
            "success|error). Per-voice detail lives on the "
            "voice.tts.chunk_emitted log event with "
            "voice.synthesis_latency_ms field — log-derived metrics "
            "tools (Loki/Grafana, Datadog log search) cover the "
            "per-voice dashboard need without paying the cardinality "
            "cost at the metric layer.",
        )
        self.voice_queue_depth = self._histogram(
            "sovyx.voice.queue.depth",
            "USE Utilisation — current depth of an async queue between "
            "voice stages, sampled per producer enqueue (labels: owner="
            "capture|vad|stt|tts|output). Unit is ``1`` (count of items "
            "queued) — interpretation requires pairing with the matching "
            "voice.queue.saturation_pct sample.",
            unit="1",
        )
        self.voice_queue_saturation_pct = self._histogram(
            "sovyx.voice.queue.saturation_pct",
            "USE Saturation — queue depth as a percentage of capacity, "
            "sampled per producer enqueue (labels: owner). 0 = empty, "
            "100 = at capacity (next enqueue blocks or drops). Sustained "
            "p95 > 80%% indicates an under-provisioned consumer.",
            unit="%",
        )

    # ── Internal instrument factories ───────────────────────────────
    # Centralised so every counter / histogram registered on this
    # registry is automatically gated by the cardinality budget — a
    # bare ``meter.create_counter(...)`` would slip past the budget and
    # silently inflate the scrape series count. Keep this the only path
    # that creates instruments.

    def _counter(
        self,
        name: str,
        description: str,
        *,
        unit: str = "1",
    ) -> _BudgetedInstrument:
        """Create a budget-gated counter and stash it on the registry."""
        instrument = self._meter.create_counter(
            name=name,
            description=description,
            unit=unit,
        )
        return _BudgetedInstrument(name=name, instrument=instrument, budget=self._budget)

    def _histogram(
        self,
        name: str,
        description: str,
        *,
        unit: str = "ms",
    ) -> _BudgetedInstrument:
        """Create a budget-gated histogram and stash it on the registry."""
        instrument = self._meter.create_histogram(
            name=name,
            description=description,
            unit=unit,
        )
        return _BudgetedInstrument(name=name, instrument=instrument, budget=self._budget)

    @property
    def cardinality_budget(self) -> CardinalityBudget:
        """Expose the underlying budget for diagnostics endpoints."""
        return self._budget

    def cardinality_report(self, top_n: int = 20) -> dict[str, Any]:
        """Return a dashboard-friendly snapshot of the cardinality budget.

        Wraps :meth:`CardinalityBudget.report` with the global totals
        so the dashboard renders one payload per request.
        """
        return {
            "max_series": self._budget.max_series,
            "total_series": self._budget.total_series,
            "metrics": self._budget.report(top_n=top_n),
        }

    @contextlib.contextmanager
    def measure_latency(
        self,
        histogram: _BudgetedInstrument,
        attributes: dict[str, str] | None = None,
    ) -> Generator[None, None, None]:
        """Context manager to measure and record latency in ms.

        Usage::

            with registry.measure_latency(registry.llm_response_latency,
                                          {"provider": "anthropic"}):
                result = await llm.call(...)

        Args:
            histogram: The histogram instrument to record to. Always a
                :class:`_BudgetedInstrument` because every histogram
                registered on this registry is created via
                :meth:`_histogram`, which wraps the OTel instrument in
                the cardinality-budget proxy.
            attributes: Optional OTel attributes for the measurement.
        """
        start = time.monotonic()
        try:
            yield
        finally:
            elapsed_ms = (time.monotonic() - start) * 1000
            histogram.record(elapsed_ms, attributes=attributes)


# ── No-op stub ──────────────────────────────────────────────────────────────


class _NoOpRegistry:
    """Drop-in replacement when metrics are disabled.

    Every attribute access returns a no-op object whose methods
    (``add``, ``record``, etc.) silently do nothing.
    """

    class _NoOpInstrument:
        """No-op instrument that accepts any call.

        Also handles chained attribute access (e.g. ``noop.a.b``)
        by returning itself for any unknown attribute.
        """

        def add(self, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
            """No-op add."""

        def record(self, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
            """No-op record."""

        def __getattr__(self, name: str) -> _NoOpRegistry._NoOpInstrument:
            """Return self for any attribute access — safe chaining."""
            return self

    _noop = _NoOpInstrument()

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401
        """Return no-op instrument for any attribute access."""
        return self._noop

    @contextlib.contextmanager
    def measure_latency(
        self,
        histogram: Any = None,  # noqa: ANN401
        attributes: dict[str, str] | None = None,
    ) -> Generator[None, None, None]:
        """No-op context manager — just yields."""
        yield


# ── Setup / Teardown ────────────────────────────────────────────────────────


def setup_metrics(
    *,
    readers: list[MetricReader] | None = None,
    service_name: str = "sovyx",
    max_series: int = DEFAULT_MAX_SERIES,
) -> MetricsRegistry:
    """Initialize the OTel metrics pipeline and return the registry.

    Call once at application startup (e.g. in ``Engine.start()``).

    Args:
        readers: Optional list of MetricReaders (e.g. PrometheusMetricReader,
            PeriodicExportingMetricReader).  If ``None``, an
            :class:`InMemoryMetricReader` is used (good for tests and
            the ``/api/metrics`` JSON endpoint).
        service_name: OTel service name attribute.
        max_series: Global cardinality budget passed straight to
            :class:`CardinalityBudget`. Defaults to
            :data:`DEFAULT_MAX_SERIES`. Bootstrap reads
            ``observability.metrics_max_series`` and forwards it here.

    Returns:
        The active :class:`MetricsRegistry`.
    """
    global _active_registry  # noqa: PLW0603

    if readers is None:
        readers = [InMemoryMetricReader()]

    provider = MeterProvider(metric_readers=readers)
    # Reset any existing provider before setting the new one.
    # OTel warns on override — we silence it by using the internal API
    # only when we know we're replacing (e.g. tests).
    metrics.set_meter_provider(provider)

    meter = provider.get_meter(_METER_NAME, _METER_VERSION)
    _active_registry = MetricsRegistry(meter, max_series=max_series)
    return _active_registry


def teardown_metrics() -> None:
    """Shut down the metrics pipeline.

    Flushes pending metrics and resets the module-level registry.
    """
    global _active_registry  # noqa: PLW0603

    provider = metrics.get_meter_provider()
    if isinstance(provider, MeterProvider):
        provider.shutdown()

    _active_registry = None


def get_metrics() -> MetricsRegistry | _NoOpRegistry:
    """Return the active metrics registry, or a no-op stub.

    Safe to call at any time — if :func:`setup_metrics` hasn't been
    called, returns a :class:`_NoOpRegistry` so instrumented code
    doesn't need ``if metrics:`` guards.
    """
    if _active_registry is not None:
        return _active_registry
    return _NoOpRegistry()


def collect_json(reader: InMemoryMetricReader) -> list[dict[str, Any]]:
    """Collect current metrics as a JSON-serializable list.

    Designed for the ``/api/metrics`` endpoint.  Reads from an
    :class:`InMemoryMetricReader` and converts to plain dicts.

    Args:
        reader: The InMemoryMetricReader to collect from.

    Returns:
        List of metric dicts with name, description, unit, and data points.
    """
    data = reader.get_metrics_data()
    result: list[dict[str, Any]] = []

    if data is None:
        return result

    for resource_metrics in data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                points: list[dict[str, Any]] = []
                for point in metric.data.data_points:
                    point_dict: dict[str, Any] = {
                        "attributes": dict(point.attributes) if point.attributes else {},
                        "time_unix_nano": point.time_unix_nano,
                    }
                    # Counter/UpDownCounter → value; Histogram → sum, count, etc.
                    if hasattr(point, "value"):
                        point_dict["value"] = point.value
                    if hasattr(point, "sum"):
                        point_dict["sum"] = point.sum
                    if hasattr(point, "count"):
                        point_dict["count"] = point.count
                    if hasattr(point, "min"):
                        point_dict["min"] = point.min
                    if hasattr(point, "max"):
                        point_dict["max"] = point.max
                    if hasattr(point, "bucket_counts"):
                        point_dict["bucket_counts"] = list(point.bucket_counts)
                    if hasattr(point, "explicit_bounds"):
                        point_dict["explicit_bounds"] = list(point.explicit_bounds)
                    points.append(point_dict)

                result.append(
                    {
                        "name": metric.name,
                        "description": metric.description,
                        "unit": metric.unit,
                        "data_points": points,
                    }
                )

    return result
