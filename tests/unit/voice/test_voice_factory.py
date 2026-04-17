"""Tests for sovyx.voice.factory — voice pipeline factory."""

from __future__ import annotations

import pytest

from sovyx.voice.factory import VoiceBundle, VoiceFactoryError, _create_wake_word_stub


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


class TestCreateWakeWordStub:
    """_create_wake_word_stub returns a usable no-op object."""

    def test_stub_returns_object(self) -> None:
        stub = _create_wake_word_stub()
        assert stub is not None

    def test_stub_process_frame(self) -> None:
        stub = _create_wake_word_stub()
        result = stub.process_frame(b"\x00" * 1024)
        assert result.detected is False


class TestFactoryContract:
    """Verify factory raises VoiceFactoryError when no TTS available."""

    @pytest.mark.asyncio()
    async def test_no_tts_raises(self, tmp_path) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        vad_file = tmp_path / "silero_vad.onnx"
        vad_file.write_bytes(b"fake")

        import sovyx.voice.factory as factory_mod

        original_create_vad = factory_mod._create_vad
        original_create_stt = factory_mod._create_stt

        factory_mod._create_vad = lambda *a, **kw: MagicMock()
        factory_mod._create_stt = lambda *a, **kw: MagicMock()
        try:
            with (
                patch.object(
                    factory_mod, "ensure_silero_vad", new=AsyncMock(return_value=vad_file)
                ),
                patch.object(factory_mod, "detect_tts_engine", return_value="none"),
                pytest.raises(Exception) as exc_info,
            ):
                await factory_mod.create_voice_pipeline(model_dir=tmp_path)
            assert type(exc_info.value).__name__ == "VoiceFactoryError"
        finally:
            factory_mod._create_vad = original_create_vad
            factory_mod._create_stt = original_create_stt


class TestVoiceBundle:
    """VoiceBundle wraps the pipeline and its capture task."""

    def test_fields(self) -> None:
        from unittest.mock import MagicMock

        pipeline = MagicMock()
        capture = MagicMock()
        bundle = VoiceBundle(pipeline=pipeline, capture_task=capture)
        assert bundle.pipeline is pipeline
        assert bundle.capture_task is capture


class TestFactoryWiresDeviceAndCapture:
    """create_voice_pipeline passes input_device through to the capture task."""

    @pytest.mark.asyncio()
    async def test_input_device_forwarded(self, tmp_path) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        vad_file = tmp_path / "silero_vad.onnx"
        vad_file.write_bytes(b"fake")

        import sovyx.voice.factory as factory_mod

        fake_pipeline = MagicMock()
        fake_pipeline.start = AsyncMock()

        original_create_vad = factory_mod._create_vad
        original_create_stt = factory_mod._create_stt
        original_create_piper = factory_mod._create_piper_tts
        factory_mod._create_vad = lambda *a, **kw: MagicMock()
        factory_mod._create_stt = lambda *a, **kw: MagicMock()
        factory_mod._create_piper_tts = lambda *a, **kw: MagicMock()

        try:
            with (
                patch.object(
                    factory_mod, "ensure_silero_vad", new=AsyncMock(return_value=vad_file)
                ),
                patch.object(factory_mod, "detect_tts_engine", return_value="piper"),
                patch(
                    "sovyx.voice.pipeline._orchestrator.VoicePipeline",
                    return_value=fake_pipeline,
                ),
            ):
                bundle = await factory_mod.create_voice_pipeline(
                    model_dir=tmp_path,
                    input_device=7,
                    output_device=3,
                )
            assert bundle.pipeline is fake_pipeline
            assert bundle.capture_task.input_device == 7
            fake_pipeline.start.assert_awaited_once()
        finally:
            factory_mod._create_vad = original_create_vad
            factory_mod._create_stt = original_create_stt
            factory_mod._create_piper_tts = original_create_piper
