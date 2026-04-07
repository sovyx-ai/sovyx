"""VAL-36: API contract properties — fuzz random inputs on all endpoints."""

from __future__ import annotations

import secrets
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from sovyx.dashboard.server import create_app
from sovyx.engine.config import APIConfig

_TOKEN = "contract-test"


@pytest.fixture()
async def client() -> AsyncClient:
    with patch("sovyx.dashboard.server.TOKEN_FILE") as mock_tf:
        mock_tf.exists.return_value = True
        mock_tf.read_text.return_value = _TOKEN
        app = create_app(APIConfig(host="127.0.0.1", port=0))
    transport = ASGITransport(app=app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c  # type: ignore[misc]


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {_TOKEN}"}


class TestStatusContract:
    async def test_always_returns_dict(self, client: AsyncClient) -> None:
        r = await client.get("/api/status", headers=_auth())
        assert r.status_code == 200
        assert isinstance(r.json(), dict)


class TestHealthContract:
    async def test_has_checks_or_status(self, client: AsyncClient) -> None:
        r = await client.get("/api/health", headers=_auth())
        assert r.status_code == 200
        data = r.json()
        assert "checks" in data or "status" in data


class TestConversationIdFuzz:
    @pytest.mark.parametrize(
        "conv_id",
        [
            "valid-uuid-1234",
            "a" * 200,
            "",
            "../../etc/passwd",
            "<script>alert(1)</script>",
            "null",
            "-1",
            secrets.token_hex(32),
        ],
    )
    async def test_random_ids_never_crash(
        self,
        client: AsyncClient,
        conv_id: str,
    ) -> None:
        r = await client.get(
            f"/api/conversations/{conv_id}",
            headers=_auth(),
        )
        # 503 is acceptable when registry is not bootstrapped (CI without full setup)
        assert r.status_code in {200, 404, 422, 503}


class TestLogsQueryFuzz:
    @pytest.mark.parametrize(
        ("level", "limit"),
        [
            ("DEBUG", 10),
            ("INFO", 100),
            ("WARNING", 0),
            ("ERROR", -1),
            ("CRITICAL", 1000),
            ("invalid", 50),
            ("ALL", 50),
            ("", 50),
        ],
    )
    async def test_log_params_never_crash(
        self,
        client: AsyncClient,
        level: str,
        limit: int,
    ) -> None:
        r = await client.get(
            "/api/logs",
            params={"level": level, "limit": limit},
            headers=_auth(),
        )
        assert r.status_code in {200, 400, 422}


class TestBrainGraphContract:
    async def test_returns_nodes_and_links(self, client: AsyncClient) -> None:
        r = await client.get("/api/brain/graph", headers=_auth())
        assert r.status_code == 200
        data = r.json()
        assert "nodes" in data
        assert "links" in data
        assert isinstance(data["nodes"], list)
        assert isinstance(data["links"], list)
