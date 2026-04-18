"""Tests for sovyx.voice.model_registry — dep check, registry, download."""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest

from sovyx.engine._model_downloader import ModelDownloader
from sovyx.voice.model_registry import (
    _KOKORO_MODEL_URLS,
    _KOKORO_VOICES_URLS,
    _SILERO_URLS,
    VOICE_MODELS,
    VoiceModelInfo,
    check_voice_deps,
    detect_tts_engine,
    ensure_kokoro_tts,
    ensure_silero_vad,
    get_default_model_dir,
    get_models_for_tier,
)

if TYPE_CHECKING:
    from sovyx.engine._model_downloader import DownloadAttempt


class TestVoiceModelInfo:
    """VoiceModelInfo dataclass basics."""

    def test_frozen(self) -> None:
        m = VoiceModelInfo(name="test", category="vad", size_mb=1.0, urls=(), filename="x.onnx")
        with pytest.raises(Exception):
            m.name = "other"  # type: ignore[misc]

    def test_defaults(self) -> None:
        m = VoiceModelInfo(
            name="a",
            category="b",
            size_mb=0.0,
            urls=("https://example.com/a",),
            filename="f",
        )
        assert m.download_available is True
        assert m.description == ""

    def test_url_property_returns_primary(self) -> None:
        """`.url` back-compat property exposes the first mirror."""
        m = VoiceModelInfo(
            name="a",
            category="b",
            size_mb=0.0,
            urls=("https://primary/a", "https://mirror/a"),
            filename="f",
        )
        assert m.url == "https://primary/a"

    def test_url_property_empty_when_no_urls(self) -> None:
        """`.url` returns '' when urls is empty (e.g. manual-download models)."""
        m = VoiceModelInfo(
            name="a",
            category="b",
            size_mb=0.0,
            urls=(),
            filename="f",
            download_available=False,
        )
        assert m.url == ""


class TestVoiceModels:
    """VOICE_MODELS registry."""

    def test_silero_vad_entry(self) -> None:
        info = VOICE_MODELS["silero-vad-v5"]
        assert info.category == "vad"
        assert info.size_mb > 0
        assert info.download_available is True
        assert len(info.urls) >= 2  # primary + at least one mirror

    def test_moonshine_entry(self) -> None:
        info = VOICE_MODELS["moonshine-tiny"]
        assert info.category == "stt"
        assert info.download_available is False
        assert info.urls == ()

    def test_kokoro_model_entry(self) -> None:
        info = VOICE_MODELS["kokoro-v1.0-int8"]
        assert info.category == "tts"
        assert info.download_available is True
        assert len(info.urls) >= 2  # primary + mirror

    def test_kokoro_voices_entry(self) -> None:
        info = VOICE_MODELS["kokoro-voices-v1.0"]
        assert info.category == "tts"
        assert info.download_available is True
        assert len(info.urls) >= 2

    def test_all_downloadable_models_pin_checksum(self) -> None:
        """Every downloadable model has a SHA-256 pin — catches drift."""
        for info in VOICE_MODELS.values():
            if info.download_available:
                assert info.sha256, f"{info.name} missing SHA-256 pin"
                assert len(info.sha256) == 64, f"{info.name} SHA-256 wrong length"


class TestMirrorURLTables:
    """Mirror URL tables sanity checks (offline — no HTTP call)."""

    def test_silero_has_sovyx_fallback(self) -> None:
        """Self-hosted mirror must be present — the GH raw layer 504s."""
        assert any("sovyx-ai/sovyx" in u for u in _SILERO_URLS), (
            "Silero URL table missing self-hosted sovyx mirror — re-add it. "
            "github.com/snakers4/.../raw/... 504'd in production v0.17.0 "
            "and the sovyx release is the guaranteed byte-exact fallback."
        )

    def test_kokoro_model_has_sovyx_fallback(self) -> None:
        assert any("sovyx-ai/sovyx" in u for u in _KOKORO_MODEL_URLS)

    def test_kokoro_voices_has_sovyx_fallback(self) -> None:
        assert any("sovyx-ai/sovyx" in u for u in _KOKORO_VOICES_URLS)


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
    """ensure_silero_vad delegates to the shared ModelDownloader."""

    @pytest.mark.asyncio()
    async def test_returns_existing_model(self, tmp_path: Path) -> None:
        """Fast path: file on disk with matching checksum — no network call."""
        info = VOICE_MODELS["silero-vad-v5"]
        model_file = tmp_path / info.filename
        model_file.write_bytes(b"fake-onnx-model")

        download_stub = AsyncMock()
        with (
            patch.object(ModelDownloader, "_download", new=download_stub),
            patch.object(ModelDownloader, "_verify_checksum", return_value=True),
        ):
            result = await ensure_silero_vad(tmp_path)

        assert result == model_file
        download_stub.assert_not_called()

    @pytest.mark.asyncio()
    async def test_downloads_when_missing(self, tmp_path: Path) -> None:
        """Missing file triggers _download via the shared ModelDownloader."""
        info = VOICE_MODELS["silero-vad-v5"]

        async def fake_download(
            url: str,
            dest: Path,
            callback: Any = None,
            headers: dict[str, str] | None = None,
        ) -> None:
            dest.write_bytes(b"onnx-payload")

        # ``_download`` is a @staticmethod — patch.object replaces the
        # descriptor, and a plain function would become a bound method.
        # Wrap in ``staticmethod`` to preserve the invocation shape.
        with (
            patch.object(ModelDownloader, "_download", staticmethod(fake_download)),
            patch.object(ModelDownloader, "_verify_checksum", return_value=True),
        ):
            result = await ensure_silero_vad(tmp_path)

        assert result == tmp_path / info.filename
        assert result.exists()

    @pytest.mark.asyncio()
    async def test_mirror_failover(self, tmp_path: Path) -> None:
        """Primary 504 → primary exhausts retries → mirror-1 succeeds."""
        import httpx

        info = VOICE_MODELS["silero-vad-v5"]
        call_urls: list[str] = []

        async def fake_download(
            url: str,
            dest: Path,
            callback: Any = None,
            headers: dict[str, str] | None = None,
        ) -> None:
            call_urls.append(url)
            # Fail on the primary URL until retries exhaust, then let
            # mirror-1 succeed.
            if url == info.urls[0]:
                resp = httpx.Response(504, request=httpx.Request("GET", url))
                raise httpx.HTTPStatusError("504", request=resp.request, response=resp)
            dest.write_bytes(b"onnx-payload")

        # Short retry budget so the test runs fast.
        with (
            patch.object(ModelDownloader, "MAX_RETRIES", 1),
            patch.object(ModelDownloader, "BACKOFF_BASE", 0.0),
            patch.object(ModelDownloader, "_download", staticmethod(fake_download)),
            patch.object(ModelDownloader, "_verify_checksum", return_value=True),
        ):
            result = await ensure_silero_vad(tmp_path)

        assert result.exists()
        # Primary called (failed) + mirror-1 called (succeeded).
        assert call_urls[0] == info.urls[0]
        assert info.urls[1] in call_urls


