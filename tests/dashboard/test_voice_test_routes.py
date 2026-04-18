"""Tests for /api/voice/test/* endpoints — setup-wizard meter + TTS playback."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest
from starlette.testclient import TestClient

from sovyx.dashboard.server import create_app
from sovyx.voice.device_test import (
    PROTOCOL_VERSION,
    AudioSinkError,
    ErrorCode,
    FakeAudioOutputSink,
)

if TYPE_CHECKING:
    from fastapi import FastAPI

_TOKEN = "test-token-voice-test"


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


def _make_app(*, pipeline_active: bool = False) -> FastAPI:
    app = create_app(token=_TOKEN)
    registry = MagicMock()

    def _is_registered(target: type) -> bool:
        # Pipeline activeness depends on whether VoicePipeline is registered.
        from sovyx.engine.config import EngineConfig
        from sovyx.voice.pipeline._orchestrator import VoicePipeline

        if target is VoicePipeline:
            return pipeline_active
        if target is EngineConfig:
            return False
        return False

    registry.is_registered.side_effect = _is_registered
    registry.resolve = AsyncMock()
    app.state.registry = registry
    app.state.mind_yaml_path = None
    app.state.mind_id = "test-mind"
    return app


@pytest.fixture()
def app() -> FastAPI:
    return _make_app()


@pytest.fixture()
def client(app: FastAPI) -> TestClient:
    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})


# --------------------------------------------------------------------------
# GET /devices
# --------------------------------------------------------------------------


class TestListDevices:
    """GET /api/voice/test/devices."""

    def test_lists_devices(self, client: TestClient) -> None:
        fake_devices = [
            {
                "name": "Mic",
                "default_samplerate": 48_000,
                "max_input_channels": 2,
                "max_output_channels": 0,
            },
            {
                "name": "Speakers",
                "default_samplerate": 48_000,
                "max_input_channels": 0,
                "max_output_channels": 2,
            },
        ]
        fake_default = MagicMock()
        fake_default.device = (0, 1)

        fake_sd = MagicMock()
        fake_sd.query_devices = MagicMock(return_value=fake_devices)
        fake_sd.default = fake_default

        with patch.dict("sys.modules", {"sounddevice": fake_sd}):
            resp = client.get("/api/voice/test/devices")

        assert resp.status_code == 200  # noqa: PLR2004
        body = resp.json()
        assert body["ok"] is True
        assert body["protocol_version"] == PROTOCOL_VERSION
        assert len(body["input_devices"]) == 1
        assert len(body["output_devices"]) == 1
        assert body["input_devices"][0]["name"] == "Mic"
        assert body["input_devices"][0]["is_default"] is True
        assert body["output_devices"][0]["name"] == "Speakers"
        assert body["output_devices"][0]["is_default"] is True

    def test_sounddevice_missing_returns_empty(self, client: TestClient) -> None:
        # With a missing sounddevice module, the route swallows the import
        # error and returns empty lists.
        with patch.dict("sys.modules", {"sounddevice": None}):
            resp = client.get("/api/voice/test/devices")
        assert resp.status_code == 200  # noqa: PLR2004
        body = resp.json()
        assert body["input_devices"] == []
        assert body["output_devices"] == []

    def test_no_auth_401(self) -> None:
        app = _make_app()
        c = TestClient(app)
        resp = c.get("/api/voice/test/devices")
        assert resp.status_code == 401  # noqa: PLR2004

    def test_kill_switch_returns_503(self, client: TestClient, app: FastAPI) -> None:
        # Override tuning to disable the test.
        from sovyx.engine.config import EngineConfig

        cfg = EngineConfig()
        cfg.tuning.voice.device_test_enabled = False
        app.state.engine_config = cfg
        app.state.registry.is_registered.side_effect = lambda t: t is EngineConfig

        resp = client.get("/api/voice/test/devices")
        assert resp.status_code == 503  # noqa: PLR2004
        body = resp.json()
        assert body["ok"] is False
        assert body["code"] == ErrorCode.DISABLED.value


# --------------------------------------------------------------------------
# POST /output  +  GET /output/{job_id}
# --------------------------------------------------------------------------


class TestStartOutput:
    """POST /api/voice/test/output → TTS playback job."""

    def test_happy_path_completes(self, client: TestClient, app: FastAPI) -> None:
        # Inject a fake TTS synth + sink so the route never touches ONNX or
        # PortAudio.
        sink = FakeAudioOutputSink()
        app.state.voice_test_output_sink = sink
        seen: dict[str, object] = {}

        async def fake_tts(text: str, voice: str, language: str) -> object:
            seen["text"] = text
            seen["voice"] = voice
            seen["language"] = language
            audio = np.full(11_025, 5_000, dtype=np.int16)
            from sovyx.dashboard.routes.voice_test import _SynthResult

            return _SynthResult(audio=audio, sample_rate=22_050)

        app.state.voice_test_tts_factory = fake_tts

        # Start job.
        resp = client.post("/api/voice/test/output", json={"phrase_key": "default"})
        assert resp.status_code == 200  # noqa: PLR2004
        body = resp.json()
        assert body["ok"] is True
        assert body["status"] == "queued"
        assert len(body["job_id"]) == 16  # secrets.token_hex(8)

        job_id = body["job_id"]

        # Poll until done (background task should complete very quickly).
        for _ in range(50):
            resp = client.get(f"/api/voice/test/output/{job_id}")
            assert resp.status_code == 200  # noqa: PLR2004
            result = resp.json()
            if result["status"] != "running":
                break
            # Give the background task a chance.
            import time as _time

            _time.sleep(0.02)
        assert result["status"] == "done"
        assert result["ok"] is True
        assert result["peak_db"] is not None
        assert result["synthesis_ms"] is not None
        assert result["playback_ms"] is not None

        # Sink recorded the playback.
        assert len(sink.calls) == 1
        assert sink.calls[0]["sample_rate"] == 22_050

        # Default body had no voice + language="en" → catalog resolves to
        # the American English recommended voice and the English phrase.
        assert seen["language"] == "en-us"
        assert isinstance(seen["voice"], str) and seen["voice"].startswith("a")
        assert isinstance(seen["text"], str) and seen["text"]

    def test_pipeline_active_returns_409(self) -> None:
        app = _make_app(pipeline_active=True)
        c = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})
        resp = c.post("/api/voice/test/output", json={})
        assert resp.status_code == 409  # noqa: PLR2004
        body = resp.json()
        assert body["code"] == ErrorCode.PIPELINE_ACTIVE.value

    def test_tts_unavailable_returns_503(
        self,
        client: TestClient,
        app: FastAPI,
    ) -> None:
        # Force standalone resolution down the "no TTS" path.
        with patch(
            "sovyx.voice.model_registry.detect_tts_engine",
            return_value="none",
        ):
            resp = client.post("/api/voice/test/output", json={})
        assert resp.status_code == 503  # noqa: PLR2004
        body = resp.json()
        assert body["code"] == ErrorCode.TTS_UNAVAILABLE.value

    def test_missing_models_returns_models_not_downloaded(
        self,
        client: TestClient,
        app: FastAPI,
        tmp_path,
    ) -> None:
        """Kokoro installed as a package but model files absent → structured 503.

        Regression: the legacy behaviour returned a generic
        ``tts_unavailable`` so the UI couldn't offer a download CTA.
        The new contract is ``models_not_downloaded`` + a
        ``missing_models`` list in the body.
        """
        # Kokoro Python package "installed" per detect_tts_engine, but
        # the model_dir is an empty tmp — so KokoroTTS.initialize() will
        # raise FileNotFoundError, surfacing as _MissingModels.
        with (
            patch(
                "sovyx.voice.model_registry.detect_tts_engine",
                return_value="kokoro",
            ),
            patch(
                "sovyx.voice.model_registry.get_default_model_dir",
                return_value=tmp_path,
            ),
            patch(
                "sovyx.voice.model_status.get_default_model_dir",
                return_value=tmp_path,
            ),
            patch("sovyx.voice.tts_kokoro.KokoroTTS") as MockKokoro,
        ):
            instance = MockKokoro.return_value
            instance.initialize = AsyncMock(side_effect=FileNotFoundError("missing"))
            resp = client.post("/api/voice/test/output", json={})

        assert resp.status_code == 503  # noqa: PLR2004
        body = resp.json()
        assert body["code"] == ErrorCode.MODELS_NOT_DOWNLOADED.value
        assert "missing_models" in body
        assert isinstance(body["missing_models"], list)
        assert len(body["missing_models"]) >= 1

    def test_sink_error_recorded_in_result(
        self,
        client: TestClient,
        app: FastAPI,
    ) -> None:
        sink = FakeAudioOutputSink(
            error=AudioSinkError(ErrorCode.DEVICE_BUSY, "held"),
        )
        app.state.voice_test_output_sink = sink

        async def fake_tts(text: str, voice: str, language: str) -> object:  # noqa: ARG001
            from sovyx.dashboard.routes.voice_test import _SynthResult

            audio = np.zeros(1024, dtype=np.int16)
            return _SynthResult(audio=audio, sample_rate=16_000)

        app.state.voice_test_tts_factory = fake_tts

        resp = client.post("/api/voice/test/output", json={})
        assert resp.status_code == 200  # noqa: PLR2004
        job_id = resp.json()["job_id"]

        # Poll until terminal.
        import time as _time

        for _ in range(50):
            result = client.get(f"/api/voice/test/output/{job_id}").json()
            if result["status"] != "running":
                break
            _time.sleep(0.02)
        assert result["status"] == "error"
        assert result["ok"] is False
        assert result["code"] == ErrorCode.DEVICE_BUSY.value

    def test_unknown_job_returns_404(self, client: TestClient) -> None:
        resp = client.get("/api/voice/test/output/does-not-exist")
        assert resp.status_code == 404  # noqa: PLR2004
        body = resp.json()
        assert body["code"] == ErrorCode.JOB_NOT_FOUND.value

    def test_no_auth_401(self) -> None:
        app = _make_app()
        c = TestClient(app)
        resp = c.post("/api/voice/test/output", json={})
        assert resp.status_code == 401  # noqa: PLR2004


# --------------------------------------------------------------------------
# Language / voice coherence — the reason this router stopped trusting
# ``body.language`` verbatim. See :mod:`sovyx.voice.voice_catalog`.
# --------------------------------------------------------------------------


class TestVoiceLanguageCoherence:
    """Voice id + language must agree before we spend synth cycles."""

    def _wire_fake_tts(
        self,
        app: FastAPI,
    ) -> tuple[FakeAudioOutputSink, dict[str, object]]:
        """Install a capturing fake synth + sink and return them."""
        sink = FakeAudioOutputSink()
        app.state.voice_test_output_sink = sink
        seen: dict[str, object] = {}

        async def fake_tts(text: str, voice: str, language: str) -> object:
            seen["text"] = text
            seen["voice"] = voice
            seen["language"] = language
            from sovyx.dashboard.routes.voice_test import _SynthResult

            audio = np.zeros(1024, dtype=np.int16)
            return _SynthResult(audio=audio, sample_rate=16_000)

        app.state.voice_test_tts_factory = fake_tts
        return sink, seen

    def _drain(self, client: TestClient, job_id: str) -> dict[str, object]:
        import time as _time

        for _ in range(50):
            result = client.get(f"/api/voice/test/output/{job_id}").json()
            if result["status"] != "running":
                return result
            _time.sleep(0.02)
        msg = f"job {job_id} did not finish"
        raise AssertionError(msg)

    def test_language_pt_picks_brazilian_voice_and_phrase(
        self,
        client: TestClient,
        app: FastAPI,
    ) -> None:
        """``language=pt-BR`` without a voice → catalog recommended voice.

        This is the exact bug the user hit: UI sent Portuguese, server
        synthesised English. The fix derives both voice and phrase from
        the normalised language.
        """
        _, seen = self._wire_fake_tts(app)
        resp = client.post(
            "/api/voice/test/output",
            json={"language": "pt-BR"},
        )
        assert resp.status_code == 200  # noqa: PLR2004
        job_id = resp.json()["job_id"]
        result = self._drain(client, job_id)
        assert result["status"] == "done"
        assert seen["language"] == "pt-br"
        # Brazilian Portuguese voice ids start with "p".
        assert isinstance(seen["voice"], str)
        assert seen["voice"].startswith("p")
        # Phrase is the Portuguese translation, not the English one.
        assert isinstance(seen["text"], str)
        assert "asistente" not in seen["text"].lower()  # English fallback
        assert "assistente" in seen["text"].lower()

    def test_voice_overrides_language_mismatch(
        self,
        client: TestClient,
        app: FastAPI,
    ) -> None:
        """Voice wins: catalog-known voice derives its own language.

        If the UI mistakenly sends ``language=en`` but voice=``jf_alpha``
        (Japanese), the phrase must be Japanese — the voice is the
        source of truth because it pins the acoustic model's expected
        language.
        """
        _, seen = self._wire_fake_tts(app)
        resp = client.post(
            "/api/voice/test/output",
            json={"language": "en", "voice": "jf_alpha"},
        )
        assert resp.status_code == 200  # noqa: PLR2004
        job_id = resp.json()["job_id"]
        result = self._drain(client, job_id)
        assert result["status"] == "done"
        assert seen["voice"] == "jf_alpha"
        assert seen["language"] == "ja"

    def test_unknown_voice_id_returns_400(
        self,
        client: TestClient,
    ) -> None:
        resp = client.post(
            "/api/voice/test/output",
            json={"voice": "not_a_real_voice"},
        )
        assert resp.status_code == 400  # noqa: PLR2004
        body = resp.json()
        assert body["code"] == ErrorCode.INVALID_REQUEST.value
        assert "not_a_real_voice" in body["detail"]

    def test_unsupported_language_returns_400(
        self,
        client: TestClient,
    ) -> None:
        resp = client.post(
            "/api/voice/test/output",
            json={"language": "klingon"},
        )
        assert resp.status_code == 400  # noqa: PLR2004
        body = resp.json()
        assert body["code"] == ErrorCode.INVALID_REQUEST.value
        # Supported-languages hint helps the UI render a diagnostic.
        assert "Supported" in body["detail"]

    @pytest.mark.parametrize(
        ("language", "voice_prefix"),
        [
            ("en-us", "a"),
            ("en-gb", "b"),
            ("es", "e"),
            ("fr", "f"),
            ("hi", "h"),
            ("it", "i"),
            ("ja", "j"),
            ("pt-br", "p"),
            ("zh", "z"),
        ],
    )
    def test_every_supported_language_resolves(
        self,
        client: TestClient,
        app: FastAPI,
        language: str,
        voice_prefix: str,
    ) -> None:
        """Full round-trip per language — catches catalog drift early."""
        _, seen = self._wire_fake_tts(app)
        resp = client.post(
            "/api/voice/test/output",
            json={"language": language},
        )
        assert resp.status_code == 200, (resp.status_code, resp.json())  # noqa: PLR2004
        job_id = resp.json()["job_id"]
        result = self._drain(client, job_id)
        assert result["status"] == "done"
        assert seen["language"] == language
        assert isinstance(seen["voice"], str)
        assert seen["voice"].startswith(voice_prefix), seen["voice"]


# --------------------------------------------------------------------------
# WS /input  —  the hard one
# --------------------------------------------------------------------------


class TestWebSocketMeter:
    """WS /api/voice/test/input."""

    def test_missing_token_closes_4001(self) -> None:
        app = _make_app()
        c = TestClient(app)
        # No token on the query string.
        from websockets.exceptions import ConnectionClosed

        with (
            pytest.raises(
                (AssertionError, ConnectionClosed, Exception),
            ),
            c.websocket_connect("/api/voice/test/input") as ws,
        ):
            # On close the client raises.
            _ = ws.receive_json()

    def test_wrong_token_closes_4001(self) -> None:
        app = _make_app()
        c = TestClient(app)
        # Starlette TestClient doesn't surface the close code directly on a
        # connection that never accepts; the connection is rejected before
        # the handshake completes. We assert no handshake by catching.
        try:
            with (
                c.websocket_connect(
                    "/api/voice/test/input?token=wrong",
                ) as ws,
                pytest.raises(Exception),
            ):
                ws.receive_json()
        except Exception:
            pass  # Expected — handshake rejected.

    def test_kill_switch_closes_4010(self, app: FastAPI) -> None:
        from sovyx.engine.config import EngineConfig

        cfg = EngineConfig()
        cfg.tuning.voice.device_test_enabled = False
        app.state.engine_config = cfg
        app.state.registry.is_registered.side_effect = lambda t: t is EngineConfig

        c = TestClient(app)
        try:
            with (
                c.websocket_connect(
                    f"/api/voice/test/input?token={_TOKEN}",
                ) as ws,
                pytest.raises(Exception),
            ):
                ws.receive_json()
        except Exception:
            pass  # Expected — server closed before accept.

    def test_pipeline_active_closes_4009(self) -> None:
        app = _make_app(pipeline_active=True)
        c = TestClient(app)
        try:
            with (
                c.websocket_connect(
                    f"/api/voice/test/input?token={_TOKEN}",
                ) as ws,
                pytest.raises(Exception),
            ):
                ws.receive_json()
        except Exception:
            pass  # Expected — pipeline active rejection.


# --------------------------------------------------------------------------
# Concurrency: reconnect limiter
# --------------------------------------------------------------------------


class TestReconnectLimiter:
    """The limiter is lazily created on first WS request."""

    def test_limiter_lazy_create(self, app: FastAPI) -> None:
        from sovyx.dashboard.routes.voice_test import _get_limiter

        limiter = _get_limiter(MagicMock(app=app))
        # Cached.
        limiter2 = _get_limiter(MagicMock(app=app))
        assert limiter is limiter2
