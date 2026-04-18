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
