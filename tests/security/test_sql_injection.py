"""VAL-33: SQL injection tests for all dashboard endpoints.

Verifies that malicious SQL payloads in query params and path params
are safely handled — no unintended queries, no crashes, proper HTTP
error codes (422 for bad params, 200/404 for safe parameterized queries).

The dashboard uses SQLite with parameterized queries throughout, so
these tests validate the defense-in-depth posture.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from sovyx.dashboard.server import create_app
from sovyx.engine.config import APIConfig

_TOKEN = "test-token-fixo"

# ── Classic SQL injection payloads ──────────────────────────────────────────

SQL_PAYLOADS = [
    "1;DROP TABLE conversations",
    "1;DROP TABLE persons--",
    "1 OR 1=1",
    "1 OR 1=1--",
    "1' OR '1'='1",
    "1' OR '1'='1'--",
    "'; DROP TABLE conversations; --",
    "1; DELETE FROM persons WHERE '1'='1",
    "' UNION SELECT * FROM persons--",
    "1 UNION SELECT id,name,display_name,metadata,created_at,updated_at FROM persons",
    "1; UPDATE persons SET name='pwned'--",
    "'; INSERT INTO persons VALUES('hack','x','x','{}',datetime(),datetime());--",
    "-1 OR 1=1",
    "0; ATTACH DATABASE ':memory:' AS hack",
    "1/**/OR/**/1=1",
]

PATH_PAYLOADS = [
    "'; DROP TABLE conversations--",
    "' OR '1'='1",
    "1 UNION SELECT 1,2,3,4",
    "../../../etc/passwd",
    "'; DELETE FROM conversation_turns;--",
    "null",
    "undefined",
    "' OR ''='",
]

LEVEL_PAYLOADS = [
    "INFO' OR '1'='1",
    "INFO; DROP TABLE--",
    "INFO' UNION SELECT 1--",
    "DEBUG'; DELETE FROM persons;--",
    "WARNING/**/OR/**/1=1",
]


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture()
def client() -> TestClient:
    app = create_app(APIConfig(host="127.0.0.1", port=0), token=_TOKEN)
    return TestClient(app)


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_TOKEN}"}


# ── /api/conversations?limit=... & ?offset=... ─────────────────────────────


class TestConversationsLimitInjection:
    """SQL injection via ?limit= query param."""

    @pytest.mark.parametrize("payload", SQL_PAYLOADS[:8])
    def test_limit_injection_rejected(self, client: TestClient, payload: str) -> None:
        """Malicious limit values → 422 (FastAPI validation rejects non-int)."""
        resp = client.get(
            f"/api/conversations?limit={payload}",
            headers=_auth_headers(),
        )
        assert resp.status_code == 422

    @pytest.mark.parametrize("payload", SQL_PAYLOADS[:8])
    def test_offset_injection_rejected(self, client: TestClient, payload: str) -> None:
        """Malicious offset values → 422."""
        resp = client.get(
            f"/api/conversations?offset={payload}",
            headers=_auth_headers(),
        )
        assert resp.status_code == 422

    def test_negative_limit_rejected(self, client: TestClient) -> None:
        resp = client.get("/api/conversations?limit=-1", headers=_auth_headers())
        assert resp.status_code == 422

    def test_overflow_limit_rejected(self, client: TestClient) -> None:
        resp = client.get("/api/conversations?limit=999999", headers=_auth_headers())
        assert resp.status_code == 422


# ── /api/conversations/{id} ─────────────────────────────────────────────────


class TestConversationIdInjection:
    """SQL injection via conversation_id path param."""

    @pytest.mark.parametrize("payload", PATH_PAYLOADS)
    def test_path_injection_safe(self, client: TestClient, payload: str) -> None:
        """Malicious conversation_id → safe response (200 empty or 404)."""
        resp = client.get(
            f"/api/conversations/{payload}",
            headers=_auth_headers(),
        )
        # Payloads with slashes (e.g. ../../../etc/passwd) won't match the
        # {conversation_id} route — Starlette routes them to the SPA fallback
        # which returns 200 with HTML.  That's safe (no data leak, no crash).
        # Other payloads hit the API and return 200 JSON (empty) or 404.
        # The key invariant: never 500.
        assert resp.status_code != 500
        if resp.status_code == 200:
            content_type = resp.headers.get("content-type", "")
            if "application/json" in content_type:
                data = resp.json()
                # No data leaked — should be empty since the ID doesn't exist
                assert data.get("messages", []) == []


# ── /api/brain/graph?limit=... ──────────────────────────────────────────────


class TestBrainGraphInjection:
    """SQL injection via brain graph limit param."""

    @pytest.mark.parametrize("payload", SQL_PAYLOADS[:6])
    def test_limit_injection_rejected(self, client: TestClient, payload: str) -> None:
        resp = client.get(
            f"/api/brain/graph?limit={payload}",
            headers=_auth_headers(),
        )
        assert resp.status_code == 422


# ── /api/logs?level=... & ?module=... & ?search=... ────────────────────────


class TestLogsInjection:
    """SQL injection via log query params (logs use file parsing, not SQL)."""

    @pytest.mark.parametrize("payload", LEVEL_PAYLOADS)
    def test_level_injection_safe(self, client: TestClient, payload: str) -> None:
        """Malicious level param → no crash (logs are file-based, not SQL)."""
        resp = client.get(
            f"/api/logs?level={payload}",
            headers=_auth_headers(),
        )
        # Logs are parsed from files, not SQL. Should return 200 with no matches.
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data.get("entries"), list)

    @pytest.mark.parametrize(
        "payload",
        [
            "sovyx' OR '1'='1",
            "sovyx; DROP TABLE--",
            "'; DELETE FROM persons;--",
        ],
    )
    def test_module_injection_safe(self, client: TestClient, payload: str) -> None:
        resp = client.get(
            f"/api/logs?module={payload}",
            headers=_auth_headers(),
        )
        assert resp.status_code == 200

    @pytest.mark.parametrize(
        "payload",
        [
            "'; DROP TABLE conversations;--",
            "' UNION SELECT * FROM persons--",
        ],
    )
    def test_search_injection_safe(self, client: TestClient, payload: str) -> None:
        resp = client.get(
            f"/api/logs?search={payload}",
            headers=_auth_headers(),
        )
        assert resp.status_code == 200


# ── /api/settings (PUT body injection) ──────────────────────────────────────


class TestSettingsInjection:
    """SQL injection via settings JSON body."""

    @pytest.mark.parametrize(
        "body",
        [
            {"log_level": "'; DROP TABLE engine_state;--"},
            {"log_level": "INFO' OR '1'='1"},
            {"data_dir": "'; DELETE FROM persons;--"},
            {"__proto__": {"admin": True}},
            {"constructor": {"prototype": {"admin": True}}},
        ],
    )
    def test_settings_body_injection_safe(self, client: TestClient, body: dict) -> None:
        """Malicious settings body → handled safely (no SQL, config validation)."""
        resp = client.put(
            "/api/settings",
            json=body,
            headers=_auth_headers(),
        )
        # Should succeed (200) or fail validation — never 500
        assert resp.status_code in (200, 422)

    def test_non_dict_body_rejected(self, client: TestClient) -> None:
        """Array or string body → 422."""
        resp = client.put(
            "/api/settings",
            content="[1,2,3]",
            headers={"Content-Type": "application/json", **_auth_headers()},
        )
        assert resp.status_code == 422

    def test_invalid_json_body_rejected(self, client: TestClient) -> None:
        """Invalid JSON → 422."""
        resp = client.put(
            "/api/settings",
            content="{invalid json",
            headers={"Content-Type": "application/json", **_auth_headers()},
        )
        assert resp.status_code == 422


# ── /api/status ─────────────────────────────────────────────────────────────


class TestStatusInjection:
    """Status endpoint — no user input params, but test anyway."""

    def test_status_no_injection_vector(self, client: TestClient) -> None:
        """Status endpoint has no query params → always safe."""
        resp = client.get("/api/status", headers=_auth_headers())
        assert resp.status_code == 200


# ── Combined injection attempts ────────────────────────────────────────────


class TestCombinedInjection:
    """Multiple injection vectors combined."""

    def test_multiple_malicious_params(self, client: TestClient) -> None:
        """All params malicious at once."""
        resp = client.get(
            "/api/logs?level='; DROP TABLE--&module='; DELETE FROM--&search=' OR 1=1",
            headers=_auth_headers(),
        )
        assert resp.status_code == 200

    def test_unicode_injection(self, client: TestClient) -> None:
        """Unicode-based injection attempts."""
        resp = client.get(
            "/api/conversations/\u0027 OR \u00271\u0027=\u00271",
            headers=_auth_headers(),
        )
        assert resp.status_code in (200, 404)

    def test_null_byte_injection(self, client: TestClient) -> None:
        """Null byte injection attempt."""
        resp = client.get(
            "/api/conversations/test%00'; DROP TABLE--",
            headers=_auth_headers(),
        )
        assert resp.status_code in (200, 404)

    def test_url_encoded_injection(self, client: TestClient) -> None:
        """URL-encoded SQL injection."""
        resp = client.get(
            "/api/conversations/%27%20OR%20%271%27%3D%271",
            headers=_auth_headers(),
        )
        assert resp.status_code in (200, 404)

    def test_no_500_on_any_endpoint(self, client: TestClient) -> None:
        """Sweep all endpoints with worst payload — none returns 500."""
        worst = "'; DROP TABLE conversations; DELETE FROM persons; --"
        endpoints = [
            f"/api/conversations?limit={worst}",
            f"/api/conversations/{worst}",
            f"/api/brain/graph?limit={worst}",
            f"/api/logs?level={worst}",
            f"/api/logs?module={worst}",
            f"/api/logs?search={worst}",
        ]
        for endpoint in endpoints:
            resp = client.get(endpoint, headers=_auth_headers())
            assert resp.status_code != 500, f"500 on {endpoint}"
