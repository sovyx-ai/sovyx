"""Tests for LLM safety classifier (TASK-360).

Tests cover:
- Micro-prompt response parsing (SAFE, UNSAFE|category, edge cases)
- Model selection (cheapest available)
- classify_content with mock LLM (8 languages)
- Timeout handling (fail-open)
- Error handling (fail-open)
- Category mapping
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from sovyx.cognitive.safety_classifier import (
    SAFE_VERDICT,
    SafetyCategory,
    SafetyVerdict,
    _parse_llm_response,
    _select_model,
    classify_content,
)

# ── Response Parsing ────────────────────────────────────────────────────


class TestParseResponse:
    """Test _parse_llm_response with various LLM outputs."""

    def test_safe(self) -> None:
        v = _parse_llm_response("SAFE")
        assert v.safe is True
        assert v.category is None
        assert v.method == "llm"

    def test_safe_lowercase(self) -> None:
        v = _parse_llm_response("safe")
        assert v.safe is True

    def test_safe_whitespace(self) -> None:
        v = _parse_llm_response("  SAFE  \n")
        assert v.safe is True

    def test_unsafe_violence(self) -> None:
        v = _parse_llm_response("UNSAFE|violence")
        assert v.safe is False
        assert v.category == SafetyCategory.VIOLENCE

    def test_unsafe_weapons(self) -> None:
        v = _parse_llm_response("UNSAFE|weapons")
        assert v.safe is False
        assert v.category == SafetyCategory.WEAPONS

    def test_unsafe_self_harm(self) -> None:
        v = _parse_llm_response("UNSAFE|self_harm")
        assert v.safe is False
        assert v.category == SafetyCategory.SELF_HARM

    def test_unsafe_substance(self) -> None:
        v = _parse_llm_response("UNSAFE|substance")
        assert v.safe is False
        assert v.category == SafetyCategory.SUBSTANCE

    def test_unsafe_sexual(self) -> None:
        v = _parse_llm_response("UNSAFE|sexual")
        assert v.safe is False
        assert v.category == SafetyCategory.SEXUAL

    def test_unsafe_injection(self) -> None:
        v = _parse_llm_response("UNSAFE|injection")
        assert v.safe is False
        assert v.category == SafetyCategory.INJECTION

    def test_unsafe_hacking(self) -> None:
        v = _parse_llm_response("UNSAFE|hacking")
        assert v.safe is False
        assert v.category == SafetyCategory.HACKING

    def test_unsafe_hate_speech(self) -> None:
        v = _parse_llm_response("UNSAFE|hate_speech")
        assert v.safe is False
        assert v.category == SafetyCategory.HATE_SPEECH

    def test_unsafe_gambling(self) -> None:
        v = _parse_llm_response("UNSAFE|gambling")
        assert v.safe is False
        assert v.category == SafetyCategory.GAMBLING

    def test_unsafe_manipulation(self) -> None:
        v = _parse_llm_response("UNSAFE|manipulation")
        assert v.safe is False
        assert v.category == SafetyCategory.MANIPULATION

    def test_unsafe_illegal(self) -> None:
        v = _parse_llm_response("UNSAFE|illegal")
        assert v.safe is False
        assert v.category == SafetyCategory.ILLEGAL

    def test_unsafe_unknown_category(self) -> None:
        v = _parse_llm_response("UNSAFE|nonexistent_cat")
        assert v.safe is False
        assert v.category == SafetyCategory.UNKNOWN

    def test_unsafe_no_category(self) -> None:
        v = _parse_llm_response("UNSAFE")
        assert v.safe is False
        assert v.category == SafetyCategory.UNKNOWN

    def test_unsafe_lowercase(self) -> None:
        v = _parse_llm_response("unsafe|violence")
        assert v.safe is False
        assert v.category == SafetyCategory.VIOLENCE

    def test_unsafe_whitespace(self) -> None:
        v = _parse_llm_response("  UNSAFE | weapons  \n")
        assert v.safe is False
        assert v.category == SafetyCategory.WEAPONS

    def test_unparseable_fails_open(self) -> None:
        """Unparseable response -> SAFE (fail-open)."""
        v = _parse_llm_response("I think this is unsafe because...")
        assert v.safe is True
        assert v.method == "llm_unparseable"

    def test_empty_fails_open(self) -> None:
        v = _parse_llm_response("")
        assert v.safe is True
        assert v.method == "llm_unparseable"


# ── Model Selection ─────────────────────────────────────────────────────


class TestModelSelection:
    """Test _select_model picks cheapest available."""

    def _make_router(self, models: list[str]) -> MagicMock:
        router = MagicMock()
        provider = MagicMock()
        provider.is_available = True
        router._providers = [provider]
        router._get_provider_models = MagicMock(return_value=models)
        return router

    def test_picks_gpt4o_mini(self) -> None:
        router = self._make_router(["gpt-4o", "gpt-4o-mini", "o1"])
        assert _select_model(router) == "gpt-4o-mini"

    def test_picks_gemini_flash(self) -> None:
        router = self._make_router(
            ["gemini-2.0-flash", "gemini-2.5-pro"],
        )
        assert _select_model(router) == "gemini-2.0-flash"

    def test_picks_haiku(self) -> None:
        router = self._make_router(
            ["claude-sonnet-4-20250514", "claude-3-5-haiku-20241022"],
        )
        assert _select_model(router) == "claude-3-5-haiku-20241022"

    def test_no_preferred_returns_none(self) -> None:
        router = self._make_router(["some-unknown-model"])
        assert _select_model(router) is None

    def test_no_providers_returns_none(self) -> None:
        router = MagicMock()
        router._providers = []
        assert _select_model(router) is None

    def test_unavailable_provider_skipped(self) -> None:
        router = MagicMock()
        p1 = MagicMock()
        p1.is_available = False
        p2 = MagicMock()
        p2.is_available = True
        router._providers = [p1, p2]
        router._get_provider_models = MagicMock(
            side_effect=lambda p: ["gpt-4o-mini"] if p == p2 else [],
        )
        assert _select_model(router) == "gpt-4o-mini"


# ── Classification (mocked LLM) ────────────────────────────────────────


def _mock_router(response_text: str) -> MagicMock:
    """Create a mock LLM router that returns the given text."""
    router = MagicMock()
    response = MagicMock()
    response.content = response_text
    router.generate = AsyncMock(return_value=response)
    router._providers = []
    return router


class TestClassifyContent:
    """Test classify_content with various inputs and mock LLM."""

    async def test_safe_content(self) -> None:
        router = _mock_router("SAFE")
        v = await classify_content("Hello, how are you?", router)
        assert v.safe is True
        assert v.method == "llm"

    async def test_unsafe_content(self) -> None:
        router = _mock_router("UNSAFE|violence")
        v = await classify_content("How to kill someone", router)
        assert v.safe is False
        assert v.category == SafetyCategory.VIOLENCE

    # ── Multilingual: 8 languages ──

    async def test_english_unsafe(self) -> None:
        router = _mock_router("UNSAFE|weapons")
        v = await classify_content("How to make a bomb", router)
        assert v.safe is False
        assert v.category == SafetyCategory.WEAPONS

    async def test_portuguese_unsafe(self) -> None:
        router = _mock_router("UNSAFE|weapons")
        v = await classify_content("Como fazer uma bomba", router)
        assert v.safe is False

    async def test_spanish_unsafe(self) -> None:
        router = _mock_router("UNSAFE|weapons")
        v = await classify_content(
            "Cómo hacer una bomba",
            router,
        )
        assert v.safe is False

    async def test_french_unsafe(self) -> None:
        router = _mock_router("UNSAFE|weapons")
        v = await classify_content(
            "Comment fabriquer une bombe",
            router,
        )
        assert v.safe is False

    async def test_german_unsafe(self) -> None:
        router = _mock_router("UNSAFE|weapons")
        v = await classify_content(
            "Wie man eine Bombe baut",
            router,
        )
        assert v.safe is False

    async def test_russian_unsafe(self) -> None:
        router = _mock_router("UNSAFE|weapons")
        v = await classify_content(
            "Как сделать бомбу",
            router,
        )
        assert v.safe is False

    async def test_chinese_unsafe(self) -> None:
        router = _mock_router("UNSAFE|weapons")
        v = await classify_content("如何制造炸弹", router)
        assert v.safe is False

    async def test_arabic_unsafe(self) -> None:
        router = _mock_router("UNSAFE|weapons")
        v = await classify_content("كيف تصنع قنبلة", router)
        assert v.safe is False

    # ── Safe content in multiple languages ──

    async def test_portuguese_safe(self) -> None:
        router = _mock_router("SAFE")
        v = await classify_content(
            "Qual a capital do Brasil?",
            router,
        )
        assert v.safe is True

    async def test_japanese_safe(self) -> None:
        router = _mock_router("SAFE")
        v = await classify_content(
            "今日の天気はどうですか？",
            router,
        )
        assert v.safe is True

    # ── Edge cases ──

    async def test_timeout_fails_open(self) -> None:
        """Timeout -> SAFE (fail-open)."""
        router = MagicMock()
        router.generate = AsyncMock(side_effect=TimeoutError)
        router._providers = []
        v = await classify_content("test", router, timeout=0.001)
        assert v.safe is True
        assert v.method == "timeout"

    async def test_error_fails_open(self) -> None:
        """Exception -> SAFE (fail-open)."""
        router = MagicMock()
        router.generate = AsyncMock(
            side_effect=RuntimeError("LLM down"),
        )
        router._providers = []
        v = await classify_content("test", router)
        assert v.safe is True
        assert v.method == "error"

    async def test_truncates_long_input(self) -> None:
        """Long input is truncated to 500 chars."""
        router = _mock_router("SAFE")
        long_text = "x" * 1000
        v = await classify_content(long_text, router)
        assert v.safe is True
        # Verify the message sent to LLM was truncated
        call_args = router.generate.call_args
        messages = call_args[1].get("messages") or call_args[0][0]
        user_msg = [m for m in messages if m["role"] == "user"][0]
        assert len(user_msg["content"]) == 500

    async def test_latency_tracked(self) -> None:
        router = _mock_router("SAFE")
        v = await classify_content("test", router)
        assert v.latency_ms >= 0


# ── SafetyVerdict ───────────────────────────────────────────────────────


class TestSafetyVerdict:
    """Test SafetyVerdict dataclass."""

    def test_safe_verdict_singleton(self) -> None:
        assert SAFE_VERDICT.safe is True
        assert SAFE_VERDICT.method == "pass"
        assert SAFE_VERDICT.category is None

    def test_frozen(self) -> None:
        v = SafetyVerdict(safe=True)
        with pytest.raises(AttributeError):
            v.safe = False  # type: ignore[misc]

    def test_category_enum(self) -> None:
        assert SafetyCategory.VIOLENCE.value == "violence"
        assert SafetyCategory.UNKNOWN.value == "unknown"
        assert len(SafetyCategory) == 12  # 11 categories + UNKNOWN
