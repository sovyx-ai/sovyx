"""Tests for sovyx.cognitive.safety_classifier — LLM content safety classification.

Covers:
- Micro-prompt structure and response parsing
- SafetyVerdict dataclass behavior
- Model selection logic
- classify_content with mocked LLM router
- Timeout and error handling (fail-open)
- Multilingual classification (EN, PT, ES, FR, DE, RU, ZH, AR)
- Metrics recording
- Edge cases (empty input, huge input, malformed LLM responses)
- Batch classification with deduplication and concurrency control
- Cache statistics snapshot
- ClassificationCache LRU eviction and TTL expiry
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from sovyx.cognitive.safety_classifier import (
    _CLASSIFY_TIMEOUT_SEC,
    _PREFERRED_MODELS,
    _SYSTEM_PROMPT,
    SAFE_VERDICT,
    ClassificationCache,
    SafetyCategory,
    SafetyVerdict,
    _parse_llm_response,
    _select_model,
    batch_classify_content,
    classify_content,
    get_classification_cache,
)
from sovyx.llm.models import LLMResponse

# ── Fixtures ────────────────────────────────────────────────────────────


def _make_llm_response(content: str, model: str = "gpt-4o-mini") -> LLMResponse:
    """Create a minimal LLMResponse for testing."""
    return LLMResponse(
        content=content,
        model=model,
        tokens_in=30,
        tokens_out=5,
        latency_ms=50,
        cost_usd=0.0001,
        finish_reason="stop",
        provider="openai",
    )


def _make_mock_router(
    response_content: str = "SAFE",
    available_models: list[str] | None = None,
    side_effect: Exception | None = None,
) -> MagicMock:
    """Create a mock LLMRouter with configurable behavior."""
    router = MagicMock()

    if available_models is None:
        available_models = ["gpt-4o-mini", "claude-sonnet-4-20250514"]

    # Mock providers
    provider = MagicMock()
    provider.is_available = True
    provider.name = "openai"

    router._providers = [provider]

    def get_provider_models(p: object) -> list[str]:  # noqa: ARG1
        return available_models

    router._get_provider_models = get_provider_models

    if side_effect:
        router.generate = AsyncMock(side_effect=side_effect)
    else:
        router.generate = AsyncMock(return_value=_make_llm_response(response_content))

    return router


# ── SafetyVerdict Tests ─────────────────────────────────────────────────


class TestSafetyVerdict:
    """Tests for the SafetyVerdict dataclass."""

    def test_safe_verdict_singleton(self) -> None:
        """SAFE_VERDICT is a pre-built safe result."""
        assert SAFE_VERDICT.safe is True
        assert SAFE_VERDICT.category is None
        assert SAFE_VERDICT.method == "pass"

    def test_verdict_is_frozen(self) -> None:
        """SafetyVerdict instances are immutable."""
        verdict = SafetyVerdict(safe=True)
        with pytest.raises(AttributeError):
            verdict.safe = False  # type: ignore[misc]

    def test_verdict_defaults(self) -> None:
        """Default values are sensible."""
        v = SafetyVerdict(safe=True)
        assert v.confidence == 1.0
        assert v.method == "llm"
        assert v.latency_ms == 0
        assert v.category is None

    def test_unsafe_verdict_with_category(self) -> None:
        """Unsafe verdict carries category info."""
        v = SafetyVerdict(
            safe=False,
            category=SafetyCategory.VIOLENCE,
            method="llm",
        )
        assert v.safe is False
        assert v.category == SafetyCategory.VIOLENCE


# ── SafetyCategory Tests ────────────────────────────────────────────────


class TestSafetyCategory:
    """Tests for the SafetyCategory enum."""

    def test_all_categories_exist(self) -> None:
        """All expected categories are defined."""
        expected = {
            "violence",
            "weapons",
            "self_harm",
            "hacking",
            "substance",
            "sexual",
            "gambling",
            "hate_speech",
            "manipulation",
            "illegal",
            "injection",
            "unknown",
        }
        actual = {c.value for c in SafetyCategory}
        assert actual == expected

    def test_category_from_string(self) -> None:
        """Categories can be constructed from their string values."""
        assert SafetyCategory("violence") == SafetyCategory.VIOLENCE
        assert SafetyCategory("self_harm") == SafetyCategory.SELF_HARM


# ── _parse_llm_response Tests ──────────────────────────────────────────


class TestParseLlmResponse:
    """Tests for the LLM response parser."""

    def test_parse_safe(self) -> None:
        """'SAFE' response parses correctly."""
        v = _parse_llm_response("SAFE")
        assert v.safe is True
        assert v.method == "llm"

    def test_parse_safe_lowercase(self) -> None:
        """Parser is case-insensitive."""
        v = _parse_llm_response("safe")
        assert v.safe is True

    def test_parse_safe_with_whitespace(self) -> None:
        """Parser strips whitespace."""
        v = _parse_llm_response("  SAFE  \n")
        assert v.safe is True

    def test_parse_unsafe_violence(self) -> None:
        """'UNSAFE|violence' parses with correct category."""
        v = _parse_llm_response("UNSAFE|violence")
        assert v.safe is False
        assert v.category == SafetyCategory.VIOLENCE
        assert v.method == "llm"

    def test_parse_unsafe_self_harm(self) -> None:
        """'UNSAFE|self_harm' parses correctly."""
        v = _parse_llm_response("UNSAFE|self_harm")
        assert v.safe is False
        assert v.category == SafetyCategory.SELF_HARM

    def test_parse_unsafe_all_categories(self) -> None:
        """Every valid category parses correctly."""
        for cat in SafetyCategory:
            if cat == SafetyCategory.UNKNOWN:
                continue
            v = _parse_llm_response(f"UNSAFE|{cat.value}")
            assert v.safe is False
            assert v.category == cat

    def test_parse_unsafe_unknown_category(self) -> None:
        """Unknown category falls back to UNKNOWN."""
        v = _parse_llm_response("UNSAFE|nonexistent_category")
        assert v.safe is False
        assert v.category == SafetyCategory.UNKNOWN

    def test_parse_unsafe_no_category(self) -> None:
        """'UNSAFE' without pipe defaults to UNKNOWN category."""
        v = _parse_llm_response("UNSAFE")
        assert v.safe is False
        assert v.category == SafetyCategory.UNKNOWN

    def test_parse_unsafe_case_insensitive(self) -> None:
        """Parser handles mixed case in unsafe responses."""
        v = _parse_llm_response("unsafe|Violence")
        assert v.safe is False
        assert v.category == SafetyCategory.VIOLENCE

    def test_parse_unparseable_fails_open(self) -> None:
        """Unparseable responses default to SAFE (fail-open)."""
        v = _parse_llm_response("I cannot classify this message.")
        assert v.safe is True
        assert v.method == "llm_unparseable"

    def test_parse_empty_string(self) -> None:
        """Empty string fails open."""
        v = _parse_llm_response("")
        assert v.safe is True
        assert v.method == "llm_unparseable"

    def test_parse_unsafe_extra_pipes(self) -> None:
        """Extra pipes are ignored — only first split matters."""
        v = _parse_llm_response("UNSAFE|violence|extra|stuff")
        assert v.safe is False
        # split("|", maxsplit=1) → category = "violence|extra|stuff"
        # SafetyCategory("violence|extra|stuff") → ValueError → UNKNOWN
        assert v.category == SafetyCategory.UNKNOWN

    def test_parse_unsafe_with_whitespace_in_category(self) -> None:
        """Whitespace in category is stripped."""
        v = _parse_llm_response("UNSAFE| violence ")
        assert v.safe is False
        assert v.category == SafetyCategory.VIOLENCE


# ── _select_model Tests ─────────────────────────────────────────────────


class TestSelectModel:
    """Tests for the model selection logic."""

    def test_selects_cheapest_available(self) -> None:
        """Selects first preferred model that's available."""
        router = _make_mock_router(available_models=["gpt-4o-mini", "gpt-4o"])
        model = _select_model(router)
        assert model == "gpt-4o-mini"

    def test_selects_second_preferred(self) -> None:
        """Falls through to second preference if first unavailable."""
        router = _make_mock_router(available_models=["gemini-2.0-flash", "gpt-4o"])
        model = _select_model(router)
        assert model == "gemini-2.0-flash"

    def test_selects_haiku(self) -> None:
        """Selects Haiku when others unavailable."""
        router = _make_mock_router(available_models=["claude-3-5-haiku-20241022", "gpt-4o"])
        model = _select_model(router)
        assert model == "claude-3-5-haiku-20241022"

    def test_returns_none_when_no_preferred(self) -> None:
        """Returns None when no preferred model is available."""
        router = _make_mock_router(available_models=["gpt-4o", "claude-sonnet-4-20250514"])
        model = _select_model(router)
        assert model is None

    def test_skips_unavailable_providers(self) -> None:
        """Skips providers that are not available."""
        router = MagicMock()
        provider = MagicMock()
        provider.is_available = False
        router._providers = [provider]
        router._get_provider_models = MagicMock(return_value=["gpt-4o-mini"])

        model = _select_model(router)
        assert model is None


