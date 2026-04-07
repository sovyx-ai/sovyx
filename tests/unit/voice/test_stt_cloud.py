"""Tests for CloudSTT — OpenAI Whisper API fallback (V05-25).

Covers: config validation, WAV encoding, fallback logic, API mocking,
lifecycle states, error handling, streaming fallback.
"""

from __future__ import annotations

import struct
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

import numpy as np
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from sovyx.voice.stt import (
    PartialTranscription,
    STTState,
    TranscriptionResult,
)
from sovyx.voice.stt_cloud import (
    _DEFAULT_SAMPLE_RATE,
    _MAX_AUDIO_DURATION_S,
    _WAV_SAMPLE_WIDTH,
    CloudSTT,
    CloudSTTConfig,
    CloudSTTError,
    _audio_to_wav_bytes,
    needs_cloud_fallback,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def config() -> CloudSTTConfig:
    """Default valid config."""
    return CloudSTTConfig(api_key="sk-test-key-12345")


@pytest.fixture()
def audio_1s() -> np.ndarray:
    """1 second of 440 Hz sine wave at 16 kHz."""
    t = np.linspace(0, 1.0, _DEFAULT_SAMPLE_RATE, endpoint=False, dtype=np.float32)
    return np.sin(2.0 * np.pi * 440.0 * t).astype(np.float32)


@pytest.fixture()
def short_audio() -> np.ndarray:
    """Very short audio (100 samples)."""
    return np.zeros(100, dtype=np.float32)


# ---------------------------------------------------------------------------
# CloudSTTConfig tests
# ---------------------------------------------------------------------------


class TestCloudSTTConfig:
    """Configuration validation."""

    def test_defaults(self) -> None:
        cfg = CloudSTTConfig()
        assert cfg.api_key == ""
        assert cfg.model == "whisper-1"
        assert cfg.language == "en"
        assert cfg.confidence_threshold == 0.6
        assert cfg.api_timeout == 30.0
        assert cfg.api_base_url == "https://api.openai.com/v1"

    def test_custom_values(self) -> None:
        cfg = CloudSTTConfig(
            api_key="sk-abc",
            model="whisper-2",
            language="pt",
            confidence_threshold=0.8,
            api_timeout=60.0,
            api_base_url="https://custom.api/v1",
        )
        assert cfg.api_key == "sk-abc"
        assert cfg.model == "whisper-2"
        assert cfg.language == "pt"
        assert cfg.confidence_threshold == 0.8
        assert cfg.api_timeout == 60.0
        assert cfg.api_base_url == "https://custom.api/v1"

    def test_confidence_threshold_too_low(self) -> None:
        with pytest.raises(ValueError, match="confidence_threshold"):
            CloudSTTConfig(confidence_threshold=-0.1)

    def test_confidence_threshold_too_high(self) -> None:
        with pytest.raises(ValueError, match="confidence_threshold"):
            CloudSTTConfig(confidence_threshold=1.1)

    def test_confidence_threshold_boundary_zero(self) -> None:
        cfg = CloudSTTConfig(confidence_threshold=0.0)
        assert cfg.confidence_threshold == 0.0

    def test_confidence_threshold_boundary_one(self) -> None:
        cfg = CloudSTTConfig(confidence_threshold=1.0)
        assert cfg.confidence_threshold == 1.0

    def test_negative_timeout(self) -> None:
        with pytest.raises(ValueError, match="api_timeout"):
            CloudSTTConfig(api_timeout=-1.0)

    def test_zero_timeout(self) -> None:
        with pytest.raises(ValueError, match="api_timeout"):
            CloudSTTConfig(api_timeout=0.0)

    def test_empty_model(self) -> None:
        with pytest.raises(ValueError, match="model"):
            CloudSTTConfig(model="")

    def test_frozen(self) -> None:
        cfg = CloudSTTConfig()
        with pytest.raises(AttributeError):
            cfg.api_key = "new"  # type: ignore[misc]

    @settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(threshold=st.floats(min_value=0.0, max_value=1.0))
    def test_valid_threshold_range(self, threshold: float) -> None:
        """Any threshold in [0, 1] is valid."""
        cfg = CloudSTTConfig(confidence_threshold=threshold)
        assert cfg.confidence_threshold == threshold


# ---------------------------------------------------------------------------
# _audio_to_wav_bytes tests
# ---------------------------------------------------------------------------


class TestAudioToWavBytes:
    """WAV encoding helper."""

    def test_basic_wav_structure(self, audio_1s: np.ndarray) -> None:
        wav = _audio_to_wav_bytes(audio_1s, _DEFAULT_SAMPLE_RATE)
        assert wav[:4] == b"RIFF"
        assert wav[8:12] == b"WAVE"
        assert wav[12:16] == b"fmt "
        assert wav[36:40] == b"data"

    def test_wav_mono_pcm(self, audio_1s: np.ndarray) -> None:
        wav = _audio_to_wav_bytes(audio_1s, _DEFAULT_SAMPLE_RATE)
        # fmt chunk: PCM=1, channels=1
        audio_format = struct.unpack_from("<H", wav, 20)[0]
        num_channels = struct.unpack_from("<H", wav, 22)[0]
        assert audio_format == 1  # PCM
        assert num_channels == 1  # mono

    def test_wav_sample_rate(self, audio_1s: np.ndarray) -> None:
        wav = _audio_to_wav_bytes(audio_1s, _DEFAULT_SAMPLE_RATE)
        sr = struct.unpack_from("<I", wav, 24)[0]
        assert sr == _DEFAULT_SAMPLE_RATE

    def test_wav_bits_per_sample(self, audio_1s: np.ndarray) -> None:
        wav = _audio_to_wav_bytes(audio_1s, _DEFAULT_SAMPLE_RATE)
        bits = struct.unpack_from("<H", wav, 34)[0]
        assert bits == 16

    def test_wav_data_size(self, audio_1s: np.ndarray) -> None:
        wav = _audio_to_wav_bytes(audio_1s, _DEFAULT_SAMPLE_RATE)
        data_size = struct.unpack_from("<I", wav, 40)[0]
        expected = len(audio_1s) * _WAV_SAMPLE_WIDTH
        assert data_size == expected

    def test_wav_riff_size(self, audio_1s: np.ndarray) -> None:
        wav = _audio_to_wav_bytes(audio_1s, _DEFAULT_SAMPLE_RATE)
        riff_size = struct.unpack_from("<I", wav, 4)[0]
        assert riff_size == len(wav) - 8

    def test_int16_input(self) -> None:
        audio = np.array([0, 16384, -16384, 32767, -32768], dtype=np.int16)
        wav = _audio_to_wav_bytes(audio, 8000)
        assert wav[:4] == b"RIFF"
        data_size = struct.unpack_from("<I", wav, 40)[0]
        assert data_size == 10  # 5 samples * 2 bytes

    def test_float64_input(self) -> None:
        audio = np.array([0.0, 0.5, -0.5], dtype=np.float64)
        wav = _audio_to_wav_bytes(audio, 8000)
        assert wav[:4] == b"RIFF"

    def test_clipping(self) -> None:
        """Values beyond [-1, 1] are clipped."""
        audio = np.array([2.0, -2.0, 0.5], dtype=np.float32)
        wav = _audio_to_wav_bytes(audio, 8000)
        # Extract int16 samples
        data_offset = 44
        s0 = struct.unpack_from("<h", wav, data_offset)[0]
        s1 = struct.unpack_from("<h", wav, data_offset + 2)[0]
        assert s0 == 32767  # clipped to 1.0
        assert s1 == -32767  # clipped to -1.0

    def test_empty_audio(self) -> None:
        audio = np.array([], dtype=np.float32)
        wav = _audio_to_wav_bytes(audio, 16000)
        data_size = struct.unpack_from("<I", wav, 40)[0]
        assert data_size == 0

    @settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(length=st.integers(min_value=1, max_value=1000))
    def test_roundtrip_length(self, length: int) -> None:
        """WAV data section has correct length for any input size."""
        audio = np.zeros(length, dtype=np.float32)
        wav = _audio_to_wav_bytes(audio, 16000)
        data_size = struct.unpack_from("<I", wav, 40)[0]
        assert data_size == length * _WAV_SAMPLE_WIDTH


# ---------------------------------------------------------------------------
# needs_cloud_fallback tests
# ---------------------------------------------------------------------------


class TestNeedsCloudFallback:
    """Fallback decision logic."""

    def test_low_confidence_triggers_fallback(self) -> None:
        result = TranscriptionResult(text="hello", confidence=0.3)
        assert needs_cloud_fallback(result, threshold=0.6) is True

    def test_high_confidence_no_fallback(self) -> None:
        result = TranscriptionResult(text="hello", confidence=0.9)
        assert needs_cloud_fallback(result, threshold=0.6) is False

    def test_exact_threshold_no_fallback(self) -> None:
        """At exactly the threshold, confidence is NOT below."""
        result = TranscriptionResult(text="hello", confidence=0.6)
        assert needs_cloud_fallback(result, threshold=0.6) is False

    def test_empty_text_always_fallback(self) -> None:
        result = TranscriptionResult(text="", confidence=0.99)
        assert needs_cloud_fallback(result) is True

    def test_whitespace_only_text_fallback(self) -> None:
        result = TranscriptionResult(text="   ", confidence=0.99)
        assert needs_cloud_fallback(result) is True

    def test_default_threshold(self) -> None:
        result = TranscriptionResult(text="hello", confidence=0.5)
        assert needs_cloud_fallback(result) is True  # 0.5 < 0.6

    @settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(
        confidence=st.floats(min_value=0.0, max_value=1.0),
        threshold=st.floats(min_value=0.0, max_value=1.0),
    )
    def test_deterministic(self, confidence: float, threshold: float) -> None:
        """Fallback decision is deterministic for given inputs."""
        result = TranscriptionResult(text="hello world", confidence=confidence)
        r1 = needs_cloud_fallback(result, threshold)
        r2 = needs_cloud_fallback(result, threshold)
        assert r1 == r2


# ---------------------------------------------------------------------------
# CloudSTT lifecycle tests
# ---------------------------------------------------------------------------


class TestCloudSTTLifecycle:
    """Engine state machine tests."""

    def test_initial_state(self, config: CloudSTTConfig) -> None:
        engine = CloudSTT(config)
        assert engine.state == STTState.UNINITIALIZED

    def test_config_property(self, config: CloudSTTConfig) -> None:
        engine = CloudSTT(config)
        assert engine.config is config

    def test_default_config(self) -> None:
        engine = CloudSTT()
        assert engine.config.api_key == ""

    @pytest.mark.asyncio()
    async def test_initialize_ready(self, config: CloudSTTConfig) -> None:
        with patch("sovyx.voice.stt_cloud.httpx") as mock_httpx:
            mock_httpx.AsyncClient.return_value = AsyncMock()
            mock_httpx.Timeout.return_value = MagicMock()
            engine = CloudSTT(config)
            await engine.initialize()
            assert engine.state == STTState.READY
            await engine.close()

    @pytest.mark.asyncio()
    async def test_initialize_idempotent(self, config: CloudSTTConfig) -> None:
        with patch("sovyx.voice.stt_cloud.httpx") as mock_httpx:
            mock_httpx.AsyncClient.return_value = AsyncMock()
            mock_httpx.Timeout.return_value = MagicMock()
            engine = CloudSTT(config)
            await engine.initialize()
            await engine.initialize()  # Should not raise
            assert engine.state == STTState.READY
            await engine.close()

    @pytest.mark.asyncio()
    async def test_initialize_no_api_key(self) -> None:
        engine = CloudSTT(CloudSTTConfig(api_key=""))
        with pytest.raises(ValueError, match="API key is required"):
            await engine.initialize()

    @pytest.mark.asyncio()
    async def test_initialize_after_close(self, config: CloudSTTConfig) -> None:
        with patch("sovyx.voice.stt_cloud.httpx") as mock_httpx:
            mock_httpx.AsyncClient.return_value = AsyncMock()
            mock_httpx.Timeout.return_value = MagicMock()
            engine = CloudSTT(config)
            await engine.initialize()
            await engine.close()
            with pytest.raises(RuntimeError, match="closed"):
                await engine.initialize()

    @pytest.mark.asyncio()
    async def test_close_sets_state(self, config: CloudSTTConfig) -> None:
        with patch("sovyx.voice.stt_cloud.httpx") as mock_httpx:
            mock_httpx.AsyncClient.return_value = AsyncMock()
            mock_httpx.Timeout.return_value = MagicMock()
            engine = CloudSTT(config)
            await engine.initialize()
            await engine.close()
            assert engine.state == STTState.CLOSED

    @pytest.mark.asyncio()
    async def test_close_without_init(self, config: CloudSTTConfig) -> None:
        engine = CloudSTT(config)
        await engine.close()  # Should not raise
        assert engine.state == STTState.CLOSED

    @pytest.mark.asyncio()
    async def test_transcribe_before_init(
        self, config: CloudSTTConfig, audio_1s: np.ndarray
    ) -> None:
        engine = CloudSTT(config)
        with pytest.raises(RuntimeError, match="not initialized"):
            await engine.transcribe(audio_1s)

    @pytest.mark.asyncio()
    async def test_transcribe_after_close(
        self, config: CloudSTTConfig, audio_1s: np.ndarray
    ) -> None:
        with patch("sovyx.voice.stt_cloud.httpx") as mock_httpx:
            mock_httpx.AsyncClient.return_value = AsyncMock()
            mock_httpx.Timeout.return_value = MagicMock()
            engine = CloudSTT(config)
            await engine.initialize()
            await engine.close()
            with pytest.raises(RuntimeError, match="closed"):
                await engine.transcribe(audio_1s)


# ---------------------------------------------------------------------------
# CloudSTT transcription tests
# ---------------------------------------------------------------------------


class TestCloudSTTTranscribe:
    """Transcription via mocked API."""

    @pytest.mark.asyncio()
    async def test_successful_transcription(
        self, config: CloudSTTConfig, audio_1s: np.ndarray
    ) -> None:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"text": "Hello world"}

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response

        with patch("sovyx.voice.stt_cloud.httpx") as mock_httpx:
            mock_httpx.AsyncClient.return_value = mock_client
            mock_httpx.Timeout.return_value = MagicMock()
            engine = CloudSTT(config)
            await engine.initialize()

            result = await engine.transcribe(audio_1s)

            assert result.text == "Hello world"
            assert result.confidence == 0.95
            assert result.language == "en"
            assert result.duration_ms > 0
            assert engine.state == STTState.READY

            await engine.close()

    @pytest.mark.asyncio()
    async def test_api_sends_correct_data(
        self, config: CloudSTTConfig, audio_1s: np.ndarray
    ) -> None:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"text": "test"}

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response

        with patch("sovyx.voice.stt_cloud.httpx") as mock_httpx:
            mock_httpx.AsyncClient.return_value = mock_client
            mock_httpx.Timeout.return_value = MagicMock()
            engine = CloudSTT(config)
            await engine.initialize()
            await engine.transcribe(audio_1s)

            call_args = mock_client.post.call_args
            assert call_args[0][0] == "/audio/transcriptions"
            assert "file" in call_args[1]["files"]
            assert call_args[1]["data"]["model"] == "whisper-1"
            assert call_args[1]["data"]["language"] == "en"

            await engine.close()

    @pytest.mark.asyncio()
    async def test_api_error_status(
        self, config: CloudSTTConfig, audio_1s: np.ndarray
    ) -> None:
        mock_response = MagicMock()
        mock_response.status_code = 429
        mock_response.text = "Rate limit exceeded"

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response

        with patch("sovyx.voice.stt_cloud.httpx") as mock_httpx:
            mock_httpx.AsyncClient.return_value = mock_client
            mock_httpx.Timeout.return_value = MagicMock()
            engine = CloudSTT(config)
            await engine.initialize()

            with pytest.raises(CloudSTTError, match="429"):
                await engine.transcribe(audio_1s)

            # State should recover to READY
            assert engine.state == STTState.READY
            await engine.close()

    @pytest.mark.asyncio()
    async def test_api_network_error(
        self, config: CloudSTTConfig, audio_1s: np.ndarray
    ) -> None:
        mock_client = AsyncMock()
        mock_client.post.side_effect = ConnectionError("Network down")

        with patch("sovyx.voice.stt_cloud.httpx") as mock_httpx:
            mock_httpx.AsyncClient.return_value = mock_client
            mock_httpx.Timeout.return_value = MagicMock()
            engine = CloudSTT(config)
            await engine.initialize()

            with pytest.raises(CloudSTTError, match="request failed"):
                await engine.transcribe(audio_1s)

            assert engine.state == STTState.READY
            await engine.close()

    @pytest.mark.asyncio()
    async def test_api_invalid_json(
        self, config: CloudSTTConfig, audio_1s: np.ndarray
    ) -> None:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.side_effect = ValueError("bad json")
        mock_response.text = "not json"

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response

        with patch("sovyx.voice.stt_cloud.httpx") as mock_httpx:
            mock_httpx.AsyncClient.return_value = mock_client
            mock_httpx.Timeout.return_value = MagicMock()
            engine = CloudSTT(config)
            await engine.initialize()

            with pytest.raises(CloudSTTError, match="Invalid JSON"):
                await engine.transcribe(audio_1s)

            await engine.close()

    @pytest.mark.asyncio()
    async def test_api_unexpected_response_format(
        self, config: CloudSTTConfig, audio_1s: np.ndarray
    ) -> None:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"text": 42}  # not a string

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response

        with patch("sovyx.voice.stt_cloud.httpx") as mock_httpx:
            mock_httpx.AsyncClient.return_value = mock_client
            mock_httpx.Timeout.return_value = MagicMock()
            engine = CloudSTT(config)
            await engine.initialize()

            with pytest.raises(CloudSTTError, match="Unexpected response"):
                await engine.transcribe(audio_1s)

            await engine.close()

    @pytest.mark.asyncio()
    async def test_audio_too_long(self, config: CloudSTTConfig) -> None:
        # Create audio longer than max duration
        samples = int((_MAX_AUDIO_DURATION_S + 1) * _DEFAULT_SAMPLE_RATE)
        long_audio = np.zeros(samples, dtype=np.float32)

        mock_client = AsyncMock()
        with patch("sovyx.voice.stt_cloud.httpx") as mock_httpx:
            mock_httpx.AsyncClient.return_value = mock_client
            mock_httpx.Timeout.return_value = MagicMock()
            engine = CloudSTT(config)
            await engine.initialize()

            with pytest.raises(CloudSTTError, match="Audio too long"):
                await engine.transcribe(long_audio)

            await engine.close()

    @pytest.mark.asyncio()
    async def test_text_stripped(
        self, config: CloudSTTConfig, audio_1s: np.ndarray
    ) -> None:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"text": "  hello world  "}

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response

        with patch("sovyx.voice.stt_cloud.httpx") as mock_httpx:
            mock_httpx.AsyncClient.return_value = mock_client
            mock_httpx.Timeout.return_value = MagicMock()
            engine = CloudSTT(config)
            await engine.initialize()

            result = await engine.transcribe(audio_1s)
            assert result.text == "hello world"

            await engine.close()

    @pytest.mark.asyncio()
    async def test_empty_text_response(
        self, config: CloudSTTConfig, audio_1s: np.ndarray
    ) -> None:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"text": ""}

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response

        with patch("sovyx.voice.stt_cloud.httpx") as mock_httpx:
            mock_httpx.AsyncClient.return_value = mock_client
            mock_httpx.Timeout.return_value = MagicMock()
            engine = CloudSTT(config)
            await engine.initialize()

            result = await engine.transcribe(audio_1s)
            assert result.text == ""
            assert result.confidence == 0.95

            await engine.close()

    @pytest.mark.asyncio()
    async def test_custom_language(self, audio_1s: np.ndarray) -> None:
        cfg = CloudSTTConfig(api_key="sk-test", language="pt")
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"text": "olá mundo"}

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response

        with patch("sovyx.voice.stt_cloud.httpx") as mock_httpx:
            mock_httpx.AsyncClient.return_value = mock_client
            mock_httpx.Timeout.return_value = MagicMock()
            engine = CloudSTT(cfg)
            await engine.initialize()

            result = await engine.transcribe(audio_1s)
            assert result.language == "pt"

            # Check API received language param
            call_data = mock_client.post.call_args[1]["data"]
            assert call_data["language"] == "pt"

            await engine.close()

    @pytest.mark.asyncio()
    async def test_no_language_hint(self, audio_1s: np.ndarray) -> None:
        cfg = CloudSTTConfig(api_key="sk-test", language="")
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"text": "hello"}

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response

        with patch("sovyx.voice.stt_cloud.httpx") as mock_httpx:
            mock_httpx.AsyncClient.return_value = mock_client
            mock_httpx.Timeout.return_value = MagicMock()
            engine = CloudSTT(cfg)
            await engine.initialize()

            await engine.transcribe(audio_1s)

            call_data = mock_client.post.call_args[1]["data"]
            assert "language" not in call_data

            await engine.close()

    @pytest.mark.asyncio()
    async def test_custom_sample_rate(
        self, config: CloudSTTConfig
    ) -> None:
        audio = np.zeros(44100, dtype=np.float32)  # 1s at 44.1kHz
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"text": "test"}

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response

        with patch("sovyx.voice.stt_cloud.httpx") as mock_httpx:
            mock_httpx.AsyncClient.return_value = mock_client
            mock_httpx.Timeout.return_value = MagicMock()
            engine = CloudSTT(config)
            await engine.initialize()

            result = await engine.transcribe(audio, sample_rate=44100)
            assert result.text == "test"

            await engine.close()

    @pytest.mark.asyncio()
    async def test_state_returns_ready_after_error(
        self, config: CloudSTTConfig, audio_1s: np.ndarray
    ) -> None:
        """State recovers to READY after failed transcription."""
        mock_client = AsyncMock()
        mock_client.post.side_effect = ConnectionError("fail")

        with patch("sovyx.voice.stt_cloud.httpx") as mock_httpx:
            mock_httpx.AsyncClient.return_value = mock_client
            mock_httpx.Timeout.return_value = MagicMock()
            engine = CloudSTT(config)
            await engine.initialize()

            with pytest.raises(CloudSTTError):
                await engine.transcribe(audio_1s)

            assert engine.state == STTState.READY
            await engine.close()


