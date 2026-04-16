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
        mock_pipeline.stop.assert_awaited_once()
