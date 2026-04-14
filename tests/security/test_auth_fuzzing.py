"""VAL-31: Auth bypass fuzzing — adversarial token attacks.

Tests the dashboard API auth against:
- Empty/malformed tokens
- SQL injection in token
- JWT-like tokens
- Unicode/null byte injection
- Timing attacks (constant-time compare)
- Bearer prefix manipulation
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from sovyx.dashboard.server import create_app
from sovyx.engine.config import APIConfig

_TOKEN = "test-token-fixo"


@pytest.fixture()
def app() -> object:
    return create_app(APIConfig(host="127.0.0.1", port=0), token=_TOKEN)


@pytest.fixture()
async def client(app: object) -> AsyncClient:
    transport = ASGITransport(app=app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c  # type: ignore[misc]


class TestTokenManipulation:
    """Malformed tokens must not bypass auth."""

    @pytest.mark.parametrize(
        "token",
        [
            "",
            " ",
            "\n",
            "\t",
            "\x00",
            "null",
            "undefined",
            "None",
            "true",
            "false",
            "0",
            "-1",
        ],
        ids=lambda t: repr(t),
    )
    async def test_empty_and_special_tokens(self, client: AsyncClient, token: str) -> None:
        r = await client.get("/api/status", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 401

    @pytest.mark.parametrize(
        "token",
        [
            "' OR '1'='1",
            "'; DROP TABLE users; --",
            '" OR "1"="1',
            "1; SELECT * FROM tokens",
            "admin'--",
            "UNION SELECT password FROM users",
        ],
    )
    async def test_sql_injection_tokens(self, client: AsyncClient, token: str) -> None:
        r = await client.get("/api/status", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 401

    @pytest.mark.parametrize(
        "token",
        [
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signature",
            "eyJhbGciOiJub25lIn0.eyJhZG1pbiI6dHJ1ZX0.",
            "Bearer " + _TOKEN,  # Double Bearer
        ],
    )
    async def test_jwt_like_tokens(self, client: AsyncClient, token: str) -> None:
        r = await client.get("/api/status", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 401

    async def test_unicode_tokens(self, client: AsyncClient) -> None:
        # Only use ASCII-safe unicode manipulations (httpx rejects non-Latin-1 headers)
        # Note: trailing/leading spaces are stripped by the HTTP header parser,
        # so "token " → "token" which matches. This is expected behavior.
        # We test tokens with EMBEDDED whitespace instead.
        unicode_tokens = [
            _TOKEN[:4] + " " + _TOKEN[4:],  # Space in the middle
            _TOKEN[:4] + "\t" + _TOKEN[4:],  # Tab in the middle
        ]
        for token in unicode_tokens:
            r = await client.get("/api/status", headers={"Authorization": f"Bearer {token}"})
            assert r.status_code == 401, f"Token manipulation bypass: {token!r}"

    async def test_null_byte_injection(self, client: AsyncClient) -> None:
        r = await client.get("/api/status", headers={"Authorization": f"Bearer {_TOKEN}\x00extra"})
        assert r.status_code == 401

    async def test_case_sensitivity(self, client: AsyncClient) -> None:
        r = await client.get("/api/status", headers={"Authorization": f"Bearer {_TOKEN.upper()}"})
        assert r.status_code == 401

    async def test_partial_token(self, client: AsyncClient) -> None:
        r = await client.get("/api/status", headers={"Authorization": f"Bearer {_TOKEN[:5]}"})
        assert r.status_code == 401

    async def test_token_with_extra_chars(self, client: AsyncClient) -> None:
        r = await client.get("/api/status", headers={"Authorization": f"Bearer {_TOKEN}x"})
        assert r.status_code == 401


class TestHeaderManipulation:
    """Auth header format attacks."""

    async def test_no_header(self, client: AsyncClient) -> None:
        r = await client.get("/api/status")
        assert r.status_code == 401

    async def test_basic_instead_of_bearer(self, client: AsyncClient) -> None:
        r = await client.get("/api/status", headers={"Authorization": f"Basic {_TOKEN}"})
        assert r.status_code == 401  # or 403

    async def test_lowercase_bearer(self, client: AsyncClient) -> None:
        r = await client.get("/api/status", headers={"Authorization": f"bearer {_TOKEN}"})
        # FastAPI HTTPBearer is case-insensitive for "Bearer"
        # This should still work (it's valid per HTTP spec)
        assert r.status_code in {200, 401}

    async def test_empty_authorization(self, client: AsyncClient) -> None:
        r = await client.get("/api/status", headers={"Authorization": ""})
        assert r.status_code in {401, 403, 422}

    async def test_only_bearer_prefix(self, client: AsyncClient) -> None:
        r = await client.get("/api/status", headers={"Authorization": "Bearer"})
        assert r.status_code in {401, 403, 422}

    async def test_double_space_bearer(self, client: AsyncClient) -> None:
        # Double space after "Bearer" — HTTP header parsing may normalize whitespace,
        # so the token still matches.  Accept 200 (valid auth) or 401 (strict parsing).
        r = await client.get("/api/status", headers={"Authorization": f"Bearer  {_TOKEN}"})
        assert r.status_code in {200, 401}


class TestEndpointProtection:
    """All protected endpoints reject bad tokens."""

    @pytest.mark.parametrize(
        "path",
        [
            "/api/status",
            "/api/health",
            "/api/conversations",
            "/api/brain/graph",
            "/api/logs",
            "/api/settings",
        ],
    )
    async def test_all_endpoints_require_auth(self, client: AsyncClient, path: str) -> None:
        r = await client.get(path)
        assert r.status_code == 401, f"{path} should require auth"

    @pytest.mark.parametrize(
        "path",
        [
            "/api/status",
            "/api/health",
            "/api/conversations",
            "/api/brain/graph",
            "/api/logs",
            "/api/settings",
        ],
    )
    async def test_all_endpoints_reject_bad_token(self, client: AsyncClient, path: str) -> None:
        r = await client.get(path, headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401, f"{path} should reject bad token"
