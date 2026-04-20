"""Tests for /api/voice/* endpoints — hardware-detect, enable, disable."""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.testclient import TestClient

from sovyx.dashboard.server import create_app

_TOKEN = "test-token-voice"


@pytest.fixture()
def app():
    application = create_app(token=_TOKEN)
    registry = MagicMock()
    registry.is_registered.return_value = False
    registry.resolve = AsyncMock()
    application.state.registry = registry
    application.state.mind_yaml_path = None
    application.state.mind_id = "test-mind"
    return application


@pytest.fixture()
def client(app) -> TestClient:
    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})


class TestHardwareDetect:
    """GET /api/voice/hardware-detect."""

    def test_returns_hardware_info(self, client: TestClient) -> None:
        hw = MagicMock()
        hw.cpu_cores = 8
        hw.ram_mb = 16384
        hw.has_gpu = True
        hw.gpu_vram_mb = 4096
        hw.tier.name = "DESKTOP_GPU"

        with (
            patch(
                "sovyx.voice.auto_select.detect_hardware",
                return_value=hw,
            ),
            patch("sovyx.voice.model_registry.get_models_for_tier", return_value=[]),
        ):
            resp = client.get("/api/voice/hardware-detect")

        assert resp.status_code == 200  # noqa: PLR2004
        data = resp.json()
        assert data["hardware"]["cpu_cores"] == 8  # noqa: PLR2004
        assert data["hardware"]["has_gpu"] is True
        assert data["hardware"]["tier"] == "DESKTOP_GPU"
        assert "audio" in data
        assert "recommended_models" in data

    def test_hardware_detect_failure(self, client: TestClient) -> None:
        with patch(
            "sovyx.voice.auto_select.detect_hardware",
            side_effect=RuntimeError("no psutil"),
        ):
            resp = client.get("/api/voice/hardware-detect")
        assert resp.status_code == 500  # noqa: PLR2004
        assert "error" in resp.json()

    def test_no_auth_401(self) -> None:
        app = create_app(token=_TOKEN)
        app.state.registry = MagicMock()
        c = TestClient(app)
        resp = c.get("/api/voice/hardware-detect")
        assert resp.status_code == 401  # noqa: PLR2004


class TestEnableVoice:
    """POST /api/voice/enable."""

    def test_missing_deps_returns_400(self, client: TestClient) -> None:
        missing = [{"module": "moonshine_voice", "package": "moonshine-voice"}]
        with (
            patch(
                "sovyx.voice.model_registry.check_voice_deps",
                return_value=([], missing),
            ),
            patch("sovyx.voice.model_registry.detect_tts_engine", return_value="piper"),
        ):
            resp = client.post("/api/voice/enable")
        assert resp.status_code == 400  # noqa: PLR2004
        data = resp.json()
        assert data["error"] == "missing_deps"
        assert len(data["missing_deps"]) == 1
        assert "install_command" in data

    def test_no_tts_returns_400(self, client: TestClient) -> None:
        with (
            patch(
                "sovyx.voice.model_registry.check_voice_deps",
                return_value=(
                    [{"module": "moonshine_voice", "package": "moonshine-voice"}],
                    [],
                ),
            ),
            patch("sovyx.voice.model_registry.detect_tts_engine", return_value="none"),
        ):
            resp = client.post("/api/voice/enable")
        assert resp.status_code == 400  # noqa: PLR2004
        assert resp.json()["error"] == "missing_deps"

    def test_no_audio_returns_400(self, client: TestClient) -> None:
        fake_sd = ModuleType("sounddevice")
        fake_sd.query_devices = MagicMock(return_value=[])  # type: ignore[attr-defined]
        with (
            patch(
                "sovyx.voice.model_registry.check_voice_deps",
                return_value=(
                    [
                        {"module": "moonshine_voice", "package": "moonshine-voice"},
                        {"module": "sounddevice", "package": "sounddevice"},
                    ],
                    [],
                ),
            ),
            patch("sovyx.voice.model_registry.detect_tts_engine", return_value="piper"),
            patch.dict(sys.modules, {"sounddevice": fake_sd}),
        ):
            resp = client.post("/api/voice/enable")
        assert resp.status_code == 400  # noqa: PLR2004
        assert "audio" in resp.json()["error"].lower()

    def test_idempotent_when_already_active(self, app, client: TestClient) -> None:
        app.state.registry.is_registered.return_value = True
        fake_sd = ModuleType("sounddevice")
        fake_sd.query_devices = MagicMock(  # type: ignore[attr-defined]
            return_value=[
                {"name": "mic", "max_input_channels": 1, "max_output_channels": 0},
                {"name": "spk", "max_input_channels": 0, "max_output_channels": 2},
            ],
        )
        with (
            patch(
                "sovyx.voice.model_registry.check_voice_deps",
                return_value=(
                    [
                        {"module": "moonshine_voice", "package": "moonshine-voice"},
                        {"module": "sounddevice", "package": "sounddevice"},
                    ],
                    [],
                ),
            ),
            patch("sovyx.voice.model_registry.detect_tts_engine", return_value="piper"),
            patch.dict(sys.modules, {"sounddevice": fake_sd}),
        ):
            resp = client.post("/api/voice/enable")
        assert resp.status_code == 200  # noqa: PLR2004
        assert resp.json()["status"] == "already_active"


