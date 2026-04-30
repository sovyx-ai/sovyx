"""Tests for the AEC + WASAPI-exclusive bypass detector [Phase 4 T4.6].

Coverage:

* :data:`METRIC_AEC_BYPASS_COMBO` name pin.
* :func:`record_aec_bypass_combo` no-op safety + state propagation.
* :func:`_evaluate_aec_bypass_combo` classifier matrix:
  - exclusive=False, AEC=False → ``"safe_shared"``, no force.
  - exclusive=True,  AEC=True  → ``"safe_engaged"``, no force.
  - exclusive=False, AEC=True  → ``"safe_belt_and_suspenders"``, no
    force.
  - exclusive=True,  AEC=False, auto_engage=False → ``"dangerous"``,
    no force.
  - exclusive=True,  AEC=False, auto_engage=True  →
    ``"auto_engaged"``, force=True.
* :func:`_build_aec_wiring` integration:
  - dangerous combo emits WARN + counter, returns ``(None, None)``
    (operator config respected).
  - auto-engaged combo force-engages AEC even when
    ``voice_aec_enabled=False``, promoting ``engine="off"`` to
    ``"speex"``.
  - safe combos do NOT WARN (only INFO/DEBUG paths).
* Foundation default: ``voice_aec_auto_engage_on_exclusive=False``.
"""

from __future__ import annotations

from collections.abc import Generator
from typing import Any

import pytest  # noqa: TC002 — pytest types resolved at runtime via fixtures
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from sovyx.engine.config import VoiceTuningConfig
from sovyx.observability.metrics import (
    MetricsRegistry,
    setup_metrics,
    teardown_metrics,
)
from sovyx.voice._aec import SpeexAecProcessor
from sovyx.voice._render_pcm_buffer import RenderPcmBuffer
from sovyx.voice.factory import _build_aec_wiring, _evaluate_aec_bypass_combo
from sovyx.voice.health._metrics import (
    METRIC_AEC_BYPASS_COMBO,
    record_aec_bypass_combo,
)

# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture()
def reader() -> InMemoryMetricReader:
    return InMemoryMetricReader()


@pytest.fixture(autouse=True)
def _reset_otel() -> Generator[None, None, None]:
    from opentelemetry.metrics import _internal as otel_internal

    yield
    otel_internal._METER_PROVIDER_SET_ONCE._done = False
    otel_internal._METER_PROVIDER = None


@pytest.fixture()
def registry(reader: InMemoryMetricReader) -> Generator[MetricsRegistry, None, None]:
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


# ── Stable name + foundation default contracts ──────────────────────────


class TestStableNameContract:
    def test_aec_bypass_combo_name(self) -> None:
        assert METRIC_AEC_BYPASS_COMBO == "sovyx.voice.aec.bypass_combo"

    def test_auto_engage_default_false(self) -> None:
        # feedback_staged_adoption: new override flags ship disabled.
        tuning = VoiceTuningConfig()
        assert tuning.voice_aec_auto_engage_on_exclusive is False


# ── record_aec_bypass_combo ─────────────────────────────────────────────


