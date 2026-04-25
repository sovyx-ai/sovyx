"""Tests for MoonshineSTT — ONNX inference, Tiny/Base, preprocessing (V05-19).

Strategy: mock moonshine_voice library to test engine lifecycle, transcription
flow, streaming, error handling, and config validation without real models.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

import numpy as np
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from sovyx.voice.stt import (
    _COMPRESSION_RATIO_THRESHOLD,
    _DEFAULT_SAMPLE_RATE,
    _HALLUCINATION_MIN_LEN_FOR_RATIO_CHECK,
    _HALLUCINATION_STOPLIST,
    _MODEL_SPECS,
    _STREAMING_DRAIN_S,
    _TRANSCRIBE_TIMEOUT_S,
    MoonshineConfig,
    MoonshineSTT,
    PartialTranscription,
    STTState,
    TranscriptionResult,
    TranscriptionSegment,
    _compute_compression_ratio,
    _is_hallucination,
    _normalise_for_stoplist,
)

# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------


def _make_mock_transcriber(
    completed_text: str = "hello world",
    *,
    emit_started: bool = True,
    emit_changed: bool = True,
) -> MagicMock:
    """Build a mock Transcriber that fires events on stream lifecycle."""
    transcriber = MagicMock()

    def _create_stream(update_interval: float = 0.3) -> MagicMock:  # noqa: ARG001
        stream = MagicMock()
        listeners: list[object] = []

        def _add_listener(listener: object) -> None:
            listeners.append(listener)

        def _add_audio(audio: list | np.ndarray, sample_rate: int) -> None:  # noqa: ARG001
            # Fire events when audio is added
            for lis in listeners:
                if emit_started and hasattr(lis, "on_line_started"):
                    event = MagicMock()
                    event.line.text = ""
                    lis.on_line_started(event)
                if emit_changed and hasattr(lis, "on_line_text_changed"):
                    event = MagicMock()
                    event.line.text = completed_text[:5]
                    lis.on_line_text_changed(event)

        def _stop() -> None:
            for lis in listeners:
                if hasattr(lis, "on_line_completed"):
                    event = MagicMock()
                    event.line.text = completed_text
                    lis.on_line_completed(event)

        stream.add_listener = _add_listener
        stream.add_audio = _add_audio
        stream.stop = _stop
        stream.start = MagicMock()
        stream.close = MagicMock()
        return stream

    transcriber.create_stream = _create_stream
    return transcriber


def _make_mock_moonshine_voice(
    transcriber: MagicMock | None = None,
) -> MagicMock:
    """Build a mock moonshine_voice module."""
    mock_module = MagicMock()
    mock_module.get_model_for_language.return_value = ("/mock/path", "tiny")
    mock_module.Transcriber.return_value = transcriber or _make_mock_transcriber()

    # TranscriptEventListener needs to be a real base class for subclassing
    class MockListener:
        def on_line_started(self, event: object) -> None: ...
        def on_line_text_changed(self, event: object) -> None: ...
        def on_line_completed(self, event: object) -> None: ...

    mock_module.TranscriptEventListener = MockListener
    return mock_module


def _build_stt(
    config: MoonshineConfig | None = None,
    completed_text: str = "hello world",
    *,
    emit_started: bool = True,
    emit_changed: bool = True,
) -> tuple[MoonshineSTT, MagicMock]:
    """Construct a MoonshineSTT with mocked moonshine_voice."""
    cfg = config or MoonshineConfig()
    transcriber = _make_mock_transcriber(
        completed_text,
        emit_started=emit_started,
        emit_changed=emit_changed,
    )
    mock_mv = _make_mock_moonshine_voice(transcriber)
    stt = MoonshineSTT(config=cfg)
    return stt, mock_mv


async def _audio_stream(
    chunks: list[np.ndarray],
    sample_rate: int = _DEFAULT_SAMPLE_RATE,
) -> AsyncIterator[tuple[np.ndarray, int]]:
    """Helper to create an async audio stream from chunks."""
    for chunk in chunks:
        yield (chunk, sample_rate)


# ---------------------------------------------------------------------------
# Config tests
# ---------------------------------------------------------------------------


class TestMoonshineConfig:
    """Tests for MoonshineConfig validation."""

    def test_default_config(self) -> None:
        cfg = MoonshineConfig()
        assert cfg.language == "en"
        assert cfg.model_size == "tiny"
        assert cfg.update_interval == 0.3
        assert cfg.transcribe_timeout == _TRANSCRIBE_TIMEOUT_S

    def test_valid_model_sizes(self) -> None:
        for size in ("tiny", "small", "medium"):
            cfg = MoonshineConfig(model_size=size)
            assert cfg.model_size == size

    def test_invalid_model_size_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown model_size"):
            MoonshineConfig(model_size="huge")

    def test_invalid_update_interval_raises(self) -> None:
        with pytest.raises(ValueError, match="update_interval must be positive"):
            MoonshineConfig(update_interval=0)

    def test_negative_update_interval_raises(self) -> None:
        with pytest.raises(ValueError, match="update_interval must be positive"):
            MoonshineConfig(update_interval=-1.0)

    def test_invalid_timeout_raises(self) -> None:
        with pytest.raises(ValueError, match="transcribe_timeout must be positive"):
            MoonshineConfig(transcribe_timeout=0)

    def test_custom_language(self) -> None:
        cfg = MoonshineConfig(language="es")
        assert cfg.language == "es"

    def test_frozen_slots(self) -> None:
        """Config is immutable."""
        cfg = MoonshineConfig()
        with pytest.raises(AttributeError):
            cfg.language = "fr"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Data types tests
# ---------------------------------------------------------------------------


class TestDataTypes:
    """Tests for TranscriptionResult, PartialTranscription, TranscriptionSegment."""

    def test_transcription_result_defaults(self) -> None:
        r = TranscriptionResult(text="hello")
        assert r.text == "hello"
        assert r.language is None
        assert r.confidence == 0.0
        assert r.duration_ms == 0.0
        assert r.segments is None

    def test_transcription_result_full(self) -> None:
        seg = TranscriptionSegment(text="hello", start_ms=0, end_ms=500, confidence=0.9)
        r = TranscriptionResult(
            text="hello",
            language="en",
            confidence=0.95,
            duration_ms=123.4,
            segments=[seg],
        )
        assert r.language == "en"
        assert r.confidence == 0.95
        assert r.duration_ms == 123.4
        assert len(r.segments) == 1  # type: ignore[arg-type]
        assert r.segments[0].text == "hello"  # type: ignore[index]

    def test_partial_transcription_defaults(self) -> None:
        p = PartialTranscription(text="hel")
        assert p.text == "hel"
        assert p.is_final is False
        assert p.confidence == 0.0

    def test_partial_transcription_final(self) -> None:
        p = PartialTranscription(text="hello world", is_final=True, confidence=0.95)
        assert p.is_final is True
        assert p.confidence == 0.95

    def test_transcription_segment(self) -> None:
        s = TranscriptionSegment(text="world", start_ms=500, end_ms=1000)
        assert s.text == "world"
        assert s.start_ms == 500
        assert s.end_ms == 1000
        assert s.confidence == 0.0

    def test_result_is_frozen(self) -> None:
        r = TranscriptionResult(text="test")
        with pytest.raises(AttributeError):
            r.text = "other"  # type: ignore[misc]

    def test_partial_is_frozen(self) -> None:
        p = PartialTranscription(text="test")
        with pytest.raises(AttributeError):
            p.text = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# STTState tests
# ---------------------------------------------------------------------------


class TestSTTState:
    """Tests for STT state enum."""

    def test_all_states_exist(self) -> None:
        assert STTState.UNINITIALIZED is not None
        assert STTState.READY is not None
        assert STTState.TRANSCRIBING is not None
        assert STTState.CLOSED is not None

    def test_states_are_distinct(self) -> None:
        states = [STTState.UNINITIALIZED, STTState.READY, STTState.TRANSCRIBING, STTState.CLOSED]
        assert len(set(states)) == 4


# ---------------------------------------------------------------------------
# Model specs tests
# ---------------------------------------------------------------------------


class TestModelSpecs:
    """Tests for model specification constants."""

    def test_all_sizes_present(self) -> None:
        assert set(_MODEL_SPECS.keys()) == {"tiny", "small", "medium"}

    def test_tiny_specs(self) -> None:
        spec = _MODEL_SPECS["tiny"]
        assert spec["params_m"] == 34
        assert spec["latency_ms"] == 237
        assert spec["wer_pct"] == 12.0

    def test_small_specs(self) -> None:
        spec = _MODEL_SPECS["small"]
        assert spec["params_m"] == 123

    def test_medium_specs(self) -> None:
        spec = _MODEL_SPECS["medium"]
        assert spec["params_m"] == 245


# ---------------------------------------------------------------------------
# Engine lifecycle tests
# ---------------------------------------------------------------------------


class TestMoonshineSTTLifecycle:
    """Tests for engine initialization, state transitions, and cleanup."""

    def test_initial_state(self) -> None:
        stt = MoonshineSTT()
        assert stt.state == STTState.UNINITIALIZED

    def test_default_config(self) -> None:
        stt = MoonshineSTT()
        assert stt.config.model_size == "tiny"
        assert stt.config.language == "en"

    def test_custom_config(self) -> None:
        cfg = MoonshineConfig(language="es", model_size="small")
        stt = MoonshineSTT(config=cfg)
        assert stt.config.language == "es"
        assert stt.config.model_size == "small"

    @pytest.mark.asyncio
    async def test_initialize_sets_ready(self) -> None:
        stt, mock_mv = _build_stt()
        with patch.dict("sys.modules", {"moonshine_voice": mock_mv}):
            await stt.initialize()
        assert stt.state == STTState.READY

    @pytest.mark.asyncio
    async def test_initialize_idempotent(self) -> None:
        stt, mock_mv = _build_stt()
        with patch.dict("sys.modules", {"moonshine_voice": mock_mv}):
            await stt.initialize()
            await stt.initialize()  # Should not raise
        assert stt.state == STTState.READY
        # get_model_for_language called only once
        assert mock_mv.get_model_for_language.call_count == 1

    @pytest.mark.asyncio
    async def test_close_sets_closed(self) -> None:
        stt, mock_mv = _build_stt()
        with patch.dict("sys.modules", {"moonshine_voice": mock_mv}):
            await stt.initialize()
            await stt.close()
        assert stt.state == STTState.CLOSED

    @pytest.mark.asyncio
    async def test_initialize_after_close_raises(self) -> None:
        stt, mock_mv = _build_stt()
        with patch.dict("sys.modules", {"moonshine_voice": mock_mv}):
            await stt.initialize()
            await stt.close()
            with pytest.raises(RuntimeError, match="Cannot initialize a closed"):
                await stt.initialize()

    @pytest.mark.asyncio
    async def test_transcribe_before_init_raises(self) -> None:
        stt = MoonshineSTT()
        audio = np.zeros(16000, dtype=np.float32)
        with pytest.raises(RuntimeError, match="not initialized"):
            await stt.transcribe(audio)

    @pytest.mark.asyncio
    async def test_transcribe_after_close_raises(self) -> None:
        stt, mock_mv = _build_stt()
        with patch.dict("sys.modules", {"moonshine_voice": mock_mv}):
            await stt.initialize()
            await stt.close()
        audio = np.zeros(16000, dtype=np.float32)
        with pytest.raises(RuntimeError, match="closed"):
            await stt.transcribe(audio)


# ---------------------------------------------------------------------------
# Transcription tests
# ---------------------------------------------------------------------------


class TestMoonshineSTTTranscribe:
    """Tests for full utterance transcription."""

    @pytest.mark.asyncio
    async def test_transcribe_returns_result(self) -> None:
        stt, mock_mv = _build_stt(completed_text="hello world")
        with patch.dict("sys.modules", {"moonshine_voice": mock_mv}):
            await stt.initialize()
            audio = np.random.randn(16000).astype(np.float32)
            result = await stt.transcribe(audio)

        assert isinstance(result, TranscriptionResult)
        assert result.text == "hello world"
        assert result.language == "en"
        assert result.confidence == 0.9
        # duration_ms can be 0.0 on very fast hardware where the mocked
        # transcribe returns sub-microsecond — only assert non-negative.
        assert result.duration_ms >= 0

    @pytest.mark.asyncio
    async def test_transcribe_strips_whitespace(self) -> None:
        stt, mock_mv = _build_stt(completed_text="  hello  ")
        with patch.dict("sys.modules", {"moonshine_voice": mock_mv}):
            await stt.initialize()
            audio = np.zeros(16000, dtype=np.float32)
            result = await stt.transcribe(audio)

        assert result.text == "hello"

    @pytest.mark.asyncio
    async def test_transcribe_custom_sample_rate(self) -> None:
        stt, mock_mv = _build_stt(completed_text="test")
        with patch.dict("sys.modules", {"moonshine_voice": mock_mv}):
            await stt.initialize()
            audio = np.zeros(48000, dtype=np.float32)  # 48kHz, 1s
            result = await stt.transcribe(audio, sample_rate=48000)

        assert result.text == "test"

    @pytest.mark.asyncio
    async def test_transcribe_state_returns_to_ready(self) -> None:
        stt, mock_mv = _build_stt()
        with patch.dict("sys.modules", {"moonshine_voice": mock_mv}):
            await stt.initialize()
            audio = np.zeros(16000, dtype=np.float32)
            await stt.transcribe(audio)

        assert stt.state == STTState.READY

    @pytest.mark.asyncio
    async def test_transcribe_empty_on_timeout(self) -> None:
        """When moonshine doesn't fire on_line_completed, we get empty text."""
        stt, mock_mv = _build_stt(completed_text="test")

        # Override: make a transcriber that never fires completed
        transcriber = MagicMock()

        def _create_stream(update_interval: float = 0.3) -> MagicMock:  # noqa: ARG001
            stream = MagicMock()
            stream.add_listener = MagicMock()
            stream.add_audio = MagicMock()
            stream.stop = MagicMock()  # Never fires completed event
            stream.start = MagicMock()
            stream.close = MagicMock()
            return stream

        transcriber.create_stream = _create_stream
        mock_mv.Transcriber.return_value = transcriber

        cfg = MoonshineConfig(transcribe_timeout=0.1)  # Very short timeout
        stt = MoonshineSTT(config=cfg)

        with patch.dict("sys.modules", {"moonshine_voice": mock_mv}):
            await stt.initialize()
            audio = np.zeros(1600, dtype=np.float32)
            result = await stt.transcribe(audio)

        assert result.text == ""
        assert stt.state == STTState.READY

    @pytest.mark.asyncio
    async def test_transcribe_multiple_times(self) -> None:
        stt, mock_mv = _build_stt(completed_text="hi")
        with patch.dict("sys.modules", {"moonshine_voice": mock_mv}):
            await stt.initialize()
            audio = np.zeros(16000, dtype=np.float32)
            r1 = await stt.transcribe(audio)
            r2 = await stt.transcribe(audio)

        assert r1.text == "hi"
        assert r2.text == "hi"
        assert stt.state == STTState.READY

    @pytest.mark.asyncio
    async def test_transcribe_with_language_config(self) -> None:
        cfg = MoonshineConfig(language="es")
        stt, mock_mv = _build_stt(config=cfg, completed_text="hola")
        with patch.dict("sys.modules", {"moonshine_voice": mock_mv}):
            await stt.initialize()
            audio = np.zeros(16000, dtype=np.float32)
            result = await stt.transcribe(audio)

        assert result.language == "es"
        assert result.text == "hola"


