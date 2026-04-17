"""Tests for Sovyx Web Intelligence Plugin (TASK-496)."""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sovyx.plugins.official import web_intelligence as _web_mod  # anti-pattern #11
from sovyx.plugins.official.web_intelligence import (
    DuckDuckGoBackend,
    SearchBackend,
    SearchResult,
    WebIntelligencePlugin,
    _extract_domain,
    _RateLimiter,
)


def _parse(raw: str) -> dict[str, object]:
    return json.loads(raw)  # type: ignore[no-any-return]


# ── SearchResult ──


class TestSearchResult:
    """Tests for SearchResult schema."""

    def test_to_dict_minimal(self) -> None:
        r = SearchResult(title="Test", url="https://example.com", snippet="A snippet")
        d = r.to_dict()
        assert d["title"] == "Test"
        assert d["url"] == "https://example.com"
        assert d["snippet"] == "A snippet"
        assert "source" not in d
        assert "date" not in d
        assert "type" not in d

    def test_to_dict_full(self) -> None:
        r = SearchResult(
            title="News",
            url="https://reuters.com/article",
            snippet="Breaking",
            source="reuters.com",
            date="2026-04-12",
            result_type="news",
        )
        d = r.to_dict()
        assert d["source"] == "reuters.com"
        assert d["date"] == "2026-04-12"
        assert d["type"] == "news"

    def test_web_type_not_included(self) -> None:
        """Default 'web' type is omitted from dict."""
        r = SearchResult(title="T", url="U", snippet="S", result_type="web")
        assert "type" not in r.to_dict()


# ── Domain Extraction ──


class TestExtractDomain:
    """Tests for _extract_domain helper."""

    def test_basic(self) -> None:
        assert _extract_domain("https://www.example.com/path") == "example.com"

    def test_no_www(self) -> None:
        assert _extract_domain("https://reuters.com/article") == "reuters.com"

    def test_subdomain(self) -> None:
        assert _extract_domain("https://news.bbc.co.uk/story") == "news.bbc.co.uk"

    def test_empty(self) -> None:
        assert _extract_domain("") == ""

    def test_invalid(self) -> None:
        assert _extract_domain("not-a-url") == ""


# ── Rate Limiter ──


class TestRateLimiter:
    """Tests for _RateLimiter."""

    def test_within_limit(self) -> None:
        rl = _RateLimiter(3, 60.0)
        assert rl.check() is True
        assert rl.check() is True
        assert rl.check() is True

    def test_exceeds_limit(self) -> None:
        rl = _RateLimiter(2, 60.0)
        assert rl.check() is True
        assert rl.check() is True
        assert rl.check() is False


# ── Plugin Properties ──


class TestPluginProperties:
    """Tests for WebIntelligencePlugin metadata."""

    def test_name(self) -> None:
        assert WebIntelligencePlugin().name == "web-intelligence"

    def test_version(self) -> None:
        assert WebIntelligencePlugin().version == "1.0.0"

    def test_description(self) -> None:
        desc = WebIntelligencePlugin().description
        assert "search" in desc.lower()


# ── Search Tool ──


def _mock_ddgs_text(results: list[dict[str, str]]) -> AsyncMock:
    """Create mock for DuckDuckGoBackend.search_text."""
    mock = AsyncMock(
        return_value=[
            SearchResult(
                title=r["title"],
                url=r["url"],
                snippet=r["snippet"],
                source=_extract_domain(r["url"]),
            )
            for r in results
        ],
    )
    return mock


def _mock_ddgs_news(results: list[dict[str, str]]) -> AsyncMock:
    """Create mock for DuckDuckGoBackend.search_news."""
    mock = AsyncMock(
        return_value=[
            SearchResult(
                title=r["title"],
                url=r["url"],
                snippet=r["snippet"],
                source=r.get("source", ""),
                date=r.get("date", ""),
                result_type="news",
            )
            for r in results
        ],
    )
    return mock


_SAMPLE_WEB_RESULTS = [
    {
        "title": "Python Programming Language",
        "url": "https://www.python.org",
        "snippet": "Python is a programming language.",
    },
    {
        "title": "Learn Python",
        "url": "https://realpython.com",
        "snippet": "Real Python tutorials.",
    },
]

_SAMPLE_NEWS_RESULTS = [
    {
        "title": "Bitcoin Surges Past $80K",
        "url": "https://reuters.com/crypto/bitcoin",
        "snippet": "Bitcoin surged past $80,000 today.",
        "source": "Reuters",
        "date": "2026-04-12T02:00:00+00:00",
    },
]


class TestSearchWeb:
    """Tests for search tool — web mode."""

    @pytest.mark.anyio()
    async def test_basic_search(self) -> None:
        p = WebIntelligencePlugin()
        p._backend.search_text = _mock_ddgs_text(_SAMPLE_WEB_RESULTS)  # type: ignore[assignment]
        data = _parse(await p.search("python programming"))
        assert data["ok"] is True
        assert data["mode"] == "web"
        assert data["count"] == 2
        assert data["backend"] == "duckduckgo"
        results = data["results"]
        assert isinstance(results, list)
        assert results[0]["title"] == "Python Programming Language"
        assert results[0]["url"] == "https://www.python.org"

    @pytest.mark.anyio()
    async def test_empty_query(self) -> None:
        p = WebIntelligencePlugin()
        data = _parse(await p.search(""))
        assert data["ok"] is False
        assert "empty" in str(data["message"])

    @pytest.mark.anyio()
    async def test_query_too_long(self) -> None:
        p = WebIntelligencePlugin()
        data = _parse(await p.search("x" * 501))
        assert data["ok"] is False
        assert "too long" in str(data["message"])

    @pytest.mark.anyio()
    async def test_no_results(self) -> None:
        p = WebIntelligencePlugin()
        p._backend.search_text = AsyncMock(return_value=[])  # type: ignore[assignment]
        data = _parse(await p.search("xyznonexistent12345"))
        assert data["ok"] is False
        assert "no results" in str(data["message"])

    @pytest.mark.anyio()
    async def test_max_results_capped(self) -> None:
        """max_results capped at 20."""
        p = WebIntelligencePlugin()
        p._backend.search_text = _mock_ddgs_text(_SAMPLE_WEB_RESULTS)  # type: ignore[assignment]
        await p.search("test", max_results=100)
        # Should have been capped — check the call
        p._backend.search_text.assert_awaited_once()  # type: ignore[union-attr]
        call_args = p._backend.search_text.call_args  # type: ignore[union-attr]
        assert call_args[0][1] <= 20  # noqa: PLR2004

    @pytest.mark.anyio()
    async def test_invalid_mode(self) -> None:
        p = WebIntelligencePlugin()
        data = _parse(await p.search("test", mode="invalid"))
        assert data["ok"] is False
        assert "web" in str(data["message"])
        assert "news" in str(data["message"])


class TestSearchNews:
    """Tests for search tool — news mode."""

    @pytest.mark.anyio()
    async def test_news_search(self) -> None:
        p = WebIntelligencePlugin()
        p._backend.search_news = _mock_ddgs_news(_SAMPLE_NEWS_RESULTS)  # type: ignore[assignment]
        data = _parse(await p.search("bitcoin", mode="news"))
        assert data["ok"] is True
        assert data["mode"] == "news"
        results = data["results"]
        assert results[0]["type"] == "news"
        assert "date" in results[0]

    @pytest.mark.anyio()
    async def test_news_no_results(self) -> None:
        p = WebIntelligencePlugin()
        p._backend.search_news = AsyncMock(return_value=[])  # type: ignore[assignment]
        data = _parse(await p.search("xyznonexistent", mode="news"))
        assert data["ok"] is False


class TestSearchTimeout:
    """Tests for search timeout handling."""

    @pytest.mark.anyio()
    async def test_timeout(self) -> None:
        p = WebIntelligencePlugin()

        async def slow_search(*_: object, **__: object) -> list[SearchResult]:
            await asyncio.sleep(0.5)
            return []

        p._backend.search_text = slow_search  # type: ignore[assignment]
        with patch.object(_web_mod, "_SEARCH_TIMEOUT", 0.05):
            data = _parse(await p.search("test"))
        assert data["ok"] is False
        assert "timed out" in str(data["message"])


class TestSearchRateLimit:
    """Tests for rate limiting."""

    @pytest.mark.anyio()
    async def test_rate_limit_exceeded(self) -> None:
        p = WebIntelligencePlugin()
        p._rate_limiter = _RateLimiter(1, 60.0)
        p._backend.search_text = _mock_ddgs_text(_SAMPLE_WEB_RESULTS)  # type: ignore[assignment]
        # First call OK
        data1 = _parse(await p.search("test1"))
        assert data1["ok"] is True
        # Second call rate limited
        data2 = _parse(await p.search("test2"))
        assert data2["ok"] is False
        assert "rate limit" in str(data2["message"])


