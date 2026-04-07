"""VAL-33: SQL injection testing on all API endpoints.

Tests parameterized queries by sending SQL injection payloads
through all user-controllable inputs.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from sovyx.dashboard.server import create_app
from sovyx.engine.config import APIConfig

_TOKEN = "sqli-test-token"

_SQLI_PAYLOADS = [
    "' OR '1'='1",
    "'; DROP TABLE conversations; --",
    "\" OR \"1\"=\"1",
    "1 UNION SELECT * FROM sqlite_master",
    "1; ATTACH DATABASE ':memory:' AS x",
    "'); DELETE FROM concepts; --",
    "' AND 1=1 --",
    "' WAITFOR DELAY '0:0:5' --",
    "${sleep(5)}",
    "1' AND (SELECT COUNT(*) FROM sqlite_master) > 0 --",
]


@pytest.fixture()
def app() -> object:
    with patch("sovyx.dashboard.server.TOKEN_FILE") as mock_tf:
        mock_tf.exists.return_value = True
        mock_tf.read_text.return_value = _TOKEN
        return create_app(APIConfig(host="127.0.0.1", port=0))


@pytest.fixture()
async def client(app: object) -> AsyncClient:
    transport = ASGITransport(app=app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c  # type: ignore[misc]


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {_TOKEN}"}


class TestConversationIdInjection:
    """GET /api/conversations/{id} with SQL injection payloads."""

    @pytest.mark.parametrize("payload", _SQLI_PAYLOADS)
    async def test_conversation_id(
        self, client: AsyncClient, payload: str,
    ) -> None:
        r = await client.get(
            f"/api/conversations/{payload}",
            headers=_auth(),
        )
        # Should return 404 (not found) or 422 (validation error), never 500
        assert r.status_code in {
            200, 404, 422,
        }, f"Unexpected {r.status_code} for payload: {payload}"
        # If 200, should be empty/null, not all records
        if r.status_code == 200:
            data = r.json()
            assert not isinstance(data, list) or len(data) <= 1


class TestQueryParamInjection:
    """Query parameters with SQL injection payloads."""

    @pytest.mark.parametrize("payload", _SQLI_PAYLOADS)
    async def test_logs_level_filter(
        self, client: AsyncClient, payload: str,
    ) -> None:
        r = await client.get(
            "/api/logs",
            params={"level": payload},
            headers=_auth(),
        )
        assert r.status_code in {200, 400, 422}

    @pytest.mark.parametrize("payload", _SQLI_PAYLOADS)
    async def test_logs_search_filter(
        self, client: AsyncClient, payload: str,
    ) -> None:
        r = await client.get(
            "/api/logs",
            params={"search": payload},
            headers=_auth(),
        )
        assert r.status_code in {200, 400, 422}


class TestSettingsInjection:
    """PUT /api/settings with injection in body."""

    async def test_settings_payload(self, client: AsyncClient) -> None:
        r = await client.put(
            "/api/settings",
            json={"name": "'; DROP TABLE settings; --", "value": "test"},
            headers=_auth(),
        )
        # Should not crash — 200, 400, or 422
        assert r.status_code in {200, 400, 422}
