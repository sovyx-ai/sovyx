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
