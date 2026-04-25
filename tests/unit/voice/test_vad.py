"""Tests for SileroVAD v5 — onset/offset FSM, hysteresis, 512-sample window (V05-17).

Strategy: mock ONNX session to control probabilities, verify FSM transitions.
"""

from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from sovyx.voice.vad import (
    _CORRUPTION_RECOVERY_FRAMES,
    _CORRUPTION_UNRECOVERABLE_THRESHOLD,
    _CORRUPTION_UNRECOVERABLE_WINDOW,
    _HYSTERESIS_MIN_DELTA,
    _LSTM_STATE_SHAPE,
    SILERO_CANONICAL_HYSTERESIS_DELTA,
    SileroVAD,
    VADConfig,
    VADEvent,
    VADState,
    _validate_config,
    _validate_inference_outputs,
)

_VAD_LOGGER = "sovyx.voice.vad"


def _transitions_of(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    """Return VAD state-transition event payloads observed by ``caplog``.

    See the rationale in ``tests/unit/voice/conftest.py``: Sovyx's structlog
    chain ends at ``wrap_for_formatter`` which delivers the event_dict as the
    stdlib ``LogRecord.msg``. Using ``caplog`` sidesteps ``capture_logs``'
    fragility against ``structlog.configure`` churn.
    """
    return [
        r.msg
        for r in caplog.records
        if r.name == _VAD_LOGGER
        and isinstance(r.msg, dict)
        and r.msg.get("event") == "vad_state_transition"
    ]


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------

_WINDOW = 512  # 16 kHz default


def _make_mock_session(probabilities: list[float]) -> MagicMock:
    """Build a mock ONNX session that returns *probabilities* in sequence."""
    session = MagicMock()
    call_idx = {"i": 0}

    def _run(_output_names: Any, inputs: dict[str, Any]) -> list[Any]:  # noqa: ANN401
        idx = call_idx["i"] % len(probabilities)
        call_idx["i"] += 1
        prob = probabilities[idx]
        output = np.array([[prob]], dtype=np.float32)
        state = inputs["state"]
        return [output, state]

    session.run = _run
    return session


def _build_vad(
    probabilities: list[float],
    config: VADConfig | None = None,
) -> SileroVAD:
    """Construct a SileroVAD with mocked ONNX session.

    Disables the band-aid #36 smoke probe so the mock session's
    deterministic probability sequence isn't consumed by the
    construction-time probe call.
    """
    cfg = config or VADConfig()
    mock_session = _make_mock_session(probabilities)

    mock_ort = MagicMock()
    mock_ort.SessionOptions.return_value = MagicMock()
    mock_ort.GraphOptimizationLevel.ORT_ENABLE_ALL = 99
    mock_ort.InferenceSession.return_value = mock_session

    with patch.dict("sys.modules", {"onnxruntime": mock_ort}):
        vad = SileroVAD(
            Path("/fake/model.onnx"),
            config=cfg,
            smoke_probe_at_construction=False,
        )

    return vad


def _make_corruptable_session(
    outputs: list[tuple[float, np.ndarray | None]],
) -> MagicMock:
    """Mock ONNX session that returns ``(probability, lstm_state)`` per call.

    Each tuple in ``outputs`` is one inference. ``lstm_state=None`` means
    "echo back the state the caller passed in" (current healthy
    behaviour); a concrete numpy array is returned verbatim, allowing
    NaN/Inf injection or shape mismatch tests.
    """
    session = MagicMock()
    call_idx = {"i": 0}

    def _run(_output_names: Any, inputs: dict[str, Any]) -> list[Any]:  # noqa: ANN401
        idx = call_idx["i"] % len(outputs)
        call_idx["i"] += 1
        prob, custom_state = outputs[idx]
        output = np.array([[prob]], dtype=np.float32)
        state = custom_state if custom_state is not None else inputs["state"]
        return [output, state]

    session.run = _run
    return session


def _build_corruptable_vad(
    outputs: list[tuple[float, np.ndarray | None]],
    config: VADConfig | None = None,
) -> SileroVAD:
    """Construct a SileroVAD whose ONNX session emits ``(prob, state)`` pairs."""
    cfg = config or VADConfig()
    mock_session = _make_corruptable_session(outputs)

    mock_ort = MagicMock()
    mock_ort.SessionOptions.return_value = MagicMock()
    mock_ort.GraphOptimizationLevel.ORT_ENABLE_ALL = 99
    mock_ort.InferenceSession.return_value = mock_session

    with patch.dict("sys.modules", {"onnxruntime": mock_ort}):
        return SileroVAD(
            Path("/fake/model.onnx"),
            config=cfg,
            smoke_probe_at_construction=False,
        )


def _events_of(
    caplog: pytest.LogCaptureFixture,
    event_name: str,
) -> list[dict[str, Any]]:
    """Filter caplog records by the structured ``event`` field."""
    return [
        r.msg
        for r in caplog.records
        if r.name == _VAD_LOGGER and isinstance(r.msg, dict) and r.msg.get("event") == event_name
    ]


def _silence_frame() -> np.ndarray:
    return np.zeros(_WINDOW, dtype=np.float32)


def _speech_frame() -> np.ndarray:
    """Non-zero frame (content doesn't matter — we mock the model)."""
    return np.ones(_WINDOW, dtype=np.float32) * 0.5


# ---------------------------------------------------------------------------
# VADConfig tests
# ---------------------------------------------------------------------------


class TestVADConfig:
    """Tests for VADConfig dataclass and validation."""

    def test_defaults(self) -> None:
        cfg = VADConfig()
        assert cfg.onset_threshold == 0.5
        assert cfg.offset_threshold == 0.3
        assert cfg.min_onset_frames == 3
        assert cfg.min_offset_frames == 8
        assert cfg.sample_rate == 16000

    def test_window_size_16k(self) -> None:
        cfg = VADConfig(sample_rate=16000)
        assert cfg.window_size == 512

    def test_window_size_8k(self) -> None:
        cfg = VADConfig(sample_rate=8000)
        assert cfg.window_size == 256

    def test_window_size_unsupported_rate(self) -> None:
        cfg = VADConfig(sample_rate=44100)
        with pytest.raises(ValueError, match="Unsupported sample rate"):
            _ = cfg.window_size

    def test_validate_onset_too_low(self) -> None:
        with pytest.raises(ValueError, match="onset_threshold"):
            _validate_config(VADConfig(onset_threshold=0.0))

    def test_validate_onset_too_high(self) -> None:
        with pytest.raises(ValueError, match="onset_threshold"):
            _validate_config(VADConfig(onset_threshold=1.0))

    def test_validate_offset_too_low(self) -> None:
        with pytest.raises(ValueError, match="offset_threshold"):
            _validate_config(VADConfig(offset_threshold=0.0))

    def test_validate_offset_too_high(self) -> None:
        with pytest.raises(ValueError, match="offset_threshold"):
            _validate_config(VADConfig(offset_threshold=1.0))

    def test_validate_offset_gte_onset(self) -> None:
        with pytest.raises(ValueError, match="offset_threshold.*must be <"):
            _validate_config(VADConfig(onset_threshold=0.5, offset_threshold=0.5))

    def test_validate_offset_gt_onset(self) -> None:
        with pytest.raises(ValueError, match="offset_threshold.*must be <"):
            _validate_config(VADConfig(onset_threshold=0.3, offset_threshold=0.5))

    def test_validate_min_onset_frames_zero(self) -> None:
        with pytest.raises(ValueError, match="min_onset_frames"):
            _validate_config(VADConfig(min_onset_frames=0))

    def test_validate_min_offset_frames_zero(self) -> None:
        with pytest.raises(ValueError, match="min_offset_frames"):
            _validate_config(VADConfig(min_offset_frames=0))

    def test_validate_invalid_sample_rate(self) -> None:
        with pytest.raises(ValueError, match="Unsupported sample rate"):
            _validate_config(VADConfig(sample_rate=22050))

    def test_frozen(self) -> None:
        cfg = VADConfig()
        with pytest.raises(AttributeError):
            cfg.onset_threshold = 0.9  # type: ignore[misc]


# ---------------------------------------------------------------------------
# VADEvent tests
# ---------------------------------------------------------------------------


class TestVADEvent:
    """Tests for VADEvent dataclass."""

    def test_fields(self) -> None:
        evt = VADEvent(is_speech=True, probability=0.8, state=VADState.SPEECH)
        assert evt.is_speech is True
        assert evt.probability == pytest.approx(0.8)
        assert evt.state == VADState.SPEECH

    def test_frozen(self) -> None:
        evt = VADEvent(is_speech=False, probability=0.1, state=VADState.SILENCE)
        with pytest.raises(AttributeError):
            evt.is_speech = True  # type: ignore[misc]


# ---------------------------------------------------------------------------
# VADState enum tests
# ---------------------------------------------------------------------------


class TestVADState:
    """Tests for the VADState enum values."""

    def test_all_states_defined(self) -> None:
        assert set(VADState) == {
            VADState.SILENCE,
            VADState.SPEECH_ONSET,
            VADState.SPEECH,
            VADState.SPEECH_OFFSET,
        }


# ---------------------------------------------------------------------------
# FSM state transition tests
# ---------------------------------------------------------------------------


class TestFSMTransitions:
    """Full FSM transition coverage: SILENCE→ONSET→SPEECH→OFFSET→SILENCE."""

    def test_starts_in_silence(self) -> None:
        vad = _build_vad([0.1])
        assert vad.state == VADState.SILENCE
        assert vad.is_speaking is False

    def test_silence_stays_on_low_prob(self) -> None:
        vad = _build_vad([0.1])
        evt = vad.process_frame(_silence_frame())
        assert evt.is_speech is False
        assert evt.state == VADState.SILENCE

    def test_silence_to_onset(self) -> None:
        """One frame above onset threshold → SPEECH_ONSET (not yet SPEECH)."""
        vad = _build_vad([0.6])
        evt = vad.process_frame(_speech_frame())
        assert evt.is_speech is False
        assert evt.state == VADState.SPEECH_ONSET

    def test_onset_requires_consecutive_frames(self) -> None:
        """Need min_onset_frames consecutive frames above threshold."""
        config = VADConfig(min_onset_frames=3)
        vad = _build_vad([0.6, 0.7, 0.8], config=config)
        frame = _speech_frame()

        evt1 = vad.process_frame(frame)  # Frame 1 → ONSET, count=1
        assert evt1.state == VADState.SPEECH_ONSET
        assert evt1.is_speech is False

        evt2 = vad.process_frame(frame)  # Frame 2 → ONSET, count=2
        assert evt2.state == VADState.SPEECH_ONSET
        assert evt2.is_speech is False

        evt3 = vad.process_frame(frame)  # Frame 3 → SPEECH (count=3 >= min)
        assert evt3.state == VADState.SPEECH
        assert evt3.is_speech is True

    def test_onset_false_alarm_returns_to_silence(self) -> None:
        """If prob drops below onset during ONSET → back to SILENCE."""
        config = VADConfig(min_onset_frames=3)
        vad = _build_vad([0.6, 0.2], config=config)
        frame = _speech_frame()

        vad.process_frame(frame)  # → ONSET
        assert vad.state == VADState.SPEECH_ONSET

        evt = vad.process_frame(frame)  # prob 0.2 < 0.5 → SILENCE
        assert evt.state == VADState.SILENCE
        assert evt.is_speech is False

    def test_speech_continues_on_high_prob(self) -> None:
        """Once in SPEECH, high prob keeps it there."""
        config = VADConfig(min_onset_frames=1)
        vad = _build_vad([0.8, 0.9], config=config)
        frame = _speech_frame()

        vad.process_frame(frame)  # → SPEECH (min_onset_frames=1)
        evt = vad.process_frame(frame)
        assert evt.state == VADState.SPEECH
        assert evt.is_speech is True

    def test_speech_to_offset(self) -> None:
        """Low prob during SPEECH → SPEECH_OFFSET (still returns True)."""
        config = VADConfig(min_onset_frames=1)
        vad = _build_vad([0.8, 0.1], config=config)
        frame = _speech_frame()

        vad.process_frame(frame)  # → SPEECH
        evt = vad.process_frame(frame)  # prob 0.1 < 0.3 → OFFSET
        assert evt.state == VADState.SPEECH_OFFSET
        assert evt.is_speech is True  # OFFSET still counts as speaking

    def test_offset_requires_consecutive_frames(self) -> None:
        """Need min_offset_frames consecutive below threshold to end speech."""
        config = VADConfig(min_onset_frames=1, min_offset_frames=3)
        # 1 frame onset, then 3 frames below offset
        vad = _build_vad([0.8, 0.1, 0.1, 0.1], config=config)
        frame = _speech_frame()

        vad.process_frame(frame)  # → SPEECH
        assert vad.state == VADState.SPEECH

        evt1 = vad.process_frame(frame)  # → OFFSET, count=1
        assert evt1.state == VADState.SPEECH_OFFSET
        assert evt1.is_speech is True

        evt2 = vad.process_frame(frame)  # → OFFSET, count=2
        assert evt2.state == VADState.SPEECH_OFFSET
        assert evt2.is_speech is True

        evt3 = vad.process_frame(frame)  # → SILENCE (count=3 >= min)
        assert evt3.state == VADState.SILENCE
        assert evt3.is_speech is False

    def test_offset_speech_resumes(self) -> None:
        """If prob rises during OFFSET → back to SPEECH."""
        config = VADConfig(min_onset_frames=1, min_offset_frames=3)
        vad = _build_vad([0.8, 0.1, 0.7], config=config)
        frame = _speech_frame()

        vad.process_frame(frame)  # → SPEECH
        vad.process_frame(frame)  # → OFFSET

        evt = vad.process_frame(frame)  # prob 0.7 > 0.3 → back to SPEECH
        assert evt.state == VADState.SPEECH
        assert evt.is_speech is True

    def test_full_cycle(self) -> None:
        """SILENCE → ONSET → SPEECH → OFFSET → SILENCE complete cycle."""
        config = VADConfig(min_onset_frames=2, min_offset_frames=2)
        probs = [
            0.6,
            0.7,  # onset (2 frames)
            0.9,
            0.8,  # speech
            0.1,
            0.1,  # offset (2 frames → silence)
        ]
        vad = _build_vad(probs, config=config)
        frame = _speech_frame()

        states = []
        speeches = []
        for _ in range(6):
            evt = vad.process_frame(frame)
            states.append(evt.state)
            speeches.append(evt.is_speech)

        assert states == [
            VADState.SPEECH_ONSET,
            VADState.SPEECH,
            VADState.SPEECH,
            VADState.SPEECH,
            VADState.SPEECH_OFFSET,
            VADState.SILENCE,
        ]
        assert speeches == [False, True, True, True, True, False]

    def test_is_speaking_property(self) -> None:
        """is_speaking includes both SPEECH and SPEECH_OFFSET."""
        config = VADConfig(min_onset_frames=1, min_offset_frames=3)
        vad = _build_vad([0.8, 0.1], config=config)
        frame = _speech_frame()

        assert vad.is_speaking is False  # SILENCE

        vad.process_frame(frame)  # → SPEECH
        assert vad.is_speaking is True

        vad.process_frame(frame)  # → OFFSET
        assert vad.is_speaking is True


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------


class TestReset:
    """Tests for VAD reset functionality."""

    def test_reset_clears_state(self) -> None:
        config = VADConfig(min_onset_frames=1)
        vad = _build_vad([0.8, 0.1], config=config)
        frame = _speech_frame()

        vad.process_frame(frame)  # → SPEECH
        assert vad.state == VADState.SPEECH

        vad.reset()
        assert vad.state == VADState.SILENCE
        assert vad.is_speaking is False

    def test_reset_clears_lstm_state(self) -> None:
        config = VADConfig(min_onset_frames=1)
        vad = _build_vad([0.8], config=config)
        frame = _speech_frame()

        vad.process_frame(frame)
        vad.reset()

        # LSTM state should be zeroed
        assert np.all(vad._state == 0.0)  # noqa: SLF001


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


class TestInputValidation:
    """Tests for audio frame validation."""

    def test_wrong_frame_length(self) -> None:
        vad = _build_vad([0.5])
        wrong_frame = np.zeros(256, dtype=np.float32)
        with pytest.raises(ValueError, match="Expected frame of 512"):
            vad.process_frame(wrong_frame)

    def test_wrong_frame_length_2d(self) -> None:
        vad = _build_vad([0.5])
        wrong_frame = np.zeros((1, 512), dtype=np.float32)
        with pytest.raises(ValueError, match="Expected frame of 512"):
            vad.process_frame(wrong_frame)

    def test_int16_input_normalised(self) -> None:
        """int16 frames should be accepted and normalised to float32."""
        config = VADConfig(min_onset_frames=1)
        vad = _build_vad([0.6], config=config)
        int16_frame = np.zeros(_WINDOW, dtype=np.int16)
        evt = vad.process_frame(int16_frame)
        assert isinstance(evt, VADEvent)

    def test_8k_window_size(self) -> None:
        """8 kHz config should expect 256 samples."""
        config = VADConfig(sample_rate=8000)
        vad = _build_vad([0.1], config=config)
        frame_256 = np.zeros(256, dtype=np.float32)
        evt = vad.process_frame(frame_256)
        assert evt.state == VADState.SILENCE

    def test_8k_rejects_512_frame(self) -> None:
        """8 kHz config must reject 512-sample frames."""
        config = VADConfig(sample_rate=8000)
        vad = _build_vad([0.1], config=config)
        frame_512 = np.zeros(512, dtype=np.float32)
        with pytest.raises(ValueError, match="Expected frame of 256"):
            vad.process_frame(frame_512)


# ---------------------------------------------------------------------------
# Config property
# ---------------------------------------------------------------------------


class TestConfigProperty:
    """Tests for config accessor."""

    def test_config_returns_active_config(self) -> None:
        cfg = VADConfig(onset_threshold=0.7, offset_threshold=0.4)
        vad = _build_vad([0.1], config=cfg)
        assert vad.config is cfg
        assert vad.config.onset_threshold == 0.7


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge case and boundary condition tests."""

    def test_silence_only_stream(self) -> None:
        """100 frames of silence — should never trigger speech."""
        vad = _build_vad([0.1])
        frame = _silence_frame()
        for _ in range(100):
            evt = vad.process_frame(frame)
            assert evt.is_speech is False
            assert evt.state == VADState.SILENCE

    def test_continuous_speech(self) -> None:
        """After onset, continuous high prob stays in SPEECH."""
        config = VADConfig(min_onset_frames=2)
        vad = _build_vad([0.9], config=config)
        frame = _speech_frame()

        # First two frames for onset
        vad.process_frame(frame)
        vad.process_frame(frame)

        # Next 100 should all be SPEECH
        for _ in range(100):
            evt = vad.process_frame(frame)
            assert evt.is_speech is True
            assert evt.state == VADState.SPEECH

    def test_very_short_utterance_rejected(self) -> None:
        """Speech shorter than min_onset_frames is rejected as false alarm."""
        config = VADConfig(min_onset_frames=5)
        # 3 frames above onset, then drop — should never reach SPEECH
        vad = _build_vad([0.6, 0.7, 0.8, 0.1], config=config)
        frame = _speech_frame()

        for _ in range(3):
            evt = vad.process_frame(frame)
            assert evt.state == VADState.SPEECH_ONSET
            assert evt.is_speech is False

        evt = vad.process_frame(frame)
        assert evt.state == VADState.SILENCE
        assert evt.is_speech is False

    def test_borderline_probability_at_onset_threshold(self) -> None:
        """Probability exactly at onset threshold does NOT trigger (> not >=)."""
        vad = _build_vad([0.5])  # onset_threshold default is 0.5
        frame = _speech_frame()
        evt = vad.process_frame(frame)
        assert evt.state == VADState.SILENCE  # 0.5 is NOT > 0.5

    def test_borderline_probability_at_offset_threshold(self) -> None:
        """Probability exactly at offset threshold does NOT trigger offset (< not <=)."""
        config = VADConfig(min_onset_frames=1, offset_threshold=0.3)
        vad = _build_vad([0.8, 0.3], config=config)
        frame = _speech_frame()

        vad.process_frame(frame)  # → SPEECH
        evt = vad.process_frame(frame)  # prob 0.3, NOT < 0.3
        assert evt.state == VADState.SPEECH  # stays in SPEECH

    def test_rapid_alternation_damped(self) -> None:
        """Rapidly alternating probabilities should NOT cause rapid state changes."""
        config = VADConfig(min_onset_frames=3, min_offset_frames=3)
        # Alternating high/low — should bounce between SILENCE and ONSET
        probs = [0.6, 0.1, 0.6, 0.1, 0.6, 0.1]
        vad = _build_vad(probs, config=config)
        frame = _speech_frame()

        for _ in range(6):
            evt = vad.process_frame(frame)
            # Should never reach SPEECH due to insufficient consecutive frames
            assert evt.state in (VADState.SILENCE, VADState.SPEECH_ONSET)
            assert evt.is_speech is False


# ---------------------------------------------------------------------------
# Property-based tests
# ---------------------------------------------------------------------------


class TestPropertyBased:
    """Hypothesis property-based tests for FSM invariants."""

    @settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(
        probs=st.lists(
            st.floats(min_value=0.0, max_value=1.0),
            min_size=1,
            max_size=50,
        ),
    )
    def test_probability_always_in_range(self, probs: list[float]) -> None:
        """Returned probability should always be between 0 and 1."""
        vad = _build_vad(probs)
        frame = _speech_frame()
        for _ in range(len(probs)):
            evt = vad.process_frame(frame)
            assert 0.0 <= evt.probability <= 1.0

    @settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(
        probs=st.lists(
            st.floats(min_value=0.0, max_value=1.0),
            min_size=1,
            max_size=50,
        ),
    )
    def test_state_always_valid(self, probs: list[float]) -> None:
        """FSM state should always be a valid VADState member."""
        vad = _build_vad(probs)
        frame = _speech_frame()
        for _ in range(len(probs)):
            evt = vad.process_frame(frame)
            assert evt.state in set(VADState)

    @settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(
        probs=st.lists(
            st.floats(min_value=0.0, max_value=1.0),
            min_size=1,
            max_size=50,
        ),
    )
    def test_is_speech_consistent_with_state(self, probs: list[float]) -> None:
        """is_speech=True only in SPEECH or SPEECH_OFFSET states."""
        vad = _build_vad(probs)
        frame = _speech_frame()
        for _ in range(len(probs)):
            evt = vad.process_frame(frame)
            if evt.is_speech:
                assert evt.state in (VADState.SPEECH, VADState.SPEECH_OFFSET)
            else:
                assert evt.state in (VADState.SILENCE, VADState.SPEECH_ONSET)

    @settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(
        probs=st.lists(
            st.floats(min_value=0.0, max_value=1.0),
            min_size=1,
            max_size=100,
        ),
    )
    def test_reset_always_returns_to_silence(self, probs: list[float]) -> None:
        """After processing any sequence, reset should restore SILENCE."""
        vad = _build_vad(probs)
        frame = _speech_frame()
        for _ in range(len(probs)):
            vad.process_frame(frame)
        vad.reset()
        assert vad.state == VADState.SILENCE
        assert vad.is_speaking is False


# ---------------------------------------------------------------------------
# Construction / init
# ---------------------------------------------------------------------------


class TestConstruction:
    """Tests for SileroVAD construction and initialisation."""

    def test_invalid_config_raises(self) -> None:
        """Invalid config should raise at construction time."""
        bad_config = VADConfig(onset_threshold=0.3, offset_threshold=0.5)
        with pytest.raises(ValueError, match="offset_threshold"):
            _build_vad([0.1], config=bad_config)

    def test_default_config_applied(self) -> None:
        """None config should use defaults."""
        vad = _build_vad([0.1])
        assert vad.config == VADConfig()


# ---------------------------------------------------------------------------
# Telemetry — FSM transition logs
# ---------------------------------------------------------------------------


class TestStateTransitionTelemetry:
    """``vad_state_transition`` is emitted whenever the FSM changes state.

    Regression for the silent-pipeline diagnosis: before this telemetry,
    operators had no way to tell whether VAD probabilities crossed the
    onset threshold at all — the orchestrator would sit in IDLE forever
    with no signal in the log.
    """

    def test_no_log_when_state_unchanged(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.INFO, logger=_VAD_LOGGER)
        vad = _build_vad([0.1, 0.1, 0.1])
        caplog.clear()
        for _ in range(3):
            vad.process_frame(_silence_frame())
        assert _transitions_of(caplog) == []

    def test_logs_silence_to_onset(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.INFO, logger=_VAD_LOGGER)
        vad = _build_vad([0.9])
        caplog.clear()
        vad.process_frame(_speech_frame())
        transitions = _transitions_of(caplog)
        assert len(transitions) == 1
        assert transitions[0]["from_state"] == "SILENCE"
        assert transitions[0]["to_state"] == "SPEECH_ONSET"
        assert transitions[0]["probability"] == 0.9  # noqa: PLR2004

    def test_logs_full_onset_to_speech_sequence(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.INFO, logger=_VAD_LOGGER)
        vad = _build_vad([0.9, 0.9, 0.9])
        caplog.clear()
        for _ in range(3):
            vad.process_frame(_speech_frame())
        transitions = [(log["from_state"], log["to_state"]) for log in _transitions_of(caplog)]
        assert transitions == [
            ("SILENCE", "SPEECH_ONSET"),
            ("SPEECH_ONSET", "SPEECH"),
        ]


# ---------------------------------------------------------------------------
# V1: NaN/Inf inference guard (Ring 3 defense-in-depth)
# ---------------------------------------------------------------------------
#
# Regression for the "deaf microphone" silent-failure class: an ONNX
# model that returns NaN/Inf in the probability OR poisons the recurrent
# LSTM state will silently freeze the FSM in SILENCE forever (every
# ``prob > threshold`` comparison evaluates False on NaN). The V1 guard
# fail-closes (treats corrupt frame as silence), zeros the LSTM state,
# and emits structured telemetry at three escalation tiers:
#
# * ``voice.vad.session_corrupt`` — every detection (WARNING)
# * ``voice.vad.session_recovered`` — after N clean frames (INFO)
# * ``voice.vad.session_unrecoverable`` — repeated corruption (ERROR)
#
# Reference: MISSION-voice-mixer-enterprise-refactor-2026-04-25 §2.3, §3.2, V1.


def _bad_state(value: float) -> np.ndarray:
    """Return an LSTM state filled with ``value`` (NaN/Inf for tests)."""
    return np.full(_LSTM_STATE_SHAPE, value, dtype=np.float32)


def _good_state() -> np.ndarray:
    return np.zeros(_LSTM_STATE_SHAPE, dtype=np.float32)


class TestValidateInferenceOutputs:
    """Pure-function validator — exhaustive coverage of corruption taxa."""

    def test_clean_inputs_pass(self) -> None:
        is_corrupt, kind = _validate_inference_outputs(0.5, _good_state())
        assert is_corrupt is False
        assert kind == ""

    def test_probability_at_zero_passes(self) -> None:
        is_corrupt, _kind = _validate_inference_outputs(0.0, _good_state())
        assert is_corrupt is False

    def test_probability_at_one_passes(self) -> None:
        is_corrupt, _kind = _validate_inference_outputs(1.0, _good_state())
        assert is_corrupt is False

    def test_probability_nan_detected(self) -> None:
        is_corrupt, kind = _validate_inference_outputs(float("nan"), _good_state())
        assert is_corrupt is True
        assert kind == "probability_nan"

    def test_probability_pos_inf_detected(self) -> None:
        is_corrupt, kind = _validate_inference_outputs(float("inf"), _good_state())
        assert is_corrupt is True
        assert kind == "probability_nan"

    def test_probability_neg_inf_detected(self) -> None:
        is_corrupt, kind = _validate_inference_outputs(float("-inf"), _good_state())
        assert is_corrupt is True
        assert kind == "probability_nan"

    def test_probability_above_one_detected(self) -> None:
        is_corrupt, kind = _validate_inference_outputs(1.5, _good_state())
        assert is_corrupt is True
        assert kind == "probability_out_of_range"

    def test_probability_negative_detected(self) -> None:
        is_corrupt, kind = _validate_inference_outputs(-0.1, _good_state())
        assert is_corrupt is True
        assert kind == "probability_out_of_range"

    def test_lstm_state_nan_detected(self) -> None:
        is_corrupt, kind = _validate_inference_outputs(0.5, _bad_state(float("nan")))
        assert is_corrupt is True
        assert kind == "lstm_state_nan"

    def test_lstm_state_pos_inf_detected(self) -> None:
        is_corrupt, kind = _validate_inference_outputs(0.5, _bad_state(float("inf")))
        assert is_corrupt is True
        assert kind == "lstm_state_nan"

    def test_lstm_state_partial_nan_detected(self) -> None:
        """Even one NaN cell in the recurrent state must trip the guard."""
        state = _good_state()
        state[0, 0, 42] = float("nan")
        is_corrupt, kind = _validate_inference_outputs(0.5, state)
        assert is_corrupt is True
        assert kind == "lstm_state_nan"

    def test_lstm_state_wrong_shape_detected(self) -> None:
        wrong_shape = np.zeros((1, 1, 64), dtype=np.float32)
        is_corrupt, kind = _validate_inference_outputs(0.5, wrong_shape)
        assert is_corrupt is True
        assert kind == "lstm_state_shape_invalid"

    def test_lstm_state_not_ndarray_detected(self) -> None:
        is_corrupt, kind = _validate_inference_outputs(0.5, [[0.0, 0.0]])  # type: ignore[arg-type]
        assert is_corrupt is True
        assert kind == "lstm_state_shape_invalid"

    def test_probability_checked_before_state(self) -> None:
        """Both corrupt → probability kind wins (cheaper check, more visible)."""
        is_corrupt, kind = _validate_inference_outputs(
            float("nan"),
            _bad_state(float("nan")),
        )
        assert is_corrupt is True
        assert kind == "probability_nan"


class TestVADCorruptionGuard:
    """End-to-end V1 guard wired through ``process_frame``."""

    def test_clean_run_emits_no_corruption_event(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level(logging.WARNING, logger=_VAD_LOGGER)
        vad = _build_corruptable_vad([(0.1, None)] * 5)
        for _ in range(5):
            vad.process_frame(_silence_frame())
        assert _events_of(caplog, "voice.vad.session_corrupt") == []
        assert vad.corruption_count == 0
        assert vad.is_session_unrecoverable is False

    def test_nan_probability_emits_corrupt_event(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level(logging.WARNING, logger=_VAD_LOGGER)
        vad = _build_corruptable_vad([(float("nan"), None)])
        evt = vad.process_frame(_speech_frame())
        # Fail-closed: NaN → 0.0 → FSM stays SILENCE despite "speech" frame.
        assert evt.probability == 0.0
        assert evt.is_speech is False
        assert evt.state == VADState.SILENCE
        events = _events_of(caplog, "voice.vad.session_corrupt")
        assert len(events) == 1
        assert events[0]["voice.corruption_kind"] == "probability_nan"
        assert events[0]["voice.lifetime_corruption_count"] == 1
        assert vad.corruption_count == 1

    def test_nan_lstm_state_resets_state_to_zeros(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level(logging.WARNING, logger=_VAD_LOGGER)
        # First inference returns clean prob but corrupt next-state →
        # the guard must zero state instead of accepting the corruption.
        vad = _build_corruptable_vad([(0.5, _bad_state(float("nan")))])
        vad.process_frame(_speech_frame())
        assert vad.corruption_count == 1
        assert np.all(vad._state == 0.0)  # noqa: SLF001
        events = _events_of(caplog, "voice.vad.session_corrupt")
        assert len(events) == 1
        assert events[0]["voice.corruption_kind"] == "lstm_state_nan"

    def test_corruption_does_not_drop_active_speech_state(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """One corrupt frame mid-speech must NOT collapse FSM to SILENCE."""
        caplog.set_level(logging.WARNING, logger=_VAD_LOGGER)
        config = VADConfig(min_onset_frames=1, min_offset_frames=8)
        # Get into SPEECH, then inject one corrupt frame.
        vad = _build_corruptable_vad(
            [(0.9, None), (float("nan"), None), (0.9, None)],
            config=config,
        )
        evt1 = vad.process_frame(_speech_frame())
        assert evt1.state == VADState.SPEECH
        evt2 = vad.process_frame(_speech_frame())
        # Corrupt frame → prob=0.0 → FSM hysteresis transitions to OFFSET
        # but is_speaking stays True (offset still counts as speech).
        assert evt2.is_speech is True
        assert evt2.state == VADState.SPEECH_OFFSET
        # Resume speech — FSM rebounds.
        evt3 = vad.process_frame(_speech_frame())
        assert evt3.state == VADState.SPEECH

    def test_recovery_event_after_n_clean_frames(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level(logging.INFO, logger=_VAD_LOGGER)
        vad = _build_corruptable_vad(
            [(float("nan"), None)] + [(0.1, None)] * _CORRUPTION_RECOVERY_FRAMES,
        )
        for _ in range(1 + _CORRUPTION_RECOVERY_FRAMES):
            vad.process_frame(_silence_frame())
        recovered = _events_of(caplog, "voice.vad.session_recovered")
        assert len(recovered) == 1
        assert recovered[0]["voice.clean_frames_since_corrupt"] == _CORRUPTION_RECOVERY_FRAMES

    def test_recovery_event_not_emitted_before_threshold(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level(logging.INFO, logger=_VAD_LOGGER)
        # One corruption + (N-1) clean frames → still no recovery event.
        vad = _build_corruptable_vad(
            [(float("nan"), None)] + [(0.1, None)] * (_CORRUPTION_RECOVERY_FRAMES - 1),
        )
        for _ in range(_CORRUPTION_RECOVERY_FRAMES):
            vad.process_frame(_silence_frame())
        assert _events_of(caplog, "voice.vad.session_recovered") == []

    def test_recovery_streak_reset_by_new_corruption(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level(logging.INFO, logger=_VAD_LOGGER)
        # Corrupt → 2 clean → corrupt → 2 more clean. Streak reset means
        # neither half reaches the recovery threshold (default 5).
        ops: list[tuple[float, np.ndarray | None]] = [
            (float("nan"), None),
            (0.1, None),
            (0.1, None),
            (float("nan"), None),
            (0.1, None),
            (0.1, None),
        ]
        vad = _build_corruptable_vad(ops)
        for _ in range(len(ops)):
            vad.process_frame(_silence_frame())
        assert _events_of(caplog, "voice.vad.session_recovered") == []
        assert vad.corruption_count == 2

    def test_unrecoverable_signal_emitted_on_repeated_corruption(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level(logging.ERROR, logger=_VAD_LOGGER)
        # Threshold corruptions back-to-back → well within window → fires.
        vad = _build_corruptable_vad(
            [(float("nan"), None)] * _CORRUPTION_UNRECOVERABLE_THRESHOLD,
        )
        for _ in range(_CORRUPTION_UNRECOVERABLE_THRESHOLD):
            vad.process_frame(_silence_frame())
        unrecoverable = _events_of(caplog, "voice.vad.session_unrecoverable")
        assert len(unrecoverable) == 1
        assert unrecoverable[0]["voice.window_frames"] == _CORRUPTION_UNRECOVERABLE_WINDOW
        assert vad.is_session_unrecoverable is True

    def test_unrecoverable_signal_emitted_only_once(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level(logging.ERROR, logger=_VAD_LOGGER)
        vad = _build_corruptable_vad(
            [(float("nan"), None)] * (_CORRUPTION_UNRECOVERABLE_THRESHOLD + 5),
        )
        for _ in range(_CORRUPTION_UNRECOVERABLE_THRESHOLD + 5):
            vad.process_frame(_silence_frame())
        # Multiple corruptions still produce exactly one unrecoverable.
        assert len(_events_of(caplog, "voice.vad.session_unrecoverable")) == 1

    def test_unrecoverable_not_emitted_when_corruption_outside_window(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Corruptions spaced beyond the window must NOT trigger unrecoverable."""
        caplog.set_level(logging.ERROR, logger=_VAD_LOGGER)
        # 1 corrupt + (window+1) cleans + 1 corrupt + (window+1) cleans + 1 corrupt
        # → 3 corruptions but each pair >window apart, so the sliding-window
        # span fails the check.
        ops: list[tuple[float, np.ndarray | None]] = []
        for _ in range(_CORRUPTION_UNRECOVERABLE_THRESHOLD):
            ops.append((float("nan"), None))
            ops.extend([(0.1, None)] * (_CORRUPTION_UNRECOVERABLE_WINDOW + 1))
        vad = _build_corruptable_vad(ops)
        for _ in range(len(ops)):
            vad.process_frame(_silence_frame())
        assert _events_of(caplog, "voice.vad.session_unrecoverable") == []
        assert vad.is_session_unrecoverable is False
        # But the cumulative count and per-event WARNINGs still incremented.
        assert vad.corruption_count == _CORRUPTION_UNRECOVERABLE_THRESHOLD

    def test_corruption_count_survives_reset(self) -> None:
        """``reset()`` clears FSM/state but NOT cumulative model-health counters."""
        vad = _build_corruptable_vad(
            [(float("nan"), None), (float("nan"), None)],
        )
        vad.process_frame(_silence_frame())
        vad.process_frame(_silence_frame())
        assert vad.corruption_count == 2
        vad.reset()
        assert vad.corruption_count == 2  # cumulative — does NOT reset
        assert vad.state == VADState.SILENCE  # FSM does reset

    def test_unrecoverable_flag_survives_reset(self) -> None:
        """Once unrecoverable, the session stays unrecoverable until rebuild."""
        vad = _build_corruptable_vad(
            [(float("nan"), None)] * _CORRUPTION_UNRECOVERABLE_THRESHOLD,
        )
        for _ in range(_CORRUPTION_UNRECOVERABLE_THRESHOLD):
            vad.process_frame(_silence_frame())
        assert vad.is_session_unrecoverable is True
        vad.reset()
        assert vad.is_session_unrecoverable is True

    def test_out_of_range_probability_treated_as_corruption(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Sigmoid contract violation (prob > 1.0) → corruption, not propagation."""
        caplog.set_level(logging.WARNING, logger=_VAD_LOGGER)
        vad = _build_corruptable_vad([(1.5, None)])
        evt = vad.process_frame(_speech_frame())
        assert evt.probability == 0.0
        events = _events_of(caplog, "voice.vad.session_corrupt")
        assert events[0]["voice.corruption_kind"] == "probability_out_of_range"


class TestCorruptionGuardPropertyBased:
    """Hypothesis: V1 guard never lets NaN/Inf escape into ``VADEvent``."""

    @pytest.mark.filterwarnings(
        # Hypothesis emits values like 1e308 which numpy casts to inf
        # when packed into the mock ONNX output array. The V1 guard
        # then correctly catches the inf — the warning itself is the
        # mock layer reporting the cast, not a bug. Suppress it so the
        # CI signal stays clean.
        "ignore:overflow encountered in cast:RuntimeWarning",
    )
    @settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(
        # Any sequence of arbitrary float64 values, including NaN / Inf,
        # must produce VADEvents whose ``probability`` is finite and in
        # [0, 1]. This is the V1 contract distilled to one assertion.
        probs=st.lists(
            st.one_of(
                st.floats(min_value=0.0, max_value=1.0),
                st.floats(allow_nan=True, allow_infinity=True),
            ),
            min_size=1,
            max_size=50,
        ),
    )
    def test_probability_always_finite_and_in_unit_range(
        self,
        probs: list[float],
    ) -> None:
        outputs: list[tuple[float, np.ndarray | None]] = [(p, None) for p in probs]
        vad = _build_corruptable_vad(outputs)
        for _ in range(len(probs)):
            evt = vad.process_frame(_silence_frame())
            assert math.isfinite(evt.probability)
            assert 0.0 <= evt.probability <= 1.0

    @settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(
        # When ANY frame is corrupt, the LSTM state must be zeroed by
        # the time the next frame runs — otherwise recurrent poisoning
        # would silently spread. Sample mixed clean/corrupt sequences
        # and assert the post-corruption state invariant.
        sequence=st.lists(
            st.one_of(
                st.just("clean"),
                st.just("corrupt_prob"),
                st.just("corrupt_state"),
            ),
            min_size=2,
            max_size=20,
        ),
    )
    def test_lstm_state_zeroed_after_any_corruption(
        self,
        sequence: list[str],
    ) -> None:
        outputs: list[tuple[float, np.ndarray | None]] = []
        for kind in sequence:
            if kind == "clean":
                outputs.append((0.1, None))
            elif kind == "corrupt_prob":
                outputs.append((float("nan"), None))
            else:  # corrupt_state
                outputs.append((0.5, _bad_state(float("nan"))))
        vad = _build_corruptable_vad(outputs)
        for kind in sequence:
            vad.process_frame(_silence_frame())
            if kind != "clean":
                # Right after a corrupt frame, the LSTM state must have
                # been replaced with zeros (the V1 reset action).
                assert np.all(vad._state == 0.0)  # noqa: SLF001

    @settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(
        # Counter monotonicity: corruption_count never decreases, ever.
        n_frames=st.integers(min_value=1, max_value=30),
        corrupt_indices=st.sets(
            st.integers(min_value=0, max_value=29),
            max_size=15,
        ),
    )
    def test_corruption_counter_monotonic_non_decreasing(
        self,
        n_frames: int,
        corrupt_indices: set[int],
    ) -> None:
        outputs: list[tuple[float, np.ndarray | None]] = [
            (float("nan"), None) if i in corrupt_indices else (0.1, None) for i in range(n_frames)
        ]
        vad = _build_corruptable_vad(outputs)
        prev = 0
        for _ in range(n_frames):
            vad.process_frame(_silence_frame())
            assert vad.corruption_count >= prev
            prev = vad.corruption_count


# ---------------------------------------------------------------------------
# V3: Schmitt-trigger hysteresis formalisation (Ring 3)
# ---------------------------------------------------------------------------
#
# The Schmitt trigger needs a wide-enough gap between onset and offset
# thresholds to suppress noise-floor chatter. The V3 tightening adds:
#
# * SILERO_CANONICAL_HYSTERESIS_DELTA — public constant naming the
#   Silero/LiveKit-recommended 0.15 gap.
# * _HYSTERESIS_MIN_DELTA — enforced minimum (anti-chatter floor).
# * VADConfig.with_canonical_hysteresis() — factory deriving offset
#   from onset using the canonical delta.
# * VADConfig.hysteresis_delta — computed property for observability.
#
# Reference: MISSION-voice-mixer-enterprise-refactor-2026-04-25 §2.3, V3.


class TestSchmittTriggerCanonicalConstants:
    """Public constants must match the Silero/LiveKit canonical values."""

    def test_canonical_delta_value(self) -> None:
        # If this changes, dashboards / docs / third-party integrations
        # that reference the constant break — bump deliberately, never
        # by accident.
        assert SILERO_CANONICAL_HYSTERESIS_DELTA == 0.15  # noqa: PLR2004

    def test_min_delta_below_canonical(self) -> None:
        # Anti-chatter floor must be strictly less than the canonical
        # gap — otherwise the canonical config itself would be rejected.
        assert _HYSTERESIS_MIN_DELTA < SILERO_CANONICAL_HYSTERESIS_DELTA

    def test_min_delta_strictly_positive(self) -> None:
        # Zero-or-negative floor would mean no enforcement at all.
        assert _HYSTERESIS_MIN_DELTA > 0.0


class TestHysteresisDeltaProperty:
    """``VADConfig.hysteresis_delta`` is a computed property."""

    def test_delta_for_default(self) -> None:
        cfg = VADConfig()
        assert cfg.hysteresis_delta == pytest.approx(0.2)

    def test_delta_for_canonical(self) -> None:
        cfg = VADConfig.with_canonical_hysteresis(0.7)
        assert cfg.hysteresis_delta == pytest.approx(SILERO_CANONICAL_HYSTERESIS_DELTA)

    def test_delta_property_is_read_only(self) -> None:
        # Frozen+slots dataclasses with @property raise either
        # FrozenInstanceError (sub of AttributeError) or TypeError
        # depending on the CPython version; assert on the broader
        # superclass that covers both.
        cfg = VADConfig()
        with pytest.raises((AttributeError, TypeError)):
            cfg.hysteresis_delta = 0.0  # type: ignore[misc]


class TestHysteresisMinDeltaEnforcement:
    """``_validate_config`` rejects configs below the anti-chatter floor."""

    def test_rejects_delta_below_floor(self) -> None:
        # delta = 0.04 < 0.05 → reject
        with pytest.raises(ValueError, match="hysteresis delta"):
            _validate_config(
                VADConfig(onset_threshold=0.5, offset_threshold=0.46),
            )

    def test_rejects_delta_at_floor_minus_epsilon(self) -> None:
        # delta = 0.049 < 0.05 → reject
        with pytest.raises(ValueError, match="hysteresis delta"):
            _validate_config(
                VADConfig(onset_threshold=0.5, offset_threshold=0.451),
            )

    def test_accepts_delta_at_floor(self) -> None:
        # delta = 0.05 == 0.05 → accept
        _validate_config(VADConfig(onset_threshold=0.5, offset_threshold=0.45))

    def test_accepts_default_delta(self) -> None:
        # The shipped defaults must continue to validate (no surprise
        # behaviour change in v0.22.5).
        _validate_config(VADConfig())  # delta = 0.2 ≥ 0.05

    def test_error_mentions_canonical_factory(self) -> None:
        """The error message must point users at the recommended fix."""
        with pytest.raises(ValueError, match="with_canonical_hysteresis"):
            _validate_config(
                VADConfig(onset_threshold=0.5, offset_threshold=0.48),
            )

    def test_existing_offset_gte_onset_check_still_fires_first(self) -> None:
        """offset >= onset must still raise the original error, not the new one."""
        with pytest.raises(ValueError, match="offset_threshold.*must be <"):
            _validate_config(VADConfig(onset_threshold=0.5, offset_threshold=0.5))


class TestWithCanonicalHysteresis:
    """``VADConfig.with_canonical_hysteresis`` factory contract."""

    def test_derives_offset_from_onset(self) -> None:
        cfg = VADConfig.with_canonical_hysteresis(0.7)
        assert cfg.onset_threshold == pytest.approx(0.7)
        assert cfg.offset_threshold == pytest.approx(0.55)

    def test_derives_offset_for_default_silero_recommendation(self) -> None:
        # LiveKit's "improved EOU" recommendation: onset=0.7 / offset=0.55
        cfg = VADConfig.with_canonical_hysteresis(0.7)
        assert cfg.hysteresis_delta == pytest.approx(SILERO_CANONICAL_HYSTERESIS_DELTA)

    def test_returns_validated_config(self) -> None:
        # Result must already pass _validate_config (i.e. be usable in
        # SileroVAD construction without further checks).
        cfg = VADConfig.with_canonical_hysteresis(0.6)
        _validate_config(cfg)

    def test_clamps_low_onset_to_min_delta(self) -> None:
        # onset=0.10 → naive derived offset = -0.05 (invalid). Factory
        # must fall back to the min-delta gap so the result is still
        # a valid Schmitt trigger, not an exception.
        cfg = VADConfig.with_canonical_hysteresis(0.10)
        assert cfg.onset_threshold == pytest.approx(0.10)
        assert cfg.offset_threshold > 0.0
        assert cfg.hysteresis_delta >= _HYSTERESIS_MIN_DELTA
        # And the result still validates (no exception).
        _validate_config(cfg)

    def test_high_onset_works(self) -> None:
        # onset=0.95 → derived=0.80 — well-defined, no clamping.
        cfg = VADConfig.with_canonical_hysteresis(0.95)
        assert cfg.offset_threshold == pytest.approx(0.80)
        _validate_config(cfg)

    def test_optional_frame_overrides(self) -> None:
        cfg = VADConfig.with_canonical_hysteresis(
            0.7,
            min_onset_frames=8,
            min_offset_frames=3,
        )
        assert cfg.min_onset_frames == 8  # noqa: PLR2004
        assert cfg.min_offset_frames == 3  # noqa: PLR2004

    def test_optional_sample_rate_override(self) -> None:
        cfg = VADConfig.with_canonical_hysteresis(0.7, sample_rate=8000)
        assert cfg.sample_rate == 8000  # noqa: PLR2004
        assert cfg.window_size == 256  # noqa: PLR2004

    def test_invalid_onset_propagates(self) -> None:
        # onset > 1.0 is invalid for the BASE config (sigmoid range
        # violation), separate from the hysteresis-delta check.
        with pytest.raises(ValueError, match="onset_threshold"):
            cfg = VADConfig.with_canonical_hysteresis(1.5)
            _validate_config(cfg)


class TestSchmittTriggerEndToEnd:
    """A canonical-hysteresis VAD still produces correct FSM behaviour."""

    def test_canonical_hysteresis_silence_to_speech(self) -> None:
        cfg = VADConfig.with_canonical_hysteresis(0.7, min_onset_frames=1)
        # Probability above onset (0.7) → ONSET → SPEECH (min_onset=1)
        vad = _build_vad([0.85], config=cfg)
        evt = vad.process_frame(_speech_frame())
        assert evt.state == VADState.SPEECH

    def test_canonical_hysteresis_offset_does_not_fire_on_intermediate(self) -> None:
        """Probabilities BETWEEN offset (0.55) and onset (0.7) preserve SPEECH."""
        cfg = VADConfig.with_canonical_hysteresis(
            0.7,
            min_onset_frames=1,
            min_offset_frames=3,
        )
        vad = _build_vad([0.85, 0.6, 0.6], config=cfg)
        frame = _speech_frame()
        vad.process_frame(frame)  # → SPEECH
        evt2 = vad.process_frame(frame)  # 0.6 between 0.55 and 0.7 → stays SPEECH
        assert evt2.state == VADState.SPEECH
        evt3 = vad.process_frame(frame)
        # Still SPEECH — Schmitt's middle band absorbs the noise.
        assert evt3.state == VADState.SPEECH


# ---------------------------------------------------------------------------
# M2 wire-up — RED + USE telemetry on VAD
# ---------------------------------------------------------------------------


class TestVADM2WireUp:
    """SileroVAD.process_frame must emit M2 stage events.

    Mirrors STT/TTS/capture adoption — proves the M2 foundation is
    wired in the VAD stage too. Corrupt-inference path emits DROP
    with the corruption_kind as error_type so dashboards can
    attribute the rate of ONNX corruption per kind without
    parsing logs.
    """

    def test_clean_inference_records_success_event(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:

        recorded: list[tuple[Any, Any, Any]] = []

        from sovyx.voice import vad as vad_mod
        from sovyx.voice._stage_metrics import StageEventKind, VoiceStage

        def _capture(stage: Any, kind: Any, *, error_type: str | None = None) -> None:
            recorded.append((stage, kind, error_type))

        monkeypatch.setattr(vad_mod, "record_stage_event", _capture)

        vad = _build_vad([0.5])
        vad.process_frame(_speech_frame())

        assert (VoiceStage.VAD, StageEventKind.SUCCESS, None) in recorded

    def test_corrupt_inference_records_drop_with_kind(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:

        recorded: list[tuple[Any, Any, Any]] = []

        from sovyx.voice import vad as vad_mod
        from sovyx.voice._stage_metrics import StageEventKind, VoiceStage

        def _capture(stage: Any, kind: Any, *, error_type: str | None = None) -> None:
            recorded.append((stage, kind, error_type))

        monkeypatch.setattr(vad_mod, "record_stage_event", _capture)

        # NaN probability → corruption kind "probability_nan".
        cfg = VADConfig()
        mock_session = _make_corruptable_session([(float("nan"), None)])
        mock_ort = MagicMock()
        mock_ort.SessionOptions.return_value = MagicMock()
        mock_ort.GraphOptimizationLevel.ORT_ENABLE_ALL = 99
        mock_ort.InferenceSession.return_value = mock_session
        with patch.dict("sys.modules", {"onnxruntime": mock_ort}):
            vad = SileroVAD(
                Path("/fake/model.onnx"),
                config=cfg,
                smoke_probe_at_construction=False,
            )

        vad.process_frame(_speech_frame())

        assert (VoiceStage.VAD, StageEventKind.DROP, "probability_nan") in recorded
        # SUCCESS must NOT have been recorded for this frame.
        successes = [
            (s, k, et)
            for (s, k, et) in recorded
            if s == VoiceStage.VAD and k == StageEventKind.SUCCESS
        ]
        assert successes == []

    def test_bad_frame_shape_propagates_as_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Wrong window size → ValueError. Caught by
        measure_stage_duration's BaseException handler and re-raised;
        no SUCCESS or DROP event recorded (the stage never decided
        an outcome — caller bug)."""

        recorded: list[tuple[Any, Any, Any]] = []

        from sovyx.voice import vad as vad_mod
        from sovyx.voice._stage_metrics import VoiceStage

        def _capture(stage: Any, kind: Any, *, error_type: str | None = None) -> None:
            recorded.append((stage, kind, error_type))

        monkeypatch.setattr(vad_mod, "record_stage_event", _capture)

        vad = _build_vad([0.5])
        bad = np.zeros(7, dtype=np.float32)  # not a valid window size
        with pytest.raises(ValueError):  # noqa: PT011
            vad.process_frame(bad)

        # No event recorded — exception propagated before any
        # record_stage_event call site.
        vad_events = [(s, k, et) for (s, k, et) in recorded if s == VoiceStage.VAD]
        assert vad_events == []


# ---------------------------------------------------------------------------
# TS3 chaos wire-up — VAD_CORRUPTION injection
# ---------------------------------------------------------------------------


class TestVADChaosWireUp:
    """SileroVAD.process_frame must honour the chaos injector.

    With chaos enabled at 100% rate, every frame's raw
    probability gets overwritten with NaN before the V1 guard
    runs — proving the V1 corruption-detection + recovery path
    fires correctly under chaos, not just under the
    deterministic NaN-injection mock the V1 unit tests use.
    """

    def test_chaos_disabled_no_injection(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from sovyx.voice._chaos import _ENABLED_ENV_VAR, _RATE_ENV_VAR_PREFIX

        monkeypatch.delenv(_ENABLED_ENV_VAR, raising=False)
        monkeypatch.setenv(f"{_RATE_ENV_VAR_PREFIX}VAD_CORRUPTION_PCT", "100")

        vad = _build_vad([0.5])
        evt = vad.process_frame(_speech_frame())
        # No corruption registered; probability stays clean.
        assert vad.corruption_count == 0
        assert evt.probability == 0.5  # noqa: PLR2004

    def test_chaos_at_100_pct_injects_nan_every_frame(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from sovyx.voice._chaos import _ENABLED_ENV_VAR, _RATE_ENV_VAR_PREFIX

        monkeypatch.setenv(_ENABLED_ENV_VAR, "true")
        monkeypatch.setenv(f"{_RATE_ENV_VAR_PREFIX}VAD_CORRUPTION_PCT", "100")

        vad = _build_vad([0.5])
        # Drive 5 frames — every one should be classified as
        # corrupt by the V1 guard (NaN injected by chaos).
        for _ in range(5):
            evt = vad.process_frame(_speech_frame())
            # V1 fail-closes to probability=0.0 on corruption.
            assert evt.probability == 0.0
        assert vad.corruption_count == 5  # noqa: PLR2004

    def test_chaos_injects_via_same_v1_path_as_real_corruption(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Chaos NaN injection fires the same M2 DROP event
        (error_type=probability_nan) as a real ONNX NaN output."""
        from sovyx.voice import vad as vad_mod
        from sovyx.voice._chaos import _ENABLED_ENV_VAR, _RATE_ENV_VAR_PREFIX
        from sovyx.voice._stage_metrics import StageEventKind, VoiceStage

        recorded: list[tuple[Any, Any, Any]] = []

        def _capture(stage: Any, kind: Any, *, error_type: str | None = None) -> None:
            recorded.append((stage, kind, error_type))

        monkeypatch.setattr(vad_mod, "record_stage_event", _capture)
        monkeypatch.setenv(_ENABLED_ENV_VAR, "true")
        monkeypatch.setenv(f"{_RATE_ENV_VAR_PREFIX}VAD_CORRUPTION_PCT", "100")

        vad = _build_vad([0.5])
        vad.process_frame(_speech_frame())

        assert (VoiceStage.VAD, StageEventKind.DROP, "probability_nan") in recorded


# ---------------------------------------------------------------------------
# Band-aid #36 — startup smoke probe validation
# ---------------------------------------------------------------------------


class TestSmokeProbeAtConstruction:
    """Band-aid #36: VAD construction validates the loaded ONNX
    model agrees with the configured sample_rate + window_size.
    Failure modes (rate mismatch, window mismatch, corrupt model)
    raise RuntimeError at construction instead of degrading silently
    via the V1 fail-closed-to-silence path on every real frame."""

    def test_smoke_probe_passes_on_healthy_session(self) -> None:
        """Healthy session returning sane probability → construction
        succeeds, no exception."""
        cfg = VADConfig()
        # Mock returns 0.0 (silence-baseline probability — what a
        # healthy session would return for a zero-input probe).
        mock_session = _make_mock_session([0.0])
        mock_ort = MagicMock()
        mock_ort.SessionOptions.return_value = MagicMock()
        mock_ort.GraphOptimizationLevel.ORT_ENABLE_ALL = 99
        mock_ort.InferenceSession.return_value = mock_session
        with patch.dict("sys.modules", {"onnxruntime": mock_ort}):
            vad = SileroVAD(
                Path("/fake/model.onnx"),
                config=cfg,
                # Smoke probe ENABLED (the production default).
                smoke_probe_at_construction=True,
            )
        # Construction succeeded; VAD is usable.
        assert vad.state == VADState.SILENCE

    def test_smoke_probe_raises_on_nan_probability(self) -> None:
        """NaN output → RuntimeError with corruption_kind in message."""
        cfg = VADConfig()
        mock_session = _make_corruptable_session([(float("nan"), None)])
        mock_ort = MagicMock()
        mock_ort.SessionOptions.return_value = MagicMock()
        mock_ort.GraphOptimizationLevel.ORT_ENABLE_ALL = 99
        mock_ort.InferenceSession.return_value = mock_session
        with patch.dict("sys.modules", {"onnxruntime": mock_ort}):  # noqa: SIM117
            with pytest.raises(RuntimeError, match="smoke probe.*probability_nan"):
                SileroVAD(
                    Path("/fake/model.onnx"),
                    config=cfg,
                    smoke_probe_at_construction=True,
                )

    def test_smoke_probe_raises_on_out_of_range_probability(self) -> None:
        """Probability above 1.0 → RuntimeError. Real Silero
        probabilities are always in [0, 1]; out-of-range output
        means the model isn't a real Silero VAD."""
        cfg = VADConfig()
        mock_session = _make_corruptable_session([(99.0, None)])
        mock_ort = MagicMock()
        mock_ort.SessionOptions.return_value = MagicMock()
        mock_ort.GraphOptimizationLevel.ORT_ENABLE_ALL = 99
        mock_ort.InferenceSession.return_value = mock_session
        with patch.dict("sys.modules", {"onnxruntime": mock_ort}):  # noqa: SIM117
            with pytest.raises(RuntimeError, match="probability_out_of_range"):
                SileroVAD(
                    Path("/fake/model.onnx"),
                    config=cfg,
                    smoke_probe_at_construction=True,
                )

    def test_smoke_probe_raises_on_session_run_exception(self) -> None:
        """ONNX session.run raising → translated to RuntimeError
        with operator-actionable context."""
        cfg = VADConfig()
        mock_session = MagicMock()
        # session.run raises — simulates the window-size mismatch
        # case where ONNX's InvalidArgument fires.
        mock_session.run = MagicMock(side_effect=RuntimeError("InvalidArgument: shape mismatch"))
        mock_ort = MagicMock()
        mock_ort.SessionOptions.return_value = MagicMock()
        mock_ort.GraphOptimizationLevel.ORT_ENABLE_ALL = 99
        mock_ort.InferenceSession.return_value = mock_session
        with patch.dict("sys.modules", {"onnxruntime": mock_ort}):  # noqa: SIM117
            with pytest.raises(RuntimeError, match="smoke probe failed"):
                SileroVAD(
                    Path("/fake/model.onnx"),
                    config=cfg,
                    smoke_probe_at_construction=True,
                )

    def test_smoke_probe_disabled_skips_validation(self) -> None:
        """smoke_probe_at_construction=False bypasses the probe.
        Used by the test suite to feed deterministic probability
        sequences into the mock session without the probe consuming
        the first element."""
        cfg = VADConfig()
        # Even with a NaN-returning mock, construction succeeds when
        # the probe is disabled (the V1 runtime guard would catch
        # the NaN on the first real process_frame call instead).
        mock_session = _make_corruptable_session([(float("nan"), None)])
        mock_ort = MagicMock()
        mock_ort.SessionOptions.return_value = MagicMock()
        mock_ort.GraphOptimizationLevel.ORT_ENABLE_ALL = 99
        mock_ort.InferenceSession.return_value = mock_session
        with patch.dict("sys.modules", {"onnxruntime": mock_ort}):
            # Should not raise.
            vad = SileroVAD(
                Path("/fake/model.onnx"),
                config=cfg,
                smoke_probe_at_construction=False,
            )
        assert vad.state == VADState.SILENCE
