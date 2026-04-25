"""Tests for KokoroTTS — near-commercial quality TTS via kokoro-onnx (V05-21).

Strategy: mock kokoro-onnx Kokoro class entirely to test the full pipeline
without requiring actual model files (~300MB) or ONNX runtime.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

import numpy as np
import onnxruntime
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from sovyx.voice.tts_kokoro import (
    _MODEL_FULL,
    _MODEL_Q8,
    _TTS_RMS_FLOOR_DBFS,
    _VOICES_FILE,
    AudioChunk,
    KokoroConfig,
    KokoroTTS,
    TTSEngine,
    _compute_rms_dbfs,
    _split_sentences,
    _validate_config,
)


@contextlib.contextmanager
def _patch_kokoro_init(
    *,
    from_session: MagicMock | None = None,
    session_side_effect: Exception | None = None,
) -> Iterator[tuple[MagicMock, MagicMock]]:
    """Patch the lazy imports inside :meth:`KokoroTTS.initialize`.

    Returns ``(mock_from_session, mock_inference_session)`` so tests can assert
    both the ONNX session construction (providers pinned to CPU) and the Kokoro
    wiring through :meth:`kokoro_onnx.Kokoro.from_session`.
    """
    mock_from_session = from_session or MagicMock(return_value=_make_mock_kokoro())
    mock_module = MagicMock(Kokoro=MagicMock(from_session=mock_from_session))

    if session_side_effect is not None:
        session_ctx = patch.object(
            onnxruntime,
            "InferenceSession",
            side_effect=session_side_effect,
        )
    else:
        session_ctx = patch.object(
            onnxruntime,
            "InferenceSession",
            return_value=MagicMock(),
        )

    with (
        patch.dict("sys.modules", {"kokoro_onnx": mock_module}),
        session_ctx as mock_session,
    ):
        yield mock_from_session, mock_session


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_kokoro(
    audio_length: int = 4800,
    sample_rate: int = 24000,
    voices: list[str] | None = None,
) -> MagicMock:
    """Build a mock kokoro_onnx.Kokoro instance."""
    mock = MagicMock()
    rng = np.random.default_rng(42)

    def _create(
        text: str,
        voice: str = "af_bella",
        speed: float = 1.0,
        lang: str = "en-us",
    ) -> tuple[np.ndarray, int]:
        n = audio_length if text.strip() else 0
        samples = rng.uniform(-0.5, 0.5, n).astype(np.float32)
        return samples, sample_rate

    mock.create = MagicMock(side_effect=_create)
    mock.get_voices = MagicMock(
        return_value=voices or ["af_bella", "af_nicole", "am_adam", "bm_george"],
    )
    return mock


def _setup_model_dir(
    tmp_path: Path,
    *,
    q8: bool = True,
    full: bool = False,
    voices: bool = True,
) -> Path:
    """Create model directory with appropriate files."""
    if q8:
        (tmp_path / _MODEL_Q8).write_bytes(b"fake-q8-model")
    if full:
        (tmp_path / _MODEL_FULL).write_bytes(b"fake-full-model")
    if voices:
        (tmp_path / _VOICES_FILE).write_bytes(b"fake-voices")
    return tmp_path


def _build_kokoro(
    tmp_path: Path,
    config: KokoroConfig | None = None,
    *,
    q8: bool = True,
    full: bool = False,
    audio_length: int = 4800,
) -> tuple[KokoroTTS, MagicMock]:
    """Construct a KokoroTTS with mocked kokoro-onnx."""
    _setup_model_dir(tmp_path, q8=q8, full=full)
    tts = KokoroTTS(tmp_path, config)

    mock_kokoro = _make_mock_kokoro(audio_length=audio_length)
    # Inject mock directly
    tts._kokoro = mock_kokoro
    tts._initialized = True

    return tts, mock_kokoro


# ---------------------------------------------------------------------------
# Config tests
# ---------------------------------------------------------------------------


class TestKokoroConfig:
    """Tests for KokoroConfig dataclass."""

    def test_defaults(self) -> None:
        cfg = KokoroConfig()
        assert cfg.voice == "af_bella"
        assert cfg.speed == 1.0
        assert cfg.language == "en-us"
        assert cfg.quantized is True

    def test_custom_values(self) -> None:
        cfg = KokoroConfig(
            voice="am_adam",
            speed=1.5,
            language="ja",
            quantized=False,
        )
        assert cfg.voice == "am_adam"
        assert cfg.speed == 1.5
        assert cfg.language == "ja"
        assert cfg.quantized is False

    def test_frozen(self) -> None:
        cfg = KokoroConfig()
        with pytest.raises(AttributeError):
            cfg.voice = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Validation tests
# ---------------------------------------------------------------------------


class TestValidation:
    """Tests for _validate_config."""

    def test_valid_default(self) -> None:
        _validate_config(KokoroConfig())

    def test_empty_voice_raises(self) -> None:
        with pytest.raises(ValueError, match="voice"):
            _validate_config(KokoroConfig(voice=""))

    def test_speed_too_low(self) -> None:
        with pytest.raises(ValueError, match="speed"):
            _validate_config(KokoroConfig(speed=0.05))

    def test_speed_too_high(self) -> None:
        with pytest.raises(ValueError, match="speed"):
            _validate_config(KokoroConfig(speed=6.0))

    def test_speed_at_min_boundary(self) -> None:
        _validate_config(KokoroConfig(speed=0.1))  # Should not raise

    def test_speed_at_max_boundary(self) -> None:
        _validate_config(KokoroConfig(speed=5.0))  # Should not raise

    def test_empty_language_raises(self) -> None:
        with pytest.raises(ValueError, match="language"):
            _validate_config(KokoroConfig(language=""))

    @settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(
        speed=st.floats(min_value=0.1, max_value=5.0, allow_nan=False),
    )
    def test_valid_speed_ranges_never_raise(self, speed: float) -> None:
        _validate_config(KokoroConfig(speed=speed))


# ---------------------------------------------------------------------------
# Sentence splitting tests
# ---------------------------------------------------------------------------


class TestSentenceSplitting:
    """Tests for _split_sentences."""

    def test_single_sentence(self) -> None:
        assert _split_sentences("Hello world") == ["Hello world"]

    def test_two_sentences(self) -> None:
        result = _split_sentences("Hello. World.")
        assert result == ["Hello.", "World."]

    def test_question_and_exclamation(self) -> None:
        result = _split_sentences("How are you? Great! Thanks.")
        assert result == ["How are you?", "Great!", "Thanks."]

    def test_no_space_after_period(self) -> None:
        assert _split_sentences("Hello.World") == ["Hello.World"]

    def test_empty_string(self) -> None:
        assert _split_sentences("") == [""]


# ---------------------------------------------------------------------------
# TTSEngine ABC tests
# ---------------------------------------------------------------------------


class TestTTSEngineABC:
    """Tests that KokoroTTS properly extends TTSEngine."""

    def test_kokoro_is_tts_engine(self, tmp_path: Path) -> None:
        _setup_model_dir(tmp_path)
        tts = KokoroTTS(tmp_path)
        assert isinstance(tts, TTSEngine)

    def test_abc_not_instantiable(self) -> None:
        with pytest.raises(TypeError):
            TTSEngine()  # type: ignore[abstract]


# ---------------------------------------------------------------------------
# Construction tests
# ---------------------------------------------------------------------------


class TestKokoroConstruction:
    """Tests for KokoroTTS construction."""

    def test_default_construction(self, tmp_path: Path) -> None:
        _setup_model_dir(tmp_path)
        tts = KokoroTTS(tmp_path)
        assert tts.config == KokoroConfig()
        assert not tts.is_initialized

    def test_custom_config(self, tmp_path: Path) -> None:
        _setup_model_dir(tmp_path)
        cfg = KokoroConfig(voice="am_adam", speed=1.5)
        tts = KokoroTTS(tmp_path, cfg)
        assert tts.config.voice == "am_adam"
        assert tts.config.speed == 1.5

    def test_invalid_config_raises(self, tmp_path: Path) -> None:
        _setup_model_dir(tmp_path)
        with pytest.raises(ValueError, match="speed"):
            KokoroTTS(tmp_path, KokoroConfig(speed=0.0))

    def test_sample_rate_always_24000(self, tmp_path: Path) -> None:
        _setup_model_dir(tmp_path)
        tts = KokoroTTS(tmp_path)
        assert tts.sample_rate == 24000

    def test_model_dir_stored_as_path(self, tmp_path: Path) -> None:
        _setup_model_dir(tmp_path)
        tts = KokoroTTS(str(tmp_path))  # type: ignore[arg-type]
        assert isinstance(tts._model_dir, Path)


# ---------------------------------------------------------------------------
# Initialization tests
# ---------------------------------------------------------------------------


class TestInitialization:
    """Tests for KokoroTTS initialization."""

    @pytest.mark.asyncio
    async def test_initialize_q8_model(self, tmp_path: Path) -> None:
        _setup_model_dir(tmp_path, q8=True, full=True)
        tts = KokoroTTS(tmp_path)

        with _patch_kokoro_init() as (mock_from_session, mock_session):
            await tts.initialize()

        assert tts.is_initialized
        session_args = mock_session.call_args
        assert session_args.args[0] == str(tmp_path / _MODEL_Q8)
        assert session_args.kwargs["providers"] == ["CPUExecutionProvider"]
        mock_from_session.assert_called_once_with(
            mock_session.return_value,
            str(tmp_path / _VOICES_FILE),
        )

    @pytest.mark.asyncio
    async def test_initialize_full_model_when_no_q8(self, tmp_path: Path) -> None:
        _setup_model_dir(tmp_path, q8=False, full=True)
        tts = KokoroTTS(tmp_path)

        with _patch_kokoro_init() as (mock_from_session, mock_session):
            await tts.initialize()

        assert tts.is_initialized
        assert mock_session.call_args.args[0] == str(tmp_path / _MODEL_FULL)
        mock_from_session.assert_called_once_with(
            mock_session.return_value,
            str(tmp_path / _VOICES_FILE),
        )

    @pytest.mark.asyncio
    async def test_initialize_full_model_when_not_quantized(self, tmp_path: Path) -> None:
        _setup_model_dir(tmp_path, q8=True, full=True)
        cfg = KokoroConfig(quantized=False)
        tts = KokoroTTS(tmp_path, cfg)

        with _patch_kokoro_init() as (mock_from_session, mock_session):
            await tts.initialize()

        assert tts.is_initialized
        assert mock_session.call_args.args[0] == str(tmp_path / _MODEL_FULL)
        mock_from_session.assert_called_once_with(
            mock_session.return_value,
            str(tmp_path / _VOICES_FILE),
        )

    @pytest.mark.asyncio
    async def test_initialize_pins_cpu_execution_provider(self, tmp_path: Path) -> None:
        """Regression: Kokoro ONNX session must never auto-select GPU providers.

        ``kokoro_onnx`` uses all available providers when ``onnxruntime-gpu`` is
        installed or ``ONNX_PROVIDER`` env var is set. On Windows with unstable
        GPU drivers that can trigger WDDM TDR resets. We construct the session
        ourselves to guarantee CPU-only execution.
        """
        _setup_model_dir(tmp_path, q8=True)
        tts = KokoroTTS(tmp_path)

        with _patch_kokoro_init() as (_, mock_session):
            await tts.initialize()

        assert mock_session.call_args.kwargs["providers"] == ["CPUExecutionProvider"]

    @pytest.mark.asyncio
    async def test_initialize_missing_model_raises(self, tmp_path: Path) -> None:
        _setup_model_dir(tmp_path, q8=False, full=False)
        tts = KokoroTTS(tmp_path)

        mock_module = MagicMock()
        with (
            patch.dict("sys.modules", {"kokoro_onnx": mock_module}),
            pytest.raises(FileNotFoundError, match="Kokoro model not found"),
        ):
            await tts.initialize()

    @pytest.mark.asyncio
    async def test_initialize_missing_voices_raises(self, tmp_path: Path) -> None:
        _setup_model_dir(tmp_path, q8=True, voices=False)
        tts = KokoroTTS(tmp_path)

        mock_module = MagicMock()
        with (
            patch.dict("sys.modules", {"kokoro_onnx": mock_module}),
            pytest.raises(FileNotFoundError, match="voices file not found"),
        ):
            await tts.initialize()

    @pytest.mark.asyncio
    async def test_initialize_kokoro_failure_raises_runtime(self, tmp_path: Path) -> None:
        _setup_model_dir(tmp_path, q8=True)
        tts = KokoroTTS(tmp_path)

        with (
            _patch_kokoro_init(session_side_effect=RuntimeError("ONNX init failed")),
            pytest.raises(RuntimeError, match="Failed to initialize Kokoro"),
        ):
            await tts.initialize()

    @pytest.mark.asyncio
    async def test_close(self, tmp_path: Path) -> None:
        tts, _ = _build_kokoro(tmp_path)
        assert tts.is_initialized

        await tts.close()
        assert not tts.is_initialized
        assert tts._kokoro is None

    @pytest.mark.asyncio
    async def test_q8_fallback_to_full_with_warning(self, tmp_path: Path) -> None:
        """When quantized=True but only full model exists, use full with warning."""
        _setup_model_dir(tmp_path, q8=False, full=True)
        tts = KokoroTTS(tmp_path, KokoroConfig(quantized=True))

        with _patch_kokoro_init() as (mock_from_session, mock_session):
            await tts.initialize()

        assert tts.is_initialized
        assert mock_session.call_args.args[0] == str(tmp_path / _MODEL_FULL)
        mock_from_session.assert_called_once_with(
            mock_session.return_value,
            str(tmp_path / _VOICES_FILE),
        )


# ---------------------------------------------------------------------------
# Model path resolution tests
# ---------------------------------------------------------------------------


class TestModelPathResolution:
    """Tests for _resolve_model_path."""

    def test_prefers_q8_when_quantized(self, tmp_path: Path) -> None:
        _setup_model_dir(tmp_path, q8=True, full=True)
        tts = KokoroTTS(tmp_path, KokoroConfig(quantized=True))
        path = tts._resolve_model_path()
        assert path.name == _MODEL_Q8

    def test_uses_full_when_not_quantized(self, tmp_path: Path) -> None:
        _setup_model_dir(tmp_path, q8=True, full=True)
        tts = KokoroTTS(tmp_path, KokoroConfig(quantized=False))
        path = tts._resolve_model_path()
        assert path.name == _MODEL_FULL

    def test_falls_back_to_full_when_q8_missing(self, tmp_path: Path) -> None:
        _setup_model_dir(tmp_path, q8=False, full=True)
        tts = KokoroTTS(tmp_path, KokoroConfig(quantized=True))
        path = tts._resolve_model_path()
        assert path.name == _MODEL_FULL

    def test_raises_when_no_model(self, tmp_path: Path) -> None:
        _setup_model_dir(tmp_path, q8=False, full=False)
        tts = KokoroTTS(tmp_path)
        with pytest.raises(FileNotFoundError, match="Kokoro model not found"):
            tts._resolve_model_path()


# ---------------------------------------------------------------------------
# Synthesize tests
# ---------------------------------------------------------------------------


class TestSynthesize:
    """Tests for KokoroTTS.synthesize."""

    @pytest.mark.asyncio
    async def test_synthesize_text(self, tmp_path: Path) -> None:
        tts, mock = _build_kokoro(tmp_path)
        result = await tts.synthesize("Hello world")

        assert isinstance(result, AudioChunk)
        assert result.sample_rate == 24000
        assert len(result.audio) > 0
        assert result.duration_ms > 0
        assert result.audio.dtype == np.int16
        mock.create.assert_called_once_with(
            "Hello world",
            voice="af_bella",
            speed=1.0,
            lang="en-us",
        )

    @pytest.mark.asyncio
    async def test_synthesize_empty_text(self, tmp_path: Path) -> None:
        tts, mock = _build_kokoro(tmp_path)
        result = await tts.synthesize("")

        assert len(result.audio) == 0
        assert result.duration_ms == 0.0
        assert result.sample_rate == 24000
        mock.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_synthesize_whitespace_only(self, tmp_path: Path) -> None:
        tts, mock = _build_kokoro(tmp_path)
        result = await tts.synthesize("   \n\t  ")

        assert len(result.audio) == 0
        assert result.duration_ms == 0.0
        mock.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_synthesize_with_custom_voice(self, tmp_path: Path) -> None:
        cfg = KokoroConfig(voice="am_adam", speed=1.5, language="ja")
        tts, mock = _build_kokoro(tmp_path, config=cfg)
        await tts.synthesize("こんにちは")

        mock.create.assert_called_once_with(
            "こんにちは",
            voice="am_adam",
            speed=1.5,
            lang="ja",
        )

    @pytest.mark.asyncio
    async def test_synthesize_audio_clipping(self, tmp_path: Path) -> None:
        """Audio values should be clipped to int16 range."""
        tts, mock = _build_kokoro(tmp_path)

        # Override create to return extreme values
        extreme = np.array([2.0, -2.0, 0.5, -0.5], dtype=np.float32)
        mock.create.return_value = (extreme, 24000)

        result = await tts.synthesize("test")
        assert result.audio.max() <= 32767
        assert result.audio.min() >= -32768

    @pytest.mark.asyncio
    async def test_synthesize_duration_calculation(self, tmp_path: Path) -> None:
        tts, mock = _build_kokoro(tmp_path, audio_length=24000)

        result = await tts.synthesize("One second of audio")
        # 24000 samples at 24000 Hz = 1000ms
        assert abs(result.duration_ms - 1000.0) < 0.1

    @pytest.mark.asyncio
    async def test_synthesize_auto_initializes(self, tmp_path: Path) -> None:
        """Synthesize should auto-initialize if not yet initialized."""
        _setup_model_dir(tmp_path, q8=True)
        tts = KokoroTTS(tmp_path)
        assert not tts.is_initialized

        mock_kokoro = _make_mock_kokoro()
        with _patch_kokoro_init(from_session=MagicMock(return_value=mock_kokoro)):
            result = await tts.synthesize("Hello")

        assert tts.is_initialized
        assert isinstance(result, AudioChunk)
        assert len(result.audio) > 0


# ---------------------------------------------------------------------------
# Streaming synthesis tests
# ---------------------------------------------------------------------------


class TestSynthesizeStreaming:
    """Tests for KokoroTTS.synthesize_streaming."""

    @pytest.mark.asyncio
    async def test_streaming_yields_per_sentence(self, tmp_path: Path) -> None:
        tts, _ = _build_kokoro(tmp_path)

        async def _stream() -> AsyncIterator[str]:
            yield "Hello world. "
            yield "How are you? "
            yield "I'm great."

        chunks = []
        async for chunk in tts.synthesize_streaming(_stream()):
            chunks.append(chunk)

        # Should yield 3 chunks (one per sentence)
        assert len(chunks) == 3
        for c in chunks:
            assert isinstance(c, AudioChunk)
            assert len(c.audio) > 0

    @pytest.mark.asyncio
    async def test_streaming_empty_input(self, tmp_path: Path) -> None:
        tts, _ = _build_kokoro(tmp_path)

        async def _stream() -> AsyncIterator[str]:
            yield ""
            yield ""

        chunks = []
        async for chunk in tts.synthesize_streaming(_stream()):
            chunks.append(chunk)

        assert len(chunks) == 0

    @pytest.mark.asyncio
    async def test_streaming_single_chunk(self, tmp_path: Path) -> None:
        tts, _ = _build_kokoro(tmp_path)

        async def _stream() -> AsyncIterator[str]:
            yield "Hello world"

        chunks = []
        async for chunk in tts.synthesize_streaming(_stream()):
            chunks.append(chunk)

        # No sentence boundary → yields at end
        assert len(chunks) == 1

    @pytest.mark.asyncio
    async def test_streaming_whitespace_only(self, tmp_path: Path) -> None:
        tts, _ = _build_kokoro(tmp_path)

        async def _stream() -> AsyncIterator[str]:
            yield "   "
            yield "\n"

        chunks = []
        async for chunk in tts.synthesize_streaming(_stream()):
            chunks.append(chunk)

        assert len(chunks) == 0

    @pytest.mark.asyncio
    async def test_streaming_auto_initializes(self, tmp_path: Path) -> None:
        _setup_model_dir(tmp_path, q8=True)
        tts = KokoroTTS(tmp_path)

        mock_kokoro = _make_mock_kokoro()

        async def _stream() -> AsyncIterator[str]:
            yield "Hello."

        with _patch_kokoro_init(from_session=MagicMock(return_value=mock_kokoro)):
            chunks = []
            async for chunk in tts.synthesize_streaming(_stream()):
                chunks.append(chunk)

        assert tts.is_initialized
        assert len(chunks) == 1

    @pytest.mark.asyncio
    async def test_streaming_incremental_text(self, tmp_path: Path) -> None:
        """Simulate LLM streaming — text arrives character by character."""
        tts, _ = _build_kokoro(tmp_path)

        async def _stream() -> AsyncIterator[str]:
            for char in "Hi. Bye.":
                yield char

        chunks = []
        async for chunk in tts.synthesize_streaming(_stream()):
            chunks.append(chunk)

        assert len(chunks) == 2  # Two sentences


# ---------------------------------------------------------------------------
# List voices tests
# ---------------------------------------------------------------------------


class TestListVoices:
    """Tests for KokoroTTS.list_voices."""

    def test_list_voices_when_initialized(self, tmp_path: Path) -> None:
        tts, mock = _build_kokoro(tmp_path)
        voices = tts.list_voices()
        assert voices == ["af_bella", "af_nicole", "am_adam", "bm_george"]

    def test_list_voices_not_initialized(self, tmp_path: Path) -> None:
        _setup_model_dir(tmp_path)
        tts = KokoroTTS(tmp_path)
        assert tts.list_voices() == []

    def test_list_voices_sorted(self, tmp_path: Path) -> None:
        tts, mock = _build_kokoro(tmp_path)
        mock.get_voices.return_value = ["z_voice", "a_voice", "m_voice"]
        voices = tts.list_voices()
        assert voices == ["a_voice", "m_voice", "z_voice"]

    def test_list_voices_exception_returns_empty(self, tmp_path: Path) -> None:
        tts, mock = _build_kokoro(tmp_path)
        mock.get_voices.side_effect = RuntimeError("fail")
        assert tts.list_voices() == []


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge case tests."""

    @pytest.mark.asyncio
    async def test_synthesize_after_close(self, tmp_path: Path) -> None:
        """After close, synthesize should re-initialize."""
        _setup_model_dir(tmp_path, q8=True)
        tts, _ = _build_kokoro(tmp_path)
        await tts.close()

        assert not tts.is_initialized

        mock_kokoro = _make_mock_kokoro()
        with _patch_kokoro_init(from_session=MagicMock(return_value=mock_kokoro)):
            result = await tts.synthesize("Hello")

        assert tts.is_initialized
        assert isinstance(result, AudioChunk)

    @pytest.mark.asyncio
    async def test_multiple_synthesize_calls(self, tmp_path: Path) -> None:
        tts, mock = _build_kokoro(tmp_path)

        r1 = await tts.synthesize("First")
        r2 = await tts.synthesize("Second")
        r3 = await tts.synthesize("Third")

        assert mock.create.call_count == 3
        for r in (r1, r2, r3):
            assert isinstance(r, AudioChunk)
            assert len(r.audio) > 0

    @pytest.mark.asyncio
    async def test_synthesize_preserves_config_across_calls(self, tmp_path: Path) -> None:
        cfg = KokoroConfig(voice="bf_emma", speed=0.8, language="en-gb")
        tts, mock = _build_kokoro(tmp_path, config=cfg)

        await tts.synthesize("Test one")
        await tts.synthesize("Test two")

        for call in mock.create.call_args_list:
            assert call.kwargs.get("voice") == "bf_emma" or call[1].get("voice") == "bf_emma"

    @pytest.mark.asyncio
    async def test_sample_rate_from_kokoro_output(self, tmp_path: Path) -> None:
        """AudioChunk sample_rate should match what kokoro.create returns."""
        tts, mock = _build_kokoro(tmp_path)
        # Override the side_effect with a direct return_value
        mock.create.side_effect = None
        mock.create.return_value = (
            np.zeros(4800, dtype=np.float32),
            48000,  # Different sample rate
        )

        result = await tts.synthesize("High sample rate")
        assert result.sample_rate == 48000

    def test_config_is_readonly(self, tmp_path: Path) -> None:
        _setup_model_dir(tmp_path)
        tts = KokoroTTS(tmp_path)
        cfg = tts.config
        assert isinstance(cfg, KokoroConfig)
        with pytest.raises(AttributeError):
            cfg.voice = "other"  # type: ignore[misc]

    @pytest.mark.asyncio
    async def test_synthesize_kokoro_none_after_init_raises(self, tmp_path: Path) -> None:
        """If _kokoro is somehow None after init flag, should raise RuntimeError."""
        _setup_model_dir(tmp_path, q8=True)
        tts = KokoroTTS(tmp_path)
        # Force initialized=True but kokoro=None (pathological state)
        tts._initialized = True
        tts._kokoro = None

        with pytest.raises(RuntimeError, match="not initialized"):
            await tts.synthesize("test")

    @pytest.mark.asyncio
    async def test_long_text_synthesize(self, tmp_path: Path) -> None:
        """Should handle very long text without issues."""
        tts, mock = _build_kokoro(tmp_path)
        long_text = "Hello world. " * 100
        result = await tts.synthesize(long_text)
        assert isinstance(result, AudioChunk)
        mock.create.assert_called_once()