# ── DuckDuckGo Backend ──


class TestDuckDuckGoBackend:
    """Tests for DuckDuckGoBackend."""

    @pytest.mark.anyio()
    async def test_search_text_returns_results(self) -> None:
        backend = DuckDuckGoBackend()
        mock_results = [
            {"title": "Test", "href": "https://example.com", "body": "A test"},
        ]
        with patch("ddgs.DDGS") as MockDDGS:
            mock_instance = MockDDGS.return_value.__enter__.return_value
            mock_instance.text.return_value = mock_results
            results = await backend.search_text("test", 5)

        assert len(results) == 1
        assert results[0].title == "Test"
        assert results[0].url == "https://example.com"

    @pytest.mark.anyio()
    async def test_search_news_returns_results(self) -> None:
        backend = DuckDuckGoBackend()
        mock_results = [
            {
                "title": "News",
                "url": "https://reuters.com/article",
                "body": "Breaking",
                "source": "Reuters",
                "date": "2026-04-12",
            },
        ]
        with patch("ddgs.DDGS") as MockDDGS:
            mock_instance = MockDDGS.return_value.__enter__.return_value
            mock_instance.news.return_value = mock_results
            results = await backend.search_news("test", 5)

        assert len(results) == 1
        assert results[0].result_type == "news"
        assert results[0].source == "Reuters"

    @pytest.mark.anyio()
    async def test_ddgs_not_installed(self) -> None:
        backend = DuckDuckGoBackend()
        with (
            patch.dict("sys.modules", {"ddgs": None}),
            patch(
                "sovyx.plugins.official.web_intelligence.DuckDuckGoBackend.search_text",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            results = await backend.search_text("test", 5)
        assert results == []


# ── SearchBackend Interface ──


class TestSearchBackendInterface:
    """Tests for abstract SearchBackend."""

    @pytest.mark.anyio()
    async def test_abstract_search_text(self) -> None:
        backend = SearchBackend()
        with pytest.raises(NotImplementedError):
            await backend.search_text("test", 5)

    @pytest.mark.anyio()
    async def test_abstract_search_news(self) -> None:
        backend = SearchBackend()
        with pytest.raises(NotImplementedError):
            await backend.search_news("test", 5)


import asyncio  # noqa: E402 — needed for TestSearchTimeout

# ── SearXNG Backend (TASK-497) ──
from sovyx.plugins.official.web_intelligence import (
    BraveBackend,
    SearXNGBackend,
    _create_backend,
)


class TestSearXNGBackend:
    """Tests for SearXNG search backend."""

    @pytest.mark.anyio()
    async def test_search_text(self) -> None:
        backend = SearXNGBackend("https://search.example.com")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "results": [
                {
                    "title": "Python Docs",
                    "url": "https://docs.python.org",
                    "content": "Official Python documentation.",
                },
                {
                    "title": "Real Python",
                    "url": "https://realpython.com",
                    "content": "Python tutorials.",
                },
            ],
        }

        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            # SandboxedHttpClient routes through ._client.request(...)
            mock_client.request = AsyncMock(return_value=mock_resp)
            mock_client.aclose = AsyncMock()
            MockClient.return_value = mock_client

            results = await backend.search_text("python", 5)

        assert len(results) == 2
        assert results[0].title == "Python Docs"
        assert results[0].source == "docs.python.org"

    @pytest.mark.anyio()
    async def test_search_news(self) -> None:
        backend = SearXNGBackend("https://search.example.com")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "results": [
                {
                    "title": "Breaking News",
                    "url": "https://reuters.com/article",
                    "content": "Something happened.",
                    "publishedDate": "2026-04-12",
                },
            ],
        }

        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            # SandboxedHttpClient routes through ._client.request(...)
            mock_client.request = AsyncMock(return_value=mock_resp)
            mock_client.aclose = AsyncMock()
            MockClient.return_value = mock_client

            results = await backend.search_news("news", 5)

        assert len(results) == 1
        assert results[0].result_type == "news"
        assert results[0].date == "2026-04-12"

    @pytest.mark.anyio()
    async def test_api_error(self) -> None:
        backend = SearXNGBackend("https://search.example.com")
        mock_resp = MagicMock()
        mock_resp.status_code = 500

        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            # SandboxedHttpClient routes through ._client.request(...)
            mock_client.request = AsyncMock(return_value=mock_resp)
            mock_client.aclose = AsyncMock()
            MockClient.return_value = mock_client

            results = await backend.search_text("test", 5)

        assert results == []

    @pytest.mark.anyio()
    async def test_network_error(self) -> None:
        backend = SearXNGBackend("https://search.example.com")

        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.request = AsyncMock(side_effect=Exception("connection refused"))
            mock_client.aclose = AsyncMock()
            MockClient.return_value = mock_client

            results = await backend.search_text("test", 5)

        assert results == []

    @pytest.mark.anyio()
    async def test_max_results_respected(self) -> None:
        backend = SearXNGBackend("https://search.example.com")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "results": [
                {"title": f"Result {i}", "url": f"https://example.com/{i}", "content": "..."}
                for i in range(10)
            ],
        }

        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            # SandboxedHttpClient routes through ._client.request(...)
            mock_client.request = AsyncMock(return_value=mock_resp)
            mock_client.aclose = AsyncMock()
            MockClient.return_value = mock_client

            results = await backend.search_text("test", 3)

        assert len(results) == 3


# ── Brave Backend (TASK-497) ──


class TestBraveBackend:
    """Tests for Brave search backend."""

    @pytest.mark.anyio()
    async def test_search_text(self) -> None:
        backend = BraveBackend("test-api-key")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "web": {
                "results": [
                    {
                        "title": "Example",
                        "url": "https://example.com",
                        "description": "An example site.",
                    },
                ],
            },
        }

        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            # SandboxedHttpClient routes through ._client.request(...)
            mock_client.request = AsyncMock(return_value=mock_resp)
            mock_client.aclose = AsyncMock()
            MockClient.return_value = mock_client

            results = await backend.search_text("test", 5)

        assert len(results) == 1
        assert results[0].title == "Example"
        # Verify API key was sent. SandboxedHttpClient calls the
        # underlying httpx client via .request("GET", url, headers=..., ...)
        call_kwargs = mock_client.request.call_args
        assert call_kwargs[1]["headers"]["X-Subscription-Token"] == "test-api-key"

    @pytest.mark.anyio()
    async def test_search_news(self) -> None:
        backend = BraveBackend("test-key")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "news": {
                "results": [
                    {
                        "title": "Breaking",
                        "url": "https://reuters.com/art",
                        "description": "News.",
                        "age": "2h ago",
                    },
                ],
            },
        }

        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            # SandboxedHttpClient routes through ._client.request(...)
            mock_client.request = AsyncMock(return_value=mock_resp)
            mock_client.aclose = AsyncMock()
            MockClient.return_value = mock_client

            results = await backend.search_news("news", 5)

        assert len(results) == 1
        assert results[0].result_type == "news"

    @pytest.mark.anyio()
    async def test_api_error(self) -> None:
        backend = BraveBackend("key")
        mock_resp = MagicMock()
        mock_resp.status_code = 401

        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            # SandboxedHttpClient routes through ._client.request(...)
            mock_client.request = AsyncMock(return_value=mock_resp)
            mock_client.aclose = AsyncMock()
            MockClient.return_value = mock_client

            results = await backend.search_text("test", 5)

        assert results == []


# ── Backend Factory (TASK-497) ──


class TestCreateBackend:
    """Tests for _create_backend factory."""

    def test_default_duckduckgo(self) -> None:
        backend = _create_backend("duckduckgo")
        assert isinstance(backend, DuckDuckGoBackend)

    def test_unknown_defaults_to_ddg(self) -> None:
        backend = _create_backend("unknown")
        assert isinstance(backend, DuckDuckGoBackend)

    def test_searxng(self) -> None:
        backend = _create_backend("searxng", searxng_url="https://search.example.com")
        assert isinstance(backend, SearXNGBackend)

    def test_searxng_no_url_raises(self) -> None:
        with pytest.raises(ValueError, match="searxng_url"):
            _create_backend("searxng")

    def test_brave(self) -> None:
        backend = _create_backend("brave", brave_api_key="test-key")
        assert isinstance(backend, BraveBackend)

    def test_brave_no_key_raises(self) -> None:
        with pytest.raises(ValueError, match="brave_api_key"):
            _create_backend("brave")


