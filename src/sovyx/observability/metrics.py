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

if TYPE_CHECKING:
    from collections.abc import Generator


# ── Metrics Registry ────────────────────────────────────────────────────────

_METER_NAME = "sovyx"
# Tracks the observability API version (not the package version in pyproject.toml).
# Bump when instrument names, units, or attribute schemas change.
_METER_VERSION = "0.2.0"

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

    def __init__(self, meter: metrics.Meter) -> None:
        self._meter = meter

        # ── Counters ────────────────────────────────────────────────
        self.messages_received = meter.create_counter(
            name="sovyx.messages.received",
            description="Total messages received from channels (inbound)",
            unit="1",
        )

        self.messages_processed = meter.create_counter(
            name="sovyx.messages.processed",
            description="Total messages fully processed through the cognitive loop",
            unit="1",
        )

        self.llm_calls = meter.create_counter(
            name="sovyx.llm.calls",
            description="Total LLM provider calls",
            unit="1",
        )

        self.errors = meter.create_counter(
            name="sovyx.errors",
            description="Total errors by category",
            unit="1",
        )

        self.tokens_used = meter.create_counter(
            name="sovyx.llm.tokens",
            description="Total tokens consumed",
            unit="1",
        )

        self.llm_cost = meter.create_counter(
            name="sovyx.llm.cost",
            description="Cumulative LLM cost",
            unit="USD",
        )

        self.concepts_created = meter.create_counter(
            name="sovyx.brain.concepts.created",
            description="Total concepts created in brain memory",
            unit="1",
        )

        self.episodes_encoded = meter.create_counter(
            name="sovyx.brain.episodes.encoded",
            description="Total episodes encoded into brain memory",
            unit="1",
        )

        self.safety_blocks = meter.create_counter(
            name="sovyx.safety.blocks",
            description="Total safety filter blocks (label: direction, tier, category)",
            unit="1",
        )

        self.safety_pii_redacted = meter.create_counter(
            name="sovyx.safety.pii.redacted",
            description="Total PII redactions in output (label: type)",
            unit="1",
        )

        self.safety_llm_classifications = meter.create_counter(
            name="sovyx.safety.llm.classifications",
            description="Total LLM safety classifications (label: result, method, category)",
            unit="1",
        )

        # ── Histograms ─────────────────────────────────────────────
        self.safety_filter_latency = meter.create_histogram(
            name="sovyx.safety.filter.latency",
            description="Safety filter latency per direction (label: direction)",
            unit="ms",
        )

        self.llm_response_latency = meter.create_histogram(
            name="sovyx.llm.latency",
            description="LLM call latency",
            unit="ms",
        )

        self.cognitive_loop_latency = meter.create_histogram(
            name="sovyx.cognitive.latency",
            description="Full cognitive loop latency (perceive to act)",
            unit="ms",
        )

        self.brain_search_latency = meter.create_histogram(
            name="sovyx.brain.search.latency",
            description="Brain semantic search latency",
            unit="ms",
        )

        self.context_assembly_latency = meter.create_histogram(
            name="sovyx.context.assembly.latency",
            description="Context assembly latency",
            unit="ms",
        )

        self.safety_llm_classify_latency = meter.create_histogram(
            name="sovyx.safety.llm.classify.latency",
            description="LLM safety classifier latency per call",
            unit="ms",
        )

        # ── Voice device test (setup-wizard meter + TTS playback) ─
        self.voice_test_sessions = meter.create_counter(
            name="sovyx.voice.test.sessions",
            description="Total voice-test meter sessions (label: result)",
            unit="1",
        )

        self.voice_test_clipping_events = meter.create_counter(
            name="sovyx.voice.test.clipping.events",
            description="Voice-test meter frames flagged as clipping",
            unit="1",
        )

        self.voice_test_stream_open_latency = meter.create_histogram(
            name="sovyx.voice.test.stream.open.latency",
            description="Latency from WS accept to first LevelFrame emitted",
            unit="ms",
        )

        self.voice_test_output_synthesis_ms = meter.create_histogram(
            name="sovyx.voice.test.output.synthesis.latency",
            description="TTS synthesis latency for the voice-test playback job",
            unit="ms",
        )

        self.voice_test_output_playback_ms = meter.create_histogram(
            name="sovyx.voice.test.output.playback.latency",
            description="Sink playback latency for the voice-test job",
            unit="ms",
        )

        # Unified stream opener — one counter increment per attempted
        # (host_api, auto_convert, result) triple so field debugging can
        # answer "which host API + auto_convert combination is the mic
        # actually landing on in the wild?" without parsing logs.
        self.voice_stream_open_attempts = meter.create_counter(
            name="sovyx.voice.stream.open.attempts",
            description=(
                "Stream-opener attempts (labels: host_api, auto_convert, "
                "kind=input|output, result=ok|silent|error, error_code)"
            ),
            unit="1",
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
        self.model_download_attempts = meter.create_counter(
            name="sovyx.model.download.attempts",
            description=(
                "Model download attempts (labels: model, source=primary|mirror-N, "
                "result=ok|transient|permanent, error_type)"
            ),
            unit="1",
        )

        # ── Voice Capture Health Lifecycle (ADR §5.8) ──────────────
        # Stable metric names — see docs-internal/ADR-voice-capture-health-lifecycle.md.
        # Labels are kept low-cardinality so Prometheus series counts stay bounded
        # even in long-running daemons with many hot-plug events.
        self.voice_health_cascade_attempts = meter.create_counter(
            name="sovyx.voice.health.cascade.attempts",
            description=(
                "Cascade attempts against an endpoint (labels: platform, "
                "host_api, success=true|false, source=pinned|store|cascade)"
            ),
            unit="1",
        )
        self.voice_health_combo_store_hits = meter.create_counter(
            name="sovyx.voice.health.combo_store.hits",
            description=(
                "ComboStore fast-path resolutions (labels: endpoint_class, "
                "result=hit|miss|needs_revalidation)"
            ),
            unit="1",
        )
        self.voice_health_combo_store_invalidations = meter.create_counter(
            name="sovyx.voice.health.combo_store.invalidations",
            description="ComboStore invalidations (labels: reason — §4.1 rule tags)",
            unit="1",
        )
        self.voice_health_probe_diagnosis = meter.create_counter(
            name="sovyx.voice.health.probe.diagnosis",
            description=(
                "Probe outcomes (labels: diagnosis — Diagnosis enum value, mode=cold|warm)"
            ),
            unit="1",
        )
        self.voice_health_probe_duration = meter.create_histogram(
            name="sovyx.voice.health.probe.duration",
            description="Probe wall-clock duration (label: mode=cold|warm)",
            unit="ms",
        )
        self.voice_health_preflight_failures = meter.create_counter(
            name="sovyx.voice.health.preflight.failures",
            description=(
                "Pre-flight step failures (labels: step — PreflightStep value, "
                "code — PreflightStepCode value)"
            ),
            unit="1",
        )
        self.voice_health_recovery_attempts = meter.create_counter(
            name="sovyx.voice.health.recovery.attempts",
            description=(
                "Watchdog recovery triggers (labels: trigger=deaf_backoff|"
                "hotplug|default_change|power|audio_service)"
            ),
            unit="1",
        )
        self.voice_health_self_feedback_blocks = meter.create_counter(
            name="sovyx.voice.health.self_feedback.blocks",
            description=("Self-feedback isolation blocks (label: layer=gate|duck|spectral)"),
            unit="1",
        )
        self.voice_health_active_endpoint_changes = meter.create_counter(
            name="sovyx.voice.health.active_endpoint.changes",
            description=("Active endpoint swaps (label: reason=hotplug|default|manual|recovery)"),
            unit="1",
        )
        self.voice_health_kernel_invalidated_events = meter.create_counter(
            name="sovyx.voice.health.kernel_invalidated.events",
            description=(
                "Kernel-side IAudioClient invalidation events detected by "
                "the probe (labels: platform, host_api, action=quarantine|"
                "failover|recheck_recovered|recheck_still_invalid)"
            ),
            unit="1",
        )
        self.voice_health_probe_start_time_errors = meter.create_counter(
            name="sovyx.voice.health.probe.start_time_errors",
            description=(
                "Probe stream.start() failures classified into Diagnosis "
                "values (labels: diagnosis, host_api, platform). Before "
                "v0.20.2 these bypassed the probe classifier and appeared "
                "as generic DRIVER_ERROR in cascade logs."
            ),
            unit="1",
        )
        self.voice_health_time_to_first_utterance = meter.create_histogram(
            name="sovyx.voice.health.time_to_first_utterance",
            description=(
                "User-perceived KPI — latency from WakeWordDetectedEvent to "
                "SpeechStartedEvent. ADR §5.14 target p95 ≤ 200 ms."
            ),
            unit="ms",
        )

    @contextlib.contextmanager
    def measure_latency(
        self,
        histogram: metrics.Histogram,
        attributes: dict[str, str] | None = None,
    ) -> Generator[None, None, None]:
        """Context manager to measure and record latency in ms.

        Usage::

            with registry.measure_latency(registry.llm_response_latency,
                                          {"provider": "anthropic"}):
                result = await llm.call(...)

        Args:
            histogram: The histogram instrument to record to.
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
) -> MetricsRegistry:
    """Initialize the OTel metrics pipeline and return the registry.

    Call once at application startup (e.g. in ``Engine.start()``).

    Args:
        readers: Optional list of MetricReaders (e.g. PrometheusMetricReader,
            PeriodicExportingMetricReader).  If ``None``, an
            :class:`InMemoryMetricReader` is used (good for tests and
            the ``/api/metrics`` JSON endpoint).
        service_name: OTel service name attribute.

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
    _active_registry = MetricsRegistry(meter)
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
