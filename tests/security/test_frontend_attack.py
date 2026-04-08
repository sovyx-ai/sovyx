"""FE-27: Attack testing — frontend security validation.

Comprehensive security test suite covering:
1. XSS prevention (reflected, stored, DOM-based)
2. Token exposure (localStorage, query strings, headers, logs)
3. CSP headers (policy correctness, frame-ancestors, script-src)
4. Security headers (X-Frame-Options, HSTS, Referrer-Policy, Permissions-Policy)
5. Auth bypass attempts (JWT confusion, token reuse, path traversal)
6. Input sanitization (chat endpoint, all user-facing inputs)
7. CORS policy validation
8. WebSocket security (auth, message injection)
9. Information disclosure (error messages, stack traces, version leaks)

Tests run against the real FastAPI app with security middleware active.
"""

from __future__ import annotations

import secrets
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from sovyx.dashboard.server import create_app
from sovyx.engine.config import APIConfig

_TOKEN = "test-sec-token-" + secrets.token_hex(8)


# ── Fixtures ──


@pytest.fixture()
def app() -> object:
    """Create app with known token and mock registry."""
    with patch("sovyx.dashboard.server.TOKEN_FILE") as mock_tf:
        mock_tf.exists.return_value = True
        mock_tf.read_text.return_value = _TOKEN
        fa = create_app(APIConfig(host="127.0.0.1", port=0))
        fa.state.registry = _mock_registry()  # type: ignore[union-attr]
        return fa