# ---------------------------------------------------------------------------
# Streaming transcription tests
# ---------------------------------------------------------------------------


class TestMoonshineSTTStreaming:
    """Tests for streaming transcription."""

    @pytest.mark.asyncio
    async def test_streaming_yields_partials(self) -> None:
        stt, mock_mv = _build_stt(completed_text="hello world")
        with patch.dict("sys.modules", {"moonshine_voice": mock_mv}):
            await stt.initialize()

            chunks = [np.random.randn(3200).astype(np.float32) for _ in range(3)]
            results: list[PartialTranscription] = []
            async for partial in stt.transcribe_streaming(_audio_stream(chunks)):
                results.append(partial)

        # Should have partials + final
        assert len(results) > 0
        assert any(r.is_final for r in results)

    @pytest.mark.asyncio
    async def test_streaming_final_has_high_confidence(self) -> None:
        stt, mock_mv = _build_stt(completed_text="test text")
        with patch.dict("sys.modules", {"moonshine_voice": mock_mv}):
            await stt.initialize()

            chunks = [np.zeros(3200, dtype=np.float32)]
            results: list[PartialTranscription] = []
            async for partial in stt.transcribe_streaming(_audio_stream(chunks)):
                results.append(partial)

        finals = [r for r in results if r.is_final]
        assert len(finals) >= 1
        assert finals[-1].confidence == 0.95

    @pytest.mark.asyncio
    async def test_streaming_non_final_has_lower_confidence(self) -> None:
        stt, mock_mv = _build_stt(completed_text="test text")
        with patch.dict("sys.modules", {"moonshine_voice": mock_mv}):
            await stt.initialize()

            chunks = [np.zeros(3200, dtype=np.float32)]
            results: list[PartialTranscription] = []
            async for partial in stt.transcribe_streaming(_audio_stream(chunks)):
                results.append(partial)

        non_finals = [r for r in results if not r.is_final]
        for nf in non_finals:
            assert nf.confidence < 0.95

    @pytest.mark.asyncio
    async def test_streaming_state_returns_to_ready(self) -> None:
        stt, mock_mv = _build_stt()
        with patch.dict("sys.modules", {"moonshine_voice": mock_mv}):
            await stt.initialize()

            chunks = [np.zeros(3200, dtype=np.float32)]
            async for _ in stt.transcribe_streaming(_audio_stream(chunks)):
                pass

        assert stt.state == STTState.READY

    @pytest.mark.asyncio
    async def test_streaming_empty_audio(self) -> None:
        stt, mock_mv = _build_stt(completed_text="")
        with patch.dict("sys.modules", {"moonshine_voice": mock_mv}):
            await stt.initialize()

            results: list[PartialTranscription] = []
            async for partial in stt.transcribe_streaming(_audio_stream([])):
                results.append(partial)

        # With no audio chunks, we still drain after stop
        # Final event may or may not fire depending on mock
        assert stt.state == STTState.READY

    @pytest.mark.asyncio
    async def test_streaming_before_init_raises(self) -> None:
        stt = MoonshineSTT()
        chunks = [np.zeros(3200, dtype=np.float32)]
        with pytest.raises(RuntimeError, match="not initialized"):
            async for _ in stt.transcribe_streaming(_audio_stream(chunks)):
                pass


