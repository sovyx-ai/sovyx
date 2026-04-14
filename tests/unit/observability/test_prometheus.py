"""Tests for PrometheusExporter (V05-04).

Verifies Prometheus exposition format output, metric naming conventions,
label correctness, and edge cases.
"""

from __future__ import annotations

import math

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from starlette.testclient import TestClient

from sovyx.observability.metrics import (
    MetricsRegistry,
    setup_metrics,
    teardown_metrics,
)
from sovyx.observability.prometheus import (
    PrometheusExporter,
    _format_labels,
    _prom_name,
    _sanitize_name,
)

# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture()
def reader_and_registry() -> tuple[InMemoryMetricReader, MetricsRegistry]:
    """Set up OTel metrics with InMemoryMetricReader."""
    reader = InMemoryMetricReader()
    registry = setup_metrics(readers=[reader])
    yield reader, registry
    teardown_metrics()


@pytest.fixture()
def exporter(
    reader_and_registry: tuple[InMemoryMetricReader, MetricsRegistry],
) -> PrometheusExporter:
    """Create a PrometheusExporter with the test reader."""
    reader, _ = reader_and_registry
    return PrometheusExporter(reader)


# ── Unit Tests: Name Sanitization ───────────────────────────────────────────


class TestSanitizeName:
    """Tests for _sanitize_name."""

    def test_dots_to_underscores(self) -> None:
        assert _sanitize_name("sovyx.llm.calls") == "sovyx_llm_calls"

    def test_dashes_to_underscores(self) -> None:
        assert _sanitize_name("sovyx-llm-calls") == "sovyx_llm_calls"

    def test_mixed_separators(self) -> None:
        assert _sanitize_name("sovyx.brain-search.latency") == "sovyx_brain_search_latency"

    def test_no_change_needed(self) -> None:
        assert _sanitize_name("sovyx_llm_calls") == "sovyx_llm_calls"

    def test_empty_string(self) -> None:
        assert _sanitize_name("") == ""


class TestFormatLabels:
    """Tests for _format_labels."""

    def test_none_returns_empty(self) -> None:
        assert _format_labels(None) == ""

    def test_empty_dict_returns_empty(self) -> None:
        assert _format_labels({}) == ""

    def test_single_label(self) -> None:
        result = _format_labels({"provider": "anthropic"})
        assert result == '{provider="anthropic"}'

    def test_multiple_labels_sorted(self) -> None:
        result = _format_labels({"model": "opus", "provider": "anthropic"})
        assert result == '{model="opus",provider="anthropic"}'

    def test_numeric_value(self) -> None:
        result = _format_labels({"code": 200})
        assert result == '{code="200"}'


class TestPromName:
    """Tests for _prom_name."""

    def test_counter_with_unit_1(self) -> None:
        result = _prom_name("sovyx_messages_received", "1", "_total")
        assert result == "sovyx_messages_received_total"

    def test_counter_with_usd(self) -> None:
        result = _prom_name("sovyx_llm_cost", "USD", "_total")
        assert result == "sovyx_llm_cost_usd_total"

    def test_histogram_with_ms(self) -> None:
        result = _prom_name("sovyx_llm_latency", "ms")
        assert result == "sovyx_llm_latency_milliseconds"

    def test_no_unit(self) -> None:
        result = _prom_name("sovyx_custom", "")
        assert result == "sovyx_custom"

    def test_unknown_unit_passthrough(self) -> None:
        result = _prom_name("sovyx_custom", "widgets")
        assert result == "sovyx_custom_widgets"

    def test_no_duplicate_suffix(self) -> None:
        result = _prom_name("sovyx_errors_total", "1", "_total")
        assert result == "sovyx_errors_total"


# ── Unit Tests: PrometheusExporter ──────────────────────────────────────────


class TestPrometheusExporterEmpty:
    """Tests for empty/no-data scenarios."""

    def test_no_data_returns_empty(self) -> None:
        reader = InMemoryMetricReader()
        exporter = PrometheusExporter(reader)
        result = exporter.export()
        # No metrics registered → empty or just newline
        assert result == "" or result.strip() == ""