class TestEnableVoiceCaptureWiring:
    """Enable happy path starts the capture task and registers both services."""

    def test_enable_starts_capture_and_registers_bundle(self, app, client: TestClient) -> None:
        fake_sd = ModuleType("sounddevice")
        fake_sd.query_devices = MagicMock(  # type: ignore[attr-defined]
            return_value=[
                {"name": "mic", "max_input_channels": 1, "max_output_channels": 0},
                {"name": "spk", "max_input_channels": 0, "max_output_channels": 2},
            ],
        )

        capture = MagicMock()
        capture.start = AsyncMock()
        pipeline = MagicMock()
        pipeline.stop = AsyncMock()
        pipeline.config.wake_word_enabled = False

        from sovyx.voice.factory import VoiceBundle

        bundle = VoiceBundle(pipeline=pipeline, capture_task=capture)

        with (
            patch(
                "sovyx.voice.model_registry.check_voice_deps",
                return_value=(
                    [
                        {"module": "moonshine_voice", "package": "moonshine-voice"},
                        {"module": "sounddevice", "package": "sounddevice"},
                    ],
                    [],
                ),
            ),
            patch("sovyx.voice.model_registry.detect_tts_engine", return_value="piper"),
            patch.dict(sys.modules, {"sounddevice": fake_sd}),
            patch(
                "sovyx.voice.factory.create_voice_pipeline",
                new=AsyncMock(return_value=bundle),
            ),
        ):
            resp = client.post(
                "/api/voice/enable",
                json={"input_device": 1, "output_device": 2},
            )

        assert resp.status_code == 200  # noqa: PLR2004
        assert resp.json()["status"] == "active"
        capture.start.assert_awaited_once()
        # Pipeline + capture + sub-components registered for status cards.
        registered = [
            call.args[0].__name__ for call in app.state.registry.register_instance.mock_calls
        ]
        assert "VoicePipeline" in registered
        assert "AudioCaptureTask" in registered
        assert "SileroVAD" in registered
        # STTEngine / TTSEngine are ABCs — verify the concrete register
        # call uses the interface as the key.
        assert "STTEngine" in registered
        assert "TTSEngine" in registered
        # Wake word disabled — must not be registered.
        assert "WakeWordDetector" not in registered

    def test_enable_registers_wake_word_when_enabled(self, app, client: TestClient) -> None:
        """Wake-word-enabled pipelines also register the WakeWordDetector."""
        fake_sd = ModuleType("sounddevice")
        fake_sd.query_devices = MagicMock(  # type: ignore[attr-defined]
            return_value=[
                {"name": "mic", "max_input_channels": 1, "max_output_channels": 0},
                {"name": "spk", "max_input_channels": 0, "max_output_channels": 2},
            ],
        )

        capture = MagicMock()
        capture.start = AsyncMock()
        pipeline = MagicMock()
        pipeline.stop = AsyncMock()
        pipeline.config.wake_word_enabled = True

        from sovyx.voice.factory import VoiceBundle

        bundle = VoiceBundle(pipeline=pipeline, capture_task=capture)

        with (
            patch(
                "sovyx.voice.model_registry.check_voice_deps",
                return_value=(
                    [
                        {"module": "moonshine_voice", "package": "moonshine-voice"},
                        {"module": "sounddevice", "package": "sounddevice"},
                    ],
                    [],
                ),
            ),
            patch("sovyx.voice.model_registry.detect_tts_engine", return_value="piper"),
            patch.dict(sys.modules, {"sounddevice": fake_sd}),
            patch(
                "sovyx.voice.factory.create_voice_pipeline",
                new=AsyncMock(return_value=bundle),
            ),
        ):
            resp = client.post("/api/voice/enable")

        assert resp.status_code == 200  # noqa: PLR2004
        registered = [
            call.args[0].__name__ for call in app.state.registry.register_instance.mock_calls
        ]
        assert "WakeWordDetector" in registered

    def test_enable_tears_down_when_capture_start_fails(self, app, client: TestClient) -> None:
        fake_sd = ModuleType("sounddevice")
        fake_sd.query_devices = MagicMock(  # type: ignore[attr-defined]
            return_value=[
                {"name": "mic", "max_input_channels": 1, "max_output_channels": 0},
                {"name": "spk", "max_input_channels": 0, "max_output_channels": 2},
            ],
        )

        capture = MagicMock()
        capture.start = AsyncMock(side_effect=RuntimeError("device busy"))
        pipeline = MagicMock()
        pipeline.stop = AsyncMock()

        from sovyx.voice.factory import VoiceBundle

        bundle = VoiceBundle(pipeline=pipeline, capture_task=capture)

        with (
            patch(
                "sovyx.voice.model_registry.check_voice_deps",
                return_value=(
                    [
                        {"module": "moonshine_voice", "package": "moonshine-voice"},
                        {"module": "sounddevice", "package": "sounddevice"},
                    ],
                    [],
                ),
            ),
            patch("sovyx.voice.model_registry.detect_tts_engine", return_value="piper"),
            patch.dict(sys.modules, {"sounddevice": fake_sd}),
            patch(
                "sovyx.voice.factory.create_voice_pipeline",
                new=AsyncMock(return_value=bundle),
            ),
        ):
            resp = client.post("/api/voice/enable")

        assert resp.status_code == 500  # noqa: PLR2004
        pipeline.stop.assert_awaited_once()
        # Nothing got registered when capture failed to start.
        app.state.registry.register_instance.assert_not_called()

    def test_enable_returns_503_when_cascade_declares_inoperative(
        self, app, client: TestClient
    ) -> None:
        """v0.20.2 §4.4.7 / Bug D — CaptureInoperativeError → 503.

        The factory raises ``CaptureInoperativeError`` when the VCHL
        boot cascade exhausts every viable combo. The route must map
        that to HTTP 503 with a structured body so the UI can show a
        real "no working microphone" prompt instead of a generic 500.
        """
        fake_sd = ModuleType("sounddevice")
        fake_sd.query_devices = MagicMock(  # type: ignore[attr-defined]
            return_value=[
                {"name": "mic", "max_input_channels": 1, "max_output_channels": 0},
                {"name": "spk", "max_input_channels": 0, "max_output_channels": 2},
            ],
        )

        from sovyx.voice._capture_task import CaptureInoperativeError

        exc = CaptureInoperativeError(
            "cascade exhausted",
            device=3,
            host_api="Windows WASAPI",
            reason="no_winner",
            attempts=5,
        )

        with (
            patch(
                "sovyx.voice.model_registry.check_voice_deps",
                return_value=(
                    [
                        {"module": "moonshine_voice", "package": "moonshine-voice"},
                        {"module": "sounddevice", "package": "sounddevice"},
                    ],
                    [],
                ),
            ),
            patch("sovyx.voice.model_registry.detect_tts_engine", return_value="piper"),
            patch.dict(sys.modules, {"sounddevice": fake_sd}),
            patch(
                "sovyx.voice.factory.create_voice_pipeline",
                new=AsyncMock(side_effect=exc),
            ),
        ):
            resp = client.post("/api/voice/enable")

        assert resp.status_code == 503  # noqa: PLR2004
        body = resp.json()
        assert body["ok"] is False
        assert body["error"] == "capture_inoperative"
        assert body["device"] == 3  # noqa: PLR2004
        assert body["host_api"] == "Windows WASAPI"
        assert body["reason"] == "no_winner"
        assert body["attempts"] == 5  # noqa: PLR2004
        # Cascade failure happens BEFORE bundle exists, so no registry
        # writes and no pipeline tear-down.
        app.state.registry.register_instance.assert_not_called()

    def test_enable_closes_live_voice_test_sessions_before_factory(
        self, app, client: TestClient
    ) -> None:
        """v0.20.2 / Bug B — /enable must release the browser meter's mic.

        A live voice_test session holds PortAudio open on the capture
        endpoint; if /enable calls into the factory while that session
        is running, every cascade probe fails with DEVICE_BUSY. The
        route closes + waits for every voice_test session BEFORE
        invoking ``create_voice_pipeline``.
        """
        from sovyx.voice.device_test import CloseReason, SessionRegistry

        registry_spy = SessionRegistry(max_per_token=1, force_close_grace_s=0.05)
        app.state.voice_test_registry = registry_spy

        fake_sd = ModuleType("sounddevice")
        fake_sd.query_devices = MagicMock(  # type: ignore[attr-defined]
            return_value=[
                {"name": "mic", "max_input_channels": 1, "max_output_channels": 0},
                {"name": "spk", "max_input_channels": 0, "max_output_channels": 2},
            ],
        )

        capture = MagicMock()
        capture.start = AsyncMock()
        pipeline = MagicMock()
        pipeline.stop = AsyncMock()
        pipeline.config.wake_word_enabled = False

        from sovyx.voice.factory import VoiceBundle

        bundle = VoiceBundle(pipeline=pipeline, capture_task=capture)

        call_order: list[str] = []
        close_all_kwargs: dict[str, object] = {}

        async def _tracked_close_all(*, reason: CloseReason = CloseReason.SERVER_SHUTDOWN) -> None:
            call_order.append("close_all")
            close_all_kwargs["reason"] = reason

        async def _tracked_factory(*args: object, **kwargs: object) -> VoiceBundle:
            call_order.append("factory")
            return bundle

        registry_spy.close_all = _tracked_close_all  # type: ignore[method-assign]

        with (
            patch(
                "sovyx.voice.model_registry.check_voice_deps",
                return_value=(
                    [
                        {"module": "moonshine_voice", "package": "moonshine-voice"},
                        {"module": "sounddevice", "package": "sounddevice"},
                    ],
                    [],
                ),
            ),
            patch("sovyx.voice.model_registry.detect_tts_engine", return_value="piper"),
            patch.dict(sys.modules, {"sounddevice": fake_sd}),
            patch(
                "sovyx.voice.factory.create_voice_pipeline",
                new=AsyncMock(side_effect=_tracked_factory),
            ),
        ):
            resp = client.post("/api/voice/enable")

        assert resp.status_code == 200  # noqa: PLR2004
        # Invariant: close_all happens BEFORE create_voice_pipeline.
        # If ordering slips, the factory probes the mic while the old
        # voice_test session still owns the PortAudio endpoint, and
        # every cascade probe hits DEVICE_BUSY.
        assert call_order == ["close_all", "factory"]
        assert close_all_kwargs == {"reason": CloseReason.SERVER_SHUTDOWN}


