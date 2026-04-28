"""Tests for :mod:`sovyx.voice._stage_metrics`.

Covers M2's three reusable surfaces:

* ``record_stage_event`` — RED Rate + Errors counter with bucketed
  ``error_type`` cardinality protection.
* ``measure_stage_duration`` — async/sync context manager that
  records duration + outcome (success / error) on exit.
* ``record_queue_depth`` — USE Utilisation + Saturation paired
  histograms with capacity-required loud-fail.

Reference: MISSION-voice-mixer-enterprise-refactor-2026-04-25 §2.6
(Ring 6), §3.10 M2.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from sovyx.observability.metrics import (
    MetricsRegistry,
    setup_metrics,
    teardown_metrics,
)
from sovyx.voice._stage_metrics import (
    _ERROR_TYPE_BUCKET_MAXSIZE,
    StageEventKind,
    StageOutcome,
    VoiceStage,
    _bucket_engine_family,
    measure_stage_duration,
    record_queue_depth,
    record_stage_event,
    record_tts_synthesis_latency,
    reset_error_type_bucket_for_tests,
)

if TYPE_CHECKING:
    from collections.abc import Generator

# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture()
def reader() -> InMemoryMetricReader:
    return InMemoryMetricReader()


@pytest.fixture(autouse=True)
def _reset_otel() -> Generator[None, None, None]:
    """Reset OTel global state between tests (per existing test_metrics.py pattern)."""
    from opentelemetry.metrics import _internal as otel_internal

    yield
    otel_internal._METER_PROVIDER_SET_ONCE._done = False  # type: ignore[attr-defined]
    otel_internal._METER_PROVIDER = None  # type: ignore[attr-defined]


@pytest.fixture(autouse=True)
def _reset_error_type_bucket() -> Generator[None, None, None]:
    """Fresh error_type cardinality bucket per test — module-level state."""
    reset_error_type_bucket_for_tests()
    yield
    reset_error_type_bucket_for_tests()


@pytest.fixture()
def registry(reader: InMemoryMetricReader) -> Generator[MetricsRegistry, None, None]:
    reg = setup_metrics(readers=[reader])
    yield reg
    teardown_metrics()


def _collect(reader: InMemoryMetricReader) -> list[Any]:
    """Flatten reader → list[Metric] across resource/scope hierarchy."""
    data = reader.get_metrics_data()
    if data is None:
        return []
    out: list[Any] = []
    for resource_metrics in data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            out.extend(scope_metrics.metrics)
    return out


def _find(metrics: list[Any], name: str) -> Any | None:
    for m in metrics:
        if m.name == name:
            return m
    return None


# ── VoiceStage / StageEventKind / StageOutcome enum invariants ──────


class TestVoiceStageEnum:
    def test_values_are_lowercase_strings(self) -> None:
        for stage in VoiceStage:
            assert isinstance(stage.value, str)
            assert stage.value == stage.value.lower()

    def test_closed_set_size_is_5(self) -> None:
        """Capture / VAD / STT / TTS / Output — adding a stage requires
        a deliberate metric-cardinality decision (this guard makes it
        loud)."""
        assert len(list(VoiceStage)) == 5

    def test_str_enum_value_comparison(self) -> None:
        """Anti-pattern #9 — string equality must work (xdist-safe)."""
        assert VoiceStage.STT == "stt"
        assert VoiceStage.STT.value == "stt"

    def test_kind_three_way_split(self) -> None:
        kinds = {k.value for k in StageEventKind}
        assert kinds == {"success", "error", "drop"}

    def test_outcome_two_way_split(self) -> None:
        """Drop folds into success for duration purposes."""
        outcomes = {o.value for o in StageOutcome}
        assert outcomes == {"success", "error"}


# ── record_stage_event ──────────────────────────────────────────────


