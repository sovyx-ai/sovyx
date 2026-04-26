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
    METRIC_BYPASS_TIER1_RAW_ATTEMPTED,
    METRIC_BYPASS_TIER1_RAW_OUTCOME,
    METRIC_BYPASS_TIER2_HOST_API_ROTATE_ATTEMPTED,
    METRIC_BYPASS_TIER2_HOST_API_ROTATE_OUTCOME,
    METRIC_CASCADE_ATTEMPTS,
    METRIC_COMBO_STORE_HITS,
    METRIC_COMBO_STORE_INVALIDATIONS,
    METRIC_HOTPLUG_LISTENER_REGISTERED,
    METRIC_OPENER_HOST_API_ALIGNMENT,
    METRIC_PREFLIGHT_FAILURES,
    METRIC_PROBE_COLD_SILENCE_REJECTED,
    METRIC_PROBE_DIAGNOSIS,
    METRIC_PROBE_DURATION,
    METRIC_PROBE_START_TIME_ERRORS,
    METRIC_RECOVERY_ATTEMPTS,
    METRIC_SELF_FEEDBACK_BLOCKS,
    METRIC_TIME_TO_FIRST_UTTERANCE,
    record_active_endpoint_change,
    record_cascade_attempt,
    record_cold_silence_rejected,
    record_combo_store_hit,
    record_combo_store_invalidation,
    record_hotplug_listener_registered,
    record_opener_host_api_alignment,
    record_preflight_failure,
    record_probe_diagnosis,
    record_probe_duration,
    record_probe_result,
    record_recovery_attempt,
    record_self_feedback_block,
    record_start_time_error,
    record_tier1_raw_attempted,
    record_tier1_raw_outcome,
    record_tier2_host_api_rotate_attempted,
    record_tier2_host_api_rotate_outcome,
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

    def test_probe_start_time_errors_name(self) -> None:
        # v0.20.2 §4.4.7 / Bug A — must live under the established
        # ``sovyx.voice.health.*`` namespace so existing Grafana panels
        # can fold it into the same VCHL boards.
        assert METRIC_PROBE_START_TIME_ERRORS == "sovyx.voice.health.probe.start_time_errors"

    # ── Voice Windows Paranoid Mission counters (foundation v0.24.0) ─

    def test_probe_cold_silence_rejected_name(self) -> None:
        assert (
            METRIC_PROBE_COLD_SILENCE_REJECTED == "sovyx.voice.health.probe.cold_silence_rejected"
        )

    def test_bypass_tier1_raw_attempted_name(self) -> None:
        assert METRIC_BYPASS_TIER1_RAW_ATTEMPTED == "sovyx.voice.health.bypass.tier1_raw.attempted"

    def test_bypass_tier1_raw_outcome_name(self) -> None:
        assert METRIC_BYPASS_TIER1_RAW_OUTCOME == "sovyx.voice.health.bypass.tier1_raw.outcome"

    def test_bypass_tier2_host_api_rotate_attempted_name(self) -> None:
        assert (
            METRIC_BYPASS_TIER2_HOST_API_ROTATE_ATTEMPTED
            == "sovyx.voice.health.bypass.tier2_host_api_rotate.attempted"
        )

    def test_bypass_tier2_host_api_rotate_outcome_name(self) -> None:
        assert (
            METRIC_BYPASS_TIER2_HOST_API_ROTATE_OUTCOME
            == "sovyx.voice.health.bypass.tier2_host_api_rotate.outcome"
        )

    def test_opener_host_api_alignment_name(self) -> None:
        assert METRIC_OPENER_HOST_API_ALIGNMENT == "sovyx.voice.opener.host_api_alignment"

    def test_hotplug_listener_registered_name(self) -> None:
        assert METRIC_HOTPLUG_LISTENER_REGISTERED == "sovyx.voice.hotplug.listener.registered"


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


