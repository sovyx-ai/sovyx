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
from typing import TYPE_CHECKING, Any, ClassVar

from sovyx.plugins.sdk import ISovyxPlugin, tool

if TYPE_CHECKING:
    from sovyx.plugins.context import BrainAccess

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


# ── Source Credibility Scoring ──

# Tier 1: Highly trusted (academic, gov, major wire services)
_TIER1_DOMAINS = frozenset(
    {
        # Academic / research
        "arxiv.org",
        "scholar.google.com",
        "pubmed.ncbi.nlm.nih.gov",
        "nature.com",
        "science.org",
        "ieee.org",
        "acm.org",
        "jstor.org",
        "ssrn.com",
        "researchgate.net",
        # Government
        "gov.br",
        "gov.uk",
        "usa.gov",
        "europa.eu",
        "who.int",
        "worldbank.org",
        "imf.org",
        "un.org",
        "bcb.gov.br",
        "sec.gov",
        "federalreserve.gov",
        "bls.gov",
        "census.gov",
        # Major wire services
        "reuters.com",
        "apnews.com",
        "afp.com",
    }
)

# Tier 2: Established news / reference
_TIER2_DOMAINS = frozenset(
    {
        # News
        "bbc.com",
        "bbc.co.uk",
        "nytimes.com",
        "washingtonpost.com",
        "theguardian.com",
        "economist.com",
        "ft.com",
        "bloomberg.com",
        "wsj.com",
        "cnn.com",
        "aljazeera.com",
        "dw.com",
        # Tech
        "techcrunch.com",
        "arstechnica.com",
        "wired.com",
        "theverge.com",
        "hackernews.com",
        "github.com",
        "stackoverflow.com",
        # Reference
        "wikipedia.org",
        "britannica.com",
        "investopedia.com",
        # Brazil
        "folha.uol.com.br",
        "estadao.com.br",
        "g1.globo.com",
        "valor.globo.com",
        "infomoney.com.br",
        # Finance
        "coindesk.com",
        "coingecko.com",
        "tradingview.com",
        "yahoo.com",
        "cnbc.com",
        "marketwatch.com",
    }
)

# Tier 3: Known but lower trust (blogs, social, user-generated)
_TIER3_DOMAINS = frozenset(
    {
        "medium.com",
        "substack.com",
        "dev.to",
        "reddit.com",
        "quora.com",
        "twitter.com",
        "x.com",
        "facebook.com",
        "youtube.com",
        "tiktok.com",
        "instagram.com",
        "linkedin.com",
        "pinterest.com",
        "tumblr.com",
    }
)

# TLD credibility adjustments
_TRUSTED_TLDS = frozenset({".edu", ".gov", ".gov.br", ".ac.uk", ".edu.br"})
_LOW_TRUST_TLDS = frozenset({".xyz", ".info", ".biz", ".click", ".top"})


class CredibilityScore:
    """Source credibility assessment."""

    __slots__ = ("score", "tier", "domain", "reasons")

    def __init__(
        self,
        *,
        score: float,
        tier: str,
        domain: str,
        reasons: list[str],
    ) -> None:
        self.score = score  # 0.0-1.0
        self.tier = tier  # "tier1", "tier2", "tier3", "unknown"
        self.domain = domain
        self.reasons = reasons

    def to_dict(self) -> dict[str, object]:
        """Serialize to dict."""
        return {
            "score": round(self.score, 2),
            "tier": self.tier,
            "domain": self.domain,
            "reasons": self.reasons,
        }


