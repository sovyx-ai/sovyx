"""Tests for :mod:`sovyx.voice.health._metrics` — §5.8 metrics facade.

Verifies the 10 VCHL instruments are wired on the central
:class:`MetricsRegistry` with the ADR-mandated names, that the record
helpers emit exactly one data point per call with the documented label
surface, and that the facade is safe to call when metrics are torn down
(no-op behaviour).
"""

from __future__ import annotations

from typing import Any

import pytest
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from sovyx.observability.metrics import (
    MetricsRegistry,
    setup_metrics,
    teardown_metrics,
)
from sovyx.voice.health._metrics import (
    METRIC_ACTIVE_ENDPOINT_CHANGES,
    METRIC_CASCADE_ATTEMPTS,
    METRIC_COMBO_STORE_HITS,
    METRIC_COMBO_STORE_INVALIDATIONS,
    METRIC_PREFLIGHT_FAILURES,
    METRIC_PROBE_DIAGNOSIS,
    METRIC_PROBE_DURATION,
    METRIC_RECOVERY_ATTEMPTS,
    METRIC_SELF_FEEDBACK_BLOCKS,
    METRIC_TIME_TO_FIRST_UTTERANCE,
    record_active_endpoint_change,
    record_cascade_attempt,
    record_combo_store_hit,
    record_combo_store_invalidation,
    record_preflight_failure,
    record_probe_diagnosis,
    record_probe_duration,
    record_probe_result,
    record_recovery_attempt,
    record_self_feedback_block,
    record_time_to_first_utterance,
)
from sovyx.voice.health.contract import Combo, Diagnosis, ProbeMode, ProbeResult


def _collect(reader: InMemoryMetricReader) -> list[dict[str, Any]]:
    from sovyx.observability.metrics import collect_json

    return collect_json(reader)