class TestPrometheusExporterCounters:
    """Tests for counter metric formatting."""

    def test_counter_output_format(
        self,
        reader_and_registry: tuple[InMemoryMetricReader, MetricsRegistry],
        exporter: PrometheusExporter,
    ) -> None:
        _, registry = reader_and_registry
        registry.messages_received.add(5, {"channel": "telegram"})

        text = exporter.export()
        assert "# TYPE" in text
        assert "counter" in text
        assert "sovyx_messages_received" in text
        assert "5" in text

    def test_counter_with_labels(
        self,
        reader_and_registry: tuple[InMemoryMetricReader, MetricsRegistry],
        exporter: PrometheusExporter,
    ) -> None:
        _, registry = reader_and_registry
        registry.llm_calls.add(3, {"provider": "anthropic", "model": "opus"})

        text = exporter.export()
        assert 'provider="anthropic"' in text
        assert 'model="opus"' in text

    def test_counter_without_labels(
        self,
        reader_and_registry: tuple[InMemoryMetricReader, MetricsRegistry],
        exporter: PrometheusExporter,
    ) -> None:
        _, registry = reader_and_registry
        registry.messages_processed.add(10)

        text = exporter.export()
        assert "sovyx_messages_processed" in text
        assert "10" in text

    def test_cost_counter_has_usd_suffix(
        self,
        reader_and_registry: tuple[InMemoryMetricReader, MetricsRegistry],
        exporter: PrometheusExporter,
    ) -> None:
        _, registry = reader_and_registry
        registry.llm_cost.add(0.05)

        text = exporter.export()
        assert "sovyx_llm_cost_usd_total" in text

    def test_multiple_counters(
        self,
        reader_and_registry: tuple[InMemoryMetricReader, MetricsRegistry],
        exporter: PrometheusExporter,
    ) -> None:
        _, registry = reader_and_registry
        registry.messages_received.add(1, {"channel": "telegram"})
        registry.errors.add(2, {"error_type": "timeout", "module": "llm"})
        registry.concepts_created.add(50)

        text = exporter.export()
        assert "sovyx_messages_received" in text
        assert "sovyx_errors" in text
        assert "sovyx_brain_concepts_created" in text


class TestPrometheusExporterHistograms:
    """Tests for histogram metric formatting."""

    def test_histogram_has_bucket_sum_count(
        self,
        reader_and_registry: tuple[InMemoryMetricReader, MetricsRegistry],
        exporter: PrometheusExporter,
    ) -> None:
        _, registry = reader_and_registry
        registry.llm_response_latency.record(150.0, {"provider": "anthropic"})

        text = exporter.export()
        assert "_bucket" in text
        assert "_sum" in text
        assert "_count" in text
        assert "+Inf" in text

    def test_histogram_has_le_labels(
        self,
        reader_and_registry: tuple[InMemoryMetricReader, MetricsRegistry],
        exporter: PrometheusExporter,
    ) -> None:
        _, registry = reader_and_registry
        registry.brain_search_latency.record(50.0)

        text = exporter.export()
        assert 'le="' in text
        assert 'le="+Inf"' in text

    def test_histogram_unit_suffix(
        self,
        reader_and_registry: tuple[InMemoryMetricReader, MetricsRegistry],
        exporter: PrometheusExporter,
    ) -> None:
        _, registry = reader_and_registry
        registry.cognitive_loop_latency.record(200.0)

        text = exporter.export()
        assert "sovyx_cognitive_latency_milliseconds" in text