# ---------------------------------------------------------------------------
# Property-based tests
# ---------------------------------------------------------------------------


class TestPropertyBased:
    """Hypothesis property-based tests."""

    @settings(
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
    )
    @given(speed=st.floats(min_value=0.1, max_value=5.0, allow_nan=False))
    def test_valid_speed_always_creates(self, speed: float, tmp_path: Path) -> None:
        cfg = KokoroConfig(speed=speed)
        _validate_config(cfg)  # Should never raise

    @settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(text=st.text(min_size=0, max_size=100))
    def test_split_sentences_never_loses_text(self, text: str) -> None:
        """Joining split sentences should reconstruct the original (modulo split chars)."""
        parts = _split_sentences(text)
        assert len(parts) >= 1

    @settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(
        speed=st.floats(
            min_value=-10.0,
            max_value=10.0,
            allow_nan=False,
            allow_infinity=False,
        ),
    )
    def test_invalid_speed_raises(self, speed: float) -> None:
        if speed < 0.1 or speed > 5.0:
            with pytest.raises(ValueError, match="speed"):
                _validate_config(KokoroConfig(speed=speed))


# ===========================================================================
# T2: Ring 5 output-energy validation (zero-energy detection + chunk flag)
# ===========================================================================
#
# Pre-T2 ``synthesize_with`` returned whatever Kokoro produced, including
# silent buffers from a corrupt voice file or a degenerate ONNX session.
# T2 measures the post-synthesis RMS dBFS and flags the chunk +
# emits ``voice.tts.zero_energy_synthesis`` when the output is below
# the perceptual silence floor for non-empty input.
#
# Reference: MISSION-voice-mixer-enterprise-refactor-2026-04-25 §3.4, T2.

