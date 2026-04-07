"""VAL-27: Config → Server binding integration.

Verifies that APIConfig correctly flows through to the FastAPI app:
CORS origins, static file handling, auth config.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from sovyx.dashboard.server import create_app
from sovyx.engine.config import APIConfig

_TOKEN = "config-test-token"


@pytest.fixture()
def _mock_token() -> object:
    with patch("sovyx.dashboard.server.TOKEN_FILE") as mock_tf:
        mock_tf.exists.return_value = True
        mock_tf.read_text.return_value = _TOKEN
        yield mock_tf


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {_TOKEN}"}


class TestCORSConfig:
    """CORS origins from APIConfig flow to FastAPI middleware."""

    @pytest.mark.usefixtures("_mock_token")
    async def test_cors_allows_configured_origin(self) -> None:
        config = APIConfig(cors_origins=["http://localhost:3000"])
        app = create_app(config)
        transport = ASGITransport(app=app)  # type: ignore[arg-type]
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.options(
                "/api/status",
                headers={
                    "Origin": "http://localhost:3000",
                    "Access-Control-Request-Method": "GET",
                },
            )
            assert r.headers.get("access-control-allow-origin") == "http://localhost:3000"

    @pytest.mark.usefixtures("_mock_token")
    async def test_cors_blocks_unconfigured_origin(self) -> None:
        config = APIConfig(cors_origins=["http://localhost:3000"])
        app = create_app(config)
        transport = ASGITransport(app=app)  # type: ignore[arg-type]
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.options(
                "/api/status",
                headers={
                    "Origin": "http://evil.com",
                    "Access-Control-Request-Method": "GET",
                },
            )
            # Should not include evil.com in CORS headers
            acao = r.headers.get("access-control-allow-origin", "")
            assert "evil.com" not in acao


class TestDefaultConfig:
    """Default APIConfig values work correctly."""

    @pytest.mark.usefixtures("_mock_token")
    async def test_default_host_and_port(self) -> None:
        config = APIConfig()
        assert config.host == "127.0.0.1"
        assert config.port == 7777
        assert config.enabled is True

    @pytest.mark.usefixtures("_mock_token")
    async def test_disabled_config(self) -> None:
        config = APIConfig(enabled=False)
        assert config.enabled is False
        # App can still be created (the caller decides whether to start it)
        app = create_app(config)
        assert app is not None


class TestCustomPort:
    """Custom port config is accepted."""

    @pytest.mark.usefixtures("_mock_token")
    async def test_custom_port(self) -> None:
        config = APIConfig(port=9999)
        app = create_app(config)
        transport = ASGITransport(app=app)  # type: ignore[arg-type]
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.get("/api/status", headers=_auth())
            assert r.status_code == 200