class TestEnsureKokoroTTS:
    """ensure_kokoro_tts downloads both model and voices via shared downloader."""

    @pytest.mark.asyncio()
    async def test_downloads_model_and_voices(self, tmp_path: Path) -> None:
        async def fake_download(
            url: str,
            dest: Path,
            callback: Any = None,
            headers: dict[str, str] | None = None,
        ) -> None:
            dest.write_bytes(b"payload")

        with (
            patch.object(ModelDownloader, "_download", staticmethod(fake_download)),
            patch.object(ModelDownloader, "_verify_checksum", return_value=True),
        ):
            result = await ensure_kokoro_tts(tmp_path)

        kokoro_dir = tmp_path / "kokoro"
        assert result == kokoro_dir
        # Both assets materialise after the atomic .tmp → final rename.
        assert (kokoro_dir / "kokoro-v1.0.int8.onnx").exists()
        assert (kokoro_dir / "voices-v1.0.bin").exists()


class TestOnAttemptHook:
    """ensure_* helpers propagate the on_attempt callback to the downloader."""

    @pytest.mark.asyncio()
    async def test_on_attempt_invoked_on_success(self, tmp_path: Path) -> None:
        attempts: list[DownloadAttempt] = []

        async def fake_download(
            url: str,
            dest: Path,
            callback: Any = None,
            headers: dict[str, str] | None = None,
        ) -> None:
            dest.write_bytes(b"payload")

        with (
            patch.object(ModelDownloader, "_download", staticmethod(fake_download)),
            patch.object(ModelDownloader, "_verify_checksum", return_value=True),
        ):
            await ensure_silero_vad(tmp_path, on_attempt=attempts.append)

        assert len(attempts) == 1
        assert attempts[0].filename == "silero_vad.onnx"
        assert attempts[0].source == "primary"
        assert attempts[0].result == "ok"


class TestOtelAttemptCounter:
    """The voice-tier downloader always records to the OTel counter."""

    @pytest.mark.asyncio()
    async def test_counter_receives_success(self, tmp_path: Path) -> None:
        """Successful download emits ``sovyx.model.download.attempts`` with result=ok."""
        from sovyx.observability.metrics import setup_metrics, teardown_metrics

        registry = setup_metrics()
        counter_calls: list[tuple[int, dict[str, str]]] = []

        def spy_add(value: int, attributes: dict[str, str] | None = None) -> None:
            counter_calls.append((value, dict(attributes or {})))

        async def fake_download(
            url: str,
            dest: Path,
            callback: Any = None,
            headers: dict[str, str] | None = None,
        ) -> None:
            dest.write_bytes(b"payload")

        try:
            with (
                patch.object(registry.model_download_attempts, "add", new=spy_add),
                patch.object(ModelDownloader, "_download", staticmethod(fake_download)),
                patch.object(ModelDownloader, "_verify_checksum", return_value=True),
            ):
                await ensure_silero_vad(tmp_path)
        finally:
            teardown_metrics()

        assert len(counter_calls) == 1
        _value, labels = counter_calls[0]
        assert labels["model"] == "silero_vad.onnx"
        assert labels["source"] == "primary"
        assert labels["result"] == "ok"

    @pytest.mark.asyncio()
    async def test_user_hook_and_otel_both_fire(self, tmp_path: Path) -> None:
        """Caller-supplied ``on_attempt`` is composed with the OTel hook."""
        from sovyx.observability.metrics import setup_metrics, teardown_metrics

        registry = setup_metrics()
        counter_calls: list[dict[str, str]] = []
        user_attempts: list[DownloadAttempt] = []

        def spy_add(value: int, attributes: dict[str, str] | None = None) -> None:
            counter_calls.append(dict(attributes or {}))

        async def fake_download(
            url: str,
            dest: Path,
            callback: Any = None,
            headers: dict[str, str] | None = None,
        ) -> None:
            dest.write_bytes(b"payload")

        try:
            with (
                patch.object(registry.model_download_attempts, "add", new=spy_add),
                patch.object(ModelDownloader, "_download", staticmethod(fake_download)),
                patch.object(ModelDownloader, "_verify_checksum", return_value=True),
            ):
                await ensure_silero_vad(tmp_path, on_attempt=user_attempts.append)
        finally:
            teardown_metrics()

        assert len(counter_calls) == 1
        assert len(user_attempts) == 1
        assert user_attempts[0].filename == "silero_vad.onnx"
