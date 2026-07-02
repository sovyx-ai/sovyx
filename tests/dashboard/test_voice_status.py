"""Tests for sovyx.dashboard.voice_status — voice pipeline status helpers."""

from __future__ import annotations

from typing import Any
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
        # LIVE-2 P1-10: nothing registered → Wyoming is not configured, so
        # the dashboard hides the card instead of showing "Disconnected".
        assert status["wyoming"]["configured"] is False
        assert status["hardware"]["tier"] is None
        # LIVE-2 P0-1: nothing registered → every subsystem is honestly
        # "unavailable" (NOT healthy, NOT unknown).
        assert status["stt"]["health"] == "unavailable"
        assert status["tts"]["health"] == "unavailable"
        assert status["wake_word"]["health"] == "unavailable"
        assert status["vad"]["health"] == "unavailable"

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
        """STT engine name and model are read from the real config.

        LIVE-2 P1-1 regression: uses a REAL ``MoonshineConfig`` rather
        than an attribute-accepting ``MagicMock``. The prior test set
        ``config.model_name`` on a MagicMock, which silently accepts any
        attribute and so passed against the buggy producer (AP #33). The
        configured model identity lives on ``model_size`` — reading the
        old ``model_name`` field yields None even when STT is running.
        """
        from sovyx.voice.stt import MoonshineConfig, MoonshineSTT, STTEngine, STTState

        mock_stt = MagicMock(spec=MoonshineSTT, name="MoonshineSTT")
        # Distinct from the "tiny" default so the assertion proves the
        # real configured value is surfaced, not a coincidental default.
        mock_stt.config = MoonshineConfig(model_size="small")
        mock_stt.state = STTState.READY

        def is_reg(cls: type) -> bool:
            return cls is STTEngine

        mock_registry.is_registered = is_reg
        mock_registry.resolve = AsyncMock(return_value=mock_stt)

        status = await get_voice_status(mock_registry)

        assert status["stt"]["engine"] is not None  # type(mock).__name__ in test env
        assert status["stt"]["model"] == "small"
        assert status["stt"]["state"] == "ready"
        # READY → healthy (LIVE-2 P0-1).
        assert status["stt"]["health"] == "healthy"

    @pytest.mark.asyncio()
    async def test_tts_engine_detected(self, mock_registry: MagicMock) -> None:
        """TTS engine name, model, and init state are read from the real config.

        LIVE-2 P1-2 regression: REAL ``PiperConfig``. The model identity
        lives on ``voice``; the prior ``model_path`` read yielded None
        even with TTS initialized (and the old MagicMock test masked it).
        """
        from sovyx.voice.tts_piper import PiperConfig, PiperTTS, TTSEngine

        mock_tts = MagicMock(spec=PiperTTS, name="PiperTTS")
        mock_tts.config = PiperConfig(voice="en_US-lessac-medium")
        mock_tts.is_initialized = True

        def is_reg(cls: type) -> bool:
            return cls is TTSEngine

        mock_registry.is_registered = is_reg
        mock_registry.resolve = AsyncMock(return_value=mock_tts)

        status = await get_voice_status(mock_registry)

        assert status["tts"]["engine"] is not None  # type(mock).__name__ in test env
        assert status["tts"]["initialized"] is True
        assert status["tts"]["model"] == "en_US-lessac-medium"
        # initialized → healthy (LIVE-2 P0-1).
        assert status["tts"]["health"] == "healthy"

    @pytest.mark.asyncio()
    async def test_tts_kokoro_model_from_voice(self, mock_registry: MagicMock) -> None:
        """Kokoro TTS model identity also reads from ``KokoroConfig.voice``.

        LIVE-2 P1-2: the producer resolves the abstract ``TTSEngine``, so
        the fix must hold for Kokoro as well as Piper. Both configs expose
        the model identity on ``voice``.
        """
        from sovyx.voice.tts_kokoro import KokoroConfig, KokoroTTS
        from sovyx.voice.tts_piper import TTSEngine

        mock_tts = MagicMock(spec=KokoroTTS, name="KokoroTTS")
        mock_tts.config = KokoroConfig(voice="am_adam")
        mock_tts.is_initialized = True

        def is_reg(cls: type) -> bool:
            return cls is TTSEngine

        mock_registry.is_registered = is_reg
        mock_registry.resolve = AsyncMock(return_value=mock_tts)

        status = await get_voice_status(mock_registry)

        assert status["tts"]["model"] == "am_adam"

    @pytest.mark.asyncio()
    async def test_vad_detected(self, mock_registry: MagicMock) -> None:
        """VAD shows enabled + healthy when registered with a sound session."""
        from sovyx.voice.vad import SileroVAD

        mock_vad = MagicMock(spec=SileroVAD)
        mock_vad.is_session_unrecoverable = False
        mock_vad.corruption_count = 0

        def is_reg(cls: type) -> bool:
            return cls is SileroVAD

        mock_registry.is_registered = is_reg
        mock_registry.resolve = AsyncMock(return_value=mock_vad)

        status = await get_voice_status(mock_registry)

        assert status["vad"]["enabled"] is True
        assert status["vad"]["health"] == "healthy"

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
        # Registered detector with a readable FSM state → healthy (P0-1).
        assert status["wake_word"]["health"] == "healthy"

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

        # LIVE-2 P0-1: ``enabled`` (registration fact) is set before the
        # resolve, so it survives a resolve failure — but health must NOT
        # claim healthy when the engine couldn't be probed; it stays
        # "unknown". This is the core anti-presence-only-lie guarantee.
        assert status["vad"]["enabled"] is True
        assert status["vad"]["health"] == "unknown"
        assert status["pipeline"]["running"] is False

    # ── LIVE-2 Phase 3 (P0-1) — health is real readiness, not registration ──

    @staticmethod
    def _only(cls_obj: type) -> Any:
        def is_reg(cls: type) -> bool:
            return cls is cls_obj

        return is_reg

    @pytest.mark.asyncio()
    async def test_stt_uninitialized_is_degraded_not_healthy(
        self, mock_registry: MagicMock
    ) -> None:
        from sovyx.voice.stt import MoonshineSTT, STTEngine, STTState

        mock_stt = MagicMock(spec=MoonshineSTT)
        mock_stt.state = STTState.UNINITIALIZED
        mock_registry.is_registered = self._only(STTEngine)
        mock_registry.resolve = AsyncMock(return_value=mock_stt)

        status = await get_voice_status(mock_registry)

        assert status["stt"]["health"] == "degraded"

    @pytest.mark.asyncio()
    async def test_stt_closed_is_failed(self, mock_registry: MagicMock) -> None:
        from sovyx.voice.stt import MoonshineSTT, STTEngine, STTState

        mock_stt = MagicMock(spec=MoonshineSTT)
        mock_stt.state = STTState.CLOSED
        mock_registry.is_registered = self._only(STTEngine)
        mock_registry.resolve = AsyncMock(return_value=mock_stt)

        status = await get_voice_status(mock_registry)

        assert status["stt"]["health"] == "failed"

    @pytest.mark.asyncio()
    async def test_stt_registered_but_resolve_fails_is_unknown_not_healthy(
        self, mock_registry: MagicMock
    ) -> None:
        """Headline P0-1 regression — a registered STT whose instance can't
        be resolved (the 'registered but broken' case) must report
        ``unknown``, never ``healthy``, and must not invent an engine name.
        """
        from sovyx.voice.stt import STTEngine

        mock_registry.is_registered = self._only(STTEngine)
        mock_registry.resolve = AsyncMock(side_effect=RuntimeError("broken"))

        status = await get_voice_status(mock_registry)

        assert status["stt"]["health"] == "unknown"
        assert status["stt"]["health"] != "healthy"
        assert status["stt"]["engine"] is None

    @pytest.mark.asyncio()
    async def test_tts_not_initialized_is_degraded_not_healthy(
        self, mock_registry: MagicMock
    ) -> None:
        from sovyx.voice.tts_piper import PiperConfig, PiperTTS, TTSEngine

        mock_tts = MagicMock(spec=PiperTTS)
        mock_tts.config = PiperConfig(voice="en_US-lessac-medium")
        mock_tts.is_initialized = False
        mock_registry.is_registered = self._only(TTSEngine)
        mock_registry.resolve = AsyncMock(return_value=mock_tts)

        status = await get_voice_status(mock_registry)

        assert status["tts"]["initialized"] is False
        assert status["tts"]["health"] == "degraded"

    @pytest.mark.asyncio()
    async def test_vad_session_unrecoverable_is_failed(self, mock_registry: MagicMock) -> None:
        from sovyx.voice.vad import SileroVAD

        mock_vad = MagicMock(spec=SileroVAD)
        mock_vad.is_session_unrecoverable = True
        mock_vad.corruption_count = 3
        mock_registry.is_registered = self._only(SileroVAD)
        mock_registry.resolve = AsyncMock(return_value=mock_vad)

        status = await get_voice_status(mock_registry)

        # Registered (enabled) but the ONNX session is dead → failed, NOT
        # a green/healthy dot.
        assert status["vad"]["enabled"] is True
        assert status["vad"]["health"] == "failed"

    @pytest.mark.asyncio()
    async def test_vad_corruption_without_unrecoverable_is_degraded(
        self, mock_registry: MagicMock
    ) -> None:
        from sovyx.voice.vad import SileroVAD

        mock_vad = MagicMock(spec=SileroVAD)
        mock_vad.is_session_unrecoverable = False
        mock_vad.corruption_count = 2
        mock_registry.is_registered = self._only(SileroVAD)
        mock_registry.resolve = AsyncMock(return_value=mock_vad)

        status = await get_voice_status(mock_registry)

        assert status["vad"]["health"] == "degraded"

    @pytest.mark.asyncio()
    async def test_pipeline_latency_surfaced_when_measured(self, mock_registry: MagicMock) -> None:
        """LIVE-2 P1-3 — the pipeline's persisted last-utterance latency is
        surfaced on ``pipeline.latency_ms`` (previously always None / "—")."""
        from sovyx.voice.pipeline import VoicePipeline, VoicePipelineState

        mock_pipeline = MagicMock(spec=VoicePipeline)
        mock_pipeline.is_running = True
        mock_pipeline.state = VoicePipelineState.IDLE
        mock_pipeline.last_stt_latency_ms = 88.0
        mock_registry.is_registered = self._only(VoicePipeline)
        mock_registry.resolve = AsyncMock(return_value=mock_pipeline)

        status = await get_voice_status(mock_registry)

        assert status["pipeline"]["latency_ms"] == 88.0  # noqa: PLR2004

    @pytest.mark.asyncio()
    async def test_pipeline_latency_none_before_first_utterance(
        self, mock_registry: MagicMock
    ) -> None:
        """LIVE-2 P1-3 — None (not a fabricated number) until a turn
        completes; the frontend renders an explanatory pending state."""
        from sovyx.voice.pipeline import VoicePipeline, VoicePipelineState

        mock_pipeline = MagicMock(spec=VoicePipeline)
        mock_pipeline.is_running = True
        mock_pipeline.state = VoicePipelineState.IDLE
        mock_pipeline.last_stt_latency_ms = None
        mock_registry.is_registered = self._only(VoicePipeline)
        mock_registry.resolve = AsyncMock(return_value=mock_pipeline)

        status = await get_voice_status(mock_registry)

        assert status["pipeline"]["latency_ms"] is None

    @pytest.mark.asyncio()
    async def test_wyoming_configured_running_reads_running_and_host_port(
        self, mock_registry: MagicMock
    ) -> None:
        """LIVE-2 P1-10 — reads the real ``running`` property (not the
        non-existent ``is_running``) and composes the endpoint from
        ``host``:``port`` (``WyomingConfig`` has no ``endpoint`` field).
        Both were latent bugs that would keep a live server showing
        "Disconnected" with no endpoint.
        """
        from sovyx.voice.wyoming import SovyxWyomingServer, WyomingConfig

        mock_wy = MagicMock(spec=SovyxWyomingServer)
        mock_wy.running = True
        mock_wy.config = WyomingConfig()  # host=127.0.0.1, port=10700
        mock_registry.is_registered = self._only(SovyxWyomingServer)
        mock_registry.resolve = AsyncMock(return_value=mock_wy)

        status = await get_voice_status(mock_registry)

        assert status["wyoming"]["configured"] is True
        assert status["wyoming"]["connected"] is True
        assert status["wyoming"]["endpoint"] == "127.0.0.1:10700"

    @pytest.mark.asyncio()
    async def test_wyoming_configured_but_not_running(self, mock_registry: MagicMock) -> None:
        from sovyx.voice.wyoming import SovyxWyomingServer, WyomingConfig

        mock_wy = MagicMock(spec=SovyxWyomingServer)
        mock_wy.running = False
        mock_wy.config = WyomingConfig()
        mock_registry.is_registered = self._only(SovyxWyomingServer)
        mock_registry.resolve = AsyncMock(return_value=mock_wy)

        status = await get_voice_status(mock_registry)

        # Registered → card shows; not running → "Disconnected" truthfully.
        assert status["wyoming"]["configured"] is True
        assert status["wyoming"]["connected"] is False

    @pytest.mark.asyncio()
    async def test_health_values_are_within_the_ssot_vocabulary(
        self, mock_registry: MagicMock
    ) -> None:
        """Every emitted health value belongs to the SSoT set."""
        from sovyx.dashboard.voice_status import VOICE_HEALTH_VALUES

        status = await get_voice_status(mock_registry)
        for axis in ("stt", "tts", "wake_word", "vad"):
            assert status[axis]["health"] in VOICE_HEALTH_VALUES

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
            # Mission C3 §T2.8 — server-side degraded-mode marker.
            "degraded",
            "preflight_warnings",
        }
        assert set(status.keys()) == expected_keys


