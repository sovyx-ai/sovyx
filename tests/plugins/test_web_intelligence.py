"""Tests for Sovyx Web Intelligence Plugin (TASK-496)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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
            await asyncio.sleep(20)
            return []

        p._backend.search_text = slow_search  # type: ignore[assignment]
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
            mock_client.get = AsyncMock(return_value=mock_resp)
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

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
            mock_client.get = AsyncMock(return_value=mock_resp)
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

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
            mock_client.get = AsyncMock(return_value=mock_resp)
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

            results = await backend.search_text("test", 5)

        assert results == []

    @pytest.mark.anyio()
    async def test_network_error(self) -> None:
        backend = SearXNGBackend("https://search.example.com")

        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=Exception("connection refused"))
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

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
            mock_client.get = AsyncMock(return_value=mock_resp)
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

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
            mock_client.get = AsyncMock(return_value=mock_resp)
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

            results = await backend.search_text("test", 5)

        assert len(results) == 1
        assert results[0].title == "Example"
        # Verify API key was sent
        call_kwargs = mock_client.get.call_args
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
            mock_client.get = AsyncMock(return_value=mock_resp)
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

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
            mock_client.get = AsyncMock(return_value=mock_resp)
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

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
        with patch(
            "sovyx.plugins.official.web_intelligence._fetch_html",
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
        with patch(
            "sovyx.plugins.official.web_intelligence._fetch_html",
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
        with patch(
            "sovyx.plugins.official.web_intelligence._fetch_html",
            new_callable=AsyncMock,
            return_value=None,
        ):
            data = _parse(await p.fetch("https://example.com/404"))
        assert data["ok"] is False

    @pytest.mark.anyio()
    async def test_fetch_timeout(self) -> None:
        p = WebIntelligencePlugin()

        async def slow_fetch(_url: str) -> str | None:
            await asyncio.sleep(20)
            return "<html></html>"

        with patch(
            "sovyx.plugins.official.web_intelligence._fetch_html",
            side_effect=slow_fetch,
        ):
            data = _parse(await p.fetch("https://example.com"))
        assert data["ok"] is False
        assert "timed out" in str(data["message"])

    @pytest.mark.anyio()
    async def test_metadata_in_output(self) -> None:
        p = WebIntelligencePlugin()
        with patch(
            "sovyx.plugins.official.web_intelligence._fetch_html",
            new_callable=AsyncMock,
            return_value=_SAMPLE_HTML,
        ):
            data = _parse(await p.fetch("https://example.com/article"))
        assert "title" in data
        assert "author" in data
        assert "language" in data
        assert "char_count" in data
