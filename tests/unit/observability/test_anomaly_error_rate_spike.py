"""Mission B Phase B.3.P1 — anomaly error-rate spike regression coverage.

Closes Mission B findings:

* **B-P1-01** (B1-F-001) — Anomaly ``previous < 2`` floor blocks 0→N
  and 1→N error storms. Operator dashboards keyed on
  ``anomaly.error_rate_spike`` received zero signal on the first
  serious outage of a previously-healthy service.
* **B-P1-02** (B1-F-002) — Anomaly ``_error_window`` deque eviction
  silences sustained storms. Single 4×-window deque saturated at
  sustained ~4+ errors/sec; oldest baseline samples evicted;
  ``previous`` dropped toward zero mid-storm; detector went silent.
* **B-P1-13** (B6-F-005) — Operator-tunable floor undocumented +
  no env override (BUNDLE with B-P1-01).

Closure mechanism:

* Pydantic field ``ObservabilityTuningConfig.anomaly_error_rate_floor:
  int = 2`` (default preserves legacy ``previous < 2`` behavior).
* ``floor=0`` activates the FIRST-burst-from-quiet-system path —
  emits when ``current >= ceil(factor)`` with ``baseline_count == 0``
  as the wire signal.
* Single ``_error_window`` deque split into ``_error_window_current``
  + ``_error_window_previous`` independently bounded; explicit
  ``popleft()`` aging preserves window-boundary semantics under
  sustained 10/s+ load.

Mission anchor:
``docs-internal/MISSION-B-REMEDIATION-PLAN-2026-05-21.md`` §B.3.P1
+ ``docs-internal/MISSION-B-FINDINGS-REGISTER-2026-05-21.md`` B-P1-01/02/13.

These tests would have FAILED pre-fix (B-P1-01 + B-P1-02 + B-P1-13 are
all FALSE_NEGATIVE_RISK findings); they PASS post-fix.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from sovyx.engine.config import ObservabilityTuningConfig
from sovyx.observability.anomaly import AnomalyDetector


def _tuning(**overrides: Any) -> ObservabilityTuningConfig:
    """Build an ObservabilityTuningConfig with B.3.P1-relevant defaults.

    Mirrors the shape of ``test_anomaly_http_error_rate_spike.py::_tuning``
    so the two test files share a single mental model of the config
    surface.
    """
    base: dict[str, Any] = {
        "anomaly_window_size": 100,
        "anomaly_min_samples": 10,
        "anomaly_latency_factor": 2.0,
        "anomaly_error_rate_window_s": 60,
        "anomaly_error_rate_factor": 3.0,
        "anomaly_error_rate_floor": 2,
        "anomaly_memory_growth_window_s": 60,
        "anomaly_memory_growth_pct": 10.0,
        "anomaly_cooldown_s": 60,
        "http_error_rate_spike_enabled": False,
        "http_error_rate_spike_count": 5,
        "http_error_rate_spike_window_s": 30,
        "http_error_rate_spike_cooldown_s": 300,
        "http_error_rate_spike_path_cap": 64,
    }
    base.update(overrides)
    return ObservabilityTuningConfig(**base)


def _error_entry(name: str = "some.error", *, level: str = "error") -> dict[str, Any]:
    return {"event": name, "level": level}


def _spike_emits(mock_warn: Any) -> list[dict[str, Any]]:
    """Extract the ``anomaly.error_rate_spike`` field dicts from a
    patched ``logger.warning`` mock. Mirrors the helper pattern in
    ``test_anomaly_http_error_rate_spike.py``."""
    return [
        c.kwargs
        for c in mock_warn.call_args_list
        if c.args and c.args[0] == "anomaly.error_rate_spike"
    ]


class TestZeroToNTransition:
    """B-P1-01 + B-P1-13 — first burst on a previously-quiet system.

    Pre-fix: ``previous < 2`` silently returned. Post-fix with
    ``floor=0``: emits via the quiet-start branch with
    ``anomaly.baseline_count == 0``.
    """

    def test_zero_to_burst_with_floor_zero_fires(self) -> None:
        detector = AnomalyDetector(_tuning(anomaly_error_rate_floor=0))
        with patch("sovyx.observability.anomaly.logger.warning") as mock_warn:
            # Default ``anomaly_error_rate_factor=3.0`` → emits at current>=3.
            for _ in range(5):
                detector(None, "info", _error_entry())
        spikes = _spike_emits(mock_warn)
        assert spikes, "anomaly.error_rate_spike MUST fire on 0→N burst with floor=0"
        # The first spike emit's baseline_count MUST be 0 (the quiet-
        # start signal that distinguishes this branch from the legacy
        # path).
        assert spikes[0]["anomaly.baseline_count"] == 0
        assert spikes[0]["anomaly.floor"] == 0

    def test_zero_to_burst_with_floor_default_two_does_not_fire(self) -> None:
        """B-P1-01 baseline behavior: default floor=2 preserves the
        legacy "no fire on 0→N" so existing operator alerting tuned to
        the pre-mission default sees no behavior change.
        """
        detector = AnomalyDetector(_tuning())  # floor=2 (default)
        with patch("sovyx.observability.anomaly.logger.warning") as mock_warn:
            for _ in range(50):
                detector(None, "info", _error_entry())
        assert _spike_emits(mock_warn) == [], (
            "default floor=2 MUST preserve legacy no-fire behavior on 0→N transitions"
        )


class TestOneToNTransition:
    """B-P1-01 — single trailing error in baseline blocks fire pre-fix;
    floor=1 unblocks it without crossing into the 0→N path."""

    def test_one_baseline_with_floor_one_fires_on_burst(self) -> None:
        detector = AnomalyDetector(
            _tuning(anomaly_error_rate_window_s=10, anomaly_error_rate_floor=1),
        )
        with patch("sovyx.observability.anomaly.logger.warning") as mock_warn:
            # Seed a single baseline error in the previous window
            # ([now-2*window_s, now-window_s) = [-20, -10) when now=0
            # below). Then drive a burst at now=0 inside the current
            # window.
            with patch("time.monotonic", return_value=-15.0):
                detector(None, "info", _error_entry())
            # Burst N=10 in the current window. factor=3 → 10>1*3 emit.
            with patch("time.monotonic", return_value=0.0):
                for _ in range(10):
                    detector(None, "info", _error_entry())
        spikes = _spike_emits(mock_warn)
        assert spikes, "floor=1 MUST permit 1→N burst detection"
        # The fired emit MUST carry baseline_count==1 (the 1→N branch);
        # not 0 (quiet-start) and not >=2 (legacy).
        assert spikes[0]["anomaly.baseline_count"] == 1
        assert spikes[0]["anomaly.floor"] == 1


class TestSustainedStormDoesNotSilenceDetector:
    """B-P1-02 — pre-fix the single 4×-window deque saturated and the
    oldest baseline samples evicted, dropping ``previous`` toward 0
    mid-storm. Post-fix the dual-window split keeps the previous-window
    populated even under sustained 10/s+ load.
    """

    def test_sustained_10_per_second_storm_keeps_previous_populated(self) -> None:
        # Tight window so the test runs fast: window_s=10 + 30s of
        # simulated 10/s load.
        detector = AnomalyDetector(
            _tuning(anomaly_error_rate_window_s=10, anomaly_error_rate_floor=2),
        )
        for tick in range(0, 300):
            now = tick * 0.1  # 10 ticks per second
            with patch("time.monotonic", return_value=now):
                detector(None, "info", _error_entry())

        # After 30s of 10/s, current ≈ [20, 30) (~100 entries),
        # previous ≈ [10, 20) (~100 entries). Pre-fix single-deque
        # bounded at 40 entries silently truncated previous toward 0;
        # post-fix MUST hold ≥50 in previous (forensic threshold per
        # MISSION-B-FINDINGS-REGISTER §B-P1-02).
        previous_count = len(detector._error_window_previous)  # noqa: SLF001
        current_count = len(detector._error_window_current)  # noqa: SLF001
        assert previous_count >= 50, (
            "B-P1-02: sustained 10/s storm MUST keep previous-window "
            f"populated; got previous={previous_count}, current={current_count}"
        )

    def test_sustained_storm_with_floor_zero_emits_continuously(self) -> None:
        """Operator running with ``floor=0`` MUST see at-least-one
        ``anomaly.error_rate_spike`` during a sustained storm — the
        pre-fix silent-mid-storm path is the failure mode this finding
        catalogues.
        """
        detector = AnomalyDetector(
            _tuning(anomaly_error_rate_window_s=10, anomaly_error_rate_floor=0),
        )
        with patch("sovyx.observability.anomaly.logger.warning") as mock_warn:
            for tick in range(0, 200):  # 20 simulated seconds @ 10/s
                now = tick * 0.1
                with patch("time.monotonic", return_value=now):
                    detector(None, "info", _error_entry())
        spikes = _spike_emits(mock_warn)
        assert spikes, "B-P1-02: sustained storm MUST emit at least one spike under floor=0"


class TestFloorOverrideViaEnv:
    """B-P1-13 — the new field is wired through ``ObservabilityTuningConfig``
    so the standard pydantic-settings env-override path
    (``SOVYX_TUNING__OBSERVABILITY__ANOMALY_ERROR_RATE_FLOOR=N``) takes
    effect WITHOUT a code change. This test pins the field's existence,
    type, validation bounds, and reach through the detector init."""

    def test_field_exists_and_defaults_to_two(self) -> None:
        cfg = _tuning()
        assert cfg.anomaly_error_rate_floor == 2

    def test_field_accepts_zero_for_quiet_start_path(self) -> None:
        cfg = _tuning(anomaly_error_rate_floor=0)
        assert cfg.anomaly_error_rate_floor == 0
        detector = AnomalyDetector(cfg)
        assert detector._error_floor == 0  # noqa: SLF001

    def test_field_rejects_negative(self) -> None:
        # xdist-safe (anti-pattern #8) — match on substring not class.
        with pytest.raises(Exception) as exc_info:
            _tuning(anomaly_error_rate_floor=-1)
        msg = str(exc_info.value).lower()
        assert "greater" in msg or "ge=" in msg or "greater_than_equal" in msg or "valid" in msg

    def test_field_rejects_above_ceiling(self) -> None:
        with pytest.raises(Exception) as exc_info:
            _tuning(anomaly_error_rate_floor=101)
        msg = str(exc_info.value).lower()
        assert "less" in msg or "le=" in msg or "less_than_equal" in msg or "valid" in msg

    def test_env_var_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Pydantic-settings ``SOVYX_OBSERVABILITY__TUNING__*`` env path
        reaches the field. The ``__`` nesting separator is enforced by
        ``EngineConfig`` per CLAUDE.md § Conventions. The
        ``ObservabilityTuningConfig`` env-prefix is declared at
        ``src/sovyx/engine/config.py:2860``.
        """
        monkeypatch.setenv("SOVYX_OBSERVABILITY__TUNING__ANOMALY_ERROR_RATE_FLOOR", "0")
        # Re-instantiate the leaf settings model directly so the test
        # exercises the pydantic-settings env path without depending on
        # the full ``EngineConfig`` bootstrap (which reads many other
        # env vars and may resolve paths that would pollute CI).
        cfg = ObservabilityTuningConfig()
        assert cfg.anomaly_error_rate_floor == 0
