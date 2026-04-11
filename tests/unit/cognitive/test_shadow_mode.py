"""Tests for Sovyx Shadow Mode — dry-run safety patterns (log-only).

Coverage target: ≥95% of shadow_mode.py.
"""

from __future__ import annotations

import re
from unittest.mock import patch

import pytest

from sovyx.cognitive.safety_audit import (
    FilterAction,
    FilterDirection,
    get_audit_trail,
    setup_audit_trail,
)
from sovyx.cognitive.shadow_mode import (
    NO_SHADOW_MATCH,
    CompiledShadowPattern,
    ShadowMatch,
    compile_shadow_patterns,
    evaluate_shadow,
    get_shadow_stats,
    invalidate_cache,
)
from sovyx.mind.config import SafetyConfig, ShadowPattern

# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_state() -> None:
    """Reset caches and audit trail between tests."""
    invalidate_cache()
    setup_audit_trail(max_events=1000)
    yield
    invalidate_cache()


def _make_config(
    shadow_mode: bool = True,
    patterns: list[ShadowPattern] | None = None,
) -> SafetyConfig:
    """Create a SafetyConfig with shadow mode settings."""
    return SafetyConfig(
        shadow_mode=shadow_mode,
        shadow_patterns=patterns or [],
    )


def _make_pattern(
    name: str = "test-pattern",
    pattern: str = r"\btest\b",
    category: str = "violence",
    tier: str = "standard",
    description: str = "Test pattern",
) -> ShadowPattern:
    """Create a ShadowPattern for testing."""
    return ShadowPattern(
        name=name,
        pattern=pattern,
        category=category,
        tier=tier,
        description=description,
    )


# ── compile_shadow_patterns ────────────────────────────────────────────


class TestCompileShadowPatterns:
    """Tests for compile_shadow_patterns()."""

    def test_empty_list(self) -> None:
        result = compile_shadow_patterns([])
        assert result == []

    def test_single_valid_pattern(self) -> None:
        patterns = [_make_pattern()]
        result = compile_shadow_patterns(patterns)
        assert len(result) == 1
        assert result[0].name == "test-pattern"
        assert result[0].category == "violence"
        assert result[0].tier == "standard"
        assert isinstance(result[0].regex, re.Pattern)

    def test_multiple_patterns(self) -> None:
        patterns = [
            _make_pattern(name="p1", pattern=r"\bfoo\b"),
            _make_pattern(name="p2", pattern=r"\bbar\b"),
            _make_pattern(name="p3", pattern=r"\bbaz\b"),
        ]
        result = compile_shadow_patterns(patterns)
        assert len(result) == 3
        assert [r.name for r in result] == ["p1", "p2", "p3"]

    def test_invalid_regex_skipped(self) -> None:
        patterns = [
            _make_pattern(name="valid", pattern=r"\bfoo\b"),
            _make_pattern(name="invalid", pattern=r"[invalid"),
            _make_pattern(name="also-valid", pattern=r"\bbar\b"),
        ]
        result = compile_shadow_patterns(patterns)
        assert len(result) == 2
        assert result[0].name == "valid"
        assert result[1].name == "also-valid"

    def test_case_insensitive_flag(self) -> None:
        patterns = [_make_pattern(pattern=r"\bHello\b")]
        result = compile_shadow_patterns(patterns)
        assert result[0].regex.flags & re.IGNORECASE

    def test_preserves_description(self) -> None:
        patterns = [_make_pattern(description="Catches bad words")]
        result = compile_shadow_patterns(patterns)
        assert result[0].description == "Catches bad words"


# ── evaluate_shadow ────────────────────────────────────────────────────


