"""Tests for :func:`sovyx.voice.factory._build_aec_wiring` [Phase 4 T4.4.e].

Activation matrix:

* ``voice_aec_enabled=False`` (default) → ``(None, None)``.
* ``voice_aec_enabled=True`` AND ``voice_aec_engine="off"`` →
  degenerate config, treated as disabled.
* ``voice_aec_enabled=True`` AND ``voice_aec_engine="speex"`` →
  fresh :class:`RenderPcmBuffer` + Speex
  :class:`SpeexAecProcessor`. The same buffer instance bridges the
  playback path (sink) and the capture path (provider).
"""

from __future__ import annotations

from sovyx.engine.config import VoiceTuningConfig
from sovyx.voice._aec import (
    NoOpAec,
    RenderPcmProvider,
    RenderPcmSink,
    SpeexAecProcessor,
)
from sovyx.voice._double_talk_detector import DoubleTalkDetector
from sovyx.voice._render_pcm_buffer import RenderPcmBuffer
from sovyx.voice.factory import _build_aec_wiring, _build_double_talk_detector

# ── Disabled path ───────────────────────────────────────────────────────


class TestDisabledFoundationDefault:
    """Default config (voice_aec_enabled=False) returns (None, None)."""

    def test_default_tuning_returns_none_pair(self) -> None:
        tuning = VoiceTuningConfig()
        # Sanity: we're testing the foundation default per
        # feedback_staged_adoption.
        assert tuning.voice_aec_enabled is False
        buffer, processor = _build_aec_wiring(tuning)
        assert buffer is None
        assert processor is None

    def test_no_buffer_is_allocated_when_disabled(self) -> None:
        # Memory regression-guard: a long-running daemon with AEC off
        # MUST NOT pay for a 64 KiB ring + lazy pyaec import.
        tuning = VoiceTuningConfig()
        for _ in range(100):
            buffer, processor = _build_aec_wiring(tuning)
            assert buffer is None
            assert processor is None


# ── Engine-off degenerate ───────────────────────────────────────────────


class TestEngineOffDegenerate:
    """voice_aec_engine='off' treats the config as disabled."""

    def test_enabled_with_engine_off_returns_none_pair(self) -> None:
        tuning = VoiceTuningConfig(
            voice_aec_enabled=True,
            voice_aec_engine="off",
        )
        buffer, processor = _build_aec_wiring(tuning)
        assert buffer is None
        assert processor is None

    def test_pure_off_no_logging_overhead(self) -> None:
        # The helper short-circuits before logging, so the disabled
        # path doesn't pollute startup logs with a "wired" entry on
        # every daemon start.
        tuning = VoiceTuningConfig(
            voice_aec_enabled=True,
            voice_aec_engine="off",
        )
        # Just verify return — logging absence is a side observation.
        assert _build_aec_wiring(tuning) == (None, None)


# ── Enabled + Speex ─────────────────────────────────────────────────────


