"""Tests for the NS observability stack [Phase 4 T4.16].

Mirror of ``test_aec_telemetry.py`` for the noise-suppression
stage. Coverage:

* Metric name constants pin the wire contract.
* Record helpers no-op safely when the registry is torn down.
* :class:`FrameNormalizer` emits ``voice.ns.windows{processed}``
  + ``voice.ns.suppression_db`` when NS attenuates a window.
* :class:`FrameNormalizer` emits ``voice.ns.windows{passthrough}``
  WITHOUT a suppression sample when the gate finds nothing to
  attenuate (sub-0.5-dB drift only).
* The ``noise_suppressor=None`` branch is fully passive (no
  emissions).
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
    METRIC_NS_SUPPRESSION_DB,
    METRIC_NS_WINDOWS,
    record_ns_suppression_db,
    record_ns_window,
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


class _AttenuatingNs:
    """NS that halves the window — guaranteed ~6 dB suppression."""

    def process(self, frame: np.ndarray) -> np.ndarray:
        return (frame.astype(np.int32) // 2).astype(np.int16)

    def reset(self) -> None: ...


class _NoOpNs:
    """NS that returns input verbatim — 0 dB suppression."""

    def process(self, frame: np.ndarray) -> np.ndarray:
        return frame

    def reset(self) -> None: ...


# ── Stable name constants ────────────────────────────────────────────────


class TestStableNameContract:
    def test_ns_windows_name(self) -> None:
        assert METRIC_NS_WINDOWS == "sovyx.voice.ns.windows"

    def test_ns_suppression_db_name(self) -> None:
        assert METRIC_NS_SUPPRESSION_DB == "sovyx.voice.ns.suppression_db"


# ── Record helpers — no-op safety ────────────────────────────────────────


class TestRecordHelpersNoOp:
    def test_record_ns_window_without_registry(self) -> None:
        record_ns_window(state="processed")
        record_ns_window(state="passthrough")

    def test_record_ns_suppression_db_without_registry(self) -> None:
        record_ns_suppression_db(suppression_db=10.0)


# ── Record helpers — happy paths ─────────────────────────────────────────


class TestRecordNsWindow:
    def test_processed_state_emits_one_increment(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        record_ns_window(state="processed")
        metric = _find(_collect(reader), METRIC_NS_WINDOWS)
        assert metric is not None
        attrs = metric["data_points"][0]["attributes"]
        assert attrs == {"state": "processed"}

    def test_passthrough_state_emits_separate_data_point(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        record_ns_window(state="processed")
        record_ns_window(state="passthrough")
        metric = _find(_collect(reader), METRIC_NS_WINDOWS)
        assert metric is not None
        states = sorted(dp["attributes"]["state"] for dp in metric["data_points"])
        assert states == ["passthrough", "processed"]


class TestRecordNsSuppressionDb:
    def test_emits_one_data_point(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        record_ns_suppression_db(suppression_db=12.5)
        metric = _find(_collect(reader), METRIC_NS_SUPPRESSION_DB)
        assert metric is not None
        dp = metric["data_points"][0]
        assert dp["count"] == 1
        assert dp["sum"] == pytest.approx(12.5, abs=0.01)


# ── FrameNormalizer wire-up ──────────────────────────────────────────────


class TestFrameNormalizerEmitsNsMetrics:
    def test_no_metrics_when_ns_unwired(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        # Foundation default: ns=None → no NS metric emitted.
        norm = FrameNormalizer(source_rate=16_000, source_channels=1)
        norm.push(np.full(512, 2000, dtype=np.int16))
        data = _collect(reader)
        assert _find(data, METRIC_NS_WINDOWS) is None
        assert _find(data, METRIC_NS_SUPPRESSION_DB) is None

    def test_processed_state_when_ns_attenuates(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        norm = FrameNormalizer(
            source_rate=16_000,
            source_channels=1,
            noise_suppressor=_AttenuatingNs(),
        )
        norm.push(np.full(512, 8000, dtype=np.int16))

        windows_metric = _find(_collect(reader), METRIC_NS_WINDOWS)
        assert windows_metric is not None
        states = [dp["attributes"]["state"] for dp in windows_metric["data_points"]]
        assert states == ["processed"]

    def test_suppression_db_emitted_for_attenuated_window(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        norm = FrameNormalizer(
            source_rate=16_000,
            source_channels=1,
            noise_suppressor=_AttenuatingNs(),
        )
        # Halving the window's int16 amplitude → ~6 dB drop.
        norm.push(np.full(512, 8000, dtype=np.int16))

        sup_metric = _find(_collect(reader), METRIC_NS_SUPPRESSION_DB)
        assert sup_metric is not None
        dp = sup_metric["data_points"][0]
        assert dp["count"] == 1
        # 20*log10(2) ≈ 6.02 dB — allow generous margin for int
        # division rounding on small samples.
        assert 5.0 < dp["sum"] < 7.5

    def test_passthrough_state_when_ns_returns_input(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        # NoOp NS — output identical to input → suppression < 0.5 dB.
        norm = FrameNormalizer(
            source_rate=16_000,
            source_channels=1,
            noise_suppressor=_NoOpNs(),
        )
        norm.push(np.full(512, 2000, dtype=np.int16))

        windows_metric = _find(_collect(reader), METRIC_NS_WINDOWS)
        assert windows_metric is not None
        states = [dp["attributes"]["state"] for dp in windows_metric["data_points"]]
        assert states == ["passthrough"]

    def test_no_suppression_db_for_passthrough(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        # Passthrough windows must NOT emit suppression samples
        # (would distort histogram p50 with floor-level zeros).
        norm = FrameNormalizer(
            source_rate=16_000,
            source_channels=1,
            noise_suppressor=_NoOpNs(),
        )
        norm.push(np.full(512, 2000, dtype=np.int16))

        sup_metric = _find(_collect(reader), METRIC_NS_SUPPRESSION_DB)
        if sup_metric is not None:
            assert all(dp["count"] == 0 for dp in sup_metric["data_points"])

    def test_processed_count_grows_with_emitted_windows(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        norm = FrameNormalizer(
            source_rate=16_000,
            source_channels=1,
            noise_suppressor=_AttenuatingNs(),
        )
        # 5 windows × 512 samples each.
        norm.push(np.full(2560, 4000, dtype=np.int16))

        windows_metric = _find(_collect(reader), METRIC_NS_WINDOWS)
        assert windows_metric is not None
        # Single (state="processed") data point with value=5.
        assert len(windows_metric["data_points"]) == 1
        assert windows_metric["data_points"][0]["value"] == 5
