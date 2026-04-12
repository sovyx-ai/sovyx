"""Smoke tests for Web Intelligence Plugin — REAL API calls.

These tests hit real APIs (DuckDuckGo, Open-Meteo, real URLs).
Run manually: pytest tests/smoke/test_web_intelligence_smoke.py -v --timeout=60

NOT part of CI — requires internet and may be flaky.
"""

from __future__ import annotations

import json

import pytest

from sovyx.plugins.official.web_intelligence import (
    WebIntelligencePlugin,
    _extract_city,
    _extract_content,
    _fetch_html,
)


def _parse(raw: str) -> dict:
    return json.loads(raw)


# ── Real Search ──


class TestRealSearch:
    """Search with real DuckDuckGo API."""

    @pytest.mark.anyio()
    async def test_web_search(self) -> None:
        p = WebIntelligencePlugin()
        data = _parse(await p.search("Python programming language", mode="web"))
        print(f"\n🔍 Web search: {data.get('count', 0)} results")
        assert data["ok"] is True, f"Failed: {data.get('message')}"
        assert data["count"] >= 1
        assert len(data["results"]) >= 1
        # Check result structure
        r = data["results"][0]
        assert r["title"], "Empty title"
        assert r["url"].startswith("http"), f"Bad URL: {r['url']}"
        assert "credibility" in r
        print(f"   Top: {r['title'][:60]} ({r['credibility']['tier']})")

    @pytest.mark.anyio()
    async def test_news_search(self) -> None:
        p = WebIntelligencePlugin()
        data = _parse(await p.search("technology news", mode="news"))
        print(f"\n📰 News search: {data.get('count', 0)} results")
        assert data["ok"] is True, f"Failed: {data.get('message')}"
        assert data["count"] >= 1
        r = data["results"][0]
        print(f"   Top: {r['title'][:60]}")

    @pytest.mark.anyio()
    async def test_auto_mode_factual(self) -> None:
        p = WebIntelligencePlugin()
        data = _parse(await p.search("what is quantum computing"))
        print(f"\n🤖 Auto (factual): mode={data.get('mode')}")
        assert data["ok"] is True
        assert data["mode"] == "web"
        if "intent" in data:
            print(f"   Intent: {data['intent']}")

    @pytest.mark.anyio()
    async def test_auto_mode_temporal(self) -> None:
        p = WebIntelligencePlugin()
        data = _parse(await p.search("breaking news today"))
        print(f"\n🤖 Auto (temporal): mode={data.get('mode')}")
        assert data["ok"] is True
        assert data["mode"] == "news"

    @pytest.mark.anyio()
    async def test_portuguese_query(self) -> None:
        p = WebIntelligencePlugin()
        data = _parse(await p.search("o que é inteligência artificial", mode="web"))
        print(f"\n🇧🇷 PT-BR search: {data.get('count', 0)} results")
        assert data["ok"] is True
        assert data["count"] >= 1


# ── Real Fetch ──


class TestRealFetch:
    """Fetch real URLs with trafilatura."""

    @pytest.mark.anyio()
    async def test_fetch_wikipedia(self) -> None:
        p = WebIntelligencePlugin()
        data = _parse(await p.fetch("https://en.wikipedia.org/wiki/Python_(programming_language)"))
        print(f"\n📄 Wikipedia: {data.get('char_count', 0)} chars")
        assert data["ok"] is True, f"Failed: {data.get('message')}"
        assert data["char_count"] > 100
        assert data["credibility"]["tier"] == "tier2"
        print(f"   Title: {data.get('title', 'N/A')[:60]}")

    @pytest.mark.anyio()
    async def test_fetch_reuters(self) -> None:
        p = WebIntelligencePlugin()
        data = _parse(await p.fetch("https://www.reuters.com"))
        print(f"\n📄 Reuters: {data.get('char_count', 0)} chars")
        # Reuters may block bots, so just check we don't crash
        if data["ok"]:
            assert data["credibility"]["tier"] == "tier1"
            print(f"   Title: {data.get('title', 'N/A')[:60]}")
        else:
            print(f"   Blocked/failed: {data.get('message')}")

    @pytest.mark.anyio()
    async def test_fetch_github(self) -> None:
        p = WebIntelligencePlugin()
        data = _parse(await p.fetch("https://github.com/sovyx-ai/sovyx"))
        print(f"\n📄 GitHub: ok={data['ok']}, chars={data.get('char_count', 0)}")
        # GitHub may return limited content, just verify no crash

    @pytest.mark.anyio()
    async def test_fetch_nonexistent(self) -> None:
        p = WebIntelligencePlugin()
        data = _parse(await p.fetch("https://thisdomaindoesnotexist12345.com"))
        print(f"\n📄 Nonexistent: ok={data['ok']}")
        assert data["ok"] is False

    @pytest.mark.anyio()
    async def test_raw_html_extraction(self) -> None:
        """Test trafilatura with a real page."""
        html = await _fetch_html("https://en.wikipedia.org/wiki/Sorocaba")
        if html:
            result = _extract_content(html, "https://en.wikipedia.org/wiki/Sorocaba")
            print(f"\n📄 Raw extraction: {len(result['text'])} chars")
            assert len(result["text"]) > 50, "Too little content extracted"
            print(f"   Title: {result['title'][:60]}")
        else:
            print("\n📄 Raw extraction: fetch failed (might be rate limited)")