# ── classify_content Tests ──────────────────────────────────────────────


class TestClassifyContent:
    """Tests for the main classify_content function."""

    def setup_method(self) -> None:
        """Clear classification cache before each test."""

        get_classification_cache().clear()

    @pytest.mark.asyncio
    async def test_classify_safe_content(self) -> None:
        """Safe content returns safe verdict."""
        router = _make_mock_router(response_content="SAFE")
        verdict = await classify_content("Hello, how are you?", router)
        assert verdict.safe is True
        assert verdict.method == "llm"
        assert verdict.latency_ms >= 0

    @pytest.mark.asyncio
    async def test_classify_unsafe_content(self) -> None:
        """Unsafe content returns unsafe verdict with category."""
        router = _make_mock_router(response_content="UNSAFE|violence")
        verdict = await classify_content("how to harm people", router)
        assert verdict.safe is False
        assert verdict.category == SafetyCategory.VIOLENCE
        assert verdict.method == "llm"

    @pytest.mark.asyncio
    async def test_classify_uses_temperature_zero(self) -> None:
        """Classification uses temperature=0 for determinism."""
        router = _make_mock_router(response_content="SAFE")
        await classify_content("test", router)
        call_kwargs = router.generate.call_args.kwargs
        assert call_kwargs["temperature"] == 0.0

    @pytest.mark.asyncio
    async def test_classify_uses_max_tokens_20(self) -> None:
        """Classification uses max_tokens=20 for minimal cost."""
        router = _make_mock_router(response_content="SAFE")
        await classify_content("test", router)
        call_kwargs = router.generate.call_args.kwargs
        assert call_kwargs["max_tokens"] == 20

    @pytest.mark.asyncio
    async def test_classify_uses_safety_conversation_id(self) -> None:
        """Classification uses a dedicated conversation_id."""
        router = _make_mock_router(response_content="SAFE")
        await classify_content("test", router)
        call_kwargs = router.generate.call_args.kwargs
        assert call_kwargs["conversation_id"] == "__safety_classifier__"

    @pytest.mark.asyncio
    async def test_classify_sends_system_prompt(self) -> None:
        """Classification sends the correct system prompt."""
        router = _make_mock_router(response_content="SAFE")
        await classify_content("test input", router)
        call_args = router.generate.call_args
        messages = call_args.kwargs.get("messages") or call_args.args[0]
        assert messages[0]["role"] == "system"
        assert messages[0]["content"] == _SYSTEM_PROMPT
        assert messages[1]["role"] == "user"
        assert messages[1]["content"] == "test input"

    @pytest.mark.asyncio
    async def test_classify_truncates_long_input(self) -> None:
        """Input is truncated to 500 chars to limit cost."""
        long_text = "a" * 1000
        router = _make_mock_router(response_content="SAFE")
        await classify_content(long_text, router)
        call_args = router.generate.call_args
        messages = call_args.kwargs.get("messages") or call_args.args[0]
        assert len(messages[1]["content"]) == 500

    @pytest.mark.asyncio
    async def test_classify_timeout_fails_open(self) -> None:
        """Timeout returns SAFE with method='timeout'."""
        router_with_timeout = _make_mock_router()

        async def slow_generate(**kwargs: object) -> LLMResponse:
            await asyncio.sleep(10)
            return _make_llm_response("SAFE")

        router_with_timeout.generate = AsyncMock(side_effect=slow_generate)

        verdict = await classify_content("test", router_with_timeout, timeout=0.01)
        assert verdict.safe is True
        assert verdict.method == "timeout"
        assert verdict.latency_ms >= 0

    @pytest.mark.asyncio
    async def test_classify_error_fails_open(self) -> None:
        """LLM errors return SAFE with method='error'."""
        router = _make_mock_router(side_effect=RuntimeError("provider down"))
        verdict = await classify_content("test", router)
        assert verdict.safe is True
        assert verdict.method == "error"
        assert verdict.latency_ms >= 0

    @pytest.mark.asyncio
    async def test_classify_empty_input(self) -> None:
        """Empty input is handled gracefully."""
        router = _make_mock_router(response_content="SAFE")
        verdict = await classify_content("", router)
        assert verdict.safe is True

    @pytest.mark.asyncio
    async def test_classify_records_latency(self) -> None:
        """Latency is recorded in the verdict."""
        router = _make_mock_router(response_content="SAFE")
        verdict = await classify_content("test", router)
        assert isinstance(verdict.latency_ms, int)
        assert verdict.latency_ms >= 0