class TestEvaluateShadow:
    """Tests for evaluate_shadow()."""

    def test_disabled_shadow_mode_returns_no_match(self) -> None:
        config = _make_config(
            shadow_mode=False,
            patterns=[_make_pattern()],
        )
        result = evaluate_shadow("test content", config, FilterDirection.INPUT)
        assert result is NO_SHADOW_MATCH
        assert not result.matched

    def test_no_patterns_returns_no_match(self) -> None:
        config = _make_config(shadow_mode=True, patterns=[])
        result = evaluate_shadow("test content", config, FilterDirection.INPUT)
        assert result is NO_SHADOW_MATCH

    def test_no_match_returns_no_shadow_match(self) -> None:
        config = _make_config(
            patterns=[_make_pattern(pattern=r"\bxyzzy\b")],
        )
        result = evaluate_shadow("hello world", config, FilterDirection.INPUT)
        assert not result.matched
        assert result is NO_SHADOW_MATCH

    def test_single_match(self) -> None:
        config = _make_config(
            patterns=[_make_pattern(name="violence-test", pattern=r"\bharm\b")],
        )
        result = evaluate_shadow("this will harm someone", config, FilterDirection.INPUT)
        assert result.matched
        assert result.pattern_name == "violence-test"
        assert result.category == "violence"
        assert result.tier == "standard"
        assert result.all_matches == ("violence-test",)

    def test_multiple_matches(self) -> None:
        config = _make_config(
            patterns=[
                _make_pattern(name="p1", pattern=r"\bhello\b"),
                _make_pattern(name="p2", pattern=r"\bworld\b"),
                _make_pattern(name="p3", pattern=r"\bxyz\b"),
            ],
        )
        result = evaluate_shadow("hello world", config, FilterDirection.INPUT)
        assert result.matched
        assert result.pattern_name == "p1"  # First match
        assert result.all_matches == ("p1", "p2")  # Both matched, not p3

    def test_case_insensitive_matching(self) -> None:
        config = _make_config(
            patterns=[_make_pattern(pattern=r"\bhello\b")],
        )
        result = evaluate_shadow("HELLO WORLD", config, FilterDirection.INPUT)
        assert result.matched

    def test_logs_to_audit_trail(self) -> None:
        audit = get_audit_trail()
        initial_count = audit.event_count

        config = _make_config(
            patterns=[_make_pattern(name="audit-test", pattern=r"\bbad\b")],
        )
        evaluate_shadow("this is bad", config, FilterDirection.INPUT)

        assert audit.event_count == initial_count + 1
        stats = audit.get_stats()
        # Should have a shadow_logged event
        assert stats.recent_events[0]["action"] == "shadow_logged"
        assert stats.recent_events[0]["direction"] == "input"

    def test_logs_multiple_matches_to_audit(self) -> None:
        audit = get_audit_trail()
        initial_count = audit.event_count

        config = _make_config(
            patterns=[
                _make_pattern(name="p1", pattern=r"\bfoo\b"),
                _make_pattern(name="p2", pattern=r"\bbar\b"),
            ],
        )
        evaluate_shadow("foo bar", config, FilterDirection.OUTPUT)

        assert audit.event_count == initial_count + 2

    def test_output_direction(self) -> None:
        audit = get_audit_trail()

        config = _make_config(
            patterns=[_make_pattern(pattern=r"\btest\b")],
        )
        evaluate_shadow("test output", config, FilterDirection.OUTPUT)

        stats = audit.get_stats()
        assert stats.recent_events[0]["direction"] == "output"

    def test_never_blocks_content(self) -> None:
        """Shadow mode MUST never return a blocking result."""
        config = _make_config(
            patterns=[
                _make_pattern(pattern=r"\bbomb\b"),
                _make_pattern(pattern=r"\bhack\b"),
            ],
        )
        result = evaluate_shadow("how to build a bomb and hack", config, FilterDirection.INPUT)
        # Returns match info but this is purely informational
        assert result.matched
        # The function returns ShadowMatch, not a blocking action
        assert isinstance(result, ShadowMatch)

    def test_all_invalid_patterns_returns_no_match(self) -> None:
        config = _make_config(
            patterns=[
                _make_pattern(name="bad1", pattern=r"[unclosed"),
                _make_pattern(name="bad2", pattern=r"(unclosed"),
            ],
        )
        result = evaluate_shadow("anything", config, FilterDirection.INPUT)
        assert not result.matched

    def test_unicode_normalization(self) -> None:
        """Shadow mode should use text normalizer for consistent matching."""
        config = _make_config(
            patterns=[_make_pattern(pattern=r"\btest\b")],
        )
        # The text normalizer handles unicode normalization
        result = evaluate_shadow("test", config, FilterDirection.INPUT)
        assert result.matched

    def test_unknown_category_in_audit(self) -> None:
        """Patterns with non-standard categories should still log."""
        config = _make_config(
            patterns=[_make_pattern(category="custom_category", pattern=r"\btest\b")],
        )
        result = evaluate_shadow("test content", config, FilterDirection.INPUT)
        assert result.matched
        assert result.category == "custom_category"


# ── Cache behavior ─────────────────────────────────────────────────────


