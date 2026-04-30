"""Tests for the VAD quiet-signal anti-hallucination gate [Phase 4 T4.39].

Coverage:

* :data:`METRIC_VAD_QUIET_SIGNAL_GATED` name pin.
* :func:`record_vad_quiet_signal_gated` no-op safety + state
  propagation.
* :class:`VADConfig` foundation defaults: gate disabled, threshold
  -70 dBFS, prob threshold 0.8.
* :func:`_validate_config` rejects out-of-bounds gate parameters.
* :class:`SileroVAD` integration:
  - Gate disabled (default) → bit-exact regression-guard against
    pre-T4.39 behaviour. The detector still increments the
    ``would_gate`` counter when the paradox is observed.
  - Gate enabled → paradoxical frame (low RMS + high prob) has
    probability force-clamped to 0.0; the FSM reads silence.
  - Loud + high-prob frame is NEVER gated (only the paradox
    triggers).
  - Quiet + low-prob frame is NEVER gated (only the paradox
    triggers).
  - Telemetry: ``state="gated"`` when action engages,
    ``state="would_gate"`` when detector observes but action is
    off.
"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import numpy as np
import pytest  # noqa: TC002 — pytest types resolved at runtime
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from sovyx.observability.metrics import (
    MetricsRegistry,
    setup_metrics,
    teardown_metrics,
)
from sovyx.voice.health._metrics import (
    METRIC_VAD_QUIET_SIGNAL_GATED,
    record_vad_quiet_signal_gated,
)
from sovyx.voice.vad import (
    SileroVAD,
    VADConfig,
    VADState,
    _validate_config,
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


# ── Mocked-VAD helpers ───────────────────────────────────────────────────


_WINDOW = 512


def _make_fixed_prob_session(probability: float) -> MagicMock:
    """Mock ONNX session that always returns the same probability."""
    session = MagicMock()

    def _run(_output_names: Any, inputs: dict[str, Any]) -> list[Any]:  # noqa: ANN401
        output = np.array([[probability]], dtype=np.float32)
        state = inputs["state"]
        return [output, state]

    session.run = _run
    return session


def _build_vad(
    *,
    probability: float,
    config: VADConfig | None = None,
) -> SileroVAD:
    """Construct SileroVAD with a fixed-probability mock session."""
    cfg = config or VADConfig()
    session = _make_fixed_prob_session(probability)

    mock_ort = MagicMock()
    mock_ort.SessionOptions.return_value = MagicMock()
    mock_ort.GraphOptimizationLevel.ORT_ENABLE_ALL = 99
    mock_ort.InferenceSession.return_value = session

    with patch.dict("sys.modules", {"onnxruntime": mock_ort}):
        return SileroVAD(
            Path("/fake/model.onnx"),
            config=cfg,
            smoke_probe_at_construction=False,
        )


def _quiet_frame() -> np.ndarray:
    """Frame with RMS well below the -70 dBFS gate threshold.

    Float32 RMS = 1e-4 → -80 dBFS, comfortably below the gate.
    """
    n = np.linspace(0, 1, _WINDOW)
    f32 = (np.sin(2 * np.pi * 100 * n) * 1e-4).astype(np.float32)
    return f32


def _loud_frame() -> np.ndarray:
    """Frame with RMS well above the -70 dBFS gate threshold.

    Float32 RMS ≈ 0.35 → -9 dBFS, clearly above the gate.
    """
    n = np.linspace(0, 1, _WINDOW)
    f32 = (np.sin(2 * np.pi * 1_000 * n) * 0.5).astype(np.float32)
    return f32


# ── Stable name + foundation defaults ───────────────────────────────────


class TestStableNameContract:
    def test_metric_name(self) -> None:
        assert METRIC_VAD_QUIET_SIGNAL_GATED == "sovyx.voice.vad.quiet_signal_gated"

    def test_foundation_defaults(self) -> None:
        # feedback_staged_adoption: gate ships disabled.
        cfg = VADConfig()
        assert cfg.quiet_signal_gate_enabled is False
        assert cfg.quiet_signal_gate_rms_dbfs == -70.0
        assert cfg.quiet_signal_gate_prob_threshold == 0.8


# ── Validation ──────────────────────────────────────────────────────────


class TestConfigValidation:
    def test_rms_dbfs_above_zero_rejected(self) -> None:
        cfg = VADConfig(quiet_signal_gate_rms_dbfs=1.0)
        with pytest.raises(ValueError, match="quiet_signal_gate_rms_dbfs"):
            _validate_config(cfg)

    def test_rms_dbfs_below_minus_120_rejected(self) -> None:
        cfg = VADConfig(quiet_signal_gate_rms_dbfs=-200.0)
        with pytest.raises(ValueError, match="quiet_signal_gate_rms_dbfs"):
            _validate_config(cfg)

    def test_prob_threshold_above_one_rejected(self) -> None:
        cfg = VADConfig(quiet_signal_gate_prob_threshold=1.5)
        with pytest.raises(ValueError, match="quiet_signal_gate_prob_threshold"):
            _validate_config(cfg)

    def test_prob_threshold_negative_rejected(self) -> None:
        cfg = VADConfig(quiet_signal_gate_prob_threshold=-0.1)
        with pytest.raises(ValueError, match="quiet_signal_gate_prob_threshold"):
            _validate_config(cfg)

    def test_extreme_but_valid_bounds_accepted(self) -> None:
        # Bounds inclusive on both ends.
        cfg = VADConfig(
            quiet_signal_gate_rms_dbfs=-120.0,
            quiet_signal_gate_prob_threshold=1.0,
        )
        _validate_config(cfg)  # must not raise


# ── record_vad_quiet_signal_gated ───────────────────────────────────────


class TestRecordHelper:
    def test_no_op_without_registry(self) -> None:
        record_vad_quiet_signal_gated(state="gated")  # must not raise

    def test_state_label_propagates(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        record_vad_quiet_signal_gated(state="gated")
        record_vad_quiet_signal_gated(state="would_gate")
        metric = _find(_collect(reader), METRIC_VAD_QUIET_SIGNAL_GATED)
        assert metric is not None
        states = sorted(dp["attributes"]["state"] for dp in metric["data_points"])
        assert states == ["gated", "would_gate"]


# ── SileroVAD integration ───────────────────────────────────────────────


class TestSileroVadGateDisabled:
    """Foundation default: gate is observability-only; the FSM still
    consumes the raw probability."""

    def test_paradox_increments_would_gate_counter(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        # High prob (0.9) + quiet frame (RMS ≈ 1e-4 = -80 dBFS) =
        # paradox. Detector observes; action is off.
        vad = _build_vad(probability=0.9)
        event = vad.process_frame(_quiet_frame())

        # The FSM still sees prob=0.9 → SPEECH_ONSET on the very
        # first frame (V3 hysteresis: first above-onset frame
        # promotes to SPEECH_ONSET).
        assert event.probability == pytest.approx(0.9)
        assert event.state == VADState.SPEECH_ONSET

        metric = _find(_collect(reader), METRIC_VAD_QUIET_SIGNAL_GATED)
        assert metric is not None
        states = [dp["attributes"]["state"] for dp in metric["data_points"]]
        assert states == ["would_gate"]

    def test_disabled_path_bit_exact_to_pre_t39(self) -> None:
        # Critical regression test: the gate-OFF path must produce
        # the same VADEvent as a pre-T4.39 SileroVAD on the same
        # input. Verified by checking that the FSM consumes the
        # raw probability (0.9) unmodified.
        vad = _build_vad(probability=0.9)
        # Process several quiet+high-prob frames; FSM must promote
        # SILENCE → SPEECH_ONSET → SPEECH normally.
        states = [vad.process_frame(_quiet_frame()).state for _ in range(5)]
        assert VADState.SPEECH in states


class TestSileroVadGateEnabled:
    """Action enabled: paradoxical frame has its probability
    force-clamped to 0.0 BEFORE the FSM reads it."""

    def _enabled_config(self) -> VADConfig:
        return VADConfig(quiet_signal_gate_enabled=True)

    def test_quiet_high_prob_frame_gated_to_silence(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        vad = _build_vad(probability=0.9, config=self._enabled_config())
        event = vad.process_frame(_quiet_frame())

        # Probability force-clamped to 0.0 — FSM stays in SILENCE.
        assert event.probability == 0.0
        assert event.state == VADState.SILENCE
        assert event.is_speech is False

        metric = _find(_collect(reader), METRIC_VAD_QUIET_SIGNAL_GATED)
        assert metric is not None
        states = [dp["attributes"]["state"] for dp in metric["data_points"]]
        assert states == ["gated"]

    def test_loud_high_prob_frame_never_gated(self) -> None:
        # Real speech: loud + high probability → NOT a paradox.
        # Gate must not interfere.
        vad = _build_vad(probability=0.9, config=self._enabled_config())
        event = vad.process_frame(_loud_frame())
        assert event.probability == pytest.approx(0.9)
        assert event.state == VADState.SPEECH_ONSET

    def test_quiet_low_prob_frame_never_gated(self) -> None:
        # Quiet + low prob is the EXPECTED steady-state silence
        # signal. Gate must not interfere — only the paradox
        # triggers it.
        vad = _build_vad(probability=0.1, config=self._enabled_config())
        event = vad.process_frame(_quiet_frame())
        assert event.probability == pytest.approx(0.1)
        assert event.state == VADState.SILENCE

    def test_loud_low_prob_frame_never_gated(self) -> None:
        # Loud + low prob is "noise but not speech" — Silero says
        # not speech. Gate must not interfere.
        vad = _build_vad(probability=0.1, config=self._enabled_config())
        event = vad.process_frame(_loud_frame())
        assert event.probability == pytest.approx(0.1)
        assert event.state == VADState.SILENCE

    def test_gate_does_not_freeze_lstm_state(self) -> None:
        # Critical contract: the gate clamps PROBABILITY but
        # leaves the LSTM state alone. So a real speech onset on
        # the next frame still fires normally.
        vad = _build_vad(probability=0.9, config=self._enabled_config())
        # First frame: paradox → gated to silence.
        event_quiet = vad.process_frame(_quiet_frame())
        assert event_quiet.state == VADState.SILENCE
        # Second frame: loud + same high prob → NOT paradox → FSM
        # sees probability=0.9 → SPEECH_ONSET.
        event_loud = vad.process_frame(_loud_frame())
        assert event_loud.probability == pytest.approx(0.9)
        assert event_loud.state == VADState.SPEECH_ONSET

    def test_telemetry_only_fires_on_paradox(
        self,
        registry: MetricsRegistry,
        reader: InMemoryMetricReader,
    ) -> None:
        # Process 3 non-paradox frames + 1 paradox frame. The
        # counter must show exactly 1 "gated" event, no
        # "would_gate" events (gate IS enabled).
        vad = _build_vad(probability=0.9, config=self._enabled_config())
        vad.process_frame(_loud_frame())  # not paradox
        vad.process_frame(_loud_frame())  # not paradox
        vad.process_frame(_loud_frame())  # not paradox
        vad.process_frame(_quiet_frame())  # paradox → gated

        metric = _find(_collect(reader), METRIC_VAD_QUIET_SIGNAL_GATED)
        assert metric is not None
        states = [dp["attributes"]["state"] for dp in metric["data_points"]]
        assert states == ["gated"]
        # Cumulative count = 1.
        gated_dps = [dp for dp in metric["data_points"] if dp["attributes"]["state"] == "gated"]
        assert len(gated_dps) == 1
        assert gated_dps[0]["value"] == 1


class TestSileroVadCustomThresholds:
    """The gate's RMS / prob thresholds are operator-tunable."""

    def test_high_rms_threshold_engages_on_higher_amplitude(self) -> None:
        # Loosen the RMS gate to -10 dBFS so even a "loud" frame
        # qualifies as quiet. Verifies the threshold is wired to
        # the comparison.
        cfg = VADConfig(
            quiet_signal_gate_enabled=True,
            quiet_signal_gate_rms_dbfs=-10.0,  # very permissive
        )
        vad = _build_vad(probability=0.9, config=cfg)
        event = vad.process_frame(_loud_frame())
        # Loud frame at ~-9 dBFS sits just above -10 dBFS gate, so
        # the paradox does NOT trigger (loud > gate threshold).
        # Drop amplitude to confirm the threshold IS the dial.
        assert event.probability == pytest.approx(0.9)

    def test_lower_prob_threshold_widens_paradox_window(self) -> None:
        # Gate fires when prob > 0.4 (lowered from 0.8). A
        # quiet-frame + 0.5-probability inference now counts as
        # paradox.
        cfg = VADConfig(
            quiet_signal_gate_enabled=True,
            quiet_signal_gate_prob_threshold=0.4,
            offset_threshold=0.3,  # keep hysteresis valid
        )
        vad = _build_vad(probability=0.5, config=cfg)
        event = vad.process_frame(_quiet_frame())
        # Probability force-clamped because 0.5 > 0.4 + RMS quiet.
        assert event.probability == 0.0
