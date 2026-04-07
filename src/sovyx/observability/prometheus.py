"""Sovyx Prometheus exporter — OpenMetrics text format from OTel metrics.

Converts the OTel ``InMemoryMetricReader`` data into Prometheus exposition
format for ``/metrics`` scrape endpoints.  Zero external dependencies beyond
the OpenTelemetry SDK already in use.

Naming follows Prometheus conventions (IMPL-015 §1.3):
- Dots → underscores
- Counters get ``_total`` suffix
- Histograms get ``_bucket``, ``_sum``, ``_count`` suffixes
- Units appended where present (``ms`` → ``_milliseconds``, ``USD`` → ``_usd``)

Usage::

    from sovyx.observability.prometheus import PrometheusExporter

    exporter = PrometheusExporter(reader)
    text = exporter.export()
    # → "# HELP sovyx_messages_received_total Total messages received...\\n..."
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

# Unit mapping: OTel unit → Prometheus suffix
_UNIT_MAP: dict[str, str] = {
    "ms": "milliseconds",
    "s": "seconds",
    "USD": "usd",
    "By": "bytes",
    "1": "",
}


def _sanitize_name(name: str) -> str:
    """Convert OTel metric name to Prometheus-compatible name.

    Replaces dots and dashes with underscores.

    Args:
        name: OTel metric name (e.g. ``sovyx.llm.calls``).

    Returns:
        Prometheus name (e.g. ``sovyx_llm_calls``).
    """
    return name.replace(".", "_").replace("-", "_")


def _format_labels(attributes: dict[str, Any] | None) -> str:
    """Format OTel attributes as Prometheus label set.

    Args:
        attributes: Key-value attribute dict.

    Returns:
        Label string like ``{provider="anthropic",model="opus"}`` or empty string.
    """
    if not attributes:
        return ""
    pairs = [f'{k}="{v}"' for k, v in sorted(attributes.items())]
    return "{" + ",".join(pairs) + "}"


def _prom_name(name: str, unit: str, suffix: str = "") -> str:
    """Build full Prometheus metric name with unit and type suffix.

    Args:
        name: Sanitized base name.
        unit: OTel unit string.
        suffix: Type suffix (e.g. ``_total``, ``_bucket``).

    Returns:
        Full Prometheus metric name.
    """
    unit_suffix = _UNIT_MAP.get(unit, unit)
    parts = [name]
    if unit_suffix:
        parts.append(unit_suffix)
    result = "_".join(parts)
    if suffix and not result.endswith(suffix):
        result += suffix
    return result


class PrometheusExporter:
    """Exports OTel metrics as Prometheus exposition format text.

    Reads from an :class:`InMemoryMetricReader` and produces the text
    that a Prometheus scraper expects at ``/metrics``.

    Args:
        reader: The OTel InMemoryMetricReader to collect from.
    """

    def __init__(self, reader: InMemoryMetricReader) -> None:
        self._reader = reader

    def export(self) -> str:
        """Collect and format all metrics as Prometheus text.

        Returns:
            Prometheus exposition format string.
        """
        data = self._reader.get_metrics_data()
        if data is None:
            return ""

        lines: list[str] = []

        for resource_metrics in data.resource_metrics:
            for scope_metrics in resource_metrics.scope_metrics:
                for metric in scope_metrics.metrics:
                    lines.extend(self._format_metric(metric))

        # Prometheus expects trailing newline
        if lines:
            lines.append("")
        return "\n".join(lines)

    def _format_metric(self, metric: Any) -> list[str]:  # noqa: ANN401
        """Format a single OTel metric into Prometheus text lines.

        Args:
            metric: An OTel Metric object with name, description, unit, data.

        Returns:
            List of text lines for this metric.
        """
        base_name = _sanitize_name(metric.name)
        unit = metric.unit or ""
        data = metric.data
        data_type = type(data).__name__

        if data_type == "Sum":
            return self._format_counter(base_name, unit, metric.description, data)
        if data_type == "Histogram":
            return self._format_histogram(base_name, unit, metric.description, data)
        if data_type == "Gauge":
            return self._format_gauge(base_name, unit, metric.description, data)

        return []

    def _format_counter(
        self,
        base_name: str,
        unit: str,
        description: str,
        data: Any,  # noqa: ANN401
    ) -> list[str]:
        """Format a Sum (counter) metric.

        Args:
            base_name: Sanitized metric name.
            unit: OTel unit.
            description: Metric description.
            data: OTel Sum data object.

        Returns:
            Prometheus text lines.
        """
        full_name = _prom_name(base_name, unit, "_total")
        lines = [
            f"# HELP {full_name} {description}",
            f"# TYPE {full_name} counter",
        ]
        for point in data.data_points:
            attrs = dict(point.attributes) if point.attributes else None
            labels = _format_labels(attrs)
            lines.append(f"{full_name}{labels} {self._format_value(point.value)}")
        return lines

    def _format_histogram(
        self,
        base_name: str,
        unit: str,
        description: str,
        data: Any,  # noqa: ANN401
    ) -> list[str]:
        """Format a Histogram metric with buckets.

        Args:
            base_name: Sanitized metric name.
            unit: OTel unit.
            description: Metric description.
            data: OTel Histogram data object.

        Returns:
            Prometheus text lines with _bucket, _sum, _count.
        """
        name_with_unit = _prom_name(base_name, unit)
        lines = [
            f"# HELP {name_with_unit} {description}",
            f"# TYPE {name_with_unit} histogram",
        ]
        for point in data.data_points:
            attrs = dict(point.attributes) if point.attributes else None
            cumulative = 0
            bounds = list(point.explicit_bounds) if hasattr(point, "explicit_bounds") else []
            counts = list(point.bucket_counts) if hasattr(point, "bucket_counts") else []

            for i, bound in enumerate(bounds):
                cumulative += counts[i] if i < len(counts) else 0
                bucket_labels = dict(attrs) if attrs else {}
                bucket_labels["le"] = self._format_value(bound)
                lines.append(
                    f"{name_with_unit}_bucket{_format_labels(bucket_labels)} {cumulative}"
                )

            # +Inf bucket
            if counts:
                cumulative += counts[-1] if len(counts) > len(bounds) else 0
            inf_labels = dict(attrs) if attrs else {}
            inf_labels["le"] = "+Inf"
            lines.append(
                f"{name_with_unit}_bucket{_format_labels(inf_labels)} {cumulative}"
            )

            # _sum and _count
            base_labels = _format_labels(attrs)
            if hasattr(point, "sum"):
                lines.append(
                    f"{name_with_unit}_sum{base_labels} {self._format_value(point.sum)}"
                )
            if hasattr(point, "count"):
                lines.append(f"{name_with_unit}_count{base_labels} {point.count}")

        return lines

    def _format_gauge(
        self,
        base_name: str,
        unit: str,
        description: str,
        data: Any,  # noqa: ANN401
    ) -> list[str]:
        """Format a Gauge metric.

        Args:
            base_name: Sanitized metric name.
            unit: OTel unit.
            description: Metric description.
            data: OTel Gauge data object.

        Returns:
            Prometheus text lines.
        """
        full_name = _prom_name(base_name, unit)
        lines = [
            f"# HELP {full_name} {description}",
            f"# TYPE {full_name} gauge",
        ]
        for point in data.data_points:
            attrs = dict(point.attributes) if point.attributes else None
            labels = _format_labels(attrs)
            lines.append(f"{full_name}{labels} {self._format_value(point.value)}")
        return lines

    @staticmethod
    def _format_value(value: int | float) -> str:
        """Format a numeric value for Prometheus output.

        Handles infinity and NaN per Prometheus spec.

        Args:
            value: Numeric value to format.

        Returns:
            String representation.
        """
        if isinstance(value, float):
            if math.isinf(value):
                return "+Inf" if value > 0 else "-Inf"
            if math.isnan(value):
                return "NaN"
            # Avoid trailing zeros for clean integers
            if value == int(value):
                return str(int(value))
        return str(value)