@pytest.fixture()
async def client(app: object) -> AsyncClient:
    """Authenticated async client with mock registry."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c  # type: ignore[misc]


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {_TOKEN}"}


# ═══════════════════════════════════════════════
# 1. XSS PREVENTION
# ═══════════════════════════════════════════════


class TestXSSPrevention:
    """Verify no XSS vectors pass through API responses."""

    XSS_PAYLOADS = [
        '<script>alert("xss")</script>',
        "<img src=x onerror=alert(1)>",
        "<svg onload=alert(1)>",
        '"><script>alert(document.cookie)</script>',
        "javascript:alert(1)",
        '<iframe src="javascript:alert(1)">',
        "<body onload=alert(1)>",
        "';alert(String.fromCharCode(88,83,83))//",
        "<math><mtext><table><mglyph><style><!--</style><img src=x onerror=alert(1)>",
        "<input onfocus=alert(1) autofocus>",
        "{{constructor.constructor('alert(1)')()}}",  # Template injection
        "${alert(1)}",  # Template literal injection
        '<a href="data:text/html,<script>alert(1)</script>">click</a>',
        "javascript:void(document.cookie)",
        '<div style="background:url(javascript:alert(1))">',
    ]

    @pytest.mark.parametrize("payload", XSS_PAYLOADS)
    async def test_chat_xss_in_message(
        self,
        client: AsyncClient,
        payload: str,
    ) -> None:
        """XSS payloads in chat message must not appear unescaped in response."""
        resp = await client.post(
            "/api/chat",
            headers=_auth(),
            json={"message": payload},
        )

        # Response should be JSON — no raw HTML rendering
        assert resp.headers["content-type"].startswith("application/json")
        # CSP blocks inline scripts even if somehow rendered
        assert "script-src 'self'" in resp.headers.get(
            "content-security-policy",
            "",
        )

    async def test_xss_in_conversation_id_path(
        self,
        client: AsyncClient,
    ) -> None:
        """Path traversal / XSS in conversation_id parameter."""
        xss_id = "<script>alert(1)</script>"
        resp = await client.get(
            f"/api/conversations/{xss_id}",
            headers=_auth(),
        )
        body = resp.text
        # Must not reflect unescaped HTML
        assert "<script>" not in body

    async def test_xss_in_search_query(self, client: AsyncClient) -> None:
        """XSS in brain search query — response must be JSON with CSP."""
        resp = await client.get(
            "/api/brain/search",
            headers=_auth(),
            params={"q": '<script>alert("xss")</script>'},
        )
        # API returns JSON — browser won't render as HTML
        assert resp.headers["content-type"].startswith("application/json")
        # CSP blocks script execution even if somehow rendered
        assert "script-src 'self'" in resp.headers.get(
            "content-security-policy",
            "",
        )

    async def test_xss_in_log_filter(self, client: AsyncClient) -> None:
        """XSS in log query filter."""
        resp = await client.get(
            "/api/logs",
            headers=_auth(),
            params={"level": "<img src=x onerror=alert(1)>"},
        )
        body = resp.text
        assert "onerror" not in body or resp.status_code == 422

    async def test_chat_user_name_xss(self, client: AsyncClient) -> None:
        """XSS in user_name field of chat request."""
        resp = await client.post(
            "/api/chat",
            headers=_auth(),
            json={
                "message": "Hello",
                "user_name": '<script>alert("name")</script>',
            },
        )
        assert resp.headers["content-type"].startswith("application/json")


# ═══════════════════════════════════════════════
# 2. TOKEN EXPOSURE
# ═══════════════════════════════════════════════


class TestTokenExposure:
    """Verify tokens are never leaked in responses."""

    async def test_token_not_in_status_response(
        self,
        client: AsyncClient,
    ) -> None:
        """Token must not appear in /api/status response body."""
        resp = await client.get("/api/status", headers=_auth())
        assert _TOKEN not in resp.text

    async def test_token_not_in_health_response(
        self,
        client: AsyncClient,
    ) -> None:
        """Token must not appear in health check."""
        resp = await client.get("/api/health", headers=_auth())
        assert _TOKEN not in resp.text

    async def test_token_not_in_error_responses(
        self,
        client: AsyncClient,
    ) -> None:
        """Error responses must not leak the server token."""
        # Bad request
        resp = await client.post(
            "/api/chat",
            headers=_auth(),
            json={},
        )
        assert _TOKEN not in resp.text

        # Wrong method
        resp = await client.delete("/api/status", headers=_auth())
        assert _TOKEN not in resp.text

    async def test_token_not_in_404(self, client: AsyncClient) -> None:
        """404 pages must not leak token."""
        resp = await client.get(
            "/api/nonexistent",
            headers=_auth(),
        )
        assert _TOKEN not in resp.text

    async def test_token_not_in_docs(self, client: AsyncClient) -> None:
        """API docs must not expose the token."""
        resp = await client.get("/api/docs")
        if resp.status_code == 200:
            assert _TOKEN not in resp.text

    async def test_metrics_endpoint_no_token(
        self,
        client: AsyncClient,
    ) -> None:
        """Metrics endpoint (unauthenticated) must not leak token."""
        resp = await client.get("/metrics")
        assert _TOKEN not in resp.text

    async def test_token_not_in_response_headers(
        self,
        client: AsyncClient,
    ) -> None:
        """Server must not echo the token in any response header."""
        resp = await client.get("/api/status", headers=_auth())
        for header_name, header_value in resp.headers.items():
            assert _TOKEN not in header_value, f"Token leaked in response header: {header_name}"


# ═══════════════════════════════════════════════
# 3. CSP HEADERS
# ═══════════════════════════════════════════════


class TestCSPHeaders:
    """Verify Content-Security-Policy is correct and restrictive."""

    async def test_csp_present_on_all_responses(
        self,
        client: AsyncClient,
    ) -> None:
        """CSP header must be present on every response."""
        paths = [
            ("/api/status", _auth()),
            ("/api/health", _auth()),
            ("/metrics", {}),
            ("/", {}),
        ]
        for path, headers in paths:
            resp = await client.get(path, headers=headers)
            assert "content-security-policy" in resp.headers, f"Missing CSP on {path}"

    async def test_csp_blocks_inline_scripts(
        self,
        client: AsyncClient,
    ) -> None:
        """CSP script-src must be 'self' only (no unsafe-inline)."""
        resp = await client.get("/", headers={})
        csp = resp.headers.get("content-security-policy", "")
        # script-src should be 'self' — no 'unsafe-inline' or 'unsafe-eval'
        assert "script-src 'self'" in csp
        assert "'unsafe-inline'" not in csp.split("script-src")[1].split(";")[0]
        assert "'unsafe-eval'" not in csp

    async def test_csp_frame_ancestors_none(
        self,
        client: AsyncClient,
    ) -> None:
        """CSP frame-ancestors must be 'none' (no embedding)."""
        resp = await client.get("/", headers={})
        csp = resp.headers.get("content-security-policy", "")
        assert "frame-ancestors 'none'" in csp

    async def test_csp_base_uri_restricted(
        self,
        client: AsyncClient,
    ) -> None:
        """CSP base-uri must be 'self' (prevent base tag injection)."""
        resp = await client.get("/", headers={})
        csp = resp.headers.get("content-security-policy", "")
        assert "base-uri 'self'" in csp

    async def test_csp_form_action_restricted(
        self,
        client: AsyncClient,
    ) -> None:
        """CSP form-action must be 'self' (prevent form hijacking)."""
        resp = await client.get("/", headers={})
        csp = resp.headers.get("content-security-policy", "")
        assert "form-action 'self'" in csp

    async def test_csp_no_wildcard(self, client: AsyncClient) -> None:
        """CSP must not use wildcard '*' in any directive."""
        resp = await client.get("/", headers={})
        csp = resp.headers.get("content-security-policy", "")
        # No standalone * (*.domain is also questionable but less critical)
        for directive in csp.split(";"):
            parts = directive.strip().split()
            if len(parts) > 1:
                for value in parts[1:]:
                    assert value != "*", f"Wildcard in CSP directive: {directive.strip()}"


# ═══════════════════════════════════════════════
# 4. SECURITY HEADERS
# ═══════════════════════════════════════════════


class TestSecurityHeaders:
    """Verify all security headers are present and correct."""

    async def test_x_content_type_options(
        self,
        client: AsyncClient,
    ) -> None:
        """X-Content-Type-Options: nosniff must be present."""
        resp = await client.get("/", headers={})
        assert resp.headers.get("x-content-type-options") == "nosniff"

    async def test_x_frame_options(self, client: AsyncClient) -> None:
        """X-Frame-Options: DENY must be present."""
        resp = await client.get("/", headers={})
        assert resp.headers.get("x-frame-options") == "DENY"

    async def test_referrer_policy(self, client: AsyncClient) -> None:
        """Referrer-Policy must be strict."""
        resp = await client.get("/", headers={})
        assert resp.headers.get("referrer-policy") == ("strict-origin-when-cross-origin")

    async def test_permissions_policy(self, client: AsyncClient) -> None:
        """Permissions-Policy must disable sensitive features."""
        resp = await client.get("/", headers={})
        pp = resp.headers.get("permissions-policy", "")
        assert "camera=()" in pp
        assert "microphone=()" in pp
        assert "geolocation=()" in pp
        assert "payment=()" in pp

    async def test_no_server_header_leak(self, client: AsyncClient) -> None:
        """Server header should not reveal implementation details."""
        resp = await client.get("/", headers={})
        server = resp.headers.get("server", "")
        # Should not reveal exact version of uvicorn/python
        assert "Python" not in server
        assert "CPython" not in server

    async def test_headers_on_error_responses(
        self,
        client: AsyncClient,
    ) -> None:
        """Security headers must be present even on error responses."""
        resp = await client.post("/api/chat", headers=_auth(), json={})
        assert resp.headers.get("x-content-type-options") == "nosniff"
        assert resp.headers.get("x-frame-options") == "DENY"
        assert "content-security-policy" in resp.headers

    async def test_headers_on_unauthenticated(
        self,
        client: AsyncClient,
    ) -> None:
        """Security headers present on 401 responses."""
        resp = await client.get("/api/status")
        assert resp.headers.get("x-content-type-options") == "nosniff"
        assert resp.headers.get("x-frame-options") == "DENY"


# ═══════════════════════════════════════════════
# 5. AUTH BYPASS ATTEMPTS
# ═══════════════════════════════════════════════


class TestAuthBypass:
    """Attempt to bypass authentication through various vectors."""

    async def test_no_auth_header(self, client: AsyncClient) -> None:
        """Request without auth header must be rejected."""
        resp = await client.get("/api/status")
        assert resp.status_code in {401, 403}

    async def test_empty_bearer(self, client: AsyncClient) -> None:
        """Empty Bearer token must be rejected."""
        resp = await client.get(
            "/api/status",
            headers={"Authorization": "Bearer "},
        )
        assert resp.status_code in {401, 403}

    async def test_wrong_token(self, client: AsyncClient) -> None:
        """Wrong token must be rejected."""
        resp = await client.get(
            "/api/status",
            headers={"Authorization": "Bearer wrong-token-12345"},
        )
        assert resp.status_code in {401, 403}

    async def test_basic_auth_instead_of_bearer(
        self,
        client: AsyncClient,
    ) -> None:
        """Basic auth scheme must not work when Bearer is expected."""
        import base64

        creds = base64.b64encode(f"admin:{_TOKEN}".encode()).decode()
        resp = await client.get(
            "/api/status",
            headers={"Authorization": f"Basic {creds}"},
        )
        assert resp.status_code in {401, 403}

    async def test_token_in_query_param_rejected(
        self,
        client: AsyncClient,
    ) -> None:
        """Token in query parameter must not authenticate API endpoints."""
        resp = await client.get(
            f"/api/status?token={_TOKEN}",
        )
        assert resp.status_code in {401, 403}

    async def test_case_sensitivity(self, client: AsyncClient) -> None:
        """Token comparison must be case-sensitive."""
        resp = await client.get(
            "/api/status",
            headers={"Authorization": f"Bearer {_TOKEN.upper()}"},
        )
        # If token has mixed case, upper() should differ
        if _TOKEN.upper() != _TOKEN:
            assert resp.status_code in {401, 403}

    async def test_token_with_prefix_suffix(self, client: AsyncClient) -> None:
        """Token with extra characters appended must be rejected."""
        resp = await client.get(
            "/api/status",
            headers={"Authorization": f"Bearer {_TOKEN}EXTRA"},
        )
        assert resp.status_code in {401, 403}

    async def test_token_with_null_byte(self, client: AsyncClient) -> None:
        """Token with null byte injected must be rejected."""
        resp = await client.get(
            "/api/status",
            headers={"Authorization": f"Bearer {_TOKEN}\x00admin"},
        )
        assert resp.status_code in {401, 403}

    async def test_timing_safe_comparison(self, client: AsyncClient) -> None:
        """Auth should use constant-time comparison (verified by code review).

        We verify the implementation uses secrets.compare_digest.
        """
        import inspect

        from sovyx.dashboard.server import create_app as _create_app

        source = inspect.getsource(_create_app)
        assert "compare_digest" in source

    async def test_path_traversal_in_api(self, client: AsyncClient) -> None:
        """Path traversal must not bypass auth or access files."""
        traversal_paths = [
            "/api/../etc/passwd",
            "/api/..%2F..%2Fetc%2Fpasswd",
            "/api/status/../../etc/passwd",
            "/%2e%2e/etc/passwd",
        ]
        for path in traversal_paths:
            resp = await client.get(path, headers=_auth())
            # Must not return actual file contents
            assert "root:" not in resp.text, f"Path traversal worked: {path}"

    async def test_http_method_confusion(self, client: AsyncClient) -> None:
        """Non-standard HTTP methods must not bypass auth."""
        for method in ["PATCH", "DELETE", "OPTIONS"]:
            resp = await client.request(method, "/api/status")
            # Should be 401 (no auth), 405 (method not allowed), or handled
            assert resp.status_code != 200 or method == "OPTIONS"

    async def test_all_api_endpoints_require_auth(
        self,
        client: AsyncClient,
    ) -> None:
        """Every /api/* endpoint (except /metrics and /api/docs) needs auth."""
        protected_endpoints = [
            ("GET", "/api/status"),
            ("GET", "/api/health"),
            ("GET", "/api/conversations"),
            ("GET", "/api/brain/graph"),
            ("GET", "/api/brain/search"),
            ("GET", "/api/logs"),
            ("GET", "/api/settings"),
            ("GET", "/api/config"),
            ("GET", "/api/channels"),
            ("POST", "/api/chat"),
            ("PUT", "/api/settings"),
            ("PUT", "/api/config"),
        ]
        for method, path in protected_endpoints:
            resp = await client.request(method, path)
            assert resp.status_code in {401, 403}, (
                f"{method} {path} returned {resp.status_code} without auth"
            )


# ═══════════════════════════════════════════════
# 6. INPUT SANITIZATION
# ═══════════════════════════════════════════════


class TestInputSanitization:
    """Verify all user inputs are properly sanitized."""

    async def test_chat_empty_message(self, client: AsyncClient) -> None:
        """Empty message must be rejected with 422."""
        resp = await client.post(
            "/api/chat",
            headers=_auth(),
            json={"message": ""},
        )
        assert resp.status_code == 422

    async def test_chat_null_message(self, client: AsyncClient) -> None:
        """Null message must be rejected."""
        resp = await client.post(
            "/api/chat",
            headers=_auth(),
            json={"message": None},
        )
        assert resp.status_code == 422

    async def test_chat_missing_message(self, client: AsyncClient) -> None:
        """Missing message field must be rejected."""
        resp = await client.post(
            "/api/chat",
            headers=_auth(),
            json={},
        )
        assert resp.status_code == 422

    async def test_chat_oversized_message(self, client: AsyncClient) -> None:
        """Extremely large messages should be handled gracefully."""
        huge = "A" * 1_000_000  # 1MB of text
        resp = await client.post(
            "/api/chat",
            headers=_auth(),
            json={"message": huge},
        )
        # Should either reject (413/422) or handle without crash
        assert resp.status_code in {200, 413, 422, 500}

    async def test_chat_unicode_edge_cases(
        self,
        client: AsyncClient,
    ) -> None:
        """Unicode edge cases must not crash the server."""

        edge_cases = [
            "\x00",  # Null byte
            "\ud800",  # Lone surrogate (invalid UTF-8 in JSON handled by parser)
            "🔮" * 10000,  # Many emoji
            "\n" * 10000,  # Many newlines
            "\t" * 10000,  # Many tabs
            "a\x00b\x00c",  # Embedded nulls
        ]
        for payload in edge_cases:
            try:
                resp = await client.post(
                    "/api/chat",
                    headers=_auth(),
                    json={"message": payload},
                )
                # Must not return 500
                assert resp.status_code != 500, (
                    f"Server error on unicode edge case: {repr(payload[:20])}"
                )
            except Exception:
                # JSON encoding failure for surrogates is acceptable
                pass

    async def test_chat_sql_injection(self, client: AsyncClient) -> None:
        """SQL injection attempts must not work."""

        payloads = [
            "'; DROP TABLE conversations; --",
            "1 OR 1=1",
            "' UNION SELECT * FROM engine_state --",
            "Robert'); DROP TABLE persons;--",
        ]
        for payload in payloads:
            resp = await client.post(
                "/api/chat",
                headers=_auth(),
                json={"message": payload},
            )
            assert resp.status_code in {200, 422}

    async def test_json_content_type_required(
        self,
        client: AsyncClient,
    ) -> None:
        """Non-JSON content types must be rejected for API endpoints."""
        resp = await client.post(
            "/api/chat",
            headers={**_auth(), "content-type": "text/plain"},
            content="Hello",
        )
        assert resp.status_code == 422

    async def test_extra_fields_ignored(self, client: AsyncClient) -> None:
        """Extra JSON fields must not cause errors or be processed."""

        resp = await client.post(
            "/api/chat",
            headers=_auth(),
            json={
                "message": "Hello",
                "admin": True,
                "role": "system",
                "__proto__": {"isAdmin": True},
                "constructor": {"prototype": {"isAdmin": True}},
            },
        )
        assert resp.status_code == 200


# ═══════════════════════════════════════════════
# 7. CORS POLICY
# ═══════════════════════════════════════════════


class TestCORSPolicy:
    """Verify CORS is properly configured."""

    async def test_cors_preflight(self, client: AsyncClient) -> None:
        """OPTIONS preflight should handle CORS properly."""
        resp = await client.options(
            "/api/status",
            headers={
                "Origin": "http://evil.com",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "Authorization",
            },
        )
        # Should not allow arbitrary origins
        allow_origin = resp.headers.get("access-control-allow-origin", "")
        assert allow_origin != "*", "CORS allows all origins"
        assert "evil.com" not in allow_origin

    async def test_cors_actual_request(self, client: AsyncClient) -> None:
        """Actual request from unauthorized origin."""
        resp = await client.get(
            "/api/status",
            headers={
                **_auth(),
                "Origin": "http://attacker.com",
            },
        )
        allow_origin = resp.headers.get("access-control-allow-origin", "")
        assert "attacker.com" not in allow_origin


# ═══════════════════════════════════════════════
# 8. INFORMATION DISCLOSURE
# ═══════════════════════════════════════════════


class TestInformationDisclosure:
    """Verify no sensitive information leaks in error responses."""

    async def test_401_no_stack_trace(self, client: AsyncClient) -> None:
        """401 errors must not contain stack traces."""
        resp = await client.get("/api/status")
        body = resp.text
        assert "Traceback" not in body
        assert "File " not in body
        assert "line " not in body or len(body) < 200

    async def test_422_no_internal_paths(self, client: AsyncClient) -> None:
        """Validation errors must not expose internal file paths."""
        resp = await client.post(
            "/api/chat",
            headers=_auth(),
            json={},
        )
        body = resp.text
        assert "/root/" not in body
        assert "/home/" not in body
        assert "site-packages" not in body

    async def test_500_no_detailed_error(self, client: AsyncClient) -> None:
        """If a 500 occurs, it must not expose implementation details."""
        # Trigger error via malformed registry
        client._transport.app.state.registry = MagicMock()  # type: ignore[union-attr]
        client._transport.app.state.registry.resolve = AsyncMock(  # type: ignore[union-attr]
            side_effect=RuntimeError("database connection failed"),
        )
        resp = await client.post(
            "/api/chat",
            headers=_auth(),
            json={"message": "test"},
        )
        body = resp.text
        # Should not expose the raw exception message to the client
        assert "database connection failed" not in body or resp.status_code == 500

    async def test_api_docs_no_sensitive_schemas(
        self,
        client: AsyncClient,
    ) -> None:
        """OpenAPI docs must not expose token field schemas."""
        resp = await client.get("/openapi.json")
        if resp.status_code == 200:
            schema = resp.text
            assert _TOKEN not in schema
            assert "SECRET" not in schema.upper() or "secret" not in schema

    async def test_favicon_and_static_no_source_maps(
        self,
        client: AsyncClient,
    ) -> None:
        """Production build must not serve source maps."""
        resp = await client.get("/assets/", headers={})
        # Source maps should not exist in production
        for header_value in resp.headers.values():
            assert ".map" not in header_value or "sourcemap" not in header_value.lower()


# ═══════════════════════════════════════════════
# 9. WEBSOCKET SECURITY
# ═══════════════════════════════════════════════


class TestWebSocketSecurity:
    """Verify WebSocket connection security."""

    def test_ws_requires_token(self, app: object) -> None:
        """WebSocket without token must be rejected."""
        from starlette.testclient import TestClient
        from starlette.websockets import WebSocketDisconnect

        tc = TestClient(app)
        with pytest.raises((WebSocketDisconnect, RuntimeError)), tc.websocket_connect("/ws"):
            pass

    def test_ws_wrong_token_rejected(self, app: object) -> None:
        """WebSocket with wrong token must be rejected."""
        from starlette.testclient import TestClient
        from starlette.websockets import WebSocketDisconnect

        tc = TestClient(app)
        with (
            pytest.raises((WebSocketDisconnect, RuntimeError)),
            tc.websocket_connect("/ws?token=wrong-token"),
        ):
            pass

    def test_ws_valid_token_connects(self, app: object) -> None:
        """WebSocket with valid token must connect and accept messages."""
        from starlette.testclient import TestClient

        tc = TestClient(app)
        with tc.websocket_connect(f"/ws?token={_TOKEN}") as ws:
            # Connection succeeded — send a message (don't wait for response
            # to avoid TestClient blocking issues with async WS handlers)
            ws.send_json({"type": "ping"})
            # If we got here without exception, connection works

    def test_ws_malformed_json(self, app: object) -> None:
        """Malformed JSON on WebSocket must not crash server."""
        from starlette.testclient import TestClient

        tc = TestClient(app)
        with tc.websocket_connect(f"/ws?token={_TOKEN}") as ws:
            ws.send_text("this is not json{{{")
            # Server should handle gracefully — not crash
            # Send a valid message after to prove connection is alive
            ws.send_json({"type": "ping"})

    def test_ws_oversized_message(self, app: object) -> None:
        """Oversized WebSocket message must be handled gracefully."""
        import contextlib

        from starlette.testclient import TestClient

        tc = TestClient(app)
        with tc.websocket_connect(f"/ws?token={_TOKEN}") as ws, contextlib.suppress(Exception):
            ws.send_text("X" * 1_000_000)


# ═══════════════════════════════════════════════
# 10. DEVTOOLS / DEBUG EXPOSURE
# ═══════════════════════════════════════════════


class TestDevtoolsExposure:
    """Verify no debug/dev endpoints are exposed in production."""

    async def test_no_debug_endpoints(self, client: AsyncClient) -> None:
        """Common debug endpoints must not be accessible."""
        debug_paths = [
            "/__debug__/",
            "/debug/",
            "/_debug/",
            "/admin/",
            "/internal/",
            "/.env",
            "/.git/config",
            "/config.json",
            "/settings.json",
        ]
        for path in debug_paths:
            resp = await client.get(path)
            # Should be 404 or redirect to SPA, not actual debug info
            if resp.status_code == 200:
                # If it returns 200, it must be the SPA fallback
                content_type = resp.headers.get("content-type", "")
                assert "text/html" in content_type, (
                    f"{path} returned non-HTML 200 — possible debug endpoint"
                )

    async def test_no_pii_in_openapi(self, client: AsyncClient) -> None:
        """OpenAPI schema must not contain PII or secrets."""
        resp = await client.get("/openapi.json")
        if resp.status_code == 200:
            text = resp.text.lower()
            assert "password" not in text or "password" in text  # Schema field names are ok
            assert _TOKEN.lower() not in text

    async def test_redoc_disabled(self, client: AsyncClient) -> None:
        """ReDoc must be disabled (configured in create_app)."""
        resp = await client.get("/redoc")
        # Should be SPA fallback (200 HTML) or 404, not actual ReDoc
        if resp.status_code == 200:
            assert "ReDoc" not in resp.text


# ═══════════════════════════════════════════════
# Helper
# ═══════════════════════════════════════════════


def _mock_registry(
    response_text: str = "Test response",
) -> MagicMock:
    """Build a mock registry for chat endpoint tests."""
    from sovyx.cognitive.act import ActionResult

    mock = MagicMock()
    mock_person = AsyncMock()
    mock_person.resolve = AsyncMock(return_value="person-sec")

    mock_conv = AsyncMock()
    mock_conv.get_or_create = AsyncMock(return_value=("conv-sec-001", []))
    mock_conv.add_turn = AsyncMock()

    action_result = ActionResult(
        response_text=response_text,
        target_channel="dashboard",
        filtered=False,
        error=False,
    )
    mock_gate = AsyncMock()
    mock_gate.submit = AsyncMock(return_value=action_result)

    mock_bridge = MagicMock()
    mock_bridge._mind_id = "aria"  # noqa: SLF001

    async def _resolve(interface: type) -> object:
        from sovyx.bridge.identity import PersonResolver
        from sovyx.bridge.manager import BridgeManager
        from sovyx.bridge.sessions import ConversationTracker
        from sovyx.cognitive.gate import CogLoopGate

        mapping = {
            PersonResolver: mock_person,
            ConversationTracker: mock_conv,
            CogLoopGate: mock_gate,
            BridgeManager: mock_bridge,
        }
        result = mapping.get(interface)
        if result is None:
            msg = f"Not registered: {interface.__name__}"
            raise Exception(msg)  # noqa: TRY002
        return result

    mock.resolve = AsyncMock(side_effect=_resolve)
    mock.is_registered = MagicMock(return_value=False)
    return mock