# ── Fetch Tool + Content Extraction (TASK-498) ──

from sovyx.plugins.official.web_intelligence import (
    _extract_content,
    _extract_fallback,
    _validate_url,
)

_SAMPLE_HTML = """
<html>
<head><title>Test Article</title></head>
<body>
<article>
<h1>Understanding Python</h1>
<p>Python is a versatile programming language used in web development,
data science, artificial intelligence, and many other fields.</p>
<p>It was created by Guido van Rossum and first released in 1991.</p>
</article>
</body>
</html>
"""


class TestValidateUrl:
    """Tests for URL validation."""

    def test_valid_https(self) -> None:
        assert _validate_url("https://example.com/path") == ""

    def test_valid_http(self) -> None:
        assert _validate_url("http://example.com") == ""

    def test_no_scheme(self) -> None:
        assert _validate_url("example.com") != ""

    def test_file_scheme(self) -> None:
        result = _validate_url("file:///etc/passwd")
        assert "disallowed" in result

    def test_javascript_scheme(self) -> None:
        result = _validate_url("javascript:alert(1)")
        assert "disallowed" in result

    def test_private_ip_127(self) -> None:
        result = _validate_url("http://127.0.0.1/admin")
        assert "private" in result

    def test_private_ip_192(self) -> None:
        result = _validate_url("http://192.168.1.1/")
        assert "private" in result

    def test_private_ip_10(self) -> None:
        result = _validate_url("http://10.0.0.1/")
        assert "private" in result

    def test_localhost(self) -> None:
        result = _validate_url("http://localhost:8080/")
        assert "private" in result

    def test_empty(self) -> None:
        assert _validate_url("") != ""


class TestExtractContent:
    """Tests for content extraction."""

    def test_trafilatura_extraction(self) -> None:
        result = _extract_content(_SAMPLE_HTML, "https://example.com")
        assert "Python" in result["text"]
        assert result["title"] != "" or result["text"] != ""

    def test_fallback_extraction(self) -> None:
        result = _extract_fallback(_SAMPLE_HTML)
        assert "Python" in result["text"]
        assert result["title"] == "Test Article"

    def test_fallback_strips_scripts(self) -> None:
        html = "<html><body><script>alert('xss')</script><p>Clean text.</p></body></html>"
        result = _extract_fallback(html)
        assert "alert" not in result["text"]
        assert "Clean text" in result["text"]

    def test_fallback_strips_styles(self) -> None:
        html = "<html><body><style>body{color:red}</style><p>Content here.</p></body></html>"
        result = _extract_fallback(html)
        assert "color" not in result["text"]
        assert "Content" in result["text"]

    def test_empty_html(self) -> None:
        result = _extract_content("", "https://example.com")
        assert result["text"] == "" or result["text"].strip() == ""


class TestFetchTool:
    """Tests for fetch tool."""

    @pytest.mark.anyio()
    async def test_basic_fetch(self) -> None:
        p = WebIntelligencePlugin()
        with patch.object(
            _web_mod,
            "_fetch_html",
            new_callable=AsyncMock,
            return_value=_SAMPLE_HTML,
        ):
            data = _parse(await p.fetch("https://example.com/article"))
        assert data["ok"] is True
        assert "Python" in str(data["text"])
        assert data["truncated"] is False

    @pytest.mark.anyio()
    async def test_truncation(self) -> None:
        p = WebIntelligencePlugin()
        long_html = "<html><body><p>" + "x" * 5000 + "</p></body></html>"
        with patch.object(
            _web_mod,
            "_fetch_html",
            new_callable=AsyncMock,
            return_value=long_html,
        ):
            data = _parse(await p.fetch("https://example.com", max_chars=100))
        assert data["ok"] is True
        assert data["truncated"] is True
        assert len(str(data["text"])) <= 200  # 100 + "..."

    @pytest.mark.anyio()
    async def test_invalid_url(self) -> None:
        p = WebIntelligencePlugin()
        data = _parse(await p.fetch("not-a-url"))
        assert data["ok"] is False

    @pytest.mark.anyio()
    async def test_private_ip_blocked(self) -> None:
        p = WebIntelligencePlugin()
        data = _parse(await p.fetch("http://192.168.1.1/admin"))
        assert data["ok"] is False
        assert "private" in str(data["message"])

    @pytest.mark.anyio()
    async def test_file_scheme_blocked(self) -> None:
        p = WebIntelligencePlugin()
        data = _parse(await p.fetch("file:///etc/passwd"))
        assert data["ok"] is False

    @pytest.mark.anyio()
    async def test_fetch_failed(self) -> None:
        p = WebIntelligencePlugin()
        with patch.object(
            _web_mod,
            "_fetch_html",
            new_callable=AsyncMock,
            return_value=None,
        ):
            data = _parse(await p.fetch("https://example.com/404"))
        assert data["ok"] is False

    @pytest.mark.anyio()
    async def test_fetch_timeout(self) -> None:
        p = WebIntelligencePlugin()

        async def slow_fetch(_url: str) -> str | None:
            await asyncio.sleep(0.5)
            return "<html></html>"

        with (
            patch.object(_web_mod, "_fetch_html", side_effect=slow_fetch),
            patch.object(_web_mod, "_FETCH_TIMEOUT", 0.05),
        ):
            data = _parse(await p.fetch("https://example.com"))
        assert data["ok"] is False
        assert "timed out" in str(data["message"])

    @pytest.mark.anyio()
    async def test_metadata_in_output(self) -> None:
        p = WebIntelligencePlugin()
        with patch.object(
            _web_mod,
            "_fetch_html",
            new_callable=AsyncMock,
            return_value=_SAMPLE_HTML,
        ):
            data = _parse(await p.fetch("https://example.com/article"))
        assert "title" in data
        assert "author" in data
        assert "language" in data
        assert "char_count" in data


# ── Query Intent Classification (TASK-499) ──

from sovyx.plugins.official.web_intelligence import QueryIntent, classify_query


class TestClassifyQuery:
    """Tests for query intent classification."""

    def test_factual(self) -> None:
        intent = classify_query("What is Python?")
        assert intent.intent_type == "factual"
        assert intent.search_mode == "web"

    def test_temporal_today(self) -> None:
        intent = classify_query("what happened today in the stock market")
        assert intent.intent_type in ("temporal", "price")
        assert intent.search_mode == "news"
        assert intent.time_range == "day"
        assert intent.confidence >= 0.7

    def test_temporal_this_week(self) -> None:
        intent = classify_query("latest news this week")
        assert intent.search_mode == "news"
        assert intent.time_range == "week"

    def test_temporal_breaking(self) -> None:
        intent = classify_query("breaking news bitcoin")
        assert intent.intent_type in ("temporal", "price")
        assert intent.search_mode == "news"

    def test_price_bitcoin(self) -> None:
        intent = classify_query("Bitcoin price")
        assert intent.intent_type == "price"
        assert intent.time_range != ""

    def test_price_crypto(self) -> None:
        intent = classify_query("ETH crypto price today")
        assert intent.intent_type == "price"

    def test_price_stock(self) -> None:
        intent = classify_query("NASDAQ stock market")
        assert intent.intent_type in ("price", "temporal")

    def test_procedural_how_to(self) -> None:
        intent = classify_query("how to install Docker on Ubuntu")
        assert intent.intent_type == "procedural"
        assert intent.search_mode == "web"

    def test_procedural_tutorial(self) -> None:
        intent = classify_query("Python tutorial for beginners")
        assert intent.intent_type == "procedural"

    def test_portuguese_hoje(self) -> None:
        intent = classify_query("o que aconteceu hoje no Brasil")
        assert intent.search_mode == "news"
        assert intent.time_range == "day"

    def test_portuguese_como(self) -> None:
        intent = classify_query("como instalar Python no Windows")
        assert intent.intent_type == "procedural"

    def test_news_keyword(self) -> None:
        intent = classify_query("Fed inflation report")
        assert intent.intent_type in ("temporal", "price")
        assert intent.search_mode == "news"

    def test_confidence_range(self) -> None:
        """Confidence always in [0, 1]."""
        queries = [
            "test",
            "bitcoin price today",
            "how to code",
            "breaking news war",
            "what is AI",
            "agora",
        ]
        for q in queries:
            intent = classify_query(q)
            assert 0 <= intent.confidence <= 1, f"Bad confidence for '{q}'"


class TestQueryIntentSchema:
    """Tests for QueryIntent schema."""

    def test_to_dict(self) -> None:
        intent = QueryIntent(
            intent_type="temporal",
            search_mode="news",
            time_range="day",
            confidence=0.85,
        )
        d = intent.to_dict()
        assert d["intent_type"] == "temporal"
        assert d["search_mode"] == "news"
        assert d["time_range"] == "day"
        assert d["confidence"] == 0.85