class TestRecordStartTimeError:
    """v0.20.2 Phase 1 / Bug A — probe stream.start() failure counter."""

    def test_emits_with_diagnosis_host_api_platform(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        record_start_time_error(
            diagnosis=Diagnosis.KERNEL_INVALIDATED,
            host_api="WASAPI",
            platform="win32",
        )
        metric = _find(_collect(reader), METRIC_PROBE_START_TIME_ERRORS)
        assert metric is not None
        assert len(metric["data_points"]) == 1
        attrs = metric["data_points"][0]["attributes"]
        assert attrs == {
            "diagnosis": "kernel_invalidated",
            "host_api": "WASAPI",
            "platform": "win32",
        }
        assert metric["data_points"][0]["value"] == 1

    def test_missing_host_api_defaults_to_unknown(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        record_start_time_error(
            diagnosis=Diagnosis.DEVICE_BUSY,
            host_api="",
            platform="win32",
        )
        metric = _find(_collect(reader), METRIC_PROBE_START_TIME_ERRORS)
        assert metric is not None
        assert metric["data_points"][0]["attributes"]["host_api"] == "unknown"

    def test_distinct_diagnoses_produce_distinct_series(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        record_start_time_error(
            diagnosis=Diagnosis.KERNEL_INVALIDATED,
            host_api="WASAPI",
            platform="win32",
        )
        record_start_time_error(
            diagnosis=Diagnosis.DEVICE_BUSY,
            host_api="WASAPI",
            platform="win32",
        )
        metric = _find(_collect(reader), METRIC_PROBE_START_TIME_ERRORS)
        assert metric is not None
        diagnoses = {dp["attributes"]["diagnosis"] for dp in metric["data_points"]}
        assert diagnoses == {
            "kernel_invalidated",
            "device_busy",
        }


# ── Voice Windows Paranoid Mission record helpers ─────────────────────────


class TestRecordColdSilenceRejected:
    """Furo W-1 telemetry — counts cold-probe silence-rejection events."""

    def test_strict_reject_emits_one_point(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        record_cold_silence_rejected(mode="strict_reject", host_api="MME")
        metric = _find(_collect(reader), METRIC_PROBE_COLD_SILENCE_REJECTED)
        assert metric is not None
        assert len(metric["data_points"]) == 1
        assert metric["data_points"][0]["attributes"] == {
            "mode": "strict_reject",
            "host_api": "MME",
        }
        assert metric["data_points"][0]["value"] == 1

    def test_lenient_passthrough_separate_label(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        record_cold_silence_rejected(mode="lenient_passthrough", host_api="Windows DirectSound")
        metric = _find(_collect(reader), METRIC_PROBE_COLD_SILENCE_REJECTED)
        assert metric is not None
        attrs = metric["data_points"][0]["attributes"]
        assert attrs["mode"] == "lenient_passthrough"
        assert attrs["host_api"] == "Windows DirectSound"

    def test_missing_host_api_defaults_to_unknown(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        record_cold_silence_rejected(mode="strict_reject", host_api="")
        metric = _find(_collect(reader), METRIC_PROBE_COLD_SILENCE_REJECTED)
        assert metric is not None
        assert metric["data_points"][0]["attributes"]["host_api"] == "unknown"


class TestRecordTier1Raw:
    """Tier 1 RAW + Communications bypass attempt + outcome counters."""

    def test_attempted_emits_with_raw_supported_true(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        record_tier1_raw_attempted(host_api="Windows WASAPI", raw_supported=True)
        metric = _find(_collect(reader), METRIC_BYPASS_TIER1_RAW_ATTEMPTED)
        assert metric is not None
        assert metric["data_points"][0]["attributes"] == {
            "host_api": "Windows WASAPI",
            "raw_supported": "true",
        }

    def test_attempted_with_raw_supported_false(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        record_tier1_raw_attempted(host_api="MME", raw_supported=False)
        metric = _find(_collect(reader), METRIC_BYPASS_TIER1_RAW_ATTEMPTED)
        assert metric is not None
        assert metric["data_points"][0]["attributes"]["raw_supported"] == "false"

    def test_outcome_records_verdict(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        record_tier1_raw_outcome(verdict="raw_engaged", host_api="Windows WASAPI")
        metric = _find(_collect(reader), METRIC_BYPASS_TIER1_RAW_OUTCOME)
        assert metric is not None
        assert metric["data_points"][0]["attributes"] == {
            "verdict": "raw_engaged",
            "host_api": "Windows WASAPI",
        }


class TestRecordTier2HostApiRotate:
    """Tier 2 host_api_rotate-then-exclusive Phase A + combined-outcome counters."""

    def test_attempted_records_source_and_target(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        record_tier2_host_api_rotate_attempted(
            source_host_api="MME",
            target_host_api="Windows WASAPI",
        )
        metric = _find(_collect(reader), METRIC_BYPASS_TIER2_HOST_API_ROTATE_ATTEMPTED)
        assert metric is not None
        assert metric["data_points"][0]["attributes"] == {
            "source_host_api": "MME",
            "target_host_api": "Windows WASAPI",
        }

    def test_outcome_records_two_phase_verdicts(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        record_tier2_host_api_rotate_outcome(
            phase_a_verdict="rotated_success",
            phase_b_verdict="exclusive_engaged",
            resulting_host_api="Windows WASAPI",
        )
        metric = _find(_collect(reader), METRIC_BYPASS_TIER2_HOST_API_ROTATE_OUTCOME)
        assert metric is not None
        assert metric["data_points"][0]["attributes"] == {
            "phase_a_verdict": "rotated_success",
            "phase_b_verdict": "exclusive_engaged",
            "resulting_host_api": "Windows WASAPI",
        }

    def test_outcome_with_skipped_phase_b(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        """When Phase A fails, Phase B is skipped; the metric records that."""
        record_tier2_host_api_rotate_outcome(
            phase_a_verdict="no_target_sibling",
            phase_b_verdict="skipped",
        )
        metric = _find(_collect(reader), METRIC_BYPASS_TIER2_HOST_API_ROTATE_OUTCOME)
        assert metric is not None
        assert metric["data_points"][0]["attributes"]["phase_b_verdict"] == "skipped"


class TestRecordOpenerHostApiAlignment:
    """Furo W-4 cascade ↔ runtime alignment SLI counter."""

    def test_aligned_true_records_match(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        record_opener_host_api_alignment(
            aligned=True,
            cascade_winner_host_api="Windows DirectSound",
            runtime_chain_head_host_api="Windows DirectSound",
        )
        metric = _find(_collect(reader), METRIC_OPENER_HOST_API_ALIGNMENT)
        assert metric is not None
        attrs = metric["data_points"][0]["attributes"]
        assert attrs["aligned"] == "true"
        assert attrs["cascade_winner_host_api"] == "Windows DirectSound"
        assert attrs["runtime_chain_head_host_api"] == "Windows DirectSound"

    def test_aligned_false_records_drift(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        """The bug signature: cascade picked DirectSound but runtime drifted to MME."""
        record_opener_host_api_alignment(
            aligned=False,
            cascade_winner_host_api="Windows DirectSound",
            runtime_chain_head_host_api="MME",
        )
        metric = _find(_collect(reader), METRIC_OPENER_HOST_API_ALIGNMENT)
        assert metric is not None
        assert metric["data_points"][0]["attributes"]["aligned"] == "false"


class TestRecordHotplugListenerRegistered:
    """IMMNotificationClient registration health counter."""

    def test_registered_true(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        record_hotplug_listener_registered(registered=True)
        metric = _find(_collect(reader), METRIC_HOTPLUG_LISTENER_REGISTERED)
        assert metric is not None
        assert metric["data_points"][0]["attributes"] == {
            "registered": "true",
            "error": "none",
        }

    def test_registered_false_with_error(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        record_hotplug_listener_registered(registered=False, error="comtypes_unavailable")
        metric = _find(_collect(reader), METRIC_HOTPLUG_LISTENER_REGISTERED)
        assert metric is not None
        assert metric["data_points"][0]["attributes"] == {
            "registered": "false",
            "error": "comtypes_unavailable",
        }


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
        record_start_time_error(
            diagnosis=Diagnosis.KERNEL_INVALIDATED,
            host_api="WASAPI",
            platform="win32",
        )
        record_time_to_first_utterance(duration_ms=200.0)
        # Voice Windows Paranoid Mission counters — must also be no-op safe.
        record_cold_silence_rejected(mode="strict_reject", host_api="MME")
        record_tier1_raw_attempted(host_api="Windows WASAPI", raw_supported=True)
        record_tier1_raw_outcome(verdict="raw_engaged", host_api="Windows WASAPI")
        record_tier2_host_api_rotate_attempted(
            source_host_api="MME",
            target_host_api="Windows WASAPI",
        )
        record_tier2_host_api_rotate_outcome(
            phase_a_verdict="rotated_success",
            phase_b_verdict="exclusive_engaged",
        )
        record_opener_host_api_alignment(aligned=True)
        record_hotplug_listener_registered(registered=True)
