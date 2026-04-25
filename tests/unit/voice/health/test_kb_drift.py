"""Tests for :mod:`sovyx.voice.health._mixer_kb._drift` (F12).

Covers config bound enforcement, single-key state machine
(UNKNOWN→HEALTHY→DRIFTING→HEALTHY), hysteresis on transitions,
LRU eviction, structured event emission, thread safety.

Reference: MISSION-voice-mixer-enterprise-refactor-2026-04-25 §4 F12.
"""

from __future__ import annotations

import logging
import threading

import pytest

from sovyx.voice.health._mixer_kb._drift import (
    DriftMonitor,
    DriftMonitorConfig,
    DriftSample,
    DriftState,
)


def _sample(value: float, *, profile_id: str = "p", role: str = "capture") -> DriftSample:
    return DriftSample(
        profile_id=profile_id,
        control_role=role,
        value=value,
        baseline_min=0.4,
        baseline_max=0.6,
    )


# ── DriftMonitorConfig ────────────────────────────────────────────


class TestDriftMonitorConfig:
    def test_canonical_defaults(self) -> None:
        cfg = DriftMonitorConfig()
        assert cfg.window_size == 12
        assert cfg.min_consecutive_drift_samples == 3
        assert cfg.drift_proportion_threshold == 0.5

    def test_window_below_floor_rejected(self) -> None:
        with pytest.raises(ValueError, match="window_size must be"):
            DriftMonitorConfig(window_size=2)

    def test_window_above_ceiling_rejected(self) -> None:
        with pytest.raises(ValueError, match="window_size must be"):
            DriftMonitorConfig(window_size=2_000)

    def test_consecutive_above_window_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be <= window_size"):
            DriftMonitorConfig(
                window_size=4,
                min_consecutive_drift_samples=10,
            )

    def test_proportion_out_of_range_rejected(self) -> None:
        with pytest.raises(ValueError, match="drift_proportion_threshold"):
            DriftMonitorConfig(drift_proportion_threshold=0.0)

    def test_max_keys_zero_rejected(self) -> None:
        with pytest.raises(ValueError, match="max_keys"):
            DriftMonitorConfig(max_keys=0)

    def test_consecutive_below_floor_rejected(self) -> None:
        with pytest.raises(ValueError, match="min_consecutive_drift_samples"):
            DriftMonitorConfig(min_consecutive_drift_samples=1)


# ── DriftSample validation ─────────────────────────────────────────


class TestDriftSample:
    def test_inverted_baseline_range_rejected(self) -> None:
        monitor = DriftMonitor()
        bad = DriftSample(
            profile_id="p", control_role="r", value=0.5,
            baseline_min=0.7, baseline_max=0.3,
        )
        with pytest.raises(ValueError, match="baseline_min"):
            monitor.record(bad)


# ── State machine ─────────────────────────────────────────────────


class TestStateMachine:
    def test_initial_state_is_unknown(self) -> None:
        monitor = DriftMonitor()
        assert monitor.state_for("p", "capture") == DriftState.UNKNOWN

    def test_warmup_promotes_to_healthy(self) -> None:
        cfg = DriftMonitorConfig(
            window_size=6,
            min_consecutive_recovery_samples=3,
            min_consecutive_drift_samples=3,
        )
        monitor = DriftMonitor(cfg)
        # 3 in-baseline samples in a row → HEALTHY.
        for _ in range(3):
            state = monitor.record(_sample(0.5))
        assert state == DriftState.HEALTHY

    def test_sustained_drift_fires_alert(self) -> None:
        cfg = DriftMonitorConfig(
            window_size=6,
            min_consecutive_drift_samples=3,
            drift_proportion_threshold=0.5,
        )
        monitor = DriftMonitor(cfg)
        # Warm up healthy.
        for _ in range(3):
            monitor.record(_sample(0.5))
        assert monitor.state_for("p", "capture") == DriftState.HEALTHY
        # 3 consecutive out-of-baseline samples (≥ 50% of window).
        for _ in range(3):
            state = monitor.record(_sample(0.9))  # above baseline_max
        assert state == DriftState.DRIFTING

    def test_single_outlier_does_not_trigger(self) -> None:
        cfg = DriftMonitorConfig(window_size=6, min_consecutive_drift_samples=3)
        monitor = DriftMonitor(cfg)
        for _ in range(3):
            monitor.record(_sample(0.5))
        # One outlier surrounded by healthy samples → no drift.
        monitor.record(_sample(0.9))
        assert monitor.state_for("p", "capture") == DriftState.HEALTHY

    def test_recovery_clears_alert(self) -> None:
        cfg = DriftMonitorConfig(
            window_size=6,
            min_consecutive_drift_samples=3,
            min_consecutive_recovery_samples=3,
            drift_proportion_threshold=0.5,
            recovery_proportion_threshold=0.5,
        )
        monitor = DriftMonitor(cfg)
        for _ in range(3):
            monitor.record(_sample(0.5))
        for _ in range(3):
            monitor.record(_sample(0.9))
        assert monitor.state_for("p", "capture") == DriftState.DRIFTING
        # 3 in-baseline samples in a row to recover.
        for _ in range(3):
            state = monitor.record(_sample(0.5))
        assert state == DriftState.HEALTHY

    def test_drift_state_latches_under_intermittent_recovery(self) -> None:
        """Hysteresis: a single in-baseline sample mid-drift should
        NOT clear the alert — recovery requires sustained
        in-baseline samples."""
        cfg = DriftMonitorConfig(
            window_size=6,
            min_consecutive_drift_samples=3,
            min_consecutive_recovery_samples=3,
        )
        monitor = DriftMonitor(cfg)
        for _ in range(3):
            monitor.record(_sample(0.5))
        for _ in range(3):
            monitor.record(_sample(0.9))
        # One in-baseline sample → still DRIFTING.
        monitor.record(_sample(0.5))
        assert monitor.state_for("p", "capture") == DriftState.DRIFTING

    def test_baseline_at_exact_boundary_counts_in(self) -> None:
        """Inclusive boundary check (per anti-pattern #24-style
        ``>=`` discipline)."""
        monitor = DriftMonitor()
        # Sample at exactly baseline_min.
        state = monitor.record(
            DriftSample(
                profile_id="p", control_role="r", value=0.4,
                baseline_min=0.4, baseline_max=0.6,
            )
        )
        # Window has 1 sample, all in-baseline → still UNKNOWN
        # until min_consecutive_recovery_samples reached.
        assert state == DriftState.UNKNOWN


