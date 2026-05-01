"""Tests for ``voice/_wake_word_stt_fallback.py`` — Phase 8 / T8.17-T8.19.

Pin the STT-based fallback wake-word detector contract:
- T8.17: STTWakeWordDetector class semantics (buffer rolling, periodic
  STT call, ASCII-fold matching, cooldown after match)
- T8.18: Router hot-swap from STT to ONNX (covered via test of
  re-register_mind replacing detector — same semantics as T8.6)
- T8.19: Detection-method telemetry — counter increments at every
  router-driven detection labeled by which detector class fired
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import numpy as np
import pytest

from sovyx.voice._wake_word_stt_fallback import (
    STTWakeWordConfig,
    STTWakeWordDetector,
    _ascii_fold,
    _validate_stt_config,
)
from sovyx.voice.wake_word import WakeWordState

if TYPE_CHECKING:
    import numpy.typing as npt


_FRAME = 1280


def _silent_frame() -> np.ndarray:
    return np.zeros(_FRAME, dtype=np.float32)


def _int16_frame(val: int = 1000) -> np.ndarray:
    return np.full(_FRAME, val, dtype=np.int16)


# ── Helper: spy transcribe_fn ────────────────────────────────────────


class _SpyTranscribe:
    """Programmable transcribe_fn that returns queued texts."""

    def __init__(self, texts: list[str]) -> None:
        self.texts = list(texts)
        self.calls = 0
        self.last_audio: npt.NDArray[np.float32] | None = None

    def __call__(self, audio: npt.NDArray[np.float32]) -> str:
        self.calls += 1
        self.last_audio = audio
        if self.texts:
            return self.texts.pop(0)
        return ""


# ── _ascii_fold helper ───────────────────────────────────────────────


class TestAsciiFold:
    """Pin the ASCII-fold canonicalisation contract."""

    def test_lowercase_passthrough(self) -> None:
        assert _ascii_fold("hello") == "hello"

    def test_uppercase_to_lower(self) -> None:
        assert _ascii_fold("HELLO") == "hello"

    def test_mixed_case_to_lower(self) -> None:
        assert _ascii_fold("Hello World") == "hello world"

    def test_diacritics_stripped(self) -> None:
        assert _ascii_fold("Lúcia") == "lucia"
        assert _ascii_fold("François") == "francois"
        assert _ascii_fold("Müller") == "muller"
        assert _ascii_fold("Joaquín") == "joaquin"

    def test_diacritic_strip_preserves_separators(self) -> None:
        # Spaces, punctuation, numbers preserved.
        assert _ascii_fold("Hey Lúcia, are you there?") == "hey lucia, are you there?"


# ── STTWakeWordConfig validation ─────────────────────────────────────


class TestSTTWakeWordConfigValidation:
    def test_minimal_valid_config(self) -> None:
        config = STTWakeWordConfig(wake_variants=("sovyx",))
        # No exception → valid.
        _validate_stt_config(config)

    def test_negative_buffer_seconds_rejected(self) -> None:
        config = STTWakeWordConfig(
            wake_variants=("sovyx",),
            buffer_seconds=-1.0,
        )
        with pytest.raises(ValueError, match="buffer_seconds"):
            _validate_stt_config(config)

    def test_zero_buffer_seconds_rejected(self) -> None:
        config = STTWakeWordConfig(
            wake_variants=("sovyx",),
            buffer_seconds=0.0,
        )
        with pytest.raises(ValueError, match="buffer_seconds"):
            _validate_stt_config(config)

    def test_zero_stt_call_interval_rejected(self) -> None:
        config = STTWakeWordConfig(
            wake_variants=("sovyx",),
            stt_call_interval_frames=0,
        )
        with pytest.raises(ValueError, match="stt_call_interval_frames"):
            _validate_stt_config(config)

    def test_negative_cooldown_rejected(self) -> None:
        config = STTWakeWordConfig(
            wake_variants=("sovyx",),
            cooldown_seconds=-0.5,
        )
        with pytest.raises(ValueError, match="cooldown_seconds"):
            _validate_stt_config(config)

    def test_wrong_sample_rate_rejected(self) -> None:
        config = STTWakeWordConfig(
            wake_variants=("sovyx",),
            sample_rate=44100,
        )
        with pytest.raises(ValueError, match="sample_rate"):
            _validate_stt_config(config)

    def test_wrong_frame_samples_rejected(self) -> None:
        config = STTWakeWordConfig(
            wake_variants=("sovyx",),
            frame_samples=512,
        )
        with pytest.raises(ValueError, match="frame_samples"):
            _validate_stt_config(config)


# ── STTWakeWordDetector core behaviour ───────────────────────────────


class TestSTTDetectorConstruction:
    def test_minimal_construction(self) -> None:
        config = STTWakeWordConfig(wake_variants=("sovyx",))
        spy = _SpyTranscribe([])
        detector = STTWakeWordDetector(transcribe_fn=spy, config=config)
        assert detector.state == WakeWordState.IDLE

    def test_invalid_config_rejected_at_construction(self) -> None:
        config = STTWakeWordConfig(
            wake_variants=("sovyx",),
            buffer_seconds=-1.0,
        )
        spy = _SpyTranscribe([])
        with pytest.raises(ValueError, match="buffer_seconds"):
            STTWakeWordDetector(transcribe_fn=spy, config=config)


class TestSTTDetectorFrameValidation:
    def test_wrong_frame_size_raises(self) -> None:
        config = STTWakeWordConfig(wake_variants=("sovyx",))
        spy = _SpyTranscribe([])
        detector = STTWakeWordDetector(transcribe_fn=spy, config=config)
        with pytest.raises(ValueError, match="Expected frame of 1280"):
            detector.process_frame(np.zeros(512, dtype=np.float32))

    def test_int16_frame_accepted(self) -> None:
        config = STTWakeWordConfig(wake_variants=("sovyx",))
        spy = _SpyTranscribe([])
        detector = STTWakeWordDetector(transcribe_fn=spy, config=config)
        # int16 input normalised internally — no exception.
        event = detector.process_frame(_int16_frame())
        assert not event.detected


class TestSTTDetectorPeriodicCall:
    def test_stt_only_fires_at_interval(self) -> None:
        """STT call fires every ``stt_call_interval_frames``, not every frame."""
        config = STTWakeWordConfig(
            wake_variants=("sovyx",),
            stt_call_interval_frames=5,
            buffer_seconds=2.0,
        )
        spy = _SpyTranscribe(["nothing", "nothing", "nothing"])
        detector = STTWakeWordDetector(transcribe_fn=spy, config=config)

        # 4 frames — STT not called yet (interval is 5).
        for _ in range(4):
            detector.process_frame(_silent_frame())
        assert spy.calls == 0

        # 5th frame — STT call fires (and frame_counter resets).
        detector.process_frame(_silent_frame())
        # The detector also requires buffered audio ≥ half the
        # rolling-window cap; with default buffer_seconds=2s and
        # frame_size 1280@16kHz = ~25 frames, half is ~12 frames.
        # We've only buffered 5 → STT should NOT have run yet.
        assert spy.calls == 0

    def test_stt_runs_after_buffer_fills(self) -> None:
        """STT runs once buffer has ≥ half the rolling-window cap."""
        config = STTWakeWordConfig(
            wake_variants=("sovyx",),
            stt_call_interval_frames=5,
            buffer_seconds=0.5,  # 0.5s = ~6 frames at 80ms/frame
        )
        spy = _SpyTranscribe(["nothing"])
        detector = STTWakeWordDetector(transcribe_fn=spy, config=config)

        # Buffer fills at ~6 frames; STT interval is 5; so STT fires
        # on frame 5 only if buffer is half-full (≥ 3 frames). With
        # 5 frames buffered we have 5 >= 3, so STT fires.
        for _ in range(5):
            detector.process_frame(_silent_frame())
        assert spy.calls == 1


class TestSTTDetectorMatchPath:
    def test_no_match_keeps_state_idle(self) -> None:
        config = STTWakeWordConfig(
            wake_variants=("sovyx",),
            stt_call_interval_frames=2,
            buffer_seconds=0.3,  # ~3-4 frames
        )
        spy = _SpyTranscribe(["the weather is nice"])
        detector = STTWakeWordDetector(transcribe_fn=spy, config=config)
        for _ in range(5):
            detector.process_frame(_silent_frame())
        assert detector.state == WakeWordState.IDLE

    def test_match_transitions_to_cooldown(self) -> None:
        """Transcript containing wake variant fires + transitions to COOLDOWN."""
        config = STTWakeWordConfig(
            wake_variants=("sovyx",),
            stt_call_interval_frames=2,
            buffer_seconds=0.3,
        )
        spy = _SpyTranscribe(["hey sovyx are you there"])
        detector = STTWakeWordDetector(transcribe_fn=spy, config=config)
        # Drive enough frames to trigger STT call + match.
        event = None
        for _ in range(10):
            event = detector.process_frame(_silent_frame())
            if event.detected:
                break
        assert event is not None
        assert event.detected
        assert detector.state == WakeWordState.COOLDOWN

    def test_diacritic_match_via_ascii_fold(self) -> None:
        """``Lúcia`` variant matches transcript ``"hey lucia"``."""
        config = STTWakeWordConfig(
            wake_variants=("Lúcia",),
            stt_call_interval_frames=2,
            buffer_seconds=0.3,
        )
        spy = _SpyTranscribe(["hey lucia please respond"])
        detector = STTWakeWordDetector(transcribe_fn=spy, config=config)
        event = None
        for _ in range(10):
            event = detector.process_frame(_silent_frame())
            if event.detected:
                break
        assert event is not None
        assert event.detected

    def test_case_match_via_ascii_fold(self) -> None:
        """``"sovyx"`` variant matches ``"HEY SOVYX"`` transcript."""
        config = STTWakeWordConfig(
            wake_variants=("sovyx",),
            stt_call_interval_frames=2,
            buffer_seconds=0.3,
        )
        spy = _SpyTranscribe(["HEY SOVYX HOW ARE YOU"])
        detector = STTWakeWordDetector(transcribe_fn=spy, config=config)
        event = None
        for _ in range(10):
            event = detector.process_frame(_silent_frame())
            if event.detected:
                break
        assert event is not None
        assert event.detected

    def test_empty_variants_disables_detection(self) -> None:
        """No wake variants → detector is a permanent no-op."""
        config = STTWakeWordConfig(wake_variants=())
        spy = _SpyTranscribe(["sovyx is here"])
        detector = STTWakeWordDetector(transcribe_fn=spy, config=config)
        for _ in range(10):
            event = detector.process_frame(_silent_frame())
            assert not event.detected
        # STT was never called either.
        assert spy.calls == 0


class TestSTTDetectorCooldown:
    def test_post_match_cooldown_blocks_further_detection(self) -> None:
        """During COOLDOWN, additional STT calls are suppressed."""
        config = STTWakeWordConfig(
            wake_variants=("sovyx",),
            stt_call_interval_frames=2,
            buffer_seconds=0.3,
            cooldown_seconds=10 * _FRAME / 16000,  # 10-frame cooldown
        )
        spy = _SpyTranscribe(["sovyx detected", "sovyx again", "sovyx three"])
        detector = STTWakeWordDetector(transcribe_fn=spy, config=config)

        # Drive to first match.
        first_match_idx = None
        for i in range(10):
            event = detector.process_frame(_silent_frame())
            if event.detected:
                first_match_idx = i
                break
        assert first_match_idx is not None
        first_call_count = spy.calls

        # Continue feeding frames during cooldown — STT should NOT fire.
        for _ in range(5):
            detector.process_frame(_silent_frame())
        assert spy.calls == first_call_count

    def test_cooldown_expires_back_to_idle(self) -> None:
        config = STTWakeWordConfig(
            wake_variants=("sovyx",),
            stt_call_interval_frames=2,
            buffer_seconds=0.3,
            cooldown_seconds=3 * _FRAME / 16000,  # 3-frame cooldown
        )
        spy = _SpyTranscribe(["sovyx now"])
        detector = STTWakeWordDetector(transcribe_fn=spy, config=config)

        # Drive to match.
        for _ in range(10):
            event = detector.process_frame(_silent_frame())
            if event.detected:
                break

        # Drive 3+ frames to expire cooldown.
        for _ in range(4):
            detector.process_frame(_silent_frame())
        assert detector.state == WakeWordState.IDLE


class TestSTTDetectorFailureIsolation:
    def test_transcribe_raise_is_treated_as_no_match(self) -> None:
        """A buggy transcribe_fn must NOT crash the detector."""

        def boom(_audio: npt.NDArray[np.float32]) -> str:
            msg = "STT engine exploded"
            raise RuntimeError(msg)

        config = STTWakeWordConfig(
            wake_variants=("sovyx",),
            stt_call_interval_frames=2,
            buffer_seconds=0.3,
        )
        detector = STTWakeWordDetector(transcribe_fn=boom, config=config)
        # Drive enough frames for STT to fire; expect no-match (no crash).
        for _ in range(10):
            event = detector.process_frame(_silent_frame())
            assert not event.detected


class TestSTTDetectorReset:
    def test_reset_returns_to_idle_clears_buffer(self) -> None:
        config = STTWakeWordConfig(wake_variants=("sovyx",))
        spy = _SpyTranscribe([])
        detector = STTWakeWordDetector(transcribe_fn=spy, config=config)
        detector.process_frame(_silent_frame())
        detector.process_frame(_silent_frame())
        detector.reset()
        assert detector.state == WakeWordState.IDLE
        assert detector._buffer == []  # noqa: SLF001
        assert detector._frame_counter == 0  # noqa: SLF001


class TestSTTDetectorNoteFalsefireInterface:
    def test_note_false_fire_no_op(self) -> None:
        """Interface stub — STT detector doesn't use adaptive cooldown.

        The router calls note_false_fire uniformly across all
        registered detectors; STT path's stub must not raise.
        """
        config = STTWakeWordConfig(wake_variants=("sovyx",))
        spy = _SpyTranscribe([])
        detector = STTWakeWordDetector(transcribe_fn=spy, config=config)
        # No-op call.
        detector.note_false_fire()
        detector.note_false_fire(monotonic_now=100.0)


# ── T8.18 + T8.19 — router integration ───────────────────────────────


class TestRouterSTTFallbackIntegration:
    """T8.17 + T8.18 router-level wire-up + T8.19 telemetry."""

    def test_register_mind_stt_fallback_constructs_detector(self) -> None:

        from sovyx.engine.types import MindId  # noqa: PLC0415
        from sovyx.voice._wake_word_router import WakeWordRouter  # noqa: PLC0415

        router = WakeWordRouter()
        config = STTWakeWordConfig(
            wake_variants=("sovyx",),
            stt_call_interval_frames=2,
            buffer_seconds=0.3,
        )
        spy = _SpyTranscribe([])
        router.register_mind_stt_fallback(
            MindId("aria"),
            transcribe_fn=spy,
            config=config,
        )
        assert MindId("aria") in router
        assert len(router) == 1

    def test_register_stt_then_register_onnx_hot_swaps(self) -> None:
        """T8.18 hot-swap: register STT first, then re-register with
        an ONNX model — the STT detector is replaced."""
        from pathlib import Path  # noqa: PLC0415
        from unittest.mock import MagicMock  # noqa: PLC0415

        from sovyx.engine.types import MindId  # noqa: PLC0415
        from sovyx.voice._wake_word_router import WakeWordRouter  # noqa: PLC0415

        router = WakeWordRouter()
        # Register STT fallback first.
        stt_config = STTWakeWordConfig(wake_variants=("aria",))
        spy = _SpyTranscribe([])
        router.register_mind_stt_fallback(
            MindId("aria"),
            transcribe_fn=spy,
            config=stt_config,
        )
        first_detector = router._detectors[MindId("aria")]  # noqa: SLF001
        assert isinstance(first_detector, STTWakeWordDetector)

        # Hot-swap to ONNX.
        mock_ort = MagicMock()
        mock_ort.SessionOptions.return_value = MagicMock()
        mock_ort.GraphOptimizationLevel.ORT_ENABLE_ALL = 99
        session = MagicMock()
        inputs_meta = MagicMock()
        inputs_meta.name = "input"
        session.get_inputs.return_value = [inputs_meta]
        session.run.return_value = [np.array([[0.1]], dtype=np.float32)]
        mock_ort.InferenceSession.return_value = session

        with patch.dict("sys.modules", {"onnxruntime": mock_ort}):
            router.register_mind(
                MindId("aria"),
                model_path=Path("/fake/aria.onnx"),
            )

        second_detector = router._detectors[MindId("aria")]  # noqa: SLF001
        # Detector class swapped from STT to ONNX.
        assert not isinstance(second_detector, STTWakeWordDetector)
        # Still only one mind registered (re-register replaces).
        assert len(router) == 1

    def test_router_emits_detection_method_label_for_stt_match(self) -> None:
        """T8.19: when STT detector wins, counter records method=stt_fallback."""
        from sovyx.engine.types import MindId  # noqa: PLC0415
        from sovyx.voice._wake_word_router import WakeWordRouter  # noqa: PLC0415

        router = WakeWordRouter()
        config = STTWakeWordConfig(
            wake_variants=("sovyx",),
            stt_call_interval_frames=2,
            buffer_seconds=0.3,
        )
        spy = _SpyTranscribe(["sovyx is the wake word"])
        router.register_mind_stt_fallback(
            MindId("aria"),
            transcribe_fn=spy,
            config=config,
        )

        with patch(
            "sovyx.voice.health._metrics.record_wake_word_detection_method",
        ) as mock_record:
            for _ in range(10):
                event = router.process_frame(_silent_frame())
                if event is not None:
                    break

        # Counter fired once with method=stt_fallback + the matched mind.
        mock_record.assert_called_once()
        kwargs = mock_record.call_args.kwargs
        assert kwargs["method"] == "stt_fallback"
        assert kwargs["mind_id"] == "aria"