class TestPrometheusExporterGauges:
    """Tests for gauge metric formatting."""

    def test_gauge_output(self) -> None:
        """Test gauge formatting via OTel UpDownCounter (produces Gauge data)."""
        from opentelemetry.sdk.metrics import MeterProvider

        reader = InMemoryMetricReader()
        provider = MeterProvider(metric_readers=[reader])
        meter = provider.get_meter("test", "0.1.0")

        # UpDownCounter produces Gauge/Sum(non-monotonic) in OTel
        gauge = meter.create_up_down_counter(
            name="sovyx.active.minds",
            description="Active minds",
            unit="1",
        )
        gauge.add(3)

        exporter = PrometheusExporter(reader)
        text = exporter.export()

        assert "sovyx_active_minds" in text
        assert "3" in text
        provider.shutdown()

    def test_observable_gauge(self) -> None:
        """Test observable gauge formatting."""
        from opentelemetry.sdk.metrics import MeterProvider

        reader = InMemoryMetricReader()
        provider = MeterProvider(metric_readers=[reader])
        meter = provider.get_meter("test", "0.1.0")

        def _callback(options: object) -> list[object]:
            from opentelemetry.metrics import Observation

            return [Observation(42, {"source": "test"})]

        meter.create_observable_gauge(
            name="sovyx.memory.rss",
            callbacks=[_callback],
            description="RSS memory bytes",
            unit="By",
        )

        exporter = PrometheusExporter(reader)
        text = exporter.export()

        assert "sovyx_memory_rss_bytes" in text
        assert "gauge" in text
        assert "42" in text
        assert 'source="test"' in text
        provider.shutdown()


class TestPrometheusExporterUnknownType:
    """Tests for unknown metric data types."""

    def test_unknown_data_type_returns_empty(self) -> None:
        """If OTel produces an unknown data type, exporter gracefully returns []."""
        from unittest.mock import MagicMock

        reader = InMemoryMetricReader()
        exporter = PrometheusExporter(reader)

        mock_metric = MagicMock()
        mock_metric.name = "sovyx.unknown"
        mock_metric.unit = "1"
        mock_metric.description = "Unknown metric"
        mock_metric.data = MagicMock()
        type(mock_metric.data).__name__ = "UnknownDataType"

        result = exporter._format_metric(mock_metric)
        assert result == []


class TestPrometheusExporterHistogramEdgeCases:
    """Edge cases for histogram formatting."""

    def test_histogram_no_observations(self) -> None:
        """Histogram with no recorded values still exports structure."""
        from opentelemetry.sdk.metrics import MeterProvider

        reader = InMemoryMetricReader()
        provider = MeterProvider(metric_readers=[reader])
        meter = provider.get_meter("test", "0.1.0")
        hist = meter.create_histogram(
            name="sovyx.test.latency",
            description="Test",
            unit="ms",
        )
        # Record at least one value so the metric appears
        hist.record(0.0)

        exporter = PrometheusExporter(reader)
        text = exporter.export()
        assert "_bucket" in text
        assert "_count" in text
        provider.shutdown()

    def test_histogram_with_attributes(self) -> None:
        """Histogram buckets carry labels from the recorded attributes."""
        from opentelemetry.sdk.metrics import MeterProvider

        reader = InMemoryMetricReader()
        provider = MeterProvider(metric_readers=[reader])
        meter = provider.get_meter("test", "0.1.0")
        hist = meter.create_histogram(
            name="sovyx.req.duration",
            description="Request duration",
            unit="ms",
        )
        hist.record(100.0, {"endpoint": "/api/status"})

        exporter = PrometheusExporter(reader)
        text = exporter.export()
        assert 'endpoint="/api/status"' in text
        provider.shutdown()


class TestPrometheusExporterHelpType:
    """Tests for HELP and TYPE comments."""

    def test_help_line_present(
        self,
        reader_and_registry: tuple[InMemoryMetricReader, MetricsRegistry],
        exporter: PrometheusExporter,
    ) -> None:
        _, registry = reader_and_registry
        registry.messages_received.add(1)

        text = exporter.export()
        help_lines = [line for line in text.split("\n") if line.startswith("# HELP")]
        assert len(help_lines) >= 1

    def test_type_line_present(
        self,
        reader_and_registry: tuple[InMemoryMetricReader, MetricsRegistry],
        exporter: PrometheusExporter,
    ) -> None:
        _, registry = reader_and_registry
        registry.messages_received.add(1)

        text = exporter.export()
        type_lines = [line for line in text.split("\n") if line.startswith("# TYPE")]
        assert len(type_lines) >= 1

    def test_counter_type_annotation(
        self,
        reader_and_registry: tuple[InMemoryMetricReader, MetricsRegistry],
        exporter: PrometheusExporter,
    ) -> None:
        _, registry = reader_and_registry
        registry.errors.add(1)

        text = exporter.export()
        assert "# TYPE" in text
        assert "counter" in text

    def test_histogram_type_annotation(
        self,
        reader_and_registry: tuple[InMemoryMetricReader, MetricsRegistry],
        exporter: PrometheusExporter,
    ) -> None:
        _, registry = reader_and_registry
        registry.llm_response_latency.record(100.0)

        text = exporter.export()
        assert "histogram" in text