class TestRecordStageEvent:
    def test_success_emits_counter_with_none_error_type(
        self,
        reader: InMemoryMetricReader,
        registry: MetricsRegistry,  # noqa: ARG002
    ) -> None:
        record_stage_event(VoiceStage.CAPTURE, StageEventKind.SUCCESS)
        metric = _find(_collect(reader), "sovyx.voice.stage.events")
        assert metric is not None
        points = list(metric.data.data_points)
        assert len(points) == 1
        attrs = dict(points[0].attributes)
        assert attrs == {"stage": "capture", "kind": "success", "error_type": "none"}
        assert points[0].value == 1

    def test_error_with_explicit_error_type(
        self,
        reader: InMemoryMetricReader,
        registry: MetricsRegistry,  # noqa: ARG002
    ) -> None:
        record_stage_event(
            VoiceStage.STT,
            StageEventKind.ERROR,
            error_type="TimeoutError",
        )
        metric = _find(_collect(reader), "sovyx.voice.stage.events")
        attrs = dict(next(iter(metric.data.data_points)).attributes)
        assert attrs["error_type"] == "TimeoutError"
        assert attrs["kind"] == "error"

    def test_drop_kind_recorded_distinctly(
        self,
        reader: InMemoryMetricReader,
        registry: MetricsRegistry,  # noqa: ARG002
    ) -> None:
        record_stage_event(VoiceStage.STT, StageEventKind.DROP)
        metric = _find(_collect(reader), "sovyx.voice.stage.events")
        attrs = dict(next(iter(metric.data.data_points)).attributes)
        assert attrs["kind"] == "drop"

    def test_increments_aggregate_per_label_tuple(
        self,
        reader: InMemoryMetricReader,
        registry: MetricsRegistry,  # noqa: ARG002
    ) -> None:
        for _ in range(5):
            record_stage_event(VoiceStage.VAD, StageEventKind.SUCCESS)
        metric = _find(_collect(reader), "sovyx.voice.stage.events")
        points = list(metric.data.data_points)
        assert len(points) == 1
        assert points[0].value == 5

    def test_distinct_kind_creates_distinct_series(
        self,
        reader: InMemoryMetricReader,
        registry: MetricsRegistry,  # noqa: ARG002
    ) -> None:
        record_stage_event(VoiceStage.TTS, StageEventKind.SUCCESS)
        record_stage_event(VoiceStage.TTS, StageEventKind.ERROR, error_type="OOM")
        metric = _find(_collect(reader), "sovyx.voice.stage.events")
        points = list(metric.data.data_points)
        assert len(points) == 2
        kinds = {dict(p.attributes)["kind"] for p in points}
        assert kinds == {"success", "error"}

    def test_error_type_truncated_to_64_chars(
        self,
        reader: InMemoryMetricReader,
        registry: MetricsRegistry,  # noqa: ARG002
    ) -> None:
        long_err = "X" * 200
        record_stage_event(VoiceStage.STT, StageEventKind.ERROR, error_type=long_err)
        metric = _find(_collect(reader), "sovyx.voice.stage.events")
        attrs = dict(next(iter(metric.data.data_points)).attributes)
        # Truncated to 64 chars before bucketing — bucket preserves verbatim.
        assert len(attrs["error_type"]) == 64
        assert attrs["error_type"] == "X" * 64

    def test_empty_string_error_type_becomes_none_label(
        self,
        reader: InMemoryMetricReader,
        registry: MetricsRegistry,  # noqa: ARG002
    ) -> None:
        record_stage_event(VoiceStage.VAD, StageEventKind.SUCCESS, error_type="")
        metric = _find(_collect(reader), "sovyx.voice.stage.events")
        attrs = dict(next(iter(metric.data.data_points)).attributes)
        assert attrs["error_type"] == "none"

    def test_error_type_overflow_collapses_to_other(
        self,
        reader: InMemoryMetricReader,
        registry: MetricsRegistry,  # noqa: ARG002
    ) -> None:
        # Fill the bucket plus 5 more.
        for i in range(_ERROR_TYPE_BUCKET_MAXSIZE + 5):
            record_stage_event(
                VoiceStage.STT,
                StageEventKind.ERROR,
                error_type=f"ErrType_{i}",
            )
        metric = _find(_collect(reader), "sovyx.voice.stage.events")
        labels_seen = {dict(p.attributes)["error_type"] for p in metric.data.data_points}
        # First 32 distinct preserved, the next 5 collapse to "other".
        assert "other" in labels_seen
        # Preserved + "other" — not 32 + 5.
        assert len(labels_seen) == _ERROR_TYPE_BUCKET_MAXSIZE + 1

    def test_works_with_no_active_registry(self) -> None:
        """No-op safety — call without setup_metrics() must not raise."""
        teardown_metrics()
        record_stage_event(VoiceStage.CAPTURE, StageEventKind.SUCCESS)


