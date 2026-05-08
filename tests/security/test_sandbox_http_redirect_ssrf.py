"""SSRF hardening tests — Plugin Sandbox C1 (Round 3 paranoid audit).

Closes the ``follow_redirects=True`` SSRF bypass identified in v0.32.0:
``SandboxedHttpClient`` previously let httpx auto-follow 30x redirects
without re-running ``_validate_url``, so an attacker-controlled
allowlisted domain could 302 → ``http://169.254.169.254/`` (AWS
metadata) or any internal IP. The fix walks redirects manually and
validates EVERY hop's URL through the sandbox.

Each test mocks ``httpx.AsyncClient.request`` so the HTTP layer is
exercised end-to-end without real network egress.

Coverage:

* test_redirect_to_private_ip_rejected
* test_redirect_to_localhost_rejected
* test_redirect_chain_each_hop_validated
* test_legitimate_redirect_followed
* test_max_redirects_enforced
* test_redirect_method_downgrade_for_302
* test_redirect_method_preserved_for_307
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from sovyx.plugins.permissions import PermissionDeniedError
from sovyx.plugins.sandbox_http import SandboxedHttpClient


def _redirect_response(status: int, location: str) -> httpx.Response:
    """Build a minimal redirect response with the given Location header."""
    return httpx.Response(status_code=status, headers={"location": location})


def _ok_response(status: int = 200, *, text: str = "ok") -> httpx.Response:
    """Build a non-redirect terminal response."""
    return httpx.Response(status_code=status, text=text)


class _SequenceResponder:
    """Helper that returns a queued response per call + records args.

    Used as the ``side_effect`` of an :class:`AsyncMock` patched onto
    ``httpx.AsyncClient.request``. ``AsyncMock`` calls the side_effect
    *synchronously* and awaits the AsyncMock's own return path, so the
    ``__call__`` here is intentionally NOT a coroutine — it returns the
    response object directly. Each call pops the next response off the
    queue and records the ``(method, url, kwargs)`` tuple for later
    assertion.
    """

    def __init__(self, responses: list[httpx.Response]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def __call__(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append((method, url, dict(kwargs)))
        if not self._responses:
            raise AssertionError(
                f"_SequenceResponder ran out of queued responses (call #{len(self.calls)})"
            )
        return self._responses.pop(0)


# ── Core SSRF rejection ─────────────────────────────────────────────


class TestRedirectSsrfRejection:
    """Every redirect hop MUST re-enter ``_validate_url``."""

    @pytest.mark.anyio()
    async def test_redirect_to_private_ip_rejected(self) -> None:
        """302 → AWS metadata IP raises before the next request fires.

        Reproduces the audit's Plugin Sandbox C1 attack: attacker
        controls ``http://attacker.example.com/`` (allowlisted) and
        returns ``302 Location: http://169.254.169.254/latest/meta-data/``.
        """
        client = SandboxedHttpClient(
            "test.fetch",
            allowed_domains=None,
            allow_any_domain=True,
        )
        responder = _SequenceResponder(
            [_redirect_response(302, "http://169.254.169.254/latest/meta-data/")]
        )
        try:
            with (
                patch.object(
                    client._client, "request", new_callable=AsyncMock, side_effect=responder
                ),
                pytest.raises(PermissionDeniedError, match="redirect to unsafe URL"),
            ):
                await client.get("http://attacker.example.com/")
            # CRITICAL: only the FIRST request was issued — the metadata
            # endpoint was NEVER contacted.
            assert len(responder.calls) == 1
            assert responder.calls[0][1] == "http://attacker.example.com/"
        finally:
            await client.close()

    @pytest.mark.anyio()
    async def test_redirect_to_localhost_rejected(self) -> None:
        """302 → 127.0.0.1 raises before the next request fires."""
        client = SandboxedHttpClient(
            "test.fetch",
            allowed_domains=None,
            allow_any_domain=True,
        )
        responder = _SequenceResponder([_redirect_response(302, "http://127.0.0.1:8080/")])
        try:
            with (
                patch.object(
                    client._client, "request", new_callable=AsyncMock, side_effect=responder
                ),
                pytest.raises(PermissionDeniedError, match="redirect to unsafe URL"),
            ):
                await client.get("http://attacker.example.com/")
            assert len(responder.calls) == 1
        finally:
            await client.close()

    @pytest.mark.anyio()
    async def test_redirect_chain_each_hop_validated(self) -> None:
        """Multi-hop chain: each public hop OK, final internal hop rejected.

        Ensures the validator runs on EVERY hop, not just the first or
        last — an attacker chaining ``allowlisted → public → public →
        internal`` should still be blocked at the moment the internal
        hop is proposed.
        """
        client = SandboxedHttpClient(
            "test.fetch",
            allowed_domains=None,
            allow_any_domain=True,
        )
        responder = _SequenceResponder(
            [
                _redirect_response(302, "http://hop2.example.com/"),
                _redirect_response(302, "http://hop3.example.com/"),
                _redirect_response(302, "http://10.0.0.5/admin"),
            ]
        )
        try:
            with (
                patch.object(
                    client._client, "request", new_callable=AsyncMock, side_effect=responder
                ),
                pytest.raises(PermissionDeniedError, match="redirect to unsafe URL"),
            ):
                await client.get("http://attacker.example.com/")
            # Three public hops fired, the fourth (to 10.0.0.5) was
            # blocked BEFORE the request was issued.
            assert len(responder.calls) == 3
            assert responder.calls[0][1] == "http://attacker.example.com/"
            assert responder.calls[1][1] == "http://hop2.example.com/"
            assert responder.calls[2][1] == "http://hop3.example.com/"
        finally:
            await client.close()


# ── Legitimate redirects + bounds ──────────────────────────────────


class TestRedirectLegitimate:
    """Sandbox MUST still follow legitimate public-to-public redirects."""

    @pytest.mark.anyio()
    async def test_legitimate_redirect_followed(self) -> None:
        """Public 302 → public, both pass validation, terminal response returned."""
        client = SandboxedHttpClient(
            "test.fetch",
            allowed_domains=None,
            allow_any_domain=True,
        )
        responder = _SequenceResponder(
            [
                _redirect_response(302, "http://final.example.com/page"),
                _ok_response(200, text="hello"),
            ]
        )
        try:
            with patch.object(
                client._client, "request", new_callable=AsyncMock, side_effect=responder
            ):
                resp = await client.get("http://start.example.com/")
            assert resp.status_code == 200
            assert resp.text == "hello"
            assert len(responder.calls) == 2
            assert responder.calls[1][1] == "http://final.example.com/page"
        finally:
            await client.close()

    @pytest.mark.anyio()
    async def test_max_redirects_enforced(self) -> None:
        """Infinite-redirect loop is bounded at _MAX_REDIRECTS hops."""
        client = SandboxedHttpClient(
            "test.fetch",
            allowed_domains=None,
            allow_any_domain=True,
        )
        # 7 redirect responses available; hop limit is 5 so the 6th
        # response is the "max redirects" trigger.
        responder = _SequenceResponder(
            [_redirect_response(302, "http://loop.example.com/") for _ in range(10)]
        )
        try:
            with (
                patch.object(
                    client._client, "request", new_callable=AsyncMock, side_effect=responder
                ),
                pytest.raises(PermissionDeniedError, match="max redirects"),
            ):
                await client.get("http://loop.example.com/")
            # 1 initial + 5 follow-ups = 6 actual requests, then the
            # 7th proposed hop trips the cap.
            assert len(responder.calls) == 6
        finally:
            await client.close()


# ── Method semantics on redirect ───────────────────────────────────


class TestRedirectMethodSemantics:
    """Method downgrade matches Python ``requests`` library."""

    @pytest.mark.anyio()
    async def test_redirect_method_downgrade_for_302(self) -> None:
        """POST → 302 → GET (body + body headers stripped).

        Defends against the POST-allowlisted-then-302 attack pattern.
        """
        client = SandboxedHttpClient(
            "test.fetch",
            allowed_domains=None,
            allow_any_domain=True,
        )
        responder = _SequenceResponder(
            [
                _redirect_response(302, "http://final.example.com/safe"),
                _ok_response(200, text="downgraded"),
            ]
        )
        try:
            with patch.object(
                client._client, "request", new_callable=AsyncMock, side_effect=responder
            ):
                resp = await client.post(
                    "http://start.example.com/",
                    json={"secret": "value"},
                    headers={"Content-Type": "application/json", "X-Trace": "keep"},
                )
            assert resp.status_code == 200
            assert len(responder.calls) == 2
            initial_method, _, initial_kwargs = responder.calls[0]
            redirect_method, redirect_url, redirect_kwargs = responder.calls[1]

            assert initial_method == "POST"
            assert "json" in initial_kwargs

            # Method downgraded to GET.
            assert redirect_method == "GET"
            assert redirect_url == "http://final.example.com/safe"
            # Body stripped.
            assert "json" not in redirect_kwargs
            assert "data" not in redirect_kwargs
            assert "content" not in redirect_kwargs
            # Body-describing headers stripped.
            stripped_headers = redirect_kwargs.get("headers", {})
            assert isinstance(stripped_headers, dict)
            lowered = {str(k).lower() for k in stripped_headers}
            assert "content-type" not in lowered
            assert "content-length" not in lowered
            # Non-body headers kept.
            assert "X-Trace" in stripped_headers
        finally:
            await client.close()

    @pytest.mark.anyio()
    async def test_redirect_method_preserved_for_307(self) -> None:
        """POST → 307 → POST (method + body preserved per RFC 7538)."""
        client = SandboxedHttpClient(
            "test.fetch",
            allowed_domains=None,
            allow_any_domain=True,
        )
        responder = _SequenceResponder(
            [
                _redirect_response(307, "http://final.example.com/safe"),
                _ok_response(200, text="preserved"),
            ]
        )
        try:
            with patch.object(
                client._client, "request", new_callable=AsyncMock, side_effect=responder
            ):
                resp = await client.post(
                    "http://start.example.com/",
                    json={"payload": "kept"},
                )
            assert resp.status_code == 200
            assert len(responder.calls) == 2
            redirect_method, _, redirect_kwargs = responder.calls[1]
            # Method + body preserved.
            assert redirect_method == "POST"
            assert redirect_kwargs.get("json") == {"payload": "kept"}
        finally:
            await client.close()


# ── Belt-and-suspenders: client construction ───────────────────────


class TestClientConstruction:
    """The httpx client must be built with follow_redirects=False."""

    def test_follow_redirects_disabled(self) -> None:
        """SSRF closure depends on ``follow_redirects=False`` — assert it.

        A future refactor that re-enables auto-follow reintroduces the
        v0.32.0 SSRF bypass. This test pins the construction invariant
        so that mistake is caught at unit-test time, not in production.
        """
        client = SandboxedHttpClient("guard", ["example.com"])
        # httpx exposes the configured value as ``follow_redirects`` on
        # the client instance.
        assert client._client.follow_redirects is False


# ── Coverage of public ``request`` and unmocked AsyncClient flow ────


class TestPublicRequestEntrypoint:
    """``client.request(method, ...)`` also walks the redirect chain."""

    @pytest.mark.anyio()
    async def test_request_method_redirect_validated(self) -> None:
        """Plugins using arbitrary verbs (PROPFIND, …) get the same guard."""
        client = SandboxedHttpClient("caldav.test", ["caldav.example.com"])
        responder = _SequenceResponder([_redirect_response(302, "http://192.168.1.1/internal")])
        try:
            with (
                patch.object(
                    client._client, "request", new_callable=AsyncMock, side_effect=responder
                ),
                pytest.raises(PermissionDeniedError, match="redirect to unsafe URL"),
            ):
                await client.request("PROPFIND", "https://caldav.example.com/dav/")
            assert len(responder.calls) == 1
        finally:
            await client.close()


# ── Sanity: module exports the AsyncMock-friendly API ──────────────


class TestSequenceResponderHarness:
    """Smoke-test the test harness itself — fail-fast on broken mocks."""

    @pytest.mark.anyio()
    async def test_responder_records_calls(self) -> None:
        responder = _SequenceResponder([_ok_response()])
        # When wired into AsyncMock as side_effect, AsyncMock calls the
        # synchronous responder, captures its return value, and awaits
        # the wrapping coroutine itself. This mirrors the real wiring in
        # the SSRF tests above.
        mock = AsyncMock(side_effect=responder)
        result = await mock("GET", "https://x/", headers={"a": "b"})
        assert result.status_code == 200
        assert responder.calls == [("GET", "https://x/", {"headers": {"a": "b"}})]
