"""Tests for WakeWordDetector — OpenWakeWord 2-stage verify (V05-18).

Strategy: mock ONNX session to control scores, verify FSM transitions
and 2-stage verification flow.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from sovyx.voice.wake_word import (
    VerificationResult,
    WakeWordConfig,
    WakeWordDetector,
    WakeWordEvent,
    WakeWordState,
    _validate_config,
    create_stt_verifier,
    default_verifier,
)

# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------

_FRAME = 1280  # 80ms at 16kHz


def _mock_onnx_session(scores: list[float]) -> MagicMock:
    """Create a mock ONNX InferenceSession that returns scores in order."""
    session = MagicMock()
    session.get_inputs.return_value = [MagicMock(name="input")]

    score_iter = iter(scores)

    def _run(_names: object, inputs: dict[str, object]) -> list[np.ndarray]:  # noqa: ARG001
        try:
            score = next(score_iter)
        except StopIteration:
            score = 0.0
        return [np.array([[score]], dtype=np.float32)]

    session.run.side_effect = _run
    return session


def _verified_true(
    audio: np.ndarray,  # noqa: ARG001
) -> VerificationResult:
    return VerificationResult(verified=True, transcription="hey sovyx")


def _verified_sovyx(
    audio: np.ndarray,  # noqa: ARG001
) -> VerificationResult:
    return VerificationResult(verified=True, transcription="sovyx")


def _verified_false(
    audio: np.ndarray,  # noqa: ARG001
) -> VerificationResult:
    return VerificationResult(verified=False, transcription="hello world")


def _make_detector(
    scores: list[float],
    config: WakeWordConfig | None = None,
    verifier: object = None,
) -> WakeWordDetector:
    """Create a WakeWordDetector with mocked ONNX session."""
    mock_session = _mock_onnx_session(scores)

    mock_ort = MagicMock()
    mock_ort.SessionOptions.return_value = MagicMock()
    mock_ort.GraphOptimizationLevel.ORT_ENABLE_ALL = 99
    mock_ort.InferenceSession.return_value = mock_session

    with patch.dict("sys.modules", {"onnxruntime": mock_ort}):
        detector = WakeWordDetector(
            model_path=Path("/fake/model.onnx"),
            config=config,
            verifier=verifier,  # type: ignore[arg-type]
        )
    return detector


def _frame(dtype: str = "float32") -> np.ndarray:
    """Create a silent audio frame."""
    if dtype == "int16":
        return np.zeros(_FRAME, dtype=np.int16)
    return np.zeros(_FRAME, dtype=np.float32)


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


class TestWakeWordConfig:
    """Tests for WakeWordConfig validation."""

    def test_default_config_valid(self) -> None:
        config = WakeWordConfig()
        _validate_config(config)
        assert config.stage1_threshold == 0.5
        assert config.stage2_threshold == 0.7
        assert config.stage2_window_seconds == 1.5
        assert config.cooldown_seconds == 2.0
        assert config.sample_rate == 16000

    def test_frame_samples(self) -> None:
        config = WakeWordConfig()
        assert config.frame_samples == 1280

    def test_stage2_window_frames(self) -> None:
        config = WakeWordConfig(stage2_window_seconds=1.6)
        # 1.6s * 16000 / 1280 = 20 frames
        assert config.stage2_window_frames == 20

    def test_cooldown_frames(self) -> None:
        config = WakeWordConfig(cooldown_seconds=2.0)
        # 2.0s * 16000 / 1280 = 25 frames
        assert config.cooldown_frames == 25

    @pytest.mark.parametrize("threshold", [0.0, 1.0, -0.5, 1.5])
    def test_invalid_stage1_threshold(self, threshold: float) -> None:
        config = WakeWordConfig(stage1_threshold=threshold)
        with pytest.raises(ValueError, match="stage1_threshold"):
            _validate_config(config)

    @pytest.mark.parametrize("threshold", [0.0, 1.0, -0.5, 1.5])
    def test_invalid_stage2_threshold(self, threshold: float) -> None:
        config = WakeWordConfig(stage2_threshold=threshold)
        with pytest.raises(ValueError, match="stage2_threshold"):
            _validate_config(config)

    def test_stage2_less_than_stage1_raises(self) -> None:
        config = WakeWordConfig(stage1_threshold=0.7, stage2_threshold=0.5)
        with pytest.raises(ValueError, match="stage2_threshold.*must be >="):
            _validate_config(config)

    def test_stage2_equal_to_stage1_valid(self) -> None:
        config = WakeWordConfig(stage1_threshold=0.5, stage2_threshold=0.5)
        _validate_config(config)  # Should not raise

    def test_invalid_window_seconds(self) -> None:
        config = WakeWordConfig(stage2_window_seconds=0)
        with pytest.raises(ValueError, match="stage2_window_seconds"):
            _validate_config(config)

    def test_negative_cooldown(self) -> None:
        config = WakeWordConfig(cooldown_seconds=-1)
        with pytest.raises(ValueError, match="cooldown_seconds"):
            _validate_config(config)

    def test_zero_cooldown_valid(self) -> None:
        config = WakeWordConfig(cooldown_seconds=0)
        _validate_config(config)  # Should not raise

    def test_invalid_sample_rate(self) -> None:
        config = WakeWordConfig(sample_rate=44100)
        with pytest.raises(ValueError, match="Only 16000"):
            _validate_config(config)

    def test_wake_variants_default(self) -> None:
        config = WakeWordConfig()
        assert "sovyx" in config.wake_variants
        assert "hey sovyx" in config.wake_variants


# ---------------------------------------------------------------------------
# State machine: IDLE
# ---------------------------------------------------------------------------


class TestIdleState:
    """Tests for IDLE state behaviour."""

    def test_initial_state_is_idle(self) -> None:
        detector = _make_detector([0.0])
        assert detector.state == WakeWordState.IDLE

    def test_low_score_stays_idle(self) -> None:
        detector = _make_detector([0.1, 0.2, 0.1])
        for _ in range(3):
            event = detector.process_frame(_frame())
            assert not event.detected
            assert event.state == WakeWordState.IDLE

    def test_above_threshold_triggers_stage1(self) -> None:
        detector = _make_detector([0.6])
        event = detector.process_frame(_frame())
        assert not event.detected
        assert event.state == WakeWordState.STAGE1_TRIGGERED

    def test_score_exactly_at_threshold(self) -> None:
        config = WakeWordConfig(stage1_threshold=0.5, stage2_threshold=0.5)
        detector = _make_detector([0.5], config=config)
        event = detector.process_frame(_frame())
        assert event.state == WakeWordState.STAGE1_TRIGGERED

    def test_score_below_threshold(self) -> None:
        config = WakeWordConfig(stage1_threshold=0.5, stage2_threshold=0.5)
        detector = _make_detector([0.49], config=config)
        event = detector.process_frame(_frame())
        assert event.state == WakeWordState.IDLE


# ---------------------------------------------------------------------------
# State machine: STAGE1_TRIGGERED → verified detection
# ---------------------------------------------------------------------------


class TestStage1ToDetection:
    """Tests for STAGE1_TRIGGERED state and full 2-stage verification."""

    def test_full_detection_cycle(self) -> None:
        """Stage-1 trigger + high peak + STT verification → detected."""
        config = WakeWordConfig(
            stage1_threshold=0.5,
            stage2_threshold=0.7,
            stage2_window_seconds=3 * 1280 / 16000,
        )
        scores = [0.6, 0.8, 0.75]
        detector = _make_detector(scores, config=config, verifier=_verified_true)

        e1 = detector.process_frame(_frame())
        assert not e1.detected
        assert e1.state == WakeWordState.STAGE1_TRIGGERED

        e2 = detector.process_frame(_frame())
        assert not e2.detected
        assert e2.state == WakeWordState.STAGE1_TRIGGERED

        e3 = detector.process_frame(_frame())
        assert e3.detected
        assert e3.state == WakeWordState.COOLDOWN

    def test_stage2_threshold_not_met(self) -> None:
        """Peak score below stage2_threshold → not detected, back to IDLE."""
        config = WakeWordConfig(
            stage1_threshold=0.5,
            stage2_threshold=0.7,
            stage2_window_seconds=2 * 1280 / 16000,
        )
        scores = [0.6, 0.55]
        detector = _make_detector(scores, config=config)

        e1 = detector.process_frame(_frame())
        assert e1.state == WakeWordState.STAGE1_TRIGGERED

        e2 = detector.process_frame(_frame())
        assert not e2.detected
        assert e2.state == WakeWordState.IDLE

    def test_stage2_verifier_rejects(self) -> None:
        """Peak meets threshold but STT verification fails → not detected."""
        config = WakeWordConfig(
            stage1_threshold=0.5,
            stage2_threshold=0.7,
            stage2_window_seconds=2 * 1280 / 16000,
        )
        scores = [0.6, 0.8]
        detector = _make_detector(scores, config=config, verifier=_verified_false)

        detector.process_frame(_frame())
        e2 = detector.process_frame(_frame())
        assert not e2.detected
        assert e2.state == WakeWordState.IDLE

    def test_peak_score_tracks_maximum(self) -> None:
        """Peak score should be max across all frames in window."""
        config = WakeWordConfig(
            stage1_threshold=0.5,
            stage2_threshold=0.9,
            stage2_window_seconds=4 * 1280 / 16000,
        )
        scores = [0.6, 0.7, 0.95, 0.8]
        detector = _make_detector(scores, config=config, verifier=_verified_true)

        for _ in range(3):
            detector.process_frame(_frame())

        e4 = detector.process_frame(_frame())
        assert e4.detected  # Peak 0.95 >= 0.9

    def test_audio_buffer_concatenated_for_verifier(self) -> None:
        """Verifier receives concatenated audio from all frames in window."""
        config = WakeWordConfig(
            stage1_threshold=0.5,
            stage2_threshold=0.7,
            stage2_window_seconds=3 * 1280 / 16000,
        )
        scores = [0.6, 0.8, 0.75]
        received_audio: list[np.ndarray] = []

        def capture_verifier(audio: np.ndarray) -> VerificationResult:
            received_audio.append(audio)
            return VerificationResult(verified=True, transcription="hey sovyx")

        detector = _make_detector(scores, config=config, verifier=capture_verifier)

        for _ in range(3):
            detector.process_frame(_frame())

        assert len(received_audio) == 1
        assert received_audio[0].shape == (3 * _FRAME,)

    def test_int16_input_normalised(self) -> None:
        """int16 frames should be normalised before processing."""
        config = WakeWordConfig(
            stage1_threshold=0.5,
            stage2_threshold=0.5,
            stage2_window_seconds=1280 / 16000,
        )
        detector = _make_detector([0.8], config=config, verifier=_verified_true)
        e = detector.process_frame(_frame("int16"))
        assert e.detected


# ---------------------------------------------------------------------------
# State machine: COOLDOWN
# ---------------------------------------------------------------------------


class TestCooldownState:
    """Tests for COOLDOWN state behaviour."""

    def test_cooldown_ignores_high_scores(self) -> None:
        """During cooldown, high scores are ignored."""
        # cooldown_frames = 4. After detection, 4 frames of cooldown.
        # Frames where counter < cooldown_frames remain COOLDOWN,
        # the frame where counter >= cooldown_frames transitions to IDLE.
        config = WakeWordConfig(
            stage1_threshold=0.5,
            stage2_threshold=0.5,
            stage2_window_seconds=1280 / 16000,
            cooldown_seconds=4 * 1280 / 16000,
        )
        # Frame 1: detected. Frames 2-4: COOLDOWN (counters 1-3).
        # Frame 5: counter=4 → IDLE.
        scores = [0.8, 0.9, 0.9, 0.9, 0.1]
        detector = _make_detector(scores, config=config, verifier=_verified_true)

        e1 = detector.process_frame(_frame())
        assert e1.detected
        assert e1.state == WakeWordState.COOLDOWN

        # 3 frames stay in COOLDOWN
        for _ in range(3):
            e = detector.process_frame(_frame())
            assert not e.detected
            assert e.state == WakeWordState.COOLDOWN

        # 4th cooldown frame transitions to IDLE
        e5 = detector.process_frame(_frame())
        assert not e5.detected
        assert e5.state == WakeWordState.IDLE

    def test_zero_cooldown(self) -> None:
        """With cooldown=0, immediately goes back to IDLE."""
        config = WakeWordConfig(
            stage1_threshold=0.5,
            stage2_threshold=0.5,
            stage2_window_seconds=1280 / 16000,
            cooldown_seconds=0,
        )
        detector = _make_detector([0.8, 0.1], config=config, verifier=_verified_true)

        e1 = detector.process_frame(_frame())
        assert e1.detected
        e2 = detector.process_frame(_frame())
        assert e2.state == WakeWordState.IDLE


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------


class TestReset:
    """Tests for reset behaviour."""

    def test_reset_clears_state(self) -> None:
        config = WakeWordConfig(
            stage1_threshold=0.5,
            stage2_threshold=0.5,
            stage2_window_seconds=3 * 1280 / 16000,
        )
        detector = _make_detector([0.8, 0.0], config=config)

        detector.process_frame(_frame())
        assert detector.state == WakeWordState.STAGE1_TRIGGERED

        detector.reset()
        assert detector.state == WakeWordState.IDLE

    def test_reset_after_detection_allows_redetection(self) -> None:
        config = WakeWordConfig(
            stage1_threshold=0.5,
            stage2_threshold=0.5,
            stage2_window_seconds=1280 / 16000,
            cooldown_seconds=100,
        )
        detector = _make_detector([0.8, 0.8], config=config, verifier=_verified_true)

        e1 = detector.process_frame(_frame())
        assert e1.detected

        detector.reset()
        assert detector.state == WakeWordState.IDLE


# ---------------------------------------------------------------------------
# Verifier helpers
# ---------------------------------------------------------------------------


def _transcribe_sovyx(audio: np.ndarray) -> str:  # noqa: ARG001
    return "i said hey sovyx please"


def _transcribe_hello(audio: np.ndarray) -> str:  # noqa: ARG001
    return "hello world"


def _transcribe_uppercase(audio: np.ndarray) -> str:  # noqa: ARG001
    return "Hey SOVYX!"


def _transcribe_variant(audio: np.ndarray) -> str:  # noqa: ARG001
    return "i heard so vyx"


class TestVerifiers:
    """Tests for verifier factory functions."""

    def test_default_verifier_always_true(self) -> None:
        verifier = default_verifier(frozenset({"sovyx"}))
        result = verifier(np.zeros(1280, dtype=np.float32))
        assert result.verified is True
        assert result.transcription == "<no-stt>"

    def test_stt_verifier_matches(self) -> None:
        verifier = create_stt_verifier(_transcribe_sovyx, frozenset({"sovyx", "hey sovyx"}))
        result = verifier(np.zeros(1280, dtype=np.float32))
        assert result.verified is True
        assert "hey sovyx" in result.transcription

    def test_stt_verifier_no_match(self) -> None:
        verifier = create_stt_verifier(_transcribe_hello, frozenset({"sovyx", "hey sovyx"}))
        result = verifier(np.zeros(1280, dtype=np.float32))
        assert result.verified is False

    def test_stt_verifier_case_insensitive(self) -> None:
        verifier = create_stt_verifier(_transcribe_uppercase, frozenset({"sovyx"}))
        result = verifier(np.zeros(1280, dtype=np.float32))
        assert result.verified is True

    def test_stt_verifier_variant_matching(self) -> None:
        verifier = create_stt_verifier(_transcribe_variant, frozenset({"sovyx", "so vyx"}))
        result = verifier(np.zeros(1280, dtype=np.float32))
        assert result.verified is True


# ---------------------------------------------------------------------------
# Frame validation
# ---------------------------------------------------------------------------


class TestFrameValidation:
    """Tests for input validation."""

    def test_wrong_frame_size_raises(self) -> None:
        detector = _make_detector([0.0])
        with pytest.raises(ValueError, match="Expected frame of 1280"):
            detector.process_frame(np.zeros(512, dtype=np.float32))

    def test_empty_frame_raises(self) -> None:
        detector = _make_detector([0.0])
        with pytest.raises(ValueError, match="Expected frame of 1280"):
            detector.process_frame(np.zeros(0, dtype=np.float32))

    def test_2d_frame_raises(self) -> None:
        detector = _make_detector([0.0])
        with pytest.raises(ValueError, match="Expected frame of 1280"):
            detector.process_frame(np.zeros((1, 1280), dtype=np.float32))


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


class TestProperties:
    """Tests for public properties."""

    def test_config_property(self) -> None:
        config = WakeWordConfig(stage1_threshold=0.6, stage2_threshold=0.8)
        detector = _make_detector([0.0], config=config)
        assert detector.config.stage1_threshold == 0.6
        assert detector.config.stage2_threshold == 0.8

    def test_state_property_reflects_fsm(self) -> None:
        detector = _make_detector([0.8, 0.0])
        assert detector.state == WakeWordState.IDLE
        detector.process_frame(_frame())
        assert detector.state == WakeWordState.STAGE1_TRIGGERED


# ---------------------------------------------------------------------------
# WakeWordEvent
# ---------------------------------------------------------------------------


class TestWakeWordEvent:
    """Tests for WakeWordEvent dataclass."""

    def test_event_fields(self) -> None:
        event = WakeWordEvent(detected=True, score=0.85, state=WakeWordState.COOLDOWN)
        assert event.detected is True
        assert event.score == 0.85
        assert event.state == WakeWordState.COOLDOWN

    def test_event_is_frozen(self) -> None:
        event = WakeWordEvent(detected=False, score=0.1, state=WakeWordState.IDLE)
        with pytest.raises(AttributeError):
            event.detected = True  # type: ignore[misc]


# ---------------------------------------------------------------------------
# VerificationResult
# ---------------------------------------------------------------------------


class TestVerificationResult:
    """Tests for VerificationResult dataclass."""

    def test_fields(self) -> None:
        result = VerificationResult(verified=True, transcription="hey sovyx")
        assert result.verified is True
        assert result.transcription == "hey sovyx"

    def test_frozen(self) -> None:
        result = VerificationResult(verified=True, transcription="test")
        with pytest.raises(AttributeError):
            result.verified = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge cases and boundary conditions."""

    def test_multiple_detections_separated_by_cooldown(self) -> None:
        """After cooldown, detector can detect again."""
        # cooldown_frames=3, window=1 frame.
        # Frame 1: detect → COOLDOWN. Frames 2-3: COOLDOWN (counters 1-2).
        # Frame 4: counter=3 → IDLE. Frame 5: new trigger → detect again.
        config = WakeWordConfig(
            stage1_threshold=0.5,
            stage2_threshold=0.5,
            stage2_window_seconds=1280 / 16000,
            cooldown_seconds=3 * 1280 / 16000,
        )
        scores = [0.8, 0.1, 0.1, 0.1, 0.8]
        detector = _make_detector(scores, config=config, verifier=_verified_true)

        e1 = detector.process_frame(_frame())
        assert e1.detected

        # 2 frames stay COOLDOWN
        for _ in range(2):
            e = detector.process_frame(_frame())
            assert not e.detected
            assert e.state == WakeWordState.COOLDOWN

        # Frame 4: cooldown expires → IDLE
        e4 = detector.process_frame(_frame())
        assert not e4.detected
        assert e4.state == WakeWordState.IDLE

        # Frame 5: new detection (window=1 → immediate)
        e5 = detector.process_frame(_frame())
        assert e5.detected

    def test_stage1_window_single_frame(self) -> None:
        """When window is 1 frame, detection happens on first trigger frame."""
        config = WakeWordConfig(
            stage1_threshold=0.5,
            stage2_threshold=0.5,
            stage2_window_seconds=1280 / 16000,
        )
        detector = _make_detector([0.8], config=config, verifier=_verified_sovyx)

        e = detector.process_frame(_frame())
        assert e.detected

    @settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(score=st.floats(min_value=0.0, max_value=0.49))
    def test_below_threshold_never_triggers(self, score: float) -> None:
        """Any score below stage1_threshold never leaves IDLE."""
        detector = _make_detector([score])
        event = detector.process_frame(_frame())
        assert not event.detected
        assert event.state == WakeWordState.IDLE

    @settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(score=st.floats(min_value=0.5, max_value=0.99, exclude_max=True))
    def test_above_threshold_always_triggers_stage1(self, score: float) -> None:
        """Any score >= stage1_threshold but < high-confidence triggers STAGE1.

        T7.4 added a fast-path branch that bypasses STAGE1_TRIGGERED
        when ``score >= stage1_high_confidence_threshold`` (default
        1.0 = disabled). Hypothesis can generate values arbitrarily
        close to 1.0 that match the >= 1.0 comparison via float
        rounding, which would now correctly engage the fast path
        instead of the 2-stage path. Bound the search to < 0.99 so
        the property test continues to pin the 2-stage branch
        invariant; the fast-path branch has its own dedicated tests
        in ``TestFastPathT74``.
        """
        config = WakeWordConfig(
            stage1_threshold=0.5,
            stage2_threshold=0.5,
            stage2_window_seconds=5 * 1280 / 16000,
        )
        detector = _make_detector([score], config=config)
        event = detector.process_frame(_frame())
        assert event.state == WakeWordState.STAGE1_TRIGGERED

    def test_score_returned_in_event(self) -> None:
        detector = _make_detector([0.42])
        event = detector.process_frame(_frame())
        assert abs(event.score - 0.42) < 1e-6

    def test_continuous_low_scores(self) -> None:
        """Many consecutive low-score frames stay IDLE."""
        scores = [0.1] * 100
        detector = _make_detector(scores)
        for _ in range(100):
            e = detector.process_frame(_frame())
            assert e.state == WakeWordState.IDLE
            assert not e.detected