class TestAutoModeSearch:
    """Tests for search with mode='auto'."""

    @pytest.mark.anyio()
    async def test_auto_routes_to_news(self) -> None:
        """Auto mode routes 'today' queries to news."""
        p = WebIntelligencePlugin()
        p._backend.search_news = _mock_ddgs_news(_SAMPLE_NEWS_RESULTS)  # type: ignore[assignment]
        data = _parse(await p.search("what happened today", mode="auto"))
        assert data["ok"] is True
        assert data["mode"] == "news"
        assert "intent" in data
        assert data["intent"]["intent_type"] == "temporal"

    @pytest.mark.anyio()
    async def test_auto_routes_to_web(self) -> None:
        """Auto mode routes factual queries to web."""
        p = WebIntelligencePlugin()
        p._backend.search_text = _mock_ddgs_text(_SAMPLE_WEB_RESULTS)  # type: ignore[assignment]
        data = _parse(await p.search("what is quantum computing", mode="auto"))
        assert data["ok"] is True
        assert data["mode"] == "web"

    @pytest.mark.anyio()
    async def test_auto_price_detected(self) -> None:
        """Price queries are classified as price intent."""
        p = WebIntelligencePlugin()
        p._backend.search_text = _mock_ddgs_text(_SAMPLE_WEB_RESULTS)  # type: ignore[assignment]
        p._backend.search_news = _mock_ddgs_news(_SAMPLE_NEWS_RESULTS)  # type: ignore[assignment]
        data = _parse(await p.search("bitcoin price", mode="auto"))
        assert data["ok"] is True
        assert data["intent"]["intent_type"] == "price"

    @pytest.mark.anyio()
    async def test_explicit_mode_no_intent(self) -> None:
        """Explicit mode='web' skips intent classification."""
        p = WebIntelligencePlugin()
        p._backend.search_text = _mock_ddgs_text(_SAMPLE_WEB_RESULTS)  # type: ignore[assignment]
        data = _parse(await p.search("bitcoin price today", mode="web"))
        assert data["ok"] is True
        assert "intent" not in data


# ── Source Credibility Scoring (TASK-500) ──

from sovyx.plugins.official.web_intelligence import CredibilityScore, score_credibility


class TestScoreCredibility:
    """Tests for source credibility scoring."""

    # Tier 1
    def test_arxiv(self) -> None:
        c = score_credibility("https://arxiv.org/abs/2301.1234")
        assert c.tier == "tier1"
        assert c.score >= 0.9

    def test_reuters(self) -> None:
        c = score_credibility("https://reuters.com/article/test")
        assert c.tier == "tier1"
        assert c.score >= 0.9

    def test_gov_br(self) -> None:
        c = score_credibility("https://gov.br/page")
        assert c.tier == "tier1"

    def test_who(self) -> None:
        c = score_credibility("https://who.int/news")
        assert c.tier == "tier1"

    # Tier 2
    def test_bbc(self) -> None:
        c = score_credibility("https://bbc.com/news/article")
        assert c.tier == "tier2"
        assert 0.7 <= c.score <= 0.9

    def test_bloomberg(self) -> None:
        c = score_credibility("https://bloomberg.com/news/test")
        assert c.tier == "tier2"

    def test_stackoverflow(self) -> None:
        c = score_credibility("https://stackoverflow.com/questions/123")
        assert c.tier == "tier2"

    def test_wikipedia(self) -> None:
        c = score_credibility("https://wikipedia.org/wiki/Python")
        assert c.tier == "tier2"

    def test_folha(self) -> None:
        c = score_credibility("https://folha.uol.com.br/article")
        assert c.tier == "tier2"

    # Tier 3
    def test_reddit(self) -> None:
        c = score_credibility("https://reddit.com/r/python")
        assert c.tier == "tier3"
        assert c.score <= 0.6

    def test_medium(self) -> None:
        c = score_credibility("https://medium.com/@user/article")
        assert c.tier == "tier3"

    def test_twitter(self) -> None:
        c = score_credibility("https://twitter.com/user/status/123")
        assert c.tier == "tier3"

    # Subdomain match
    def test_subdomain_tier1(self) -> None:
        c = score_credibility("https://news.reuters.com/article")
        assert c.tier == "tier1"
        assert c.score >= 0.85

    def test_subdomain_tier2(self) -> None:
        c = score_credibility("https://tech.bloomberg.com/article")
        assert c.tier == "tier2"

    # TLD heuristics
    def test_edu_tld(self) -> None:
        c = score_credibility("https://mit.edu/research")
        assert c.score >= 0.7
        assert any("trusted TLD" in r for r in c.reasons)

    def test_gov_tld(self) -> None:
        c = score_credibility("https://example.gov/data")
        assert c.score >= 0.7

    def test_low_trust_tld(self) -> None:
        c = score_credibility("https://sketchy.xyz/page")
        assert c.score <= 0.4
        assert any("low-trust" in r for r in c.reasons)

    def test_biz_tld(self) -> None:
        c = score_credibility("https://fake.biz/deal")
        assert c.score <= 0.4

    # Unknown
    def test_unknown_domain(self) -> None:
        c = score_credibility("https://random-blog-2026.com/post")
        assert c.tier == "unknown"
        assert 0.3 <= c.score <= 0.7

    # HTTPS bonus
    def test_https_bonus(self) -> None:
        c_https = score_credibility("https://unknown-site.com/page")
        c_http = score_credibility("http://unknown-site.com/page")
        assert c_https.score >= c_http.score

    # Schema
    def test_to_dict(self) -> None:
        c = CredibilityScore(
            score=0.85,
            tier="tier2",
            domain="bbc.com",
            reasons=["established publication"],
        )
        d = c.to_dict()
        assert d["score"] == 0.85
        assert d["tier"] == "tier2"
        assert d["domain"] == "bbc.com"
        assert isinstance(d["reasons"], list)

    # Score bounds
    def test_score_always_in_range(self) -> None:
        urls = [
            "https://arxiv.org/abs/123",
            "https://reddit.com/r/test",
            "https://random.xyz/page",
            "http://unknown.com",
            "https://mit.edu/cs",
            "https://fake.click/ad",
        ]
        for url in urls:
            c = score_credibility(url)
            assert 0 <= c.score <= 1, f"Bad score for {url}: {c.score}"


class TestCredibilityInSearchResults:
    """Credibility scores appear in search and fetch results."""

    @pytest.mark.anyio()
    async def test_search_results_have_credibility(self) -> None:
        p = WebIntelligencePlugin()
        p._backend.search_text = _mock_ddgs_text(_SAMPLE_WEB_RESULTS)  # type: ignore[assignment]
        data = _parse(await p.search("test query", mode="web"))
        assert data["ok"] is True
        for r in data["results"]:
            assert "credibility" in r
            assert "score" in r["credibility"]
            assert "tier" in r["credibility"]

    @pytest.mark.anyio()
    async def test_fetch_has_credibility(self) -> None:
        p = WebIntelligencePlugin()
        html = "<html><body><p>Content here.</p></body></html>"
        with patch.object(
            _web_mod,
            "_fetch_html",
            new_callable=AsyncMock,
            return_value=html,
        ):
            data = _parse(await p.fetch("https://reuters.com/article"))
        assert data["ok"] is True
        assert data["credibility"]["tier"] == "tier1"
        assert data["credibility"]["score"] >= 0.9


# ── Research Tool (TASK-501) ──