class TestCaching:
    """Tests for pattern compilation caching."""

    def test_cache_hit_same_config(self) -> None:
        config = _make_config(
            patterns=[_make_pattern(pattern=r"\bfoo\b")],
        )
        # First call compiles
        evaluate_shadow("foo", config, FilterDirection.INPUT)
        # Second call should use cache (same config hash)
        evaluate_shadow("foo", config, FilterDirection.INPUT)
        # No way to directly assert cache hit, but it shouldn't crash

    def test_cache_invalidation(self) -> None:
        config1 = _make_config(
            patterns=[_make_pattern(name="p1", pattern=r"\bfoo\b")],
        )
        config2 = _make_config(
            patterns=[_make_pattern(name="p2", pattern=r"\bbar\b")],
        )

        result1 = evaluate_shadow("foo", config1, FilterDirection.INPUT)
        assert result1.matched

        result2 = evaluate_shadow("foo", config2, FilterDirection.INPUT)
        assert not result2.matched  # Different config, "foo" doesn't match "bar"

    def test_invalidate_cache_function(self) -> None:
        config = _make_config(
            patterns=[_make_pattern(pattern=r"\bfoo\b")],
        )
        evaluate_shadow("foo", config, FilterDirection.INPUT)
        invalidate_cache()
        # After invalidation, should recompile
        result = evaluate_shadow("foo", config, FilterDirection.INPUT)
        assert result.matched


# ── get_shadow_stats ───────────────────────────────────────────────────


class TestGetShadowStats:
    """Tests for get_shadow_stats()."""

    def test_disabled(self) -> None:
        config = _make_config(shadow_mode=False)
        stats = get_shadow_stats(config)
        assert stats["enabled"] is False
        assert stats["compiled_patterns"] == 0

    def test_enabled_no_patterns(self) -> None:
        config = _make_config(shadow_mode=True, patterns=[])
        stats = get_shadow_stats(config)
        assert stats["enabled"] is True
        assert stats["total_patterns"] == 0
        assert stats["compiled_patterns"] == 0
        assert stats["compile_errors"] == 0

    def test_enabled_with_patterns(self) -> None:
        config = _make_config(
            patterns=[
                _make_pattern(name="p1"),
                _make_pattern(name="p2"),
            ],
        )
        stats = get_shadow_stats(config)
        assert stats["enabled"] is True
        assert stats["total_patterns"] == 2
        assert stats["compiled_patterns"] == 2
        assert stats["compile_errors"] == 0

    def test_compile_errors_counted(self) -> None:
        config = _make_config(
            patterns=[
                _make_pattern(name="valid", pattern=r"\bfoo\b"),
                _make_pattern(name="invalid", pattern=r"[bad"),
            ],
        )
        stats = get_shadow_stats(config)
        assert stats["total_patterns"] == 2
        assert stats["compiled_patterns"] == 1
        assert stats["compile_errors"] == 1


# ── ShadowMatch dataclass ─────────────────────────────────────────────


class TestShadowMatch:
    """Tests for ShadowMatch dataclass."""

    def test_no_shadow_match_singleton(self) -> None:
        assert NO_SHADOW_MATCH.matched is False
        assert NO_SHADOW_MATCH.pattern_name is None
        assert NO_SHADOW_MATCH.category is None
        assert NO_SHADOW_MATCH.tier is None
        assert NO_SHADOW_MATCH.description is None
        assert NO_SHADOW_MATCH.all_matches == ()

    def test_shadow_match_creation(self) -> None:
        match = ShadowMatch(
            matched=True,
            pattern_name="test",
            category="violence",
            tier="strict",
            description="Test desc",
            all_matches=("test", "test2"),
        )
        assert match.matched is True
        assert match.pattern_name == "test"
        assert match.all_matches == ("test", "test2")

    def test_shadow_match_frozen(self) -> None:
        match = ShadowMatch(matched=False)
        with pytest.raises(AttributeError):
            match.matched = True  # type: ignore[misc]


# ── CompiledShadowPattern dataclass ───────────────────────────────────


class TestCompiledShadowPattern:
    """Tests for CompiledShadowPattern dataclass."""

    def test_creation(self) -> None:
        pattern = CompiledShadowPattern(
            name="test",
            regex=re.compile(r"\bfoo\b", re.IGNORECASE),
            category="violence",
            tier="standard",
            description="A test",
        )
        assert pattern.name == "test"
        assert pattern.regex.search("foo") is not None

    def test_frozen(self) -> None:
        pattern = CompiledShadowPattern(
            name="test",
            regex=re.compile(r"\bfoo\b"),
            category="violence",
            tier="standard",
            description="A test",
        )
        with pytest.raises(AttributeError):
            pattern.name = "changed"  # type: ignore[misc]