# ── measure_stage_duration ──────────────────────────────────────────


class TestMeasureStageDuration:
    def test_records_success_outcome_when_body_clean(
        self,
        reader: InMemoryMetricReader,
        registry: MetricsRegistry,  # noqa: ARG002
    ) -> None:
        with measure_stage_duration(VoiceStage.STT):
            pass
        metric = _find(_collect(reader), "sovyx.voice.stage.duration")
        assert metric is not None
        points = list(metric.data.data_points)
        assert len(points) == 1
        attrs = dict(points[0].attributes)
        assert attrs == {"stage": "stt", "outcome": "success"}
        assert points[0].count == 1

    def test_records_error_outcome_when_body_raises(
        self,
        reader: InMemoryMetricReader,
        registry: MetricsRegistry,  # noqa: ARG002
    ) -> None:
        with pytest.raises(RuntimeError), measure_stage_duration(VoiceStage.STT):
            msg = "boom"
            raise RuntimeError(msg)
        metric = _find(_collect(reader), "sovyx.voice.stage.duration")
        attrs = dict(next(iter(metric.data.data_points)).attributes)
        assert attrs["outcome"] == "error"

    def test_records_error_outcome_via_explicit_mark(
        self,
        reader: InMemoryMetricReader,
        registry: MetricsRegistry,  # noqa: ARG002
    ) -> None:
        """Handled-failure path — caller returned a sentinel, didn't raise."""
        with measure_stage_duration(VoiceStage.STT) as token:
            token.mark_error()
        metric = _find(_collect(reader), "sovyx.voice.stage.duration")
        attrs = dict(next(iter(metric.data.data_points)).attributes)
        assert attrs["outcome"] == "error"

    def test_mark_error_idempotent(
        self,
        reader: InMemoryMetricReader,
        registry: MetricsRegistry,  # noqa: ARG002
    ) -> None:
        with measure_stage_duration(VoiceStage.STT) as token:
            token.mark_error()
            token.mark_error()
        metric = _find(_collect(reader), "sovyx.voice.stage.duration")
        attrs = dict(next(iter(metric.data.data_points)).attributes)
        assert attrs["outcome"] == "error"

    def test_records_positive_elapsed_ms(
        self,
        reader: InMemoryMetricReader,
        registry: MetricsRegistry,  # noqa: ARG002
    ) -> None:
        import time

        # Use 100 ms — anti-pattern #22: time.monotonic() Windows tick is
        # ~15.6 ms; sleeps below 50 ms can round to zero.
        with measure_stage_duration(VoiceStage.STT):
            time.sleep(0.1)
        metric = _find(_collect(reader), "sovyx.voice.stage.duration")
        point = next(iter(metric.data.data_points))
        assert point.sum > 0.0
        # Should be in the ballpark of 100 ms (allow generous ±50 ms slack
        # for CI scheduling jitter on the slowest runner).
        assert 50.0 <= point.sum <= 500.0

    def test_distinct_stages_create_distinct_series(
        self,
        reader: InMemoryMetricReader,
        registry: MetricsRegistry,  # noqa: ARG002
    ) -> None:
        with measure_stage_duration(VoiceStage.STT):
            pass
        with measure_stage_duration(VoiceStage.TTS):
            pass
        metric = _find(_collect(reader), "sovyx.voice.stage.duration")
        stages = {dict(p.attributes)["stage"] for p in metric.data.data_points}
        assert stages == {"stt", "tts"}

    def test_works_with_no_active_registry(self) -> None:
        """No-op safety."""
        teardown_metrics()
        with measure_stage_duration(VoiceStage.CAPTURE):
            pass