class TestRecordAecBypassCombo:
    def test_no_op_without_registry(self) -> None:
        record_aec_bypass_combo(state="dangerous")  # must not raise

    def test_state_label_propagates(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        record_aec_bypass_combo(state="safe_shared")
        record_aec_bypass_combo(state="dangerous")
        metric = _find(_collect(reader), METRIC_AEC_BYPASS_COMBO)
        assert metric is not None
        states = sorted(dp["attributes"]["state"] for dp in metric["data_points"])
        assert states == ["dangerous", "safe_shared"]


# ── Classifier matrix ───────────────────────────────────────────────────


class TestEvaluateAecBypassCombo:
    def test_safe_shared_default(self) -> None:
        tuning = VoiceTuningConfig()
        # Defaults: exclusive=False, AEC=False, auto_engage=False.
        state, force = _evaluate_aec_bypass_combo(tuning)
        assert state == "safe_shared"
        assert force is False

    def test_safe_engaged_exclusive_with_aec(self) -> None:
        tuning = VoiceTuningConfig(
            capture_wasapi_exclusive=True,
            voice_aec_enabled=True,
        )
        state, force = _evaluate_aec_bypass_combo(tuning)
        assert state == "safe_engaged"
        assert force is False

    def test_safe_belt_and_suspenders_shared_with_aec(self) -> None:
        tuning = VoiceTuningConfig(
            capture_wasapi_exclusive=False,
            voice_aec_enabled=True,
        )
        state, force = _evaluate_aec_bypass_combo(tuning)
        assert state == "safe_belt_and_suspenders"
        assert force is False

    def test_dangerous_exclusive_without_aec(self) -> None:
        tuning = VoiceTuningConfig(
            capture_wasapi_exclusive=True,
            voice_aec_enabled=False,
            voice_aec_auto_engage_on_exclusive=False,
        )
        state, force = _evaluate_aec_bypass_combo(tuning)
        assert state == "dangerous"
        assert force is False

    def test_auto_engaged_with_override(self) -> None:
        tuning = VoiceTuningConfig(
            capture_wasapi_exclusive=True,
            voice_aec_enabled=False,
            voice_aec_auto_engage_on_exclusive=True,
        )
        state, force = _evaluate_aec_bypass_combo(tuning)
        assert state == "auto_engaged"
        assert force is True

    def test_auto_engage_flag_ignored_when_aec_already_on(self) -> None:
        # The override only applies to the dangerous combo; if AEC is
        # already on, the flag is moot.
        tuning = VoiceTuningConfig(
            capture_wasapi_exclusive=True,
            voice_aec_enabled=True,
            voice_aec_auto_engage_on_exclusive=True,
        )
        state, force = _evaluate_aec_bypass_combo(tuning)
        assert state == "safe_engaged"
        assert force is False

    def test_auto_engage_flag_ignored_when_not_exclusive(self) -> None:
        # No exclusive → no OS-AEC bypass → no override needed.
        tuning = VoiceTuningConfig(
            capture_wasapi_exclusive=False,
            voice_aec_enabled=False,
            voice_aec_auto_engage_on_exclusive=True,
        )
        state, force = _evaluate_aec_bypass_combo(tuning)
        assert state == "safe_shared"
        assert force is False


# ── _build_aec_wiring integration ────────────────────────────────────────


class TestBuildAecWiringBypassIntegration:
    def test_safe_shared_emits_metric_no_warn(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging

        tuning = VoiceTuningConfig()
        with caplog.at_level(logging.WARNING, logger="sovyx.voice.factory"):
            buffer, processor = _build_aec_wiring(tuning)
        assert buffer is None
        assert processor is None

        metric = _find(_collect(reader), METRIC_AEC_BYPASS_COMBO)
        assert metric is not None
        states = [dp["attributes"]["state"] for dp in metric["data_points"]]
        assert states == ["safe_shared"]
        # No WARN for the safe path.
        warn_records = [r for r in caplog.records if "bypass_combo" in r.getMessage()]
        assert warn_records == []

    def test_dangerous_combo_warns_and_respects_operator(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging

        tuning = VoiceTuningConfig(
            capture_wasapi_exclusive=True,
            voice_aec_enabled=False,
            voice_aec_auto_engage_on_exclusive=False,
        )
        with caplog.at_level(logging.WARNING, logger="sovyx.voice.factory"):
            buffer, processor = _build_aec_wiring(tuning)
        # Operator's voice_aec_enabled=False is respected even though
        # the combo is dangerous — the contract is OBSERVABILITY +
        # opt-in override, not silent mutation.
        assert buffer is None
        assert processor is None

        metric = _find(_collect(reader), METRIC_AEC_BYPASS_COMBO)
        assert metric is not None
        states = [dp["attributes"]["state"] for dp in metric["data_points"]]
        assert states == ["dangerous"]
        warn_records = [
            r
            for r in caplog.records
            if "bypass_combo_dangerous" in r.getMessage() and r.levelno == logging.WARNING
        ]
        assert len(warn_records) == 1

    def test_auto_engaged_combo_force_engages_aec(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        # voice_aec_enabled=False but auto_engage flips it ON.
        tuning = VoiceTuningConfig(
            capture_wasapi_exclusive=True,
            voice_aec_enabled=False,
            voice_aec_auto_engage_on_exclusive=True,
        )
        buffer, processor = _build_aec_wiring(tuning)
        assert isinstance(buffer, RenderPcmBuffer)
        assert isinstance(processor, SpeexAecProcessor)

        metric = _find(_collect(reader), METRIC_AEC_BYPASS_COMBO)
        assert metric is not None
        states = [dp["attributes"]["state"] for dp in metric["data_points"]]
        assert states == ["auto_engaged"]

    def test_auto_engaged_promotes_engine_off_to_speex(self) -> None:
        # Operator pinned engine="off" but opted into auto-engage —
        # the override promotes engine to "speex" so AEC actually
        # runs.
        tuning = VoiceTuningConfig(
            capture_wasapi_exclusive=True,
            voice_aec_enabled=False,
            voice_aec_engine="off",
            voice_aec_auto_engage_on_exclusive=True,
        )
        buffer, processor = _build_aec_wiring(tuning)
        assert isinstance(buffer, RenderPcmBuffer)
        assert isinstance(processor, SpeexAecProcessor)

    def test_safe_engaged_emits_no_warn(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging

        tuning = VoiceTuningConfig(
            capture_wasapi_exclusive=True,
            voice_aec_enabled=True,
        )
        with caplog.at_level(logging.WARNING, logger="sovyx.voice.factory"):
            buffer, processor = _build_aec_wiring(tuning)
        assert isinstance(buffer, RenderPcmBuffer)
        assert isinstance(processor, SpeexAecProcessor)

        metric = _find(_collect(reader), METRIC_AEC_BYPASS_COMBO)
        assert metric is not None
        states = [dp["attributes"]["state"] for dp in metric["data_points"]]
        assert states == ["safe_engaged"]
        warn_records = [r for r in caplog.records if "bypass_combo" in r.getMessage()]
        assert warn_records == []

    def test_combo_metric_emits_once_per_call(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        # Cardinality contract: one event per voice pipeline
        # construction. Calling the helper N times → counter
        # increments by N (not N² or 0).
        tuning = VoiceTuningConfig()
        for _ in range(5):
            _build_aec_wiring(tuning)
        metric = _find(_collect(reader), METRIC_AEC_BYPASS_COMBO)
        assert metric is not None
        # All 5 carry state="safe_shared" — single data point with
        # cumulative value 5.
        dps = [dp for dp in metric["data_points"] if dp["attributes"]["state"] == "safe_shared"]
        assert len(dps) == 1
        assert dps[0]["value"] == 5
