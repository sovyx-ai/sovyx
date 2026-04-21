"""Property tests for the anomaly detector and streaming percentile.

Pins the invariants that the detector must hold under arbitrary input:
the structlog pipeline never raises, dict identity is preserved,
percentiles are monotonic, cooldowns suppress duplicates, and the
self-recursion guard never lets ``anomaly.*`` events feed themselves.

Aligned with IMPL-OBSERVABILITY-001 §8 (anomaly detection) + §11.
"""

from __future__ import annotations

import math
from typing import Any
from unittest.mock import patch

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from sovyx.engine.config import ObservabilityTuningConfig
from sovyx.observability.anomaly import (
    AnomalyDetector,
    StreamingPercentile,
)

# ── Strategies ──────────────────────────────────────────────────────────

# Finite floats for percentile sampling. We exclude NaN/Inf because the
# detector would happily store them (deque doesn't validate) but the
# percentile semantics aren't defined; the production callers (latency,
# RSS) only ever feed finite non-negative numbers.
_finite_floats = st.floats(
    min_value=-1e9, max_value=1e9, allow_nan=False, allow_infinity=False
)
_non_negative_floats = st.floats(
    min_value=0.0, max_value=1e9, allow_nan=False, allow_infinity=False
)


def _tuning(**overrides: Any) -> ObservabilityTuningConfig:
    """Build a tuning config with sane test defaults."""
    base: dict[str, Any] = {
        "anomaly_window_size": 100,
        "anomaly_min_samples": 10,
        "anomaly_latency_factor": 2.0,
        "anomaly_error_rate_window_s": 60,
        "anomaly_error_rate_factor": 3.0,
        "anomaly_memory_growth_window_s": 60,
        "anomaly_memory_growth_pct": 10.0,
        "anomaly_cooldown_s": 60,
    }
    base.update(overrides)
    return ObservabilityTuningConfig(**base)


# ── StreamingPercentile invariants ─────────────────────────────────────


class TestStreamingPercentileBasics:
    """Empty-state, single-sample, and bounds semantics."""

    def test_empty_window_returns_none(self) -> None:
        sp = StreamingPercentile(maxlen=10)
        assert sp.percentile(0.5) is None
        assert sp.percentile(0.0) is None
        assert sp.percentile(1.0) is None
        assert sp.count() == 0

    def test_single_sample_returns_that_sample_for_every_p(self) -> None:
        sp = StreamingPercentile(maxlen=10)
        sp.observe(42.0)
        # Linear interpolation against a 1-sample window collapses to the
        # sample for every p ∈ [0, 1] — guards against off-by-one in
        # the index math.
        assert sp.percentile(0.0) == 42.0
        assert sp.percentile(0.5) == 42.0
        assert sp.percentile(0.99) == 42.0
        assert sp.percentile(1.0) == 42.0
        assert sp.count() == 1

    @settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(samples=st.lists(_non_negative_floats, min_size=2, max_size=200))
    def test_p50_le_p95_le_p99(self, samples: list[float]) -> None:
        sp = StreamingPercentile(maxlen=len(samples))
        for s in samples:
            sp.observe(s)
        p50 = sp.percentile(0.50)
        p95 = sp.percentile(0.95)
        p99 = sp.percentile(0.99)
        assert p50 is not None and p95 is not None and p99 is not None
        # Monotonic ordering — anomaly thresholds depend on this. Use
        # math.isclose-style relative tolerance because at high
        # magnitudes (1e8+ ms is unrealistic but legal) float
        # interpolation rounding can flip neighbouring percentiles by
        # 1 ULP.
        assert p50 <= p95 or math.isclose(p50, p95, rel_tol=1e-12, abs_tol=1e-9)
        assert p95 <= p99 or math.isclose(p95, p99, rel_tol=1e-12, abs_tol=1e-9)

    @settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(samples=st.lists(_finite_floats, min_size=1, max_size=200))
    def test_p0_is_min_and_p1_is_max(self, samples: list[float]) -> None:
        sp = StreamingPercentile(maxlen=len(samples))
        for s in samples:
            sp.observe(s)
        assert sp.percentile(0.0) == min(samples)
        assert sp.percentile(1.0) == max(samples)

    @settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(
        maxlen=st.integers(min_value=1, max_value=50),
        samples=st.lists(_finite_floats, min_size=1, max_size=200),
    )
    def test_count_never_exceeds_maxlen(
        self, maxlen: int, samples: list[float]
    ) -> None:
        sp = StreamingPercentile(maxlen=maxlen)
        for s in samples:
            sp.observe(s)
        # Deque maxlen guarantees count ≤ maxlen; this is a structural
        # invariant we want pinned because the percentile cost is O(N log N).
        assert sp.count() <= maxlen
        assert sp.count() == min(maxlen, len(samples))

    @settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(value=_finite_floats)
    def test_observe_never_raises_on_finite_floats(self, value: float) -> None:
        sp = StreamingPercentile(maxlen=10)
        sp.observe(value)
        assert sp.count() == 1

    def test_p_is_clamped_outside_unit_interval(self) -> None:
        sp = StreamingPercentile(maxlen=5)
        for v in (1.0, 2.0, 3.0, 4.0, 5.0):
            sp.observe(v)
        # Clamping behaviour is part of the contract — callers pass
        # 0.99 always, but a stray > 1.0 should not index out of bounds.
        assert sp.percentile(-1.0) == 1.0
        assert sp.percentile(2.0) == 5.0