# ── record_queue_depth ──────────────────────────────────────────────


class TestRecordQueueDepth:
    def test_records_depth_and_saturation(
        self,
        reader: InMemoryMetricReader,
        registry: MetricsRegistry,  # noqa: ARG002
    ) -> None:
        record_queue_depth(VoiceStage.CAPTURE, depth=20, capacity=100)
        metrics = _collect(reader)

        depth_metric = _find(metrics, "sovyx.voice.queue.depth")
        assert depth_metric is not None
        depth_point = next(iter(depth_metric.data.data_points))
        assert dict(depth_point.attributes) == {"owner": "capture"}
        assert depth_point.sum == 20.0

        sat_metric = _find(metrics, "sovyx.voice.queue.saturation_pct")
        assert sat_metric is not None
        sat_point = next(iter(sat_metric.data.data_points))
        assert sat_point.sum == 20.0

    def test_zero_depth_full_capacity_zero_saturation(
        self,
        reader: InMemoryMetricReader,
        registry: MetricsRegistry,  # noqa: ARG002
    ) -> None:
        record_queue_depth(VoiceStage.STT, depth=0, capacity=50)
        sat_metric = _find(_collect(reader), "sovyx.voice.queue.saturation_pct")
        assert next(iter(sat_metric.data.data_points)).sum == 0.0

    def test_depth_equals_capacity_yields_100_pct(
        self,
        reader: InMemoryMetricReader,
        registry: MetricsRegistry,  # noqa: ARG002
    ) -> None:
        record_queue_depth(VoiceStage.TTS, depth=50, capacity=50)
        sat_metric = _find(_collect(reader), "sovyx.voice.queue.saturation_pct")
        assert next(iter(sat_metric.data.data_points)).sum == 100.0

    def test_overflow_clamped_to_100_pct(
        self,
        reader: InMemoryMetricReader,
        registry: MetricsRegistry,  # noqa: ARG002
    ) -> None:
        """Defensive clamp + warning when producer counts depth wrong."""
        record_queue_depth(VoiceStage.TTS, depth=200, capacity=100)
        sat_metric = _find(_collect(reader), "sovyx.voice.queue.saturation_pct")
        assert next(iter(sat_metric.data.data_points)).sum == 100.0

    def test_negative_depth_clamped_to_zero(
        self,
        reader: InMemoryMetricReader,
        registry: MetricsRegistry,  # noqa: ARG002
    ) -> None:
        record_queue_depth(VoiceStage.OUTPUT, depth=-5, capacity=10)
        depth_metric = _find(_collect(reader), "sovyx.voice.queue.depth")
        assert next(iter(depth_metric.data.data_points)).sum == 0.0

    def test_zero_capacity_loud_fail(self) -> None:
        with pytest.raises(ValueError, match="capacity must be"):
            record_queue_depth(VoiceStage.CAPTURE, depth=0, capacity=0)

    def test_negative_capacity_loud_fail(self) -> None:
        with pytest.raises(ValueError, match="capacity must be"):
            record_queue_depth(VoiceStage.CAPTURE, depth=0, capacity=-1)

    def test_distinct_owners_create_distinct_series(
        self,
        reader: InMemoryMetricReader,
        registry: MetricsRegistry,  # noqa: ARG002
    ) -> None:
        record_queue_depth(VoiceStage.CAPTURE, depth=10, capacity=100)
        record_queue_depth(VoiceStage.STT, depth=5, capacity=20)
        depth_metric = _find(_collect(reader), "sovyx.voice.queue.depth")
        owners = {dict(p.attributes)["owner"] for p in depth_metric.data.data_points}
        assert owners == {"capture", "stt"}

    def test_works_with_no_active_registry(self) -> None:
        teardown_metrics()
        record_queue_depth(VoiceStage.CAPTURE, depth=1, capacity=10)


# ── Integration smoke ──────────────────────────────────────────────


