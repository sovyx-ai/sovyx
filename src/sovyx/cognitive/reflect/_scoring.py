"""Auto-extracted from cognitive/reflect.py — see __init__.py for the public re-exports."""

from __future__ import annotations

from sovyx.cognitive.reflect._categories import (
    _CATEGORY_MAP,
    _DEFAULT_IMPORTANCE,
    _DEFAULT_SOURCE_CONFIDENCE,
    _EXPLICIT_PATTERNS,
    _IMPORTANCE,
    _SOURCE_CONFIDENCE,
)


def resolve_category(raw_category: str) -> str:
    """Resolve a raw LLM category string to a canonical ConceptCategory value.

    Uses ``_CATEGORY_MAP`` for alias resolution. Falls back to ``"fact"``
    for unknown categories.

    Args:
        raw_category: The raw category string from LLM or regex extraction.

    Returns:
        A canonical category string matching a ``ConceptCategory`` enum value.
    """
    return _CATEGORY_MAP.get(raw_category.strip().lower(), "fact")


def get_importance(category: str) -> float:
    """Return initial importance for a concept category.

    Args:
        category: Canonical category string (after ``resolve_category``).

    Returns:
        Importance value in [0.0, 1.0].
    """
    return _IMPORTANCE.get(category, _DEFAULT_IMPORTANCE)


def get_source_confidence(source: str) -> float:
    """Return midpoint confidence for extraction source quality.

    Maps the extraction method to an epistemic certainty score.
    LLM explicit extraction → high confidence; regex fallback → lower.

    Args:
        source: Source type key (e.g. ``"llm_explicit"``, ``"regex_fallback"``).

    Returns:
        Confidence midpoint in [0.0, 1.0].
    """
    low, high = _SOURCE_CONFIDENCE.get(source, _DEFAULT_SOURCE_CONFIDENCE)
    return (low + high) / 2


def detect_explicit_importance(message: str) -> bool:
    """Detect if user explicitly asks to remember information.

    Checks for phrases like "remember this", "don't forget",
    "lembra disso", etc. in both English and Portuguese.

    When True, ALL concepts from this message get their importance
    floor raised to 0.85 and confidence floor raised to 0.75.

    Args:
        message: User message text.

    Returns:
        True if explicit importance signal detected.
    """
    return any(p.search(message) for p in _EXPLICIT_PATTERNS)


def compute_episode_importance(
    message: str,
    num_concepts: int,
    max_valence: float,
    concept_importances: list[float] | None = None,
) -> float:
    """Compute dynamic episode importance from message characteristics.

    Scoring formula (weights sum to 1.0):
    - Base (0.30): 0.3 + message_length / 500 (longer = more content)
    - Concepts (0.25): count bonus + mean concept importance
    - Emotion (0.15): |max_valence|
    - Concept importance signal (0.30): mean of extracted concept
      importance scores. Episodes containing high-importance concepts
      inherit that importance.
    - Clamped to [0.1, 1.0]

    If ``concept_importances`` is empty or None, falls back to the
    original count-only formula for backwards compatibility.

    Args:
        message: The user's input message.
        num_concepts: Number of concepts extracted from the message.
        max_valence: Maximum absolute sentiment across concepts.
        concept_importances: Importance scores of extracted concepts.

    Returns:
        Episode importance in [0.1, 1.0].
    """
    base = min(0.7, 0.3 + len(message) / 500)
    concept_count_bonus = 0.05 * min(num_concepts, 6)
    emotion_bonus = 0.1 * abs(max_valence)

    if concept_importances:
        mean_importance = sum(concept_importances) / len(concept_importances)
        # Weighted: base(0.30) + concept_count(0.10) + emotion(0.15) +
        # mean_concept_importance(0.45)
        score = (
            0.30 * base
            + 0.10 * concept_count_bonus * 5  # Scale 0-0.30 → 0-1.5 → capped
            + 0.15 * abs(max_valence)
            + 0.45 * mean_importance
        )
    else:
        # Fallback: original formula (no concept scores available)
        score = base + concept_count_bonus + emotion_bonus

    return max(0.1, min(1.0, score))


def clamp_sentiment(value: float) -> float:
    """Clamp a sentiment value to [-1.0, 1.0].

    Args:
        value: Raw sentiment value.

    Returns:
        Clamped value in [-1.0, 1.0].
    """
    return max(-1.0, min(1.0, value))
