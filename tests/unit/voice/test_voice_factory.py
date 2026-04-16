"""Tests for sovyx.voice.factory — voice pipeline factory."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sovyx.voice.factory import VoiceFactoryError, create_voice_pipeline


class TestVoiceFactoryError:
    """VoiceFactoryError stores missing_models."""

    def test_message(self) -> None:
        err = VoiceFactoryError("no tts")
        assert str(err) == "no tts"
        assert err.missing_models == []

    def test_with_missing_models(self) -> None:
        models = [{"name": "piper-tts", "install_command": "pip install piper-tts"}]
        err = VoiceFactoryError("missing", missing_models=models)
        assert err.missing_models == models


class TestCreateVoicePipeline:
    """create_voice_pipeline end-to-end with mocked components."""

    @pytest.mark.asyncio()
    async def test_raises_when_no_tts(self, tmp_path: Path) -> None:
        vad_file = tmp_path / "silero_vad.onnx"
        vad_file.write_bytes(b"fake")

        with (
            patch(
                "sovyx.voice.factory.ensure_silero_vad",
                new_callable=AsyncMock,
                return_value=vad_file,
            ),
            patch("sovyx.voice.factory.detect_tts_engine", return_value="none"),
            patch("sovyx.voice.factory._create_vad", return_value=MagicMock()),
            patch("sovyx.voice.factory._create_stt", return_value=MagicMock()),
            pytest.raises(Exception) as exc_info,
        ):
            await create_voice_pipeline(model_dir=tmp_path)
        assert type(exc_info.value).__name__ == "VoiceFactoryError"
        assert exc_info.value.missing_models  # type: ignore[union-attr]

    @pytest.mark.asyncio()
    async def test_creates_pipeline_with_piper(self, tmp_path: Path) -> None:
        vad_file = tmp_path / "silero_vad.onnx"
        vad_file.write_bytes(b"fake")

        mock_pipeline = MagicMock()
        mock_pipeline.start = AsyncMock()

        with (
            patch(
                "sovyx.voice.factory.ensure_silero_vad",
                new_callable=AsyncMock,
                return_value=vad_file,
            ),
            patch("sovyx.voice.factory.detect_tts_engine", return_value="piper"),
            patch("sovyx.voice.factory._create_vad", return_value=MagicMock()),
            patch("sovyx.voice.factory._create_stt", return_value=MagicMock()),
            patch("sovyx.voice.factory._create_piper_tts", return_value=MagicMock()),
            patch("sovyx.voice.factory._create_wake_word_stub", return_value=MagicMock()),
            patch("sovyx.voice.pipeline._orchestrator.VoicePipeline", return_value=mock_pipeline),
            patch("sovyx.voice.pipeline._config.VoicePipelineConfig"),
        ):
            result = await create_voice_pipeline(model_dir=tmp_path)
        assert result is mock_pipeline
        mock_pipeline.start.assert_awaited_once()

    @pytest.mark.asyncio()
    async def test_creates_pipeline_with_kokoro(self, tmp_path: Path) -> None:
        vad_file = tmp_path / "silero_vad.onnx"
        vad_file.write_bytes(b"fake")

        mock_pipeline = MagicMock()
        mock_pipeline.start = AsyncMock()

        with (
            patch(
                "sovyx.voice.factory.ensure_silero_vad",
                new_callable=AsyncMock,
                return_value=vad_file,
            ),
            patch("sovyx.voice.factory.detect_tts_engine", return_value="kokoro"),
            patch("sovyx.voice.factory._create_vad", return_value=MagicMock()),
            patch("sovyx.voice.factory._create_stt", return_value=MagicMock()),
            patch("sovyx.voice.factory._create_kokoro_tts", return_value=MagicMock()),
            patch("sovyx.voice.factory._create_wake_word_stub", return_value=MagicMock()),
            patch("sovyx.voice.pipeline._orchestrator.VoicePipeline", return_value=mock_pipeline),
            patch("sovyx.voice.pipeline._config.VoicePipelineConfig"),
        ):
            result = await create_voice_pipeline(model_dir=tmp_path)
        assert result is mock_pipeline

    @pytest.mark.asyncio()
    async def test_uses_default_model_dir(self) -> None:
        with (
            patch("sovyx.voice.factory.get_default_model_dir") as mock_dir,
            patch("sovyx.voice.factory.ensure_silero_vad", new_callable=AsyncMock),
            patch("sovyx.voice.factory.detect_tts_engine", return_value="none"),
            patch("sovyx.voice.factory._create_vad", return_value=MagicMock()),
            patch("sovyx.voice.factory._create_stt", return_value=MagicMock()),
            pytest.raises(Exception),
        ):
            mock_path = MagicMock()
            mock_dir.return_value = mock_path
            await create_voice_pipeline()
        mock_dir.assert_called_once()

    @pytest.mark.asyncio()
    async def test_passes_event_bus(self, tmp_path: Path) -> None:
        vad_file = tmp_path / "silero_vad.onnx"
        vad_file.write_bytes(b"fake")

        mock_pipeline_cls = MagicMock()
        mock_instance = MagicMock()
        mock_instance.start = AsyncMock()
        mock_pipeline_cls.return_value = mock_instance
        mock_bus = MagicMock()

        with (
            patch(
                "sovyx.voice.factory.ensure_silero_vad",
                new_callable=AsyncMock,
                return_value=vad_file,
            ),
            patch("sovyx.voice.factory.detect_tts_engine", return_value="piper"),
            patch("sovyx.voice.factory._create_vad", return_value=MagicMock()),
            patch("sovyx.voice.factory._create_stt", return_value=MagicMock()),
            patch("sovyx.voice.factory._create_piper_tts", return_value=MagicMock()),
            patch("sovyx.voice.factory._create_wake_word_stub", return_value=MagicMock()),
            patch("sovyx.voice.pipeline._orchestrator.VoicePipeline", mock_pipeline_cls),
            patch("sovyx.voice.pipeline._config.VoicePipelineConfig"),
        ):
            await create_voice_pipeline(model_dir=tmp_path, event_bus=mock_bus)
        _, kwargs = mock_pipeline_cls.call_args
        assert kwargs["event_bus"] is mock_bus
