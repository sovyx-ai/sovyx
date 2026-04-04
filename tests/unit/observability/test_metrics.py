"""Tests for sovyx.observability.metrics — OTel counters, histograms, and registry."""

from __future__ import annotations

import time
from typing import Any

import pytest
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from sovyx.observability.metrics import (
    MetricsRegistry,
    _NoOpRegistry,
    collect_json,
    get_metrics,
    setup_metrics,
    teardown_metrics,
)

# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture()
def reader() -> InMemoryMetricReader:
    """Create a fresh InMemoryMetricReader."""
    return InMemoryMetricReader()


@pytest.fixture(autouse=True)
def _reset_otel() -> None:
    """Reset OTel global state between tests to avoid override warnings."""
    from opentelemetry.metrics import _internal as otel_internal

    yield  # type: ignore[misc]
    # Force-reset the global meter provider so next test can set_meter_provider.
    # This is necessary because OTel's set_meter_provider uses a Once guard.
    otel_internal._METER_PROVIDER_SET_ONCE._done = False  # type: ignore[attr-defined]
    otel_internal._METER_PROVIDER = None  # type: ignore[attr-defined]


@pytest.fixture()
def registry(reader: InMemoryMetricReader) -> MetricsRegistry:
    """Set up metrics with in-memory reader, tear down after test."""
    reg = setup_metrics(readers=[reader])
    yield reg  # type: ignore[misc]
    teardown_metrics()


# ── Setup / Teardown ────────────────────────────────────────────────────────


class TestSetupTeardown:
    """setup_metrics / teardown_metrics lifecycle."""

    def test_setup_returns_registry(self, reader: InMemoryMetricReader) -> None:
        reg = setup_metrics(readers=[reader])
        assert isinstance(reg, MetricsRegistry)
        teardown_metrics()

    def test_teardown_clears_registry(self, reader: InMemoryMetricReader) -> None:
        setup_metrics(readers=[reader])
        teardown_metrics()
        result = get_metrics()
        assert isinstance(result, _NoOpRegistry)

    def test_get_metrics_returns_registry_when_active(
        self,
        registry: MetricsRegistry,
    ) -> None:
        assert get_metrics() is registry

    def test_get_metrics_returns_noop_when_inactive(self) -> None:
        # Ensure no active registry
        teardown_metrics()
        result = get_metrics()
        assert isinstance(result, _NoOpRegistry)

    def test_setup_with_default_reader(self) -> None:
        """setup_metrics without readers uses InMemoryMetricReader."""
        reg = setup_metrics()
        assert isinstance(reg, MetricsRegistry)
        teardown_metrics()


# ── Counters ────────────────────────────────────────────────────────────────