class TestEnableVoiceLanguageCoherence:
    """``/enable`` forwards the mind's language + voice_id into the factory.

    Before Phase 3, the factory ignored MindConfig entirely and always
    built a Kokoro engine with the hardcoded English ``af_bella``
    default. A mind with ``language="pt"`` still spoke English in the
    live pipeline — the setup wizard's voice test was coherent but
    real conversation wasn't. These tests lock in that the enable
    route reads ``request.app.state.mind_config`` and forwards both
    fields to ``create_voice_pipeline``.
    """

    def _fake_sd_with_devices(self) -> ModuleType:
        fake_sd = ModuleType("sounddevice")
        fake_sd.query_devices = MagicMock(  # type: ignore[attr-defined]
            return_value=[
                {"name": "mic", "max_input_channels": 1, "max_output_channels": 0},
                {"name": "spk", "max_input_channels": 0, "max_output_channels": 2},
            ],
        )
        return fake_sd

    def test_enable_forwards_mind_language_and_voice_id(self, app, client: TestClient) -> None:
        """MindConfig(language='pt', voice_id='pf_dora') → factory gets both."""
        # Stand-in for MindConfig — the route only reads ``language`` and
        # ``voice_id`` via ``getattr``, so a duck-typed object is enough
        # and keeps the test independent of pydantic validation churn.
        mind_config = MagicMock()
        mind_config.language = "pt"
        mind_config.voice_id = "pf_dora"
        mind_config.llm.streaming = True
        app.state.mind_config = mind_config

        capture = MagicMock()
        capture.start = AsyncMock()
        pipeline = MagicMock()
        pipeline.stop = AsyncMock()
        pipeline.config.wake_word_enabled = False

        from sovyx.voice.factory import VoiceBundle

        bundle = VoiceBundle(pipeline=pipeline, capture_task=capture)
        factory_mock = AsyncMock(return_value=bundle)

        with (
            patch(
                "sovyx.voice.model_registry.check_voice_deps",
                return_value=(
                    [
                        {"module": "moonshine_voice", "package": "moonshine-voice"},
                        {"module": "sounddevice", "package": "sounddevice"},
                    ],
                    [],
                ),
            ),
            patch("sovyx.voice.model_registry.detect_tts_engine", return_value="piper"),
            patch.dict(sys.modules, {"sounddevice": self._fake_sd_with_devices()}),
            patch("sovyx.voice.factory.create_voice_pipeline", new=factory_mock),
        ):
            resp = client.post("/api/voice/enable")

        assert resp.status_code == 200  # noqa: PLR2004
        kwargs = factory_mock.call_args.kwargs
        assert kwargs["language"] == "pt"
        assert kwargs["voice_id"] == "pf_dora"

    def test_enable_defaults_when_no_mind_config(self, app, client: TestClient) -> None:
        """No mind_config on app.state → factory sees English defaults, empty voice_id."""
        # Explicitly clear anything a previous test may have set.
        app.state.mind_config = None

        capture = MagicMock()
        capture.start = AsyncMock()
        pipeline = MagicMock()
        pipeline.stop = AsyncMock()
        pipeline.config.wake_word_enabled = False

        from sovyx.voice.factory import VoiceBundle

        bundle = VoiceBundle(pipeline=pipeline, capture_task=capture)
        factory_mock = AsyncMock(return_value=bundle)

        with (
            patch(
                "sovyx.voice.model_registry.check_voice_deps",
                return_value=(
                    [
                        {"module": "moonshine_voice", "package": "moonshine-voice"},
                        {"module": "sounddevice", "package": "sounddevice"},
                    ],
                    [],
                ),
            ),
            patch("sovyx.voice.model_registry.detect_tts_engine", return_value="piper"),
            patch.dict(sys.modules, {"sounddevice": self._fake_sd_with_devices()}),
            patch("sovyx.voice.factory.create_voice_pipeline", new=factory_mock),
        ):
            resp = client.post("/api/voice/enable")

        assert resp.status_code == 200  # noqa: PLR2004
        kwargs = factory_mock.call_args.kwargs
        assert kwargs["language"] == "en"
        assert kwargs["voice_id"] == ""