def score_credibility(url: str) -> CredibilityScore:
    """Score source credibility based on domain reputation.

    Uses tiered domain lists + TLD heuristics.
    Fast, deterministic, no external calls.
    """
    domain = _extract_domain(url).lower()
    reasons: list[str] = []

    # Check exact domain match
    if domain in _TIER1_DOMAINS:
        return CredibilityScore(
            score=0.95,
            tier="tier1",
            domain=domain,
            reasons=["known authoritative source"],
        )
    if domain in _TIER2_DOMAINS:
        return CredibilityScore(
            score=0.80,
            tier="tier2",
            domain=domain,
            reasons=["established publication"],
        )
    if domain in _TIER3_DOMAINS:
        return CredibilityScore(
            score=0.50,
            tier="tier3",
            domain=domain,
            reasons=["user-generated or social platform"],
        )

    # Check parent domain (e.g. "news.bbc.co.uk" → "bbc.co.uk")
    parts = domain.split(".")
    for i in range(1, len(parts)):
        parent = ".".join(parts[i:])
        if parent in _TIER1_DOMAINS:
            return CredibilityScore(
                score=0.90,
                tier="tier1",
                domain=domain,
                reasons=[f"subdomain of {parent} (authoritative)"],
            )
        if parent in _TIER2_DOMAINS:
            return CredibilityScore(
                score=0.75,
                tier="tier2",
                domain=domain,
                reasons=[f"subdomain of {parent} (established)"],
            )

    # TLD-based heuristics
    score = 0.50
    for tld in _TRUSTED_TLDS:
        if domain.endswith(tld):
            score = 0.80
            reasons.append(f"trusted TLD ({tld})")
            break

    for tld in _LOW_TRUST_TLDS:
        if domain.endswith(tld):
            score = 0.30
            reasons.append(f"low-trust TLD ({tld})")
            break

    # HTTPS bonus (implied by url scheme)
    if url.startswith("https://"):
        score = min(score + 0.05, 1.0)
        reasons.append("HTTPS")

    if not reasons:
        reasons.append("unknown domain")

    return CredibilityScore(
        score=round(score, 2),
        tier="unknown",
        domain=domain,
        reasons=reasons,
    )


# ── Query Intent Classification ──


class QueryIntent:
    """Classified query intent for routing."""

    __slots__ = ("intent_type", "search_mode", "time_range", "confidence")

    def __init__(
        self,
        *,
        intent_type: str,
        search_mode: str,
        time_range: str,
        confidence: float,
    ) -> None:
        self.intent_type = intent_type  # factual, temporal, price, procedural
        self.search_mode = search_mode  # web, news
        self.time_range = time_range  # "", "day", "week", "month"
        self.confidence = confidence  # 0.0-1.0

    def to_dict(self) -> dict[str, object]:
        """Serialize to dict."""
        return {
            "intent_type": self.intent_type,
            "search_mode": self.search_mode,
            "time_range": self.time_range,
            "confidence": self.confidence,
        }


# Temporal keywords → (time_range, confidence_boost)
_TEMPORAL_KEYWORDS: dict[str, tuple[str, float]] = {
    "today": ("day", 0.9),
    "yesterday": ("day", 0.8),
    "tonight": ("day", 0.85),
    "now": ("day", 0.7),
    "right now": ("day", 0.8),
    "this week": ("week", 0.85),
    "this month": ("month", 0.8),
    "latest": ("week", 0.75),
    "recent": ("week", 0.7),
    "just": ("day", 0.6),
    "breaking": ("day", 0.9),
    "current": ("day", 0.65),
    "hoje": ("day", 0.9),
    "ontem": ("day", 0.8),
    "agora": ("day", 0.7),
    "esta semana": ("week", 0.85),
    "este mês": ("month", 0.8),
    "último": ("week", 0.7),
    "recente": ("week", 0.7),
}

# News-indicating keywords
_NEWS_KEYWORDS = frozenset(
    {
        "news",
        "notícia",
        "notícias",
        "happened",
        "announced",
        "released",
        "launched",
        "update",
        "report",
        "statement",
        "press",
        "election",
        "crash",
        "surge",
        "rally",
        "war",
        "embargo",
        "tariff",
        "regulation",
        "fed",
        "inflation",
    }
)