# ---------------------------------------------------------------------------
# Preprocessing / audio format tests
# ---------------------------------------------------------------------------


class TestAudioPreprocessing:
    """Tests for audio format handling."""

    @pytest.mark.asyncio
    async def test_accepts_float32_audio(self) -> None:
        stt, mock_mv = _build_stt(completed_text="test")
        with patch.dict("sys.modules", {"moonshine_voice": mock_mv}):
            await stt.initialize()
            audio = np.random.randn(16000).astype(np.float32)
            result = await stt.transcribe(audio)
        assert result.text == "test"

    @pytest.mark.asyncio
    async def test_accepts_int16_audio(self) -> None:
        stt, mock_mv = _build_stt(completed_text="test")
        with patch.dict("sys.modules", {"moonshine_voice": mock_mv}):
            await stt.initialize()
            audio = (np.random.randn(16000) * 32767).astype(np.int16)
            result = await stt.transcribe(audio)
        assert result.text == "test"

    @pytest.mark.asyncio
    async def test_accepts_various_sample_rates(self) -> None:
        """Library handles resampling internally; we just pass through."""
        for rate in (8000, 16000, 22050, 44100, 48000):
            stt, mock_mv = _build_stt(completed_text="test")
            with patch.dict("sys.modules", {"moonshine_voice": mock_mv}):
                await stt.initialize()
                audio = np.zeros(rate, dtype=np.float32)  # 1 second at given rate
                result = await stt.transcribe(audio, sample_rate=rate)
            assert result.text == "test"

    @pytest.mark.asyncio
    async def test_short_audio_segment(self) -> None:
        """Very short audio (< 100ms) should still work."""
        stt, mock_mv = _build_stt(completed_text="")
        with patch.dict("sys.modules", {"moonshine_voice": mock_mv}):
            await stt.initialize()
            audio = np.zeros(160, dtype=np.float32)  # 10ms at 16kHz
            result = await stt.transcribe(audio)
        assert isinstance(result, TranscriptionResult)

    @pytest.mark.asyncio
    async def test_long_audio_segment(self) -> None:
        """30 second audio should work fine."""
        stt, mock_mv = _build_stt(completed_text="long speech")
        with patch.dict("sys.modules", {"moonshine_voice": mock_mv}):
            await stt.initialize()
            audio = np.random.randn(16000 * 30).astype(np.float32)
            result = await stt.transcribe(audio)
        assert result.text == "long speech"