class TestEnableVoiceRequestBodyOverride:
    """``/enable`` accepts ``voice_id`` + ``language`` in the body.

    The wizard's voice-test picker surfaces these values via
    ``VoiceStep``. They must:

    * win over any stale ``MindConfig.voice_id`` (user just picked a new
      voice — respect the live pick);
    * get validated against the voice catalog before any model load so
      a typo doesn't turn into an opaque ONNX error;
    * be persisted to ``mind.yaml`` via ``ConfigEditor.set_scalar`` so
      the next daemon boot picks up the same voice.
    """

    def _fake_sd_with_devices(self) -> ModuleType:
        fake_sd = ModuleType("sounddevice")
        fake_sd.query_devices = MagicMock(  # type: ignore[attr-defined]
            return_value=[
                {"name": "mic", "max_input_channels": 1, "max_output_channels": 0},
                {"name": "spk", "max_input_channels": 0, "max_output_channels": 2},
            ],
        )
        return fake_sd

    def test_request_body_overrides_mind_config(self, app, client: TestClient) -> None:
        """Body ``voice_id`` wins over ``app.state.mind_config.voice_id``."""
        mind_config = MagicMock()
        mind_config.language = "en"
        mind_config.voice_id = "af_bella"
        mind_config.llm.streaming = True
        app.state.mind_config = mind_config

        capture = MagicMock()
        capture.start = AsyncMock()
        pipeline = MagicMock()
        pipeline.stop = AsyncMock()
        pipeline.config.wake_word_enabled = False

        from sovyx.voice.factory import VoiceBundle

        bundle = VoiceBundle(pipeline=pipeline, capture_task=capture)
        factory_mock = AsyncMock(return_value=bundle)

        with (
            patch(
                "sovyx.voice.model_registry.check_voice_deps",
                return_value=(
                    [
                        {"module": "moonshine_voice", "package": "moonshine-voice"},
                        {"module": "sounddevice", "package": "sounddevice"},
                    ],
                    [],
                ),
            ),
            patch("sovyx.voice.model_registry.detect_tts_engine", return_value="kokoro"),
            patch.dict(sys.modules, {"sounddevice": self._fake_sd_with_devices()}),
            patch("sovyx.voice.factory.create_voice_pipeline", new=factory_mock),
        ):
            resp = client.post(
                "/api/voice/enable",
                json={"voice_id": "pf_dora", "language": "pt"},
            )

        assert resp.status_code == 200  # noqa: PLR2004
        kwargs = factory_mock.call_args.kwargs
        assert kwargs["voice_id"] == "pf_dora"
        assert kwargs["language"] == "pt"
        # Live pick replaces the in-memory MindConfig in place so the
        # next /enable without a body still sees the fresh pick.
        assert mind_config.voice_id == "pf_dora"
        assert mind_config.language == "pt"

    def test_invalid_voice_id_returns_400(self, app, client: TestClient) -> None:
        """Unknown voice id → 400 before any models load."""
        app.state.mind_config = None

        factory_mock = AsyncMock()

        with (
            patch(
                "sovyx.voice.model_registry.check_voice_deps",
                return_value=([{"module": "m", "package": "m"}], []),
            ),
            patch("sovyx.voice.model_registry.detect_tts_engine", return_value="kokoro"),
            patch.dict(sys.modules, {"sounddevice": self._fake_sd_with_devices()}),
            patch("sovyx.voice.factory.create_voice_pipeline", new=factory_mock),
        ):
            resp = client.post(
                "/api/voice/enable",
                json={"voice_id": "zz_nobody"},
            )

        assert resp.status_code == 400  # noqa: PLR2004
        assert "Unknown voice id" in resp.json()["error"]
        factory_mock.assert_not_awaited()

    def test_invalid_language_returns_400(self, app, client: TestClient) -> None:
        """Unsupported language code → 400 before any models load."""
        app.state.mind_config = None

        factory_mock = AsyncMock()

        with (
            patch(
                "sovyx.voice.model_registry.check_voice_deps",
                return_value=([{"module": "m", "package": "m"}], []),
            ),
            patch("sovyx.voice.model_registry.detect_tts_engine", return_value="kokoro"),
            patch.dict(sys.modules, {"sounddevice": self._fake_sd_with_devices()}),
            patch("sovyx.voice.factory.create_voice_pipeline", new=factory_mock),
        ):
            resp = client.post(
                "/api/voice/enable",
                json={"language": "xx-yy"},
            )

        assert resp.status_code == 400  # noqa: PLR2004
        assert "Unsupported language" in resp.json()["error"]
        factory_mock.assert_not_awaited()

    def test_voice_id_is_persisted_to_mind_yaml(self, app, client: TestClient, tmp_path) -> None:
        """After a successful /enable, mind.yaml carries the new voice_id."""
        mind_yaml = tmp_path / "mind.yaml"
        mind_yaml.write_text("language: en\nvoice_id: af_bella\n", encoding="utf-8")
        app.state.mind_yaml_path = str(mind_yaml)
        app.state.mind_config = None

        capture = MagicMock()
        capture.start = AsyncMock()
        pipeline = MagicMock()
        pipeline.stop = AsyncMock()
        pipeline.config.wake_word_enabled = False

        from sovyx.voice.factory import VoiceBundle

        bundle = VoiceBundle(pipeline=pipeline, capture_task=capture)
        factory_mock = AsyncMock(return_value=bundle)

        with (
            patch(
                "sovyx.voice.model_registry.check_voice_deps",
                return_value=(
                    [
                        {"module": "moonshine_voice", "package": "moonshine-voice"},
                        {"module": "sounddevice", "package": "sounddevice"},
                    ],
                    [],
                ),
            ),
            patch("sovyx.voice.model_registry.detect_tts_engine", return_value="kokoro"),
            patch.dict(sys.modules, {"sounddevice": self._fake_sd_with_devices()}),
            patch("sovyx.voice.factory.create_voice_pipeline", new=factory_mock),
        ):
            resp = client.post(
                "/api/voice/enable",
                json={"voice_id": "pf_dora", "language": "pt"},
            )

        assert resp.status_code == 200  # noqa: PLR2004
        from ruamel.yaml import YAML

        yaml = YAML()
        data = yaml.load(mind_yaml.read_text(encoding="utf-8"))
        assert data["voice_id"] == "pf_dora"
        assert data["language"] == "pt"


class TestEnableVoiceCognitiveWiring:
    """Enable wires VoiceCognitiveBridge when CognitiveLoop is registered."""

    def test_enable_wires_bridge_when_cognitive_loop_registered(
        self, app, client: TestClient
    ) -> None:
        """on_perception is passed to the factory and bridge is registered."""
        from sovyx.cognitive.loop import CognitiveLoop

        fake_sd = ModuleType("sounddevice")
        fake_sd.query_devices = MagicMock(  # type: ignore[attr-defined]
            return_value=[
                {"name": "mic", "max_input_channels": 1, "max_output_channels": 0},
                {"name": "spk", "max_input_channels": 0, "max_output_channels": 2},
            ],
        )

        cog_loop = MagicMock(spec=CognitiveLoop)
        capture = MagicMock()
        capture.start = AsyncMock()
        pipeline = MagicMock()
        pipeline.stop = AsyncMock()
        pipeline.config.wake_word_enabled = False

        from sovyx.voice.factory import VoiceBundle

        bundle = VoiceBundle(pipeline=pipeline, capture_task=capture)

        def is_registered(cls):  # type: ignore[no-untyped-def]
            return cls is CognitiveLoop

        app.state.registry.is_registered.side_effect = is_registered
        app.state.registry.resolve = AsyncMock(return_value=cog_loop)

        factory_mock = AsyncMock(return_value=bundle)
        with (
            patch(
                "sovyx.voice.model_registry.check_voice_deps",
                return_value=(
                    [
                        {"module": "moonshine_voice", "package": "moonshine-voice"},
                        {"module": "sounddevice", "package": "sounddevice"},
                    ],
                    [],
                ),
            ),
            patch("sovyx.voice.model_registry.detect_tts_engine", return_value="piper"),
            patch.dict(sys.modules, {"sounddevice": fake_sd}),
            patch("sovyx.voice.factory.create_voice_pipeline", new=factory_mock),
        ):
            resp = client.post("/api/voice/enable")

        assert resp.status_code == 200  # noqa: PLR2004
        # on_perception was passed (not None) because CognitiveLoop is registered
        kwargs = factory_mock.call_args.kwargs
        assert kwargs["on_perception"] is not None
        assert callable(kwargs["on_perception"])
        # VoiceCognitiveBridge was registered
        registered = [
            call.args[0].__name__ for call in app.state.registry.register_instance.mock_calls
        ]
        assert "VoiceCognitiveBridge" in registered

    def test_enable_skips_bridge_when_no_cognitive_loop(self, app, client: TestClient) -> None:
        """Without CognitiveLoop, on_perception stays None — pipeline still works."""
        fake_sd = ModuleType("sounddevice")
        fake_sd.query_devices = MagicMock(  # type: ignore[attr-defined]
            return_value=[
                {"name": "mic", "max_input_channels": 1, "max_output_channels": 0},
                {"name": "spk", "max_input_channels": 0, "max_output_channels": 2},
            ],
        )

        capture = MagicMock()
        capture.start = AsyncMock()
        pipeline = MagicMock()
        pipeline.stop = AsyncMock()
        pipeline.config.wake_word_enabled = False

        from sovyx.voice.factory import VoiceBundle

        bundle = VoiceBundle(pipeline=pipeline, capture_task=capture)

        factory_mock = AsyncMock(return_value=bundle)
        with (
            patch(
                "sovyx.voice.model_registry.check_voice_deps",
                return_value=(
                    [
                        {"module": "moonshine_voice", "package": "moonshine-voice"},
                        {"module": "sounddevice", "package": "sounddevice"},
                    ],
                    [],
                ),
            ),
            patch("sovyx.voice.model_registry.detect_tts_engine", return_value="piper"),
            patch.dict(sys.modules, {"sounddevice": fake_sd}),
            patch("sovyx.voice.factory.create_voice_pipeline", new=factory_mock),
        ):
            resp = client.post("/api/voice/enable")

        assert resp.status_code == 200  # noqa: PLR2004
        assert factory_mock.call_args.kwargs["on_perception"] is None
        registered = [
            call.args[0].__name__ for call in app.state.registry.register_instance.mock_calls
        ]
        assert "VoiceCognitiveBridge" not in registered


