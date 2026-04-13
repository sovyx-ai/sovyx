"""Tests for GET /api/providers — LLM provider status endpoint.

Covers: provider listing, Ollama ping/models, cloud provider detection,
active config, error handling, no-registry case.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, PropertyMock

import pytest
from fastapi.testclient import TestClient

from sovyx.dashboard.server import create_app
from sovyx.mind.config import MindConfig


def _make_mock_provider(
    name: str,
    available: bool = True,
) -> MagicMock:
    """Create a mock LLM provider."""
    p = MagicMock()
    p.name = name
    type(p).is_available = PropertyMock(return_value=available)
    return p


def _make_ollama_provider(
    reachable: bool = True,
    models: list[str] | None = None,
    base_url: str = "http://localhost:11434",
) -> MagicMock:
    """Create a mock Ollama provider with ping/list_models."""
    from sovyx.llm.providers.ollama import OllamaProvider

    p = MagicMock(spec=OllamaProvider)
    p.name = "ollama"
    type(p).is_available = PropertyMock(return_value=reachable)
    type(p).base_url = PropertyMock(return_value=base_url)
    p.ping = AsyncMock(return_value=reachable)
    p.list_models = AsyncMock(return_value=models or [])
    return p


@pytest.fixture()
def app(token: str) -> TestClient:
    """Create test client with valid auth (token from conftest)."""
    application = create_app()
    return TestClient(application)


@pytest.fixture()
def auth(token: str) -> dict[str, str]:
    """Auth headers (token from conftest)."""
    return {"Authorization": f"Bearer {token}"}


def _wire_app(
    client: TestClient,
    providers: list[MagicMock],
    mind: MindConfig | None = None,
) -> None:
    """Wire mock registry + mind config into the test app."""
    router = MagicMock()
    router._providers = providers

    registry = MagicMock()
    registry.is_registered = MagicMock(return_value=True)
    registry.resolve = AsyncMock(return_value=router)

    client.app.state.registry = registry  # type: ignore[union-attr]
    client.app.state.mind_config = mind or MindConfig(name="Test")  # type: ignore[union-attr]


class TestGetProviders:
    """GET /api/providers tests."""

    @staticmethod
    def _get(client: TestClient, headers: dict[str, str]) -> dict:  # type: ignore[type-arg]
        resp = client.get("/api/providers", headers=headers)
        assert resp.status_code == 200
        return resp.json()

    def test_cloud_provider_available(self, app: TestClient, auth: dict[str, str]) -> None:
        openai = _make_mock_provider("openai", available=True)
        ollama = _make_ollama_provider(reachable=False)

        mind = MindConfig(name="Test")
        mind.llm.default_provider = "openai"
        mind.llm.default_model = "gpt-4o"
        _wire_app(app, [openai, ollama], mind)

        data = self._get(app, auth)
        openai_info = next(p for p in data["providers"] if p["name"] == "openai")
        assert openai_info["configured"] is True
        assert openai_info["available"] is True
        assert data["active"]["provider"] == "openai"
        assert data["active"]["model"] == "gpt-4o"

    def test_ollama_reachable_with_models(self, app: TestClient, auth: dict[str, str]) -> None:
        ollama = _make_ollama_provider(
            reachable=True,
            models=["llama3.1:latest", "mistral:7b"],
            base_url="http://gpu:11434",
        )
        mind = MindConfig(name="Test")
        mind.llm.default_provider = "ollama"
        mind.llm.default_model = "llama3.1:latest"
        _wire_app(app, [ollama], mind)

        data = self._get(app, auth)
        info = data["providers"][0]
        assert info["reachable"] is True
        assert info["models"] == ["llama3.1:latest", "mistral:7b"]
        assert info["base_url"] == "http://gpu:11434"

    def test_ollama_unreachable(self, app: TestClient, auth: dict[str, str]) -> None:
        ollama = _make_ollama_provider(reachable=False)
        _wire_app(app, [ollama])

        data = self._get(app, auth)
        info = data["providers"][0]
        assert info["reachable"] is False
        assert info["models"] == []

    def test_no_registry_returns_503(self, app: TestClient, auth: dict[str, str]) -> None:
        app.app.state.registry = None  # type: ignore[union-attr]
        resp = app.get("/api/providers", headers=auth)
        assert resp.status_code == 503

    def test_active_reflects_mind_config(self, app: TestClient, auth: dict[str, str]) -> None:
        mind = MindConfig(name="Test")
        mind.llm.default_provider = "anthropic"
        mind.llm.default_model = "claude-3-opus"
        mind.llm.fast_model = "claude-3-haiku"

        router = MagicMock()
        router._providers = []
        registry = MagicMock()
        registry.is_registered = MagicMock(return_value=True)
        registry.resolve = AsyncMock(return_value=router)
        app.app.state.registry = registry  # type: ignore[union-attr]
        app.app.state.mind_config = mind  # type: ignore[union-attr]

        data = self._get(app, auth)
        assert data["active"]["provider"] == "anthropic"
        assert data["active"]["model"] == "claude-3-opus"
        assert data["active"]["fast_model"] == "claude-3-haiku"

    def test_multiple_providers_ordered(self, app: TestClient, auth: dict[str, str]) -> None:
        openai = _make_mock_provider("openai", available=True)
        anthropic = _make_mock_provider("anthropic", available=False)
        ollama = _make_ollama_provider(reachable=True, models=["phi3:mini"])
        _wire_app(app, [openai, anthropic, ollama])

        data = self._get(app, auth)
        providers = data["providers"]
        assert len(providers) == 3
        names = [p["name"] for p in providers]
        assert names == ["openai", "anthropic", "ollama"]
        assert providers[0]["available"] is True
        assert providers[1]["available"] is False
        assert providers[2]["reachable"] is True

    def test_ollama_configured_always_true(self, app: TestClient, auth: dict[str, str]) -> None:
        """Ollama configured is always True (it's always registered)."""
        ollama = _make_ollama_provider(reachable=False)
        _wire_app(app, [ollama])

        data = self._get(app, auth)
        info = data["providers"][0]
        assert info["configured"] is True
        assert info["available"] is False

    def test_no_mind_config(self, app: TestClient, auth: dict[str, str]) -> None:
        """No mind config → empty active section."""
        router = MagicMock()
        router._providers = []
        registry = MagicMock()
        registry.is_registered = MagicMock(return_value=True)
        registry.resolve = AsyncMock(return_value=router)
        app.app.state.registry = registry  # type: ignore[union-attr]
        app.app.state.mind_config = None  # type: ignore[union-attr]

        data = self._get(app, auth)
        assert data["active"]["provider"] == ""
        assert data["active"]["model"] == ""


class TestPutProviders:
    """PUT /api/providers — switch active provider at runtime."""

    @staticmethod
    def _put(
        client: TestClient,
        headers: dict[str, str],
        body: dict[str, str],
    ) -> tuple[int, dict]:  # type: ignore[type-arg]
        resp = client.put("/api/providers", json=body, headers=headers)
        return resp.status_code, resp.json()

    def test_switch_provider_success(self, app: TestClient, auth: dict[str, str]) -> None:
        """Switch from openai to ollama succeeds."""

        ollama = _make_ollama_provider(reachable=True, models=["llama3.1:latest"])

        mind = MindConfig(name="Test")
        mind.llm.default_provider = "openai"
        mind.llm.default_model = "gpt-4o"
        _wire_app(app, [_make_mock_provider("openai"), ollama], mind)

        status, data = self._put(app, auth, {"provider": "ollama", "model": "llama3.1:latest"})
        assert status == 200
        assert data["ok"] is True
        assert "ollama" in data["changes"]["provider"]

        # Verify runtime update
        assert mind.llm.default_provider == "ollama"
        assert mind.llm.default_model == "llama3.1:latest"

    def test_unknown_provider_rejected(self, app: TestClient, auth: dict[str, str]) -> None:
        _wire_app(app, [_make_mock_provider("openai")])
        status, data = self._put(app, auth, {"provider": "nonexistent", "model": "x"})
        assert status == 422
        assert "Unknown provider" in data["error"]

    def test_ollama_unreachable_rejected(self, app: TestClient, auth: dict[str, str]) -> None:
        ollama = _make_ollama_provider(reachable=False)
        _wire_app(app, [ollama])
        status, data = self._put(app, auth, {"provider": "ollama", "model": "llama3.1"})
        assert status == 422
        assert "not reachable" in data["error"]

    def test_cloud_unavailable_rejected(self, app: TestClient, auth: dict[str, str]) -> None:
        openai = _make_mock_provider("openai", available=False)
        _wire_app(app, [openai])
        status, data = self._put(app, auth, {"provider": "openai", "model": "gpt-4o"})
        assert status == 422
        assert "not configured" in data["error"]

    def test_missing_fields_rejected(self, app: TestClient, auth: dict[str, str]) -> None:
        _wire_app(app, [])
        status, data = self._put(app, auth, {"provider": "ollama"})
        assert status == 422
        assert "required" in data["error"]

    def test_no_mind_config_503(self, app: TestClient, auth: dict[str, str]) -> None:
        app.app.state.mind_config = None  # type: ignore[union-attr]
        app.app.state.registry = MagicMock()  # type: ignore[union-attr]
        status, _ = self._put(app, auth, {"provider": "x", "model": "y"})
        assert status == 503

    def test_invalid_json_422(self, app: TestClient, auth: dict[str, str]) -> None:
        resp = app.put(
            "/api/providers",
            content=b"not json",
            headers={**auth, "Content-Type": "application/json"},
        )
        assert resp.status_code == 422