class TestResearchTool:
    """Tests for research tool."""

    @pytest.mark.anyio()
    async def test_basic_research(self) -> None:
        p = WebIntelligencePlugin()
        p._backend.search_text = _mock_ddgs_text(_SAMPLE_WEB_RESULTS)  # type: ignore[assignment]
        p._backend.search_news = _mock_ddgs_news(_SAMPLE_NEWS_RESULTS)  # type: ignore[assignment]

        with patch.object(
            _web_mod,
            "_fetch_html",
            new_callable=AsyncMock,
            return_value="<html><body><p>Detailed content about the topic.</p></body></html>",
        ):
            data = _parse(await p.research("test topic"))

        assert data["ok"] is True
        assert data["action"] == "research"
        assert data["source_count"] >= 1
        assert len(data["sources"]) >= 1
        assert len(data["citations"]) >= 1

    @pytest.mark.anyio()
    async def test_sources_have_citations(self) -> None:
        p = WebIntelligencePlugin()
        p._backend.search_text = _mock_ddgs_text(_SAMPLE_WEB_RESULTS)  # type: ignore[assignment]
        p._backend.search_news = _mock_ddgs_news(_SAMPLE_NEWS_RESULTS)  # type: ignore[assignment]

        with patch.object(
            _web_mod,
            "_fetch_html",
            new_callable=AsyncMock,
            return_value="<html><body><p>Content here.</p></body></html>",
        ):
            data = _parse(await p.research("test", max_sources=2))

        for source in data["sources"]:
            assert "citation" in source
            assert "title" in source
            assert "url" in source
            assert "credibility" in source
            assert "content" in source

    @pytest.mark.anyio()
    async def test_citations_format(self) -> None:
        p = WebIntelligencePlugin()
        p._backend.search_text = _mock_ddgs_text(_SAMPLE_WEB_RESULTS)  # type: ignore[assignment]
        p._backend.search_news = _mock_ddgs_news([])  # type: ignore[assignment]

        with patch.object(
            _web_mod,
            "_fetch_html",
            new_callable=AsyncMock,
            return_value=None,
        ):
            data = _parse(await p.research("test", max_sources=1))

        assert data["ok"] is True
        for cit in data["citations"]:
            assert cit.startswith("[")
            assert "—" in cit

    @pytest.mark.anyio()
    async def test_credibility_ranking(self) -> None:
        """Higher credibility sources should come first."""
        p = WebIntelligencePlugin()

        mixed_results = [
            SearchResult(
                title="Reddit Post",
                url="https://reddit.com/r/test",
                snippet="...",
                source="reddit.com",
                date="",
                result_type="web",
            ),
            SearchResult(
                title="Reuters Article",
                url="https://reuters.com/article",
                snippet="...",
                source="reuters.com",
                date="",
                result_type="web",
            ),
        ]
        p._backend.search_text = AsyncMock(return_value=mixed_results)  # type: ignore[assignment]
        p._backend.search_news = _mock_ddgs_news([])  # type: ignore[assignment]

        with patch.object(
            _web_mod,
            "_fetch_html",
            new_callable=AsyncMock,
            return_value=None,
        ):
            data = _parse(await p.research("test", max_sources=2))

        # Reuters (tier1) should be citation [1]
        assert data["sources"][0]["url"] == "https://reuters.com/article"

    @pytest.mark.anyio()
    async def test_dedup_urls(self) -> None:
        """Same URL in web and news should appear once."""
        p = WebIntelligencePlugin()
        same_results = [
            SearchResult(
                title="Same Article",
                url="https://reuters.com/same",
                snippet="...",
                source="reuters.com",
                date="",
                result_type="web",
            ),
        ]
        p._backend.search_text = AsyncMock(return_value=same_results)  # type: ignore[assignment]
        p._backend.search_news = AsyncMock(return_value=same_results)  # type: ignore[assignment]

        with patch.object(
            _web_mod,
            "_fetch_html",
            new_callable=AsyncMock,
            return_value=None,
        ):
            data = _parse(await p.research("test"))

        urls = [s["url"] for s in data["sources"]]
        assert len(urls) == len(set(urls))

    @pytest.mark.anyio()
    async def test_no_results(self) -> None:
        p = WebIntelligencePlugin()
        p._backend.search_text = _mock_ddgs_text([])  # type: ignore[assignment]
        p._backend.search_news = _mock_ddgs_news([])  # type: ignore[assignment]
        data = _parse(await p.research("obscure query"))
        assert data["ok"] is False

    @pytest.mark.anyio()
    async def test_empty_query(self) -> None:
        p = WebIntelligencePlugin()
        data = _parse(await p.research(""))
        assert data["ok"] is False

    @pytest.mark.anyio()
    async def test_max_sources_capped(self) -> None:
        p = WebIntelligencePlugin()
        many = [
            SearchResult(
                title=f"R{i}",
                url=f"https://site{i}.com/p",
                snippet="...",
                source=f"site{i}.com",
                date="",
                result_type="web",
            )
            for i in range(10)
        ]
        p._backend.search_text = AsyncMock(return_value=many)  # type: ignore[assignment]
        p._backend.search_news = _mock_ddgs_news([])  # type: ignore[assignment]

        with patch.object(
            _web_mod,
            "_fetch_html",
            new_callable=AsyncMock,
            return_value=None,
        ):
            data = _parse(await p.research("test", max_sources=2))

        assert len(data["sources"]) == 2

    @pytest.mark.anyio()
    async def test_fetch_failure_uses_snippet(self) -> None:
        """When fetch fails, source content falls back to snippet."""
        p = WebIntelligencePlugin()
        results = [
            SearchResult(
                title="Test",
                url="https://example.com/p",
                snippet="The snippet text",
                source="example.com",
                date="2026-01-01",
                result_type="web",
            ),
        ]
        p._backend.search_text = AsyncMock(return_value=results)  # type: ignore[assignment]
        p._backend.search_news = _mock_ddgs_news([])  # type: ignore[assignment]

        with patch.object(
            _web_mod,
            "_fetch_html",
            new_callable=AsyncMock,
            return_value=None,
        ):
            data = _parse(await p.research("test", max_sources=1))

        assert data["sources"][0]["content"] == "The snippet text"

    @pytest.mark.anyio()
    async def test_content_truncation(self) -> None:
        p = WebIntelligencePlugin()
        results = [
            SearchResult(
                title="Test",
                url="https://example.com/p",
                snippet="...",
                source="example.com",
                date="",
                result_type="web",
            ),
        ]
        p._backend.search_text = AsyncMock(return_value=results)  # type: ignore[assignment]
        p._backend.search_news = _mock_ddgs_news([])  # type: ignore[assignment]

        long_content = "x" * 5000
        html = f"<html><body><p>{long_content}</p></body></html>"
        with patch.object(
            _web_mod,
            "_fetch_html",
            new_callable=AsyncMock,
            return_value=html,
        ):
            data = _parse(await p.research("test", max_sources=1))

        content = str(data["sources"][0]["content"])
        assert len(content) <= 2100  # _MAX_EXTRACT_CHARS + "..."

    @pytest.mark.anyio()
    async def test_avg_credibility(self) -> None:
        p = WebIntelligencePlugin()
        results = [
            SearchResult(
                title="R1",
                url="https://reuters.com/a",
                snippet="...",
                source="reuters.com",
                date="",
                result_type="web",
            ),
        ]
        p._backend.search_text = AsyncMock(return_value=results)  # type: ignore[assignment]
        p._backend.search_news = _mock_ddgs_news([])  # type: ignore[assignment]

        with patch.object(
            _web_mod,
            "_fetch_html",
            new_callable=AsyncMock,
            return_value=None,
        ):
            data = _parse(await p.research("test", max_sources=1))

        assert data["avg_credibility"] >= 0.8

    @pytest.mark.anyio()
    async def test_include_news_false(self) -> None:
        """include_news=False skips news search."""
        p = WebIntelligencePlugin()
        p._backend.search_text = _mock_ddgs_text(_SAMPLE_WEB_RESULTS)  # type: ignore[assignment]
        news_called = False

        async def track_news(*_a: object, **_kw: object) -> list[SearchResult]:
            nonlocal news_called
            news_called = True
            return []

        p._backend.search_news = track_news  # type: ignore[assignment]

        with patch.object(
            _web_mod,
            "_fetch_html",
            new_callable=AsyncMock,
            return_value=None,
        ):
            data = _parse(await p.research("test", include_news=False))

        assert data["ok"] is True
        assert not news_called


# ── Brain Integration (TASK-502) ──


