"""Property-based tests for Web Intelligence Plugin (TASK-508).

Uses Hypothesis to verify invariants across random inputs.
"""

from __future__ import annotations

import json

from hypothesis import given, settings
from hypothesis import strategies as st

from sovyx.plugins.official.web_intelligence import (
    SearchResult,
    _extract_domain,
    _sanitize_query,
    _SearchCache,
    _validate_url,
    classify_query,
    score_credibility,
)

# ── Strategies ──

_urls = st.sampled_from(
    [
        "https://arxiv.org/abs/1234",
        "https://reuters.com/article/test",
        "https://bbc.com/news/world",
        "https://reddit.com/r/python",
        "https://random-blog.xyz/post",
        "https://mit.edu/research",
        "https://example.gov/data",
        "https://medium.com/@user/article",
        "https://github.com/repo",
        "https://unknown-site.com/page",
        "http://sketchy.biz/deal",
        "https://stackoverflow.com/q/123",
        "https://folha.uol.com.br/noticia",
        "https://coindesk.com/price/btc",
    ]
)

_queries = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N", "Z")),
    min_size=1,
    max_size=100,
)

_search_queries = st.sampled_from(
    [
        "Bitcoin price today",
        "what is kubernetes",
        "how to install Docker",
        "breaking news war",
        "weather in São Paulo",
        "temperatura Sorocaba",
        "100 USD to BRL",
        "Python tutorial",
        "Fed inflation report",
        "o que é blockchain",
        "latest crypto news",
        "agora notícias",
        "define quantum computing",
        "preço ethereum",
        "vai chover hoje",
    ]
)

_modes = st.sampled_from(["web", "news", "auto"])
_intent_types = st.sampled_from(["factual", "temporal", "price", "procedural"])


# ── Credibility Invariants ──


class TestCredibilityProperties:
    """Property tests for credibility scoring."""

    @given(url=_urls)
    @settings(max_examples=50)
    def test_score_always_in_range(self, url: str) -> None:
        """Credibility score is always [0.0, 1.0]."""
        c = score_credibility(url)
        assert 0.0 <= c.score <= 1.0, f"Score {c.score} out of range for {url}"

    @given(url=_urls)
    @settings(max_examples=50)
    def test_tier_is_valid(self, url: str) -> None:
        """Tier is always one of the known values."""
        c = score_credibility(url)
        assert c.tier in {"tier1", "tier2", "tier3", "unknown"}

    @given(url=_urls)
    @settings(max_examples=50)
    def test_has_reasons(self, url: str) -> None:
        """Always has at least one reason."""
        c = score_credibility(url)
        assert len(c.reasons) >= 1

    @given(url=_urls)
    @settings(max_examples=50)
    def test_to_dict_roundtrip(self, url: str) -> None:
        """to_dict produces valid serializable dict."""
        c = score_credibility(url)
        d = c.to_dict()
        assert isinstance(d["score"], float)
        assert isinstance(d["tier"], str)
        assert isinstance(d["domain"], str)
        assert isinstance(d["reasons"], list)
        # Must be JSON-serializable
        json.dumps(d)

    @given(url=_urls)
    @settings(max_examples=30)
    def test_tier1_beats_tier3(self, url: str) -> None:
        """Tier1 score is always >= tier3 score."""
        c = score_credibility(url)
        if c.tier == "tier1":
            assert c.score >= 0.85
        elif c.tier == "tier3":
            assert c.score <= 0.60


# ── Query Classification Invariants ──


class TestClassifyQueryProperties:
    """Property tests for intent classification."""

    @given(query=_search_queries)
    @settings(max_examples=50)
    def test_confidence_in_range(self, query: str) -> None:
        """Confidence is always [0.0, 1.0]."""
        intent = classify_query(query)
        assert 0.0 <= intent.confidence <= 1.0

    @given(query=_search_queries)
    @settings(max_examples=50)
    def test_valid_intent_type(self, query: str) -> None:
        """Intent type is always valid."""
        intent = classify_query(query)
        assert intent.intent_type in {"factual", "temporal", "price", "procedural"}

    @given(query=_search_queries)
    @settings(max_examples=50)
    def test_valid_search_mode(self, query: str) -> None:
        """Search mode is always web or news."""
        intent = classify_query(query)
        assert intent.search_mode in {"web", "news"}

    @given(query=_search_queries)
    @settings(max_examples=50)
    def test_to_dict_serializable(self, query: str) -> None:
        """to_dict is always JSON-serializable."""
        intent = classify_query(query)
        json.dumps(intent.to_dict())


# ── Sanitization Invariants ──


class TestSanitizeProperties:
    """Property tests for query sanitization."""

    @given(query=st.text(min_size=0, max_size=200))
    @settings(max_examples=100)
    def test_no_control_chars_in_output(self, query: str) -> None:
        """Output never contains control characters (except newline/CR)."""
        result = _sanitize_query(query)
        for ch in result:
            code = ord(ch)
            if code < 32:
                assert code in (10, 13), f"Control char U+{code:04X} in output"

    @given(query=st.text(min_size=0, max_size=200))
    @settings(max_examples=100)
    def test_no_leading_trailing_whitespace(self, query: str) -> None:
        """Output is always stripped."""
        result = _sanitize_query(query)
        assert result == result.strip()

    @given(query=st.text(min_size=0, max_size=200))
    @settings(max_examples=100)
    def test_no_double_spaces(self, query: str) -> None:
        """No consecutive spaces in output."""
        result = _sanitize_query(query)
        assert "  " not in result

    @given(query=st.text(min_size=1, max_size=50))
    @settings(max_examples=50)
    def test_idempotent(self, query: str) -> None:
        """Sanitizing twice gives same result."""
        once = _sanitize_query(query)
        twice = _sanitize_query(once)
        assert once == twice


