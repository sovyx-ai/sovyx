"""Tests for sovyx.voice.model_registry — dep check, model registry, download."""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import pytest

from sovyx.voice.model_registry import (
    VOICE_MODELS,
    VoiceModelInfo,
    check_voice_deps,
    detect_tts_engine,
    ensure_silero_vad,
    get_default_model_dir,
    get_models_for_tier,
)


class TestVoiceModelInfo:
    """VoiceModelInfo dataclass basics."""

    def test_frozen(self) -> None:
        m = VoiceModelInfo(name="test", category="vad", size_mb=1.0, url="", filename="x.onnx")
        with pytest.raises(Exception):
            m.name = "other"  # type: ignore[misc]

    def test_defaults(self) -> None:
        m = VoiceModelInfo(name="a", category="b", size_mb=0.0, url="u", filename="f")
        assert m.download_available is True
        assert m.description == ""


class TestVoiceModels:
    """VOICE_MODELS registry."""

    def test_silero_vad_entry(self) -> None:
        info = VOICE_MODELS["silero-vad-v5"]
        assert info.category == "vad"
        assert info.size_mb > 0
        assert info.download_available is True

    def test_moonshine_entry(self) -> None:
        info = VOICE_MODELS["moonshine-tiny"]
        assert info.category == "stt"
        assert info.download_available is False


class TestGetDefaultModelDir:
    """get_default_model_dir returns ~/.sovyx/models/voice."""

    def test_returns_path(self) -> None:
        d = get_default_model_dir()
        assert isinstance(d, Path)
        assert d.parts[-3:] == (".sovyx", "models", "voice")


class TestGetModelsForTier:
    """get_models_for_tier returns all models."""

    def test_returns_list(self) -> None:
        models = get_models_for_tier("DESKTOP_CPU")
        assert isinstance(models, list)
        assert len(models) == len(VOICE_MODELS)

    def test_returns_voice_model_info_instances(self) -> None:
        for m in get_models_for_tier("PI5"):
            assert isinstance(m, VoiceModelInfo)


class TestCheckVoiceDeps:
    """check_voice_deps detects installed/missing packages."""

    def test_returns_two_lists(self) -> None:
        installed, missing = check_voice_deps()
        assert isinstance(installed, list)
        assert isinstance(missing, list)
        assert len(installed) + len(missing) == 2  # noqa: PLR2004

    def test_dict_keys(self) -> None:
        installed, missing = check_voice_deps()
        for entry in [*installed, *missing]:
            assert "module" in entry
            assert "package" in entry

    def test_missing_module_detected(self) -> None:
        with patch.dict(sys.modules, {"moonshine_voice": None}):
            _installed, missing = check_voice_deps()
        names = [d["module"] for d in missing]
        assert "moonshine_voice" in names

    def test_installed_module_detected(self) -> None:
        fake_mod = ModuleType("moonshine_voice")
        with patch.dict(sys.modules, {"moonshine_voice": fake_mod}):
            installed, _missing = check_voice_deps()
        names = [d["module"] for d in installed]
        assert "moonshine_voice" in names


class TestDetectTTSEngine:
    """detect_tts_engine priority: piper > kokoro > none."""

    def test_piper_priority(self) -> None:
        piper = ModuleType("piper_phonemize")
        kokoro = ModuleType("kokoro_onnx")
        with patch.dict(sys.modules, {"piper_phonemize": piper, "kokoro_onnx": kokoro}):
            assert detect_tts_engine() == "piper"

    def test_kokoro_fallback(self) -> None:
        kokoro = ModuleType("kokoro_onnx")
        with patch.dict(sys.modules, {"piper_phonemize": None, "kokoro_onnx": kokoro}):
            assert detect_tts_engine() == "kokoro"

    def test_none_when_nothing(self) -> None:
        with patch.dict(sys.modules, {"piper_phonemize": None, "kokoro_onnx": None}):
            assert detect_tts_engine() == "none"


class TestEnsureSileroVAD:
    """ensure_silero_vad auto-download logic."""

    @pytest.mark.asyncio()
    async def test_returns_existing_model(self, tmp_path: Path) -> None:
        model_file = tmp_path / "silero_vad.onnx"
        model_file.write_bytes(b"fake-onnx-model")
        result = await ensure_silero_vad(tmp_path)
        assert result == model_file

    @pytest.mark.asyncio()
    async def test_downloads_when_missing(self, tmp_path: Path) -> None:
        import sovyx.voice.model_registry as reg_mod

        def fake_download(url: str, dest: Path) -> None:
            dest.write_bytes(b"onnx-data")

        original = reg_mod._download_file
        reg_mod._download_file = fake_download  # type: ignore[assignment]
        try:
            result = await ensure_silero_vad(tmp_path)
        finally:
            reg_mod._download_file = original

        assert result == tmp_path / "silero_vad.onnx"
        assert result.exists()

    def test_download_file_cleans_up_on_failure(self, tmp_path: Path) -> None:
        import httpx

        from sovyx.voice.model_registry import _download_file

        dest = tmp_path / "test_model.onnx"

        with (
            patch.object(httpx, "Client", side_effect=ConnectionError("down")),
            pytest.raises(Exception) as exc_info,
        ):
            _download_file("https://example.com/model.onnx", dest)
        assert type(exc_info.value).__name__ == "ConnectionError"
        assert not dest.exists()
        tmp_files = list(tmp_path.glob(".vad_*"))
        assert len(tmp_files) == 0