class TestLearnFromWeb:
    """Tests for learn_from_web tool."""

    @pytest.mark.anyio()
    async def test_learn_saves_to_brain(self) -> None:
        mock_brain = AsyncMock()
        mock_brain.learn = AsyncMock(return_value="concept-123")
        p = WebIntelligencePlugin(brain=mock_brain)
        data = _parse(
            await p.learn_from_web(
                name="US Tariffs 2025",
                content="New tariff policy announced.",
                url="https://reuters.com/article",
                author="Reuters Staff",
                date="2025-04-10",
            )
        )
        assert data["ok"] is True
        assert data["concept_id"] == "concept-123"
        assert data["provenance"]["url"] == "https://reuters.com/article"
        assert data["provenance"]["author"] == "Reuters Staff"
        assert data["provenance"]["source_type"] == "web"
        assert "retrieved_at" in data["provenance"]

    @pytest.mark.anyio()
    async def test_learn_credibility_sets_confidence(self) -> None:
        """Tier1 source → higher confidence."""
        mock_brain = AsyncMock()
        mock_brain.learn = AsyncMock(return_value="c-1")
        p = WebIntelligencePlugin(brain=mock_brain)
        data = _parse(
            await p.learn_from_web(
                name="Test",
                content="Content.",
                url="https://reuters.com/a",
            )
        )
        assert data["ok"] is True
        assert data["confidence"] >= 0.8

    @pytest.mark.anyio()
    async def test_learn_low_credibility(self) -> None:
        """Unknown source → lower confidence."""
        mock_brain = AsyncMock()
        mock_brain.learn = AsyncMock(return_value="c-2")
        p = WebIntelligencePlugin(brain=mock_brain)
        data = _parse(
            await p.learn_from_web(
                name="Test",
                content="Content.",
                url="https://random-blog.xyz/post",
            )
        )
        assert data["ok"] is True
        assert data["confidence"] <= 0.5

    @pytest.mark.anyio()
    async def test_learn_no_url(self) -> None:
        """No URL → default confidence 0.5."""
        mock_brain = AsyncMock()
        mock_brain.learn = AsyncMock(return_value="c-3")
        p = WebIntelligencePlugin(brain=mock_brain)
        data = _parse(await p.learn_from_web(name="Test", content="Content."))
        assert data["ok"] is True
        assert data["confidence"] == 0.5

    @pytest.mark.anyio()
    async def test_learn_no_brain(self) -> None:
        p = WebIntelligencePlugin()
        data = _parse(await p.learn_from_web(name="Test", content="Content."))
        assert data["ok"] is False
        assert "brain" in str(data["message"]).lower()

    @pytest.mark.anyio()
    async def test_learn_empty_name(self) -> None:
        mock_brain = AsyncMock()
        p = WebIntelligencePlugin(brain=mock_brain)
        data = _parse(await p.learn_from_web(name="", content="x"))
        assert data["ok"] is False

    @pytest.mark.anyio()
    async def test_learn_empty_content(self) -> None:
        mock_brain = AsyncMock()
        p = WebIntelligencePlugin(brain=mock_brain)
        data = _parse(await p.learn_from_web(name="x", content=""))
        assert data["ok"] is False

    @pytest.mark.anyio()
    async def test_learn_brain_error(self) -> None:
        mock_brain = AsyncMock()
        mock_brain.learn = AsyncMock(side_effect=Exception("db error"))
        p = WebIntelligencePlugin(brain=mock_brain)
        data = _parse(await p.learn_from_web(name="Test", content="Content."))
        assert data["ok"] is False

    @pytest.mark.anyio()
    async def test_provenance_has_credibility(self) -> None:
        mock_brain = AsyncMock()
        mock_brain.learn = AsyncMock(return_value="c-4")
        p = WebIntelligencePlugin(brain=mock_brain)
        data = _parse(
            await p.learn_from_web(
                name="Test",
                content="Content.",
                url="https://bbc.com/news/art",
            )
        )
        assert "credibility" in data["provenance"]
        assert data["provenance"]["credibility"]["tier"] == "tier2"


class TestRecallWeb:
    """Tests for recall_web tool."""

    @pytest.mark.anyio()
    async def test_recall_returns_concepts(self) -> None:
        mock_brain = AsyncMock()
        mock_brain.search = AsyncMock(
            return_value=[
                {
                    "id": "c-1",
                    "name": "Tariff Impact",
                    "content": "Tariffs increased by 25%.",
                    "category": "web_research",
                    "confidence": 0.8,
                    "score": 0.9,
                    "source": "plugin:web-intelligence",
                },
            ],
        )
        p = WebIntelligencePlugin(brain=mock_brain)
        data = _parse(await p.recall_web("tariffs"))
        assert data["ok"] is True
        assert data["count"] == 1
        assert data["concepts"][0]["name"] == "Tariff Impact"

    @pytest.mark.anyio()
    async def test_recall_empty(self) -> None:
        mock_brain = AsyncMock()
        mock_brain.search = AsyncMock(return_value=[])
        p = WebIntelligencePlugin(brain=mock_brain)
        data = _parse(await p.recall_web("nonexistent"))
        assert data["ok"] is True
        assert data["count"] == 0

    @pytest.mark.anyio()
    async def test_recall_no_brain(self) -> None:
        p = WebIntelligencePlugin()
        data = _parse(await p.recall_web("test"))
        assert data["ok"] is False

    @pytest.mark.anyio()
    async def test_recall_empty_query(self) -> None:
        mock_brain = AsyncMock()
        p = WebIntelligencePlugin(brain=mock_brain)
        data = _parse(await p.recall_web(""))
        assert data["ok"] is False

    @pytest.mark.anyio()
    async def test_recall_brain_error(self) -> None:
        mock_brain = AsyncMock()
        mock_brain.search = AsyncMock(side_effect=Exception("search failed"))
        p = WebIntelligencePlugin(brain=mock_brain)
        data = _parse(await p.recall_web("test"))
        assert data["ok"] is False


# ── Intelligent Cache (TASK-503) ──

from sovyx.plugins.official.web_intelligence import (
    _CacheEntry,
    _SearchCache,
)


class TestCacheEntry:
    """Tests for _CacheEntry."""

    def test_alive_within_ttl(self) -> None:
        entry = _CacheEntry("value", 60, "factual")
        assert entry.alive is True

    def test_expired(self) -> None:
        entry = _CacheEntry("value", 0, "factual")
        # TTL=0 → already expired
        time.sleep(0.01)
        assert entry.alive is False

    def test_hits_tracking(self) -> None:
        entry = _CacheEntry("value", 60, "factual")
        assert entry.hits == 0
        entry.hits += 1
        assert entry.hits == 1


class TestSearchCache:
    """Tests for _SearchCache."""

    def test_put_and_get(self) -> None:
        cache = _SearchCache()
        cache.put("bitcoin price", "web", '{"ok":true}', "price")
        result = cache.get("bitcoin price", "web")
        assert result == '{"ok":true}'

    def test_miss(self) -> None:
        cache = _SearchCache()
        assert cache.get("nonexistent", "web") is None

    def test_case_insensitive(self) -> None:
        cache = _SearchCache()
        cache.put("Bitcoin Price", "web", "v1", "price")
        assert cache.get("bitcoin price", "web") == "v1"

    def test_mode_separation(self) -> None:
        cache = _SearchCache()
        cache.put("test", "web", "web-result", "factual")
        cache.put("test", "news", "news-result", "temporal")
        assert cache.get("test", "web") == "web-result"
        assert cache.get("test", "news") == "news-result"

    def test_expired_returns_none(self) -> None:
        cache = _SearchCache()
        cache.put("test", "web", "value", "factual")
        # Manually expire
        key = cache._make_key("test", "web")
        cache._store[key].expires_at = 0
        assert cache.get("test", "web") is None

    def test_eviction_on_full(self) -> None:
        cache = _SearchCache(max_entries=3)
        cache.put("q1", "web", "v1", "factual")
        cache.put("q2", "web", "v2", "factual")
        cache.put("q3", "web", "v3", "factual")
        # q1, q2, q3 all have 0 hits
        # Adding q4 should evict one
        cache.put("q4", "web", "v4", "factual")
        assert len(cache._store) == 3

    def test_eviction_preserves_popular(self) -> None:
        cache = _SearchCache(max_entries=2)
        cache.put("popular", "web", "v1", "factual")
        cache.get("popular", "web")  # 1 hit
        cache.get("popular", "web")  # 2 hits
        cache.put("unpopular", "web", "v2", "factual")
        # Adding third should evict unpopular (0 hits)
        cache.put("new", "web", "v3", "factual")
        assert cache.get("popular", "web") == "v1"

    def test_clear(self) -> None:
        cache = _SearchCache()
        cache.put("q1", "web", "v1", "factual")
        cache.clear()
        assert cache.get("q1", "web") is None
        assert cache.stats["entries"] == 0

    def test_stats(self) -> None:
        cache = _SearchCache()
        cache.put("q1", "web", "v1", "factual")
        cache.get("q1", "web")  # hit
        cache.get("q2", "web")  # miss
        stats = cache.stats
        assert stats["hits"] == 1
        assert stats["misses"] == 1
        assert stats["entries"] == 1
        assert stats["hit_rate_pct"] == 50

    def test_ttl_by_intent(self) -> None:
        """Different intents get different TTLs."""
        cache = _SearchCache()
        cache.put("q1", "web", "v1", "price")
        cache.put("q2", "web", "v2", "procedural")
        k1 = cache._make_key("q1", "web")
        k2 = cache._make_key("q2", "web")
        # Price TTL (300s) should be less than procedural (86400s)
        ttl1 = cache._store[k1].expires_at - time.monotonic()
        ttl2 = cache._store[k2].expires_at - time.monotonic()
        assert ttl1 < ttl2


