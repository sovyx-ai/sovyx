"""Shared pattern types — categories, tiers, and the SafetyPattern dataclass.

Imported by every catalog module under ``cognitive/safety/``. Kept here so
that adding a new language catalog or tier is a single import, not a
rewrite of ``safety_patterns.py``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum, unique


@unique
class PatternCategory(StrEnum):
    """Safety pattern categories for audit trail."""

    VIOLENCE = "violence"
    WEAPONS = "weapons"
    SELF_HARM = "self_harm"
    HACKING = "hacking"
    SUBSTANCE = "substance"
    SEXUAL = "sexual"
    GAMBLING = "gambling"
    HATE_SPEECH = "hate_speech"
    MANIPULATION = "manipulation"
    ILLEGAL = "illegal"
    INJECTION = "injection"


@unique
class FilterTier(StrEnum):
    """Content filter tiers — each tier includes all lower tiers."""

    STANDARD = "standard"
    STRICT = "strict"
    CHILD_SAFE = "child_safe"


@dataclass(frozen=True, slots=True)
class SafetyPattern:
    """A compiled safety pattern with metadata.

    Attributes:
        regex: Compiled case-insensitive regex pattern.
        category: Pattern category for audit/metrics.
        tier: Minimum tier that activates this pattern.
        description: Human-readable description (for docs/debug).
    """

    regex: re.Pattern[str]
    category: PatternCategory
    tier: FilterTier
    description: str


def _p(
    pattern: str,
    category: PatternCategory,
    tier: FilterTier,
    description: str,
) -> SafetyPattern:
    """Shorthand to create a compiled SafetyPattern."""
    return SafetyPattern(
        regex=re.compile(pattern, re.IGNORECASE),
        category=category,
        tier=tier,
        description=description,
    )