# ---------------------------------------------------------------------------
# CloudSTT streaming tests
# ---------------------------------------------------------------------------


class TestCloudSTTStreaming:
    """Streaming transcription (batch fallback)."""

    @pytest.mark.asyncio()
    async def test_streaming_collects_and_transcribes(
        self, config: CloudSTTConfig
    ) -> None:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"text": "streaming result"}

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response

        async def audio_gen() -> AsyncIterator[tuple[np.ndarray, int]]:
            for _ in range(3):
                yield np.zeros(1600, dtype=np.float32), 16000

        with patch("sovyx.voice.stt_cloud.httpx") as mock_httpx:
            mock_httpx.AsyncClient.return_value = mock_client
            mock_httpx.Timeout.return_value = MagicMock()
            engine = CloudSTT(config)
            await engine.initialize()

            results: list[PartialTranscription] = []
            async for partial in engine.transcribe_streaming(audio_gen()):
                results.append(partial)

            assert len(results) == 1
            assert results[0].text == "streaming result"
            assert results[0].is_final is True
            assert results[0].confidence == 0.95

            await engine.close()

    @pytest.mark.asyncio()
    async def test_streaming_empty_input(self, config: CloudSTTConfig) -> None:
        mock_client = AsyncMock()

        async def empty_gen() -> AsyncIterator[tuple[np.ndarray, int]]:
            return
            yield  # type: ignore[misc]  # Make it an async generator

        with patch("sovyx.voice.stt_cloud.httpx") as mock_httpx:
            mock_httpx.AsyncClient.return_value = mock_client
            mock_httpx.Timeout.return_value = MagicMock()
            engine = CloudSTT(config)
            await engine.initialize()

            results: list[PartialTranscription] = []
            async for partial in engine.transcribe_streaming(empty_gen()):
                results.append(partial)

            assert len(results) == 1
            assert results[0].text == ""
            assert results[0].is_final is True
            assert results[0].confidence == 0.0

            await engine.close()

    @pytest.mark.asyncio()
    async def test_streaming_state_recovery(
        self, config: CloudSTTConfig
    ) -> None:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"text": "ok"}

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response

        async def audio_gen() -> AsyncIterator[tuple[np.ndarray, int]]:
            yield np.zeros(1600, dtype=np.float32), 16000

        with patch("sovyx.voice.stt_cloud.httpx") as mock_httpx:
            mock_httpx.AsyncClient.return_value = mock_client
            mock_httpx.Timeout.return_value = MagicMock()
            engine = CloudSTT(config)
            await engine.initialize()

            async for _ in engine.transcribe_streaming(audio_gen()):
                pass

            assert engine.state == STTState.READY
            await engine.close()


