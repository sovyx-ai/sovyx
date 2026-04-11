"""Sovyx Shadow Mode — dry-run safety patterns that log but never block.

Shadow mode allows operators to test new safety patterns in production
without affecting real traffic. Patterns are evaluated against every
perception and output, and matches are logged to the audit trail with
``FilterAction.SHADOW_LOGGED`` — but content is **never** blocked,
redacted, or replaced.

Use cases:
    - Validate new regex patterns before promoting to standard/strict.
    - Measure false-positive rates of candidate patterns.
    - A/B test pattern variations without user impact.
    - Gradual rollout of stricter safety policies.

Integration points:
    - ``AttendPhase.process()`` calls ``evaluate_shadow()`` after real
      safety checks pass (shadow runs even if content was accepted).
    - ``OutputGuard.check_async()`` calls ``evaluate_shadow()`` after
      real output filtering.
    - Dashboard displays shadow matches separately from real blocks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sovyx.cognitive.safety_audit import (
    FilterAction,
    FilterDirection,
    get_audit_trail,
)
from sovyx.cognitive.safety_patterns import FilterMatch, PatternCategory
from sovyx.observability.logging import get_logger
from sovyx.observability.metrics import get_metrics

if TYPE_CHECKING:
    from sovyx.mind.config import SafetyConfig, ShadowPattern

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class CompiledShadowPattern:
    """A compiled shadow pattern ready for matching.

    Attributes:
        name: Human-readable name (from config).
        regex: Compiled case-insensitive regex.
        category: Safety category string.
        tier: Intended target tier.
        description: Why this pattern exists.
    """

    name: str
    regex: re.Pattern[str]
    category: str
    tier: str
    description: str


@dataclass(frozen=True, slots=True)
class ShadowMatch:
    """Result of a shadow pattern evaluation.

    Attributes:
        matched: Whether any shadow pattern matched.
        pattern_name: Name of the first matching pattern (None if no match).
        category: Category of the matched pattern.
        tier: Intended tier of the matched pattern.
        description: Description of the matched pattern.
        all_matches: All matching pattern names (for multi-match logging).
    """

    matched: bool
    pattern_name: str | None = None
    category: str | None = None
    tier: str | None = None
    description: str | None = None
    all_matches: tuple[str, ...] = ()


# Singleton "no match" result
NO_SHADOW_MATCH = ShadowMatch(matched=False)


def compile_shadow_patterns(
    patterns: list[ShadowPattern],
) -> list[CompiledShadowPattern]:
    """Compile shadow patterns from config into regex-ready objects.

    Invalid regex patterns are logged and skipped (never crash the pipeline).

    Args:
        patterns: Raw ShadowPattern list from SafetyConfig.

    Returns:
        List of compiled patterns (may be shorter than input if some fail).
    """
    compiled: list[CompiledShadowPattern] = []
    for p in patterns:
        try:
            regex = re.compile(p.pattern, re.IGNORECASE)
            compiled.append(
                CompiledShadowPattern(
                    name=p.name,
                    regex=regex,
                    category=p.category,
                    tier=p.tier,
                    description=p.description,
                )
            )
        except re.error as e:
            logger.warning(
                "shadow_pattern_compile_error",
                pattern_name=p.name,
                pattern=p.pattern,
                error=str(e),
            )
    return compiled


# ── Module-level cache ─────────────────────────────────────────────────
# Compiled patterns are cached and invalidated when config changes.

_cached_patterns: list[CompiledShadowPattern] | None = None
_cached_config_hash: int | None = None


def _get_compiled(safety: SafetyConfig) -> list[CompiledShadowPattern]:
    """Get compiled shadow patterns, using cache when config unchanged."""
    global _cached_patterns, _cached_config_hash  # noqa: PLW0603

    # Hash based on pattern list content
    config_hash = hash(
        tuple((p.name, p.pattern, p.category, p.tier) for p in safety.shadow_patterns)
    )

    if _cached_patterns is not None and _cached_config_hash == config_hash:
        return _cached_patterns

    _cached_patterns = compile_shadow_patterns(safety.shadow_patterns)
    _cached_config_hash = config_hash
    return _cached_patterns


def invalidate_cache() -> None:
    """Force recompilation on next call (for testing / hot-reload)."""
    global _cached_patterns, _cached_config_hash  # noqa: PLW0603
    _cached_patterns = None
    _cached_config_hash = None


def evaluate_shadow(
    text: str,
    safety: SafetyConfig,
    direction: FilterDirection,
) -> ShadowMatch:
    """Evaluate text against shadow patterns (log-only, never blocks).

    This function is safe to call on every message — when shadow_mode
    is disabled or no shadow patterns exist, it returns immediately
    with zero overhead.

    Args:
        text: Text to evaluate (user input or LLM output).
        safety: Current safety configuration.
        direction: Whether this is INPUT or OUTPUT evaluation.

    Returns:
        ShadowMatch with match details (or NO_SHADOW_MATCH).
    """
    if not safety.shadow_mode or not safety.shadow_patterns:
        return NO_SHADOW_MATCH

    compiled = _get_compiled(safety)
    if not compiled:
        return NO_SHADOW_MATCH

    # Normalize text for matching
    from sovyx.cognitive.text_normalizer import normalize_text

    normalized = normalize_text(text)
    lower = normalized.lower()

    # Evaluate ALL patterns (not short-circuit — we want full match data)
    matches: list[CompiledShadowPattern] = []
    for p in compiled:
        if p.regex.search(lower):
            matches.append(p)

    if not matches:
        return NO_SHADOW_MATCH

    first = matches[0]

    # Log to audit trail with SHADOW_LOGGED action
    _log_shadow_matches(matches, direction)

    return ShadowMatch(
        matched=True,
        pattern_name=first.name,
        category=first.category,
        tier=first.tier,
        description=first.description,
        all_matches=tuple(m.name for m in matches),
    )


def _log_shadow_matches(
    matches: list[CompiledShadowPattern],
    direction: FilterDirection,
) -> None:
    """Log shadow matches to audit trail and metrics."""
    audit = get_audit_trail()
    m = get_metrics()

    for match in matches:
        # Create a FilterMatch-compatible object for the audit trail
        # Shadow patterns don't have a SafetyPattern (they're separate),
        # so we create a minimal FilterMatch.
        try:
            category = PatternCategory(match.category)
        except (ValueError, KeyError):
            category = None

        filter_match = FilterMatch(
            matched=True,
            pattern=None,
            category=category,
            tier=None,
        )

        audit.record(
            direction=direction,
            action=FilterAction.SHADOW_LOGGED,
            match=filter_match,
        )

        # Structured log with shadow-specific fields
        logger.info(
            "shadow_mode_match",
            direction=direction.value,
            pattern_name=match.name,
            category=match.category,
            tier=match.tier,
            description=match.description,
        )

        # Metrics: separate counter for shadow matches
        m.safety_blocks.add(
            1,
            {
                "direction": direction.value,
                "tier": f"shadow_{match.tier}",
                "category": match.category,
            },
        )


def get_shadow_stats(safety: SafetyConfig) -> dict[str, object]:
    """Get shadow mode status and pattern count for dashboard.

    Args:
        safety: Current safety configuration.

    Returns:
        Dict with enabled status, pattern count, and compiled count.
    """
    compiled = _get_compiled(safety) if safety.shadow_mode else []
    return {
        "enabled": safety.shadow_mode,
        "total_patterns": len(safety.shadow_patterns),
        "compiled_patterns": len(compiled),
        "compile_errors": len(safety.shadow_patterns) - len(compiled),
    }