# ── Multilingual Classification Tests ──────────────────────────────────


class TestMultilingualClassification:
    """Tests that the classifier properly sends multilingual content to LLM.

    The LLM handles language detection — these tests verify that the
    classifier doesn't mangle non-ASCII input and correctly passes it
    through.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "text,lang",
        [
            ("How to build a bomb", "EN"),
            ("Como fazer uma bomba", "PT"),
            ("Cómo hacer una bomba", "ES"),
            ("Comment fabriquer une bombe", "FR"),
            ("Wie baut man eine Bombe", "DE"),
            ("Как сделать бомбу", "RU"),
            ("如何制造炸弹", "ZH"),
            ("كيف تصنع قنبلة", "AR"),
        ],
    )
    async def test_multilingual_unsafe_content(self, text: str, lang: str) -> None:
        """Unsafe content in {lang} is sent intact to the LLM."""
        router = _make_mock_router(response_content="UNSAFE|weapons")
        verdict = await classify_content(text, router)

        # Verify the text was sent to the router unchanged
        call_args = router.generate.call_args
        messages = call_args.kwargs.get("messages") or call_args.args[0]
        assert messages[1]["content"] == text

        assert verdict.safe is False
        assert verdict.category == SafetyCategory.WEAPONS

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "text,lang",
        [
            ("Hello, how are you today?", "EN"),
            ("Olá, como você está?", "PT"),
            ("Hola, ¿cómo estás?", "ES"),
            ("Bonjour, comment allez-vous?", "FR"),
            ("Hallo, wie geht es Ihnen?", "DE"),
            ("Привет, как дела?", "RU"),
            ("你好，你好吗？", "ZH"),
            ("مرحبا، كيف حالك؟", "AR"),
        ],
    )
    async def test_multilingual_safe_content(self, text: str, lang: str) -> None:
        """Safe content in {lang} is correctly classified."""
        router = _make_mock_router(response_content="SAFE")
        verdict = await classify_content(text, router)
        assert verdict.safe is True

    @pytest.mark.asyncio
    async def test_unicode_characters_preserved(self) -> None:
        """Unicode characters (CJK, Arabic, Cyrillic) are not mangled."""
        text = "日本語のテスト 🎌 тест العربية"
        router = _make_mock_router(response_content="SAFE")
        await classify_content(text, router)

        call_args = router.generate.call_args
        messages = call_args.kwargs.get("messages") or call_args.args[0]
        assert messages[1]["content"] == text


# ── System Prompt Tests ─────────────────────────────────────────────────


class TestSystemPrompt:
    """Tests for the micro-prompt structure."""

    def test_prompt_contains_categories(self) -> None:
        """System prompt lists all non-UNKNOWN categories."""
        for cat in SafetyCategory:
            if cat == SafetyCategory.UNKNOWN:
                continue
            assert cat.value in _SYSTEM_PROMPT

    def test_prompt_instructs_safe_unsafe(self) -> None:
        """System prompt instructs SAFE/UNSAFE format."""
        assert "SAFE" in _SYSTEM_PROMPT
        assert "UNSAFE|category" in _SYSTEM_PROMPT

    def test_prompt_is_concise(self) -> None:
        """System prompt is under 200 tokens (~800 chars)."""
        # Rough heuristic: 1 token ≈ 4 chars
        assert len(_SYSTEM_PROMPT) < 800

    def test_prompt_forbids_explanation(self) -> None:
        """System prompt explicitly forbids extra text."""
        assert "No explanation" in _SYSTEM_PROMPT


# ── Constants Tests ─────────────────────────────────────────────────────


class TestConstants:
    """Tests for module-level constants."""

    def test_timeout_is_reasonable(self) -> None:
        """Timeout is between 1-5 seconds."""
        assert 1.0 <= _CLASSIFY_TIMEOUT_SEC <= 5.0

    def test_preferred_models_not_empty(self) -> None:
        """At least one preferred model is configured."""
        assert len(_PREFERRED_MODELS) > 0

    def test_preferred_models_are_cheap(self) -> None:
        """Preferred models are budget-tier (mini/flash/haiku)."""
        for model in _PREFERRED_MODELS:
            assert any(tier in model.lower() for tier in ("mini", "flash", "haiku")), (
                f"{model} doesn't look like a budget model"
            )


# ── Property-Based Tests ────────────────────────────────────────────────


class TestParseProperties:
    """Hypothesis-based tests for _parse_llm_response."""

    @given(st.text(min_size=0, max_size=200))
    @settings(max_examples=50)
    def test_parse_never_raises(self, text: str) -> None:
        """Parser never raises, regardless of input."""
        result = _parse_llm_response(text)
        assert isinstance(result, SafetyVerdict)
        assert isinstance(result.safe, bool)

    @given(st.sampled_from([c.value for c in SafetyCategory if c != SafetyCategory.UNKNOWN]))
    def test_parse_all_valid_categories(self, category: str) -> None:
        """All valid categories parse correctly from UNSAFE|cat."""
        result = _parse_llm_response(f"UNSAFE|{category}")
        assert result.safe is False
        assert result.category == SafetyCategory(category)

    @given(
        st.text(min_size=1, max_size=50).filter(
            lambda x: (
                x.strip().upper() not in ("SAFE",) and not x.strip().upper().startswith("UNSAFE")
            )
        )
    )
    def test_parse_garbage_fails_open(self, text: str) -> None:
        """Garbage input always fails open (safe=True)."""
        result = _parse_llm_response(text)
        assert result.safe is True


# ── Cache Tests (TASK-363) ──────────────────────────────────────────────


class TestClassificationCache:
    """Test ClassificationCache LRU with TTL."""

    def test_miss_returns_none(self) -> None:

        cache = ClassificationCache()
        assert cache.get("hello") is None

    def test_put_and_get(self) -> None:

        cache = ClassificationCache()
        v = SafetyVerdict(safe=True, method="llm")
        cache.put("hello world", v)
        result = cache.get("hello world")
        assert result is not None
        assert result.safe is True

    def test_same_prefix_same_key(self) -> None:
        """Text with same first 200 chars → same cache key."""

        cache = ClassificationCache()
        v = SafetyVerdict(safe=False, category=SafetyCategory.VIOLENCE, method="llm")
        base = "x" * 200
        cache.put(base + "aaaa", v)
        result = cache.get(base + "bbbb")
        assert result is not None
        assert result.safe is False

    def test_expired_entry_returns_none(self) -> None:

        cache = ClassificationCache(ttl_sec=0.001)
        v = SafetyVerdict(safe=True, method="llm")
        cache.put("test", v)
        import time

        time.sleep(0.01)
        assert cache.get("test") is None

    def test_lru_eviction(self) -> None:

        cache = ClassificationCache(max_size=2)
        v = SafetyVerdict(safe=True, method="llm")
        cache.put("a", v)
        cache.put("b", v)
        cache.put("c", v)  # Evicts "a"
        assert cache.get("a") is None
        assert cache.get("b") is not None
        assert cache.get("c") is not None
        assert cache.size == 2

    def test_hit_rate(self) -> None:

        cache = ClassificationCache()
        v = SafetyVerdict(safe=True, method="llm")
        cache.put("x", v)
        cache.get("x")  # hit
        cache.get("y")  # miss
        assert cache.hit_rate == pytest.approx(0.5)

    def test_hit_rate_no_lookups(self) -> None:

        cache = ClassificationCache()
        assert cache.hit_rate == 0.0

    def test_clear(self) -> None:

        cache = ClassificationCache()
        v = SafetyVerdict(safe=True, method="llm")
        cache.put("x", v)
        cache.clear()
        assert cache.size == 0
        assert cache.hit_rate == 0.0


class TestCacheIntegration:
    """Test cache integration in classify_content."""

    async def test_second_call_uses_cache(self) -> None:
        """Second classification with same text hits cache."""

        cache = get_classification_cache()
        cache.clear()

        router = _make_mock_router(response_content="SAFE")
        v1 = await classify_content("Hello world", router)
        v2 = await classify_content("Hello world", router)

        assert v1.method == "llm"
        assert v2.method == "cache"
        # LLM only called once
        assert router.generate.call_count == 1

    async def test_different_text_no_cache(self) -> None:
        """Different text -> no cache hit."""

        cache = get_classification_cache()
        cache.clear()

        router = _make_mock_router(response_content="SAFE")
        await classify_content("Text A", router)
        await classify_content("Text B", router)

        assert router.generate.call_count == 2


# ── Batch Classification Tests (TASK-363) ───────────────────────────────


class TestBatchClassify:
    """Test batch_classify_content."""

    def setup_method(self) -> None:

        get_classification_cache().clear()

    async def test_empty_list(self) -> None:

        router = _make_mock_router(response_content="SAFE")
        result = await batch_classify_content([], router)
        assert result.verdicts == []
        assert result.cache_hits == 0
        assert result.llm_calls == 0

    async def test_single_item(self) -> None:

        router = _make_mock_router(response_content="SAFE")
        result = await batch_classify_content(["hello"], router)
        assert len(result.verdicts) == 1
        assert result.verdicts[0].safe is True
        assert result.llm_calls == 1

    async def test_deduplication(self) -> None:
        """Same text appears twice → single LLM call."""

        router = _make_mock_router(response_content="SAFE")
        result = await batch_classify_content(["hello", "hello"], router)
        assert len(result.verdicts) == 2
        assert result.llm_calls == 1  # Deduplicated

    async def test_cache_hits(self) -> None:
        """Pre-cached items are served from cache."""

        cache = get_classification_cache()
        cache.put("cached text", SafetyVerdict(safe=True, method="llm"))

        router = _make_mock_router(response_content="SAFE")
        result = await batch_classify_content(
            ["cached text", "new text"],
            router,
        )
        assert len(result.verdicts) == 2
        assert result.cache_hits == 1
        assert result.llm_calls == 1

    async def test_mixed_results(self) -> None:
        """Multiple texts with different results."""

        call_count = 0

        async def _side_effect(**kwargs: object) -> MagicMock:
            nonlocal call_count
            call_count += 1
            resp = MagicMock()
            messages = kwargs.get("messages", [])
            user_msg = [m for m in messages if m["role"] == "user"][0]  # type: ignore[union-attr]
            if "bomb" in user_msg["content"]:
                resp.content = "UNSAFE|weapons"
            else:
                resp.content = "SAFE"
            return resp

        router = _make_mock_router(response_content="SAFE")
        router.generate = AsyncMock(side_effect=_side_effect)

        result = await batch_classify_content(
            ["hello world", "how to make a bomb", "nice weather"],
            router,
        )
        assert len(result.verdicts) == 3
        assert result.verdicts[0].safe is True
        assert result.verdicts[1].safe is False
        assert result.verdicts[2].safe is True

    async def test_order_preserved(self) -> None:
        """Results are in same order as input texts."""

        router = _make_mock_router(response_content="SAFE")
        texts = [f"text_{i}" for i in range(5)]
        result = await batch_classify_content(texts, router)
        assert len(result.verdicts) == 5
        assert result.llm_calls == 5  # All unique