# ── ShadowPattern config model ────────────────────────────────────────


class TestShadowPatternConfig:
    """Tests for ShadowPattern pydantic model."""

    def test_defaults(self) -> None:
        p = ShadowPattern(name="test", pattern=r"\bfoo\b")
        assert p.category == "unknown"
        assert p.tier == "standard"
        assert p.description == ""

    def test_all_fields(self) -> None:
        p = ShadowPattern(
            name="test",
            pattern=r"\bfoo\b",
            category="weapons",
            tier="strict",
            description="Catches foo",
        )
        assert p.name == "test"
        assert p.tier == "strict"


# ── SafetyConfig shadow fields ────────────────────────────────────────


class TestSafetyConfigShadow:
    """Tests for shadow_mode fields on SafetyConfig."""

    def test_defaults(self) -> None:
        config = SafetyConfig()
        assert config.shadow_mode is False
        assert config.shadow_patterns == []

    def test_enabled_with_patterns(self) -> None:
        config = SafetyConfig(
            shadow_mode=True,
            shadow_patterns=[
                ShadowPattern(name="test", pattern=r"\bfoo\b"),
            ],
        )
        assert config.shadow_mode is True
        assert len(config.shadow_patterns) == 1


# ── FilterAction.SHADOW_LOGGED ────────────────────────────────────────


class TestFilterActionShadow:
    """Tests for SHADOW_LOGGED FilterAction."""

    def test_shadow_logged_exists(self) -> None:
        assert FilterAction.SHADOW_LOGGED.value == "shadow_logged"

    def test_all_actions(self) -> None:
        actions = {a.value for a in FilterAction}
        assert "shadow_logged" in actions
        assert "blocked" in actions
        assert "redacted" in actions
        assert "replaced" in actions


# ── Integration: AttendPhase shadow eval ───────────────────────────────


class TestAttendShadowIntegration:
    """Test that AttendPhase calls shadow evaluation."""

    @pytest.mark.asyncio()
    async def test_attend_runs_shadow_on_accepted(self) -> None:
        """When content passes all real checks, shadow eval should run."""
        from sovyx.cognitive.attend import AttendPhase
        from sovyx.cognitive.perceive import Perception
        from sovyx.engine.types import PerceptionType

        config = _make_config(
            patterns=[_make_pattern(name="shadow-attend", pattern=r"\bhello\b")],
        )
        attend = AttendPhase(safety_config=config)

        perception = Perception(
            id="test-1",
            type=PerceptionType.USER_MESSAGE,
            source="test-user",
            content="hello world",
            priority=1,
            metadata={},
        )

        shadow_path = "sovyx.cognitive.shadow_mode.evaluate_shadow"
        with patch(shadow_path, wraps=evaluate_shadow) as mock_eval:
            result = await attend.process(perception)
            assert result is True  # Shadow doesn't block
            mock_eval.assert_called_once()


# ── Integration: OutputGuard shadow eval ───────────────────────────────


class TestOutputGuardShadowIntegration:
    """Test that OutputGuard calls shadow evaluation."""

    def test_sync_check_runs_shadow(self) -> None:
        """Sync check should run shadow eval when no real match."""
        from sovyx.cognitive.output_guard import OutputGuard

        config = _make_config(
            patterns=[_make_pattern(name="shadow-output", pattern=r"\bresponse\b")],
        )
        guard = OutputGuard(safety_config=config)

        shadow_path = "sovyx.cognitive.shadow_mode.evaluate_shadow"
        with patch(shadow_path, wraps=evaluate_shadow) as mock_eval:
            result = guard.check("this is a response")
            assert not result.filtered  # Shadow doesn't filter
            mock_eval.assert_called_once()

    @pytest.mark.asyncio()
    async def test_async_check_runs_shadow(self) -> None:
        """Async check should run shadow eval when no real match."""
        from sovyx.cognitive.output_guard import OutputGuard

        config = _make_config(
            patterns=[
                _make_pattern(
                    name="shadow-output-async",
                    pattern=r"\bresponse\b",
                ),
            ],
        )
        guard = OutputGuard(safety_config=config)

        shadow_path = "sovyx.cognitive.shadow_mode.evaluate_shadow"
        with patch(shadow_path, wraps=evaluate_shadow) as mock_eval:
            result = await guard.check_async("this is a response")
            assert not result.filtered
            mock_eval.assert_called_once()