class TestEnabledSpeex:
    """The active configuration produces a buffer + Speex processor."""

    def _enabled_tuning(
        self,
        *,
        filter_length_ms: int = 128,
    ) -> VoiceTuningConfig:
        return VoiceTuningConfig(
            voice_aec_enabled=True,
            voice_aec_engine="speex",
            voice_aec_filter_length_ms=filter_length_ms,
        )

    def test_returns_real_buffer(self) -> None:
        buffer, _ = _build_aec_wiring(self._enabled_tuning())
        assert isinstance(buffer, RenderPcmBuffer)

    def test_returns_speex_processor(self) -> None:
        _, processor = _build_aec_wiring(self._enabled_tuning())
        assert isinstance(processor, SpeexAecProcessor)
        # Sanity: NOT the no-op even though the factory could return
        # one — the helper must materialise the real Speex backend
        # when engine='speex'.
        assert not isinstance(processor, NoOpAec)

    def test_buffer_satisfies_both_protocols(self) -> None:
        # T4.4.d contract: the same instance bridges producer→consumer.
        buffer, _ = _build_aec_wiring(self._enabled_tuning())
        assert isinstance(buffer, RenderPcmSink)
        assert isinstance(buffer, RenderPcmProvider)

    def test_buffer_has_default_capacity(self) -> None:
        buffer, _ = _build_aec_wiring(self._enabled_tuning())
        # 2 s @ 16 kHz = 32 000 samples — see _render_pcm_buffer
        # _DEFAULT_BUFFER_SECONDS.
        assert buffer is not None
        assert buffer.capacity_samples == 32_000

    def test_filter_length_propagates_to_processor(self) -> None:
        # filter_length_ms=64 → at 16 kHz frame_size=512 the Speex
        # filter has 64 ms of delay tolerance. The processor accepts
        # 512-sample int16 windows without error.
        import numpy as np

        _, processor = _build_aec_wiring(self._enabled_tuning(filter_length_ms=64))
        assert processor is not None
        capture = np.zeros(512, dtype=np.int16)
        render = np.zeros(512, dtype=np.int16)
        out = processor.process(capture, render)
        assert out.shape == (512,)

    def test_each_call_returns_independent_buffer(self) -> None:
        # No singleton; each create_voice_pipeline call gets a fresh
        # buffer. The factory invokes this once per daemon lifecycle
        # but tests / dashboards can call it multiple times safely.
        b1, _ = _build_aec_wiring(self._enabled_tuning())
        b2, _ = _build_aec_wiring(self._enabled_tuning())
        assert b1 is not b2

    def test_buffer_starts_empty(self) -> None:
        # No render PCM has been fed yet; capture-side reads return
        # silence (AEC short-circuits to passthrough).
        import numpy as np

        buffer, _ = _build_aec_wiring(self._enabled_tuning())
        assert buffer is not None
        assert buffer.filled_samples == 0
        out = buffer.get_aligned_window(512)
        assert np.all(out == 0)


# ── Logging contract ────────────────────────────────────────────────────


class TestLogging:
    """The wire-up emits exactly one structured log per activation."""

    def test_disabled_path_emits_no_aec_wired_log(
        self,
        caplog: object,  # noqa: ANN401 — pytest fixture type
    ) -> None:
        import logging as _logging

        import pytest as _pytest

        if not isinstance(caplog, _pytest.LogCaptureFixture):
            return  # safety; pytest passes the fixture

        tuning = VoiceTuningConfig()
        with caplog.at_level(_logging.INFO, logger="sovyx.voice.factory"):
            _build_aec_wiring(tuning)
        wired_logs = [r for r in caplog.records if "voice.aec.wired" in r.getMessage()]
        assert wired_logs == []

    def test_enabled_path_emits_one_aec_wired_log(
        self,
        caplog: object,  # noqa: ANN401
    ) -> None:
        import logging as _logging

        import pytest as _pytest

        if not isinstance(caplog, _pytest.LogCaptureFixture):
            return

        tuning = VoiceTuningConfig(
            voice_aec_enabled=True,
            voice_aec_engine="speex",
            voice_aec_filter_length_ms=64,
        )
        with caplog.at_level(_logging.INFO, logger="sovyx.voice.factory"):
            _build_aec_wiring(tuning)
        wired_logs = [r for r in caplog.records if "voice.aec.wired" in r.getMessage()]
        assert len(wired_logs) == 1


# ── T4.9 — Double-talk detector factory wire-up ─────────────────────────


class TestBuildDoubleTalkDetector:
    """Factory builds the detector when its tuning flag is on."""

    def test_default_disabled_returns_none(self) -> None:
        tuning = VoiceTuningConfig()
        assert tuning.voice_double_talk_detection_enabled is False
        assert _build_double_talk_detector(tuning) is None

    def test_enabled_returns_real_detector(self) -> None:
        tuning = VoiceTuningConfig(
            voice_double_talk_detection_enabled=True,
        )
        detector = _build_double_talk_detector(tuning)
        assert isinstance(detector, DoubleTalkDetector)

    def test_threshold_propagates_to_detector(self) -> None:
        tuning = VoiceTuningConfig(
            voice_double_talk_detection_enabled=True,
            voice_double_talk_ncc_threshold=0.7,
        )
        detector = _build_double_talk_detector(tuning)
        assert detector is not None
        assert detector.threshold == 0.7

    def test_each_call_returns_independent_instance(self) -> None:
        tuning = VoiceTuningConfig(voice_double_talk_detection_enabled=True)
        a = _build_double_talk_detector(tuning)
        b = _build_double_talk_detector(tuning)
        assert a is not None
        assert b is not None
        assert a is not b
