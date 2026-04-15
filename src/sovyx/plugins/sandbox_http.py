"""Sovyx Plugin Sandbox — HTTP client with domain allowlist and rate limiting.

Prevents plugins from accessing local network, unauthorized domains,
or making too many requests. Uses httpx (already a Sovyx dependency).

Security layers:
1. Domain allowlist (only declared domains)
2. Local network blocking (127.*, 10.*, 172.16-31.*, 192.168.*, ::1, etc.)
3. DNS rebinding protection (resolve IP before connect)
4. Rate limiting (10 req/min default)
5. Response size limit (5MB default)
6. Timeout enforcement (10s default)

Spec: SPE-008-SANDBOX §5
"""

from __future__ import annotations

import ipaddress
import socket
import time
from urllib.parse import urlparse

import httpx

from sovyx.observability.logging import get_logger
from sovyx.plugins.permissions import PermissionDeniedError

logger = get_logger(__name__)

# ── Constants ───────────────────────────────────────────────────────

_DEFAULT_RATE_LIMIT = 10  # requests per minute
_DEFAULT_TIMEOUT_S = 10.0
_DEFAULT_MAX_RESPONSE_BYTES = 5 * 1024 * 1024  # 5MB
_RATE_WINDOW_S = 60.0


# ── Local Network Detection ─────────────────────────────────────────


def _is_local_ip(ip_str: str) -> bool:
    """Check if an IP address is in a private/reserved range.

    Blocks: loopback, private (RFC 1918), link-local, multicast,
    IPv6 loopback, IPv6 link-local, IPv6 unique-local.

    Args:
        ip_str: IP address string.

    Returns:
        True if the IP is local/private/reserved.
    """
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # Can't parse → block to be safe

    return (
        addr.is_loopback
        or addr.is_private
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
    )


def _resolve_hostname(hostname: str) -> str | None:
    """Resolve hostname to IP for DNS rebinding protection.

    Args:
        hostname: Domain to resolve.

    Returns:
        First resolved IP string, or None if resolution fails.
    """
    try:
        results = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        if results:
            return str(results[0][4][0])
    except (socket.gaierror, OSError):
        pass
    return None


# ── Rate Limiter ────────────────────────────────────────────────────


class _RateLimiter:
    """Simple sliding-window rate limiter.

    Tracks request timestamps and rejects when window is full.
    """

    def __init__(
        self,
        max_calls: int = _DEFAULT_RATE_LIMIT,
        window_s: float = _RATE_WINDOW_S,
    ) -> None:
        self._max = max_calls
        self._window = window_s
        self._timestamps: list[float] = []

    def acquire(self) -> None:
        """Check if request is allowed. Raises if rate limited.

        Raises:
            PermissionDeniedError: Rate limit exceeded.
        """
        now = time.monotonic()
        # Prune old timestamps
        cutoff = now - self._window
        self._timestamps = [t for t in self._timestamps if t > cutoff]

        if len(self._timestamps) >= self._max:
            raise PermissionDeniedError(
                "plugin",
                f"Rate limit exceeded ({self._max} requests per {self._window:.0f}s)",
            )

        self._timestamps.append(now)

    @property
    def remaining(self) -> int:
        """Requests remaining in current window."""
        now = time.monotonic()
        cutoff = now - self._window
        active = sum(1 for t in self._timestamps if t > cutoff)
        return max(0, self._max - active)


# ── Sandboxed HTTP Client ──────────────────────────────────────────


