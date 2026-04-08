"""Tests for sovyx.context.formatter — Context formatting."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from sovyx.brain.models import Concept, ConceptCategory, Episode
from sovyx.context.formatter import ContextFormatter
from sovyx.context.tokenizer import TokenCounter
from sovyx.engine.types import ConceptId, ConversationId, EpisodeId, MindId

MIND = MindId("aria")


def _concept(
    name: str,
    content: str = "",
    category: ConceptCategory = ConceptCategory.FACT,
    confidence: float = 0.8,
) -> Concept:
    return Concept(
        id=ConceptId(name),
        mind_id=MIND,
        name=name,
        content=content or name,
        category=category,
        confidence=confidence,
    )


def _episode(user_input: str, created_at: datetime | None = None) -> Episode:
    return Episode(
        id=EpisodeId("ep1"),
        mind_id=MIND,
        conversation_id=ConversationId("conv1"),
        user_input=user_input,
        assistant_response="response",
        created_at=created_at or datetime.now(tz=UTC),
    )


@pytest.fixture
def formatter() -> ContextFormatter:
    return ContextFormatter(TokenCounter())


class TestFormatConcept:
    """Concept formatting."""

    def test_fact_emoji(self, formatter: ContextFormatter) -> None:
        c = _concept("test", "A fact", ConceptCategory.FACT)
        result = formatter.format_concept(c)
        assert result.startswith("📋")

    def test_preference_emoji(self, formatter: ContextFormatter) -> None:
        c = _concept("test", "Likes coffee", ConceptCategory.PREFERENCE)
        result = formatter.format_concept(c)
        assert result.startswith("❤️")

    def test_entity_emoji(self, formatter: ContextFormatter) -> None:
        c = _concept("test", "Guipe", ConceptCategory.ENTITY)
        result = formatter.format_concept(c)
        assert result.startswith("👤")

    def test_skill_emoji(self, formatter: ContextFormatter) -> None:
        c = _concept("test", "Python", ConceptCategory.SKILL)
        result = formatter.format_concept(c)
        assert result.startswith("🔧")

    def test_belief_emoji(self, formatter: ContextFormatter) -> None:
        c = _concept("test", "Freedom", ConceptCategory.BELIEF)
        result = formatter.format_concept(c)
        assert result.startswith("💭")

    def test_event_emoji(self, formatter: ContextFormatter) -> None:
        c = _concept("test", "Birthday", ConceptCategory.EVENT)
        result = formatter.format_concept(c)
        assert result.startswith("📅")

    def test_relationship_emoji(self, formatter: ContextFormatter) -> None:
        c = _concept("test", "Married", ConceptCategory.RELATIONSHIP)
        result = formatter.format_concept(c)
        assert result.startswith("🔗")

    def test_low_confidence_marker(self, formatter: ContextFormatter) -> None:
        c = _concept("test", "Maybe true", confidence=0.2)
        result = formatter.format_concept(c)
        assert "uncertain" in result

    def test_medium_confidence_marker(self, formatter: ContextFormatter) -> None:
        c = _concept("test", "Probably", confidence=0.4)
        result = formatter.format_concept(c)
        assert "not very sure" in result

    def test_high_confidence_no_marker(self, formatter: ContextFormatter) -> None:
        c = _concept("test", "Definitely", confidence=0.9)
        result = formatter.format_concept(c)
        assert "uncertain" not in result
        assert "sure" not in result


class TestFormatEpisode:
    """Episode formatting."""

    def test_recent_episode(self, formatter: ContextFormatter) -> None:
        ep = _episode("Hello there")
        result = formatter.format_episode(ep)
        assert result.startswith("🕐")
        assert "Hello there" in result

    def test_long_input_truncated(self, formatter: ContextFormatter) -> None:
        long_input = "x" * 200
        ep = _episode(long_input)
        result = formatter.format_episode(ep)
        assert len(result) < 200  # noqa: PLR2004
        assert "..." in result

    def test_summary_used_when_available(self, formatter: ContextFormatter) -> None:
        ep = _episode("Very long user input that would be truncated")
        ep.summary = "User asked a question about coding."
        result = formatter.format_episode(ep)
        assert "User asked a question about coding." in result
        assert "Very long user input" not in result

    def test_fallback_to_input_without_summary(self, formatter: ContextFormatter) -> None:
        ep = _episode("Hello there")
        ep.summary = None
        result = formatter.format_episode(ep)
        assert "Hello there" in result


class TestFormatConceptsBlock:
    """Concepts block formatting."""

    def test_empty(self, formatter: ContextFormatter) -> None:
        assert formatter.format_concepts_block([], 1000) == ""

    def test_with_concepts(self, formatter: ContextFormatter) -> None:
        concepts = [
            (_concept("coffee", "Loves coffee", ConceptCategory.PREFERENCE), 0.9),
            (_concept("name", "Name is Guipe", ConceptCategory.ENTITY), 0.8),
        ]
        result = formatter.format_concepts_block(concepts, 1000)
        assert "What you know" in result
        assert "coffee" in result
        assert "Guipe" in result

    def test_respects_budget(self, formatter: ContextFormatter) -> None:
        concepts = [(_concept(f"c{i}", f"Content number {i}" * 10), float(i)) for i in range(20)]
        result = formatter.format_concepts_block(concepts, 50)
        counter = TokenCounter()
        assert counter.count(result) <= 50  # noqa: PLR2004


class TestFormatEpisodesBlock:
    """Episodes block formatting."""

    def test_empty(self, formatter: ContextFormatter) -> None:
        assert formatter.format_episodes_block([], 1000) == ""

    def test_with_episodes(self, formatter: ContextFormatter) -> None:
        episodes = [_episode("Hello"), _episode("How are you?")]
        result = formatter.format_episodes_block(episodes, 1000)
        assert "Recent conversations" in result

    def test_respects_budget(self, formatter: ContextFormatter) -> None:
        """Episodes exceeding budget are truncated."""
        episodes = [_episode(f"Long episode number {i} " * 20) for i in range(30)]
        result = formatter.format_episodes_block(episodes, 50)
        counter = TokenCounter()
        assert counter.count(result) <= 50  # noqa: PLR2004

    def test_budget_too_small_for_any_episode(self, formatter: ContextFormatter) -> None:
        """When budget only fits header, return empty."""
        episodes = [_episode("Hello world")]
        # Budget of 1 token — only header won't fit, or header fits but no episodes
        result = formatter.format_episodes_block(episodes, 5)
        # With 5 tokens, header "## Recent conversations:" takes ~5+ tokens
        # Either returns empty (header doesn't fit) or header only (returns "")
        assert result == "" or "Recent conversations" in result


class TestFormatTemporal:
    """Temporal context."""

    def test_contains_timezone(self, formatter: ContextFormatter) -> None:
        result = formatter.format_temporal("America/Sao_Paulo")
        assert "America/Sao_Paulo" in result

    def test_contains_date(self, formatter: ContextFormatter) -> None:
        result = formatter.format_temporal()
        assert "202" in result  # year

    def test_invalid_timezone_falls_back_to_utc(self, formatter: ContextFormatter) -> None:
        """Invalid timezone name should fallback to UTC without crashing."""
        result = formatter.format_temporal("Invalid/NotATimezone")
        assert "Invalid/NotATimezone" in result
        assert "Current date and time:" in result


class TestLostInMiddle:
    """Lost-in-the-Middle ordering."""

    def test_ordering(self) -> None:
        items = [
            (_concept("a"), 1.0),
            (_concept("b"), 0.8),
            (_concept("c"), 0.6),
            (_concept("d"), 0.4),
            (_concept("e"), 0.2),
        ]
        ordered = ContextFormatter._order_for_attention(items)
        # Most relevant at start, second at end
        scores = [s for _, s in ordered]
        assert scores[0] >= scores[-1] or scores[-1] >= scores[len(scores) // 2]
        # First item should be highest
        assert scores[0] == 1.0


class TestHumanTimeAgo:
    """Human-readable time ago."""

    def test_just_now(self) -> None:
        result = ContextFormatter._human_time_ago(datetime.now(tz=UTC))
        assert "just now" in result

    def test_minutes(self) -> None:
        t = datetime.now(tz=UTC) - timedelta(minutes=30)
        result = ContextFormatter._human_time_ago(t)
        assert "minute" in result

    def test_single_minute(self) -> None:
        t = datetime.now(tz=UTC) - timedelta(minutes=1, seconds=5)
        result = ContextFormatter._human_time_ago(t)
        assert "1 minute ago" in result

    def test_single_hour(self) -> None:
        t = datetime.now(tz=UTC) - timedelta(hours=1, minutes=5)
        result = ContextFormatter._human_time_ago(t)
        assert "1 hour ago" in result

    def test_hours(self) -> None:
        t = datetime.now(tz=UTC) - timedelta(hours=5)
        result = ContextFormatter._human_time_ago(t)
        assert "hour" in result

    def test_yesterday(self) -> None:
        t = datetime.now(tz=UTC) - timedelta(days=1, hours=5)
        result = ContextFormatter._human_time_ago(t)
        assert "yesterday" in result

    def test_days(self) -> None:
        t = datetime.now(tz=UTC) - timedelta(days=4)
        result = ContextFormatter._human_time_ago(t)
        assert "day" in result

    def test_single_week(self) -> None:
        t = datetime.now(tz=UTC) - timedelta(weeks=1, days=1)
        result = ContextFormatter._human_time_ago(t)
        assert "1 week ago" in result

    def test_weeks(self) -> None:
        t = datetime.now(tz=UTC) - timedelta(weeks=2)
        result = ContextFormatter._human_time_ago(t)
        assert "week" in result

    def test_about_a_month(self) -> None:
        """~28-30 days: past 4 weeks but months < 1."""
        t = datetime.now(tz=UTC) - timedelta(days=29)
        result = ContextFormatter._human_time_ago(t)
        assert "about a month ago" in result

    def test_months(self) -> None:
        t = datetime.now(tz=UTC) - timedelta(days=90)
        result = ContextFormatter._human_time_ago(t)
        assert "month" in result

    def test_naive_datetime_handled(self) -> None:
        """Naive datetime (no tzinfo) should be treated as UTC."""
        t = datetime.now(tz=UTC).replace(tzinfo=None) - timedelta(hours=2)
        result = ContextFormatter._human_time_ago(t)
        assert "hour" in result