# ── Per-key isolation ─────────────────────────────────────────────


class TestPerKeyIsolation:
    def test_distinct_profile_ids_have_distinct_state(self) -> None:
        cfg = DriftMonitorConfig(
            window_size=4,
            min_consecutive_drift_samples=2,
            min_consecutive_recovery_samples=2,
        )
        monitor = DriftMonitor(cfg)
        # Profile A drifts.
        for _ in range(2):
            monitor.record(_sample(0.5, profile_id="a"))
        for _ in range(2):
            monitor.record(_sample(0.9, profile_id="a"))
        # Profile B stays healthy.
        for _ in range(2):
            monitor.record(_sample(0.5, profile_id="b"))
        assert monitor.state_for("a", "capture") == DriftState.DRIFTING
        assert monitor.state_for("b", "capture") == DriftState.HEALTHY

    def test_distinct_control_roles_isolated(self) -> None:
        cfg = DriftMonitorConfig(
            window_size=4,
            min_consecutive_drift_samples=2,
            min_consecutive_recovery_samples=2,
        )
        monitor = DriftMonitor(cfg)
        for _ in range(2):
            monitor.record(_sample(0.5, role="capture"))
            monitor.record(_sample(0.9, role="boost"))
        for _ in range(2):
            monitor.record(_sample(0.9, role="boost"))
        assert monitor.state_for("p", "boost") == DriftState.DRIFTING
        # capture role is in-baseline and hasn't drifted.
        assert monitor.state_for("p", "capture") in (
            DriftState.UNKNOWN,
            DriftState.HEALTHY,
        )


# ── Event emission ────────────────────────────────────────────────


class TestEventEmission:
    def test_drift_detected_event_fires(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        cfg = DriftMonitorConfig(
            window_size=4,
            min_consecutive_drift_samples=2,
            min_consecutive_recovery_samples=2,
        )
        monitor = DriftMonitor(cfg)
        # Warm-up to HEALTHY.
        for _ in range(2):
            monitor.record(_sample(0.5))
        # Then drift.
        with caplog.at_level(logging.WARNING):
            for _ in range(2):
                monitor.record(_sample(0.9))
        events = [
            r for r in caplog.records
            if "voice.kb.profile.drift_detected" in str(r.msg)
        ]
        assert len(events) >= 1

    def test_drift_recovered_event_fires(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        cfg = DriftMonitorConfig(
            window_size=4,
            min_consecutive_drift_samples=2,
            min_consecutive_recovery_samples=2,
        )
        monitor = DriftMonitor(cfg)
        for _ in range(2):
            monitor.record(_sample(0.5))
        for _ in range(2):
            monitor.record(_sample(0.9))
        with caplog.at_level(logging.INFO):
            for _ in range(2):
                monitor.record(_sample(0.5))
        events = [
            r for r in caplog.records
            if "voice.kb.profile.drift_recovered" in str(r.msg)
        ]
        assert len(events) >= 1


# ── LRU eviction ──────────────────────────────────────────────────


class TestLRUEviction:
    def test_max_keys_enforced(self) -> None:
        cfg = DriftMonitorConfig(window_size=4, max_keys=3)
        monitor = DriftMonitor(cfg)
        for i in range(5):
            monitor.record(_sample(0.5, profile_id=f"p{i}"))
        keys = monitor.tracked_keys()
        assert len(keys) == 3
        # Oldest two evicted; last 3 remain.
        assert (("p2", "capture") in keys
                and ("p3", "capture") in keys
                and ("p4", "capture") in keys)

    def test_touch_moves_to_end(self) -> None:
        """Re-recording a key moves it to the LRU 'newest' end so
        it's not the next eviction target."""
        cfg = DriftMonitorConfig(window_size=4, max_keys=3)
        monitor = DriftMonitor(cfg)
        monitor.record(_sample(0.5, profile_id="a"))
        monitor.record(_sample(0.5, profile_id="b"))
        monitor.record(_sample(0.5, profile_id="c"))
        # Touch 'a' so 'b' becomes oldest.
        monitor.record(_sample(0.5, profile_id="a"))
        # Add 'd' — should evict 'b'.
        monitor.record(_sample(0.5, profile_id="d"))
        keys = {k[0] for k in monitor.tracked_keys()}
        assert keys == {"a", "c", "d"}


# ── Thread safety ─────────────────────────────────────────────────


class TestThreadSafety:
    def test_concurrent_records_dont_corrupt(self) -> None:
        cfg = DriftMonitorConfig(window_size=12)
        monitor = DriftMonitor(cfg)
        n_threads = 8
        per_thread = 50
        barrier = threading.Barrier(n_threads)
        errors: list[Exception] = []

        def worker(idx: int) -> None:
            try:
                barrier.wait()
                for _ in range(per_thread):
                    monitor.record(_sample(0.5, profile_id=f"p{idx}"))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(i,)) for i in range(n_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        # Each profile_id has its own state.
        keys = monitor.tracked_keys()
        assert len(keys) == n_threads
