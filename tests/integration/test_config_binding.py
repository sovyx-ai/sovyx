"""VAL-27: Config → Server binding integration.

Verifies that APIConfig correctly flows through to the FastAPI app:
CORS origins, static file handling, auth config.
"""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from sovyx.dashboard.server import create_app
from sovyx.engine.config import APIConfig

_TOKEN = "test-token-fixo"


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {_TOKEN}"}


class TestCORSConfig:
    """CORS origins from APIConfig flow to FastAPI middleware."""

    async def test_cors_allows_configured_origin(self) -> None:
        config = APIConfig(cors_origins=["http://localhost:3000"])
        app = create_app(config, token=_TOKEN)
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

    async def test_cors_blocks_unconfigured_origin(self) -> None:
        config = APIConfig(cors_origins=["http://localhost:3000"])
        app = create_app(config, token=_TOKEN)
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

    async def test_default_host_and_port(self) -> None:
        config = APIConfig()
        assert config.host == "127.0.0.1"
        assert config.port == 7777
        assert config.enabled is True

    async def test_disabled_config(self) -> None:
        config = APIConfig(enabled=False)
        assert config.enabled is False
        # App can still be created (the caller decides whether to start it)
        app = create_app(config, token=_TOKEN)
        assert app is not None


class TestCustomPort:
    """Custom port config is accepted."""

    async def test_custom_port(self) -> None:
        config = APIConfig(port=9999)
        app = create_app(config, token=_TOKEN)
        transport = ASGITransport(app=app)  # type: ignore[arg-type]
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.get("/api/status", headers=_auth())
            assert r.status_code == 200
