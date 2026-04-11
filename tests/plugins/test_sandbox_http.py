"""Tests for Sovyx Plugin Sandbox HTTP — domain allowlist, rate limiting.

Coverage target: ≥95% on plugins/sandbox_http.py
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from sovyx.plugins.permissions import PermissionDeniedError
from sovyx.plugins.sandbox_http import (
    SandboxedHttpClient,
    _RateLimiter,
    _is_local_ip,
    _resolve_hostname,
)


# ── Local IP Detection ──────────────────────────────────────────────


class TestIsLocalIp:
    """Tests for _is_local_ip."""

    def test_loopback_v4(self) -> None:
        assert _is_local_ip("127.0.0.1") is True

    def test_loopback_v6(self) -> None:
        assert _is_local_ip("::1") is True

    def test_private_10(self) -> None:
        assert _is_local_ip("10.0.0.1") is True

    def test_private_172(self) -> None:
        assert _is_local_ip("172.16.0.1") is True

    def test_private_192(self) -> None:
        assert _is_local_ip("192.168.1.1") is True

    def test_link_local(self) -> None:
        assert _is_local_ip("169.254.1.1") is True

    def test_public_ip(self) -> None:
        assert _is_local_ip("8.8.8.8") is False

    def test_public_ip_2(self) -> None:
        assert _is_local_ip("1.1.1.1") is False

    def test_invalid_ip(self) -> None:
        """Invalid IP is blocked (safe default)."""
        assert _is_local_ip("not-an-ip") is True

    def test_ipv6_private(self) -> None:
        assert _is_local_ip("fd00::1") is True

    def test_ipv6_public(self) -> None:
        assert _is_local_ip("2001:4860:4860::8888") is False


# ── DNS Resolution ──────────────────────────────────────────────────


class TestResolveHostname:
    """Tests for _resolve_hostname."""

    def test_resolve_localhost(self) -> None:
        ip = _resolve_hostname("localhost")
        assert ip is not None
        assert _is_local_ip(ip) is True

    def test_resolve_nonexistent(self) -> None:
        ip = _resolve_hostname("this-domain-definitely-does-not-exist-xyz123.com")
        # May return None or a catch-all DNS IP
        # We just check it doesn't crash


# ── Rate Limiter ────────────────────────────────────────────────────


class TestRateLimiter:
    """Tests for _RateLimiter."""

    def test_allows_under_limit(self) -> None:
        limiter = _RateLimiter(max_calls=3)
        limiter.acquire()
        limiter.acquire()
        limiter.acquire()
        # 3 calls OK

    def test_blocks_over_limit(self) -> None:
        limiter = _RateLimiter(max_calls=2)
        limiter.acquire()
        limiter.acquire()
        with pytest.raises(PermissionDeniedError, match="Rate limit"):
            limiter.acquire()

    def test_remaining_count(self) -> None:
        limiter = _RateLimiter(max_calls=5)
        assert limiter.remaining == 5
        limiter.acquire()
        assert limiter.remaining == 4
        limiter.acquire()
        assert limiter.remaining == 3

    def test_window_expiry(self) -> None:
        """Old requests expire from the window."""
        limiter = _RateLimiter(max_calls=1, window_s=0.01)
        limiter.acquire()
        import time
        time.sleep(0.02)
        # Window expired, should work again
        limiter.acquire()


# ── URL Validation ──────────────────────────────────────────────────


class TestUrlValidation:
    """Tests for SandboxedHttpClient URL validation."""

    def test_allowed_domain_passes(self) -> None:
        client = SandboxedHttpClient("test", ["api.example.com"])
        # Should not raise
        client._validate_url("https://api.example.com/data")

    def test_disallowed_domain_blocked(self) -> None:
        client = SandboxedHttpClient("test", ["api.example.com"])
        with pytest.raises(PermissionDeniedError, match="not in allowed"):
            client._validate_url("https://evil.com/steal")

    def test_empty_allowlist_blocks_all(self) -> None:
        """Empty allowlist = no domains allowed."""
        client = SandboxedHttpClient("test", [])
        with pytest.raises(PermissionDeniedError, match="not in allowed"):
            client._validate_url("https://any-domain.com/")

    def test_no_allowlist_blocks_all(self) -> None:
        """None allowlist = no domains allowed."""
        client = SandboxedHttpClient("test", None)
        # None → empty set → blocks all
        with pytest.raises(PermissionDeniedError, match="not in allowed"):
            client._validate_url("https://any-domain.com/")

    def test_local_ip_blocked(self) -> None:
        client = SandboxedHttpClient("test", ["127.0.0.1"])
        with pytest.raises(PermissionDeniedError, match="local"):
            client._validate_url("http://127.0.0.1:8080/")

    def test_private_ip_blocked(self) -> None:
        client = SandboxedHttpClient("test", ["192.168.1.1"])
        with pytest.raises(PermissionDeniedError, match="local"):
            client._validate_url("http://192.168.1.1/")

    def test_allow_local_flag(self) -> None:
        """allow_local=True permits local network access."""
        client = SandboxedHttpClient("test", ["192.168.1.1"], allow_local=True)
        # Should not raise
        client._validate_url("http://192.168.1.1:8123/api")

    def test_invalid_url(self) -> None:
        client = SandboxedHttpClient("test", ["example.com"])
        with pytest.raises(PermissionDeniedError, match="Invalid URL"):
            client._validate_url("not-a-url")

    @patch("sovyx.plugins.sandbox_http._resolve_hostname", return_value="127.0.0.1")
    def test_dns_rebinding_blocked(self, _mock: object) -> None:
        """Domain that resolves to local IP is blocked."""
        client = SandboxedHttpClient("test", ["evil.com"])
        with pytest.raises(PermissionDeniedError, match="resolves to local"):
            client._validate_url("https://evil.com/steal")


# ── HTTP Requests ───────────────────────────────────────────────────


class TestHttpRequests:
    """Tests for actual HTTP request methods."""

    @pytest.mark.anyio()
    async def test_get_success(self) -> None:
        """GET request to allowed domain works."""
        client = SandboxedHttpClient("test", ["httpbin.org"])

        mock_response = httpx.Response(200, text='{"ok": true}')
        with patch.object(client._client, "request", new_callable=AsyncMock, return_value=mock_response):
            resp = await client.get("https://httpbin.org/get")
            assert resp.status_code == 200

        await client.close()

    @pytest.mark.anyio()
    async def test_post_success(self) -> None:
        """POST request to allowed domain works."""
        client = SandboxedHttpClient("test", ["api.example.com"])

        mock_response = httpx.Response(201, text="created")
        with patch.object(client._client, "request", new_callable=AsyncMock, return_value=mock_response):
            resp = await client.post("https://api.example.com/data", json={"key": "val"})
            assert resp.status_code == 201

        await client.close()

    @pytest.mark.anyio()
    async def test_rate_limit_enforced(self) -> None:
        """Rate limit blocks excess requests."""
        client = SandboxedHttpClient("test", ["api.example.com"], rate_limit=2)

        mock_response = httpx.Response(200)
        with patch.object(client._client, "request", new_callable=AsyncMock, return_value=mock_response):
            await client.get("https://api.example.com/1")
            await client.get("https://api.example.com/2")
            with pytest.raises(PermissionDeniedError, match="Rate limit"):
                await client.get("https://api.example.com/3")

        await client.close()

    @pytest.mark.anyio()
    async def test_response_size_warning(self) -> None:
        """Large response logs warning but doesn't block."""
        client = SandboxedHttpClient("test", ["api.example.com"], max_response_bytes=100)

        mock_response = httpx.Response(200, headers={"content-length": "999999"}, text="big")
        with patch.object(client._client, "request", new_callable=AsyncMock, return_value=mock_response):
            resp = await client.get("https://api.example.com/big")
            assert resp.status_code == 200  # Not blocked, just warned

        await client.close()

    @pytest.mark.anyio()
    async def test_context_manager(self) -> None:
        """Async context manager works."""
        async with SandboxedHttpClient("test", ["example.com"]) as client:
            assert client.remaining_requests > 0

    @pytest.mark.anyio()
    async def test_remaining_requests(self) -> None:
        """remaining_requests decreases with usage."""
        client = SandboxedHttpClient("test", ["api.example.com"], rate_limit=5)
        assert client.remaining_requests == 5

        mock_response = httpx.Response(200)
        with patch.object(client._client, "request", new_callable=AsyncMock, return_value=mock_response):
            await client.get("https://api.example.com/1")
        assert client.remaining_requests == 4

        await client.close()