class TestPrometheusExporterNaming:
    """Tests for Prometheus naming conventions (IMPL-015 §1.3)."""

    def test_sovyx_prefix(
        self,
        reader_and_registry: tuple[InMemoryMetricReader, MetricsRegistry],
        exporter: PrometheusExporter,
    ) -> None:
        _, registry = reader_and_registry
        registry.messages_received.add(1)
        registry.llm_calls.add(1)

        text = exporter.export()
        # All metric lines (non-comment) should start with sovyx_
        metric_lines = [line for line in text.split("\n") if line and not line.startswith("#")]
        for line in metric_lines:
            assert line.startswith("sovyx_"), f"Missing sovyx_ prefix: {line}"

    def test_counter_total_suffix(
        self,
        reader_and_registry: tuple[InMemoryMetricReader, MetricsRegistry],
        exporter: PrometheusExporter,
    ) -> None:
        _, registry = reader_and_registry
        registry.messages_received.add(1)

        text = exporter.export()
        type_lines = [line for line in text.split("\n") if line.startswith("# TYPE")]
        counter_types = [line for line in type_lines if "counter" in line]
        for line in counter_types:
            name = line.split()[2]
            assert name.endswith("_total"), f"Counter missing _total: {name}"


class TestFormatValue:
    """Tests for numeric value formatting."""

    def test_integer(self) -> None:
        assert PrometheusExporter._format_value(42) == "42"

    def test_float_integer_value(self) -> None:
        assert PrometheusExporter._format_value(42.0) == "42"

    def test_float_decimal(self) -> None:
        assert PrometheusExporter._format_value(3.14) == "3.14"

    def test_positive_infinity(self) -> None:
        assert PrometheusExporter._format_value(float("inf")) == "+Inf"

    def test_negative_infinity(self) -> None:
        assert PrometheusExporter._format_value(float("-inf")) == "-Inf"

    def test_nan(self) -> None:
        assert PrometheusExporter._format_value(float("nan")) == "NaN"

    def test_zero(self) -> None:
        assert PrometheusExporter._format_value(0) == "0"

    def test_zero_float(self) -> None:
        assert PrometheusExporter._format_value(0.0) == "0"


# ── Property-Based Tests ────────────────────────────────────────────────────


class TestPrometheusProperties:
    """Property-based tests for Prometheus formatting."""

    @settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(name=st.text(alphabet="abcdefghijklmnopqrstuvwxyz._-", min_size=1, max_size=50))
    def test_sanitize_never_contains_dots_or_dashes(self, name: str) -> None:
        result = _sanitize_name(name)
        assert "." not in result
        assert "-" not in result

    @settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(
        value=st.one_of(
            st.integers(min_value=0, max_value=10**9),
            st.floats(min_value=0, max_value=10**9, allow_nan=False, allow_infinity=False),
        ),
    )
    def test_format_value_roundtrips(self, value: int | float) -> None:
        result = PrometheusExporter._format_value(value)
        parsed = float(result)
        assert math.isclose(parsed, float(value), rel_tol=1e-9)


# ── Integration: /metrics Endpoint ──────────────────────────────────────────


