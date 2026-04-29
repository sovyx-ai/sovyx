"""Tests for the AEC observability stack [Phase 4 T4.7 + T4.8].

Coverage:

* Metric name constants pin the wire contract.
* Record helpers no-op safely when the registry is torn down.
* :class:`FrameNormalizer` emits ``voice.aec.windows{processed}``
  + ``voice.aec.erle_db`` on non-silent render windows.
* :class:`FrameNormalizer` emits ``voice.aec.windows{render_silent}``
  WITHOUT ERLE on silent render windows (echo undefined).
* The disabled / aec=None branch is fully passive (no emissions).
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from sovyx.observability.metrics import (
    MetricsRegistry,
    setup_metrics,
    teardown_metrics,
)
from sovyx.voice._frame_normalizer import FrameNormalizer
from sovyx.voice.health._metrics import (
    METRIC_AEC_ERLE_DB,
    METRIC_AEC_WINDOWS,
    record_aec_erle,
    record_aec_window,
)

# ── Test fixtures ────────────────────────────────────────────────────────


@pytest.fixture()
def reader() -> InMemoryMetricReader:
    return InMemoryMetricReader()


@pytest.fixture(autouse=True)
def _reset_otel() -> None:
    from opentelemetry.metrics import _internal as otel_internal

    yield
    otel_internal._METER_PROVIDER_SET_ONCE._done = False  # type: ignore[attr-defined]
    otel_internal._METER_PROVIDER = None  # type: ignore[attr-defined]


@pytest.fixture()
def registry(reader: InMemoryMetricReader) -> MetricsRegistry:
    reg = setup_metrics(readers=[reader])
    yield reg
    teardown_metrics()


def _collect(reader: InMemoryMetricReader) -> list[dict[str, Any]]:
    from sovyx.observability.metrics import collect_json

    return collect_json(reader)


def _find(data: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    for m in data:
        if m["name"] == name:
            return m
    return None


# ── Test doubles ─────────────────────────────────────────────────────────


class _RecordingAec:
    """AecProcessor that returns capture / 2 so ERLE is computable."""

    def process(
        self,
        capture: np.ndarray,
        render: np.ndarray,  # noqa: ARG002 — interface contract
    ) -> np.ndarray:
        # Halving the residual gives a ~6 dB ERLE on non-zero captures.
        return (capture.astype(np.int32) // 2).astype(np.int16)

    def reset(self) -> None: ...


class _ConstantRenderProvider:
    """Render provider returning a constant value (silent or non-silent)."""

    def __init__(self, *, value: int) -> None:
        self._value = value

    def get_aligned_window(self, n_samples: int) -> np.ndarray:
        return np.full(n_samples, self._value, dtype=np.int16)


# ── Stable name constants ────────────────────────────────────────────────


class TestStableNameContract:
    """Wire contract — any rename is a breaking dashboard change."""

    def test_aec_erle_db_name(self) -> None:
        assert METRIC_AEC_ERLE_DB == "sovyx.voice.aec.erle_db"

    def test_aec_windows_name(self) -> None:
        assert METRIC_AEC_WINDOWS == "sovyx.voice.aec.windows"


# ── Record helpers — no-op safety ────────────────────────────────────────


class TestRecordHelpersNoOp:
    """When the metrics registry is torn down both helpers must no-op."""

    def test_record_aec_erle_without_registry(self) -> None:
        # No setup_metrics call → get_metrics returns a torn-down stub.
        record_aec_erle(erle_db=20.0)  # Must not raise.

    def test_record_aec_window_without_registry(self) -> None:
        record_aec_window(state="processed")
        record_aec_window(state="render_silent")


# ── Record helper — happy paths ──────────────────────────────────────────


class TestRecordAecErle:
    def test_emits_one_data_point(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        record_aec_erle(erle_db=25.5)
        metric = _find(_collect(reader), METRIC_AEC_ERLE_DB)
        assert metric is not None
        assert len(metric["data_points"]) == 1
        # Histograms expose count + sum at minimum.
        dp = metric["data_points"][0]
        assert dp["count"] == 1
        assert dp["sum"] == pytest.approx(25.5, abs=0.01)

    def test_records_multiple_samples(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        for v in (10.0, 20.0, 30.0, 40.0):
            record_aec_erle(erle_db=v)
        metric = _find(_collect(reader), METRIC_AEC_ERLE_DB)
        assert metric is not None
        dp = metric["data_points"][0]
        assert dp["count"] == 4
        assert dp["sum"] == pytest.approx(100.0, abs=0.01)


class TestRecordAecWindow:
    def test_processed_state_emits_one_increment(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        record_aec_window(state="processed")
        metric = _find(_collect(reader), METRIC_AEC_WINDOWS)
        assert metric is not None
        attrs = metric["data_points"][0]["attributes"]
        assert attrs == {"state": "processed"}
        assert metric["data_points"][0]["value"] == 1

    def test_render_silent_state_emits_separate_data_point(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        record_aec_window(state="processed")
        record_aec_window(state="render_silent")
        metric = _find(_collect(reader), METRIC_AEC_WINDOWS)
        assert metric is not None
        states = sorted(dp["attributes"]["state"] for dp in metric["data_points"])
        assert states == ["processed", "render_silent"]


# ── FrameNormalizer wire-up — windows counter ────────────────────────────


class TestFrameNormalizerEmitsAecMetrics:
    """The FrameNormalizer's _apply_aec_to_window emits both metrics."""

    def test_processed_state_when_render_non_silent(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        norm = FrameNormalizer(
            source_rate=16_000,
            source_channels=1,
            aec=_RecordingAec(),
            render_provider=_ConstantRenderProvider(value=1000),
        )
        # 512 samples = exactly one emitted window.
        norm.push(np.full(512, 2000, dtype=np.int16))

        windows_metric = _find(_collect(reader), METRIC_AEC_WINDOWS)
        assert windows_metric is not None
        states = [dp["attributes"]["state"] for dp in windows_metric["data_points"]]
        assert states == ["processed"]

    def test_render_silent_state_when_render_zero(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        norm = FrameNormalizer(
            source_rate=16_000,
            source_channels=1,
            aec=_RecordingAec(),
            render_provider=_ConstantRenderProvider(value=0),
        )
        norm.push(np.full(512, 2000, dtype=np.int16))

        windows_metric = _find(_collect(reader), METRIC_AEC_WINDOWS)
        assert windows_metric is not None
        states = [dp["attributes"]["state"] for dp in windows_metric["data_points"]]
        assert states == ["render_silent"]

    def test_erle_emitted_only_for_processed_state(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        norm = FrameNormalizer(
            source_rate=16_000,
            source_channels=1,
            aec=_RecordingAec(),
            render_provider=_ConstantRenderProvider(value=1000),
        )
        # Two non-silent windows.
        norm.push(np.full(1024, 2000, dtype=np.int16))

        erle_metric = _find(_collect(reader), METRIC_AEC_ERLE_DB)
        assert erle_metric is not None
        # Two ERLE samples; the _RecordingAec halves capture so each
        # window's ERLE ≈ 6 dB (10 * log10(4) = 6.02).
        dp = erle_metric["data_points"][0]
        assert dp["count"] == 2
        assert 11.5 <= dp["sum"] <= 12.5  # ~6.02 + ~6.02

    def test_no_erle_emitted_for_silent_render(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        norm = FrameNormalizer(
            source_rate=16_000,
            source_channels=1,
            aec=_RecordingAec(),
            render_provider=_ConstantRenderProvider(value=0),
        )
        norm.push(np.full(1024, 2000, dtype=np.int16))

        erle_metric = _find(_collect(reader), METRIC_AEC_ERLE_DB)
        # ERLE histogram should have no data points (silent windows
        # are explicitly excluded — echo is undefined without a
        # reference signal).
        if erle_metric is not None:
            assert all(dp["count"] == 0 for dp in erle_metric["data_points"])

    def test_no_metrics_when_aec_is_none(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        # Regression-guard: foundation default (aec=None) must NOT
        # emit AEC telemetry. Pre-AEC contract preserved bit-exactly.
        norm = FrameNormalizer(source_rate=16_000, source_channels=1)
        norm.push(np.full(512, 2000, dtype=np.int16))

        data = _collect(reader)
        assert _find(data, METRIC_AEC_WINDOWS) is None
        assert _find(data, METRIC_AEC_ERLE_DB) is None

    def test_processed_count_grows_with_emitted_windows(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        norm = FrameNormalizer(
            source_rate=16_000,
            source_channels=1,
            aec=_RecordingAec(),
            render_provider=_ConstantRenderProvider(value=500),
        )
        # 5 windows × 512 samples each.
        norm.push(np.full(2560, 1500, dtype=np.int16))

        windows_metric = _find(_collect(reader), METRIC_AEC_WINDOWS)
        assert windows_metric is not None
        # Single (state="processed") data point with value=5.
        assert len(windows_metric["data_points"]) == 1
        assert windows_metric["data_points"][0]["value"] == 5