class TestRedUseEndToEnd:
    """RED + USE composed against a single registry — sanity for dashboards."""

    def test_full_red_plus_use_one_pass(
        self,
        reader: InMemoryMetricReader,
        registry: MetricsRegistry,  # noqa: ARG002
    ) -> None:
        # Emit 1 success + 1 error + 1 drop.
        record_stage_event(VoiceStage.STT, StageEventKind.SUCCESS)
        record_stage_event(VoiceStage.STT, StageEventKind.ERROR, error_type="OOM")
        record_stage_event(VoiceStage.STT, StageEventKind.DROP)
        with measure_stage_duration(VoiceStage.STT):
            pass
        record_queue_depth(VoiceStage.STT, depth=3, capacity=10)

        metrics = _collect(reader)
        assert _find(metrics, "sovyx.voice.stage.events") is not None
        assert _find(metrics, "sovyx.voice.stage.duration") is not None
        assert _find(metrics, "sovyx.voice.queue.depth") is not None
        assert _find(metrics, "sovyx.voice.queue.saturation_pct") is not None


# ── T1.37: TTS latency histogram (Option B — bucketed engine_family) ─


class TestBucketEngineFamily:
    """Pin the cardinality-bucket contract for the engine_family label.

    T1.37 RFC §Option B requires that the bucket function:

    * extract the language prefix from the voice_id per engine
      naming convention (kokoro: ``<lang>_<name>``; piper:
      ``<lang>-<name>-<size>``)
    * return ``"<engine>:<family>"`` to keep the metric label
      self-describing
    * fall back gracefully on malformed inputs without raising
      (a malformed voice_id is operator-controlled and must not
      crash the synthesis path)
    """

    def test_kokoro_voice_buckets_by_underscore_prefix(self) -> None:
        assert _bucket_engine_family("kokoro", "af_bella") == "kokoro:af"
        assert _bucket_engine_family("kokoro", "am_michael") == "kokoro:am"
        assert _bucket_engine_family("kokoro", "bf_alice") == "kokoro:bf"

    def test_piper_voice_buckets_by_hyphen_prefix(self) -> None:
        assert _bucket_engine_family("piper", "en_US-amy-medium") == "piper:en_US"
        assert _bucket_engine_family("piper", "pt_BR-faber-medium") == "piper:pt_BR"
        assert _bucket_engine_family("piper", "de_DE-thorsten-low") == "piper:de_DE"

    def test_engine_lowercased_and_stripped(self) -> None:
        assert _bucket_engine_family("KOKORO", "af_bella") == "kokoro:af"
        assert _bucket_engine_family("  Piper  ", "en_US-amy-medium") == "piper:en_US"

    def test_empty_engine_collapses_to_unknown(self) -> None:
        assert _bucket_engine_family("", "af_bella") == "unknown:af_bella"

    def test_empty_voice_id_collapses_to_unknown_suffix(self) -> None:
        assert _bucket_engine_family("kokoro", "") == "kokoro:unknown"
        assert _bucket_engine_family("piper", "") == "piper:unknown"
        assert _bucket_engine_family("piper", "   ") == "piper:unknown"

    def test_kokoro_voice_without_underscore_falls_back_to_full_id(self) -> None:
        # A malformed Kokoro voice (no `<lang>_` prefix) shouldn't crash;
        # bucket falls back to the full voice_id so dashboards still
        # see the malformed value instead of silently dropping it.
        assert _bucket_engine_family("kokoro", "malformed") == "kokoro:malformed"

    def test_piper_voice_without_hyphen_falls_back_to_full_id(self) -> None:
        assert _bucket_engine_family("piper", "malformed") == "piper:malformed"

    def test_unknown_engine_uses_full_voice_id_as_family(self) -> None:
        # Future engines surface as "<engine>:<voice_id>" until a
        # convention is documented for that engine.
        assert _bucket_engine_family("xtts", "speaker_0") == "xtts:speaker_0"