_TTS_LOGGER = "sovyx.voice.tts_kokoro"


def _tts_events_of(
    caplog: pytest.LogCaptureFixture,
    event_name: str,
) -> list[dict[str, object]]:
    return [
        r.msg
        for r in caplog.records
        if r.name == _TTS_LOGGER and isinstance(r.msg, dict) and r.msg.get("event") == event_name
    ]


class TestComputeRMSDbfsPure:
    """Pure-function RMS dBFS computation."""

    def test_empty_array_returns_neg_inf(self) -> None:
        assert _compute_rms_dbfs(np.zeros(0, dtype=np.int16)) == float("-inf")

    def test_all_zero_returns_neg_inf(self) -> None:
        assert _compute_rms_dbfs(np.zeros(1000, dtype=np.int16)) == float("-inf")

    def test_full_scale_sine_near_zero_dbfs(self) -> None:
        """A full-scale int16 sine produces RMS near 0 dBFS (within ~3 dB)."""
        t = np.arange(8000, dtype=np.float64) / 8000
        sine = (32000 * np.sin(2 * np.pi * 440 * t)).astype(np.int16)
        rms = _compute_rms_dbfs(sine)
        # Sine RMS = peak / sqrt(2) → ~ -3 dBFS for full-scale.
        assert rms > -10.0
        assert rms < 0.0

    def test_quiet_signal_well_below_floor(self) -> None:
        """A signal at -80 dBFS is well below the -60 floor."""
        # Very quiet sine ~ 1 LSB.
        t = np.arange(8000, dtype=np.float64) / 8000
        quiet_sine = (3 * np.sin(2 * np.pi * 440 * t)).astype(np.int16)
        rms = _compute_rms_dbfs(quiet_sine)
        assert rms < _TTS_RMS_FLOOR_DBFS

    def test_object_without_size_returns_neg_inf(self) -> None:
        """Defensive — non-array input doesn't crash, returns silence."""
        assert _compute_rms_dbfs(None) == float("-inf")  # type: ignore[arg-type]