def _find(data: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    for m in data:
        if m["name"] == name:
            return m
    return None


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


# ── Name constants ────────────────────────────────────────────────────────


class TestStableNameContract:
    """§5.8 promises these exact names — any rename breaks dashboards."""

    def test_cascade_attempts_name(self) -> None:
        assert METRIC_CASCADE_ATTEMPTS == "sovyx.voice.health.cascade.attempts"

    def test_combo_store_hits_name(self) -> None:
        assert METRIC_COMBO_STORE_HITS == "sovyx.voice.health.combo_store.hits"

    def test_combo_store_invalidations_name(self) -> None:
        assert METRIC_COMBO_STORE_INVALIDATIONS == "sovyx.voice.health.combo_store.invalidations"

    def test_probe_diagnosis_name(self) -> None:
        assert METRIC_PROBE_DIAGNOSIS == "sovyx.voice.health.probe.diagnosis"

    def test_probe_duration_name(self) -> None:
        assert METRIC_PROBE_DURATION == "sovyx.voice.health.probe.duration"

    def test_preflight_failures_name(self) -> None:
        assert METRIC_PREFLIGHT_FAILURES == "sovyx.voice.health.preflight.failures"

    def test_recovery_attempts_name(self) -> None:
        assert METRIC_RECOVERY_ATTEMPTS == "sovyx.voice.health.recovery.attempts"

    def test_self_feedback_blocks_name(self) -> None:
        assert METRIC_SELF_FEEDBACK_BLOCKS == "sovyx.voice.health.self_feedback.blocks"

    def test_active_endpoint_changes_name(self) -> None:
        assert METRIC_ACTIVE_ENDPOINT_CHANGES == "sovyx.voice.health.active_endpoint.changes"

    def test_time_to_first_utterance_name(self) -> None:
        assert METRIC_TIME_TO_FIRST_UTTERANCE == "sovyx.voice.health.time_to_first_utterance"


# ── Record helpers ────────────────────────────────────────────────────────


class TestRecordCascadeAttempt:
    def test_emits_one_point_with_all_labels(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        record_cascade_attempt(
            platform="win32",
            host_api="WASAPI",
            success=True,
            source="cascade",
        )
        metric = _find(_collect(reader), METRIC_CASCADE_ATTEMPTS)
        assert metric is not None
        assert len(metric["data_points"]) == 1
        attrs = metric["data_points"][0]["attributes"]
        assert attrs == {
            "platform": "win32",
            "host_api": "WASAPI",
            "success": "true",
            "source": "cascade",
        }
        assert metric["data_points"][0]["value"] == 1

    def test_success_false_label_is_string(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        record_cascade_attempt(platform="linux", host_api="ALSA", success=False, source="pinned")
        metric = _find(_collect(reader), METRIC_CASCADE_ATTEMPTS)
        assert metric is not None
        assert metric["data_points"][0]["attributes"]["success"] == "false"

    def test_missing_host_api_defaults_to_unknown(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        record_cascade_attempt(platform="darwin", host_api="", success=True, source="store")
        metric = _find(_collect(reader), METRIC_CASCADE_ATTEMPTS)
        assert metric is not None
        assert metric["data_points"][0]["attributes"]["host_api"] == "unknown"


class TestRecordComboStoreHit:
    def test_emits_hit_with_labels(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        record_combo_store_hit(endpoint_class="Headset", result="hit")
        metric = _find(_collect(reader), METRIC_COMBO_STORE_HITS)
        assert metric is not None
        assert metric["data_points"][0]["attributes"] == {
            "endpoint_class": "Headset",
            "result": "hit",
        }

    def test_missing_endpoint_class_defaults_to_unknown(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        record_combo_store_hit(endpoint_class="", result="miss")
        metric = _find(_collect(reader), METRIC_COMBO_STORE_HITS)
        assert metric is not None
        assert metric["data_points"][0]["attributes"]["endpoint_class"] == "unknown"

    def test_three_results_produce_three_series(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        record_combo_store_hit(endpoint_class="Audio", result="hit")
        record_combo_store_hit(endpoint_class="Audio", result="miss")
        record_combo_store_hit(endpoint_class="Audio", result="needs_revalidation")
        metric = _find(_collect(reader), METRIC_COMBO_STORE_HITS)
        assert metric is not None
        results = {p["attributes"]["result"] for p in metric["data_points"]}
        assert results == {"hit", "miss", "needs_revalidation"}


class TestRecordComboStoreInvalidation:
    def test_emits_with_reason(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        record_combo_store_invalidation(reason="fingerprint_drift")
        metric = _find(_collect(reader), METRIC_COMBO_STORE_INVALIDATIONS)
        assert metric is not None
        assert metric["data_points"][0]["attributes"] == {"reason": "fingerprint_drift"}

    def test_empty_reason_defaults_to_unknown(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        record_combo_store_invalidation(reason="")
        metric = _find(_collect(reader), METRIC_COMBO_STORE_INVALIDATIONS)
        assert metric is not None
        assert metric["data_points"][0]["attributes"]["reason"] == "unknown"


class TestRecordProbeDiagnosisAndDuration:
    def test_diagnosis_and_mode_labels(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        record_probe_diagnosis(diagnosis=Diagnosis.HEALTHY, mode=ProbeMode.COLD)
        metric = _find(_collect(reader), METRIC_PROBE_DIAGNOSIS)
        assert metric is not None
        attrs = metric["data_points"][0]["attributes"]
        assert attrs["diagnosis"] == "healthy"
        assert attrs["mode"] == "cold"

    def test_duration_records_histogram(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        record_probe_duration(duration_ms=42.5, mode=ProbeMode.WARM)
        metric = _find(_collect(reader), METRIC_PROBE_DURATION)
        assert metric is not None
        assert metric["data_points"][0]["attributes"]["mode"] == "warm"
        # Histogram data: sum should be 42.5, count should be 1.
        dp = metric["data_points"][0]
        assert dp["sum"] == pytest.approx(42.5)
        assert dp["count"] == 1

    def test_record_probe_result_emits_both(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        combo = Combo(
            host_api="WASAPI",
            sample_rate=48_000,
            channels=1,
            sample_format="int16",
            exclusive=False,
            auto_convert=False,
            frames_per_buffer=512,
            platform_key="win32",
        )
        result = ProbeResult(
            diagnosis=Diagnosis.NO_SIGNAL,
            mode=ProbeMode.COLD,
            combo=combo,
            vad_max_prob=None,
            vad_mean_prob=None,
            rms_db=-80.0,
            callbacks_fired=0,
            duration_ms=123,
        )
        record_probe_result(result)
        diag = _find(_collect(reader), METRIC_PROBE_DIAGNOSIS)
        dur = _find(_collect(reader), METRIC_PROBE_DURATION)
        assert diag is not None
        assert dur is not None
        assert diag["data_points"][0]["attributes"]["diagnosis"] == "no_signal"
        assert dur["data_points"][0]["sum"] == pytest.approx(123)


class TestRecordPreflightFailure:
    def test_emits_with_step_and_code(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        record_preflight_failure(step="audio_service_running", code="audio_service_down")
        metric = _find(_collect(reader), METRIC_PREFLIGHT_FAILURES)
        assert metric is not None
        attrs = metric["data_points"][0]["attributes"]
        assert attrs == {"step": "audio_service_running", "code": "audio_service_down"}


class TestRecordRecoveryAttempt:
    @pytest.mark.parametrize(
        "trigger",
        ["deaf_backoff", "hotplug", "default_change", "power", "audio_service"],
    )
    def test_emits_with_trigger(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
        trigger: str,
    ) -> None:
        record_recovery_attempt(trigger=trigger)
        metric = _find(_collect(reader), METRIC_RECOVERY_ATTEMPTS)
        assert metric is not None
        assert metric["data_points"][0]["attributes"]["trigger"] == trigger


class TestRecordSelfFeedbackBlock:
    @pytest.mark.parametrize("layer", ["gate", "duck", "spectral"])
    def test_emits_with_layer(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
        layer: str,
    ) -> None:
        record_self_feedback_block(layer=layer)
        metric = _find(_collect(reader), METRIC_SELF_FEEDBACK_BLOCKS)
        assert metric is not None
        assert metric["data_points"][0]["attributes"]["layer"] == layer


class TestRecordActiveEndpointChange:
    @pytest.mark.parametrize("reason", ["hotplug", "default", "manual", "recovery"])
    def test_emits_with_reason(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
        reason: str,
    ) -> None:
        record_active_endpoint_change(reason=reason)
        metric = _find(_collect(reader), METRIC_ACTIVE_ENDPOINT_CHANGES)
        assert metric is not None
        assert metric["data_points"][0]["attributes"]["reason"] == reason


class TestRecordTimeToFirstUtterance:
    def test_records_histogram_no_labels(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        record_time_to_first_utterance(duration_ms=180.0)
        record_time_to_first_utterance(duration_ms=220.0)
        metric = _find(_collect(reader), METRIC_TIME_TO_FIRST_UTTERANCE)
        assert metric is not None
        dp = metric["data_points"][0]
        assert dp["count"] == 2
        assert dp["sum"] == pytest.approx(400.0)
        # KPI has no labels by design.
        assert dp["attributes"] == {}


# ── Graceful no-op when metrics are torn down ─────────────────────────────


class TestNoOpSafety:
    """All helpers must be safe to call when setup_metrics was never invoked."""

    def test_all_helpers_are_noop_safe(self) -> None:
        teardown_metrics()
        record_cascade_attempt(platform="win32", host_api="WASAPI", success=True, source="cascade")
        record_combo_store_hit(endpoint_class="Audio", result="hit")
        record_combo_store_invalidation(reason="test")
        record_probe_diagnosis(diagnosis=Diagnosis.HEALTHY, mode=ProbeMode.COLD)
        record_probe_duration(duration_ms=10.0, mode=ProbeMode.COLD)
        record_preflight_failure(step="x", code="y")
        record_recovery_attempt(trigger="deaf_backoff")
        record_self_feedback_block(layer="gate")
        record_active_endpoint_change(reason="hotplug")
        record_time_to_first_utterance(duration_ms=200.0)