# ---------------------------------------------------------------------------
# Hypothesis property-based tests
# ---------------------------------------------------------------------------


class TestPropertyBased:
    """Property-based tests for STT components."""

    @settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(
        language=st.sampled_from(["en", "es", "zh", "ja", "ko"]),
        model_size=st.sampled_from(["tiny", "small", "medium"]),
        update_interval=st.floats(min_value=0.01, max_value=5.0),
        timeout=st.floats(min_value=0.01, max_value=60.0),
    )
    def test_valid_configs_always_accepted(
        self,
        language: str,
        model_size: str,
        update_interval: float,
        timeout: float,
    ) -> None:
        cfg = MoonshineConfig(
            language=language,
            model_size=model_size,
            update_interval=update_interval,
            transcribe_timeout=timeout,
        )
        assert cfg.model_size == model_size
        assert cfg.language == language

    @settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(
        model_size=st.text(min_size=1, max_size=10).filter(
            lambda s: s not in ("tiny", "small", "medium")
        ),
    )
    def test_invalid_model_sizes_always_rejected(self, model_size: str) -> None:
        with pytest.raises(ValueError, match="Unknown model_size"):
            MoonshineConfig(model_size=model_size)

    @settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(
        interval=st.floats(max_value=0.0),
    )
    def test_non_positive_intervals_rejected(self, interval: float) -> None:
        with pytest.raises(ValueError, match="update_interval must be positive"):
            MoonshineConfig(update_interval=interval)

    @settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(
        audio_length=st.integers(min_value=1, max_value=160000),
    )
    def test_transcription_result_always_has_text_field(self, audio_length: int) -> None:
        """TranscriptionResult text is always a string, regardless of input."""
        r = TranscriptionResult(text="x" * min(audio_length, 100))
        assert isinstance(r.text, str)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge case and error handling tests."""

    @pytest.mark.asyncio
    async def test_concurrent_transcriptions(self) -> None:
        """Multiple sequential transcriptions should work."""
        stt, mock_mv = _build_stt(completed_text="test")
        with patch.dict("sys.modules", {"moonshine_voice": mock_mv}):
            await stt.initialize()
            for _ in range(5):
                audio = np.zeros(1600, dtype=np.float32)
                result = await stt.transcribe(audio)
                assert result.text == "test"

    @pytest.mark.asyncio
    async def test_close_is_idempotent(self) -> None:
        stt, mock_mv = _build_stt()
        with patch.dict("sys.modules", {"moonshine_voice": mock_mv}):
            await stt.initialize()
            await stt.close()
            await stt.close()  # Should not raise
        assert stt.state == STTState.CLOSED

    def test_model_specs_consistency(self) -> None:
        """All model specs have the same keys."""
        keys = None
        for spec in _MODEL_SPECS.values():
            if keys is None:
                keys = set(spec.keys())
            else:
                assert set(spec.keys()) == keys

    def test_model_specs_latency_ordering(self) -> None:
        """Tiny < Small < Medium in latency."""
        assert _MODEL_SPECS["tiny"]["latency_ms"] < _MODEL_SPECS["small"]["latency_ms"]
        assert _MODEL_SPECS["small"]["latency_ms"] < _MODEL_SPECS["medium"]["latency_ms"]

    def test_model_specs_wer_ordering(self) -> None:
        """Tiny > Small > Medium in WER (bigger model = lower error)."""
        assert _MODEL_SPECS["tiny"]["wer_pct"] > _MODEL_SPECS["small"]["wer_pct"]
        assert _MODEL_SPECS["small"]["wer_pct"] > _MODEL_SPECS["medium"]["wer_pct"]

    def test_default_sample_rate(self) -> None:
        assert _DEFAULT_SAMPLE_RATE == 16_000

    def test_constants(self) -> None:
        assert _TRANSCRIBE_TIMEOUT_S == 10.0
        assert _STREAMING_DRAIN_S == 0.5


# ===========================================================================
# S1: Ring 4 decode-validation guards (hallucination + compression-ratio)
# ===========================================================================
#
# Pre-S1 every Moonshine output reached the orchestrator unfiltered.
# Whisper-class STTs emit a small set of canonical hallucinations on
# silence/music/unintelligible input ("thank you", "thanks for
# watching"...) — those would cause the orchestrator to fire phantom
# turns to the LLM, polluting context. S1 adds two output-side guards
# at the Ring 4 boundary: a per-language stop-list and the
# Whisper-canonical compression-ratio reject (2.4).
#
# Reference: MISSION-voice-mixer-enterprise-refactor-2026-04-25 §3.3, S1.

_STT_LOGGER = "sovyx.voice.stt"


def _stt_events_of(
    caplog: pytest.LogCaptureFixture,
    event_name: str,
) -> list[dict[str, Any]]:
    return [
        r.msg
        for r in caplog.records
        if r.name == _STT_LOGGER and isinstance(r.msg, dict) and r.msg.get("event") == event_name
    ]


class TestComputeCompressionRatioPure:
    """Pure-function compression-ratio diagnostics."""

    def test_empty_returns_zero(self) -> None:
        assert _compute_compression_ratio("") == 0.0

    def test_short_text_returns_low_ratio(self) -> None:
        # Very short text doesn't compress well — ratio < 1.0 is normal.
        ratio = _compute_compression_ratio("hi")
        assert ratio < 1.0

    def test_repetitive_text_high_ratio(self) -> None:
        """The signature failure mode: highly-repetitive output."""
        ratio = _compute_compression_ratio("yes " * 200)
        assert ratio > _COMPRESSION_RATIO_THRESHOLD

    def test_natural_text_below_threshold(self) -> None:
        """A natural-language sentence stays well below the threshold."""
        ratio = _compute_compression_ratio(
            "The quick brown fox jumps over the lazy dog near the riverbank."
        )
        assert ratio < _COMPRESSION_RATIO_THRESHOLD


class TestNormaliseForStoplist:
    def test_lowercases(self) -> None:
        assert _normalise_for_stoplist("Thank You") == "thank you"

    def test_strips_whitespace(self) -> None:
        assert _normalise_for_stoplist("  thank you  ") == "thank you"


class TestIsHallucination:
    """Per-language stop-list contract."""

    def test_english_thank_you_is_hallucination(self) -> None:
        assert _is_hallucination("thank you", "en") is True
        assert _is_hallucination("Thank You.", "en") is True
        assert _is_hallucination("  THANK YOU  ", "en") is True

    def test_english_real_speech_is_not_hallucination(self) -> None:
        assert _is_hallucination("hello world how are you today", "en") is False

    def test_portuguese_obrigado_is_hallucination(self) -> None:
        assert _is_hallucination("obrigado", "pt") is True
        assert _is_hallucination("Obrigada.", "pt") is True

    def test_spanish_gracias_is_hallucination(self) -> None:
        assert _is_hallucination("gracias", "es") is True
        assert _is_hallucination("muchas gracias.", "es") is True

    def test_unknown_language_falls_back_to_english(self) -> None:
        # Sovyx defaults to en stoplist when the language is unmapped.
        assert _is_hallucination("thank you", "ja") is True

    def test_empty_string_is_hallucination(self) -> None:
        assert _is_hallucination("", "en") is True
        assert _is_hallucination("   ", "en") is True

    def test_stoplist_has_required_languages(self) -> None:
        """Public-surface invariant — these locales must be supported."""
        assert "en" in _HALLUCINATION_STOPLIST
        assert "pt" in _HALLUCINATION_STOPLIST
        assert "es" in _HALLUCINATION_STOPLIST


class TestSTTGuardsEndToEnd:
    """Full round-trip: transcribe + reject + telemetry event."""

    @pytest.mark.asyncio()
    async def test_hallucination_transcript_rejected(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging

        caplog.set_level(logging.WARNING, logger=_STT_LOGGER)
        stt, mock_mv = _build_stt(completed_text="thank you")
        with patch.dict("sys.modules", {"moonshine_voice": mock_mv}):
            await stt.initialize()
            result = await stt.transcribe(np.zeros(16_000, dtype=np.float32))
        assert result.text == ""
        assert result.rejection_reason == "hallucination_stoplist"
        events = _stt_events_of(caplog, "voice.stt.transcript_rejected")
        assert len(events) == 1
        assert events[0]["voice.rejection_reason"] == "hallucination_stoplist"
        assert events[0]["voice.transcript"] == "thank you"

    @pytest.mark.asyncio()
    async def test_compression_ratio_transcript_rejected(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging

        caplog.set_level(logging.WARNING, logger=_STT_LOGGER)
        # >32 chars + highly repetitive → triggers compression-ratio reject
        repetitive = "yes " * 100
        stt, mock_mv = _build_stt(completed_text=repetitive)
        with patch.dict("sys.modules", {"moonshine_voice": mock_mv}):
            await stt.initialize()
            result = await stt.transcribe(np.zeros(16_000, dtype=np.float32))
        assert result.text == ""
        assert result.rejection_reason == "compression_ratio_exceeded"
        events = _stt_events_of(caplog, "voice.stt.transcript_rejected")
        assert len(events) == 1
        assert events[0]["voice.rejection_reason"] == "compression_ratio_exceeded"
        assert events[0]["voice.compression_ratio"] > _COMPRESSION_RATIO_THRESHOLD

    @pytest.mark.asyncio()
    async def test_short_repetitive_text_not_rejected_by_ratio_check(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Below _HALLUCINATION_MIN_LEN_FOR_RATIO_CHECK the ratio check
        skips — we don't want short-text false positives. The chosen
        short text is also NOT in the stop-list."""
        import logging

        caplog.set_level(logging.WARNING, logger=_STT_LOGGER)
        # 12 chars, repetitive but below the ratio-check floor.
        stt, mock_mv = _build_stt(completed_text="ab ab ab ab")
        with patch.dict("sys.modules", {"moonshine_voice": mock_mv}):
            await stt.initialize()
            result = await stt.transcribe(np.zeros(16_000, dtype=np.float32))
        assert result.rejection_reason is None
        assert result.text == "ab ab ab ab"

    @pytest.mark.asyncio()
    async def test_natural_long_text_accepted(self) -> None:
        stt, mock_mv = _build_stt(
            completed_text="hello world today the weather is sunny and warm everyone"
        )
        with patch.dict("sys.modules", {"moonshine_voice": mock_mv}):
            await stt.initialize()
            result = await stt.transcribe(np.zeros(16_000, dtype=np.float32))
        assert result.rejection_reason is None
        assert result.text.startswith("hello world")
        assert result.confidence > 0.0

    @pytest.mark.asyncio()
    async def test_hallucination_check_runs_before_ratio_check(self) -> None:
        """The stop-list catches degenerate output first; ratio check
        only fires for transcripts that PASSED the stop-list."""
        # Empty string is in the stop-list — must reject as
        # hallucination, NOT compression_ratio.
        stt, mock_mv = _build_stt(completed_text="")
        with patch.dict("sys.modules", {"moonshine_voice": mock_mv}):
            await stt.initialize()
            result = await stt.transcribe(np.zeros(16_000, dtype=np.float32))
        assert result.rejection_reason == "hallucination_stoplist"

    @pytest.mark.asyncio()
    async def test_response_event_not_emitted_on_rejection(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """When transcript is rejected, the success-path
        ``voice.stt.response`` event must NOT fire (would mislead
        dashboards)."""
        import logging

        caplog.set_level(logging.INFO, logger=_STT_LOGGER)
        stt, mock_mv = _build_stt(completed_text="thank you")
        with patch.dict("sys.modules", {"moonshine_voice": mock_mv}):
            await stt.initialize()
            await stt.transcribe(np.zeros(16_000, dtype=np.float32))
        rejected = _stt_events_of(caplog, "voice.stt.transcript_rejected")
        assert len(rejected) == 1
        responses = _stt_events_of(caplog, "voice.stt.response")
        assert len(responses) == 0


class TestS1Constants:
    """Public-surface tuning values must not drift silently."""

    def test_compression_ratio_threshold(self) -> None:
        # Whisper canonical from openai/whisper transcribe.py.
        assert _COMPRESSION_RATIO_THRESHOLD == 2.4  # noqa: PLR2004

    def test_min_len_for_ratio_check(self) -> None:
        assert _HALLUCINATION_MIN_LEN_FOR_RATIO_CHECK == 32  # noqa: PLR2004