class TestOnPerceptionCallback:
    """The on_perception closure bridges transcription → cognitive loop."""

    @pytest.mark.asyncio
    async def test_callback_submits_cognitive_request_to_bridge(self) -> None:
        """Callback builds a CognitiveRequest and hands it to the bridge."""
        from sovyx.cognitive.loop import CognitiveLoop
        from sovyx.dashboard.server import create_app

        fake_sd = ModuleType("sounddevice")
        fake_sd.query_devices = MagicMock(  # type: ignore[attr-defined]
            return_value=[
                {"name": "mic", "max_input_channels": 1, "max_output_channels": 0},
                {"name": "spk", "max_input_channels": 0, "max_output_channels": 2},
            ],
        )

        application = create_app(token=_TOKEN)
        registry = MagicMock()
        cog_loop = MagicMock(spec=CognitiveLoop)
        registry.is_registered.side_effect = lambda cls: cls is CognitiveLoop
        registry.resolve = AsyncMock(return_value=cog_loop)
        application.state.registry = registry
        application.state.mind_yaml_path = None
        application.state.mind_id = "demo-mind"

        capture = MagicMock()
        capture.start = AsyncMock()
        pipeline = MagicMock()
        pipeline.stop = AsyncMock()
        pipeline.config.wake_word_enabled = False

        from sovyx.voice.factory import VoiceBundle

        bundle = VoiceBundle(pipeline=pipeline, capture_task=capture)
        factory_mock = AsyncMock(return_value=bundle)

        bridge_process = AsyncMock()

        class _StubBridge:
            def __init__(self, *args: object, **kwargs: object) -> None:
                self._args = args
                self._kwargs = kwargs

            async def process(self, req: object) -> None:  # noqa: ANN001
                await bridge_process(req)

        with (
            patch(
                "sovyx.voice.model_registry.check_voice_deps",
                return_value=(
                    [
                        {"module": "moonshine_voice", "package": "moonshine-voice"},
                        {"module": "sounddevice", "package": "sounddevice"},
                    ],
                    [],
                ),
            ),
            patch("sovyx.voice.model_registry.detect_tts_engine", return_value="piper"),
            patch.dict(sys.modules, {"sounddevice": fake_sd}),
            patch("sovyx.voice.factory.create_voice_pipeline", new=factory_mock),
            patch("sovyx.voice.cognitive_bridge.VoiceCognitiveBridge", _StubBridge),
        ):
            client = TestClient(application, headers={"Authorization": f"Bearer {_TOKEN}"})
            resp = client.post("/api/voice/enable")
            assert resp.status_code == 200  # noqa: PLR2004

            # Extract the on_perception callback the route passed to the factory
            on_perception = factory_mock.call_args.kwargs["on_perception"]
            assert on_perception is not None

            # Invoke it — this is what the pipeline does after STT
            await on_perception("what time is it", "demo-mind")

        bridge_process.assert_awaited_once()
        submitted = bridge_process.call_args.args[0]
        assert submitted.perception.content == "what time is it"
        assert submitted.perception.source == "voice"
        assert str(submitted.mind_id) == "demo-mind"

    @pytest.mark.asyncio
    async def test_callback_ignores_empty_text(self) -> None:
        """Empty/whitespace transcriptions are dropped before the bridge."""
        from sovyx.cognitive.loop import CognitiveLoop
        from sovyx.dashboard.server import create_app

        fake_sd = ModuleType("sounddevice")
        fake_sd.query_devices = MagicMock(  # type: ignore[attr-defined]
            return_value=[
                {"name": "mic", "max_input_channels": 1, "max_output_channels": 0},
                {"name": "spk", "max_input_channels": 0, "max_output_channels": 2},
            ],
        )

        application = create_app(token=_TOKEN)
        registry = MagicMock()
        cog_loop = MagicMock(spec=CognitiveLoop)
        registry.is_registered.side_effect = lambda cls: cls is CognitiveLoop
        registry.resolve = AsyncMock(return_value=cog_loop)
        application.state.registry = registry
        application.state.mind_yaml_path = None
        application.state.mind_id = "demo-mind"

        capture = MagicMock()
        capture.start = AsyncMock()
        pipeline = MagicMock()
        pipeline.stop = AsyncMock()
        pipeline.config.wake_word_enabled = False

        from sovyx.voice.factory import VoiceBundle

        bundle = VoiceBundle(pipeline=pipeline, capture_task=capture)
        factory_mock = AsyncMock(return_value=bundle)

        bridge_process = AsyncMock()

        class _StubBridge:
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            async def process(self, req: object) -> None:  # noqa: ANN001
                await bridge_process(req)

        with (
            patch(
                "sovyx.voice.model_registry.check_voice_deps",
                return_value=(
                    [
                        {"module": "moonshine_voice", "package": "moonshine-voice"},
                        {"module": "sounddevice", "package": "sounddevice"},
                    ],
                    [],
                ),
            ),
            patch("sovyx.voice.model_registry.detect_tts_engine", return_value="piper"),
            patch.dict(sys.modules, {"sounddevice": fake_sd}),
            patch("sovyx.voice.factory.create_voice_pipeline", new=factory_mock),
            patch("sovyx.voice.cognitive_bridge.VoiceCognitiveBridge", _StubBridge),
        ):
            client = TestClient(application, headers={"Authorization": f"Bearer {_TOKEN}"})
            resp = client.post("/api/voice/enable")
            assert resp.status_code == 200  # noqa: PLR2004
            on_perception = factory_mock.call_args.kwargs["on_perception"]

            await on_perception("", "demo-mind")
            await on_perception("   ", "demo-mind")

        bridge_process.assert_not_awaited()