class TestRecordTtsSynthesisLatency:
    """Pin the histogram emission contract.

    T1.37 — wired at the chunk_emitted call site in tts_kokoro.py
    + tts_piper.py. The histogram MUST be labelled by
    ``engine_family`` (not raw ``voice_id``) so cardinality stays
    bounded regardless of operator-installed Piper voice count.
    """

    def test_records_under_engine_family_label(
        self,
        reader: InMemoryMetricReader,
        registry: MetricsRegistry,  # noqa: ARG002
    ) -> None:
        record_tts_synthesis_latency("af_bella", 42.5, engine="kokoro")
        metric = _find(_collect(reader), "sovyx.voice.tts.synthesis_latency")
        assert metric is not None
        points = list(metric.data.data_points)
        assert len(points) == 1
        attrs = dict(points[0].attributes)
        assert attrs == {"engine_family": "kokoro:af", "outcome": "success"}
        assert points[0].sum == 42.5

    def test_error_outcome_when_error_flag_set(
        self,
        reader: InMemoryMetricReader,
        registry: MetricsRegistry,  # noqa: ARG002
    ) -> None:
        record_tts_synthesis_latency("en_US-amy-medium", 99.9, engine="piper", error=True)
        metric = _find(_collect(reader), "sovyx.voice.tts.synthesis_latency")
        attrs = dict(next(iter(metric.data.data_points)).attributes)
        assert attrs == {"engine_family": "piper:en_US", "outcome": "error"}

    def test_distinct_families_create_distinct_series(
        self,
        reader: InMemoryMetricReader,
        registry: MetricsRegistry,  # noqa: ARG002
    ) -> None:
        record_tts_synthesis_latency("af_bella", 10.0, engine="kokoro")
        record_tts_synthesis_latency("am_michael", 20.0, engine="kokoro")
        record_tts_synthesis_latency("en_US-amy-medium", 30.0, engine="piper")
        metric = _find(_collect(reader), "sovyx.voice.tts.synthesis_latency")
        families = {dict(p.attributes)["engine_family"] for p in metric.data.data_points}
        assert families == {"kokoro:af", "kokoro:am", "piper:en_US"}

    def test_same_family_different_voices_collapse_to_one_series(
        self,
        reader: InMemoryMetricReader,
        registry: MetricsRegistry,  # noqa: ARG002
    ) -> None:
        """Cardinality bound — 100 distinct voices that share a family
        produce ONE series, not 100. This is the whole point of
        Option B over Option A (naive per-voice label)."""
        for i in range(100):
            record_tts_synthesis_latency(f"af_voice{i:03d}", float(i), engine="kokoro")
        metric = _find(_collect(reader), "sovyx.voice.tts.synthesis_latency")
        families = {dict(p.attributes)["engine_family"] for p in metric.data.data_points}
        assert families == {"kokoro:af"}, (
            f"100 distinct kokoro:af voices MUST collapse to one series; "
            f"got {len(families)} series: {families}"
        )

    def test_negative_duration_clamped_to_zero(
        self,
        reader: InMemoryMetricReader,
        registry: MetricsRegistry,  # noqa: ARG002
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Negative duration is a counting bug — clamp to 0 + log
        WARN so the dashboard isn't poisoned by a nonsensical
        sample."""
        import logging

        with caplog.at_level(logging.WARNING):
            record_tts_synthesis_latency("af_bella", -5.0, engine="kokoro")
        metric = _find(_collect(reader), "sovyx.voice.tts.synthesis_latency")
        point = next(iter(metric.data.data_points))
        assert point.sum == 0.0
        # WARN with action_required.
        warn_records = [
            r
            for r in caplog.records
            if isinstance(r.msg, dict)
            and r.msg.get("event") == "voice.tts.synthesis_latency_negative"
        ]
        assert len(warn_records) == 1
        assert "action_required" in warn_records[0].msg

    def test_works_with_no_active_registry(self) -> None:
        """No-op safety — the daemon must not crash if metrics
        teardown happens before the last TTS chunk lands."""
        teardown_metrics()
        record_tts_synthesis_latency("af_bella", 10.0, engine="kokoro")