# ── Cache Invariants ──


class TestCacheProperties:
    """Property tests for search cache."""

    @given(
        query=_queries,
        mode=_modes,
        intent=_intent_types,
    )
    @settings(max_examples=50)
    def test_put_then_get(self, query: str, mode: str, intent: str) -> None:
        """Stored value is always retrievable."""
        cache = _SearchCache(max_entries=100)
        cache.put(query, mode, '{"ok":true}', intent)
        result = cache.get(query, mode)
        assert result == '{"ok":true}'

    @given(
        query=_queries,
        mode=_modes,
    )
    @settings(max_examples=50)
    def test_miss_returns_none(self, query: str, mode: str) -> None:
        """Empty cache always returns None."""
        cache = _SearchCache()
        assert cache.get(query, mode) is None

    @given(
        query=_search_queries,
        mode=_modes,
        intent=_intent_types,
    )
    @settings(max_examples=50)
    def test_case_insensitive(self, query: str, mode: str, intent: str) -> None:
        """Cache is case-insensitive for ASCII queries."""
        cache = _SearchCache()
        cache.put(query, mode, "value", intent)
        assert cache.get(query.upper(), mode) == "value"

    @given(data=st.data())
    @settings(max_examples=20)
    def test_max_entries_not_exceeded(self, data: st.DataObject) -> None:
        """Cache never exceeds max_entries."""
        max_e = data.draw(st.integers(min_value=5, max_value=20))
        cache = _SearchCache(max_entries=max_e)
        for i in range(max_e * 2):
            cache.put(f"q{i}", "web", f"v{i}", "factual")
        assert len(cache._store) <= max_e

    @given(intent=_intent_types)
    @settings(max_examples=20)
    def test_stats_consistency(self, intent: str) -> None:
        """Stats hits + misses = total lookups."""
        cache = _SearchCache()
        cache.put("q1", "web", "v1", intent)
        cache.get("q1", "web")  # hit
        cache.get("q2", "web")  # miss
        stats = cache.stats
        assert stats["hits"] + stats["misses"] == 2


# ── URL Validation Invariants ──


class TestUrlValidationProperties:
    """Property tests for URL validation."""

    @given(
        url=st.sampled_from(
            [
                "file:///etc/passwd",
                "ftp://server.com/file",
                "javascript:alert(1)",
                "data:text/html,<h1>hi</h1>",
            ]
        )
    )
    @settings(max_examples=20)
    def test_dangerous_schemes_blocked(self, url: str) -> None:
        """Dangerous URL schemes are always blocked."""
        result = _validate_url(url)
        assert result != "", f"Should block: {url}"

    @given(
        ip=st.sampled_from(
            [
                "http://127.0.0.1/admin",
                "http://10.0.0.1/",
                "http://192.168.1.1/",
                "http://172.16.0.1/",
                "http://localhost:8080/",
            ]
        )
    )
    @settings(max_examples=20)
    def test_private_ips_blocked(self, ip: str) -> None:
        """Private/internal IPs are always blocked."""
        result = _validate_url(ip)
        assert result != "", f"Should block: {ip}"

    @given(
        url=st.sampled_from(
            [
                "https://example.com",
                "https://bbc.com/news",
                "http://reuters.com/article",
            ]
        )
    )
    @settings(max_examples=10)
    def test_valid_urls_pass(self, url: str) -> None:
        """Valid public URLs pass validation."""
        result = _validate_url(url)
        assert result == "", f"Should pass: {url}"


# ── Domain Extraction Invariants ──


class TestDomainProperties:
    """Property tests for domain extraction."""

    @given(url=_urls)
    @settings(max_examples=30)
    def test_no_www_prefix(self, url: str) -> None:
        """Extracted domain never starts with www."""
        domain = _extract_domain(url)
        assert not domain.startswith("www.")

    @given(url=_urls)
    @settings(max_examples=30)
    def test_no_scheme_in_domain(self, url: str) -> None:
        """Extracted domain never contains ://."""
        domain = _extract_domain(url)
        assert "://" not in domain


# ── SearchResult Invariants ──


class TestSearchResultProperties:
    """Property tests for SearchResult."""

    @given(
        title=st.text(min_size=1, max_size=50),
        url=_urls,
        snippet=st.text(min_size=0, max_size=200),
    )
    @settings(max_examples=30)
    def test_to_dict_serializable(
        self,
        title: str,
        url: str,
        snippet: str,
    ) -> None:
        """SearchResult.to_dict is always JSON-serializable."""
        sr = SearchResult(title=title, url=url, snippet=snippet)
        d = sr.to_dict()
        json.dumps(d)
        assert d["title"] == title
        assert d["url"] == url