# ── AnomalyDetector: never-raises invariant ────────────────────────────


class TestDetectorNeverRaises:
    """Whatever we feed the detector, the structlog pipeline must not crash."""

    @settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(
        event=st.one_of(
            st.none(),
            st.text(min_size=0, max_size=40),
            st.integers(),
            st.floats(allow_nan=True, allow_infinity=True),
        ),
        level=st.one_of(
            st.none(), st.sampled_from(["INFO", "ERROR", "WARNING", "garbage", ""])
        ),
        latency=st.one_of(
            st.none(),
            st.floats(allow_nan=True, allow_infinity=True),
            st.integers(),
        ),
    )
    def test_call_never_raises_for_arbitrary_input(
        self, event: object, level: object, latency: object
    ) -> None:
        detector = AnomalyDetector(_tuning())
        record: dict[str, Any] = {"event": event}
        if level is not None:
            record["level"] = level
        if latency is not None:
            record["llm.latency_ms"] = latency
        out = detector(None, "info", record)
        # Identity preservation — the detector must never copy or
        # replace the dict; structlog hands the same MutableMapping
        # down the chain.
        assert out is record

    @settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(rss=st.one_of(st.none(), st.integers(), st.text(max_size=10)))
    def test_call_tolerates_arbitrary_rss_field(self, rss: object) -> None:
        detector = AnomalyDetector(_tuning())
        record: dict[str, Any] = {"event": "rss_snapshot.tick"}
        if rss is not None:
            record["system.rss_bytes"] = rss
        # The non-int / non-positive paths must early-return cleanly.
        detector(None, "info", record)


# ── AnomalyDetector: self-recursion guard ──────────────────────────────


class TestDetectorSelfRecursionGuard:
    """The detector skips its own emitted events to avoid feedback."""

    @pytest.mark.parametrize(
        "self_event",
        [
            "anomaly.first_occurrence",
            "anomaly.latency_spike",
            "anomaly.error_rate_spike",
            "anomaly.memory_growth",
        ],
    )
    def test_detector_skips_its_own_event_names(self, self_event: str) -> None:
        detector = AnomalyDetector(_tuning())
        record: dict[str, Any] = {"event": self_event}
        detector(None, "info", record)
        # `_seen_events` should NOT have grown — self-events bypass the
        # whole pipeline before mark_seen is reached.
        assert self_event not in detector._seen_events  # noqa: SLF001