class TestSearchCacheIntegration:
    """Tests for cache integration in search tool."""

    @pytest.mark.anyio()
    async def test_second_call_cached(self) -> None:
        p = WebIntelligencePlugin()
        call_count = 0

        original_search = p._backend.search_text

        async def counting_search(q: str, n: int) -> list[SearchResult]:
            nonlocal call_count
            call_count += 1
            return await original_search(q, n)

        p._backend.search_text = _mock_ddgs_text(_SAMPLE_WEB_RESULTS)  # type: ignore[assignment]

        # First call
        data1 = _parse(await p.search("test query", mode="web"))
        assert data1["ok"] is True

        # Second call — should hit cache (no backend call)
        p._backend.search_text = counting_search  # type: ignore[assignment]
        data2 = _parse(await p.search("test query", mode="web"))
        assert data2["ok"] is True
        assert call_count == 0  # never called backend

    @pytest.mark.anyio()
    async def test_different_queries_not_cached(self) -> None:
        p = WebIntelligencePlugin()
        p._backend.search_text = _mock_ddgs_text(_SAMPLE_WEB_RESULTS)  # type: ignore[assignment]

        await p.search("query one", mode="web")
        # Clear the mock to track new calls
        p._backend.search_text = _mock_ddgs_text(_SAMPLE_WEB_RESULTS)  # type: ignore[assignment]
        data = _parse(await p.search("query two", mode="web"))
        assert data["ok"] is True


# ── Quick Lookup (TASK-504) ──


class TestLookupModeDetection:
    """Tests for auto mode detection."""

    def test_price_bitcoin(self) -> None:
        assert WebIntelligencePlugin._detect_lookup_mode("bitcoin price") == "price"

    def test_price_eth(self) -> None:
        assert WebIntelligencePlugin._detect_lookup_mode("eth value") == "price"

    def test_price_stock(self) -> None:
        assert WebIntelligencePlugin._detect_lookup_mode("NASDAQ stock") == "price"

    def test_convert_to(self) -> None:
        assert WebIntelligencePlugin._detect_lookup_mode("100 USD to BRL") == "convert"

    def test_convert_para(self) -> None:
        assert WebIntelligencePlugin._detect_lookup_mode("100 dólares para reais") == "convert"

    def test_define_what_is(self) -> None:
        assert WebIntelligencePlugin._detect_lookup_mode("what is kubernetes") == "define"

    def test_define_o_que(self) -> None:
        assert WebIntelligencePlugin._detect_lookup_mode("o que é blockchain") == "define"

    def test_default_define(self) -> None:
        assert WebIntelligencePlugin._detect_lookup_mode("Python programming") == "define"


class TestLookupTool:
    """Tests for lookup tool."""

    @pytest.mark.anyio()
    async def test_basic_lookup(self) -> None:
        p = WebIntelligencePlugin()
        p._backend.search_text = _mock_ddgs_text(_SAMPLE_WEB_RESULTS)  # type: ignore[assignment]
        data = _parse(await p.lookup("what is Python"))
        assert data["ok"] is True
        assert data["action"] == "lookup"
        assert data["mode"] == "define"
        assert "answer" in data
        assert "source" in data
        assert "url" in data

    @pytest.mark.anyio()
    async def test_price_lookup(self) -> None:
        p = WebIntelligencePlugin()
        p._backend.search_text = _mock_ddgs_text(_SAMPLE_WEB_RESULTS)  # type: ignore[assignment]
        data = _parse(await p.lookup("bitcoin price"))
        assert data["ok"] is True
        assert data["mode"] == "price"

    @pytest.mark.anyio()
    async def test_explicit_mode(self) -> None:
        p = WebIntelligencePlugin()
        p._backend.search_text = _mock_ddgs_text(_SAMPLE_WEB_RESULTS)  # type: ignore[assignment]
        data = _parse(await p.lookup("Python", mode="define"))
        assert data["ok"] is True
        assert data["mode"] == "define"

    @pytest.mark.anyio()
    async def test_lookup_cached(self) -> None:
        p = WebIntelligencePlugin()
        p._backend.search_text = _mock_ddgs_text(_SAMPLE_WEB_RESULTS)  # type: ignore[assignment]
        await p.lookup("test query")
        # Second call — should hit cache
        call_count = 0

        async def counting(*_a: object, **_kw: object) -> list[SearchResult]:
            nonlocal call_count
            call_count += 1
            return []

        p._backend.search_text = counting  # type: ignore[assignment]
        data = _parse(await p.lookup("test query"))
        assert data["ok"] is True
        assert call_count == 0

    @pytest.mark.anyio()
    async def test_lookup_no_results(self) -> None:
        p = WebIntelligencePlugin()
        p._backend.search_text = _mock_ddgs_text([])  # type: ignore[assignment]
        data = _parse(await p.lookup("xyznonexistent"))
        assert data["ok"] is False

    @pytest.mark.anyio()
    async def test_lookup_empty_query(self) -> None:
        p = WebIntelligencePlugin()
        data = _parse(await p.lookup(""))
        assert data["ok"] is False

    @pytest.mark.anyio()
    async def test_credibility_in_lookup(self) -> None:
        p = WebIntelligencePlugin()
        p._backend.search_text = _mock_ddgs_text(_SAMPLE_WEB_RESULTS)  # type: ignore[assignment]
        data = _parse(await p.lookup("test"))
        assert "credibility" in data
        assert "score" in data["credibility"]

    @pytest.mark.anyio()
    async def test_supporting_snippets(self) -> None:
        p = WebIntelligencePlugin()
        many_results = [
            {"title": f"R{i}", "url": f"https://site{i}.com/p", "snippet": f"Snippet {i}"}
            for i in range(3)
        ]
        p._backend.search_text = _mock_ddgs_text(many_results)  # type: ignore[assignment]
        data = _parse(await p.lookup("test"))
        assert data["ok"] is True
        assert len(data["supporting_snippets"]) >= 1


# ── Weather Backward Compat (TASK-505) ──

from sovyx.plugins.official.web_intelligence import _extract_city


class TestExtractCity:
    """Tests for city extraction from weather queries."""

    def test_simple(self) -> None:
        assert _extract_city("weather in São Paulo") == "São Paulo"

    def test_pt_br(self) -> None:
        assert _extract_city("clima em Sorocaba") == "Sorocaba"

    def test_forecast(self) -> None:
        result = _extract_city("forecast Berlin")
        assert "Berlin" in result

    def test_rain(self) -> None:
        result = _extract_city("will it rain Tokyo")
        assert "Tokyo" in result

    def test_temperature(self) -> None:
        result = _extract_city("temperature New York")
        assert "New" in result and "York" in result

    def test_empty_strips_all(self) -> None:
        result = _extract_city("weather forecast")
        assert result != ""


class TestWeatherMode:
    """Tests for weather mode in lookup."""

    def test_auto_detects_weather(self) -> None:
        assert WebIntelligencePlugin._detect_lookup_mode("weather in Berlin") == "weather"

    def test_auto_detects_temperatura(self) -> None:
        assert WebIntelligencePlugin._detect_lookup_mode("temperatura São Paulo") == "weather"

    def test_auto_detects_chuva(self) -> None:
        assert WebIntelligencePlugin._detect_lookup_mode("vai chover hoje") == "weather"

    def test_auto_detects_forecast(self) -> None:
        assert WebIntelligencePlugin._detect_lookup_mode("forecast London") == "weather"

    @pytest.mark.anyio()
    async def test_weather_lookup_returns_structured(self) -> None:
        """Weather lookup returns structured data."""
        p = WebIntelligencePlugin()

        async def mock_weather(_query: str) -> str:
            return json.dumps(
                {
                    "ok": True,
                    "action": "lookup",
                    "mode": "weather",
                    "city": "Berlin",
                    "answer": "Clear sky, 20°C",
                    "source": "Open-Meteo",
                }
            )

        p._weather_lookup = mock_weather  # type: ignore[assignment]
        data = _parse(await p.lookup("weather Berlin"))
        assert data["ok"] is True
        assert data["mode"] == "weather"

    @pytest.mark.anyio()
    async def test_weather_explicit_mode(self) -> None:
        p = WebIntelligencePlugin()

        async def mock_weather(_query: str) -> str:
            return json.dumps(
                {
                    "ok": True,
                    "action": "lookup",
                    "mode": "weather",
                    "answer": "Cloudy, 15°C",
                }
            )

        p._weather_lookup = mock_weather  # type: ignore[assignment]
        data = _parse(await p.lookup("Berlin", mode="weather"))
        assert data["ok"] is True

    @pytest.mark.anyio()
    async def test_weather_fallback_to_search(self) -> None:
        """When Open-Meteo import fails, falls back to web search."""
        p = WebIntelligencePlugin()
        p._backend.search_text = _mock_ddgs_text(_SAMPLE_WEB_RESULTS)  # type: ignore[assignment]

        async def failing_weather(_query: str) -> str:
            # Simulate import failure fallback — search via web
            if not p._rate_limiter.check():
                return json.dumps({"ok": False, "message": "rate limit"})
            results = await p._backend.search_text(f"{_query} weather", 3)
            if not results:
                return json.dumps({"ok": False, "message": "no results"})
            r = results[0]
            return json.dumps(
                {
                    "ok": True,
                    "action": "lookup",
                    "mode": "weather",
                    "answer": r.snippet,
                    "source": "web search",
                }
            )

        p._weather_lookup = failing_weather  # type: ignore[assignment]
        data = _parse(await p.lookup("Berlin", mode="weather"))
        assert data["ok"] is True


