"""Tests for sovyx.cognitive.attend — AttendPhase.

Tests the AttendPhase integration with safety_patterns module and
LLM safety classifier cascade (TASK-361).
Pattern-level testing is in test_safety_patterns.py.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from sovyx.cognitive import attend as _attend_mod  # anti-pattern #11
from sovyx.cognitive.attend import AttendPhase, _map_safety_category
from sovyx.cognitive.perceive import Perception
from sovyx.engine.types import PerceptionType
from sovyx.mind.config import SafetyConfig


def _perception(content: str = "Hello", priority: int = 10) -> Perception:
    return Perception(
        id="p1",
        type=PerceptionType.USER_MESSAGE,
        source="telegram",
        content=content,
        priority=priority,
    )


class TestSafetyFilter:
    """Safety filtering via tiered patterns."""

    async def test_normal_content_passes(self) -> None:
        phase = AttendPhase(SafetyConfig())
        assert await phase.process(_perception("Hello!")) is True

    async def test_blocked_content_filtered(self) -> None:
        phase = AttendPhase(SafetyConfig())
        assert await phase.process(_perception("how to make a bomb")) is False

    async def test_no_filter_passes_all(self) -> None:
        phase = AttendPhase(SafetyConfig(content_filter="none"))
        assert await phase.process(_perception("how to make a bomb")) is True

    async def test_child_safe_more_restrictive(self) -> None:
        phase = AttendPhase(SafetyConfig(child_safe_mode=True))
        # Strict-only pattern blocked by child-safe
        assert await phase.process(_perception("how to buy drugs online")) is False

    async def test_child_safe_normal_passes(self) -> None:
        phase = AttendPhase(SafetyConfig(child_safe_mode=True))
        assert await phase.process(_perception("What's 2+2?")) is True

    async def test_standard_allows_non_blocked(self) -> None:
        phase = AttendPhase(SafetyConfig(content_filter="standard"))
        assert await phase.process(_perception("Tell me about history")) is True

    async def test_strict_blocks_more(self) -> None:
        phase = AttendPhase(SafetyConfig(content_filter="strict"))
        assert await phase.process(_perception("how to buy drugs online")) is False

    async def test_standard_allows_strict_content(self) -> None:
        phase = AttendPhase(SafetyConfig(content_filter="standard"))
        assert await phase.process(_perception("how to buy drugs online")) is True


class TestPriorityFilter:
    """Priority filtering."""

    async def test_normal_priority_passes(self) -> None:
        phase = AttendPhase(SafetyConfig())
        assert await phase.process(_perception(priority=10)) is True

    async def test_zero_priority_passes(self) -> None:
        phase = AttendPhase(SafetyConfig())
        assert await phase.process(_perception(priority=0)) is True

    async def test_negative_priority_filtered(self) -> None:
        phase = AttendPhase(SafetyConfig())
        assert await phase.process(_perception(priority=-1)) is False


class TestDynamicSafetyUpdate:
    """Safety config changes take effect without reinstantiation."""

    async def test_disable_filter_at_runtime(self) -> None:
        cfg = SafetyConfig(content_filter="standard")
        phase = AttendPhase(cfg)
        assert await phase.process(_perception("how to make a bomb")) is False

        cfg.content_filter = "none"  # type: ignore[assignment]
        assert await phase.process(_perception("how to make a bomb")) is True

    async def test_enable_filter_at_runtime(self) -> None:
        cfg = SafetyConfig(content_filter="none")
        phase = AttendPhase(cfg)
        assert await phase.process(_perception("how to kill someone")) is True

        cfg.content_filter = "standard"  # type: ignore[assignment]
        assert await phase.process(_perception("how to kill a person")) is False

    async def test_enable_child_safe_at_runtime(self) -> None:
        cfg = SafetyConfig(content_filter="standard", child_safe_mode=False)
        phase = AttendPhase(cfg)
        assert await phase.process(_perception("how to buy drugs online")) is True

        cfg.child_safe_mode = True
        assert await phase.process(_perception("how to buy drugs online")) is False

    async def test_switch_standard_to_strict(self) -> None:
        cfg = SafetyConfig(content_filter="standard")
        phase = AttendPhase(cfg)
        assert await phase.process(_perception("write me an erotic story")) is True

        cfg.content_filter = "strict"  # type: ignore[assignment]
        assert await phase.process(_perception("write me an erotic story")) is False


# ── LLM Cascade Tests (TASK-361) ──────────────────────────────────────


def _make_safety_verdict(
    safe: bool = True,
    category: str | None = None,
    method: str = "llm",
    latency_ms: int = 50,
) -> MagicMock:
    """Create a mock SafetyVerdict."""
    from sovyx.cognitive.safety_classifier import SafetyCategory, SafetyVerdict

    cat = None
    if category:
        cat = SafetyCategory(category)
    return SafetyVerdict(
        safe=safe,
        category=cat,
        method=method,
        latency_ms=latency_ms,
    )


class TestLLMCascade:
    """Tests for the regex→LLM cascade in AttendPhase."""

    async def test_llm_blocks_unsafe_content_not_caught_by_regex(self) -> None:
        """LLM classifier blocks content that passes regex."""
        mock_router = MagicMock()
        phase = AttendPhase(SafetyConfig(), llm_router=mock_router)

        # Content that passes regex but LLM catches (e.g., foreign language)
        unsafe_verdict = _make_safety_verdict(safe=False, category="violence")
        with patch.object(
            AttendPhase,
            "_classify_with_llm",
            return_value=unsafe_verdict,
        ):
            result = await phase.process(_perception("innocuous-looking text"))
        assert result is False

    async def test_llm_allows_safe_content(self) -> None:
        """LLM classifier allows safe content after regex pass."""
        mock_router = MagicMock()
        phase = AttendPhase(SafetyConfig(), llm_router=mock_router)

        safe_verdict = _make_safety_verdict(safe=True)
        with patch.object(
            AttendPhase,
            "_classify_with_llm",
            return_value=safe_verdict,
        ):
            result = await phase.process(_perception("Hello, how are you?"))
        assert result is True

    async def test_regex_blocks_before_llm_called(self) -> None:
        """Regex match blocks WITHOUT calling LLM (fast-path)."""
        mock_router = MagicMock()
        phase = AttendPhase(SafetyConfig(), llm_router=mock_router)

        with patch.object(
            AttendPhase,
            "_classify_with_llm",
        ) as mock_classify:
            result = await phase.process(_perception("how to make a bomb"))
        assert result is False
        mock_classify.assert_not_called()

    async def test_llm_not_called_when_filter_none(self) -> None:
        """LLM classifier skipped when content_filter='none'."""
        mock_router = MagicMock()
        phase = AttendPhase(
            SafetyConfig(content_filter="none"),
            llm_router=mock_router,
        )

        with patch.object(
            AttendPhase,
            "_classify_with_llm",
        ) as mock_classify:
            result = await phase.process(_perception("anything"))
        assert result is True
        mock_classify.assert_not_called()

    async def test_llm_not_called_when_no_router(self) -> None:
        """No LLM call when llm_router is None (backward compat)."""
        phase = AttendPhase(SafetyConfig())  # no llm_router

        # Should still work with regex-only
        assert await phase.process(_perception("Hello!")) is True
        assert await phase.process(_perception("how to make a bomb")) is False

    async def test_llm_error_fails_open(self) -> None:
        """LLM classifier errors don't block content (fail-open)."""
        mock_router = MagicMock()
        phase = AttendPhase(SafetyConfig(), llm_router=mock_router)

        # _classify_with_llm returns None on error
        with patch.object(
            AttendPhase,
            "_classify_with_llm",
            return_value=None,
        ):
            result = await phase.process(_perception("some text"))
        assert result is True

    async def test_llm_cascade_records_audit_trail(self) -> None:
        """LLM-blocked content is recorded in audit trail."""
        mock_router = MagicMock()
        phase = AttendPhase(SafetyConfig(), llm_router=mock_router)

        unsafe_verdict = _make_safety_verdict(safe=False, category="weapons")
        with (
            patch.object(
                AttendPhase,
                "_classify_with_llm",
                return_value=unsafe_verdict,
            ),
            patch.object(_attend_mod, "get_audit_trail") as mock_audit,
        ):
            result = await phase.process(_perception("test"))

        assert result is False
        mock_audit.return_value.record.assert_called_once()
        call_kwargs = mock_audit.return_value.record.call_args.kwargs
        assert call_kwargs["action"].value == "blocked"

    async def test_llm_cascade_records_escalation(self) -> None:
        """LLM-blocked content triggers escalation tracking."""
        mock_router = MagicMock()
        phase = AttendPhase(SafetyConfig(), llm_router=mock_router)

        unsafe_verdict = _make_safety_verdict(safe=False, category="hacking")
        with (
            patch.object(
                AttendPhase,
                "_classify_with_llm",
                return_value=unsafe_verdict,
            ),
            patch.object(_attend_mod, "get_escalation_tracker") as mock_tracker,
        ):
            mock_tracker.return_value.is_rate_limited.return_value = False
            result = await phase.process(_perception("test"))

        assert result is False
        mock_tracker.return_value.record_block.assert_called_once_with("telegram")