class TestH2CaptureBypassEventMetadata:
    """Mission H2 §T2.10 — VoiceStatusCapture forward-additive platform metadata.

    Quality Gate 8 (anti-pattern #40) round-trip pair: the producer dict
    shape in :mod:`sovyx.dashboard.voice_status.get_voice_status` MUST
    parse through :class:`VoiceStatusResponse.model_validate` for every
    populated value of ``last_bypass_event_platform`` +
    ``last_bypass_event_family``. Without this pairing the v0.51.0
    promotion of these fields from optional to required would surface
    silent boundary drift across the dual-emission window.
    """

    @pytest.mark.asyncio()
    async def test_pristine_status_carries_null_platform_metadata(
        self, mock_registry: MagicMock
    ) -> None:
        """Default snapshot — no bypass dispatch has fired yet — carries
        ``None`` for the new platform-metadata fields."""
        status = await get_voice_status(mock_registry)
        assert status["capture"]["last_bypass_event_platform"] is None
        assert status["capture"]["last_bypass_event_family"] is None

    def test_round_trip_with_linux_alsa_metadata(self) -> None:
        """Operator's L1067 forensic shape (Linux Mint + ALSA capture
        chain) parses through ``VoiceStatusResponse.model_validate``."""
        from sovyx.dashboard.routes.voice import VoiceStatusResponse

        shape = {
            "pipeline": {"running": True, "state": "listening", "latency_ms": 22.5},
            "capture": {
                "running": True,
                "input_device": 5,
                "host_api": "ALSA",
                "sample_rate": 16_000,
                "frames_delivered": 50,
                "last_rms_db": -54.4,
                # Mission H2 §T2.10 platform metadata
                "last_bypass_event_platform": "linux",
                "last_bypass_event_family": "alsa_capture_chain",
            },
            "stt": {"engine": "MoonshineSTT", "model": "moonshine-tiny", "state": "ready"},
            "tts": {"engine": "PiperTTS", "model": "pt_BR-faber-medium", "initialized": True},
            "wake_word": {"enabled": False, "phrase": None},
            "vad": {"enabled": True},
            "wyoming": {"connected": False, "endpoint": None},
            "hardware": {"tier": "MINI_PC", "ram_mb": 8192},
            "preflight_warnings": [],
        }
        response = VoiceStatusResponse.model_validate(shape)
        assert response.capture.last_bypass_event_platform == "linux"
        assert response.capture.last_bypass_event_family == "alsa_capture_chain"

    def test_round_trip_with_windows_voice_clarity_metadata(self) -> None:
        """Windows Voice Clarity dispatch shape parses cleanly."""
        from sovyx.dashboard.routes.voice import VoiceStatusResponse

        shape = {
            "capture": {
                "running": True,
                "last_bypass_event_platform": "windows",
                "last_bypass_event_family": "voice_clarity",
            },
        }
        response = VoiceStatusResponse.model_validate(shape)
        assert response.capture.last_bypass_event_platform == "windows"
        assert response.capture.last_bypass_event_family == "voice_clarity"

    def test_round_trip_with_null_metadata_is_forward_compatible(self) -> None:
        """Legacy clients (pre-Phase-1.B status snapshots) MUST continue
        to round-trip — ``None``-valued metadata fields are explicit."""
        from sovyx.dashboard.routes.voice import VoiceStatusResponse

        shape = {
            "capture": {
                "running": False,
                "last_bypass_event_platform": None,
                "last_bypass_event_family": None,
            },
        }
        response = VoiceStatusResponse.model_validate(shape)
        assert response.capture.last_bypass_event_platform is None
        assert response.capture.last_bypass_event_family is None

    def test_round_trip_without_h2_fields_is_backwards_compatible(self) -> None:
        """Pre-mission status payloads (no platform-metadata fields at
        all) MUST continue to validate — anti-pattern #29 forward-
        additive optional discipline."""
        from sovyx.dashboard.routes.voice import VoiceStatusResponse

        shape = {
            "capture": {
                "running": True,
                "input_device": 5,
                "host_api": "ALSA",
                "sample_rate": 16_000,
                "frames_delivered": 50,
                "last_rms_db": -54.4,
            },
        }
        response = VoiceStatusResponse.model_validate(shape)
        # Both metadata fields default to None when absent.
        assert response.capture.last_bypass_event_platform is None
        assert response.capture.last_bypass_event_family is None

    def test_unknown_platform_value_passes_extra_allow(self) -> None:
        """``extra="allow"`` preserves forward-additive shape — operator
        platforms like ``freebsd`` / ``wsl`` flow through unblocked even
        before the Literal enum is widened."""
        from sovyx.dashboard.routes.voice import VoiceStatusResponse

        shape = {
            "capture": {
                "running": True,
                "last_bypass_event_platform": "other",
                "last_bypass_event_family": "alsa_capture_chain",
            },
        }
        response = VoiceStatusResponse.model_validate(shape)
        assert response.capture.last_bypass_event_platform == "other"


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

    @pytest.mark.asyncio()
    async def test_tier_entries_carry_availability_flags(self, mock_registry: MagicMock) -> None:
        """ENGINES-2 (AP #48) — every tier entry carries an ``available``
        map flagging, per surfaced role, whether the named model has real
        runtime backing (the matrix is partly roadmap fiction)."""
        models = await get_voice_models(mock_registry)

        for _tier_name, tier_data in models["available_tiers"].items():
            available = tier_data["available"]
            assert set(available) == {
                "stt_primary",
                "stt_streaming",
                "tts_primary",
                "tts_quality",
                "wake",
                "vad",
            }
            assert all(isinstance(v, bool) for v in available.values())

    @pytest.mark.asyncio()
    async def test_phantom_matrix_entries_flagged_unavailable(
        self, mock_registry: MagicMock
    ) -> None:
        """N100's parakeet STT has no engine/download at HEAD → False;
        PI5's moonshine-tiny is fully shipped → True."""
        models = await get_voice_models(mock_registry)

        pi5 = models["available_tiers"]["PI5"]
        assert pi5["stt_primary"] == "moonshine-tiny"
        assert pi5["available"]["stt_primary"] is True

        n100 = models["available_tiers"]["N100"]
        assert n100["stt_primary"] == "parakeet-tdt-0.6b-v3-int8"
        assert n100["available"]["stt_primary"] is False
        assert n100["available"]["tts_quality"] is False  # kokoro-onnx-fp32

    @pytest.mark.asyncio()
    async def test_availability_survives_route_boundary_model(
        self, mock_registry: MagicMock
    ) -> None:
        """AP #40/#53 — the pydantic route twin must NOT silently strip
        the additive ``available`` field (default pydantic extra-ignore
        would have dropped an undeclared key)."""
        from sovyx.dashboard.routes.voice import VoiceModelsResponse

        models = await get_voice_models(mock_registry)
        validated = VoiceModelsResponse.model_validate(models)

        n100 = validated.available_tiers["N100"]
        assert n100.available["stt_primary"] is False
        pi5 = validated.available_tiers["PI5"]
        assert pi5.available["stt_primary"] is True


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
