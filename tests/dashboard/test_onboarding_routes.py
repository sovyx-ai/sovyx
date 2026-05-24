"""Tests for /api/onboarding/* endpoints — first-run wizard API."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.testclient import TestClient

from sovyx.dashboard.server import create_app

_TOKEN = "test-token-onboarding"


@pytest.fixture()
def app():
    application = create_app(token=_TOKEN)
    registry = MagicMock()
    registry.is_registered.return_value = False
    registry.resolve = AsyncMock()
    application.state.registry = registry
    application.state.mind_yaml_path = None
    application.state.mind_id = "test-mind"

    mind_config = MagicMock()
    mind_config.configure_mock(name="test-mind")
    mind_config.id = "test-mind"
    mind_config.onboarding_complete = False
    mind_config.llm.default_provider = ""
    mind_config.llm.default_model = ""
    mind_config.llm.fast_model = ""
    mind_config.language = "en"
    application.state.mind_config = mind_config

    return application


@pytest.fixture()
def client(app) -> TestClient:
    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})


class TestGetOnboardingState:
    """GET /api/onboarding/state."""

    def test_returns_state(self, client: TestClient) -> None:
        resp = client.get("/api/onboarding/state")
        assert resp.status_code == 200  # noqa: PLR2004
        data = resp.json()
        assert data["complete"] is False
        assert "provider_configured" in data
        assert "ollama_available" in data
        assert "mind_name" in data
        # v0.31.6 C1: response carries the resolved active mind id
        # so the frontend can pass it to <VoiceCalibrationStep /> instead
        # of the literal "default" sentinel (anti-pattern #35 reincidente).
        assert "mind_id" in data

    def test_returns_mind_id_from_mind_config(self, app, client: TestClient) -> None:
        """v0.31.6 C1: ``mind_id`` mirrors ``mind_config.id`` exactly."""
        app.state.mind_config.id = "meu-mind"
        resp = client.get("/api/onboarding/state")
        assert resp.status_code == 200  # noqa: PLR2004
        data = resp.json()
        assert data["mind_id"] == "meu-mind"

    def test_returns_mind_id_none_when_no_mind_config(self, client: TestClient) -> None:
        """v0.31.6 C1: when no mind_config is mounted (very early boot
        / registry malfunction), ``mind_id`` is ``None`` so the
        frontend falls back to the ``"default"`` literal with a
        console warning instead of misattributing calibration to a
        non-existent mind."""
        application = create_app(token=_TOKEN)
        # No mind_config attached to app.state; getattr returns None.
        c = TestClient(application, headers={"Authorization": f"Bearer {_TOKEN}"})
        resp = c.get("/api/onboarding/state")
        assert resp.status_code == 200  # noqa: PLR2004
        data = resp.json()
        assert data["mind_id"] is None

    def test_complete_true_when_set(self, app, client: TestClient) -> None:
        app.state.mind_config.onboarding_complete = True
        resp = client.get("/api/onboarding/state")
        assert resp.json()["complete"] is True

    def test_no_auth_401(self) -> None:
        application = create_app(token=_TOKEN)
        c = TestClient(application)
        resp = c.get("/api/onboarding/state")
        assert resp.status_code == 401  # noqa: PLR2004


class TestConfigureProvider:
    """POST /api/onboarding/provider."""

    def test_missing_provider_422(self, client: TestClient) -> None:
        resp = client.post("/api/onboarding/provider", json={})
        assert resp.status_code == 422  # noqa: PLR2004

    def test_unknown_provider_422(self, client: TestClient, app) -> None:
        router_mock = MagicMock()
        router_mock._providers = []
        app.state.registry.is_registered.return_value = True
        app.state.registry.resolve = AsyncMock(return_value=router_mock)
        resp = client.post(
            "/api/onboarding/provider",
            json={"provider": "unknown_provider", "api_key": "test-key"},
        )
        assert resp.status_code == 422  # noqa: PLR2004

    def test_cloud_provider_missing_key_422(self, client: TestClient, app) -> None:
        router_mock = MagicMock()
        router_mock._providers = []
        app.state.registry.is_registered.return_value = True
        app.state.registry.resolve = AsyncMock(return_value=router_mock)
        resp = client.post(
            "/api/onboarding/provider",
            json={"provider": "anthropic"},
        )
        assert resp.status_code == 422  # noqa: PLR2004
        assert "api_key" in resp.json()["error"].lower()

    def test_ollama_not_reachable_422(self, client: TestClient, app) -> None:
        from sovyx.llm.providers.ollama import OllamaProvider

        mock_ollama = MagicMock(spec=OllamaProvider)
        mock_ollama.name = "ollama"
        mock_ollama.ping = AsyncMock(return_value=False)

        router_mock = MagicMock()
        router_mock._providers = [mock_ollama]
        app.state.registry.is_registered.return_value = True
        app.state.registry.resolve = AsyncMock(return_value=router_mock)

        resp = client.post(
            "/api/onboarding/provider",
            json={"provider": "ollama"},
        )
        assert resp.status_code == 422  # noqa: PLR2004
        assert "not reachable" in resp.json()["error"].lower()

    def test_ollama_success(self, client: TestClient, app) -> None:
        from sovyx.llm.providers.ollama import OllamaProvider

        mock_ollama = MagicMock(spec=OllamaProvider)
        mock_ollama.name = "ollama"
        mock_ollama.ping = AsyncMock(return_value=True)
        mock_ollama.list_models = AsyncMock(return_value=["llama3.1:latest"])
        mock_ollama.is_available = True

        router_mock = MagicMock()
        router_mock._providers = [mock_ollama]
        app.state.registry.is_registered.return_value = True
        app.state.registry.resolve = AsyncMock(return_value=router_mock)

        resp = client.post(
            "/api/onboarding/provider",
            json={"provider": "ollama", "model": "llama3.1:latest"},
        )
        assert resp.status_code == 200  # noqa: PLR2004
        data = resp.json()
        assert data["ok"] is True
        assert data["provider"] == "ollama"
        assert data["model"] == "llama3.1:latest"


class TestProviderConfigClearsDegradedAxis:
    """LIVE-1 Bug A regression — boot-no-provider → onboarding configure →
    ``axis="llm"`` clears in the SAME request via the synchronous clear-edge.

    Before the fix the boot-time ``no_provider_configured`` composite-store
    entry survived a successful provider hot-register (the dashboard kept
    reporting "no provider configured" for the process lifetime). This test
    drives the real route + a real ``LLMLivenessProbe.refresh_now`` and asserts
    the entry is gone.
    """

    def test_cloud_provider_config_clears_llm_axis(
        self, client: TestClient, app, tmp_path, monkeypatch
    ) -> None:
        import os

        import sovyx.dashboard.routes.onboarding as onboarding_mod
        from sovyx.engine._degraded_store import (
            get_default_degraded_store,
            reset_default_degraded_store,
        )
        from sovyx.engine._llm_dispatch import dispatch_llm_discovery_verdict
        from sovyx.engine._llm_liveness_probe import LLMLivenessProbe
        from sovyx.engine.config import LLMTuningConfig
        from sovyx.llm._provider_health import (
            DiscoveryVerdict,
            scan_llm_provider_health,
        )
        from sovyx.llm.router import LLMRouter

        # Persist API key into a temp data_dir (never the real home).
        app.state.data_dir = str(tmp_path)

        cloud_keys = (
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "GOOGLE_API_KEY",
            "XGROK_API_KEY",
            "DEEPSEEK_API_KEY",
            "MISTRAL_API_KEY",
            "GROQ_API_KEY",
            "TOGETHER_API_KEY",
            "FIREWORKS_API_KEY",
        )
        for var in cloud_keys:
            monkeypatch.delenv(var, raising=False)

        reset_default_degraded_store()
        try:
            # 1. Simulate the boot dispatch that recorded the banner.
            boot_report = scan_llm_provider_health(
                env={},
                ollama_ping_result=False,
                ollama_models=None,
                default_provider="",
                default_model="",
            )
            dispatch_llm_discovery_verdict(boot_report)
            assert len(get_default_degraded_store().snapshot()) == 1

            # 2. Wire a router + a REAL liveness probe into the registry.
            router_mock = MagicMock()
            router_mock._providers = []
            router_mock.add_provider = MagicMock()
            router_mock.update_discovery_report = MagicMock()

            ollama = MagicMock()
            ollama.is_available = False
            ollama.ping = AsyncMock(return_value=False)
            ollama.list_models = AsyncMock(return_value=[])

            probe = LLMLivenessProbe(
                router=router_mock,
                ollama_provider=ollama,
                config=LLMTuningConfig(),
                mind_config=app.state.mind_config,
                boot_verdict=DiscoveryVerdict.NO_PROVIDER_CONFIGURED,
            )

            async def _resolve(interface):
                return {LLMRouter: router_mock, LLMLivenessProbe: probe}[interface]

            app.state.registry.is_registered = MagicMock(return_value=True)
            app.state.registry.resolve = AsyncMock(side_effect=_resolve)

            # 3. Avoid a live key-validation network call.
            provider_instance = MagicMock()
            provider_instance.name = "openai"
            monkeypatch.setattr(
                onboarding_mod, "_create_provider", lambda *a, **k: provider_instance
            )

            async def _ok(_provider):
                return (True, "validated")

            monkeypatch.setattr(onboarding_mod, "_test_provider", _ok)

            # 4. Drive the real onboarding route.
            resp = client.post(
                "/api/onboarding/provider",
                json={"provider": "openai", "api_key": "sk-test", "model": "gpt-4o"},
            )
            assert resp.status_code == 200  # noqa: PLR2004
            assert resp.json()["ok"] is True

            # 5. Synchronous clear-edge: refresh_now re-scanned os.environ (the
            #    route hot-set OPENAI_API_KEY) → FULLY_AVAILABLE → clear_axis.
            assert get_default_degraded_store().snapshot() == []
            router_mock.add_provider.assert_called_once_with(provider_instance)
        finally:
            reset_default_degraded_store()
            os.environ.pop("OPENAI_API_KEY", None)


class TestConfigurePersonality:
    """POST /api/onboarding/personality."""

    def test_preset_warm(self, client: TestClient, app) -> None:
        resp = client.post(
            "/api/onboarding/personality",
            json={"preset": "warm"},
        )
        assert resp.status_code == 200  # noqa: PLR2004
        assert resp.json()["ok"] is True
        assert app.state.mind_config.personality.tone == "warm"

    def test_preset_direct(self, client: TestClient, app) -> None:
        resp = client.post(
            "/api/onboarding/personality",
            json={"preset": "direct"},
        )
        assert resp.status_code == 200  # noqa: PLR2004
        assert app.state.mind_config.personality.tone == "direct"

    def test_custom_personality(self, client: TestClient, app) -> None:
        resp = client.post(
            "/api/onboarding/personality",
            json={"personality": {"tone": "playful", "humor": 0.9}},
        )
        assert resp.status_code == 200  # noqa: PLR2004
        assert app.state.mind_config.personality.tone == "playful"
        assert app.state.mind_config.personality.humor == 0.9  # noqa: PLR2004

    def test_language_update(self, client: TestClient, app) -> None:
        resp = client.post(
            "/api/onboarding/personality",
            json={"language": "pt"},
        )
        assert resp.status_code == 200  # noqa: PLR2004
        assert app.state.mind_config.language == "pt"

    def test_companion_name_update(self, client: TestClient, app) -> None:
        resp = client.post(
            "/api/onboarding/personality",
            json={"companion_name": "Nova"},
        )
        assert resp.status_code == 200  # noqa: PLR2004
        assert app.state.mind_config.name == "Nova"

    def test_invalid_json_422(self, client: TestClient) -> None:
        resp = client.post(
            "/api/onboarding/personality",
            content=b"not json",
            headers={
                "Authorization": f"Bearer {_TOKEN}",
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 422  # noqa: PLR2004


class TestTelegramChannel:
    """POST /api/onboarding/channel/telegram."""

    def test_missing_token_422(self, client: TestClient) -> None:
        resp = client.post("/api/onboarding/channel/telegram", json={})
        assert resp.status_code == 422  # noqa: PLR2004

    def test_empty_token_422(self, client: TestClient) -> None:
        resp = client.post("/api/onboarding/channel/telegram", json={"token": ""})
        assert resp.status_code == 422  # noqa: PLR2004


class TestCompleteOnboarding:
    """POST /api/onboarding/complete."""

    def test_marks_complete(self, client: TestClient, app) -> None:
        assert app.state.mind_config.onboarding_complete is False
        resp = client.post("/api/onboarding/complete")
        assert resp.status_code == 200  # noqa: PLR2004
        assert resp.json()["ok"] is True
        assert app.state.mind_config.onboarding_complete is True

    def test_persists_to_yaml(self, client: TestClient, app, tmp_path) -> None:
        mind_yaml = tmp_path / "mind.yaml"
        mind_yaml.write_text("name: test\n")
        app.state.mind_yaml_path = str(mind_yaml)
        resp = client.post("/api/onboarding/complete")
        assert resp.status_code == 200  # noqa: PLR2004

    def test_voice_configured_false_when_voice_not_enabled(self, client: TestClient, app) -> None:
        """v0.31.4 GAP 8 closure: response carries ``voice_configured``
        so the frontend can render a "voice not configured" banner
        when operator finishes onboarding without enabling voice.
        Default mind_config has voice_enabled=False → response
        reports voice_configured=False."""
        # Explicit-set to False (the test fixture mind_config is a
        # MagicMock so default-attribute reads return mocks, not the
        # Pydantic default. Real production mind_config has
        # voice_enabled defaulted to False per MindConfig schema).
        app.state.mind_config.voice_enabled = False
        resp = client.post("/api/onboarding/complete")
        assert resp.status_code == 200  # noqa: PLR2004
        assert resp.json()["voice_configured"] is False

    def test_voice_configured_true_requires_pipeline_registered(
        self, client: TestClient, app
    ) -> None:
        """voice_configured=True requires BOTH voice_enabled=True
        AND a registered VoicePipeline. Either alone is insufficient
        — voice_enabled may be persisted but pipeline failed to come
        up; pipeline could be running with voice_enabled=false (legacy
        path)."""
        from unittest.mock import MagicMock

        app.state.mind_config.voice_enabled = True
        # Wire a registry that reports VoicePipeline registered.
        registry = MagicMock()
        registry.is_registered.return_value = True
        app.state.registry = registry
        resp = client.post("/api/onboarding/complete")
        assert resp.status_code == 200  # noqa: PLR2004
        assert resp.json()["voice_configured"] is True

    def test_voice_configured_false_when_pipeline_not_registered(
        self, client: TestClient, app
    ) -> None:
        """voice_enabled=True but pipeline not registered → reports
        voice_configured=False so operator gets the warning banner."""
        from unittest.mock import MagicMock

        app.state.mind_config.voice_enabled = True
        registry = MagicMock()
        registry.is_registered.return_value = False
        app.state.registry = registry
        resp = client.post("/api/onboarding/complete")
        assert resp.status_code == 200  # noqa: PLR2004
        assert resp.json()["voice_configured"] is False


class TestEndToEndFlow:
    """Full onboarding flow: state → provider → personality → complete."""

    def test_full_flow(self, client: TestClient, app) -> None:
        # 1. Check initial state
        resp = client.get("/api/onboarding/state")
        assert resp.json()["complete"] is False

        # 2. Configure personality
        resp = client.post(
            "/api/onboarding/personality",
            json={"preset": "warm", "language": "en"},
        )
        assert resp.json()["ok"] is True

        # 3. Complete onboarding
        resp = client.post("/api/onboarding/complete")
        assert resp.json()["ok"] is True

        # 4. Verify state is now complete
        resp = client.get("/api/onboarding/state")
        assert resp.json()["complete"] is True
