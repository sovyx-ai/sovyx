"""Tests for PiperTTS — VITS ONNX synthesis with espeak-ng phonemizer (V05-20).

Strategy: mock ONNX session and piper_phonemize to test the full pipeline
without requiring actual model files or espeak-ng installed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from sovyx.voice._tts_zero_energy import TTS_RMS_FLOOR_DBFS
from sovyx.voice.tts_piper import (
    AudioChunk,
    PiperConfig,
    PiperTTS,
    TTSEngine,
    _split_sentences,
    _validate_config,
)

# ---------------------------------------------------------------------------
# Test voice config (mimics a real Piper .onnx.json)
# ---------------------------------------------------------------------------

_VOICE_CONFIG: dict[str, Any] = {
    "audio": {"sample_rate": 22050},
    "num_speakers": 1,
    "espeak": {"voice": "en-us"},
    "phoneme_type": "espeak",
    "phoneme_id_map": {
        "_": [0],
        "^": [1],
        "$": [2],
        "h": [10],
        "ɛ": [11],
        "l": [12],
        "oʊ": [13],
        " ": [14],
        "w": [15],
        "ɜ": [16],
        "ɹ": [17],
        "d": [18],
    },
}

_MULTI_SPEAKER_CONFIG: dict[str, Any] = {
    **_VOICE_CONFIG,
    "num_speakers": 4,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_phonemize_module(
    return_value: list[list[str]],
) -> tuple[MagicMock, dict[str, MagicMock]]:
    """Create a mock piper_phonemize module and sys.modules patch dict."""
    fn = MagicMock(return_value=return_value)
    mod = MagicMock(phonemize_espeak=fn)
    return fn, {"piper_phonemize": mod}


def _make_mock_session(audio_length: int = 4410) -> MagicMock:
    """Build a mock ONNX session that returns synthetic audio."""
    session = MagicMock()

    def _run(
        _output_names: object,
        inputs: dict[str, Any],
    ) -> list[Any]:
        n = audio_length
        rng = np.random.default_rng(42)
        audio = rng.uniform(-0.5, 0.5, (1, 1, n)).astype(
            np.float32,
        )
        return [audio]

    session.run = _run
    return session


def _build_piper(
    tmp_path: Path,
    config: PiperConfig | None = None,
    voice_config: dict[str, Any] | None = None,
    audio_length: int = 4410,
) -> PiperTTS:
    """Construct a PiperTTS with mocked ONNX session."""
    cfg = config or PiperConfig()
    vc = voice_config or _VOICE_CONFIG

    config_path = tmp_path / f"{cfg.voice}.onnx.json"
    config_path.write_text(json.dumps(vc), encoding="utf-8")

    model_path = tmp_path / f"{cfg.voice}.onnx"
    model_path.write_bytes(b"dummy-onnx-model")

    piper = PiperTTS(model_dir=tmp_path, config=cfg)
    piper._session = _make_mock_session(audio_length)
    piper._voice_config = vc
    piper._initialized = True

    return piper


def _build_uninitialized_piper(
    tmp_path: Path,
    config: PiperConfig | None = None,
    voice_config: dict[str, Any] | None = None,
) -> PiperTTS:
    """Construct an uninitialized PiperTTS with files on disk."""
    cfg = config or PiperConfig()
    vc = voice_config or _VOICE_CONFIG

    config_path = tmp_path / f"{cfg.voice}.onnx.json"
    config_path.write_text(json.dumps(vc), encoding="utf-8")

    model_path = tmp_path / f"{cfg.voice}.onnx"
    model_path.write_bytes(b"dummy-onnx-model")

    return PiperTTS(model_dir=tmp_path, config=cfg)


async def _async_text_gen(*texts: str):  # noqa: ANN202
    """Helper async text stream generator."""
    for t in texts:
        yield t


def _tracking_run(
    tracker: dict[str, Any],
    audio_shape: tuple[int, ...] = (1, 1, 4410),
    fill: float | None = None,
) -> Any:  # noqa: ANN401
    """Create a tracking ONNX run function."""

    def _run(
        _names: object,
        inputs: dict[str, Any],
    ) -> list[Any]:
        tracker["inputs"] = inputs
        if fill is not None:
            audio = np.full(audio_shape, fill, dtype=np.float32)
        else:
            audio = np.zeros(audio_shape, dtype=np.float32)
        return [audio]

    return _run


# ---------------------------------------------------------------------------
# AudioChunk tests
# ---------------------------------------------------------------------------


class TestAudioChunk:
    """Tests for the AudioChunk dataclass."""

    def test_default_sample_rate(self) -> None:
        chunk = AudioChunk(audio=np.array([], dtype=np.int16))
        assert chunk.sample_rate == 22050

    def test_default_duration(self) -> None:
        chunk = AudioChunk(audio=np.array([], dtype=np.int16))
        assert chunk.duration_ms == 0.0

    def test_custom_values(self) -> None:
        audio = np.array([1, 2, 3], dtype=np.int16)
        chunk = AudioChunk(
            audio=audio,
            sample_rate=24000,
            duration_ms=100.0,
        )
        assert chunk.sample_rate == 24000
        assert chunk.duration_ms == 100.0
        assert len(chunk.audio) == 3

    def test_frozen(self) -> None:
        chunk = AudioChunk(audio=np.array([], dtype=np.int16))
        with pytest.raises(AttributeError):
            chunk.sample_rate = 44100  # type: ignore[misc]


# ---------------------------------------------------------------------------
# PiperConfig tests
# ---------------------------------------------------------------------------


class TestPiperConfig:
    """Tests for PiperConfig defaults and validation."""

    def test_defaults(self) -> None:
        cfg = PiperConfig()
        assert cfg.voice == "en_US-lessac-medium"
        assert cfg.noise_scale == 0.667
        assert cfg.length_scale == 1.0
        assert cfg.noise_w == 0.8
        assert cfg.sentence_silence == 0.3
        assert cfg.speaker_id is None

    def test_custom_values(self) -> None:
        cfg = PiperConfig(
            voice="en_GB-alan-low",
            noise_scale=0.5,
            length_scale=1.5,
            noise_w=0.6,
            sentence_silence=0.5,
            speaker_id=2,
        )
        assert cfg.voice == "en_GB-alan-low"
        assert cfg.speaker_id == 2


# ---------------------------------------------------------------------------
# Validation tests
# ---------------------------------------------------------------------------


class TestValidation:
    """Tests for _validate_config."""

    def test_valid_default(self) -> None:
        _validate_config(PiperConfig())

    def test_noise_scale_too_high(self) -> None:
        with pytest.raises(ValueError, match="noise_scale"):
            _validate_config(PiperConfig(noise_scale=3.0))

    def test_noise_scale_negative(self) -> None:
        with pytest.raises(ValueError, match="noise_scale"):
            _validate_config(PiperConfig(noise_scale=-0.1))

    def test_length_scale_zero(self) -> None:
        with pytest.raises(ValueError, match="length_scale"):
            _validate_config(PiperConfig(length_scale=0.0))

    def test_length_scale_too_high(self) -> None:
        with pytest.raises(ValueError, match="length_scale"):
            _validate_config(PiperConfig(length_scale=6.0))

    def test_noise_w_negative(self) -> None:
        with pytest.raises(ValueError, match="noise_w"):
            _validate_config(PiperConfig(noise_w=-0.5))

    def test_noise_w_too_high(self) -> None:
        with pytest.raises(ValueError, match="noise_w"):
            _validate_config(PiperConfig(noise_w=2.5))

    def test_sentence_silence_negative(self) -> None:
        with pytest.raises(ValueError, match="sentence_silence"):
            _validate_config(PiperConfig(sentence_silence=-1.0))

    def test_speaker_id_negative(self) -> None:
        with pytest.raises(ValueError, match="speaker_id"):
            _validate_config(PiperConfig(speaker_id=-1))

    def test_speaker_id_zero_valid(self) -> None:
        _validate_config(PiperConfig(speaker_id=0))

    def test_sentence_silence_zero_valid(self) -> None:
        _validate_config(PiperConfig(sentence_silence=0.0))

    @settings(
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(
        noise_scale=st.floats(0.0, 2.0, allow_nan=False),
        length_scale=st.floats(0.01, 5.0, allow_nan=False),
        noise_w=st.floats(0.0, 2.0, allow_nan=False),
    )
    def test_valid_ranges_never_raise(
        self,
        noise_scale: float,
        length_scale: float,
        noise_w: float,
    ) -> None:
        _validate_config(
            PiperConfig(
                noise_scale=noise_scale,
                length_scale=length_scale,
                noise_w=noise_w,
            ),
        )


# ---------------------------------------------------------------------------
# Sentence splitting tests
# ---------------------------------------------------------------------------


class TestSentenceSplitting:
    """Tests for _split_sentences helper."""

    def test_single_sentence(self) -> None:
        assert _split_sentences("Hello world") == ["Hello world"]

    def test_two_sentences(self) -> None:
        result = _split_sentences("Hello. World.")
        assert result == ["Hello.", "World."]

    def test_question_and_exclamation(self) -> None:
        result = _split_sentences("How are you? Great! Thanks.")
        assert result == ["How are you?", "Great!", "Thanks."]

    def test_no_space_after_period(self) -> None:
        result = _split_sentences("v1.0 is great")
        assert result == ["v1.0 is great"]

    def test_empty_string(self) -> None:
        assert _split_sentences("") == [""]

    def test_multiple_spaces(self) -> None:
        result = _split_sentences("Hello.   World.")
        assert result == ["Hello.", "World."]


# ---------------------------------------------------------------------------
# TTSEngine ABC tests
# ---------------------------------------------------------------------------


class TestTTSEngineABC:
    """Tests for the TTSEngine abstract base class."""

    def test_cannot_instantiate(self) -> None:
        with pytest.raises(TypeError):
            TTSEngine()  # type: ignore[abstract]

    def test_piper_is_tts_engine(self, tmp_path: Path) -> None:
        piper = _build_piper(tmp_path)
        assert isinstance(piper, TTSEngine)


# ---------------------------------------------------------------------------
# PiperTTS construction tests
# ---------------------------------------------------------------------------


class TestPiperConstruction:
    """Tests for PiperTTS.__init__ and properties."""

    def test_default_construction(self, tmp_path: Path) -> None:
        piper = PiperTTS(model_dir=tmp_path)
        assert piper.config == PiperConfig()
        assert not piper.is_initialized
        assert piper.sample_rate == 22050

    def test_custom_config(self, tmp_path: Path) -> None:
        cfg = PiperConfig(voice="test-voice", noise_scale=0.5)
        piper = PiperTTS(model_dir=tmp_path, config=cfg)
        assert piper.config.voice == "test-voice"
        assert piper.config.noise_scale == 0.5

    def test_invalid_config_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="noise_scale"):
            PiperTTS(
                model_dir=tmp_path,
                config=PiperConfig(noise_scale=5.0),
            )

    def test_num_speakers_before_init(self, tmp_path: Path) -> None:
        piper = PiperTTS(model_dir=tmp_path)
        assert piper.num_speakers == 1

    def test_num_speakers_after_init(self, tmp_path: Path) -> None:
        piper = _build_piper(
            tmp_path,
            voice_config=_MULTI_SPEAKER_CONFIG,
        )
        assert piper.num_speakers == 4

    def test_sample_rate_from_config(self, tmp_path: Path) -> None:
        piper = _build_piper(tmp_path)
        assert piper.sample_rate == 22050


# ---------------------------------------------------------------------------
# Initialization tests
# ---------------------------------------------------------------------------


class TestInitialization:
    """Tests for PiperTTS.initialize()."""

    @pytest.mark.asyncio
    async def test_initialize_success(
        self,
        tmp_path: Path,
    ) -> None:
        piper = _build_uninitialized_piper(tmp_path)

        mock_ort = MagicMock()
        mock_ort.SessionOptions.return_value = MagicMock()
        mock_ort.GraphOptimizationLevel.ORT_ENABLE_ALL = 99
        mock_ort.InferenceSession.return_value = MagicMock()

        with patch.dict(
            "sys.modules",
            {"onnxruntime": mock_ort},
        ):
            await piper.initialize()

        assert piper.is_initialized

    @pytest.mark.asyncio
    async def test_initialize_missing_model(
        self,
        tmp_path: Path,
    ) -> None:
        cfg = PiperConfig()
        config_path = tmp_path / f"{cfg.voice}.onnx.json"
        config_path.write_text(
            json.dumps(_VOICE_CONFIG),
            encoding="utf-8",
        )
        piper = PiperTTS(model_dir=tmp_path, config=cfg)

        with pytest.raises(FileNotFoundError, match="model not found"):
            await piper.initialize()

    @pytest.mark.asyncio
    async def test_initialize_missing_config(
        self,
        tmp_path: Path,
    ) -> None:
        cfg = PiperConfig()
        model_path = tmp_path / f"{cfg.voice}.onnx"
        model_path.write_bytes(b"dummy")
        piper = PiperTTS(model_dir=tmp_path, config=cfg)

        with pytest.raises(
            FileNotFoundError,
            match="config not found",
        ):
            await piper.initialize()

    @pytest.mark.asyncio
    async def test_close(self, tmp_path: Path) -> None:
        piper = _build_piper(tmp_path)
        assert piper.is_initialized

        await piper.close()
        assert not piper.is_initialized
        assert piper._session is None
        assert piper._voice_config is None


# ---------------------------------------------------------------------------
# Phonemization tests
# ---------------------------------------------------------------------------


class TestPhonemization:
    """Tests for PiperTTS._phonemize and _phonemes_to_ids."""

    def test_phonemize_not_initialized_raises(
        self,
        tmp_path: Path,
    ) -> None:
        piper = PiperTTS(model_dir=tmp_path)
        with pytest.raises(RuntimeError, match="not initialized"):
            piper._phonemize("hello")

    def test_phonemize_calls_espeak(
        self,
        tmp_path: Path,
    ) -> None:
        piper = _build_piper(tmp_path)

        fn, modules = _mock_phonemize_module(
            [["h", "ɛ", "l", "oʊ"]],
        )
        with patch.dict("sys.modules", modules):
            result = piper._phonemize("Hello")

        fn.assert_called_once_with("Hello", "en-us")
        assert result == [["h", "ɛ", "l", "oʊ"]]

    def test_phonemes_to_ids_basic(
        self,
        tmp_path: Path,
    ) -> None:
        piper = _build_piper(tmp_path)
        phonemes = ["h", "ɛ", "l", "oʊ"]
        ids = piper._phonemes_to_ids(phonemes)

        # BOS + (h+PAD + ɛ+PAD + l+PAD + oʊ+PAD) + EOS
        assert ids[0] == 1  # BOS = ^
        assert ids[-1] == 2  # EOS = $
        assert ids[1] == 10  # h
        assert ids[2] == 0  # PAD
        assert ids[3] == 11  # ɛ
        assert ids[4] == 0  # PAD
        assert ids[5] == 12  # l
        assert ids[6] == 0  # PAD
        assert ids[7] == 13  # oʊ
        assert ids[8] == 0  # PAD

    def test_phonemes_to_ids_unknown_skipped(
        self,
        tmp_path: Path,
    ) -> None:
        piper = _build_piper(tmp_path)
        ids = piper._phonemes_to_ids(["h", "UNKNOWN", "l"])
        assert ids == [1, 10, 0, 12, 0, 2]

    def test_phonemes_to_ids_empty(
        self,
        tmp_path: Path,
    ) -> None:
        piper = _build_piper(tmp_path)
        ids = piper._phonemes_to_ids([])
        assert ids == [1, 2]

    def test_phonemes_to_ids_not_initialized_raises(
        self,
        tmp_path: Path,
    ) -> None:
        piper = PiperTTS(model_dir=tmp_path)
        with pytest.raises(RuntimeError, match="not initialized"):
            piper._phonemes_to_ids(["h"])

    def test_phonemes_to_ids_truncation(
        self,
        tmp_path: Path,
    ) -> None:
        piper = _build_piper(tmp_path)
        phonemes = ["h"] * 30000
        ids = piper._phonemes_to_ids(phonemes)
        assert len(ids) <= 50000


# ---------------------------------------------------------------------------
# ONNX inference tests
# ---------------------------------------------------------------------------


class TestSynthesizeIds:
    """Tests for PiperTTS._synthesize_ids."""

    def test_basic_inference(self, tmp_path: Path) -> None:
        piper = _build_piper(tmp_path, audio_length=4410)
        ids = [1, 10, 0, 11, 0, 2]
        audio = piper._synthesize_ids(ids)

        assert audio.dtype == np.int16
        assert len(audio) == 4410

    def test_multi_speaker(self, tmp_path: Path) -> None:
        piper = _build_piper(
            tmp_path,
            voice_config=_MULTI_SPEAKER_CONFIG,
        )
        tracker: dict[str, Any] = {}
        piper._session.run = _tracking_run(tracker)  # type: ignore[union-attr]
        piper._synthesize_ids([1, 10, 0, 2], speaker_id=2)

        assert "sid" in tracker["inputs"]
        assert tracker["inputs"]["sid"][0] == 2

    def test_multi_speaker_default_id(
        self,
        tmp_path: Path,
    ) -> None:
        piper = _build_piper(
            tmp_path,
            voice_config=_MULTI_SPEAKER_CONFIG,
        )
        tracker: dict[str, Any] = {}
        piper._session.run = _tracking_run(tracker)  # type: ignore[union-attr]
        piper._synthesize_ids([1, 10, 0, 2])

        assert tracker["inputs"]["sid"][0] == 0

    def test_single_speaker_no_sid(
        self,
        tmp_path: Path,
    ) -> None:
        piper = _build_piper(tmp_path)
        tracker: dict[str, Any] = {}
        piper._session.run = _tracking_run(tracker)  # type: ignore[union-attr]
        piper._synthesize_ids([1, 10, 0, 2])

        assert "sid" not in tracker["inputs"]

    def test_session_not_loaded_raises(
        self,
        tmp_path: Path,
    ) -> None:
        piper = PiperTTS(model_dir=tmp_path)
        with pytest.raises(RuntimeError, match="session not loaded"):
            piper._synthesize_ids([1, 2])

    def test_audio_clipping(self, tmp_path: Path) -> None:
        """Audio outside [-1,1] should clip to int16 range."""
        piper = _build_piper(tmp_path)
        piper._session.run = _tracking_run(  # type: ignore[union-attr]
            {},
            audio_shape=(1, 1, 100),
            fill=2.0,
        )
        audio = piper._synthesize_ids([1, 2])
        assert np.all(audio == 32767)

    def test_scales_match_config(self, tmp_path: Path) -> None:
        cfg = PiperConfig(
            noise_scale=0.5,
            length_scale=1.2,
            noise_w=0.9,
        )
        piper = _build_piper(tmp_path, config=cfg)
        tracker: dict[str, Any] = {}
        piper._session.run = _tracking_run(  # type: ignore[union-attr]
            tracker,
            audio_shape=(1, 1, 100),
        )
        piper._synthesize_ids([1, 2])

        scales = tracker["inputs"]["scales"]
        np.testing.assert_allclose(scales, [0.5, 1.2, 0.9], atol=1e-6)


# ---------------------------------------------------------------------------
# Synthesize (full pipeline) tests
# ---------------------------------------------------------------------------


class TestSynthesize:
    """Tests for PiperTTS.synthesize (full pipeline)."""

    @pytest.mark.asyncio
    async def test_synthesize_text(
        self,
        tmp_path: Path,
    ) -> None:
        piper = _build_piper(tmp_path, audio_length=4410)
        _, modules = _mock_phonemize_module([["h", "ɛ", "l", "oʊ"]])

        with patch.dict("sys.modules", modules):
            chunk = await piper.synthesize("Hello")

        assert isinstance(chunk, AudioChunk)
        assert len(chunk.audio) > 0
        assert chunk.sample_rate == 22050
        assert chunk.duration_ms > 0

    @pytest.mark.asyncio
    async def test_synthesize_empty_text(
        self,
        tmp_path: Path,
    ) -> None:
        piper = _build_piper(tmp_path)
        chunk = await piper.synthesize("")
        assert len(chunk.audio) == 0
        assert chunk.duration_ms == 0.0

    @pytest.mark.asyncio
    async def test_synthesize_whitespace_only(
        self,
        tmp_path: Path,
    ) -> None:
        piper = _build_piper(tmp_path)
        chunk = await piper.synthesize("   \n\t  ")
        assert len(chunk.audio) == 0
        assert chunk.duration_ms == 0.0

    @pytest.mark.asyncio
    async def test_synthesize_multiple_sentences(
        self,
        tmp_path: Path,
    ) -> None:
        piper = _build_piper(tmp_path, audio_length=2205)
        _, modules = _mock_phonemize_module([["h", "ɛ"], ["w", "ɜ"]])

        with patch.dict("sys.modules", modules):
            chunk = await piper.synthesize("Hello. World.")

        assert len(chunk.audio) > 0
        assert chunk.duration_ms > 0

    @pytest.mark.asyncio
    async def test_synthesize_empty_phonemes(
        self,
        tmp_path: Path,
    ) -> None:
        piper = _build_piper(tmp_path)
        _, modules = _mock_phonemize_module([[]])

        with patch.dict("sys.modules", modules):
            chunk = await piper.synthesize("...")

        assert len(chunk.audio) == 0

    @pytest.mark.asyncio
    async def test_synthesize_auto_initializes(
        self,
        tmp_path: Path,
    ) -> None:
        """Synthesize on uninitialized engine should auto-init."""
        piper = _build_uninitialized_piper(tmp_path)

        mock_ort = MagicMock()
        mock_ort.SessionOptions.return_value = MagicMock()
        mock_ort.GraphOptimizationLevel.ORT_ENABLE_ALL = 99
        mock_ort.InferenceSession.return_value = _make_mock_session(4410)

        _, ph_modules = _mock_phonemize_module([["h", "ɛ"]])
        modules = {"onnxruntime": mock_ort, **ph_modules}

        with patch.dict("sys.modules", modules):
            chunk = await piper.synthesize("Hi")

        assert piper.is_initialized
        assert len(chunk.audio) > 0

    @pytest.mark.asyncio
    async def test_synthesize_with_speaker_id(
        self,
        tmp_path: Path,
    ) -> None:
        cfg = PiperConfig(speaker_id=2)
        piper = _build_piper(
            tmp_path,
            config=cfg,
            voice_config=_MULTI_SPEAKER_CONFIG,
        )

        tracker: dict[str, Any] = {}
        piper._session.run = _tracking_run(tracker)  # type: ignore[union-attr]

        _, modules = _mock_phonemize_module([["h"]])
        with patch.dict("sys.modules", modules):
            await piper.synthesize("Hi")

        assert tracker["inputs"]["sid"][0] == 2

    @pytest.mark.asyncio
    async def test_sentence_silence_length(
        self,
        tmp_path: Path,
    ) -> None:
        """Silence between sentences matches config."""
        cfg = PiperConfig(sentence_silence=0.5)
        piper = _build_piper(
            tmp_path,
            config=cfg,
            audio_length=100,
        )
        _, modules = _mock_phonemize_module([["h"], ["w"]])

        with patch.dict("sys.modules", modules):
            chunk = await piper.synthesize("Hello. World.")

        silence_samples = int(0.5 * 22050)
        expected_min = 2 * 100 + 2 * silence_samples
        assert len(chunk.audio) >= expected_min


# ---------------------------------------------------------------------------
# Streaming synthesis tests
# ---------------------------------------------------------------------------


class TestSynthesizeStreaming:
    """Tests for PiperTTS.synthesize_streaming."""

    @pytest.mark.asyncio
    async def test_streaming_yields_per_sentence(
        self,
        tmp_path: Path,
    ) -> None:
        piper = _build_piper(tmp_path, audio_length=2205)
        _, modules = _mock_phonemize_module([["h", "ɛ"]])

        with patch.dict("sys.modules", modules):
            chunks: list[AudioChunk] = []
            gen = _async_text_gen("Hello. ", "How are you?")
            async for chunk in piper.synthesize_streaming(gen):
                chunks.append(chunk)

        assert len(chunks) >= 2

    @pytest.mark.asyncio
    async def test_streaming_empty_input(
        self,
        tmp_path: Path,
    ) -> None:
        piper = _build_piper(tmp_path)

        chunks: list[AudioChunk] = []
        gen = _async_text_gen()
        async for chunk in piper.synthesize_streaming(gen):
            chunks.append(chunk)

        assert len(chunks) == 0

    @pytest.mark.asyncio
    async def test_streaming_single_chunk(
        self,
        tmp_path: Path,
    ) -> None:
        piper = _build_piper(tmp_path, audio_length=2205)
        _, modules = _mock_phonemize_module([["h"]])

        with patch.dict("sys.modules", modules):
            chunks: list[AudioChunk] = []
            gen = _async_text_gen("Hello world")
            async for chunk in piper.synthesize_streaming(gen):
                chunks.append(chunk)

        assert len(chunks) == 1

    @pytest.mark.asyncio
    async def test_streaming_whitespace_only(
        self,
        tmp_path: Path,
    ) -> None:
        piper = _build_piper(tmp_path)

        chunks: list[AudioChunk] = []
        gen = _async_text_gen("   ", "  ")
        async for chunk in piper.synthesize_streaming(gen):
            chunks.append(chunk)

        assert len(chunks) == 0

    @pytest.mark.asyncio
    async def test_streaming_auto_initializes(
        self,
        tmp_path: Path,
    ) -> None:
        piper = _build_uninitialized_piper(tmp_path)

        mock_ort = MagicMock()
        mock_ort.SessionOptions.return_value = MagicMock()
        mock_ort.GraphOptimizationLevel.ORT_ENABLE_ALL = 99
        mock_ort.InferenceSession.return_value = _make_mock_session(2205)

        _, ph_modules = _mock_phonemize_module([["h"]])
        modules = {"onnxruntime": mock_ort, **ph_modules}

        with patch.dict("sys.modules", modules):
            chunks: list[AudioChunk] = []
            gen = _async_text_gen("Hello")
            async for chunk in piper.synthesize_streaming(gen):
                chunks.append(chunk)

        assert piper.is_initialized


# ---------------------------------------------------------------------------
# Voice listing tests
# ---------------------------------------------------------------------------


class TestListVoices:
    """Tests for PiperTTS.list_voices."""

    def test_list_voices_empty_dir(
        self,
        tmp_path: Path,
    ) -> None:
        piper = PiperTTS(model_dir=tmp_path)
        assert piper.list_voices() == []

    def test_list_voices_nonexistent_dir(self) -> None:
        piper = PiperTTS(model_dir=Path("/nonexistent/path"))
        assert piper.list_voices() == []

    def test_list_voices_with_models(
        self,
        tmp_path: Path,
    ) -> None:
        for name in [
            "de_DE-thorsten-medium",
            "en_GB-alan-low",
            "en_US-lessac-medium",
        ]:
            (tmp_path / f"{name}.onnx").write_bytes(b"model")
            (tmp_path / f"{name}.onnx.json").write_text(
                "{}",
                encoding="utf-8",
            )

        piper = PiperTTS(model_dir=tmp_path)
        voices = piper.list_voices()

        assert len(voices) == 3
        assert voices == sorted(voices)
        assert "en_US-lessac-medium" in voices
        assert "en_GB-alan-low" in voices

    def test_list_voices_orphan_config(
        self,
        tmp_path: Path,
    ) -> None:
        (tmp_path / "orphan.onnx.json").write_text(
            "{}",
            encoding="utf-8",
        )
        piper = PiperTTS(model_dir=tmp_path)
        assert piper.list_voices() == []

    def test_list_voices_orphan_model(
        self,
        tmp_path: Path,
    ) -> None:
        (tmp_path / "orphan.onnx").write_bytes(b"model")
        piper = PiperTTS(model_dir=tmp_path)
        assert piper.list_voices() == []


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge case and error path tests."""

    @pytest.mark.asyncio
    async def test_synthesize_after_close(
        self,
        tmp_path: Path,
    ) -> None:
        """Synthesize after close should re-initialize."""
        piper = _build_uninitialized_piper(tmp_path)

        mock_ort = MagicMock()
        mock_ort.SessionOptions.return_value = MagicMock()
        mock_ort.GraphOptimizationLevel.ORT_ENABLE_ALL = 99
        mock_ort.InferenceSession.return_value = _make_mock_session(4410)

        _, ph_modules = _mock_phonemize_module([["h"]])
        modules = {"onnxruntime": mock_ort, **ph_modules}

        with patch.dict("sys.modules", modules):
            await piper.synthesize("Hi")
            assert piper.is_initialized

            await piper.close()
            assert not piper.is_initialized

            chunk2 = await piper.synthesize("Hi")
            assert piper.is_initialized
            assert len(chunk2.audio) > 0

    def test_model_dir_stored_as_path(
        self,
        tmp_path: Path,
    ) -> None:
        piper = PiperTTS(model_dir=tmp_path)
        assert isinstance(piper._model_dir, Path)

    @pytest.mark.asyncio
    async def test_duration_ms_calculation(
        self,
        tmp_path: Path,
    ) -> None:
        piper = _build_piper(tmp_path, audio_length=22050)
        _, modules = _mock_phonemize_module([["h"]])

        with patch.dict("sys.modules", modules):
            chunk = await piper.synthesize("Hello")

        # 22050 audio + 0.3*22050 silence = 28665 samples
        expected_samples = 22050 + int(0.3 * 22050)
        expected_ms = expected_samples / 22050 * 1000
        assert abs(chunk.duration_ms - expected_ms) < 1.0

    @settings(
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(
        text=st.text(
            min_size=1,
            max_size=50,
            alphabet=st.characters(
                categories=("L", "N", "P", "Z"),
            ),
        ),
    )
    def test_split_sentences_never_loses_text(
        self,
        text: str,
    ) -> None:
        """Splitting+joining preserves all content."""
        parts = _split_sentences(text)
        original = set(text.replace(" ", ""))
        rejoined = set(" ".join(parts).replace(" ", ""))
        assert original <= rejoined


# ---------------------------------------------------------------------------
# M2 wire-up — RED + USE telemetry on Piper TTS
# ---------------------------------------------------------------------------


class TestPiperM2WireUp:
    """PiperTTS.synthesize must emit M2 stage events.

    Mirrors the Kokoro M2 wire-up (commit 840ec69). Both engines
    emit consistent voice.stage.* events so dashboards see the
    fallback path (Kokoro → Piper) without per-engine branching.
    """

    @pytest.mark.asyncio()
    async def test_success_path_records_success_event(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        recorded: list[tuple[Any, Any, Any]] = []

        from sovyx.voice import tts_piper as piper_mod
        from sovyx.voice._stage_metrics import StageEventKind, VoiceStage

        def _capture(stage: Any, kind: Any, *, error_type: str | None = None) -> None:
            recorded.append((stage, kind, error_type))

        monkeypatch.setattr(piper_mod, "record_stage_event", _capture)

        piper = _build_piper(tmp_path)
        _, modules = _mock_phonemize_module([["h", "ɛ", "l", "oʊ"]])
        with patch.dict("sys.modules", modules):
            await piper.synthesize("hello world")

        assert (VoiceStage.TTS, StageEventKind.SUCCESS, None) in recorded

    @pytest.mark.asyncio()
    async def test_empty_text_records_drop_with_reason(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        recorded: list[tuple[Any, Any, Any]] = []

        from sovyx.voice import tts_piper as piper_mod
        from sovyx.voice._stage_metrics import StageEventKind, VoiceStage

        def _capture(stage: Any, kind: Any, *, error_type: str | None = None) -> None:
            recorded.append((stage, kind, error_type))

        monkeypatch.setattr(piper_mod, "record_stage_event", _capture)

        piper = _build_piper(tmp_path)
        await piper.synthesize("")

        assert (VoiceStage.TTS, StageEventKind.DROP, "empty_text") in recorded

    @pytest.mark.asyncio()
    async def test_no_phonemes_records_drop_with_reason(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When the phonemiser produces no usable phonemes (e.g.
        emoji-only text), the synth returns an empty AudioChunk and
        emits DROP with error_type=no_phonemes — distinct from
        empty_text so dashboards can attribute the rate of language-
        layer rejections separately."""
        recorded: list[tuple[Any, Any, Any]] = []

        from sovyx.voice import tts_piper as piper_mod
        from sovyx.voice._stage_metrics import StageEventKind, VoiceStage

        def _capture(stage: Any, kind: Any, *, error_type: str | None = None) -> None:
            recorded.append((stage, kind, error_type))

        monkeypatch.setattr(piper_mod, "record_stage_event", _capture)

        piper = _build_piper(tmp_path)
        # Phonemiser returns [[]] — non-empty list of empty inner
        # lists, which the synth filters out leaving all_audio empty.
        _, modules = _mock_phonemize_module([[]])
        with patch.dict("sys.modules", modules):
            await piper.synthesize("non-empty text")

        assert (VoiceStage.TTS, StageEventKind.DROP, "no_phonemes") in recorded


# ---------------------------------------------------------------------------
# T2 Ring 5 — output-energy validation (master mission Phase 1 / T1.36)
# ---------------------------------------------------------------------------

_TTS_PIPER_LOGGER = "sovyx.voice.tts_piper"


def _piper_events_of(
    caplog: pytest.LogCaptureFixture,
    event_name: str,
) -> list[dict[str, object]]:
    return [
        r.msg
        for r in caplog.records
        if r.name == _TTS_PIPER_LOGGER
        and isinstance(r.msg, dict)
        and r.msg.get("event") == event_name
    ]


def _build_piper_with_audio_fill(
    tmp_path: Path,
    fill: float,
    audio_length: int = 4410,
) -> PiperTTS:
    """Construct a PiperTTS whose ONNX session always returns a constant
    fill value — convenience for the silent / quiet-amplitude tests.
    """
    piper = _build_piper(tmp_path, audio_length=audio_length)

    def _fill_run(_names: object, _inputs: dict[str, Any]) -> list[Any]:
        return [np.full((1, 1, audio_length), fill, dtype=np.float32)]

    piper._session.run = _fill_run  # type: ignore[union-attr]
    return piper


class TestPiperT2EnergyValidation:
    """End-to-end T2 monitor wired through ``PiperTTS.synthesize``.

    Mirrors ``TestKokoroT2EnergyValidation`` so dashboards see consistent
    ``voice.synthesis_health`` attribution regardless of which engine
    produced the chunk.
    """

    @pytest.mark.asyncio()
    async def test_normal_synthesis_flagged_ok(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Healthy synthesis (uniform[-0.5, 0.5] mock) → ok, no warning."""
        import logging

        caplog.set_level(logging.WARNING, logger=_TTS_PIPER_LOGGER)
        piper = _build_piper(tmp_path)
        _, modules = _mock_phonemize_module([["h", "ɛ", "l", "oʊ"]])
        with patch.dict("sys.modules", modules):
            chunk = await piper.synthesize("hello")
        assert chunk.synthesis_health is None
        assert _piper_events_of(caplog, "voice.tts.piper_zero_energy_synthesis") == []

    @pytest.mark.asyncio()
    async def test_silent_synthesis_flagged_zero_energy(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """All-zero ONNX output → flagged + WARNING fires + DROP event."""
        import logging

        from sovyx.voice import tts_piper as piper_mod
        from sovyx.voice._stage_metrics import StageEventKind, VoiceStage

        caplog.set_level(logging.WARNING, logger=_TTS_PIPER_LOGGER)
        recorded: list[tuple[Any, Any, Any]] = []

        def _capture(stage: Any, kind: Any, *, error_type: str | None = None) -> None:
            recorded.append((stage, kind, error_type))

        piper = _build_piper_with_audio_fill(tmp_path, fill=0.0)
        _, modules = _mock_phonemize_module([["h", "ɛ", "l", "oʊ"]])
        with (
            patch.dict("sys.modules", modules),
            patch.object(piper_mod, "record_stage_event", _capture),
        ):
            chunk = await piper.synthesize("hello")

        assert chunk.synthesis_health == "zero_energy"
        events = _piper_events_of(caplog, "voice.tts.piper_zero_energy_synthesis")
        assert len(events) == 1
        assert events[0]["voice.measured_rms_dbfs"] == "-inf"
        assert events[0]["voice.rms_floor_dbfs"] == TTS_RMS_FLOOR_DBFS
        assert events[0]["voice.model"] == "piper"
        assert "fallback" in str(events[0]["voice.action_required"])
        assert (VoiceStage.TTS, StageEventKind.DROP, "zero_energy") in recorded

    @pytest.mark.asyncio()
    async def test_quiet_synthesis_flagged_zero_energy(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Audible-but-below-floor output (~ -86 dBFS) → flagged."""
        import logging

        caplog.set_level(logging.WARNING, logger=_TTS_PIPER_LOGGER)
        # 0.00005 * 32768 → peak ~1.6 LSB → RMS well below -60 dBFS.
        piper = _build_piper_with_audio_fill(tmp_path, fill=0.00005)
        _, modules = _mock_phonemize_module([["h", "ɛ", "l", "oʊ"]])
        with patch.dict("sys.modules", modules):
            chunk = await piper.synthesize("hello")
        assert chunk.synthesis_health == "zero_energy"

    @pytest.mark.asyncio()
    async def test_empty_input_skips_validation(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Empty text returns an empty AudioChunk WITHOUT firing the
        T2 warning — no synthesis happened, so silence is expected.
        """
        import logging

        caplog.set_level(logging.WARNING, logger=_TTS_PIPER_LOGGER)
        piper = _build_piper(tmp_path)
        chunk = await piper.synthesize("")
        assert chunk.synthesis_health is None
        assert chunk.audio.size == 0
        assert _piper_events_of(caplog, "voice.tts.piper_zero_energy_synthesis") == []

    @pytest.mark.asyncio()
    async def test_chunk_emitted_event_carries_synthesis_health(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Every successful chunk_emitted log must carry the health verdict."""
        import logging

        caplog.set_level(logging.INFO, logger=_TTS_PIPER_LOGGER)
        piper = _build_piper(tmp_path)
        _, modules = _mock_phonemize_module([["h", "ɛ", "l", "oʊ"]])
        with patch.dict("sys.modules", modules):
            await piper.synthesize("hello")
        events = _piper_events_of(caplog, "voice.tts.chunk_emitted")
        assert len(events) >= 1
        assert events[-1]["voice.synthesis_health"] == "ok"
