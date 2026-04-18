"""Tests for sovyx.voice.factory — voice pipeline factory."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from sovyx.voice.factory import (
    VoiceBundle,
    VoiceFactoryError,
    _create_kokoro_tts,
    _create_wake_word_stub,
)


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


class TestCreateKokoroTtsResolution:
    """_create_kokoro_tts resolves voice_id + language via the voice catalog.

    Regression: before Phase 3, the Kokoro engine was always constructed
    with the hardcoded ``af_bella`` default, so a mind with
    ``language="pt"`` still spoke English through the live pipeline.
    """

    def test_explicit_voice_id_wins_over_language(self, tmp_path) -> None:
        """voice_id in the catalog → use it + its declared language.

        The catalog's language always wins: a ``pf_dora`` voice stays
        pt-br even if the caller passed ``language="en"``. This keeps
        the per-voice phoneme model in sync with the speaker identity.
        """
        import sovyx.voice.factory as factory_mod

        captured: dict[str, object] = {}

        class _FakeKokoro:
            def __init__(self, *, model_dir, config=None) -> None:
                captured["model_dir"] = model_dir
                captured["config"] = config

        with (
            patch.object(factory_mod, "__name__", "sovyx.voice.factory"),
            patch(
                "sovyx.voice.tts_kokoro.KokoroTTS",
                _FakeKokoro,
            ),
        ):
            _create_kokoro_tts(tmp_path, voice_id="pf_dora", language="en")

        config = captured["config"]
        assert config is not None
        assert config.voice == "pf_dora"
        assert config.language == "pt-br"

    def test_empty_voice_id_picks_recommended_for_pt(self, tmp_path) -> None:
        """language="pt" without voice_id → a p-prefix (Portuguese) voice."""
        captured: dict[str, object] = {}

        class _FakeKokoro:
            def __init__(self, *, model_dir, config=None) -> None:
                captured["config"] = config

        with patch("sovyx.voice.tts_kokoro.KokoroTTS", _FakeKokoro):
            _create_kokoro_tts(tmp_path, voice_id="", language="pt")

        config = captured["config"]
        assert config is not None
        # The Kokoro naming convention pins Portuguese voices to the
        # ``p`` prefix — if this ever regresses, the coherence bug is back.
        assert config.voice.startswith("p")
        assert config.language == "pt-br"

    def test_empty_voice_id_picks_recommended_for_ja(self, tmp_path) -> None:
        captured: dict[str, object] = {}

        class _FakeKokoro:
            def __init__(self, *, model_dir, config=None) -> None:
                captured["config"] = config

        with patch("sovyx.voice.tts_kokoro.KokoroTTS", _FakeKokoro):
            _create_kokoro_tts(tmp_path, voice_id="", language="ja")

        config = captured["config"]
        assert config is not None
        assert config.voice.startswith("j")
        assert config.language == "ja"

    def test_unknown_voice_id_falls_back_to_language(self, tmp_path) -> None:
        """A voice_id not in the catalog shouldn't crash — fall back."""
        captured: dict[str, object] = {}

        class _FakeKokoro:
            def __init__(self, *, model_dir, config=None) -> None:
                captured["config"] = config

        with patch("sovyx.voice.tts_kokoro.KokoroTTS", _FakeKokoro):
            _create_kokoro_tts(tmp_path, voice_id="zz_nobody", language="fr")

        config = captured["config"]
        assert config is not None
        assert config.voice.startswith("f")
        assert config.language == "fr"

    def test_unsupported_language_uses_kokoro_defaults(self, tmp_path) -> None:
        """An exotic language the catalog doesn't cover keeps the pipeline bootable."""
        captured: dict[str, object] = {}

        class _FakeKokoro:
            def __init__(self, *, model_dir, config=None) -> None:
                captured["config"] = config

        with patch("sovyx.voice.tts_kokoro.KokoroTTS", _FakeKokoro):
            _create_kokoro_tts(tmp_path, voice_id="", language="xx-yy")

        # No config passed → KokoroTTS uses its hardcoded defaults.
        assert captured["config"] is None

    def test_en_defaults_to_en_us(self, tmp_path) -> None:
        """Bare ``en`` should canonicalise to ``en-us`` (matches server aliases)."""
        captured: dict[str, object] = {}

        class _FakeKokoro:
            def __init__(self, *, model_dir, config=None) -> None:
                captured["config"] = config

        with patch("sovyx.voice.tts_kokoro.KokoroTTS", _FakeKokoro):
            _create_kokoro_tts(tmp_path, voice_id="", language="en")

        config = captured["config"]
        assert config is not None
        assert config.language == "en-us"