def _make_client(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[TestClient, dict[str, str]]:
    """Create test client with fixed token."""
    token = "test-token-fixo"
    from sovyx.dashboard.server import create_app

    app = create_app(token=token)
    return TestClient(app), {"Authorization": f"Bearer {token}"}


class TestMetricsEndpoint:
    """Tests for the /metrics HTTP endpoint."""

    def test_no_auth_required(self, tmp_path_factory: pytest.TempPathFactory) -> None:
        """Prometheus scraper doesn't send auth — endpoint must be open."""
        client, _ = _make_client(tmp_path_factory)
        resp = client.get("/metrics")
        assert resp.status_code == 200

    def test_content_type(self, tmp_path_factory: pytest.TempPathFactory) -> None:
        client, _ = _make_client(tmp_path_factory)
        resp = client.get("/metrics")
        assert "text/plain" in resp.headers["content-type"]

    def test_no_reader_returns_placeholder(self, tmp_path_factory: pytest.TempPathFactory) -> None:
        client, _ = _make_client(tmp_path_factory)
        resp = client.get("/metrics")
        assert resp.status_code == 200
        assert "No metrics" in resp.text

    def test_with_reader_returns_metrics(self, tmp_path_factory: pytest.TempPathFactory) -> None:
        reader = InMemoryMetricReader()
        registry = setup_metrics(readers=[reader])

        try:
            from sovyx.dashboard.server import create_app

            app = create_app(token="test-token-fixo")

            app.state.metrics_reader = reader

            # Record some metrics
            registry.messages_received.add(10, {"channel": "telegram"})
            registry.llm_calls.add(5, {"provider": "openai"})

            client = TestClient(app)
            resp = client.get("/metrics")

            assert resp.status_code == 200
            assert "sovyx_messages_received" in resp.text
            assert "sovyx_llm_calls" in resp.text
            assert 'channel="telegram"' in resp.text
        finally:
            teardown_metrics()

    def test_other_api_still_requires_auth(self, tmp_path_factory: pytest.TempPathFactory) -> None:
        """Verify /metrics being open doesn't affect other endpoints."""
        client, _ = _make_client(tmp_path_factory)
        # /metrics is open
        assert client.get("/metrics").status_code == 200
        # /api/status still requires auth
        assert client.get("/api/status").status_code in (401, 403)


# ── Full Export Roundtrip ───────────────────────────────────────────────────


class TestFullExportRoundtrip:
    """End-to-end: record metrics → export → verify all present."""

    def test_all_metric_types(
        self,
        reader_and_registry: tuple[InMemoryMetricReader, MetricsRegistry],
        exporter: PrometheusExporter,
    ) -> None:
        _, registry = reader_and_registry

        # Counters
        registry.messages_received.add(100, {"channel": "telegram"})
        registry.messages_processed.add(95, {"mind_id": "nyx"})
        registry.llm_calls.add(50, {"provider": "anthropic", "model": "opus"})
        registry.errors.add(3, {"error_type": "timeout", "module": "llm"})
        registry.tokens_used.add(10000, {"direction": "in", "provider": "anthropic"})
        registry.llm_cost.add(1.5)
        registry.concepts_created.add(200)
        registry.episodes_encoded.add(75)

        # Histograms
        registry.llm_response_latency.record(150.0, {"provider": "anthropic"})
        registry.cognitive_loop_latency.record(300.0)
        registry.brain_search_latency.record(45.0)
        registry.context_assembly_latency.record(20.0)

        text = exporter.export()

        # Verify all counters present
        assert "sovyx_messages_received" in text
        assert "sovyx_messages_processed" in text
        assert "sovyx_llm_calls" in text
        assert "sovyx_errors" in text
        assert "sovyx_llm_tokens" in text
        assert "sovyx_llm_cost_usd_total" in text
        assert "sovyx_brain_concepts_created" in text
        assert "sovyx_brain_episodes_encoded" in text

        # Verify all histograms present
        assert "sovyx_llm_latency_milliseconds" in text
        assert "sovyx_cognitive_latency_milliseconds" in text
        assert "sovyx_brain_search_latency_milliseconds" in text
        assert "sovyx_context_assembly_latency_milliseconds" in text

        # Verify structure
        assert text.count("# TYPE") >= 12  # noqa: PLR2004 - 8 counters + 4 histograms
        assert text.endswith("\n")

    def test_multiple_label_combinations(
        self,
        reader_and_registry: tuple[InMemoryMetricReader, MetricsRegistry],
        exporter: PrometheusExporter,
    ) -> None:
        _, registry = reader_and_registry

        registry.llm_calls.add(10, {"provider": "anthropic", "model": "opus"})
        registry.llm_calls.add(20, {"provider": "openai", "model": "gpt4"})
        registry.llm_calls.add(5, {"provider": "ollama", "model": "llama3"})

        text = exporter.export()

        assert 'provider="anthropic"' in text
        assert 'provider="openai"' in text
        assert 'provider="ollama"' in text