class TestDisableVoice:
    """POST /api/voice/disable."""

    def test_disable_without_mind_yaml(self, client: TestClient) -> None:
        resp = client.post("/api/voice/disable")
        assert resp.status_code == 503  # noqa: PLR2004

    def test_disable_with_mind_yaml(self, app, client: TestClient, tmp_path) -> None:
        mind_yaml = tmp_path / "mind.yaml"
        mind_yaml.write_text("voice:\n  enabled: true\n")
        app.state.mind_yaml_path = str(mind_yaml)

        resp = client.post("/api/voice/disable")
        assert resp.status_code == 200  # noqa: PLR2004
        assert resp.json()["ok"] is True

    def test_disable_stops_running_pipeline(self, app, client: TestClient, tmp_path) -> None:
        mind_yaml = tmp_path / "mind.yaml"
        mind_yaml.write_text("voice:\n  enabled: true\n")
        app.state.mind_yaml_path = str(mind_yaml)

        mock_pipeline = MagicMock()
        mock_pipeline.stop = AsyncMock()
        app.state.registry.is_registered.return_value = True
        app.state.registry.resolve = AsyncMock(return_value=mock_pipeline)

        resp = client.post("/api/voice/disable")
        assert resp.status_code == 200  # noqa: PLR2004
        mock_pipeline.stop.assert_awaited()

    def test_disable_stops_capture_and_deregisters(
        self, app, client: TestClient, tmp_path
    ) -> None:
        mind_yaml = tmp_path / "mind.yaml"
        mind_yaml.write_text("voice:\n  enabled: true\n")
        app.state.mind_yaml_path = str(mind_yaml)

        mock_capture = MagicMock()
        mock_capture.stop = AsyncMock()
        mock_pipeline = MagicMock()
        mock_pipeline.stop = AsyncMock()

        from sovyx.voice._capture_task import AudioCaptureTask
        from sovyx.voice.pipeline._orchestrator import VoicePipeline

        def resolve(cls):  # type: ignore[no-untyped-def]
            if cls is AudioCaptureTask:
                return mock_capture
            if cls is VoicePipeline:
                return mock_pipeline
            return MagicMock()

        app.state.registry.is_registered.return_value = True
        app.state.registry.resolve = AsyncMock(side_effect=resolve)

        resp = client.post("/api/voice/disable")
        assert resp.status_code == 200  # noqa: PLR2004
        mock_capture.stop.assert_awaited_once()
        mock_pipeline.stop.assert_awaited_once()
        deregistered = [call.args[0].__name__ for call in app.state.registry.deregister.mock_calls]
        assert "AudioCaptureTask" in deregistered
        assert "VoicePipeline" in deregistered
        # Sub-components deregistered too — next enable rebuilds them.
        assert "SileroVAD" in deregistered
        assert "STTEngine" in deregistered
        assert "TTSEngine" in deregistered
        assert "WakeWordDetector" in deregistered


class TestModelsDiskStatusEndpoint:
    """GET /api/voice/models/status — disk-truth, not static metadata."""

    def test_returns_all_missing_for_empty_tmpdir(self, client: TestClient, tmp_path) -> None:
        with patch(
            "sovyx.voice.model_status.get_default_model_dir",
            return_value=tmp_path,
        ):
            resp = client.get("/api/voice/models/status")

        assert resp.status_code == 200  # noqa: PLR2004
        data = resp.json()
        assert data["all_installed"] is False
        assert data["missing_count"] >= 1
        assert isinstance(data["models"], list)
        # Green check must NOT be reported for files that don't exist.
        for m in data["models"]:
            if m["name"].startswith("kokoro") or m["name"] == "silero-vad-v5":
                assert m["installed"] is False

    def test_reports_installed_kokoro_when_files_present(
        self, client: TestClient, tmp_path
    ) -> None:
        (tmp_path / "kokoro").mkdir()
        (tmp_path / "kokoro" / "kokoro-v1.0.int8.onnx").write_bytes(b"X" * 1024)
        (tmp_path / "kokoro" / "voices-v1.0.bin").write_bytes(b"X" * 512)

        with patch(
            "sovyx.voice.model_status.get_default_model_dir",
            return_value=tmp_path,
        ):
            resp = client.get("/api/voice/models/status")

        data = resp.json()
        kokoro = next(m for m in data["models"] if m["name"] == "kokoro-v1.0-int8")
        voices = next(m for m in data["models"] if m["name"] == "kokoro-voices-v1.0")
        assert kokoro["installed"] is True
        assert voices["installed"] is True


class TestModelsDownloadEndpoints:
    """POST/GET /api/voice/models/download — background task orchestration."""

    def test_start_and_poll_flow_when_all_installed(self, client: TestClient, tmp_path) -> None:
        # Pre-populate every expected file so the endpoint returns
        # ``status: "done"`` immediately without spawning a download.
        (tmp_path / "silero_vad.onnx").write_bytes(b"X")
        (tmp_path / "kokoro").mkdir()
        (tmp_path / "kokoro" / "kokoro-v1.0.int8.onnx").write_bytes(b"X")
        (tmp_path / "kokoro" / "voices-v1.0.bin").write_bytes(b"X")

        with patch(
            "sovyx.voice.model_status.get_default_model_dir",
            return_value=tmp_path,
        ):
            post = client.post("/api/voice/models/download")
            assert post.status_code == 200  # noqa: PLR2004
            started = post.json()
            assert started["status"] == "done"
            task_id = started["task_id"]

            poll = client.get(f"/api/voice/models/download/{task_id}")
            assert poll.status_code == 200  # noqa: PLR2004
            assert poll.json()["status"] == "done"

    def test_poll_unknown_task_returns_404(self, client: TestClient) -> None:
        resp = client.get("/api/voice/models/download/deadbeefdeadbeef")
        assert resp.status_code == 404  # noqa: PLR2004
        assert resp.json()["error"] == "task_not_found"

    def test_download_invokes_ensure_helpers(self, client: TestClient, tmp_path) -> None:
        """The POST endpoint drives ensure_silero_vad + ensure_kokoro_tts."""
        silero_called = {"count": 0}
        kokoro_called = {"count": 0}

        async def fake_silero(model_dir):
            silero_called["count"] += 1
            return model_dir / "silero_vad.onnx"

        async def fake_kokoro(model_dir):
            kokoro_called["count"] += 1
            return model_dir / "kokoro"

        with (
            patch(
                "sovyx.voice.model_status.get_default_model_dir",
                return_value=tmp_path,
            ),
            patch(
                "sovyx.voice.model_status.ensure_silero_vad",
                side_effect=fake_silero,
            ),
            patch(
                "sovyx.voice.model_status.ensure_kokoro_tts",
                side_effect=fake_kokoro,
            ),
        ):
            resp = client.post("/api/voice/models/download")
            assert resp.status_code == 200  # noqa: PLR2004
            task_id = resp.json()["task_id"]

            # Drain the background task — poll until non-running.
            import time

            deadline = time.time() + 5.0
            final: dict = {}
            while time.time() < deadline:
                poll = client.get(f"/api/voice/models/download/{task_id}")
                final = poll.json()
                if final.get("status") != "running":
                    break
                time.sleep(0.05)

            assert final.get("status") == "done"
            assert silero_called["count"] == 1
            assert kokoro_called["count"] == 1

    def test_download_requires_auth(self) -> None:
        app = create_app(token=_TOKEN)
        c = TestClient(app)
        resp = c.post("/api/voice/models/download")
        assert resp.status_code == 401  # noqa: PLR2004