# ── Safety & Input Validation (TASK-506) ──

from sovyx.plugins.official.web_intelligence import _sanitize_query


class TestSanitizeQuery:
    """Tests for query sanitization."""

    def test_strips_control_chars(self) -> None:
        assert _sanitize_query("hello\x00world") == "helloworld"

    def test_strips_tabs(self) -> None:
        assert _sanitize_query("hello\tworld") == "helloworld"

    def test_preserves_newlines_but_normalizes(self) -> None:
        # Newlines become spaces in the collapse
        result = _sanitize_query("hello\nworld")
        assert result == "hello world"

    def test_collapses_whitespace(self) -> None:
        assert _sanitize_query("hello    world") == "hello world"

    def test_strips_edges(self) -> None:
        assert _sanitize_query("  hello  ") == "hello"

    def test_unicode_preserved(self) -> None:
        assert _sanitize_query("São Paulo") == "São Paulo"

    def test_empty(self) -> None:
        assert _sanitize_query("") == ""

    def test_only_control_chars(self) -> None:
        assert _sanitize_query("\x00\x01\x02") == ""


class TestFetchRateLimit:
    """Tests for fetch rate limiting."""

    @pytest.mark.anyio()
    async def test_fetch_rate_limited(self) -> None:
        p = WebIntelligencePlugin()
        # Exhaust fetch limiter
        for _ in range(20):
            p._fetch_limiter.check()
        data = _parse(await p.fetch("https://example.com"))
        assert data["ok"] is False
        assert "rate limit" in str(data["message"])


class TestResearchRateLimit:
    """Tests for research rate limiting."""

    @pytest.mark.anyio()
    async def test_research_rate_limited(self) -> None:
        p = WebIntelligencePlugin()
        # Exhaust research limiter
        for _ in range(5):
            p._research_limiter.check()
        data = _parse(await p.research("test query"))
        assert data["ok"] is False
        assert "rate limit" in str(data["message"])


class TestSafetyIntegration:
    """Integration tests for safety features."""

    @pytest.mark.anyio()
    async def test_search_control_chars_sanitized(self) -> None:
        p = WebIntelligencePlugin()
        p._backend.search_text = _mock_ddgs_text(_SAMPLE_WEB_RESULTS)  # type: ignore[assignment]
        # Control chars should be stripped, not cause errors
        data = _parse(await p.search("test\x00query", mode="web"))
        assert data["ok"] is True

    @pytest.mark.anyio()
    async def test_fetch_private_ip_blocked(self) -> None:
        p = WebIntelligencePlugin()
        for ip in ["http://127.0.0.1", "http://10.0.0.1", "http://192.168.1.1"]:
            data = _parse(await p.fetch(ip))
            assert data["ok"] is False, f"Should block {ip}"

    @pytest.mark.anyio()
    async def test_fetch_file_scheme_blocked(self) -> None:
        p = WebIntelligencePlugin()
        data = _parse(await p.fetch("file:///etc/passwd"))
        assert data["ok"] is False

    @pytest.mark.anyio()
    async def test_search_query_too_long(self) -> None:
        p = WebIntelligencePlugin()
        data = _parse(await p.search("x" * 600, mode="web"))
        assert data["ok"] is False
        assert "too long" in str(data["message"])


# ── Config + i18n + Output Contract (TASK-507) ──


class TestConfigSchema:
    """Tests for config schema completeness."""

    def test_schema_has_all_keys(self) -> None:
        schema = WebIntelligencePlugin.config_schema
        props = schema["properties"]
        assert isinstance(props, dict)
        expected = {
            "backend",
            "searxng_url",
            "brave_api_key",
            "default_max_results",
            "fetch_max_chars",
            "cache_enabled",
            "cache_max_size",
            "auto_learn",
            "timeouts",
        }
        assert expected == set(props.keys())

    def test_schema_has_descriptions(self) -> None:
        schema = WebIntelligencePlugin.config_schema
        props = schema["properties"]
        assert isinstance(props, dict)
        for key, spec in props.items():
            assert isinstance(spec, dict)
            assert "description" in spec, f"Missing description for {key}"

    def test_backend_enum(self) -> None:
        schema = WebIntelligencePlugin.config_schema
        props = schema["properties"]
        assert isinstance(props, dict)
        backend = props["backend"]
        assert isinstance(backend, dict)
        assert backend["enum"] == ["duckduckgo", "searxng", "brave"]


class TestOutputContract:
    """All tools return {ok, action, result, message}."""

    @pytest.mark.anyio()
    async def test_search_contract(self) -> None:
        p = WebIntelligencePlugin()
        p._backend.search_text = _mock_ddgs_text(_SAMPLE_WEB_RESULTS)  # type: ignore[assignment]
        data = _parse(await p.search("test", mode="web"))
        assert "ok" in data
        assert "action" in data
        assert "result" in data
        assert "message" in data

    @pytest.mark.anyio()
    async def test_fetch_contract(self) -> None:
        p = WebIntelligencePlugin()
        html = "<html><body><p>Test.</p></body></html>"
        with patch.object(
            _web_mod,
            "_fetch_html",
            new_callable=AsyncMock,
            return_value=html,
        ):
            data = _parse(await p.fetch("https://example.com"))
        assert "ok" in data
        assert "action" in data
        assert "result" in data
        assert "message" in data

    @pytest.mark.anyio()
    async def test_research_contract(self) -> None:
        p = WebIntelligencePlugin()
        p._backend.search_text = _mock_ddgs_text(_SAMPLE_WEB_RESULTS)  # type: ignore[assignment]
        p._backend.search_news = _mock_ddgs_news(_SAMPLE_NEWS_RESULTS)  # type: ignore[assignment]
        with patch.object(
            _web_mod,
            "_fetch_html",
            new_callable=AsyncMock,
            return_value=None,
        ):
            data = _parse(await p.research("test"))
        assert "ok" in data
        assert "action" in data
        assert "result" in data
        assert "message" in data

    @pytest.mark.anyio()
    async def test_lookup_contract(self) -> None:
        p = WebIntelligencePlugin()
        p._backend.search_text = _mock_ddgs_text(_SAMPLE_WEB_RESULTS)  # type: ignore[assignment]
        data = _parse(await p.lookup("what is Python"))
        assert "ok" in data
        assert "action" in data
        assert "result" in data
        assert "message" in data

    @pytest.mark.anyio()
    async def test_error_contract(self) -> None:
        """Error responses also have ok + message."""
        p = WebIntelligencePlugin()
        data = _parse(await p.search("", mode="web"))
        assert data["ok"] is False
        assert "message" in data


class TestMessageConstants:
    """All error messages use _MSG_ constants."""

    def test_constants_defined(self) -> None:
        from sovyx.plugins.official import web_intelligence as wi

        expected = [
            "_MSG_EMPTY_QUERY",
            "_MSG_QUERY_TOO_LONG",
            "_MSG_NO_RESULTS",
            "_MSG_SEARCH_FAILED",
            "_MSG_FETCH_FAILED",
            "_MSG_INVALID_URL",
            "_MSG_BACKEND_UNAVAILABLE",
            "_MSG_RATE_LIMIT_SEARCH",
            "_MSG_RATE_LIMIT_FETCH",
            "_MSG_RATE_LIMIT_RESEARCH",
            "_MSG_SEARCH_TIMEOUT",
            "_MSG_FETCH_TIMEOUT",
            "_MSG_RESEARCH_TIMEOUT",
            "_MSG_LOOKUP_TIMEOUT",
            "_MSG_EMPTY_NAME",
            "_MSG_EMPTY_CONTENT",
            "_MSG_WEATHER_ERROR",
            "_MSG_RATE_LIMIT",
        ]
        for name in expected:
            assert hasattr(wi, name), f"Missing constant: {name}"
            assert isinstance(getattr(wi, name), str)
