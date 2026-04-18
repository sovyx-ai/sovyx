"""Tests for SileroVAD v5 — onset/offset FSM, hysteresis, 512-sample window (V05-17).

Strategy: mock ONNX session to control probabilities, verify FSM transitions.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from structlog.testing import capture_logs

from sovyx.voice.vad import (
    SileroVAD,
    VADConfig,
    VADEvent,
    VADState,
    _validate_config,
)

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
    """Construct a SileroVAD with mocked ONNX session."""
    cfg = config or VADConfig()
    mock_session = _make_mock_session(probabilities)

    mock_ort = MagicMock()
    mock_ort.SessionOptions.return_value = MagicMock()
    mock_ort.GraphOptimizationLevel.ORT_ENABLE_ALL = 99
    mock_ort.InferenceSession.return_value = mock_session

    with patch.dict("sys.modules", {"onnxruntime": mock_ort}):
        vad = SileroVAD(Path("/fake/model.onnx"), config=cfg)

    return vad


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

    def test_no_log_when_state_unchanged(self) -> None:
        vad = _build_vad([0.1, 0.1, 0.1])
        with capture_logs() as logs:
            for _ in range(3):
                vad.process_frame(_silence_frame())
        transitions = [log for log in logs if log.get("event") == "vad_state_transition"]
        assert transitions == []

    def test_logs_silence_to_onset(self) -> None:
        vad = _build_vad([0.9])
        with capture_logs() as logs:
            vad.process_frame(_speech_frame())
        transitions = [log for log in logs if log.get("event") == "vad_state_transition"]
        assert len(transitions) == 1
        assert transitions[0]["from_state"] == "SILENCE"
        assert transitions[0]["to_state"] == "SPEECH_ONSET"
        assert transitions[0]["probability"] == 0.9  # noqa: PLR2004

    def test_logs_full_onset_to_speech_sequence(self) -> None:
        vad = _build_vad([0.9, 0.9, 0.9])
        with capture_logs() as logs:
            for _ in range(3):
                vad.process_frame(_speech_frame())
        transitions = [
            (log["from_state"], log["to_state"])
            for log in logs
            if log.get("event") == "vad_state_transition"
        ]
        assert transitions == [
            ("SILENCE", "SPEECH_ONSET"),
            ("SPEECH_ONSET", "SPEECH"),
        ]