# Price/market keywords
_PRICE_KEYWORDS = frozenset(
    {
        "price",
        "preço",
        "cotação",
        "valor",
        "cost",
        "how much",
        "quanto",
        "worth",
        "market cap",
        "btc",
        "eth",
        "bitcoin",
        "ethereum",
        "crypto",
        "stock",
        "ação",
        "ações",
        "nasdaq",
        "s&p",
        "dollar",
        "dólar",
        "euro",
        "real",
        "usd",
        "brl",
    }
)

# Procedural keywords
_PROCEDURAL_KEYWORDS = frozenset(
    {
        "how to",
        "como",
        "tutorial",
        "guide",
        "guia",
        "step by step",
        "passo a passo",
        "recipe",
        "receita",
        "install",
        "instalar",
        "setup",
        "configure",
    }
)


def classify_query(query: str) -> QueryIntent:
    """Classify query intent using keyword heuristics (no LLM call).

    Fast, deterministic classification for routing decisions.
    """
    q = query.lower().strip()

    # Check temporal keywords first
    best_time_range = ""
    best_confidence = 0.0
    for keyword, (time_range, confidence) in _TEMPORAL_KEYWORDS.items():
        if keyword in q and confidence > best_confidence:
            best_time_range = time_range
            best_confidence = confidence

    # Check price/market
    q_words = set(q.split())
    price_hits = len(q_words & _PRICE_KEYWORDS)
    if price_hits > 0:
        return QueryIntent(
            intent_type="price",
            search_mode="news" if best_time_range else "web",
            time_range=best_time_range or "day",
            confidence=min(0.5 + price_hits * 0.2, 0.95),
        )

    # Check news
    news_hits = len(q_words & _NEWS_KEYWORDS)
    if news_hits > 0 or best_confidence >= 0.7:
        return QueryIntent(
            intent_type="temporal",
            search_mode="news",
            time_range=best_time_range or "day",
            confidence=max(best_confidence, min(0.5 + news_hits * 0.2, 0.9)),
        )

    # Check procedural
    for kw in _PROCEDURAL_KEYWORDS:
        if kw in q:
            return QueryIntent(
                intent_type="procedural",
                search_mode="web",
                time_range="",
                confidence=0.7,
            )

    # Default: factual web search
    return QueryIntent(
        intent_type="factual",
        search_mode="web",
        time_range=best_time_range,
        confidence=0.5 if not best_time_range else best_confidence,
    )


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


# ── Adaptive Cache ──

# TTL by intent type (seconds)
_CACHE_TTL: dict[str, int] = {
    "price": 300,  # 5 min — volatile data
    "temporal": 600,  # 10 min — recent events
    "factual": 3600,  # 1 hour — stable facts
    "procedural": 86400,  # 24 hours — how-to rarely changes
}
_CACHE_TTL_DEFAULT = 1800  # 30 min fallback
_CACHE_MAX_ENTRIES = 200


class _CacheEntry:
    """Single cache entry with adaptive TTL."""

    __slots__ = ("value", "expires_at", "intent_type", "hits")

    def __init__(self, value: str, ttl: int, intent_type: str) -> None:
        self.value = value
        self.expires_at = time.monotonic() + ttl
        self.intent_type = intent_type
        self.hits = 0

    @property
    def alive(self) -> bool:
        """Check if entry is still valid."""
        return time.monotonic() < self.expires_at