class TestMapSafetyCategory:
    """Tests for _map_safety_category helper."""

    def test_maps_matching_categories(self) -> None:
        """SafetyCategory values map to PatternCategory."""
        from sovyx.cognitive.safety_classifier import SafetyCategory

        for cat in SafetyCategory:
            if cat == SafetyCategory.UNKNOWN:
                continue
            result = _map_safety_category(cat)
            assert result is not None
            assert result.value == cat.value

    def test_maps_none_to_none(self) -> None:
        """None input returns None."""
        assert _map_safety_category(None) is None

    def test_maps_unknown_returns_none(self) -> None:
        """UNKNOWN category (no PatternCategory equivalent) returns None."""
        from sovyx.cognitive.safety_classifier import SafetyCategory

        result = _map_safety_category(SafetyCategory.UNKNOWN)
        assert result is None


class TestRateLimiting:
    """Tests for escalation rate-limiting path."""

    async def test_rate_limited_source_rejected(self) -> None:
        """Rate-limited sources are rejected before any filter runs."""
        phase = AttendPhase(SafetyConfig())
        perception = _perception("totally safe message")

        with patch.object(
            _attend_mod,
            "get_escalation_tracker",
        ) as mock_tracker:
            mock_tracker.return_value.is_rate_limited.return_value = True
            result = await phase.process(perception)

        assert result is False