class TestVoicesCatalogEndpoint:
    """GET /api/voice/voices — static Kokoro catalog surface for the wizard."""

    def test_returns_full_catalog_shape(self, client: TestClient) -> None:
        resp = client.get("/api/voice/voices")
        assert resp.status_code == 200  # noqa: PLR2004
        body = resp.json()

        assert set(body.keys()) == {
            "supported_languages",
            "by_language",
            "recommended_per_language",
        }

    def test_supported_languages_sorted_and_non_empty(
        self,
        client: TestClient,
    ) -> None:
        body = client.get("/api/voice/voices").json()
        langs = body["supported_languages"]
        assert isinstance(langs, list)
        assert langs == sorted(langs)
        # Kokoro v1.0 ships 9 language families.
        assert len(langs) == 9  # noqa: PLR2004
        assert "pt-br" in langs
        assert "en-us" in langs

    def test_by_language_covers_every_supported_language(
        self,
        client: TestClient,
    ) -> None:
        body = client.get("/api/voice/voices").json()
        by_language = body["by_language"]
        for lang in body["supported_languages"]:
            assert lang in by_language
            assert by_language[lang], f"{lang} has no voices"
            for entry in by_language[lang]:
                assert entry["language"] == lang
                assert entry["id"]
                assert entry["display_name"]
                assert entry["gender"] in {"female", "male"}

    def test_recommended_per_language_points_into_catalog(
        self,
        client: TestClient,
    ) -> None:
        body = client.get("/api/voice/voices").json()
        recs = body["recommended_per_language"]
        by_language = body["by_language"]
        for lang, voice_id in recs.items():
            assert lang in by_language
            ids = {v["id"] for v in by_language[lang]}
            assert voice_id in ids

    def test_requires_auth(self) -> None:
        app = create_app(token=_TOKEN)
        c = TestClient(app)
        resp = c.get("/api/voice/voices")
        assert resp.status_code == 401  # noqa: PLR2004


class TestCaptureDiagnostics:
    """GET /api/voice/capture-diagnostics — Voice Clarity APO surface."""

    def test_empty_on_non_windows(self, client: TestClient) -> None:
        """Non-Windows hosts return an empty endpoint list, no error."""
        with patch(
            "sovyx.voice._apo_detector.detect_capture_apos",
            return_value=[],
        ):
            resp = client.get("/api/voice/capture-diagnostics")
        assert resp.status_code == 200  # noqa: PLR2004
        data = resp.json()
        assert data["endpoints"] == []
        assert data["voice_clarity_active"] is False
        assert data["any_voice_clarity_active"] is False
        assert data["active_endpoint"] is None
        assert data["fix_suggestion"] is None

    def test_reports_voice_clarity_active(self, client: TestClient) -> None:
        """Clarity-on-active-device flips the top-level flag + suggestion."""
        from sovyx.voice._apo_detector import CaptureApoReport

        rep_active = CaptureApoReport(
            endpoint_id="{active}",
            endpoint_name="Microfone (Razer BlackShark V2 Pro)",
            enumerator="USB",
            fx_binding_count=4,
            known_apos=["Windows Voice Clarity"],
            raw_clsids=["{CF1DDA2C-3B93-4EFE-8AA9-DEB6F8D4FDF1}"],
            voice_clarity_active=True,
        )
        rep_inactive = CaptureApoReport(
            endpoint_id="{other}",
            endpoint_name="Built-in Microphone",
            enumerator="MMDevAPI",
            fx_binding_count=1,
            known_apos=[],
            raw_clsids=[],
            voice_clarity_active=False,
        )
        with patch(
            "sovyx.voice._apo_detector.detect_capture_apos",
            return_value=[rep_active, rep_inactive],
        ):
            resp = client.get("/api/voice/capture-diagnostics")
        assert resp.status_code == 200  # noqa: PLR2004
        data = resp.json()
        assert data["any_voice_clarity_active"] is True
        assert len(data["endpoints"]) == 2  # noqa: PLR2004
        # No running capture task in this fixture → no device resolution.
        assert data["active_endpoint"] is None
        assert data["voice_clarity_active"] is False

    def test_active_endpoint_matched_via_capture_task(self, app) -> None:
        """When an AudioCaptureTask is registered, its device name drives matching."""
        from sovyx.voice._apo_detector import CaptureApoReport
        from sovyx.voice._capture_task import AudioCaptureTask

        rep = CaptureApoReport(
            endpoint_id="{active}",
            endpoint_name="Microfone (Razer BlackShark V2 Pro)",
            enumerator="USB",
            fx_binding_count=4,
            known_apos=["Windows Voice Clarity"],
            raw_clsids=[],
            voice_clarity_active=True,
        )
        fake_capture = MagicMock()
        fake_capture.input_device_name = "Microfone (Razer BlackShark V2 Pro)"

        registry = app.state.registry
        registry.is_registered = MagicMock(
            side_effect=lambda iface: iface is AudioCaptureTask,
        )
        registry.resolve = AsyncMock(return_value=fake_capture)

        c = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})
        with patch(
            "sovyx.voice._apo_detector.detect_capture_apos",
            return_value=[rep],
        ):
            resp = c.get("/api/voice/capture-diagnostics")
        assert resp.status_code == 200  # noqa: PLR2004
        data = resp.json()
        assert data["active_device_name"] == "Microfone (Razer BlackShark V2 Pro)"
        assert data["voice_clarity_active"] is True
        assert data["active_endpoint"] is not None
        assert data["active_endpoint"]["endpoint_id"] == "{active}"
        assert data["fix_suggestion"] is not None
        assert data["endpoints"][0]["is_active_device"] is True

    def test_detector_exception_returns_safe_payload(self, client: TestClient) -> None:
        """A detector crash must not 500 — dashboard shows an empty card."""
        with patch(
            "sovyx.voice._apo_detector.detect_capture_apos",
            side_effect=RuntimeError("registry unavailable"),
        ):
            resp = client.get("/api/voice/capture-diagnostics")
        assert resp.status_code == 200  # noqa: PLR2004
        data = resp.json()
        assert data["endpoints"] == []
        assert data["voice_clarity_active"] is False
        assert "registry unavailable" in data["error"]

    def test_requires_auth(self) -> None:
        app = create_app(token=_TOKEN)
        c = TestClient(app)
        resp = c.get("/api/voice/capture-diagnostics")
        assert resp.status_code == 401  # noqa: PLR2004


