"""Tests for /api/setup/{name}/* endpoints — setup wizard API."""

from __future__ import annotations

from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.testclient import TestClient

from sovyx.dashboard.server import create_app
from sovyx.plugins.sdk import ISovyxPlugin, TestResult

_TOKEN = "test-token-setup"


class _FakePlugin(ISovyxPlugin):
    """Minimal plugin with setup_schema and test_connection."""

    name = "fake-caldav"
    version = "1.0.0"
    description = "Fake CalDAV for testing."

    setup_schema: ClassVar[dict[str, object]] = {
        "providers": [
            {
                "id": "fastmail",
                "name": "Fastmail",
                "defaults": {"base_url": "https://caldav.fastmail.com/dav/"},
            },
        ],
        "fields": [
            {"id": "base_url", "type": "url", "label": "URL", "required": True},
            {"id": "username", "type": "string", "label": "User", "required": True},
            {"id": "password", "type": "secret", "label": "Pass", "required": True},
        ],
        "test_connection": True,
    }

    config_schema: ClassVar[dict[str, object]] = {
        "properties": {"base_url": {"type": "string"}},
    }

    _test_result: TestResult = TestResult(success=True, message="Connected")

    async def test_connection(self, config: dict[str, object]) -> TestResult:
        return self._test_result


class _NoSchemaPlugin(ISovyxPlugin):
    """Plugin with no setup_schema."""

    name = "calculator"
    version = "1.0.0"
    description = "Calculator."


@pytest.fixture
def app() -> object:
    application = create_app(token=_TOKEN)

    # Mock registry with PluginManager
    from sovyx.plugins._manager_types import LoadedPlugin
    from sovyx.plugins.context import PluginContext

    fake_plugin = _FakePlugin()
    no_schema_plugin = _NoSchemaPlugin()

    fake_ctx = MagicMock(spec=PluginContext)
    fake_ctx.config = {"base_url": "https://old.url"}
    fake_ctx.plugin_name = "fake-caldav"
    fake_ctx.plugin_version = "1.0.0"
    fake_ctx.data_dir = None
    fake_ctx.logger = MagicMock()
    fake_ctx.brain = None
    fake_ctx.event_bus = None

    no_ctx = MagicMock(spec=PluginContext)
    no_ctx.config = {}

    manager = MagicMock()
    manager.get_plugin.side_effect = lambda name: {
        "fake-caldav": LoadedPlugin(
            plugin=fake_plugin,
            tools=[],
            context=fake_ctx,
            enforcer=MagicMock(),
        ),
        "calculator": LoadedPlugin(
            plugin=no_schema_plugin,
            tools=[],
            context=no_ctx,
            enforcer=MagicMock(),
        ),
    }.get(name)
    manager.reconfigure = AsyncMock()
    manager.re_enable_plugin = AsyncMock()
    manager.disable_plugin = AsyncMock()
    manager.reconfigure = AsyncMock()
    manager.re_enable_plugin = AsyncMock()
    manager.disable_plugin = AsyncMock()

    registry = MagicMock()
    registry.is_registered.return_value = True
    registry.resolve = AsyncMock(return_value=manager)

    application.state.registry = registry
    application.state.mind_yaml_path = None

    return application


@pytest.fixture
def client(app: object) -> TestClient:
    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})  # type: ignore[arg-type]


class TestGetSchema:
    """GET /api/setup/{name}/schema."""

    def test_returns_setup_schema(self, client: TestClient) -> None:
        resp = client.get("/api/setup/fake-caldav/schema")
        assert resp.status_code == 200  # noqa: PLR2004
        data = resp.json()
        assert data["plugin"] == "fake-caldav"
        assert data["setup_schema"] is not None
        assert len(data["setup_schema"]["fields"]) == 3  # noqa: PLR2004
        assert data["setup_schema"]["providers"][0]["id"] == "fastmail"

    def test_no_schema_returns_null(self, client: TestClient) -> None:
        resp = client.get("/api/setup/calculator/schema")
        assert resp.status_code == 200  # noqa: PLR2004
        assert resp.json()["setup_schema"] is None

    def test_missing_plugin_404(self, client: TestClient) -> None:
        resp = client.get("/api/setup/nonexistent/schema")
        assert resp.status_code == 404  # noqa: PLR2004

    def test_no_auth_401(self) -> None:
        app = create_app(token=_TOKEN)
        c = TestClient(app)
        resp = c.get("/api/setup/fake-caldav/schema")
        assert resp.status_code == 401  # noqa: PLR2004