# ---------------------------------------------------------------------------
# T7.1 — Wake-word latency profile (Phase 7 instrumentation)
# ---------------------------------------------------------------------------


class TestLatencyProfileT71:
    """Pin the T7.1 latency-histogram instrumentation.

    Phase 7 / T7.1 adds 4 OTel histograms decomposing the wake-word
    detection latency:

    - ``stage1_inference_latency`` — per-frame ONNX inference (every
      ``process_frame`` call).
    - ``stage2_collection_latency`` — STAGE1_TRIGGERED entry to
      _evaluate_stage2 wall-clock (every evaluation).
    - ``stage2_verifier_latency`` — verifier callable duration (only
      when ``peak_score >= stage2_threshold``).
    - ``detection_latency`` — end-to-end stage-1-trigger to confirmed
      detection (only on ``verified=True``).

    Each test patches the corresponding record helper and asserts
    the emission contract: WHEN it fires, what attributes carry,
    and that it doesn't fire on the wrong path.
    """

    def test_stage1_inference_recorded_every_frame(self) -> None:
        """``record_wake_word_stage1_inference_ms`` fires once per frame."""
        scores = [0.1, 0.2, 0.3]
        detector = _make_detector(scores)
        with patch(
            "sovyx.voice.health._metrics.record_wake_word_stage1_inference_ms",
        ) as mock_record:
            for _ in range(3):
                detector.process_frame(_frame())
        assert mock_record.call_count == 3  # noqa: PLR2004
        # Every call carries the model_name attribute.
        for call in mock_record.call_args_list:
            assert "model_name" in call.kwargs
            assert call.kwargs["model_name"] == "model"
            assert call.kwargs["duration_ms"] >= 0.0

    def test_stage2_collection_outcome_confirmed(self) -> None:
        """When verifier returns True, collection outcome is ``confirmed``."""
        config = WakeWordConfig(
            stage1_threshold=0.5,
            stage2_threshold=0.5,
            stage2_window_seconds=1280 / 16000,  # 1-frame window
        )
        detector = _make_detector([0.9], config=config, verifier=_verified_true)
        with patch(
            "sovyx.voice.health._metrics.record_wake_word_stage2_collection_ms",
        ) as mock_record:
            event = detector.process_frame(_frame())
        assert event.detected
        mock_record.assert_called_once()
        assert mock_record.call_args.kwargs["outcome"] == "confirmed"

    def test_stage2_collection_outcome_rejected_threshold(self) -> None:
        """When peak_score < stage2_threshold, outcome is ``rejected_threshold``."""
        config = WakeWordConfig(
            stage1_threshold=0.5,
            stage2_threshold=0.9,  # high bar — won't be met
            stage2_window_seconds=1280 / 16000,
        )
        detector = _make_detector([0.6], config=config, verifier=_verified_true)
        with patch(
            "sovyx.voice.health._metrics.record_wake_word_stage2_collection_ms",
        ) as mock_record:
            event = detector.process_frame(_frame())
        assert not event.detected
        mock_record.assert_called_once()
        assert mock_record.call_args.kwargs["outcome"] == "rejected_threshold"

    def test_stage2_collection_outcome_rejected_verifier(self) -> None:
        """When verifier returns False, outcome is ``rejected_verifier``."""
        config = WakeWordConfig(
            stage1_threshold=0.5,
            stage2_threshold=0.5,
            stage2_window_seconds=1280 / 16000,
        )
        detector = _make_detector([0.9], config=config, verifier=_verified_false)
        with patch(
            "sovyx.voice.health._metrics.record_wake_word_stage2_collection_ms",
        ) as mock_record:
            event = detector.process_frame(_frame())
        assert not event.detected
        mock_record.assert_called_once()
        assert mock_record.call_args.kwargs["outcome"] == "rejected_verifier"

    def test_stage2_verifier_only_fires_when_threshold_crossed(self) -> None:
        """Verifier histogram is gated on peak_score crossing threshold."""
        # Peak below threshold — verifier never runs, no record call.
        config = WakeWordConfig(
            stage1_threshold=0.5,
            stage2_threshold=0.9,
            stage2_window_seconds=1280 / 16000,
        )
        detector_low = _make_detector([0.6], config=config, verifier=_verified_true)
        with patch(
            "sovyx.voice.health._metrics.record_wake_word_stage2_verifier_ms",
        ) as mock_record:
            detector_low.process_frame(_frame())
        assert mock_record.call_count == 0

        # Peak above threshold — verifier runs, record fires.
        config2 = WakeWordConfig(
            stage1_threshold=0.5,
            stage2_threshold=0.5,
            stage2_window_seconds=1280 / 16000,
        )
        detector_high = _make_detector([0.9], config=config2, verifier=_verified_true)
        with patch(
            "sovyx.voice.health._metrics.record_wake_word_stage2_verifier_ms",
        ) as mock_record:
            detector_high.process_frame(_frame())
        assert mock_record.call_count == 1
        assert mock_record.call_args.kwargs["outcome"] == "verified"

    def test_detection_latency_only_on_confirmed(self) -> None:
        """``record_wake_word_detection_ms`` fires only on verified=True."""
        config = WakeWordConfig(
            stage1_threshold=0.5,
            stage2_threshold=0.5,
            stage2_window_seconds=1280 / 16000,
        )

        # Path 1: confirmed → record_wake_word_detection_ms fires.
        detector_ok = _make_detector([0.9], config=config, verifier=_verified_true)
        with patch(
            "sovyx.voice.health._metrics.record_wake_word_detection_ms",
        ) as mock_record:
            event = detector_ok.process_frame(_frame())
        assert event.detected
        assert mock_record.call_count == 1

        # Path 2: verifier rejects → record does NOT fire.
        detector_rej = _make_detector([0.9], config=config, verifier=_verified_false)
        with patch(
            "sovyx.voice.health._metrics.record_wake_word_detection_ms",
        ) as mock_record:
            event = detector_rej.process_frame(_frame())
        assert not event.detected
        assert mock_record.call_count == 0

        # Path 3: peak below threshold → record does NOT fire.
        config_high = WakeWordConfig(
            stage1_threshold=0.5,
            stage2_threshold=0.9,
            stage2_window_seconds=1280 / 16000,
        )
        detector_below = _make_detector(
            [0.6],
            config=config_high,
            verifier=_verified_true,
        )
        with patch(
            "sovyx.voice.health._metrics.record_wake_word_detection_ms",
        ) as mock_record:
            event = detector_below.process_frame(_frame())
        assert not event.detected
        assert mock_record.call_count == 0

    def test_collection_latency_anchored_at_stage1_trigger(self) -> None:
        """Multi-frame window: collection_ms reflects time across all frames."""
        # 3-frame window — stage-1 triggers on frame 1, evaluation on frame 3.
        config = WakeWordConfig(
            stage1_threshold=0.5,
            stage2_threshold=0.5,
            stage2_window_seconds=3 * 1280 / 16000,
        )
        # All 3 frames score above threshold so peak is consistently high.
        detector = _make_detector([0.9, 0.9, 0.9], config=config, verifier=_verified_true)
        with patch(
            "sovyx.voice.health._metrics.record_wake_word_stage2_collection_ms",
        ) as mock_record:
            for _ in range(3):
                detector.process_frame(_frame())
        # Exactly one collection record (one stage-2 evaluation).
        mock_record.assert_called_once()
        # Duration is non-negative (real time elapsed across the 3 frames).
        assert mock_record.call_args.kwargs["duration_ms"] >= 0.0

    def test_fast_path_t74_engages_on_high_score(self) -> None:
        """T7.4 fast-path: score >= stage1_high_confidence_threshold.

        With the threshold set to 0.8 and an incoming score of 0.9,
        the detector skips stage-2 entirely and confirms on the
        first frame. End-to-end latency is essentially the single
        ONNX inference.
        """
        config = WakeWordConfig(
            stage1_threshold=0.5,
            stage2_threshold=0.7,
            stage1_high_confidence_threshold=0.8,
            stage2_window_seconds=5 * 1280 / 16000,
        )
        detector = _make_detector([0.9], config=config, verifier=_verified_false)
        event = detector.process_frame(_frame())
        # Fast-path fired despite a verifier that would have rejected.
        assert event.detected
        # State machine went straight to COOLDOWN, bypassing
        # STAGE1_TRIGGERED + the STT verifier.
        assert event.state == WakeWordState.COOLDOWN

    def test_fast_path_t74_records_engaged_counter(self) -> None:
        """Fast-path increments ``record_wake_word_fast_path_engaged``."""
        config = WakeWordConfig(
            stage1_threshold=0.5,
            stage2_threshold=0.7,
            stage1_high_confidence_threshold=0.8,
            stage2_window_seconds=5 * 1280 / 16000,
        )
        detector = _make_detector([0.92], config=config, verifier=_verified_true)
        with patch(
            "sovyx.voice.health._metrics.record_wake_word_fast_path_engaged",
        ) as mock_record:
            detector.process_frame(_frame())
        mock_record.assert_called_once()
        # Score is forwarded for bucketing.
        assert mock_record.call_args.kwargs["score"] == pytest.approx(0.92, abs=1e-5)

    def test_fast_path_t74_disabled_by_default(self) -> None:
        """Default ``stage1_high_confidence_threshold=1.0`` keeps 2-stage path.

        Operators must explicitly opt-in via WakeWordConfig
        construction; the default behaviour is unchanged from pre-T7.4.
        """
        # Default config — stage1_high_confidence_threshold=1.0 is disabled.
        config = WakeWordConfig(
            stage1_threshold=0.5,
            stage2_threshold=0.5,
            stage2_window_seconds=5 * 1280 / 16000,
        )
        # Score of 0.99 is high but below the disabled 1.0 sentinel.
        detector = _make_detector([0.99], config=config, verifier=_verified_true)
        event = detector.process_frame(_frame())
        # Falls into legacy 2-stage path → STAGE1_TRIGGERED.
        assert event.state == WakeWordState.STAGE1_TRIGGERED

    def test_fast_path_t74_validation_rejects_at_or_below_stage1(self) -> None:
        """``stage1_high_confidence_threshold <= stage1_threshold`` rejected.

        Setting the fast-path threshold at-or-below stage1 would
        defeat the high-confidence contract (every stage-1 trigger
        would take the fast path). Validation enforces strict
        ordering at WakeWordDetector construction time (the dataclass
        itself is frozen but validation lives in
        ``_validate_config`` called from ``__init__``).
        """
        bad_config = WakeWordConfig(
            stage1_threshold=0.5,
            stage2_threshold=0.7,
            stage1_high_confidence_threshold=0.5,  # equal to stage1
        )
        with pytest.raises(ValueError, match="stage1_high_confidence_threshold"):
            _make_detector([0.5], config=bad_config)

    def test_fast_path_t74_validation_rejects_above_one(self) -> None:
        """``stage1_high_confidence_threshold > 1.0`` rejected.

        OpenWakeWord scores live in [0, 1]; a threshold above 1.0
        would be unreachable and confusing. Validation enforces the
        upper bound at detector construction.
        """
        bad_config = WakeWordConfig(
            stage1_threshold=0.5,
            stage2_threshold=0.7,
            stage1_high_confidence_threshold=1.5,
        )
        with pytest.raises(ValueError, match="stage1_high_confidence_threshold"):
            _make_detector([0.5], config=bad_config)

    def test_fast_path_t74_does_not_fire_below_threshold(self) -> None:
        """Score below high-confidence threshold takes 2-stage path."""
        config = WakeWordConfig(
            stage1_threshold=0.5,
            stage2_threshold=0.7,
            stage1_high_confidence_threshold=0.9,
            stage2_window_seconds=5 * 1280 / 16000,
        )
        # Score 0.8 is above stage1 + stage2 thresholds but below the
        # 0.9 high-confidence cutoff → 2-stage path.
        detector = _make_detector([0.8], config=config, verifier=_verified_true)
        with patch(
            "sovyx.voice.health._metrics.record_wake_word_fast_path_engaged",
        ) as mock_record:
            event = detector.process_frame(_frame())
        # 2-stage entry — stage-1 triggered, awaiting collection window.
        assert event.state == WakeWordState.STAGE1_TRIGGERED
        # No fast-path counter increment.
        mock_record.assert_not_called()

    # T7.6 — confidence histogram per CONFIRMED detection

    def test_confidence_t76_recorded_on_two_stage_confirm(self) -> None:
        """``record_wake_word_confidence`` fires on 2-stage confirm.

        2-stage path records the peak score across the collection
        window, labeled ``detection_path=two_stage``.
        """
        config = WakeWordConfig(
            stage1_threshold=0.5,
            stage2_threshold=0.6,
            stage2_window_seconds=1280 / 16000,
        )
        detector = _make_detector([0.78], config=config, verifier=_verified_true)
        with patch(
            "sovyx.voice.health._metrics.record_wake_word_confidence",
        ) as mock_record:
            event = detector.process_frame(_frame())
        assert event.detected
        mock_record.assert_called_once()
        kwargs = mock_record.call_args.kwargs
        assert kwargs["detection_path"] == "two_stage"
        assert kwargs["score"] == pytest.approx(0.78, abs=1e-5)

    def test_confidence_t76_recorded_on_fast_path_confirm(self) -> None:
        """``record_wake_word_confidence`` fires on fast-path confirm.

        Fast-path records the trigger-frame score, labeled
        ``detection_path=fast_path``.
        """
        config = WakeWordConfig(
            stage1_threshold=0.5,
            stage2_threshold=0.7,
            stage1_high_confidence_threshold=0.8,
            stage2_window_seconds=5 * 1280 / 16000,
        )
        detector = _make_detector([0.95], config=config, verifier=_verified_false)
        with patch(
            "sovyx.voice.health._metrics.record_wake_word_confidence",
        ) as mock_record:
            event = detector.process_frame(_frame())
        assert event.detected
        mock_record.assert_called_once()
        kwargs = mock_record.call_args.kwargs
        assert kwargs["detection_path"] == "fast_path"
        assert kwargs["score"] == pytest.approx(0.95, abs=1e-5)

    def test_confidence_t76_does_not_fire_on_rejection(self) -> None:
        """Rejected detections do NOT emit the confidence histogram.

        The histogram is gated on confirmation — both verifier-rejected
        and threshold-rejected paths skip the record call.
        """
        # Path 1: verifier rejects.
        config = WakeWordConfig(
            stage1_threshold=0.5,
            stage2_threshold=0.5,
            stage2_window_seconds=1280 / 16000,
        )
        detector_rej = _make_detector([0.9], config=config, verifier=_verified_false)
        with patch(
            "sovyx.voice.health._metrics.record_wake_word_confidence",
        ) as mock_record:
            detector_rej.process_frame(_frame())
        mock_record.assert_not_called()

        # Path 2: peak below stage2 threshold.
        config_high = WakeWordConfig(
            stage1_threshold=0.5,
            stage2_threshold=0.9,
            stage2_window_seconds=1280 / 16000,
        )
        detector_below = _make_detector(
            [0.6],
            config=config_high,
            verifier=_verified_true,
        )
        with patch(
            "sovyx.voice.health._metrics.record_wake_word_confidence",
        ) as mock_record:
            detector_below.process_frame(_frame())
        mock_record.assert_not_called()

    # T7.8 — Adaptive cooldown driven by false-fire signal

    def test_adaptive_cooldown_t78_disabled_by_default(self) -> None:
        """Default ``cooldown_adaptive_enabled=False`` keeps static cooldown.

        Without explicit opt-in the detector uses ``cooldown_seconds``
        (legacy 2s default) regardless of false-fire history.
        """
        config = WakeWordConfig(cooldown_seconds=2.0)
        detector = _make_detector([0.1], config=config)
        # Note false-fires — should be silently recorded but not used.
        detector.note_false_fire(monotonic_now=100.0)
        detector.note_false_fire(monotonic_now=101.0)
        detector.note_false_fire(monotonic_now=102.0)
        # Compute the cooldown — adaptive disabled → falls back to
        # static cooldown_seconds.
        assert detector._adaptive_cooldown_seconds() == 2.0  # noqa: SLF001

    def test_adaptive_cooldown_t78_below_threshold_uses_min(self) -> None:
        """Recent false-fires < threshold → cooldown_min_seconds."""
        config = WakeWordConfig(
            cooldown_adaptive_enabled=True,
            cooldown_min_seconds=2.0,
            cooldown_max_seconds=5.0,
            cooldown_adaptive_window_seconds=60.0,
            cooldown_adaptive_threshold=3,
        )
        detector = _make_detector([0.1], config=config)
        # Single false-fire — below threshold of 3.
        detector.note_false_fire()
        assert detector._adaptive_cooldown_seconds() == 2.0  # noqa: SLF001

    def test_adaptive_cooldown_t78_at_or_above_threshold_uses_max(self) -> None:
        """Recent false-fires ≥ threshold → cooldown_max_seconds."""
        config = WakeWordConfig(
            cooldown_adaptive_enabled=True,
            cooldown_min_seconds=2.0,
            cooldown_max_seconds=5.0,
            cooldown_adaptive_window_seconds=60.0,
            cooldown_adaptive_threshold=3,
        )
        detector = _make_detector([0.1], config=config)
        # Three false-fires within the window — at the threshold.
        for _ in range(3):
            detector.note_false_fire()
        assert detector._adaptive_cooldown_seconds() == 5.0  # noqa: SLF001

    def test_adaptive_cooldown_t78_window_pruning(self) -> None:
        """False-fires older than the window are dropped from the count."""
        config = WakeWordConfig(
            cooldown_adaptive_enabled=True,
            cooldown_min_seconds=2.0,
            cooldown_max_seconds=5.0,
            cooldown_adaptive_window_seconds=60.0,
            cooldown_adaptive_threshold=2,
        )
        detector = _make_detector([0.1], config=config)
        # Two false-fires AT t=0 (would meet threshold).
        detector.note_false_fire(monotonic_now=0.0)
        detector.note_false_fire(monotonic_now=0.1)
        # Now jump time forward 100s — both are outside the 60s window.
        detector.note_false_fire(monotonic_now=100.0)
        # Active count should be 1 (only the most-recent), below the 2-threshold.
        assert detector._adaptive_cooldown_seconds() == 2.0  # noqa: SLF001

    def test_adaptive_cooldown_t78_validation_rejects_max_below_min(self) -> None:
        """``cooldown_max_seconds < cooldown_min_seconds`` rejected."""
        bad_config = WakeWordConfig(
            cooldown_adaptive_enabled=True,
            cooldown_min_seconds=5.0,
            cooldown_max_seconds=2.0,  # max < min
        )
        with pytest.raises(ValueError, match="cooldown_max_seconds"):
            _make_detector([0.5], config=bad_config)

    def test_adaptive_cooldown_t78_validation_rejects_threshold_below_one(self) -> None:
        """``cooldown_adaptive_threshold < 1`` rejected."""
        bad_config = WakeWordConfig(
            cooldown_adaptive_enabled=True,
            cooldown_adaptive_threshold=0,
        )
        with pytest.raises(ValueError, match="cooldown_adaptive_threshold"):
            _make_detector([0.5], config=bad_config)

    def test_adaptive_cooldown_t78_validation_skipped_when_disabled(self) -> None:
        """When adaptive is disabled, max < min is allowed (fields ignored).

        The static path doesn't consult the adaptive fields, so the
        validator only enforces them when the operator opts in.
        """
        # Even with max < min, this should NOT raise because adaptive
        # is disabled — the fields are dead.
        ok_config = WakeWordConfig(
            cooldown_adaptive_enabled=False,
            cooldown_min_seconds=5.0,
            cooldown_max_seconds=2.0,
        )
        # Construction succeeds.
        detector = _make_detector([0.1], config=ok_config)
        # Static path returns the legacy ``cooldown_seconds``.
        assert detector._adaptive_cooldown_seconds() == ok_config.cooldown_seconds  # noqa: SLF001

    def test_structured_log_carries_breakdown_on_confirm(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The ``voice.wake_word.detected`` log event carries 3 timing fields.

        Dashboards consume the structured event for per-detection
        breakdowns without scraping the OTel histograms. Pin the
        contract so a future refactor that drops the fields fails
        loudly. Structlog renders all keyword args into the log
        record's ``message`` as a JSON-shaped dict — we search the
        message string for each expected field name.
        """
        import logging

        config = WakeWordConfig(
            stage1_threshold=0.5,
            stage2_threshold=0.5,
            stage2_window_seconds=1280 / 16000,
        )
        detector = _make_detector([0.9], config=config, verifier=_verified_true)
        with caplog.at_level(logging.INFO, logger="sovyx.voice.wake_word"):
            event = detector.process_frame(_frame())
        assert event.detected
        # The structured event lands as the formatted message — find
        # the record whose message contains the canonical event name.
        detected_records = [
            r for r in caplog.records if "voice.wake_word.detected" in r.getMessage()
        ]
        assert len(detected_records) == 1
        message = detected_records[0].getMessage()
        for key in (
            "voice.stage2_collection_ms",
            "voice.stage2_verifier_ms",
            "voice.detection_ms",
        ):
            assert key in message, f"missing field {key} in {message}"