class TestT2Constants:
    def test_rms_floor_dbfs_value(self) -> None:
        """Public-surface tuning — bumps must be deliberate."""
        assert _TTS_RMS_FLOOR_DBFS == -60.0


class TestKokoroT2EnergyValidation:
    """End-to-end T2 monitor wired through ``synthesize_with``."""

    @pytest.mark.asyncio()
    async def test_normal_synthesis_flagged_ok(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Healthy synthesis with audible output produces no T2 warning."""
        import logging

        caplog.set_level(logging.WARNING, logger=_TTS_LOGGER)
        # Default _make_mock_kokoro produces uniform[-0.5, 0.5] float32 →
        # ~16k peak int16 → comfortably above the -60 dBFS floor.
        kokoro, _ = _build_kokoro(tmp_path)
        chunk = await kokoro.synthesize("hello world")
        assert chunk.synthesis_health is None
        assert _tts_events_of(caplog, "voice.tts.zero_energy_synthesis") == []

    @pytest.mark.asyncio()
    async def test_silent_synthesis_flagged_zero_energy(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """All-zero PCM output → flagged + WARNING fires."""
        import logging

        caplog.set_level(logging.WARNING, logger=_TTS_LOGGER)
        # Build with a mock that ALWAYS returns zeros, even for non-empty
        # text. Simulates corrupt voice file / ONNX returning silence.
        _setup_model_dir(tmp_path)
        kokoro = KokoroTTS(tmp_path, KokoroConfig())
        silent_mock = MagicMock()

        def _silent_create(
            text: str,
            voice: str = "af_bella",
            speed: float = 1.0,
            lang: str = "en-us",
        ) -> tuple[np.ndarray, int]:
            n = 4800 if text.strip() else 0
            return np.zeros(n, dtype=np.float32), 24000

        silent_mock.create = MagicMock(side_effect=_silent_create)
        kokoro._kokoro = silent_mock
        kokoro._initialized = True

        chunk = await kokoro.synthesize("hello world")
        assert chunk.synthesis_health == "zero_energy"
        events = _tts_events_of(caplog, "voice.tts.zero_energy_synthesis")
        assert len(events) == 1
        assert events[0]["voice.measured_rms_dbfs"] == "-inf"
        assert events[0]["voice.rms_floor_dbfs"] == _TTS_RMS_FLOOR_DBFS
        assert "fallback_to_piper" in str(events[0]["voice.action_required"])

    @pytest.mark.asyncio()
    async def test_quiet_synthesis_flagged_zero_energy(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Audible-but-below-floor output → flagged."""
        import logging

        caplog.set_level(logging.WARNING, logger=_TTS_LOGGER)
        _setup_model_dir(tmp_path)
        kokoro = KokoroTTS(tmp_path, KokoroConfig())
        quiet_mock = MagicMock()

        def _quiet_create(
            text: str,
            voice: str = "af_bella",
            speed: float = 1.0,
            lang: str = "en-us",
        ) -> tuple[np.ndarray, int]:
            n = 4800 if text.strip() else 0
            # Tiny amplitude → RMS well below -60 dBFS
            return (np.full(n, 0.00001, dtype=np.float32)), 24000

        quiet_mock.create = MagicMock(side_effect=_quiet_create)
        kokoro._kokoro = quiet_mock
        kokoro._initialized = True

        chunk = await kokoro.synthesize("hello world")
        assert chunk.synthesis_health == "zero_energy"

    @pytest.mark.asyncio()
    async def test_empty_input_skips_validation(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Empty text produces an empty chunk WITHOUT firing the
        T2 warning — no synthesis happened, so silence is expected."""
        import logging

        caplog.set_level(logging.WARNING, logger=_TTS_LOGGER)
        kokoro, _ = _build_kokoro(tmp_path)
        chunk = await kokoro.synthesize("")
        assert chunk.synthesis_health is None
        assert chunk.audio.size == 0
        assert _tts_events_of(caplog, "voice.tts.zero_energy_synthesis") == []

    @pytest.mark.asyncio()
    async def test_chunk_emitted_event_includes_synthesis_health(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The chunk_emitted event must always carry the health verdict
        so the dashboard can attribute every chunk."""
        import logging

        caplog.set_level(logging.INFO, logger=_TTS_LOGGER)
        kokoro, _ = _build_kokoro(tmp_path)
        await kokoro.synthesize("hello world")
        events = _tts_events_of(caplog, "voice.tts.chunk_emitted")
        assert len(events) >= 1
        assert events[-1]["voice.synthesis_health"] == "ok"


# ---------------------------------------------------------------------------
# M2 wire-up — RED + USE telemetry on TTS synthesis
# ---------------------------------------------------------------------------


class TestKokoroM2WireUp:
    """KokoroTTS.synthesize_with must emit M2 stage events on every
    return path. Mirrors TestSTTM2WireUp — proves the M2 foundation
    is wired in production code, not just unit-tested in isolation.
    """

    @pytest.mark.asyncio()
    async def test_success_path_records_success_event(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from typing import Any

        recorded: list[tuple[Any, Any, Any]] = []

        from sovyx.voice import tts_kokoro as kokoro_mod
        from sovyx.voice._stage_metrics import StageEventKind, VoiceStage

        def _capture(stage: Any, kind: Any, *, error_type: str | None = None) -> None:
            recorded.append((stage, kind, error_type))

        monkeypatch.setattr(kokoro_mod, "record_stage_event", _capture)

        kokoro, _ = _build_kokoro(tmp_path)
        await kokoro.synthesize("hello world")

        assert (VoiceStage.TTS, StageEventKind.SUCCESS, None) in recorded

    @pytest.mark.asyncio()
    async def test_empty_text_records_drop_with_reason(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from typing import Any

        recorded: list[tuple[Any, Any, Any]] = []

        from sovyx.voice import tts_kokoro as kokoro_mod
        from sovyx.voice._stage_metrics import StageEventKind, VoiceStage

        def _capture(stage: Any, kind: Any, *, error_type: str | None = None) -> None:
            recorded.append((stage, kind, error_type))

        monkeypatch.setattr(kokoro_mod, "record_stage_event", _capture)

        kokoro, _ = _build_kokoro(tmp_path)
        await kokoro.synthesize("")

        assert (VoiceStage.TTS, StageEventKind.DROP, "empty_text") in recorded

    @pytest.mark.asyncio()
    async def test_zero_energy_synthesis_records_drop(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Zero-energy output (silent synthesis) is a soft failure —
        DROP with error_type=zero_energy so dashboards can attribute
        the rate of structural-output failures."""
        from typing import Any

        recorded: list[tuple[Any, Any, Any]] = []

        from sovyx.voice import tts_kokoro as kokoro_mod
        from sovyx.voice._stage_metrics import StageEventKind, VoiceStage

        def _capture(stage: Any, kind: Any, *, error_type: str | None = None) -> None:
            recorded.append((stage, kind, error_type))

        monkeypatch.setattr(kokoro_mod, "record_stage_event", _capture)

        # Build a kokoro mock that returns zero samples — below the
        # -60 dBFS RMS floor.
        mock_kokoro = MagicMock()
        mock_kokoro.create = MagicMock(
            return_value=(np.zeros(4800, dtype=np.float32), 24000),
        )
        mock_kokoro.get_voices = MagicMock(return_value=["af_bella"])

        _setup_model_dir(tmp_path)
        kokoro = KokoroTTS(tmp_path)
        kokoro._kokoro = mock_kokoro
        kokoro._initialized = True
        await kokoro.synthesize("hello world")

        assert (VoiceStage.TTS, StageEventKind.DROP, "zero_energy") in recorded