class TestTestConnection:
    """POST /api/setup/{name}/test-connection."""

    def test_success(self, client: TestClient) -> None:
        resp = client.post(
            "/api/setup/fake-caldav/test-connection",
            json={"config": {"base_url": "https://x", "username": "u", "password": "p"}},
        )
        assert resp.status_code == 200  # noqa: PLR2004
        data = resp.json()
        assert data["success"] is True
        assert data["message"] == "Connected"

    def test_missing_plugin_404(self, client: TestClient) -> None:
        resp = client.post(
            "/api/setup/nonexistent/test-connection",
            json={"config": {}},
        )
        assert resp.status_code == 404  # noqa: PLR2004


class TestConfigure:
    """POST /api/setup/{name}/configure."""

    def test_configure_calls_reconfigure(self, client: TestClient) -> None:
        resp = client.post(
            "/api/setup/fake-caldav/configure",
            json={"config": {"base_url": "https://new.url", "username": "me", "password": "pw"}},
        )
        assert resp.status_code == 200  # noqa: PLR2004
        assert resp.json()["ok"] is True

    def test_missing_plugin_404(self, client: TestClient) -> None:
        resp = client.post(
            "/api/setup/nonexistent/configure",
            json={"config": {}},
        )
        assert resp.status_code == 404  # noqa: PLR2004


class TestEnableDisable:
    """POST /api/setup/{name}/enable and /disable."""

    def test_enable(self, client: TestClient) -> None:
        resp = client.post("/api/setup/fake-caldav/enable")
        assert resp.status_code == 200  # noqa: PLR2004
        assert resp.json()["action"] == "enabled"

    def test_disable(self, client: TestClient) -> None:
        resp = client.post("/api/setup/fake-caldav/disable")
        assert resp.status_code == 200  # noqa: PLR2004
        assert resp.json()["action"] == "disabled"

    def test_enable_delegates_to_manager(self, client: TestClient, app) -> None:
        resp = client.post("/api/setup/fake-caldav/enable")
        assert resp.status_code == 200  # noqa: PLR2004
        manager = app.state.registry.resolve.return_value
        manager.re_enable_plugin.assert_called()

    def test_disable_delegates_to_manager(self, client: TestClient, app) -> None:
        resp = client.post("/api/setup/fake-caldav/disable")
        assert resp.status_code == 200  # noqa: PLR2004
        manager = app.state.registry.resolve.return_value
        manager.disable_plugin.assert_called()


class TestTestConnectionEdgeCases:
    """Edge cases for test-connection."""

    def test_connection_failure_result(self, client: TestClient) -> None:
        _FakePlugin._test_result = TestResult(success=False, message="Auth failed")
        resp = client.post(
            "/api/setup/fake-caldav/test-connection",
            json={"config": {"base_url": "https://x", "username": "u", "password": "p"}},
        )
        assert resp.status_code == 200  # noqa: PLR2004
        data = resp.json()
        assert data["success"] is False
        assert data["message"] == "Auth failed"
        _FakePlugin._test_result = TestResult(success=True, message="Connected")

    def test_no_test_connection_for_no_schema_plugin(self, client: TestClient) -> None:
        resp = client.post(
            "/api/setup/calculator/test-connection",
            json={"config": {}},
        )
        assert resp.status_code == 200  # noqa: PLR2004


class TestSchemaEdgeCases:
    """Edge cases for schema endpoint."""

    def test_schema_includes_providers(self, client: TestClient) -> None:
        resp = client.get("/api/setup/fake-caldav/schema")
        data = resp.json()
        providers = data["setup_schema"]["providers"]
        assert len(providers) == 1
        assert providers[0]["id"] == "fastmail"
        assert "defaults" in providers[0]

    def test_schema_field_types(self, client: TestClient) -> None:
        resp = client.get("/api/setup/fake-caldav/schema")
        fields = resp.json()["setup_schema"]["fields"]
        types = [f["type"] for f in fields]
        assert "url" in types
        assert "string" in types
        assert "secret" in types

    def test_schema_test_connection_flag(self, client: TestClient) -> None:
        resp = client.get("/api/setup/fake-caldav/schema")
        assert resp.json()["setup_schema"]["test_connection"] is True
