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

        # ── Histograms ─────────────────────────────────────────────
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
        """No-op instrument that accepts any call."""

        def add(self, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
            """No-op add."""

        def record(self, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
            """No-op record."""

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