# ── AnomalyDetector: first-occurrence semantics ────────────────────────


class TestDetectorFirstOccurrence:
    """First-occurrence fires exactly once per event name."""

    def test_first_occurrence_fires_once_then_silences(self) -> None:
        detector = AnomalyDetector(_tuning())
        with patch("sovyx.observability.anomaly.logger") as mock_logger:
            for _ in range(5):
                detector(None, "info", {"event": "test.brand_new"})
        # Five calls, one emission — and that emission is the first-
        # occurrence event with the right payload.
        info_calls = mock_logger.info.call_args_list
        first_occ = [c for c in info_calls if c.args == ("anomaly.first_occurrence",)]
        assert len(first_occ) == 1
        assert first_occ[0].kwargs == {"anomaly.event": "test.brand_new"}

    def test_distinct_events_each_get_a_first_occurrence(self) -> None:
        detector = AnomalyDetector(_tuning())
        with patch("sovyx.observability.anomaly.logger") as mock_logger:
            detector(None, "info", {"event": "a.first"})
            detector(None, "info", {"event": "b.first"})
            detector(None, "info", {"event": "c.first"})
        info_calls = [
            c for c in mock_logger.info.call_args_list
            if c.args == ("anomaly.first_occurrence",)
        ]
        # Three distinct events ⇒ three first-occurrence emissions —
        # the dedup key is event_name, not the anomaly type.
        emitted_events = sorted(c.kwargs["anomaly.event"] for c in info_calls)
        assert emitted_events == ["a.first", "b.first", "c.first"]

    def test_non_string_event_does_not_register_or_emit(self) -> None:
        detector = AnomalyDetector(_tuning())
        with patch("sovyx.observability.anomaly.logger") as mock_logger:
            detector(None, "info", {"event": 123})
            detector(None, "info", {"event": None})
            detector(None, "info", {})
        # No emissions — the event-name guard rejects non-strings before
        # anything observable happens. Keeps the seen-set bounded.
        assert mock_logger.info.call_count == 0
        assert mock_logger.warning.call_count == 0


# ── AnomalyDetector: latency-spike threshold semantics ─────────────────


class TestDetectorLatencyThreshold:
    """Latency spikes need ≥ min_samples and ≥ factor× baseline P99."""

    def test_no_spike_below_min_samples(self) -> None:
        # ObservabilityTuningConfig pins anomaly_min_samples ≥ 10. With
        # 9 warmup samples below the floor, the spike check short-
        # circuits even on a clearly-anomalous 1000ms outlier.
        detector = AnomalyDetector(_tuning(anomaly_min_samples=10))
        with patch("sovyx.observability.anomaly.logger") as mock_logger:
            for _ in range(9):
                detector(None, "info", {"event": "svc.call", "llm.latency_ms": 10.0})
            detector(None, "info", {"event": "svc.call", "llm.latency_ms": 1000.0})
        spikes = [
            c for c in mock_logger.warning.call_args_list
            if c.args == ("anomaly.latency_spike",)
        ]
        assert spikes == []

    def test_spike_fires_once_after_baseline_warmup(self) -> None:
        # Warmup must be ≥100× the spike count so the percentile
        # interpolation index is integer-aligned and P99 stays at the
        # baseline value when the spike is appended (otherwise the new
        # sample contaminates its own threshold — see _observe_latency
        # comment "new sample influences the next tick's baseline").
        detector = AnomalyDetector(
            _tuning(anomaly_min_samples=10, anomaly_latency_factor=2.0)
        )
        with patch("sovyx.observability.anomaly.logger") as mock_logger:
            for _ in range(100):
                detector(None, "info", {"event": "svc.call", "llm.latency_ms": 10.0})
            # 1000ms vs P99 baseline 10ms = 100× → far above factor=2.
            detector(None, "info", {"event": "svc.call", "llm.latency_ms": 1000.0})
        spikes = [
            c for c in mock_logger.warning.call_args_list
            if c.args == ("anomaly.latency_spike",)
        ]
        assert len(spikes) == 1
        payload = spikes[0].kwargs
        assert payload["anomaly.event"] == "svc.call"
        assert payload["anomaly.latency_ms"] == 1000
        assert payload["anomaly.factor"] >= 2.0


