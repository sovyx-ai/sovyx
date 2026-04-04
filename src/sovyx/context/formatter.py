"""Sovyx context formatter — format brain data for LLM consumption.

SPE-006 §5: The LLM is a human reader. Formatting matters as much as content.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sovyx.engine.types import ConceptCategory
from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from sovyx.brain.models import Concept, Episode
    from sovyx.context.tokenizer import TokenCounter

logger = get_logger(__name__)

# Emoji map (covers all 7 ConceptCategory values + fallback)
_EMOJI_MAP: dict[ConceptCategory, str] = {
    ConceptCategory.FACT: "📋",
    ConceptCategory.PREFERENCE: "❤️",
    ConceptCategory.ENTITY: "👤",
    ConceptCategory.SKILL: "🔧",
    ConceptCategory.BELIEF: "💭",
    ConceptCategory.EVENT: "📅",
    ConceptCategory.RELATIONSHIP: "🔗",
}
_FALLBACK_EMOJI = "📌"


class ContextFormatter:
    """Format brain data as readable text for LLM context."""

    def __init__(self, token_counter: TokenCounter) -> None:
        self._counter = token_counter

    def format_concept(self, concept: Concept, activation: float = 0.0) -> str:
        """Format a single concept for LLM context.

        Format: "{emoji} {content}{confidence_marker}"

        Args:
            concept: The concept to format.
            activation: Activation score (unused in format, for sorting).

        Returns:
            Formatted concept string.
        """
        emoji = _EMOJI_MAP.get(concept.category, _FALLBACK_EMOJI)
        text = concept.content or concept.name
        marker = ""
        if concept.confidence < 0.3:  # noqa: PLR2004
            marker = " (uncertain — verify before stating)"
        elif concept.confidence < 0.5:  # noqa: PLR2004
            marker = " (you're not very sure about this)"
        return f"{emoji} {text}{marker}"

    def format_episode(self, episode: Episode) -> str:
        """Format a single episode for LLM context.

        Format: "🕐 {time_ago}: {summary_or_truncated_input}"
        """
        time_ago = self._human_time_ago(episode.created_at)
        text = episode.user_input
        if len(text) > 100:  # noqa: PLR2004
            text = text[:97] + "..."
        return f"🕐 {time_ago}: {text}"

    def format_concepts_block(
        self,
        concepts: list[tuple[Concept, float]],
        budget_tokens: int,
    ) -> str:
        """Format concept list respecting token budget.

        Applies Lost-in-Middle ordering for attention optimization.
        """
        if not concepts:
            return ""

        ordered = self._order_for_attention(concepts)
        lines = ["## What you know about this person:"]
        used = self._counter.count(lines[0])

        for item, score in ordered:
            line = self.format_concept(item, score)
            line_tokens = self._counter.count(line)
            if used + line_tokens > budget_tokens:
                break
            lines.append(line)
            used += line_tokens

        if len(lines) <= 1:
            return ""
        return "\n".join(lines)

    def format_episodes_block(
        self,
        episodes: list[Episode],
        budget_tokens: int,
    ) -> str:
        """Format episode list respecting token budget."""
        if not episodes:
            return ""

        lines = ["## Recent conversations:"]
        used = self._counter.count(lines[0])

        for episode in episodes:
            line = self.format_episode(episode)
            line_tokens = self._counter.count(line)
            if used + line_tokens > budget_tokens:
                break
            lines.append(line)
            used += line_tokens

        if len(lines) <= 1:
            return ""
        return "\n".join(lines)

    def format_temporal(self, timezone: str = "UTC") -> str:
        """Current temporal context (SPE-006 §format_temporal).

        Uses ``zoneinfo.ZoneInfo`` (stdlib, zero deps) to convert UTC
        to the mind's configured timezone.  Falls back to UTC on
        invalid timezone names (logged as warning).

        Returns string like:
        "Current date and time: Monday, March 30, 2026, 6:41 AM (America/Sao_Paulo)."
        """
        try:
            tz = ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, KeyError):
            logger.warning("invalid_timezone_falling_back_to_utc", timezone=timezone)
            tz = UTC  # type: ignore[assignment]
        now = datetime.now(tz=tz)
        formatted = now.strftime("%A, %B %d, %Y, %I:%M %p")
        return f"Current date and time: {formatted} ({timezone})."

    @staticmethod
    def _order_for_attention(
        items: list[tuple[Concept, float]],
    ) -> list[tuple[Concept, float]]:
        """Lost-in-the-Middle ordering (Liu et al. 2023).

        Most relevant at start and end, least relevant in middle.
        """
        sorted_items = sorted(items, key=lambda x: x[1], reverse=True)
        high = sorted_items[::2]
        low = sorted_items[1::2]
        return high + list(reversed(low))

    @staticmethod
    def _human_time_ago(dt: datetime) -> str:
        """Convert datetime to human-readable time ago string."""
        now = datetime.now(tz=UTC)
        # Defensive: normalise naive datetimes (from SQLite) to UTC
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        delta = now - dt

        if delta < timedelta(minutes=1):
            return "just now"
        if delta < timedelta(hours=1):
            mins = int(delta.total_seconds() / 60)
            return f"{mins} minute{'s' if mins != 1 else ''} ago"
        if delta < timedelta(hours=24):
            hours = int(delta.total_seconds() / 3600)
            return f"{hours} hour{'s' if hours != 1 else ''} ago"
        if delta < timedelta(days=2):
            return "yesterday"
        if delta < timedelta(weeks=1):
            days = delta.days
            return f"{days} days ago"
        if delta < timedelta(weeks=4):
            weeks = delta.days // 7
            return f"{weeks} week{'s' if weeks != 1 else ''} ago"
        months = delta.days // 30
        if months < 1:
            return "about a month ago"
        return f"{months} month{'s' if months != 1 else ''} ago"
