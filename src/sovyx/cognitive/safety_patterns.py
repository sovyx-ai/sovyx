"""Sovyx safety patterns — tiered content filtering with compiled regex.

Three tiers (each includes the lower):

- **none**: zero filtering, empty pattern set.
- **standard**: blocks direct harm — weapons, explosives, self-harm,
  hacking with destructive intent.
- **strict**: standard + adult content, substances, gambling, hate
  speech, controversial manipulation.
- **child_safe**: superset of strict.

Each pattern carries a category and tier (audit trail). Catalogs are
compiled once at import time and live under ``cognitive/safety/`` per
language; this module is the public entry point and dispatches to the
correct tier based on ``SafetyConfig``.

Design principles:
- Regex over substring — catches inflections, variations, typos.
- Category-tagged — enables per-category metrics and audit.
- False-positive-aware — patterns are specific enough to avoid blocking
  legitimate educational / news content. ``"bomb"`` alone does not
  trigger; ``"how to build a bomb"`` does.
- Performance — short-circuit on first match.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sovyx.cognitive.safety._pattern_types import (
    FilterTier,
    PatternCategory,
    SafetyPattern,
    _p,
)
from sovyx.cognitive.safety.patterns_child_safe import _CHILD_SAFE_PATTERNS
from sovyx.cognitive.safety.patterns_en import (
    _INJECTION_PATTERNS,
    _STANDARD_PATTERNS,
    _STRICT_PATTERNS,
)
from sovyx.cognitive.safety.patterns_es import (
    _ES_INJECTION_PATTERNS,
    _ES_STANDARD_PATTERNS,
    _ES_STRICT_PATTERNS,
)
from sovyx.cognitive.safety.patterns_pt import (
    _PT_INJECTION_PATTERNS,
    _PT_STANDARD_PATTERNS,
    _PT_STRICT_PATTERNS,
)

if TYPE_CHECKING:
    from sovyx.mind.config import SafetyConfig


# Re-exports — keep the public API of this module backward-compatible.
__all__ = [
    "ALL_CHILD_SAFE_PATTERNS",
    "ALL_STANDARD_PATTERNS",
    "ALL_STRICT_PATTERNS",
    "NO_MATCH",
    "FilterMatch",
    "FilterTier",
    "PatternCategory",
    "SafetyPattern",
    "check_content",
    "get_pattern_count",
    "get_tier_counts",
    "resolve_patterns",
]


# ── Compiled pattern sets ──────────────────────────────────────────────
# Injection patterns are included in ALL active tiers (standard+).

_ALL_STANDARD_BASE: tuple[SafetyPattern, ...] = (
    _STANDARD_PATTERNS
    + _INJECTION_PATTERNS
    + _PT_STANDARD_PATTERNS
    + _PT_INJECTION_PATTERNS
    + _ES_STANDARD_PATTERNS
    + _ES_INJECTION_PATTERNS
)

# All multilingual strict patterns (superset of standard).
_ALL_STRICT_BASE: tuple[SafetyPattern, ...] = (
    _ALL_STANDARD_BASE + _STRICT_PATTERNS + _PT_STRICT_PATTERNS + _ES_STRICT_PATTERNS
)

ALL_STANDARD_PATTERNS: tuple[SafetyPattern, ...] = _ALL_STANDARD_BASE
ALL_STRICT_PATTERNS: tuple[SafetyPattern, ...] = _ALL_STRICT_BASE
ALL_CHILD_SAFE_PATTERNS: tuple[SafetyPattern, ...] = _ALL_STRICT_BASE + _CHILD_SAFE_PATTERNS


@dataclass(frozen=True, slots=True)
class FilterMatch:
    """Result of a safety pattern match.

    Attributes:
        matched: Whether any pattern matched.
        pattern: The first pattern that matched (None if no match).
        category: Category of the matched pattern.
        tier: Tier of the matched pattern.
    """

    matched: bool
    pattern: SafetyPattern | None = None
    category: PatternCategory | None = None
    tier: FilterTier | None = None


# Singleton "no match" result.
NO_MATCH = FilterMatch(matched=False)


def resolve_patterns(safety: SafetyConfig) -> tuple[SafetyPattern, ...]:
    """Resolve the active pattern set from current safety config.

    Args:
        safety: Current safety configuration.

    Returns:
        Tuple of active SafetyPattern instances. Empty tuple when filter
        is ``"none"`` (and child_safe is off).
    """
    if safety.child_safe_mode:
        return ALL_CHILD_SAFE_PATTERNS
    if safety.content_filter == "strict":
        return ALL_STRICT_PATTERNS
    if safety.content_filter == "standard":
        return ALL_STANDARD_PATTERNS
    # content_filter == "none"
    return ()


def check_content(text: str, safety: SafetyConfig) -> FilterMatch:
    """Check text against the active safety patterns.

    Short-circuits on first match for performance. Returns ``NO_MATCH``
    when filter is ``"none"`` (zero overhead).

    Args:
        text: Text to check (user message or LLM response).
        safety: Current safety configuration.

    Returns:
        FilterMatch with match details (or NO_MATCH).
    """
    patterns = resolve_patterns(safety)
    if not patterns:
        return NO_MATCH

    # Truncate to prevent DoS via oversized input (regex on 1MB+ = CPU hang).
    max_safety_input = 10_000
    truncated = text[:max_safety_input] if len(text) > max_safety_input else text

    from sovyx.cognitive.text_normalizer import normalize_text

    normalized = normalize_text(truncated)
    lower = normalized.lower()
    for p in patterns:
        if p.regex.search(lower):
            return FilterMatch(
                matched=True,
                pattern=p,
                category=p.category,
                tier=p.tier,
            )

    return NO_MATCH


def get_pattern_count(safety: SafetyConfig) -> int:
    """Return the number of active patterns for the current config.

    Useful for dashboard display ("Standard: 20 rules").
    """
    return len(resolve_patterns(safety))


def get_tier_counts() -> dict[str, int]:
    """Return pattern counts per tier.

    Returns:
        ``{"standard": N, "strict": M, "child_safe": K}`` where each tier
        includes everything in the lower tiers.
    """
    return {
        "standard": len(ALL_STANDARD_PATTERNS),
        "strict": len(ALL_STRICT_PATTERNS),
        "child_safe": len(ALL_CHILD_SAFE_PATTERNS),
    }


# Re-export the leading underscore symbol that ``_p`` was used to build,
# for any test that imports it directly.
_ = _p
