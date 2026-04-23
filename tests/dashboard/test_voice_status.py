"""Tests for sovyx.dashboard.voice_status — voice pipeline status helpers."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from sovyx.dashboard.voice_status import (
    _selection_to_dict,
    get_voice_models,
    get_voice_status,
)

# ── Fixtures ──


@pytest.fixture()
def mock_registry() -> MagicMock:
    """Registry with nothing registered by default."""
    registry = MagicMock()
    registry.is_registered = MagicMock(return_value=False)
    return registry


# ── get_voice_status tests ──


class TestGetVoiceStatus:
    """Tests for get_voice_status()."""

    @pytest.mark.asyncio()
    async def test_returns_defaults_when_nothing_registered(
        self, mock_registry: MagicMock
    ) -> None:
        """All fields have sensible defaults when no services are registered."""
        status = await get_voice_status(mock_registry)

        assert status["pipeline"]["running"] is False
        assert status["pipeline"]["state"] == "not_configured"
        assert status["stt"]["engine"] is None
        assert status["tts"]["engine"] is None
        assert status["wake_word"]["enabled"] is False
        assert status["vad"]["enabled"] is False
        assert status["wyoming"]["connected"] is False
        assert status["hardware"]["tier"] is None

    @pytest.mark.asyncio()
    async def test_pipeline_running_requires_capture(self, mock_registry: MagicMock) -> None:
        """Pipeline reports running only when BOTH pipeline and capture are live."""
        from sovyx.voice._capture_task import AudioCaptureTask
        from sovyx.voice.pipeline import VoicePipeline, VoicePipelineState

        mock_pipeline = MagicMock(spec=VoicePipeline)
        mock_pipeline.is_running = True
        mock_pipeline.state = VoicePipelineState.IDLE

        mock_capture = MagicMock(spec=AudioCaptureTask)
        mock_capture.is_running = True
        mock_capture.input_device = 3
        # ``status_snapshot`` is the single payload ``/api/voice/status``
        # consumes — configure it here so the mock mirrors what a real
        # :class:`AudioCaptureTask` would return.
        mock_capture.status_snapshot.return_value = {
            "running": True,
            "input_device": 3,
            "host_api": "Windows WASAPI",
            "sample_rate": 16_000,
            "frames_delivered": 12,
            "last_rms_db": -42.0,
        }

        def is_reg(cls: type) -> bool:
            return cls in (VoicePipeline, AudioCaptureTask)

        async def resolve(cls: type) -> object:
            if cls is VoicePipeline:
                return mock_pipeline
            return mock_capture

        mock_registry.is_registered = is_reg
        mock_registry.resolve = AsyncMock(side_effect=resolve)

        status = await get_voice_status(mock_registry)

        assert status["pipeline"]["running"] is True
        assert status["pipeline"]["state"] == "idle"
        assert status["capture"]["running"] is True
        assert status["capture"]["input_device"] == 3  # noqa: PLR2004
        assert status["capture"]["host_api"] == "Windows WASAPI"
        assert status["capture"]["last_rms_db"] == -42.0  # noqa: PLR2004

    @pytest.mark.asyncio()
    async def test_pipeline_not_running_when_capture_dead(self, mock_registry: MagicMock) -> None:
        """Pipeline started but capture stopped means the dashboard reports NOT running."""
        from sovyx.voice._capture_task import AudioCaptureTask
        from sovyx.voice.pipeline import VoicePipeline, VoicePipelineState

        mock_pipeline = MagicMock(spec=VoicePipeline)
        mock_pipeline.is_running = True
        mock_pipeline.state = VoicePipelineState.IDLE

        mock_capture = MagicMock(spec=AudioCaptureTask)
        mock_capture.is_running = False  # capture dead — pipeline is silent
        mock_capture.input_device = None
        mock_capture.status_snapshot.return_value = {
            "running": False,
            "input_device": None,
            "host_api": None,
            "sample_rate": 16_000,
            "frames_delivered": 0,
            "last_rms_db": -120.0,
        }

        def is_reg(cls: type) -> bool:
            return cls in (VoicePipeline, AudioCaptureTask)

        async def resolve(cls: type) -> object:
            if cls is VoicePipeline:
                return mock_pipeline
            return mock_capture

        mock_registry.is_registered = is_reg
        mock_registry.resolve = AsyncMock(side_effect=resolve)

        status = await get_voice_status(mock_registry)

        assert status["pipeline"]["running"] is False
        assert status["capture"]["running"] is False

    @pytest.mark.asyncio()
    async def test_stt_engine_detected(self, mock_registry: MagicMock) -> None:
        """STT engine name and model are read from registry."""
        from sovyx.voice.stt import MoonshineSTT, STTEngine, STTState

        mock_stt = MagicMock(spec=MoonshineSTT, name="MoonshineSTT")
        mock_stt.config = MagicMock()
        mock_stt.config.model_name = "moonshine-tiny"
        mock_stt.state = STTState.READY

        def is_reg(cls: type) -> bool:
            return cls is STTEngine

        mock_registry.is_registered = is_reg
        mock_registry.resolve = AsyncMock(return_value=mock_stt)

        status = await get_voice_status(mock_registry)

        assert status["stt"]["engine"] is not None  # type(mock).__name__ in test env
        assert status["stt"]["model"] == "moonshine-tiny"
        assert status["stt"]["state"] == "ready"

    @pytest.mark.asyncio()
    async def test_tts_engine_detected(self, mock_registry: MagicMock) -> None:
        """TTS engine name and init state are read from registry."""
        from sovyx.voice.tts_piper import PiperTTS, TTSEngine

        mock_tts = MagicMock(spec=PiperTTS, name="PiperTTS")
        mock_tts.config = MagicMock()
        mock_tts.config.model_path = "/models/piper/en.onnx"
        mock_tts.is_initialized = True

        def is_reg(cls: type) -> bool:
            return cls is TTSEngine

        mock_registry.is_registered = is_reg
        mock_registry.resolve = AsyncMock(return_value=mock_tts)

        status = await get_voice_status(mock_registry)

        assert status["tts"]["engine"] is not None  # type(mock).__name__ in test env
        assert status["tts"]["initialized"] is True
        assert "en.onnx" in status["tts"]["model"]

    @pytest.mark.asyncio()
    async def test_vad_detected(self, mock_registry: MagicMock) -> None:
        """VAD shows as enabled when registered."""
        from sovyx.voice.vad import SileroVAD

        def is_reg(cls: type) -> bool:
            return cls is SileroVAD

        mock_registry.is_registered = is_reg

        status = await get_voice_status(mock_registry)

        assert status["vad"]["enabled"] is True

    @pytest.mark.asyncio()
    async def test_wake_word_detected(self, mock_registry: MagicMock) -> None:
        """Wake word shows as enabled with phrase when registered."""
        from sovyx.voice.wake_word import WakeWordDetector

        mock_ww = MagicMock(spec=WakeWordDetector)
        mock_ww.config = MagicMock()
        mock_ww.config.wake_phrase = "hey sovyx"

        def is_reg(cls: type) -> bool:
            return cls is WakeWordDetector

        mock_registry.is_registered = is_reg
        mock_registry.resolve = AsyncMock(return_value=mock_ww)

        status = await get_voice_status(mock_registry)

        assert status["wake_word"]["enabled"] is True
        assert status["wake_word"]["phrase"] == "hey sovyx"

    @pytest.mark.asyncio()
    async def test_hardware_tier_detected(self, mock_registry: MagicMock) -> None:
        """Hardware tier is read from VoiceModelAutoSelector."""
        from sovyx.voice.auto_select import (
            HardwareProfile,
            HardwareTier,
            VoiceModelAutoSelector,
        )

        mock_selector = MagicMock(spec=VoiceModelAutoSelector)
        mock_selector.profile = HardwareProfile(
            tier=HardwareTier.N100,
            ram_mb=16384,
            cpu_cores=4,
            has_gpu=False,
        )

        def is_reg(cls: type) -> bool:
            return cls is VoiceModelAutoSelector

        mock_registry.is_registered = is_reg
        mock_registry.resolve = AsyncMock(return_value=mock_selector)

        status = await get_voice_status(mock_registry)

        assert status["hardware"]["tier"] == "N100"
        assert status["hardware"]["ram_mb"] == 16384

    @pytest.mark.asyncio()
    async def test_exception_in_one_section_doesnt_break_others(
        self, mock_registry: MagicMock
    ) -> None:
        """If one service resolution throws, other sections still work."""
        from sovyx.voice.vad import SileroVAD

        def is_reg(cls: type) -> bool:
            return cls is SileroVAD

        mock_registry.is_registered = is_reg
        mock_registry.resolve = AsyncMock(side_effect=RuntimeError("boom"))

        status = await get_voice_status(mock_registry)

        # VAD doesn't need resolve, just is_registered
        assert status["vad"]["enabled"] is True
        assert status["pipeline"]["running"] is False

    @pytest.mark.asyncio()
    async def test_returns_all_expected_sections(self, mock_registry: MagicMock) -> None:
        """Status dict contains all required top-level keys.

        v1.3 §4.6 L6 added ``preflight_warnings`` — a list of boot-time
        warning dicts forwarded from the ``BootPreflightWarningsStore``
        service. The key is always present (empty by default) so the
        dashboard can render it unconditionally.
        """
        status = await get_voice_status(mock_registry)
        expected_keys = {
            "pipeline",
            "capture",
            "stt",
            "tts",
            "wake_word",
            "vad",
            "wyoming",
            "hardware",
            "preflight_warnings",
        }
        assert set(status.keys()) == expected_keys


# ── get_voice_models tests ──


class TestGetVoiceModels:
    """Tests for get_voice_models()."""

    @pytest.mark.asyncio()
    async def test_returns_available_tiers(self, mock_registry: MagicMock) -> None:
        """All hardware tiers are listed under available_tiers."""
        from sovyx.voice.auto_select import HardwareTier

        models = await get_voice_models(mock_registry)

        assert "available_tiers" in models
        for tier in HardwareTier:
            assert tier.name in models["available_tiers"]

    @pytest.mark.asyncio()
    async def test_tier_contains_model_fields(self, mock_registry: MagicMock) -> None:
        """Each tier entry has stt_primary, tts_primary, etc."""
        models = await get_voice_models(mock_registry)

        for _tier_name, tier_data in models["available_tiers"].items():
            assert "stt_primary" in tier_data
            assert "tts_primary" in tier_data
            assert "vad" in tier_data
            assert "wake" in tier_data

    @pytest.mark.asyncio()
    async def test_detected_tier_is_none_when_no_selector(self, mock_registry: MagicMock) -> None:
        """detected_tier is None when VoiceModelAutoSelector not registered."""
        models = await get_voice_models(mock_registry)
        assert models["detected_tier"] is None
        assert models["active"] is None

    @pytest.mark.asyncio()
    async def test_detected_tier_with_selector(self, mock_registry: MagicMock) -> None:
        """detected_tier comes from the auto-selector when registered."""
        from sovyx.voice.auto_select import (
            HardwareProfile,
            HardwareTier,
            ModelSelection,
            VoiceModelAutoSelector,
        )

        mock_selector = MagicMock(spec=VoiceModelAutoSelector)
        mock_selector.profile = HardwareProfile(
            tier=HardwareTier.PI5,
            ram_mb=4096,
            cpu_cores=4,
            has_gpu=False,
        )
        mock_selector.selection = ModelSelection(
            stt_primary="moonshine-tiny",
            stt_streaming="moonshine-tiny",
            tts_primary="piper",
            tts_quality="piper",
            wake="openwakeword",
            vad="silero-v5",
            speaker="none",
            voice_clone=None,
            tier=HardwareTier.PI5,
        )

        def is_reg(cls: type) -> bool:
            return cls is VoiceModelAutoSelector

        mock_registry.is_registered = is_reg
        mock_registry.resolve = AsyncMock(return_value=mock_selector)

        models = await get_voice_models(mock_registry)

        assert models["detected_tier"] == "PI5"
        assert models["active"]["stt_primary"] == "moonshine-tiny"
        assert models["active"]["tts_primary"] == "piper"


# ── _selection_to_dict tests ──


class TestSelectionToDict:
    """Tests for _selection_to_dict()."""

    def test_all_fields_present(self) -> None:
        """All key ModelSelection fields are in the output dict."""
        from sovyx.voice.auto_select import HardwareTier, ModelSelection

        sel = ModelSelection(
            stt_primary="a",
            stt_streaming="b",
            tts_primary="c",
            tts_quality="d",
            wake="e",
            vad="f",
            speaker="g",
            voice_clone=None,
            tier=HardwareTier.PI5,
        )
        d = _selection_to_dict(sel)
        assert d["stt_primary"] == "a"
        assert d["stt_streaming"] == "b"
        assert d["tts_primary"] == "c"
        assert d["tts_quality"] == "d"
        assert d["wake"] == "e"
        assert d["vad"] == "f"
