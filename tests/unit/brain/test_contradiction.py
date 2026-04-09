"""Tests for sovyx.brain.contradiction — semantic contradiction detection.

Covers:
- Heuristic fallback (all 4 classifications)
- LLM-assisted detection (mock router)
- LLM error fallback to heuristic
- Edge cases (empty content, identical content)
- JSON parsing robustness (markdown blocks, raw text)
- Property-based tests for input safety
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

from hypothesis import given, settings
from hypothesis import strategies as st

from sovyx.brain.contradiction import (
    ContentRelation,
    _detect_contradiction_heuristic,
    detect_contradiction,
)

# ── Helpers ─────────────────────────────────────────────────────────────────


def _mock_llm(relation: str, reason: str = "test") -> AsyncMock:
    """Create mock LLM router returning a specific relation."""
    from sovyx.llm.router import LLMResponse

    router = AsyncMock()
    router.generate = AsyncMock(
        return_value=LLMResponse(
            content=json.dumps({"relation": relation, "reason": reason}),
            model="gpt-4o-mini",
            tokens_in=50,
            tokens_out=30,
            latency_ms=100,
            cost_usd=0.00001,
            finish_reason="stop",
            provider="openai",
        )
    )
    return router


def _mock_llm_raw(raw_text: str) -> AsyncMock:
    """Create mock LLM router returning raw text (not JSON)."""
    from sovyx.llm.router import LLMResponse

    router = AsyncMock()
    router.generate = AsyncMock(
        return_value=LLMResponse(
            content=raw_text,
            model="gpt-4o-mini",
            tokens_in=50,
            tokens_out=30,
            latency_ms=100,
            cost_usd=0.00001,
            finish_reason="stop",
            provider="openai",
        )
    )
    return router


# ── Heuristic Tests ─────────────────────────────────────────────────────────


class TestHeuristicDetection:
    """String-based heuristic fallback."""

    def test_identical_content(self) -> None:
        result = _detect_contradiction_heuristic(
            "Favorite color is blue",
            "Favorite color is blue",
        )
        assert result == ContentRelation.SAME

    def test_identical_case_insensitive(self) -> None:
        result = _detect_contradiction_heuristic(
            "Likes Coffee",
            "likes coffee",
        )
        assert result == ContentRelation.SAME

    def test_extension_prefix_match(self) -> None:
        result = _detect_contradiction_heuristic(
            "Lives in NYC",
            "Lives in NYC, works as a developer",
        )
        assert result == ContentRelation.EXTENDS

    def test_extension_much_longer(self) -> None:
        result = _detect_contradiction_heuristic(
            "Likes dogs",
            "Likes dogs and has three of them named Rex, Spot, and Buddy",
        )
        assert result == ContentRelation.EXTENDS

    def test_contradiction_different_value(self) -> None:
        result = _detect_contradiction_heuristic(
            "Favorite color is blue",
            "Favorite color is red",
        )
        assert result == ContentRelation.CONTRADICTS

    def test_short_content_conservative(self) -> None:
        """Short content → SAME (too unreliable to flag)."""
        result = _detect_contradiction_heuristic("Yes", "No")
        assert result == ContentRelation.SAME

    def test_empty_old_content(self) -> None:
        """Empty old → SAME (nothing to contradict)."""
        result = _detect_contradiction_heuristic("", "New info")
        # detect_contradiction guards this, but heuristic handles too
        assert result in (ContentRelation.SAME, ContentRelation.EXTENDS)


# ── LLM-Assisted Tests ─────────────────────────────────────────────────────


class TestLLMDetection:
    """LLM pairwise comparison."""

    async def test_llm_detects_contradiction(self) -> None:
        router = _mock_llm("CONTRADICTS", "values differ")
        result = await detect_contradiction(
            "Favorite color is blue",
            "Favorite color is red",
            llm_router=router,
            fast_model="gpt-4o-mini",
        )
        assert result == ContentRelation.CONTRADICTS

    async def test_llm_detects_same(self) -> None:
        router = _mock_llm("SAME", "paraphrase")
        result = await detect_contradiction(
            "Likes coffee",
            "Enjoys drinking coffee",
            llm_router=router,
        )
        assert result == ContentRelation.SAME

    async def test_llm_detects_extends(self) -> None:
        router = _mock_llm("EXTENDS", "adds info")
        result = await detect_contradiction(
            "Lives in NYC",
            "Lives in NYC, works at Google",
            llm_router=router,
        )
        assert result == ContentRelation.EXTENDS

    async def test_llm_detects_unrelated(self) -> None:
        router = _mock_llm("UNRELATED", "different topics")
        result = await detect_contradiction(
            "Likes coffee",
            "The weather is nice",
            llm_router=router,
        )
        assert result == ContentRelation.UNRELATED

    async def test_llm_markdown_json_block(self) -> None:
        """LLM wraps JSON in markdown code block."""
        router = _mock_llm_raw('```json\n{"relation": "CONTRADICTS", "reason": "test"}\n```')
        result = await detect_contradiction(
            "Old content",
            "New different content that contradicts",
            llm_router=router,
        )
        assert result == ContentRelation.CONTRADICTS

    async def test_llm_raw_text_extraction(self) -> None:
        """LLM returns raw text with relation keyword."""
        router = _mock_llm_raw("The answer is EXTENDS because it adds new information")
        result = await detect_contradiction(
            "Base info",
            "Base info plus more details",
            llm_router=router,
        )
        assert result == ContentRelation.EXTENDS

    async def test_llm_unparseable_defaults_same(self) -> None:
        """Completely unparseable response → SAME (conservative)."""
        router = _mock_llm_raw("I don't understand the question")
        result = await detect_contradiction(
            "Content A",
            "Content B that differs",
            llm_router=router,
        )
        assert result == ContentRelation.SAME

    async def test_llm_truncates_long_content(self) -> None:
        """Content > 200 chars is truncated for cost control."""
        router = _mock_llm("EXTENDS")
        old_content = "a" * 500
        new_content = "b" * 500  # Different so guard doesn't skip LLM
        await detect_contradiction(
            old_content,
            new_content,
            llm_router=router,
        )
        # Verify LLM was called and prompt is reasonable size
        router.generate.assert_called_once()
        call_args = router.generate.call_args
        messages = call_args.kwargs.get("messages", [])
        prompt = messages[0]["content"]
        # Prompt ≈ template (~500) + 2x200 truncated content ≈ 900
        assert len(prompt) < 1000  # noqa: PLR2004
        # Verify content was truncated (500 → 200)
        assert "a" * 201 not in prompt


# ── Fallback Tests ──────────────────────────────────────────────────────────


class TestFallback:
    """Error handling and fallback."""

    async def test_llm_error_falls_back_to_heuristic(self) -> None:
        """LLM failure → graceful fallback to heuristic."""
        router = AsyncMock()
        router.generate = AsyncMock(side_effect=RuntimeError("API down"))
        result = await detect_contradiction(
            "Favorite color is blue",
            "Favorite color is red",
            llm_router=router,
        )
        # Heuristic should detect contradiction
        assert result == ContentRelation.CONTRADICTS

    async def test_no_router_uses_heuristic(self) -> None:
        """No LLM router → heuristic only."""
        result = await detect_contradiction(
            "Lives in NYC",
            "Lives in NYC, Brooklyn specifically",
        )
        assert result == ContentRelation.EXTENDS

    async def test_empty_content_returns_same(self) -> None:
        result = await detect_contradiction("", "New content")
        assert result == ContentRelation.SAME

    async def test_identical_skips_llm(self) -> None:
        """Identical content → SAME without LLM call."""
        router = _mock_llm("CONTRADICTS")  # Should NOT be called
        result = await detect_contradiction(
            "Same content",
            "Same content",
            llm_router=router,
        )
        assert result == ContentRelation.SAME
        router.generate.assert_not_called()


# ── Property-Based Tests ────────────────────────────────────────────────────


class TestProperties:
    """Property-based tests for robustness."""

    @given(
        old=st.text(min_size=0, max_size=300),
        new=st.text(min_size=0, max_size=300),
    )
    @settings(max_examples=100)
    def test_heuristic_always_returns_valid_relation(self, old: str, new: str) -> None:
        """Heuristic never crashes, always returns valid ContentRelation."""
        result = _detect_contradiction_heuristic(old, new)
        assert isinstance(result, ContentRelation)

    @given(content=st.text(min_size=1, max_size=200))
    @settings(max_examples=50)
    def test_identical_always_same(self, content: str) -> None:
        """Identical content always classified as SAME."""
        result = _detect_contradiction_heuristic(content, content)
        assert result == ContentRelation.SAME

    @given(
        old=st.text(min_size=1, max_size=100),
        new=st.text(min_size=1, max_size=100),
    )
    @settings(max_examples=50)
    async def test_detect_contradiction_never_crashes(self, old: str, new: str) -> None:
        """Full detect_contradiction never raises (graceful fallback)."""
        result = await detect_contradiction(old, new)
        assert isinstance(result, ContentRelation)