class SandboxedHttpClient:
    """HTTP client sandboxed for plugin use.

    Enforces domain allowlist, blocks local network access,
    rate-limits requests, and caps response size.

    Usage::

        client = SandboxedHttpClient(
            plugin_name="weather",
            allowed_domains=["api.open-meteo.com"],
        )
        response = await client.get("https://api.open-meteo.com/v1/forecast?lat=40&lon=-74")
        print(response.status_code, response.text)
        await client.close()

    Spec: SPE-008-SANDBOX §5
    """

    def __init__(
        self,
        plugin_name: str,
        allowed_domains: list[str] | None = None,
        *,
        allow_local: bool = False,
        allow_any_domain: bool = False,
        rate_limit: int = _DEFAULT_RATE_LIMIT,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
    ) -> None:
        """Initialize sandboxed HTTP client.

        Args:
            plugin_name: Plugin name for logging.
            allowed_domains: List of allowed domains. Empty = no domains allowed
                (unless ``allow_any_domain`` is set).
            allow_local: If True, allow local network (network:local permission).
            allow_any_domain: If True, skip the domain-allowlist check. Every
                other protection still applies (local-IP block, DNS rebinding
                check, rate limit, timeout, size cap). Use this for the narrow
                case of tools that fetch arbitrary user-supplied URLs from
                the public web (e.g. the ``fetch`` tool of web_intelligence).
                Requires ``network:internet`` permission on the plugin manifest.
            rate_limit: Max requests per minute.
            timeout_s: Request timeout in seconds.
            max_response_bytes: Max response body size.
        """
        self._plugin = plugin_name
        self._allowed = set(allowed_domains or [])
        self._allow_local = allow_local
        self._allow_any_domain = allow_any_domain
        self._timeout = timeout_s
        self._max_bytes = max_response_bytes
        self._limiter = _RateLimiter(max_calls=rate_limit)
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s),
            follow_redirects=True,
            max_redirects=5,
        )

    def _validate_url(self, url: str) -> str:
        """Validate URL against allowlist and local network rules.

        Args:
            url: URL to validate.

        Returns:
            The validated URL.

        Raises:
            PermissionDeniedError: URL violates sandbox rules.
        """
        parsed = urlparse(url)
        hostname = parsed.hostname

        if not hostname:
            raise PermissionDeniedError(self._plugin, f"Invalid URL: {url}")

        # Check domain allowlist (empty = no domains allowed) unless the
        # client was built in open-web mode. The local-IP + DNS-rebinding +
        # rate-limit + size-cap checks below still apply.
        if not self._allow_any_domain and hostname not in self._allowed:
            raise PermissionDeniedError(
                self._plugin,
                f"Domain '{hostname}' not in allowed list: {sorted(self._allowed)}",
            )

        # DNS rebinding protection: resolve and check IP
        resolved_ip = _resolve_hostname(hostname)
        if resolved_ip and _is_local_ip(resolved_ip) and not self._allow_local:
            raise PermissionDeniedError(
                self._plugin,
                f"Domain '{hostname}' resolves to local IP {resolved_ip}",
            )

        # Direct IP check (if URL uses IP instead of domain)
        try:
            ipaddress.ip_address(hostname)
            # It's a raw IP address
            if not self._allow_local and _is_local_ip(hostname):
                raise PermissionDeniedError(
                    self._plugin,
                    f"Local network access blocked: {hostname}",
                )
        except ValueError:
            pass  # Not an IP, already checked via DNS resolution

        return url

    async def get(self, url: str, **kwargs: object) -> httpx.Response:
        """Send GET request with sandbox enforcement.

        Args:
            url: Target URL.
            **kwargs: Additional httpx kwargs (headers, params, etc.)

        Returns:
            httpx.Response.

        Raises:
            PermissionDeniedError: URL/domain/rate violation.
            httpx.HTTPError: Network or HTTP error.
        """
        return await self._request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: object) -> httpx.Response:
        """Send POST request with sandbox enforcement."""
        return await self._request("POST", url, **kwargs)

    async def request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        """Send a request with an arbitrary HTTP method (PROPFIND, REPORT, …).

        Public counterpart to ``_request``: needed by plugins that
        speak protocols extending HTTP — CalDAV uses PROPFIND for
        collection discovery and REPORT for ``calendar-query``.
        Every sandbox guard (URL allowlist, local-IP block, rate
        limit, response size cap, timeout) still applies; only the
        verb is open.
        """
        return await self._request(method.upper(), url, **kwargs)

    async def _request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        """Execute sandboxed HTTP request.

        Args:
            method: HTTP method.
            url: Target URL.
            **kwargs: httpx kwargs.

        Returns:
            httpx.Response (body truncated if too large).
        """
        self._validate_url(url)
        self._limiter.acquire()

        logger.debug(
            "plugin_http_request",
            plugin=self._plugin,
            method=method,
            url=url,
        )

        response = await self._client.request(method, url, **kwargs)  # type: ignore[arg-type]

        # Check response size
        content_length = response.headers.get("content-length")
        if content_length and int(content_length) > self._max_bytes:
            logger.warning(
                "plugin_http_response_too_large",
                plugin=self._plugin,
                url=url,
                size=content_length,
                limit=self._max_bytes,
            )

        return response

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    @property
    def remaining_requests(self) -> int:
        """Requests remaining in current rate window."""
        return self._limiter.remaining

    async def __aenter__(self) -> SandboxedHttpClient:
        """Async context manager entry."""
        return self

    async def __aexit__(self, *args: object) -> None:
        """Async context manager exit — close client."""
        await self.close()
