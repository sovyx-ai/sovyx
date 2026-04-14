"""Auto-extracted from cognitive/reflect.py — see __init__.py for the public re-exports."""

from __future__ import annotations

import re

# ── Sentiment heuristics for regex fallback ────────────────────────────
# Maps pattern groups to default sentiment when LLM is unavailable.

_POSITIVE_WORDS = frozenset(
    {
        "love",
        "like",
        "prefer",
        "enjoy",
        "great",
        "awesome",
        "excellent",
        "best",
        "amazing",
        "adoro",
        "gosto",
        "curto",
    }
)
_NEGATIVE_WORDS = frozenset(
    {
        "hate",
        "dislike",
        "avoid",
        "terrible",
        "worst",
        "awful",
        "frustrating",
        "harmful",
        "bad",
        "odeio",
        "detesto",
    }
)


def _estimate_sentiment(text: str) -> float:
    """Heuristic sentiment estimation for regex fallback.

    Returns a rough sentiment score based on keyword presence.
    """
    lower = text.lower()
    words = set(lower.split())
    pos = len(words & _POSITIVE_WORDS)
    neg = len(words & _NEGATIVE_WORDS)
    if pos > neg:
        return min(0.6, 0.3 * pos)
    if neg > pos:
        return max(-0.6, -0.3 * neg)
    return 0.0


# ── Regex fallback patterns ────────────────────────────────────────────

_ENTITY_PATTERNS = [
    re.compile(r"(?:my name is|i'm|i am)\s+(\w+)", re.IGNORECASE),
    re.compile(r"(?:meu nome é|me chamo|sou o|sou a)\s+(\w+)", re.IGNORECASE),
]

_PREFERENCE_PATTERNS = [
    re.compile(
        r"(?:i (?:like|love|prefer|enjoy|use|work with))\s+(.+?)(?:\.|,|!|$)",
        re.IGNORECASE,
    ),
    re.compile(r"(?:i (?:hate|dislike|avoid))\s+(.+?)(?:\.|,|!|$)", re.IGNORECASE),
    re.compile(
        r"(?:eu (?:gosto|adoro|prefiro|curto|uso))"
        r"\s+(?:de |do |da )?(.+?)(?:\.|,|!|$)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:my (?:stack|tools?|setup) (?:is|includes?|:))"
        r"\s*(.+?)(?:\.|!|$)",
        re.IGNORECASE,
    ),
]

_FACT_PATTERNS = [
    re.compile(
        r"(?:i (?:work at|work for|live in|study at|am building|built))"
        r"\s+(.+?)(?:\.|,|!|$)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:i'm (?:building|developing|working on|learning))"
        r"\s+(.+?)(?:\.|,|!|$)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:(?:trabalho|moro|estudo) (?:na|no|em|na))"
        r"\s+(.+?)(?:\.|,|!|$)",
        re.IGNORECASE,
    ),
]

_SKILL_PATTERNS = [
    re.compile(
        r"(?:i (?:code|program|develop) (?:in|with))\s+(.+?)(?:\.|,|!|$)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:my (?:primary |main )?(?:language|stack|framework)s?"
        r"\s+(?:is|are|:))\s*(.+?)(?:\.|!|$)",
        re.IGNORECASE,
    ),
]

_BELIEF_PATTERNS = [
    re.compile(
        r"(?:i (?:think|believe|feel that|consider))\s+(.+?)(?:\.|,|!|$)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:in my (?:opinion|view|experience))\s*[,:]?\s*(.+?)(?:\.|!|$)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:eu (?:acho|acredito|penso) que)\s+(.+?)(?:\.|,|!|$)",
        re.IGNORECASE,
    ),
]

_EVENT_PATTERNS = [
    re.compile(
        r"(?:i (?:started|finished|completed|launched|migrated"
        r"|deployed|graduated))"
        r"\s+(.+?)(?:\.|,|!|$)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:last (?:week|month|year)|recently|yesterday|in \d{4})"
        r"\s*[,:]?\s*"
        r"(?:i |we )?(.+?)(?:\.|,|!|$)",
        re.IGNORECASE,
    ),
]

_RELATIONSHIP_PATTERNS = [
    re.compile(
        r"(?:i (?:manage|lead|report to|work with|mentor))"
        r"\s+(.+?)(?:\.|,|!|$)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:my (?:team|manager|boss|colleague|partner) (?:is|are))"
        r"\s+(.+?)(?:\.|,|!|$)",
        re.IGNORECASE,
    ),
]