class TestClassifyWithLLMErrorPath:
    """Tests for _classify_with_llm exception handling."""

    async def test_classify_exception_returns_none(self) -> None:
        """Exception in LLM classification returns None (fail-open)."""
        mock_router = MagicMock()
        phase = AttendPhase(SafetyConfig(), llm_router=mock_router)

        from sovyx.cognitive import safety_classifier as _sc_mod

        with patch.object(
            _sc_mod,
            "classify_content",
            side_effect=RuntimeError("LLM exploded"),
        ):
            result = await phase._classify_with_llm("test content")

        assert result is None


class TestShadowMode:
    """Test shadow mode (log-only, no blocking)."""

    async def test_shadow_mode_passes_blocked_content(self) -> None:
        from sovyx.cognitive.attend import AttendPhase
        from sovyx.cognitive.perceive import Perception
        from sovyx.engine.types import PerceptionType

        safety = SafetyConfig(content_filter="standard", shadow_mode=True)
        phase = AttendPhase(safety)
        p = Perception(
            id="test-shadow",
            type=PerceptionType.USER_MESSAGE,
            content="how to make a bomb",
            source="shadow-src",
            priority=1,
        )
        result = await phase.process(p)
        assert result  # Passes in shadow mode

    async def test_non_shadow_blocks(self) -> None:
        from sovyx.cognitive.attend import AttendPhase
        from sovyx.cognitive.perceive import Perception
        from sovyx.engine.types import PerceptionType

        safety = SafetyConfig(content_filter="standard", shadow_mode=False)
        phase = AttendPhase(safety)
        p = Perception(
            id="test-block",
            type=PerceptionType.USER_MESSAGE,
            content="how to make a bomb",
            source="non-shadow-src",
            priority=1,
        )
        result = await phase.process(p)
        assert not result