class TestCaptureExclusivePost:
    """POST /api/voice/capture-exclusive — persist + hot-apply."""

    def test_persists_to_system_yaml(self, app, tmp_path) -> None:
        """Sets the value on the in-memory config and writes system.yaml."""
        from unittest.mock import AsyncMock as _AsyncMock

        config_path = tmp_path / "system.yaml"
        config_path.write_text("")
        app.state.config_path = str(config_path)

        mock_engine_config = MagicMock()
        mock_engine_config.tuning.voice.capture_wasapi_exclusive = False
        app.state.engine_config = mock_engine_config

        mock_editor = MagicMock()
        mock_editor.update_section = _AsyncMock()

        c = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})
        with patch(
            "sovyx.engine.config_editor.ConfigEditor",
            return_value=mock_editor,
        ):
            resp = c.post(
                "/api/voice/capture-exclusive",
                json={"enabled": True},
            )
        assert resp.status_code == 200  # noqa: PLR2004
        data = resp.json()
        assert data["ok"] is True
        assert data["enabled"] is True
        assert data["persisted"] is True
        assert data["applied_immediately"] is False

        mock_editor.update_section.assert_awaited_once()
        args = mock_editor.update_section.await_args
        assert args.args[1] == "tuning.voice"
        assert args.args[2] == {"capture_wasapi_exclusive": True}
        assert mock_engine_config.tuning.voice.capture_wasapi_exclusive is True

    def test_triggers_exclusive_restart_when_pipeline_running(self, app, tmp_path) -> None:
        """When a capture task is registered, enable=True hot-applies it."""
        from sovyx.voice._capture_task import (
            AudioCaptureTask,
            ExclusiveRestartResult,
            ExclusiveRestartVerdict,
        )

        config_path = tmp_path / "system.yaml"
        config_path.write_text("")
        app.state.config_path = str(config_path)
        app.state.engine_config = MagicMock()

        fake_capture = MagicMock()
        fake_capture.request_exclusive_restart = AsyncMock(
            return_value=ExclusiveRestartResult(
                verdict=ExclusiveRestartVerdict.EXCLUSIVE_ENGAGED,
                engaged=True,
                host_api="Windows WASAPI",
                device=5,
                sample_rate=16_000,
            ),
        )

        registry = app.state.registry
        registry.is_registered = MagicMock(
            side_effect=lambda iface: iface is AudioCaptureTask,
        )
        registry.resolve = AsyncMock(return_value=fake_capture)

        mock_editor = MagicMock()
        mock_editor.update_section = AsyncMock()

        c = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})
        with patch(
            "sovyx.engine.config_editor.ConfigEditor",
            return_value=mock_editor,
        ):
            resp = c.post(
                "/api/voice/capture-exclusive",
                json={"enabled": True},
            )
        assert resp.status_code == 200  # noqa: PLR2004
        data = resp.json()
        assert data["applied_immediately"] is True
        assert data["verdict"] == "exclusive_engaged"
        fake_capture.request_exclusive_restart.assert_awaited_once()

    def test_downgrade_to_shared_reports_not_applied(self, app, tmp_path) -> None:
        """v0.20.2 / Bug C — WASAPI shared downgrade must surface as not applied.

        If another app holds the device exclusively (or policy denies
        exclusive access), WASAPI hands back a shared-mode stream with
        ``exclusive_used=False``. Pre-v0.20.2 the endpoint reported
        ``applied_immediately=True`` anyway — the UI showed a fake
        success banner while the APO chain was still in the signal
        path. The route now returns ``applied_immediately=False`` +
        ``verdict="downgraded_to_shared"`` + a detail string.
        """
        from sovyx.voice._capture_task import (
            AudioCaptureTask,
            ExclusiveRestartResult,
            ExclusiveRestartVerdict,
        )

        config_path = tmp_path / "system.yaml"
        config_path.write_text("")
        app.state.config_path = str(config_path)
        app.state.engine_config = MagicMock()

        fake_capture = MagicMock()
        fake_capture.request_exclusive_restart = AsyncMock(
            return_value=ExclusiveRestartResult(
                verdict=ExclusiveRestartVerdict.DOWNGRADED_TO_SHARED,
                engaged=False,
                host_api="Windows WASAPI",
                device=5,
                sample_rate=16_000,
                detail="WASAPI granted shared mode instead of exclusive",
            ),
        )

        registry = app.state.registry
        registry.is_registered = MagicMock(
            side_effect=lambda iface: iface is AudioCaptureTask,
        )
        registry.resolve = AsyncMock(return_value=fake_capture)

        mock_editor = MagicMock()
        mock_editor.update_section = AsyncMock()

        c = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})
        with patch(
            "sovyx.engine.config_editor.ConfigEditor",
            return_value=mock_editor,
        ):
            resp = c.post(
                "/api/voice/capture-exclusive",
                json={"enabled": True},
            )
        assert resp.status_code == 200  # noqa: PLR2004
        data = resp.json()
        assert data["ok"] is True
        assert data["enabled"] is True
        # The reopen ran and landed in shared mode — the APO chain is
        # still live, so the UI must NOT tell the user the bypass took.
        assert data["applied_immediately"] is False
        assert data["verdict"] == "downgraded_to_shared"
        assert "shared mode" in data["detail"]

    def test_disable_does_not_call_exclusive_restart(self, app, tmp_path) -> None:
        """enabled=False persists but never calls exclusive restart."""
        from sovyx.voice._capture_task import AudioCaptureTask

        config_path = tmp_path / "system.yaml"
        config_path.write_text("")
        app.state.config_path = str(config_path)
        app.state.engine_config = MagicMock()

        fake_capture = MagicMock()
        fake_capture.request_exclusive_restart = AsyncMock()

        registry = app.state.registry
        registry.is_registered = MagicMock(
            side_effect=lambda iface: iface is AudioCaptureTask,
        )
        registry.resolve = AsyncMock(return_value=fake_capture)

        mock_editor = MagicMock()
        mock_editor.update_section = AsyncMock()

        c = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})
        with patch(
            "sovyx.engine.config_editor.ConfigEditor",
            return_value=mock_editor,
        ):
            resp = c.post(
                "/api/voice/capture-exclusive",
                json={"enabled": False},
            )
        assert resp.status_code == 200  # noqa: PLR2004
        data = resp.json()
        assert data["enabled"] is False
        assert data["applied_immediately"] is False
        fake_capture.request_exclusive_restart.assert_not_called()

    def test_ok_even_without_config_path(self, app) -> None:
        """Missing config_path is a soft-fail — persisted=false, still ok."""
        app.state.config_path = None
        app.state.engine_config = None

        c = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})
        resp = c.post("/api/voice/capture-exclusive", json={"enabled": True})
        assert resp.status_code == 200  # noqa: PLR2004
        data = resp.json()
        assert data["ok"] is True
        assert data["persisted"] is False

    def test_requires_auth(self) -> None:
        app = create_app(token=_TOKEN)
        c = TestClient(app)
        resp = c.post("/api/voice/capture-exclusive", json={"enabled": True})
        assert resp.status_code == 401  # noqa: PLR2004
