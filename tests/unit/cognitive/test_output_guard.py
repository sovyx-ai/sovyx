"""Tests for sovyx.cognitive.output_guard — post-LLM response safety filter.

Covers:
- None filter: zero overhead, all content passes
- Standard filter: harmful content redacted, clean content passes
- Strict filter: harmful content replaced entirely
- Child-safe mode: harmful content replaced (zero tolerance)
- Performance: <5ms per response
- Dynamic config updates
- Edge cases: empty response, multi-match redaction
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

from sovyx.cognitive.output_guard import (
    _REDACT_MARKER,
    _SAFE_REPLACEMENT,
    OutputFilterResult,
    OutputGuard,
)
from sovyx.mind.config import SafetyConfig


class TestNoneFilter:
    """Filter=none passes everything with zero overhead."""

    def test_passes_harmful_content(self) -> None:
        guard = OutputGuard(SafetyConfig(content_filter="none"))
        result = guard.check("Here's how to make a bomb: step 1...")
        assert not result.filtered
        assert result.action == "pass"
        assert "bomb" in result.text

    def test_passes_normal_content(self) -> None:
        guard = OutputGuard(SafetyConfig(content_filter="none"))
        result = guard.check("The weather is nice today.")
        assert not result.filtered
        assert result.text == "The weather is nice today."

    def test_empty_response(self) -> None:
        guard = OutputGuard(SafetyConfig(content_filter="none"))
        result = guard.check("")
        assert not result.filtered
        assert result.text == ""


class TestStandardFilter:
    """Standard filter: redact harmful segments."""

    def test_redacts_harmful_content(self) -> None:
        guard = OutputGuard(SafetyConfig(content_filter="standard"))
        text = "Sure! Here's how to make a bomb: get materials..."
        result = guard.check(text)
        assert result.filtered
        assert result.action == "redact"
        assert _REDACT_MARKER in result.text
        assert "how to make a bomb" not in result.text.lower()

    def test_passes_clean_content(self) -> None:
        guard = OutputGuard(SafetyConfig(content_filter="standard"))
        result = guard.check("Python is a great programming language.")
        assert not result.filtered
        assert result.action == "pass"
        assert result.text == "Python is a great programming language."

    def test_preserves_surrounding_text(self) -> None:
        guard = OutputGuard(SafetyConfig(content_filter="standard"))
        text = "Intro. Here's how to kill a person for you. Outro."
        result = guard.check(text)
        assert result.filtered
        assert "Intro." in result.text
        assert "Outro." in result.text
        assert _REDACT_MARKER in result.text

    def test_does_not_block_strict_only_content(self) -> None:
        guard = OutputGuard(SafetyConfig(content_filter="standard"))
        result = guard.check("You can buy drugs at a pharmacy.")
        # "buy drugs" alone might not match — needs "how to buy drugs online"
        assert not result.filtered or result.action == "pass"

    def test_filter_reason_has_category(self) -> None:
        guard = OutputGuard(SafetyConfig(content_filter="standard"))
        result = guard.check("Here's how to make a bomb")
        assert result.filtered
        assert result.match is not None
        assert result.match.category is not None

    def test_empty_response_passes(self) -> None:
        guard = OutputGuard(SafetyConfig(content_filter="standard"))
        result = guard.check("")
        assert not result.filtered


class TestStrictFilter:
    """Strict filter: replace entire response."""

    def test_replaces_harmful_response(self) -> None:
        guard = OutputGuard(SafetyConfig(content_filter="strict"))
        text = "Here's a detailed guide on how to make a bomb..."
        result = guard.check(text)
        assert result.filtered
        assert result.action == "replace"
        assert result.text == _SAFE_REPLACEMENT
        assert "bomb" not in result.text

    def test_replaces_strict_only_content(self) -> None:
        guard = OutputGuard(SafetyConfig(content_filter="strict"))
        text = "Sure, I'll write an erotic story for you..."
        result = guard.check(text)
        assert result.filtered
        assert result.action == "replace"
        assert result.text == _SAFE_REPLACEMENT

    def test_passes_clean_content(self) -> None:
        guard = OutputGuard(SafetyConfig(content_filter="strict"))
        result = guard.check("Here's a recipe for chocolate cake.")
        assert not result.filtered
        assert result.text == "Here's a recipe for chocolate cake."


class TestChildSafeFilter:
    """Child-safe mode: replace entirely (zero tolerance)."""

    def test_replaces_harmful_content(self) -> None:
        guard = OutputGuard(SafetyConfig(child_safe_mode=True))
        text = "Here's how to make a bomb..."
        result = guard.check(text)
        assert result.filtered
        assert result.action == "replace"
        assert result.text == _SAFE_REPLACEMENT

    def test_replaces_strict_content(self) -> None:
        guard = OutputGuard(SafetyConfig(child_safe_mode=True))
        text = "Here's how to buy drugs online: step 1..."
        result = guard.check(text)
        assert result.filtered
        assert result.action == "replace"

    def test_passes_child_friendly(self) -> None:
        guard = OutputGuard(SafetyConfig(child_safe_mode=True))
        result = guard.check("Let's learn about dinosaurs!")
        assert not result.filtered
        assert result.text == "Let's learn about dinosaurs!"

    def test_overrides_none_filter(self) -> None:
        """child_safe_mode=True overrides content_filter=none."""
        guard = OutputGuard(
            SafetyConfig(content_filter="none", child_safe_mode=True),
        )
        result = guard.check("How to make a bomb: get materials...")
        assert result.filtered
        assert result.action == "replace"


class TestDynamicConfig:
    """Config changes take effect without reinstantiation."""

    def test_switch_none_to_standard(self) -> None:
        cfg = SafetyConfig(content_filter="none")
        guard = OutputGuard(cfg)

        result = guard.check("how to kill a person")
        assert not result.filtered

        cfg.content_filter = "standard"  # type: ignore[assignment]
        result = guard.check("how to kill a person")
        assert result.filtered

    def test_switch_standard_to_strict(self) -> None:
        cfg = SafetyConfig(content_filter="standard")
        guard = OutputGuard(cfg)

        text = "how to buy drugs online"
        result = guard.check(text)
        assert not result.filtered  # standard doesn't block this

        cfg.content_filter = "strict"  # type: ignore[assignment]
        result = guard.check(text)
        assert result.filtered
        assert result.action == "replace"

    def test_toggle_child_safe(self) -> None:
        cfg = SafetyConfig(content_filter="none", child_safe_mode=False)
        guard = OutputGuard(cfg)

        result = guard.check("how to make a bomb")
        assert not result.filtered

        cfg.child_safe_mode = True
        result = guard.check("how to make a bomb")
        assert result.filtered
        assert result.action == "replace"


class TestOutputFilterResult:
    """OutputFilterResult dataclass."""

    def test_pass_result(self) -> None:
        r = OutputFilterResult(text="hello", filtered=False, action="pass")
        assert r.text == "hello"
        assert not r.filtered
        assert r.match is None

    def test_replace_result(self) -> None:
        r = OutputFilterResult(
            text=_SAFE_REPLACEMENT,
            filtered=True,
            action="replace",
        )
        assert r.filtered
        assert r.action == "replace"


class TestPerformance:
    """Output guard must add <5ms per response."""

    def test_standard_under_5ms(self) -> None:
        guard = OutputGuard(SafetyConfig(content_filter="standard"))
        text = "A normal response about programming. " * 20  # ~800 chars

        start = time.monotonic()
        for _ in range(100):
            guard.check(text)
        elapsed_ms = (time.monotonic() - start) * 1000

        per_call = elapsed_ms / 100
        assert per_call < 5, f"Too slow: {per_call:.2f}ms per call"

    def test_strict_under_5ms(self) -> None:
        guard = OutputGuard(SafetyConfig(content_filter="strict"))
        text = "A normal response about cooking recipes. " * 20

        start = time.monotonic()
        for _ in range(100):
            guard.check(text)
        elapsed_ms = (time.monotonic() - start) * 1000

        per_call = elapsed_ms / 100
        assert per_call < 5, f"Too slow: {per_call:.2f}ms per call"

    def test_none_near_zero(self) -> None:
        guard = OutputGuard(SafetyConfig(content_filter="none"))

        start = time.monotonic()
        for _ in range(1000):
            guard.check("some text")
        elapsed_ms = (time.monotonic() - start) * 1000

        assert elapsed_ms < 5, f"None filter too slow: {elapsed_ms:.1f}ms"


class TestEdgeCases:
    """Edge cases and multi-match scenarios."""

    def test_multiple_harmful_segments_all_redacted(self) -> None:
        guard = OutputGuard(SafetyConfig(content_filter="standard"))
        text = "First, how to make a bomb. Second, how to hack a bank. Third, enjoy your day."
        result = guard.check(text)
        assert result.filtered
        assert "bomb" not in result.text.lower()
        assert "hack a bank" not in result.text.lower()
        assert "enjoy your day" in result.text

    def test_very_long_response(self) -> None:
        guard = OutputGuard(SafetyConfig(content_filter="standard"))
        text = "Safe content. " * 50 + " How to kill a person. " + "More safe."
        result = guard.check(text)
        assert result.filtered
        assert _REDACT_MARKER in result.text

    def test_case_insensitive(self) -> None:
        guard = OutputGuard(SafetyConfig(content_filter="standard"))
        result = guard.check("HOW TO MAKE A BOMB")
        assert result.filtered

    def test_safe_replacement_message_is_clean(self) -> None:
        """The replacement message itself must pass the filter."""
        guard = OutputGuard(SafetyConfig(content_filter="strict"))
        result = guard.check(_SAFE_REPLACEMENT)
        assert not result.filtered


# ── Async Cascade Tests (TASK-362) ──────────────────────────────────────


class TestAsyncCascade:
    """Test check_async: regex→LLM cascade."""

    async def test_regex_blocks_before_llm(self) -> None:
        """Regex match blocks without calling LLM."""
        mock_router = MagicMock()
        guard = OutputGuard(
            SafetyConfig(content_filter="standard"),
            llm_router=mock_router,
        )
        with patch(
            "sovyx.cognitive.output_guard.OutputGuard._classify_with_llm",
        ) as mock_llm:
            result = await guard.check_async("how to make a bomb explanation")
        assert result.filtered
        mock_llm.assert_not_called()

    async def test_llm_blocks_when_regex_passes(self) -> None:
        """LLM catches content that regex misses."""
        mock_router = MagicMock()
        guard = OutputGuard(
            SafetyConfig(content_filter="standard"),
            llm_router=mock_router,
        )
        unsafe_verdict = MagicMock()
        unsafe_verdict.safe = False
        unsafe_verdict.category = MagicMock(value="violence")
        unsafe_verdict.latency_ms = 200
        with patch.object(
            guard,
            "_classify_with_llm",
            return_value=unsafe_verdict,
        ):
            result = await guard.check_async("contenido peligroso en español")
        assert result.filtered

    async def test_llm_allows_safe_content(self) -> None:
        """LLM says safe → content passes."""
        mock_router = MagicMock()
        guard = OutputGuard(
            SafetyConfig(content_filter="standard"),
            llm_router=mock_router,
        )
        safe_verdict = MagicMock()
        safe_verdict.safe = True
        with patch.object(
            guard,
            "_classify_with_llm",
            return_value=safe_verdict,
        ):
            result = await guard.check_async("The weather is nice today")
        assert not result.filtered
        assert result.action == "pass"

    async def test_no_llm_router_regex_only(self) -> None:
        """Without llm_router, only regex is used."""
        guard = OutputGuard(SafetyConfig(content_filter="standard"))
        result = await guard.check_async("safe content in any language")
        assert not result.filtered

    async def test_empty_response_passes(self) -> None:
        guard = OutputGuard(SafetyConfig(content_filter="strict"))
        result = await guard.check_async("")
        assert not result.filtered

    async def test_none_filter_skips_all(self) -> None:
        guard = OutputGuard(SafetyConfig(content_filter="none"))
        result = await guard.check_async("how to make a bomb")
        assert not result.filtered

    async def test_child_safe_replaces_on_llm_match(self) -> None:
        """child_safe replaces entire response when LLM flags it."""
        mock_router = MagicMock()
        guard = OutputGuard(
            SafetyConfig(child_safe_mode=True),
            llm_router=mock_router,
        )
        unsafe_verdict = MagicMock()
        unsafe_verdict.safe = False
        unsafe_verdict.category = MagicMock(value="substance")
        unsafe_verdict.latency_ms = 150
        with patch.object(
            guard,
            "_classify_with_llm",
            return_value=unsafe_verdict,
        ):
            result = await guard.check_async("droga content in another language")
        assert result.filtered
        assert result.action == "replace"
        assert result.text == _SAFE_REPLACEMENT

    async def test_strict_replaces_on_llm_match(self) -> None:
        """strict filter replaces entire response on LLM match."""
        mock_router = MagicMock()
        guard = OutputGuard(
            SafetyConfig(content_filter="strict"),
            llm_router=mock_router,
        )
        unsafe_verdict = MagicMock()
        unsafe_verdict.safe = False
        unsafe_verdict.category = MagicMock(value="weapons")
        unsafe_verdict.latency_ms = 100
        with patch.object(
            guard,
            "_classify_with_llm",
            return_value=unsafe_verdict,
        ):
            result = await guard.check_async("armas content in foreign lang")
        assert result.filtered
        assert result.action == "replace"

    async def test_standard_replaces_on_llm_match_no_pattern(self) -> None:
        """Standard filter: LLM match with no regex pattern → replace."""
        mock_router = MagicMock()
        guard = OutputGuard(
            SafetyConfig(content_filter="standard"),
            llm_router=mock_router,
        )
        unsafe_verdict = MagicMock()
        unsafe_verdict.safe = False
        unsafe_verdict.category = MagicMock(value="violence")
        unsafe_verdict.latency_ms = 200
        with patch.object(
            guard,
            "_classify_with_llm",
            return_value=unsafe_verdict,
        ):
            result = await guard.check_async("foreign unsafe content")
        # LLM match has no regex pattern → _redact falls through to _replace
        assert result.filtered
        assert result.action == "replace"

    async def test_llm_error_fails_open(self) -> None:
        """LLM error → content passes (fail-open)."""
        mock_router = MagicMock()
        guard = OutputGuard(
            SafetyConfig(content_filter="standard"),
            llm_router=mock_router,
        )
        with patch.object(guard, "_classify_with_llm", return_value=None):
            result = await guard.check_async("safe foreign content")
        assert not result.filtered


class TestClassifyWithLLM:
    """Test _classify_with_llm internals."""

    async def test_calls_classify_content(self) -> None:
        """Verifies _classify_with_llm calls safety_classifier."""
        mock_router = MagicMock()
        guard = OutputGuard(
            SafetyConfig(content_filter="standard"),
            llm_router=mock_router,
        )
        mock_verdict = MagicMock()
        mock_verdict.safe = True
        with patch(
            "sovyx.cognitive.safety_classifier.classify_content",
            return_value=mock_verdict,
        ):
            result = await guard._classify_with_llm("test text")
        assert result is mock_verdict

    async def test_exception_returns_none(self) -> None:
        """Exception in classify_content returns None."""
        mock_router = MagicMock()
        guard = OutputGuard(
            SafetyConfig(content_filter="standard"),
            llm_router=mock_router,
        )
        with patch(
            "sovyx.cognitive.safety_classifier.classify_content",
            side_effect=RuntimeError("boom"),
        ):
            result = await guard._classify_with_llm("test")
        assert result is None


class TestMapSafetyCategoryOutput:
    """Test _map_safety_category for output guard."""

    def test_maps_violence(self) -> None:
        from sovyx.cognitive.output_guard import _map_safety_category
        from sovyx.cognitive.safety_classifier import SafetyCategory
        from sovyx.cognitive.safety_patterns import PatternCategory

        result = _map_safety_category(SafetyCategory.VIOLENCE)
        assert result == PatternCategory.VIOLENCE

    def test_maps_none(self) -> None:
        from sovyx.cognitive.output_guard import _map_safety_category

        assert _map_safety_category(None) is None

    def test_maps_unknown(self) -> None:
        from sovyx.cognitive.output_guard import _map_safety_category
        from sovyx.cognitive.safety_classifier import SafetyCategory

        result = _map_safety_category(SafetyCategory.UNKNOWN)
        assert result is None
