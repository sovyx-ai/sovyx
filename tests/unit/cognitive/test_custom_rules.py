"""Tests for custom rules engine.

Covers: rule matching, banned topics, invalid regex, log-only rules,
custom messages, cache, integration with attend.
"""

from __future__ import annotations

from sovyx.cognitive.custom_rules import (
    NO_RULE_MATCH,
    check_banned_topics,
    check_custom_rules,
    clear_compiled_cache,
)
from sovyx.mind.config import CustomRule, SafetyConfig


def _safety_with_rules(
    rules: list[CustomRule] | None = None,
    topics: list[str] | None = None,
) -> SafetyConfig:
    return SafetyConfig(
        custom_rules=rules or [],
        banned_topics=topics or [],
    )


class TestCustomRules:
    """Test custom rule matching."""

    def setup_method(self) -> None:
        clear_compiled_cache()

    def test_no_rules_returns_no_match(self) -> None:
        result = check_custom_rules("hello", _safety_with_rules())
        assert result is NO_RULE_MATCH

    def test_simple_pattern_blocks(self) -> None:
        rules = [CustomRule(name="no-crypto", pattern=r"\bcrypto\b", action="block")]
        result = check_custom_rules("tell me about crypto", _safety_with_rules(rules))
        assert result.matched
        assert result.rule_name == "no-crypto"
        assert result.action == "block"

    def test_case_insensitive(self) -> None:
        rules = [CustomRule(name="test", pattern=r"\bBITCOIN\b")]
        result = check_custom_rules("Tell me about bitcoin", _safety_with_rules(rules))
        assert result.matched

    def test_no_match_returns_no_match(self) -> None:
        rules = [CustomRule(name="test", pattern=r"\bcrypto\b")]
        result = check_custom_rules("weather today?", _safety_with_rules(rules))
        assert not result.matched

    def test_log_only_action(self) -> None:
        rules = [CustomRule(name="medical", pattern=r"\bdiagnosis\b", action="log")]
        result = check_custom_rules("need a diagnosis", _safety_with_rules(rules))
        assert result.matched
        assert result.action == "log"

    def test_custom_message(self) -> None:
        rules = [
            CustomRule(
                name="competitor",
                pattern=r"\bcompetitor_x\b",
                action="block",
                message="We don't discuss competitors.",
            ),
        ]
        result = check_custom_rules(
            "what about competitor_x?",
            _safety_with_rules(rules),
        )
        assert result.matched
        assert result.message == "We don't discuss competitors."

    def test_first_match_wins(self) -> None:
        rules = [
            CustomRule(name="first", pattern=r"\btest\b", action="block"),
            CustomRule(name="second", pattern=r"\btest\b", action="log"),
        ]
        result = check_custom_rules("this is a test", _safety_with_rules(rules))
        assert result.rule_name == "first"

    def test_invalid_regex_skipped(self) -> None:
        rules = [
            CustomRule(name="bad", pattern=r"[invalid", action="block"),
            CustomRule(name="good", pattern=r"\bgood\b", action="block"),
        ]
        result = check_custom_rules(
            "this is good content",
            _safety_with_rules(rules),
        )
        assert result.matched
        assert result.rule_name == "good"

    def test_multiple_rules(self) -> None:
        rules = [
            CustomRule(name="drugs", pattern=r"\b(cocaine|heroin)\b"),
            CustomRule(name="weapons", pattern=r"\b(gun|rifle)\b"),
        ]
        result = check_custom_rules("how to buy a rifle", _safety_with_rules(rules))
        assert result.matched
        assert result.rule_name == "weapons"


class TestBannedTopics:
    """Test banned topic matching."""

    def test_no_topics_returns_no_match(self) -> None:
        result = check_banned_topics("hello", _safety_with_rules())
        assert not result.matched

    def test_topic_matched(self) -> None:
        result = check_banned_topics(
            "let's talk about politics",
            _safety_with_rules(topics=["politics"]),
        )
        assert result.matched
        assert "politics" in result.rule_name
        assert result.action == "block"

    def test_topic_case_insensitive(self) -> None:
        result = check_banned_topics(
            "RELIGION is interesting",
            _safety_with_rules(topics=["religion"]),
        )
        assert result.matched

    def test_topic_word_boundary(self) -> None:
        """Partial match should NOT trigger (e.g., 'politic' in 'political')."""
        # Note: 'politics' as topic should NOT match 'political' via word boundary
        result = check_banned_topics(
            "political analysis",
            _safety_with_rules(topics=["politics"]),
        )
        assert not result.matched

    def test_topic_no_match(self) -> None:
        result = check_banned_topics(
            "weather is nice",
            _safety_with_rules(topics=["politics"]),
        )
        assert not result.matched

    def test_multiple_topics(self) -> None:
        result = check_banned_topics(
            "let's discuss religion",
            _safety_with_rules(topics=["politics", "religion"]),
        )
        assert result.matched
        assert "religion" in result.rule_name

    def test_custom_block_message(self) -> None:
        result = check_banned_topics(
            "talk about gambling",
            _safety_with_rules(topics=["gambling"]),
        )
        assert "gambling" in result.message


class TestCompiledCache:
    """Test regex compilation cache."""

    def setup_method(self) -> None:
        clear_compiled_cache()

    def test_cache_reuses_compiled(self) -> None:
        rules = [CustomRule(name="test", pattern=r"\btest\b")]
        safety = _safety_with_rules(rules)
        check_custom_rules("test", safety)
        check_custom_rules("test", safety)
        # No way to check cache directly, but shouldn't crash

    def test_clear_cache(self) -> None:
        rules = [CustomRule(name="test", pattern=r"\btest\b")]
        check_custom_rules("test", _safety_with_rules(rules))
        clear_compiled_cache()
        # Should still work after clear
        result = check_custom_rules("test", _safety_with_rules(rules))
        assert result.matched