class _SearchCache:
    """In-memory cache with intent-adaptive TTL and LRU eviction."""

    __slots__ = ("_store", "_max_entries", "_hits", "_misses")

    def __init__(self, max_entries: int = _CACHE_MAX_ENTRIES) -> None:
        self._store: dict[str, _CacheEntry] = {}
        self._max_entries = max_entries
        self._hits = 0
        self._misses = 0

    def _make_key(self, query: str, mode: str) -> str:
        """Normalize query into cache key."""
        return f"{mode}:{query.lower().strip()}"

    def get(self, query: str, mode: str) -> str | None:
        """Look up cached result. Returns None on miss or expiry."""
        key = self._make_key(query, mode)
        entry = self._store.get(key)
        if entry is None or not entry.alive:
            if entry is not None:
                del self._store[key]  # expired
            self._misses += 1
            return None
        entry.hits += 1
        self._hits += 1
        return entry.value

    def put(
        self,
        query: str,
        mode: str,
        value: str,
        intent_type: str = "",
    ) -> None:
        """Store result with intent-based TTL."""
        ttl = _CACHE_TTL.get(intent_type, _CACHE_TTL_DEFAULT)
        key = self._make_key(query, mode)

        # Evict if full
        if len(self._store) >= self._max_entries and key not in self._store:
            self._evict()

        self._store[key] = _CacheEntry(value, ttl, intent_type)

    def _evict(self) -> None:
        """Remove expired entries first, then LRU (fewest hits)."""
        # Pass 1: remove expired
        expired = [k for k, v in self._store.items() if not v.alive]
        for k in expired:
            del self._store[k]
        if len(self._store) < self._max_entries:
            return

        # Pass 2: remove lowest-hit entry
        if self._store:
            victim = min(self._store, key=lambda k: self._store[k].hits)
            del self._store[victim]

    def clear(self) -> None:
        """Clear all entries."""
        self._store.clear()
        self._hits = 0
        self._misses = 0

    @property
    def stats(self) -> dict[str, int]:
        """Cache statistics."""
        alive = sum(1 for v in self._store.values() if v.alive)
        return {
            "entries": alive,
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate_pct": (
                round(self._hits * 100 / (self._hits + self._misses))
                if (self._hits + self._misses) > 0
                else 0
            ),
        }


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

    def __init__(
        self,
        brain: BrainAccess | None = None,
    ) -> None:
        super().__init__()
        self._backend: SearchBackend = DuckDuckGoBackend()
        self._rate_limiter = _RateLimiter(
            _RATE_LIMIT_SEARCHES,
            _RATE_LIMIT_WINDOW,
        )
        self._brain = brain
        self._cache = _SearchCache()

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
            "'auto' (smart routing based on query intent — default), "
            "'web' (general search), "
            "'news' (recent news articles). "
            "Auto mode classifies intent (factual/temporal/price/procedural) "
            "and routes to the best search type automatically. "
            "Example: search(query='Bitcoin price today')"
        ),
    )
    async def search(
        self,
        query: str,
        *,
        mode: str = "auto",
        max_results: int = _MAX_RESULTS_DEFAULT,
    ) -> str:
        """Search the web.

        Args:
            query: Search query string.
            mode: 'auto', 'web', or 'news'.
            max_results: Number of results (1-20, default 5).

        Returns:
            JSON with search results and intent classification.
        """
        # Validate
        query = query.strip()
        if not query:
            return _err(_MSG_EMPTY_QUERY)
        if len(query) > _MAX_QUERY_LEN:
            return _err(_MSG_QUERY_TOO_LONG)
        max_results = max(1, min(_MAX_RESULTS_CAP, max_results))
        mode = mode.strip().lower()

        # Intent classification
        intent: QueryIntent | None = None
        if mode == "auto":
            intent = classify_query(query)
            mode = intent.search_mode

        # Cache check
        cached = self._cache.get(query, mode)
        if cached is not None:
            return cached

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

            extra: dict[str, object] = {}
            if intent is not None:
                extra["intent"] = intent.to_dict()

            response = _ok(
                "search",
                mode=mode,
                query=query,
                count=len(results),
                backend=self._backend.name,
                cached=False,
                results=[
                    {**r.to_dict(), "credibility": score_credibility(r.url).to_dict()}
                    for r in results
                ],
                result=f"Found {len(results)} results for '{query}'",
                message=f"Found {len(results)} results for '{query}'",
                **extra,
            )

            # Cache the successful result
            intent_type = intent.intent_type if intent else ""
            self._cache.put(query, mode, response, intent_type)

            return response
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

            cred = score_credibility(url)
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
                credibility=cred.to_dict(),
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

    # ── Research Tool ──

    _MAX_RESEARCH_SOURCES = 5
    _MAX_EXTRACT_CHARS = 2000

    @tool(
        description=(
            "Deep research on a topic: searches, fetches top sources, "
            "extracts content, scores credibility, and returns organized "
            "findings with numbered citations. Use for complex questions "
            "that need multiple sources. "
            "Example: research(query='impact of US tariffs on crypto market 2025')"
        ),
    )
    async def research(
        self,
        query: str,
        *,
        max_sources: int = 3,
        include_news: bool = True,
    ) -> str:
        """Multi-step research: search → fetch → organize with citations.

        Args:
            query: Research question or topic.
            max_sources: Max sources to fetch (1-5, default 3).
            include_news: Also search news (default True).

        Returns:
            JSON with sources, extracted content, citations, and credibility.
        """
        query = query.strip()
        if not query:
            return _err(_MSG_EMPTY_QUERY)
        if len(query) > _MAX_QUERY_LEN:
            return _err(_MSG_QUERY_TOO_LONG)
        max_sources = max(1, min(self._MAX_RESEARCH_SOURCES, max_sources))

        try:
            return await asyncio.wait_for(
                self._do_research(query, max_sources, include_news=include_news),
                timeout=60.0,
            )
        except TimeoutError:
            return _err("research timed out")
        except Exception as e:  # noqa: BLE001
            return _err(f"research failed: {e}")

    async def _do_research(
        self,
        query: str,
        max_sources: int,
        *,
        include_news: bool,
    ) -> str:
        """Execute research pipeline."""
        # Step 1: Search (web + optionally news)
        all_results: list[SearchResult] = []
        seen_urls: set[str] = set()

        # Web search
        if self._rate_limiter.check():
            try:
                web_results = await asyncio.wait_for(
                    self._backend.search_text(query, max_sources * 2),
                    timeout=_SEARCH_TIMEOUT,
                )
                for r in web_results:
                    if r.url not in seen_urls:
                        seen_urls.add(r.url)
                        all_results.append(r)
            except Exception:  # noqa: BLE001
                pass

        # News search
        if include_news and self._rate_limiter.check():
            try:
                news_results = await asyncio.wait_for(
                    self._backend.search_news(query, max_sources),
                    timeout=_SEARCH_TIMEOUT,
                )
                for r in news_results:
                    if r.url not in seen_urls:
                        seen_urls.add(r.url)
                        all_results.append(r)
            except Exception:  # noqa: BLE001
                pass

        if not all_results:
            return _err(_MSG_NO_RESULTS)

        # Step 2: Score and rank by credibility
        scored = [(r, score_credibility(r.url)) for r in all_results]
        scored.sort(key=lambda x: x[1].score, reverse=True)

        # Step 3: Fetch top sources
        top = scored[:max_sources]
        sources: list[dict[str, object]] = []

        for i, (result, cred) in enumerate(top, 1):
            source: dict[str, object] = {
                "citation": i,
                "title": result.title,
                "url": result.url,
                "snippet": result.snippet,
                "source": result.source,
                "credibility": cred.to_dict(),
            }

            # Try to fetch full content
            extracted = await self._safe_fetch_content(result.url)
            if extracted:
                text = extracted["text"]
                if len(text) > self._MAX_EXTRACT_CHARS:
                    text = text[: self._MAX_EXTRACT_CHARS] + "..."
                source["content"] = text
                source["author"] = extracted["author"]
                source["date"] = extracted["date"]
            else:
                source["content"] = result.snippet
                source["author"] = ""
                source["date"] = result.date

            sources.append(source)

        # Step 4: Build citation map
        citations = [f"[{s['citation']}] {s['title']} — {s['url']}" for s in sources]

        if sources:
            _total = sum(
                float(str(s["credibility"]["score"]))  # type: ignore[index, misc]
                for s in sources
            )
            avg_cred = _total / len(sources)
        else:
            avg_cred = 0.0

        return _ok(
            "research",
            query=query,
            source_count=len(sources),
            sources=sources,
            citations=citations,
            avg_credibility=round(avg_cred, 2),
            result=(
                f"Researched '{query}': {len(sources)} sources, avg credibility {avg_cred:.0%}"
            ),
            message=(
                f"Found {len(sources)} sources for '{query}' (avg credibility: {avg_cred:.0%})"
            ),
        )

    async def _safe_fetch_content(self, url: str) -> dict[str, str] | None:
        """Fetch and extract content, returning None on any failure."""
        error = _validate_url(url)
        if error:
            return None
        try:
            html = await asyncio.wait_for(
                _fetch_html(url),
                timeout=_FETCH_TIMEOUT,
            )
            if html is None:
                return None
            return _extract_content(html, url)
        except Exception:  # noqa: BLE001
            return None

    # ── Brain Integration Tool ──

    _NO_BRAIN = "brain access not configured — plugin needs brain:write permission"

    @tool(
        description=(
            "Save web research findings to the brain with source provenance. "
            "Stores each fact as a concept with URL, author, date, and "
            "credibility metadata. Use after research() to persist knowledge. "
            "Example: learn_from_web(name='US Tariff Impact 2025', "
            "content='Summary of findings...', url='https://reuters.com/article')"
        ),
    )
    async def learn_from_web(
        self,
        name: str,
        content: str,
        *,
        url: str = "",
        author: str = "",
        date: str = "",
        category: str = "web_research",
    ) -> str:
        """Save a web finding to the brain with provenance.

        Args:
            name: Concept name/title.
            content: Content to save (finding, summary, fact).
            url: Source URL for provenance tracking.
            author: Source author if known.
            date: Publication date if known.
            category: Brain category (default 'web_research').

        Returns:
            JSON with concept ID and provenance metadata.
        """
        if self._brain is None:
            return _err(self._NO_BRAIN)

        name = name.strip()
        content = content.strip()
        if not name:
            return _err("name cannot be empty")
        if not content:
            return _err("content cannot be empty")

        # Score credibility if URL provided
        cred = score_credibility(url) if url else None
        confidence = min(0.9, cred.score) if cred else 0.5

        # Build provenance metadata
        provenance: dict[str, object] = {
            "source_type": "web",
            "url": url,
            "author": author,
            "date": date,
            "retrieved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        if cred:
            provenance["credibility"] = cred.to_dict()

        try:
            concept_id = await self._brain.learn(
                name=name,
                content=content,
                category=category,
                confidence=confidence,
                importance=0.6,
                metadata={"provenance": provenance},
            )
            return _ok(
                "learn_from_web",
                concept_id=concept_id,
                name=name,
                category=category,
                confidence=confidence,
                provenance=provenance,
                result=f"Saved '{name}' to brain (confidence: {confidence:.0%})",
                message=f"Learned '{name}' from {url or 'web'} — saved to brain",
            )
        except Exception as e:  # noqa: BLE001
            return _err(f"failed to save to brain: {e}")

    @tool(
        description=(
            "Recall what the brain knows about a topic from web research. "
            "Searches brain for previously learned web findings. "
            "Example: recall_web(query='US tariffs crypto impact')"
        ),
    )
    async def recall_web(
        self,
        query: str,
        *,
        max_results: int = 5,
    ) -> str:
        """Search brain for previously learned web findings.

        Args:
            query: Search query.
            max_results: Max results (1-20, default 5).

        Returns:
            JSON with matching brain concepts and their provenance.
        """
        if self._brain is None:
            return _err(self._NO_BRAIN)

        query = query.strip()
        if not query:
            return _err(_MSG_EMPTY_QUERY)
        max_results = max(1, min(20, max_results))

        try:
            results = await self._brain.search(
                query,
                limit=max_results,
            )
            concepts: list[dict[str, object]] = []
            for r in results:
                concept: dict[str, object] = {
                    "id": r.get("id", ""),
                    "name": r.get("name", ""),
                    "content": r.get("content", ""),
                    "category": r.get("category", ""),
                    "confidence": r.get("confidence", 0),
                    "score": r.get("score", 0),
                    "source": r.get("source", ""),
                }
                concepts.append(concept)

            return _ok(
                "recall_web",
                query=query,
                count=len(concepts),
                concepts=concepts,
                result=(
                    f"Found {len(concepts)} brain memories for '{query}'"
                    if concepts
                    else f"No brain memories found for '{query}'"
                ),
                message=(
                    f"Recalled {len(concepts)} web findings for '{query}'"
                    if concepts
                    else f"No prior web research found for '{query}'"
                ),
            )
        except Exception as e:  # noqa: BLE001
            return _err(f"brain recall failed: {e}")

    # ── Quick Lookup Tool ──

    @tool(
        description=(
            "Quick lookup for instant answers: definitions, conversions, "
            "prices, and facts. Faster than full search — uses DuckDuckGo "
            "instant answers when available, falls back to web search. "
            "Modes: 'define' (word/concept definition), "
            "'convert' (unit/currency conversion), "
            "'price' (crypto/stock price lookup), "
            "'auto' (detect from query — default). "
            "Example: lookup(query='bitcoin price') or "
            "lookup(query='what is kubernetes', mode='define')"
        ),
    )
    async def lookup(
        self,
        query: str,
        *,
        mode: str = "auto",
    ) -> str:
        """Quick lookup for instant answers.

        Args:
            query: Lookup query.
            mode: 'auto', 'define', 'convert', 'price', or 'weather'.

        Returns:
            JSON with answer, source, and type.
        """
        query = query.strip()
        if not query:
            return _err(_MSG_EMPTY_QUERY)
        if len(query) > _MAX_QUERY_LEN:
            return _err(_MSG_QUERY_TOO_LONG)
        mode = mode.strip().lower()

        if mode == "auto":
            mode = self._detect_lookup_mode(query)

        # Check cache
        cached = self._cache.get(f"lookup:{query}", mode)
        if cached is not None:
            return cached

        try:
            result = await asyncio.wait_for(
                self._do_lookup(query, mode),
                timeout=_SEARCH_TIMEOUT,
            )
            # Cache with appropriate TTL
            intent = "price" if mode == "price" else "factual"
            self._cache.put(f"lookup:{query}", mode, result, intent)
            return result
        except TimeoutError:
            return _err("lookup timed out")
        except Exception as e:  # noqa: BLE001
            return _err(f"lookup failed: {e}")

    @staticmethod
    def _detect_lookup_mode(query: str) -> str:
        """Auto-detect lookup mode from query."""
        q = query.lower()

        # Weather detection
        weather_triggers = {
            "weather",
            "temperatura",
            "temperature",
            "forecast",
            "previsão",
            "clima",
            "rain",
            "chuva",
            "will it rain",
            "vai chover",
        }
        if any(t in q for t in weather_triggers):
            return "weather"

        # Price detection
        price_triggers = {
            "price",
            "preço",
            "cotação",
            "valor",
            "bitcoin",
            "btc",
            "eth",
            "ethereum",
            "stock",
            "ação",
            "nasdaq",
        }
        if any(t in q for t in price_triggers):
            return "price"

        # Conversion detection
        convert_triggers = {
            " to ",
            " in ",
            " para ",
            " em ",
            "convert",
            "converter",
            "how many",
            "quantos",
        }
        if any(t in q for t in convert_triggers):
            return "convert"

        # Definition detection
        define_triggers = {
            "what is",
            "what are",
            "o que é",
            "o que são",
            "define",
            "definição",
            "meaning",
            "significado",
        }
        if any(t in q for t in define_triggers):
            return "define"

        return "define"  # default

    async def _do_lookup(self, query: str, mode: str) -> str:
        """Execute lookup via search backend."""
        # Weather mode — use Open-Meteo directly if available
        if mode == "weather":
            return await self._weather_lookup(query)

        # Rate limit
        if not self._rate_limiter.check():
            return _err("rate limit exceeded")

        # Use web search with small result count for quick answers
        search_query = query
        if mode == "define":
            if not any(w in query.lower() for w in ("define", "what is", "o que")):
                search_query = f"what is {query}"
        elif mode == "price":
            if "price" not in query.lower() and "preço" not in query.lower():
                search_query = f"{query} price"
        elif mode == "convert":
            search_query = query  # user query usually well-formed

        results = await self._backend.search_text(search_query, 3)
        if not results:
            return _err(f"no results for '{query}'")

        # Build concise answer from top results
        top = results[0]
        snippets = [r.snippet for r in results[:3] if r.snippet]

        return _ok(
            "lookup",
            mode=mode,
            query=query,
            answer=top.snippet,
            source=top.source,
            url=top.url,
            title=top.title,
            credibility=score_credibility(top.url).to_dict(),
            supporting_snippets=snippets[1:] if len(snippets) > 1 else [],
            result=top.snippet[:200],
            message=f"Quick answer from {top.source}: {top.snippet[:100]}",
        )

    async def _weather_lookup(self, query: str) -> str:
        """Weather lookup via Open-Meteo (same backend as WeatherPlugin)."""
        try:
            from sovyx.plugins.official.weather import (  # noqa: PLC0415
                _WMO_CODES,
                _fetch_weather,
                _geocode,
            )
        except ImportError:
            # Fallback to web search for weather
            if not self._rate_limiter.check():
                return _err("rate limit exceeded")
            results = await self._backend.search_text(f"{query} weather", 3)
            if not results:
                return _err(f"no weather results for '{query}'")
            top = results[0]
            return _ok(
                "lookup",
                mode="weather",
                query=query,
                answer=top.snippet,
                source="web search",
                url=top.url,
                result=top.snippet[:200],
                message=f"Weather (web): {top.snippet[:100]}",
            )

        # Extract city name from query
        city = _extract_city(query)
        coords = await _geocode(city)
        if coords is None:
            return _err(f"could not find city: {city}")

        lat, lon, display_name = coords
        data = await _fetch_weather(lat, lon, forecast_days=1)
        if data is None:
            return _err("error fetching weather data")

        current: dict[str, Any] = data.get("current", {})
        temp = current.get("temperature_2m", "?")
        humidity = current.get("relative_humidity_2m", "?")
        wind = current.get("wind_speed_10m", "?")
        code = current.get("weather_code", 0)
        condition = _WMO_CODES.get(int(code), "Unknown")

        answer = f"{condition}, {temp}°C | Humidity: {humidity}% | Wind: {wind} km/h"

        return _ok(
            "lookup",
            mode="weather",
            query=query,
            city=display_name,
            answer=answer,
            temperature_c=temp,
            humidity_pct=humidity,
            wind_kmh=wind,
            condition=condition,
            source="Open-Meteo",
            url="https://open-meteo.com/",
            credibility={
                "score": 0.85,
                "tier": "tier2",
                "domain": "open-meteo.com",
                "reasons": ["weather API"],
            },
            result=f"Weather in {display_name}: {answer}",
            message=f"Weather in {display_name}: {answer}",
        )


def _extract_city(query: str) -> str:
    """Extract city name from weather query."""
    q = query.lower()
    # Remove common weather-related words
    remove_words = {
        "weather",
        "forecast",
        "temperature",
        "rain",
        "clima",
        "previsão",
        "temperatura",
        "chuva",
        "tempo",
        "will it",
        "vai",
        "chover",
        "in",
        "em",
        "for",
        "para",
        "what is the",
        "what's the",
        "how is the",
        "como está o",
        "qual",
        "hoje",
        "today",
        "tomorrow",
        "amanhã",
    }
    words = q.split()
    city_words = [w for w in words if w not in remove_words]
    # If we stripped everything, use the original minus the first trigger word
    if not city_words:
        return query.strip()
    return " ".join(city_words).strip().title()


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