# ---------------------------------------------------------------------------
# CloudSTTError tests
# ---------------------------------------------------------------------------


class TestCloudSTTError:
    """Error class."""

    def test_inherits_exception(self) -> None:
        assert issubclass(CloudSTTError, Exception)

    def test_message(self) -> None:
        err = CloudSTTError("test error")
        assert str(err) == "test error"

    def test_raised_and_caught(self) -> None:
        with pytest.raises(CloudSTTError, match="specific"):
            raise CloudSTTError("specific error message")


# ---------------------------------------------------------------------------
# Import / public API tests
# ---------------------------------------------------------------------------


class TestPublicAPI:
    """Verify public API is importable from voice package."""

    def test_import_from_voice_package(self) -> None:
        from sovyx.voice import (
            CloudSTT,
            CloudSTTConfig,
            CloudSTTError,
            needs_cloud_fallback,
        )

        assert CloudSTT is not None
        assert CloudSTTConfig is not None
        assert CloudSTTError is not None
        assert needs_cloud_fallback is not None

    def test_cloud_stt_extends_stt_engine(self) -> None:
        from sovyx.voice.stt import STTEngine

        assert issubclass(CloudSTT, STTEngine)

    def test_cloud_stt_uses_httpx(self) -> None:
        """CloudSTT module imports httpx at top level."""
        import sovyx.voice.stt_cloud as mod

        assert hasattr(mod, "httpx")