# ── Real Lookup ──


class TestRealLookup:
    """Lookup with real APIs."""

    @pytest.mark.anyio()
    async def test_lookup_define(self) -> None:
        p = WebIntelligencePlugin()
        data = _parse(await p.lookup("what is blockchain"))
        print(f"\n📖 Define: ok={data['ok']}, mode={data.get('mode')}")
        assert data["ok"] is True
        assert data["answer"], "Empty answer"
        print(f"   Answer: {str(data['answer'])[:80]}")

    @pytest.mark.anyio()
    async def test_lookup_price(self) -> None:
        p = WebIntelligencePlugin()
        data = _parse(await p.lookup("Bitcoin price"))
        print(f"\n💰 Price: ok={data['ok']}, mode={data.get('mode')}")
        assert data["ok"] is True
        assert data["mode"] == "price"
        print(f"   Answer: {str(data['answer'])[:80]}")


# ── Real Weather ──


class TestRealWeather:
    """Weather with real Open-Meteo API."""

    @pytest.mark.anyio()
    async def test_weather_sorocaba(self) -> None:
        p = WebIntelligencePlugin()
        data = _parse(await p.lookup("weather in Sorocaba", mode="weather"))
        print(f"\n🌤️ Sorocaba: ok={data['ok']}")
        if data["ok"]:
            print(f"   {data.get('answer', 'N/A')}")
            assert "temperature_c" in data or "answer" in data
        else:
            print(f"   Failed: {data.get('message')}")

    @pytest.mark.anyio()
    async def test_weather_new_york(self) -> None:
        p = WebIntelligencePlugin()
        data = _parse(await p.lookup("temperature New York", mode="weather"))
        print(f"\n🌤️ New York: ok={data['ok']}")
        if data["ok"]:
            print(f"   {data.get('answer', 'N/A')}")

    @pytest.mark.anyio()
    async def test_weather_tokyo(self) -> None:
        p = WebIntelligencePlugin()
        data = _parse(await p.lookup("forecast Tokyo", mode="weather"))
        print(f"\n🌤️ Tokyo: ok={data['ok']}")
        if data["ok"]:
            print(f"   {data.get('answer', 'N/A')}")

    @pytest.mark.anyio()
    async def test_city_extraction_edge_cases(self) -> None:
        """Test _extract_city with tricky inputs."""
        cases = [
            ("weather in São Paulo", "São Paulo"),
            ("vai chover em São José dos Campos", None),  # check it doesn't crash
            ("temperatura Rio de Janeiro", None),
            ("forecast London tomorrow", None),
        ]
        for query, expected in cases:
            result = _extract_city(query)
            print(f"\n   '{query}' → '{result}'")
            assert result, f"Empty city for: {query}"
            if expected:
                assert expected.lower() in result.lower(), f"Expected '{expected}' in '{result}'"


# ── Real Research ──


class TestRealResearch:
    """Research with real APIs (slow — fetches multiple pages)."""

    @pytest.mark.anyio()
    async def test_research_topic(self) -> None:
        p = WebIntelligencePlugin()
        data = _parse(await p.research(
            "impact of artificial intelligence on healthcare",
            max_sources=2,
        ))
        print(f"\n🔬 Research: ok={data['ok']}, sources={data.get('source_count', 0)}")
        assert data["ok"] is True, f"Failed: {data.get('message')}"
        assert data["source_count"] >= 1
        assert len(data["citations"]) >= 1

        for s in data["sources"]:
            cred = s.get("credibility", {})
            content_len = len(str(s.get("content", "")))
            print(f"   [{s['citation']}] {s['title'][:50]} "
                  f"({cred.get('tier', '?')}, {content_len} chars)")

        print(f"   Avg credibility: {data.get('avg_credibility', 'N/A')}")
        print(f"   Citations: {data['citations'][:2]}")


# ── Summary ──


class TestSummary:
    """Print test summary."""

    @pytest.mark.anyio()
    async def test_plugin_info(self) -> None:
        p = WebIntelligencePlugin()
        print(f"\n{'='*60}")
        print(f"Plugin: {p.name} v{p.version}")
        print(f"Description: {p.description}")
        print(f"Cache stats: {p._cache.stats}")
        print(f"{'='*60}")