class TestCounters:
    """Counter instruments record correctly."""

    def test_messages_received(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        registry.messages_received.add(1, {"channel": "telegram"})
        registry.messages_received.add(1, {"channel": "signal"})

        data = collect_json(reader)
        metric = _find_metric(data, "sovyx.messages.received")
        assert metric is not None
        total = sum(p["value"] for p in metric["data_points"])
        assert total == 2

    def test_messages_processed(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        registry.messages_processed.add(1, {"mind_id": "nyx"})
        registry.messages_processed.add(1, {"mind_id": "nyx"})

        data = collect_json(reader)
        metric = _find_metric(data, "sovyx.messages.processed")
        assert metric is not None
        total = sum(p["value"] for p in metric["data_points"])
        assert total == 2

    def test_llm_calls(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        registry.llm_calls.add(1, {"provider": "anthropic", "model": "claude-sonnet"})
        registry.llm_calls.add(1, {"provider": "openai", "model": "gpt-4o"})

        data = collect_json(reader)
        metric = _find_metric(data, "sovyx.llm.calls")
        assert metric is not None
        total = sum(p["value"] for p in metric["data_points"])
        assert total == 2

    def test_errors(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        registry.errors.add(1, {"error_type": "timeout", "module": "llm"})
        registry.errors.add(1, {"error_type": "validation", "module": "brain"})

        data = collect_json(reader)
        metric = _find_metric(data, "sovyx.errors")
        assert metric is not None
        total = sum(p["value"] for p in metric["data_points"])
        assert total == 2

    def test_tokens_used(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        registry.tokens_used.add(500, {"direction": "in", "provider": "anthropic"})
        registry.tokens_used.add(150, {"direction": "out", "provider": "anthropic"})

        data = collect_json(reader)
        metric = _find_metric(data, "sovyx.llm.tokens")
        assert metric is not None
        total = sum(p["value"] for p in metric["data_points"])
        assert total == 650

    def test_llm_cost(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        registry.llm_cost.add(0.003, {"provider": "anthropic"})
        registry.llm_cost.add(0.001, {"provider": "openai"})

        data = collect_json(reader)
        metric = _find_metric(data, "sovyx.llm.cost")
        assert metric is not None
        total = sum(p["value"] for p in metric["data_points"])
        assert abs(total - 0.004) < 1e-9

    def test_concepts_created(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        registry.concepts_created.add(1, {"source": "conversation"})

        data = collect_json(reader)
        metric = _find_metric(data, "sovyx.brain.concepts.created")
        assert metric is not None
        assert metric["data_points"][0]["value"] == 1

    def test_episodes_encoded(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        registry.episodes_encoded.add(1, {"conversation_id": "conv-1"})

        data = collect_json(reader)
        metric = _find_metric(data, "sovyx.brain.episodes.encoded")
        assert metric is not None
        assert metric["data_points"][0]["value"] == 1


# ── Histograms ──────────────────────────────────────────────────────────────


class TestHistograms:
    """Histogram instruments record distributions."""

    def test_llm_response_latency(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        registry.llm_response_latency.record(150.5, {"provider": "anthropic"})
        registry.llm_response_latency.record(200.0, {"provider": "anthropic"})

        data = collect_json(reader)
        metric = _find_metric(data, "sovyx.llm.latency")
        assert metric is not None
        point = metric["data_points"][0]
        assert point["count"] == 2
        assert abs(point["sum"] - 350.5) < 1e-6

    def test_cognitive_loop_latency(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        registry.cognitive_loop_latency.record(500.0)

        data = collect_json(reader)
        metric = _find_metric(data, "sovyx.cognitive.latency")
        assert metric is not None
        assert metric["data_points"][0]["count"] == 1

    def test_brain_search_latency(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        registry.brain_search_latency.record(45.2, {"search_type": "semantic"})

        data = collect_json(reader)
        metric = _find_metric(data, "sovyx.brain.search.latency")
        assert metric is not None
        assert metric["data_points"][0]["sum"] == pytest.approx(45.2)

    def test_context_assembly_latency(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        registry.context_assembly_latency.record(12.0)

        data = collect_json(reader)
        metric = _find_metric(data, "sovyx.context.assembly.latency")
        assert metric is not None

    def test_histogram_has_bucket_counts(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        """Histograms include bucket_counts and explicit_bounds."""
        registry.llm_response_latency.record(100.0)

        data = collect_json(reader)
        metric = _find_metric(data, "sovyx.llm.latency")
        assert metric is not None
        point = metric["data_points"][0]
        assert "bucket_counts" in point
        assert "explicit_bounds" in point
        assert len(point["bucket_counts"]) > 0


# ── measure_latency ────────────────────────────────────────────────────────


class TestMeasureLatency:
    """measure_latency context manager."""

    def test_records_elapsed_time(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        with registry.measure_latency(registry.brain_search_latency):
            time.sleep(0.01)  # ~10ms

        data = collect_json(reader)
        metric = _find_metric(data, "sovyx.brain.search.latency")
        assert metric is not None
        point = metric["data_points"][0]
        # Should be >= 10ms (sleep) but reasonable
        assert point["sum"] >= 8.0  # allow some OS jitter
        assert point["sum"] < 500.0  # sanity: not absurdly long

    def test_records_with_attributes(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        with registry.measure_latency(
            registry.llm_response_latency,
            {"provider": "openai"},
        ):
            pass  # instant

        data = collect_json(reader)
        metric = _find_metric(data, "sovyx.llm.latency")
        assert metric is not None
        assert metric["data_points"][0]["attributes"]["provider"] == "openai"

    def test_records_on_exception(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        """Latency is recorded even if body raises."""
        with (
            pytest.raises(RuntimeError, match="fail"),
            registry.measure_latency(registry.cognitive_loop_latency),
        ):
            raise RuntimeError("fail")

        data = collect_json(reader)
        metric = _find_metric(data, "sovyx.cognitive.latency")
        assert metric is not None
        assert metric["data_points"][0]["count"] == 1


# ── NoOpRegistry ────────────────────────────────────────────────────────────


class TestNoOpRegistry:
    """_NoOpRegistry is a safe drop-in when metrics are disabled."""

    def test_counter_add_noop(self) -> None:
        noop = _NoOpRegistry()
        noop.messages_processed.add(1)  # should not raise

    def test_histogram_record_noop(self) -> None:
        noop = _NoOpRegistry()
        noop.llm_response_latency.record(100.0)  # should not raise

    def test_measure_latency_noop(self) -> None:
        noop = _NoOpRegistry()
        with noop.measure_latency():
            pass  # should not raise

    def test_arbitrary_attr_returns_noop(self) -> None:
        noop = _NoOpRegistry()
        instrument = noop.anything_at_all
        instrument.add(1)  # should not raise
        instrument.record(42.0)  # should not raise

    def test_chained_attr_access(self) -> None:
        """Chained attribute access (noop.a.b.c) should not crash."""
        noop = _NoOpRegistry()
        noop.a.b  # noqa: B018
        noop.x.y.z.add(1)  # should not raise
        noop.deep.nested.attr.record(42.0)  # should not raise


# ── collect_json ────────────────────────────────────────────────────────────


class TestCollectJson:
    """collect_json serializes metrics to dicts."""

    def test_empty_when_no_data(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        data = collect_json(reader)
        # Instruments exist but no data recorded yet
        assert isinstance(data, list)

    def test_contains_metric_metadata(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        registry.messages_processed.add(1)

        data = collect_json(reader)
        metric = _find_metric(data, "sovyx.messages.processed")
        assert metric is not None
        assert metric["description"] == "Total messages fully processed through the cognitive loop"
        assert metric["unit"] == "1"

    def test_multiple_metrics_collected(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        registry.messages_processed.add(1)
        registry.llm_calls.add(1)
        registry.llm_response_latency.record(100.0)

        data = collect_json(reader)
        names = {m["name"] for m in data}
        assert "sovyx.messages.processed" in names
        assert "sovyx.llm.calls" in names
        assert "sovyx.llm.latency" in names

    def test_attributes_preserved(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        registry.llm_calls.add(1, {"provider": "anthropic", "model": "sonnet"})

        data = collect_json(reader)
        metric = _find_metric(data, "sovyx.llm.calls")
        assert metric is not None
        attrs = metric["data_points"][0]["attributes"]
        assert attrs["provider"] == "anthropic"
        assert attrs["model"] == "sonnet"


# ── Helpers ─────────────────────────────────────────────────────────────────


def _find_metric(
    data: list[dict[str, Any]],
    name: str,
) -> dict[str, Any] | None:
    """Find a metric by name in collect_json output."""
    for m in data:
        if m["name"] == name:
            return m
    return None