# ── AnomalyDetector: cooldown suppresses duplicate emissions ───────────


class TestDetectorCooldown:
    """Within `cooldown_s`, the same anomaly key cannot fire twice."""

    def test_cooldown_suppresses_back_to_back_first_occurrences_per_key(self) -> None:
        # First-occurrence fires once-per-event by design (seen-set), so
        # the cooldown guard is exercised most clearly via latency
        # spikes against the same event. We need enough warmup that
        # each spike CAN fire on its own merits — so the only thing
        # suppressing the second is the cooldown.
        detector = AnomalyDetector(
            _tuning(
                anomaly_window_size=200,
                anomaly_min_samples=10,
                anomaly_latency_factor=2.0,
                anomaly_cooldown_s=3600,
            )
        )
        with patch("sovyx.observability.anomaly.logger") as mock_logger:
            for _ in range(200):
                detector(None, "info", {"event": "svc.call", "llm.latency_ms": 10.0})
            # Two consecutive spikes 100× over baseline P99 — without
            # cooldown both would trip; cooldown collapses to one.
            detector(None, "info", {"event": "svc.call", "llm.latency_ms": 1000.0})
            detector(None, "info", {"event": "svc.call", "llm.latency_ms": 1000.0})
        spikes = [
            c for c in mock_logger.warning.call_args_list
            if c.args == ("anomaly.latency_spike",)
        ]
        # Cooldown=3600s collapses both spikes into one emission.
        assert len(spikes) == 1


# ── _extract_latency_ms field discovery ─────────────────────────────────


class TestLatencyFieldDiscovery:
    """The detector finds any ``*.latency_ms`` field across the dict."""

    @pytest.mark.parametrize(
        "field",
        ["llm.latency_ms", "brain.latency_ms", "net.latency_ms", "voice.latency_ms"],
    )
    def test_picks_up_canonical_latency_fields(self, field: str) -> None:
        detector = AnomalyDetector(
            _tuning(anomaly_min_samples=10, anomaly_latency_factor=2.0)
        )
        with patch("sovyx.observability.anomaly.logger") as mock_logger:
            for _ in range(100):
                detector(None, "info", {"event": "svc.x", field: 5.0})
            detector(None, "info", {"event": "svc.x", field: 500.0})
        spikes = [
            c for c in mock_logger.warning.call_args_list
            if c.args == ("anomaly.latency_spike",)
        ]
        assert len(spikes) == 1

    def test_ignores_bool_in_latency_field(self) -> None:
        # bool subclasses int — without an explicit guard, True would
        # become 1.0 and pollute the percentile window.
        detector = AnomalyDetector(_tuning())
        for _ in range(10):
            detector(None, "info", {"event": "svc.x", "llm.latency_ms": True})
        # The tracker either has no samples (bool rejected) — verify the
        # dict for that event was never created.
        assert "svc.x" not in detector._latency_per_event  # noqa: SLF001

    def test_handles_nan_latency_without_raising(self) -> None:
        # NaN passes the isinstance(int|float) guard but fails the
        # ``latency >= 0`` check (NaN comparisons always return False).
        # That early-return is exactly what protects the percentile
        # sort from a poison sample, so we pin the contract: NaN never
        # registers a tracker, and the detector survives the call.
        detector = AnomalyDetector(_tuning())
        for _ in range(15):
            detector(
                None, "info", {"event": "svc.nan", "llm.latency_ms": float("nan")}
            )
        assert "svc.nan" not in detector._latency_per_event  # noqa: SLF001
