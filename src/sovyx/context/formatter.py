"""Sovyx context formatter — format brain data for LLM consumption.

SPE-006 §5: The LLM is a human reader. Formatting matters as much as content.
"""

from __future__ import annotations

import datetime as _dt
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, TypeVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sovyx.engine.types import ConceptCategory
from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from sovyx.brain.models import Concept, Episode
    from sovyx.context.tokenizer import TokenCounter

# TypeVar for `_trim_to_budget` — the helper is identical for
# concepts and episodes; both pass a list of (item, line) tuples
# and the trimming logic only inspects the line side.
_TrimItem = TypeVar("_TrimItem")

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

        Format: "{emoji} {importance_prefix}{content}{confidence_marker}"

        Confidence markers (recalibrated for dynamic 0.10-0.95 range):
        - < 0.25: very uncertain — do NOT state as fact
        - < 0.45: uncertain — verify before stating
        - < 0.60: possibly — not fully sure
        - >= 0.60: no marker (confident enough)

        Importance prefix:
        - >= 0.85: ⭐ (core knowledge — always prioritize)

        Args:
            concept: The concept to format.
            activation: Activation score (unused in format, for sorting).

        Returns:
            Formatted concept string.
        """
        emoji = _EMOJI_MAP.get(concept.category, _FALLBACK_EMOJI)
        text = concept.content or concept.name

        # Importance prefix for core knowledge
        prefix = "⭐ " if concept.importance >= 0.85 else ""  # noqa: PLR2004

        # Confidence markers (recalibrated for dynamic scoring range)
        marker = ""
        if concept.confidence < 0.25:  # noqa: PLR2004
            marker = " ⚠️ (very uncertain — do NOT state as fact)"
        elif concept.confidence < 0.45:  # noqa: PLR2004
            marker = " (uncertain — verify before stating)"
        elif concept.confidence < 0.60:  # noqa: PLR2004
            marker = " (possibly — you're not fully sure)"

        return f"{emoji} {prefix}{text}{marker}"

    def format_episode(self, episode: Episode) -> str:
        """Format a single episode for LLM context.

        Uses episode.summary when available (dense, ~20 tokens).
        Falls back to truncated user_input (current behavior).

        Format: "🕐 {time_ago}: {summary_or_truncated_input}"
        """
        time_ago = self._human_time_ago(episode.created_at)
        if episode.summary:
            text = episode.summary
        else:
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

        Two-phase budget enforcement (BPE non-subadditivity safe):

        1. **Piecewise admit** — iterate ordered concepts, admitting
           each one whose individual ``count(line)`` keeps the
           running sum under ``budget_tokens``. This is the cheap
           first-pass filter.
        2. **Final-count + trim** — count the actual joined output
           via ``count("\\n".join(...))``. BPE is NOT subadditive
           (see ``test_bpe_concatenation_can_exceed_constant_slack``
           in ``tests/unit/test_brain_invariants.py``); the joined
           output can carry up to ``max(byte_len(line))`` extra
           tokens per concatenation boundary in pathological cases.
           Pop concepts from the END of the admitted list (least-
           relevant per Lost-in-Middle ordering — same direction the
           piecewise pass would have stopped) until the joined count
           fits. Mirrors the canonical pattern in
           ``assembler.py:160-172``.

        Metadata bumps for ``context_inclusion_count`` happen ONLY
        after both passes settle, so a concept the BPE-correction
        pass trims back out doesn't accumulate a stale inclusion
        count.
        """
        if not concepts:
            return ""

        header = "## What you know about this person:"
        ordered = self._order_for_attention(concepts)

        admitted: list[tuple[Concept, str]] = []
        used = self._counter.count(header)

        for item, score in ordered:
            line = self.format_concept(item, score)
            line_tokens = self._counter.count(line)
            if used + line_tokens > budget_tokens:
                break
            admitted.append((item, line))
            used += line_tokens

        admitted = self._trim_to_budget(header, admitted, budget_tokens)
        if not admitted:
            return ""

        for item, _ in admitted:
            inc_raw = item.metadata.get("context_inclusion_count", 0)
            inc = int(inc_raw) if isinstance(inc_raw, (int, float, str)) else 0
            item.metadata["context_inclusion_count"] = inc + 1

        return "\n".join([header, *(line for _, line in admitted)])

    def _trim_to_budget(
        self,
        header: str,
        admitted: list[tuple[_TrimItem, str]],
        budget_tokens: int,
    ) -> list[tuple[_TrimItem, str]]:
        """Final-count + trim pass for the joined output.

        Iteratively pops items from the END of ``admitted`` until
        ``count("\\n".join([header, *lines])) <= budget_tokens``.

        Args:
            header: The block's leading header line (counted as
                part of the output).
            admitted: Items the piecewise pass admitted, paired
                with their formatted lines. Generic over the item
                type — concepts and episodes both flow through
                this single helper.
            budget_tokens: Hard upper bound for the joined output's
                token count.

        Returns:
            The (possibly shorter) admitted list. Empty list when
            even the header alone exceeds the budget OR when no
            single line can fit alongside the header (caller must
            handle this by returning an empty block).
        """
        while admitted:
            joined = "\n".join([header, *(line for _, line in admitted)])
            if self._counter.count(joined) <= budget_tokens:
                return admitted
            admitted = admitted[:-1]
        return admitted

    def format_episodes_block(
        self,
        episodes: list[Episode],
        budget_tokens: int,
    ) -> str:
        """Format episode list respecting token budget.

        Same two-phase budget enforcement as
        :meth:`format_concepts_block` — see that method's docstring
        for the BPE non-subadditivity rationale.
        """
        if not episodes:
            return ""

        header = "## Recent conversations:"
        admitted: list[tuple[Episode, str]] = []
        used = self._counter.count(header)

        for episode in episodes:
            line = self.format_episode(episode)
            line_tokens = self._counter.count(line)
            if used + line_tokens > budget_tokens:
                break
            admitted.append((episode, line))
            used += line_tokens

        admitted = self._trim_to_budget(header, admitted, budget_tokens)
        if not admitted:
            return ""
        return "\n".join([header, *(line for _, line in admitted)])

    def format_temporal(self, timezone: str = "UTC") -> str:
        """Current temporal context (SPE-006 §format_temporal).

        Uses ``zoneinfo.ZoneInfo`` (stdlib, zero deps) to convert UTC
        to the mind's configured timezone.  Falls back to UTC on
        invalid timezone names (logged as warning).

        Returns string like:
        "Current date and time: Monday, March 30, 2026, 6:41 AM (America/Sao_Paulo)."
        """
        resolved_tz: ZoneInfo | _dt.timezone
        try:
            resolved_tz = ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, KeyError):
            logger.warning("invalid_timezone_falling_back_to_utc", timezone=timezone)
            resolved_tz = UTC
        now = datetime.now(tz=resolved_tz)
        formatted = now.strftime("%A, %B %d, %Y, %I:%M %p")
        return f"Current date and time: {formatted} ({timezone})."

    @staticmethod
    def _order_for_attention(
        items: list[tuple[Concept, float]],
    ) -> list[tuple[Concept, float]]:
        """Lost-in-the-Middle ordering with importance-weighted scoring.

        Combined score = 0.65 * search_relevance + 0.35 * importance.
        This ensures high-importance concepts survive budget cuts even
        if their text match is slightly lower.

        Most relevant at start and end, least relevant in middle
        (Liu et al. 2023).
        """
        weighted = [
            (concept, 0.65 * score + 0.35 * concept.importance) for concept, score in items
        ]
        sorted_items = sorted(weighted, key=lambda x: x[1], reverse=True)
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
