"""Sovyx Web Intelligence Plugin — Search, fetch, research, learn.

Enterprise-grade web search with multi-backend support, content extraction,
source credibility scoring, brain integration, and intelligent caching.

Default backend: DuckDuckGo (zero API key).
Optional: SearXNG (self-hosted), Brave (API key).

Permissions required: network:internet
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, ClassVar

from sovyx.plugins.sdk import ISovyxPlugin, tool

# ── Constants ──

_MAX_QUERY_LEN = 500
_MAX_RESULTS_DEFAULT = 5
_MAX_RESULTS_CAP = 20
_MAX_FETCH_CHARS = 4000
_SEARCH_TIMEOUT = 10.0
_FETCH_TIMEOUT = 15.0
_RATE_LIMIT_SEARCHES = 30
_RATE_LIMIT_WINDOW = 60.0

# ── Error Messages ──

_MSG_EMPTY_QUERY = "query cannot be empty"
_MSG_QUERY_TOO_LONG = f"query too long (max {_MAX_QUERY_LEN} chars)"
_MSG_NO_RESULTS = "no results found"
_MSG_SEARCH_FAILED = "search failed"
_MSG_FETCH_FAILED = "failed to fetch URL"
_MSG_INVALID_URL = "invalid or disallowed URL"
_MSG_BACKEND_UNAVAILABLE = "search backend unavailable"


# ── Helpers ──


def _ok(action: str, **kwargs: object) -> str:
    """Build success JSON response."""
    return json.dumps({"ok": True, "action": action, **kwargs}, default=str)


def _err(message: str) -> str:
    """Build error JSON response."""
    return json.dumps({"ok": False, "action": "error", "message": message})


class _RateLimiter:
    """Simple sliding-window rate limiter."""

    __slots__ = ("_limit", "_window", "_timestamps")

    def __init__(self, limit: int, window: float) -> None:
        self._limit = limit
        self._window = window
        self._timestamps: list[float] = []

    def check(self) -> bool:
        """Return True if within rate limit."""
        now = time.monotonic()
        cutoff = now - self._window
        self._timestamps = [t for t in self._timestamps if t > cutoff]
        if len(self._timestamps) >= self._limit:
            return False
        self._timestamps.append(now)
        return True


# ── Search Result Schema ──


class SearchResult:
    """Unified search result from any backend."""

    __slots__ = (
        "title",
        "url",
        "snippet",
        "source",
        "date",
        "result_type",
    )

    def __init__(
        self,
        *,
        title: str,
        url: str,
        snippet: str,
        source: str = "",
        date: str = "",
        result_type: str = "web",
    ) -> None:
        self.title = title
        self.url = url
        self.snippet = snippet
        self.source = source
        self.date = date
        self.result_type = result_type

    def to_dict(self) -> dict[str, str]:
        """Convert to serializable dict."""
        d: dict[str, str] = {
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
        }
        if self.source:
            d["source"] = self.source
        if self.date:
            d["date"] = self.date
        if self.result_type != "web":
            d["type"] = self.result_type
        return d


# ── Search Backend Abstraction ──


class SearchBackend:
    """Abstract search backend interface."""

    name: str = "abstract"

    async def search_text(
        self,
        query: str,
        max_results: int,
    ) -> list[SearchResult]:
        """Search for text results."""
        raise NotImplementedError

    async def search_news(
        self,
        query: str,
        max_results: int,
    ) -> list[SearchResult]:
        """Search for news results."""
        raise NotImplementedError


class DuckDuckGoBackend(SearchBackend):
    """DuckDuckGo search via ddgs library. Zero API key."""

    name = "duckduckgo"

    async def search_text(
        self,
        query: str,
        max_results: int,
    ) -> list[SearchResult]:
        """Search DuckDuckGo for text results."""
        try:
            from ddgs import DDGS  # noqa: PLC0415
        except ImportError:
            return []

        def _search() -> list[dict[str, Any]]:
            with DDGS() as ddgs:
                return list(ddgs.text(query, max_results=max_results))

        raw = await asyncio.to_thread(_search)
        return [
            SearchResult(
                title=r.get("title", ""),
                url=r.get("href", ""),
                snippet=r.get("body", ""),
                source=_extract_domain(r.get("href", "")),
            )
            for r in raw
        ]

    async def search_news(
        self,
        query: str,
        max_results: int,
    ) -> list[SearchResult]:
        """Search DuckDuckGo for news results."""
        try:
            from ddgs import DDGS  # noqa: PLC0415
        except ImportError:
            return []

        def _search() -> list[dict[str, Any]]:
            with DDGS() as ddgs:
                return list(ddgs.news(query, max_results=max_results))

        raw = await asyncio.to_thread(_search)
        return [
            SearchResult(
                title=r.get("title", ""),
                url=r.get("url", ""),
                snippet=r.get("body", ""),
                source=r.get("source", _extract_domain(r.get("url", ""))),
                date=r.get("date", ""),
                result_type="news",
            )
            for r in raw
        ]


class SearXNGBackend(SearchBackend):
    """SearXNG search via JSON API. Self-hosted or public instance."""

    name = "searxng"

    def __init__(self, instance_url: str) -> None:
        self._url = instance_url.rstrip("/")

    async def search_text(
        self,
        query: str,
        max_results: int,
    ) -> list[SearchResult]:
        """Search SearXNG for text results."""
        return await self._search(query, max_results, categories="general")

    async def search_news(
        self,
        query: str,
        max_results: int,
    ) -> list[SearchResult]:
        """Search SearXNG for news results."""
        return await self._search(
            query,
            max_results,
            categories="news",
            result_type="news",
        )

    async def _search(
        self,
        query: str,
        max_results: int,
        *,
        categories: str = "general",
        result_type: str = "web",
    ) -> list[SearchResult]:
        import httpx  # noqa: PLC0415

        params: dict[str, str | int] = {
            "q": query,
            "format": "json",
            "categories": categories,
            "pageno": 1,
        }
        try:
            async with httpx.AsyncClient(timeout=_SEARCH_TIMEOUT) as client:
                resp = await client.get(
                    f"{self._url}/search",
                    params=params,
                )
                if resp.status_code != 200:  # noqa: PLR2004
                    return []
                data: dict[str, Any] = resp.json()
                raw_results: list[dict[str, Any]] = data.get("results", [])
                results: list[SearchResult] = []
                for r in raw_results[:max_results]:
                    results.append(
                        SearchResult(
                            title=str(r.get("title", "")),
                            url=str(r.get("url", "")),
                            snippet=str(r.get("content", "")),
                            source=_extract_domain(str(r.get("url", ""))),
                            date=str(r.get("publishedDate", "")),
                            result_type=result_type,
                        ),
                    )
                return results
        except Exception:  # noqa: BLE001
            return []


class BraveBackend(SearchBackend):
    """Brave Search via API. Requires API key."""

    name = "brave"

    _API_URL = "https://api.search.brave.com/res/v1"

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    async def search_text(
        self,
        query: str,
        max_results: int,
    ) -> list[SearchResult]:
        """Search Brave for text results."""
        return await self._search(query, max_results, endpoint="web/search")

    async def search_news(
        self,
        query: str,
        max_results: int,
    ) -> list[SearchResult]:
        """Search Brave for news results."""
        return await self._search(
            query,
            max_results,
            endpoint="news/search",
            result_type="news",
        )

    async def _search(
        self,
        query: str,
        max_results: int,
        *,
        endpoint: str = "web/search",
        result_type: str = "web",
    ) -> list[SearchResult]:
        import httpx  # noqa: PLC0415

        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "X-Subscription-Token": self._api_key,
        }
        params: dict[str, str | int] = {
            "q": query,
            "count": min(max_results, 20),
        }
        try:
            async with httpx.AsyncClient(timeout=_SEARCH_TIMEOUT) as client:
                resp = await client.get(
                    f"{self._API_URL}/{endpoint}",
                    headers=headers,
                    params=params,
                )
                if resp.status_code != 200:  # noqa: PLR2004
                    return []
                data: dict[str, Any] = resp.json()

                # Brave web results are in data.web.results
                # Brave news results are in data.news.results
                if result_type == "news":
                    raw: list[dict[str, Any]] = data.get("news", {}).get("results", [])
                else:
                    raw = data.get("web", {}).get("results", [])

                results: list[SearchResult] = []
                for r in raw[:max_results]:
                    results.append(
                        SearchResult(
                            title=str(r.get("title", "")),
                            url=str(r.get("url", "")),
                            snippet=str(r.get("description", "")),
                            source=_extract_domain(str(r.get("url", ""))),
                            date=str(r.get("age", r.get("page_age", ""))),
                            result_type=result_type,
                        ),
                    )
                return results
        except Exception:  # noqa: BLE001
            return []


def _create_backend(
    backend_name: str,
    *,
    searxng_url: str = "",
    brave_api_key: str = "",
) -> SearchBackend:
    """Factory: create search backend by name."""
    if backend_name == "searxng":
        if not searxng_url:
            msg = "searxng_url required for SearXNG backend"
            raise ValueError(msg)
        return SearXNGBackend(searxng_url)
    if backend_name == "brave":
        if not brave_api_key:
            msg = "brave_api_key required for Brave backend"
            raise ValueError(msg)
        return BraveBackend(brave_api_key)
    return DuckDuckGoBackend()


def _extract_domain(url: str) -> str:
    """Extract domain from URL for source attribution."""
    try:
        from urllib.parse import urlparse  # noqa: PLC0415

        parsed = urlparse(url)
        domain = parsed.netloc
        if domain.startswith("www."):
            domain = domain[4:]
        return domain
    except Exception:  # noqa: BLE001
        return ""


# ── Plugin ──


class WebIntelligencePlugin(ISovyxPlugin):
    """Web Intelligence — search, fetch, research, learn.

    Enterprise-grade web search with DuckDuckGo (default),
    content extraction, source credibility, and brain integration.
    """

    config_schema: ClassVar[dict[str, object]] = {
        "properties": {
            "backend": {
                "type": "string",
                "enum": ["duckduckgo", "searxng", "brave"],
                "default": "duckduckgo",
            },
            "searxng_url": {"type": "string"},
            "brave_api_key": {"type": "string"},
            "max_results": {"type": "integer", "default": 5},
            "fetch_max_chars": {"type": "integer", "default": 4000},
            "cache_enabled": {"type": "boolean", "default": True},
            "auto_learn": {"type": "boolean", "default": False},
        },
    }

    def __init__(self) -> None:
        super().__init__()
        self._backend: SearchBackend = DuckDuckGoBackend()
        self._rate_limiter = _RateLimiter(
            _RATE_LIMIT_SEARCHES,
            _RATE_LIMIT_WINDOW,
        )

    @property
    def name(self) -> str:
        return "web-intelligence"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def description(self) -> str:
        return "Web search, content extraction, and research with brain integration."

    # ── Search Tool ──

    @tool(
        description=(
            "Search the web for information. Modes: "
            "'web' (general search), "
            "'news' (recent news articles). "
            "Returns structured results with title, URL, snippet, and source. "
            "Example: search(query='Bitcoin price today', mode='news')"
        ),
    )
    async def search(
        self,
        query: str,
        *,
        mode: str = "web",
        max_results: int = _MAX_RESULTS_DEFAULT,
    ) -> str:
        """Search the web.

        Args:
            query: Search query string.
            mode: 'web' or 'news'.
            max_results: Number of results (1-20, default 5).

        Returns:
            JSON with search results.
        """
        # Validate
        query = query.strip()
        if not query:
            return _err(_MSG_EMPTY_QUERY)
        if len(query) > _MAX_QUERY_LEN:
            return _err(_MSG_QUERY_TOO_LONG)
        max_results = max(1, min(_MAX_RESULTS_CAP, max_results))
        mode = mode.strip().lower()

        # Rate limit
        if not self._rate_limiter.check():
            return _err("rate limit exceeded (30 searches/min)")

        try:
            results: list[SearchResult]
            if mode == "news":
                results = await asyncio.wait_for(
                    self._backend.search_news(query, max_results),
                    timeout=_SEARCH_TIMEOUT,
                )
            elif mode == "web":
                results = await asyncio.wait_for(
                    self._backend.search_text(query, max_results),
                    timeout=_SEARCH_TIMEOUT,
                )
            else:
                return _err(f"unknown mode: '{mode}'. Valid: web, news")

            if not results:
                return _err(_MSG_NO_RESULTS)

            return _ok(
                "search",
                mode=mode,
                query=query,
                count=len(results),
                backend=self._backend.name,
                results=[r.to_dict() for r in results],
                result=f"Found {len(results)} results for '{query}'",
                message=f"Found {len(results)} results for '{query}'",
            )
        except TimeoutError:
            return _err("search timed out")
        except Exception as e:  # noqa: BLE001
            return _err(f"{_MSG_SEARCH_FAILED}: {e}")

    # ── Fetch Tool ──

    @tool(
        description=(
            "Fetch and extract readable content from a URL. "
            "Extracts main text, title, author, date using trafilatura. "
            "Returns structured content with metadata. "
            "Example: fetch(url='https://reuters.com/article/example')"
        ),
    )
    async def fetch(
        self,
        url: str,
        *,
        max_chars: int = _MAX_FETCH_CHARS,
    ) -> str:
        """Fetch and extract content from a URL.

        Args:
            url: HTTP(S) URL to fetch.
            max_chars: Maximum characters to return (default 4000).

        Returns:
            JSON with extracted text, title, author, date, and metadata.
        """
        url = url.strip()

        # Validate URL
        error = _validate_url(url)
        if error:
            return _err(error)

        max_chars = max(100, min(50000, max_chars))

        try:
            html = await asyncio.wait_for(
                _fetch_html(url),
                timeout=_FETCH_TIMEOUT,
            )
            if html is None:
                return _err(_MSG_FETCH_FAILED)

            # Extract with trafilatura (preferred) or fallback
            extracted = _extract_content(html, url)

            # Truncate content
            text = extracted["text"]
            truncated = len(text) > max_chars
            if truncated:
                text = text[:max_chars] + "..."
                extracted["text"] = text

            return _ok(
                "fetch",
                url=url,
                title=extracted["title"],
                author=extracted["author"],
                date=extracted["date"],
                language=extracted["language"],
                site=extracted["site"],
                text=text,
                char_count=len(text),
                truncated=truncated,
                result=text[:200],
                message=(
                    f"Fetched {len(text)} chars from {extracted['site'] or url}"
                    + (f" by {extracted['author']}" if extracted["author"] else "")
                ),
            )
        except TimeoutError:
            return _err("fetch timed out")
        except Exception as e:  # noqa: BLE001
            return _err(f"{_MSG_FETCH_FAILED}: {e}")


# ── URL Validation ──


_DISALLOWED_SCHEMES = {"file", "ftp", "data", "javascript", "blob"}
_PRIVATE_PREFIXES = (
    "10.",
    "172.16.",
    "172.17.",
    "172.18.",
    "172.19.",
    "172.20.",
    "172.21.",
    "172.22.",
    "172.23.",
    "172.24.",
    "172.25.",
    "172.26.",
    "172.27.",
    "172.28.",
    "172.29.",
    "172.30.",
    "172.31.",
    "192.168.",
    "127.",
    "0.",
    "169.254.",
    "::1",
    "fd",
    "fe80:",
)


def _validate_url(url: str) -> str:
    """Validate URL for safety. Returns error string or empty."""
    from urllib.parse import urlparse  # noqa: PLC0415

    try:
        parsed = urlparse(url)
    except Exception:  # noqa: BLE001
        return _MSG_INVALID_URL

    if not parsed.scheme or not parsed.netloc:
        return _MSG_INVALID_URL

    if parsed.scheme.lower() in _DISALLOWED_SCHEMES:
        return f"disallowed URL scheme: {parsed.scheme}"

    host = parsed.hostname or ""
    if any(host.startswith(p) for p in _PRIVATE_PREFIXES):
        return "private/internal URLs are not allowed"

    if host in ("localhost", ""):
        return "private/internal URLs are not allowed"

    return ""


# ── HTML Fetching ──


async def _fetch_html(url: str) -> str | None:
    """Fetch HTML content from URL."""
    import httpx  # noqa: PLC0415

    headers = {
        "User-Agent": ("Mozilla/5.0 (compatible; SovyxBot/1.0; +https://sovyx.ai)"),
        "Accept": "text/html,application/xhtml+xml,*/*",
    }
    try:
        async with httpx.AsyncClient(
            timeout=_FETCH_TIMEOUT,
            follow_redirects=True,
            max_redirects=5,
        ) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code != 200:  # noqa: PLR2004
                return None
            content_type = resp.headers.get("content-type", "")
            if "text/html" not in content_type and "xhtml" not in content_type:
                return None
            # Size check: max 1MB
            if len(resp.content) > 1_048_576:
                return None
            return resp.text
    except Exception:  # noqa: BLE001
        return None


# ── Content Extraction ──


def _extract_content(html: str, url: str) -> dict[str, str]:
    """Extract main content from HTML using trafilatura or fallback."""
    try:
        return _extract_trafilatura(html, url)
    except Exception:  # noqa: BLE001
        return _extract_fallback(html)


def _extract_trafilatura(html: str, url: str) -> dict[str, str]:
    """Extract using trafilatura (best quality)."""
    import trafilatura  # noqa: PLC0415

    doc = trafilatura.bare_extraction(
        html,
        url=url,
        include_comments=False,
        include_tables=True,
        favor_recall=True,
    )
    if doc is None:
        return _extract_fallback(html)

    return {
        "text": getattr(doc, "text", "") or "",
        "title": getattr(doc, "title", "") or "",
        "author": getattr(doc, "author", "") or "",
        "date": getattr(doc, "date", "") or "",
        "language": getattr(doc, "language", "") or "",
        "site": getattr(doc, "sitename", "") or _extract_domain(url),
    }


def _extract_fallback(html: str) -> dict[str, str]:
    """Basic HTML → text fallback when trafilatura unavailable."""
    import re  # noqa: PLC0415

    # Strip tags
    text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()

    # Extract title
    title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE)
    title = title_match.group(1).strip() if title_match else ""

    return {
        "text": text,
        "title": title,
        "author": "",
        "date": "",
        "language": "",
        "site": "",
    }
