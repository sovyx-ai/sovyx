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
