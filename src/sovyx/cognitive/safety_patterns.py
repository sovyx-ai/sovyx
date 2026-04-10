"""Sovyx safety patterns — tiered content filtering with compiled regex.

Three tiers:
- **none**: zero filtering, empty pattern set.
- **standard**: blocks direct harm — weapons, explosives, self-harm,
  hacking with destructive intent. ~20 regex patterns.
- **strict**: everything in standard + adult content, substances,
  gambling, hate speech, controversial manipulation. ~50 patterns.
- **child_safe**: superset of strict, covered in TASK-323.

Each pattern has metadata (category, tier) for audit trail (TASK-324).
Patterns are compiled once at import time for performance.

Design principles:
- Regex over substring: catches inflections, variations, typos.
- Category-tagged: enables per-category metrics and audit.
- False-positive-aware: patterns are specific enough to avoid blocking
  legitimate educational/news content. "bomb" alone doesn't trigger;
  "how to build a bomb" does.
- Performance: compiled frozenset, short-circuit on first match.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum, unique
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sovyx.mind.config import SafetyConfig


@unique
class PatternCategory(Enum):
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


@unique
class FilterTier(Enum):
    """Content filter tiers — each tier includes all lower tiers."""

    STANDARD = "standard"
    STRICT = "strict"


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


# ── STANDARD tier patterns ─────────────────────────────────────────────
# Direct harm: weapons, explosives, killing, self-harm, destructive hacking.
# These block clear intent to cause harm, NOT educational/news content.

_STANDARD_PATTERNS: tuple[SafetyPattern, ...] = (
    # ── Violence / Weapons ──
    _p(
        r"\bhow\s+to\s+(?:make|build|construct|create|assemble)\s+(?:a\s+)?(?:bomb|explosive|ied|grenade|molotov)",
        PatternCategory.WEAPONS,
        FilterTier.STANDARD,
        "Instructions for making explosives",
    ),
    _p(
        r"\bhow\s+to\s+(?:make|build|create|3d\s*print)\s+(?:a\s+)?(?:gun|firearm|weapon|rifle|pistol)",
        PatternCategory.WEAPONS,
        FilterTier.STANDARD,
        "Instructions for making firearms",
    ),
    _p(
        r"\bhow\s+to\s+(?:make|synthesize|cook|produce)\s+(?:poison|toxin|nerve\s*agent|ricin|sarin|anthrax)",
        PatternCategory.WEAPONS,
        FilterTier.STANDARD,
        "Instructions for making poisons/chemical weapons",
    ),
    _p(
        r"\bhow\s+to\s+(?:kill|murder|assassinate|strangle|suffocate|poison)\s+(?:a\s+)?(?:person|someone|people|human)",
        PatternCategory.VIOLENCE,
        FilterTier.STANDARD,
        "Instructions for killing people",
    ),
    _p(
        r"\b(?:best|easiest|most\s+effective)\s+(?:way|method|technique)\s+to\s+(?:kill|murder|harm)",
        PatternCategory.VIOLENCE,
        FilterTier.STANDARD,
        "Seeking methods to kill or harm",
    ),
    _p(
        r"\bhow\s+to\s+(?:torture|mutilate|dismember)",
        PatternCategory.VIOLENCE,
        FilterTier.STANDARD,
        "Instructions for torture or mutilation",
    ),
    _p(
        r"\bhow\s+to\s+(?:get\s+away\s+with|hide|cover\s+up)\s+(?:murder|killing|a\s+crime|homicide)",
        PatternCategory.VIOLENCE,
        FilterTier.STANDARD,
        "Evading justice for violent crimes",
    ),
    # ── Self-harm ──
    _p(
        r"\bhow\s+to\s+(?:commit\s+)?(?:suicide|kill\s+(?:my|your)self|end\s+(?:my|your)\s+life)",
        PatternCategory.SELF_HARM,
        FilterTier.STANDARD,
        "Suicide instructions",
    ),
    _p(
        r"\b(?:best|painless|quickest|easiest)\s+(?:way|method)\s+to\s+(?:die|end\s+it|kill\s+(?:my|your)self)",
        PatternCategory.SELF_HARM,
        FilterTier.STANDARD,
        "Seeking methods for self-harm",
    ),
    _p(
        r"\bself[- ]?harm\s+(?:instructions|methods|techniques|guide|tutorial)",
        PatternCategory.SELF_HARM,
        FilterTier.STANDARD,
        "Self-harm guides",
    ),
    _p(
        r"\b(?:how\s+to\s+)?(?:cut|slash|burn)\s+(?:my|your)self\b",
        PatternCategory.SELF_HARM,
        FilterTier.STANDARD,
        "Self-injury instructions",
    ),
    # ── Hacking (destructive intent) ──
    _p(
        r"\bhow\s+to\s+(?:hack|breach|break\s+into|compromise|exploit)\s+(?:a\s+)?(?:bank|government|hospital|school|company|server|database|network)",
        PatternCategory.HACKING,
        FilterTier.STANDARD,
        "Hacking critical infrastructure/institutions",
    ),
    _p(
        r"\bhow\s+to\s+(?:create|build|write|deploy|spread)\s+(?:a\s+)?(?:virus|malware|ransomware|trojan|worm|keylogger|rootkit|botnet|spyware)",
        PatternCategory.HACKING,
        FilterTier.STANDARD,
        "Creating malicious software",
    ),
    _p(
        r"\bhow\s+to\s+(?:ddos|dos|flood|crash|take\s+down)\s+(?:a\s+)?(?:website|server|service|network)",
        PatternCategory.HACKING,
        FilterTier.STANDARD,
        "DDoS/service disruption instructions",
    ),
    _p(
        r"\bhow\s+to\s+(?:steal|phish|spoof|clone)\s+(?:credentials?|passwords?|identit(?:y|ies)|credit\s+cards?|bank\s+accounts?)",
        PatternCategory.HACKING,
        FilterTier.STANDARD,
        "Identity theft and credential stealing",
    ),
    _p(
        r"\bhow\s+to\s+(?:crack|brute\s*force|bypass)\s+(?:a\s+)?(?:password|encryption|2fa|authentication|security)",
        PatternCategory.HACKING,
        FilterTier.STANDARD,
        "Bypassing security mechanisms",
    ),
    # ── Illegal activities ──
    _p(
        r"\bhow\s+to\s+(?:kidnap|abduct|traffic|enslave)\b",
        PatternCategory.ILLEGAL,
        FilterTier.STANDARD,
        "Human trafficking/kidnapping instructions",
    ),
    _p(
        r"\bhow\s+to\s+(?:make|produce|synthesize|cook)\s+(?:meth|methamphetamine|cocaine|heroin|fentanyl|mdma|ecstasy|lsd|crack)",
        PatternCategory.ILLEGAL,
        FilterTier.STANDARD,
        "Drug manufacturing instructions",
    ),
    _p(
        r"\b(?:child|minor|underage)\s+(?:porn|pornograph|sexual|exploit|abuse|nude)",
        PatternCategory.SEXUAL,
        FilterTier.STANDARD,
        "Child sexual exploitation",
    ),
    _p(
        r"\bhow\s+to\s+(?:groom|lure|seduce)\s+(?:a\s+)?(?:child|minor|kid|teen|underage)",
        PatternCategory.SEXUAL,
        FilterTier.STANDARD,
        "Child grooming",
    ),
)

# ── STRICT tier patterns ───────────────────────────────────────────────
# Extended: adult content, substances, gambling, hate speech, manipulation.
# More sensitive — blocks topics that are legal but inappropriate for
# professional/controlled environments.

_STRICT_PATTERNS: tuple[SafetyPattern, ...] = (
    # ── Substances ──
    _p(
        r"\bhow\s+to\s+(?:buy|get|obtain|find|score|order)\s+(?:drugs?|weed|marijuana|cocaine|heroin|pills|xanax|oxy|adderall|steroids)",
        PatternCategory.SUBSTANCE,
        FilterTier.STRICT,
        "Obtaining controlled substances",
    ),
    _p(
        r"\bhow\s+to\s+(?:grow|cultivate)\s+(?:weed|marijuana|cannabis|mushrooms|psilocybin|poppies|coca)",
        PatternCategory.SUBSTANCE,
        FilterTier.STRICT,
        "Growing controlled substances",
    ),
    _p(
        r"\bhow\s+to\s+(?:use|smoke|inject|snort|take)\s+(?:drugs?|meth|cocaine|heroin|crack|lsd|dmt|ketamine)",
        PatternCategory.SUBSTANCE,
        FilterTier.STRICT,
        "Drug usage instructions",
    ),
    _p(
        r"\b(?:best|strongest|cheapest)\s+(?:drugs?|high|trip|buzz)\b",
        PatternCategory.SUBSTANCE,
        FilterTier.STRICT,
        "Seeking drug recommendations",
    ),
    # ── Sexual content ──
    _p(
        r"\b(?:explicit|graphic)\s+(?:sex|sexual|porn|erotic)\b",
        PatternCategory.SEXUAL,
        FilterTier.STRICT,
        "Explicit sexual content request",
    ),
    _p(
        r"\bwrite\s+(?:(?:me|you|us)\s+)?(?:an?\s+)?(?:erotic|sexual|porn|nsfw|smut)\s+(?:story|fiction|scene|fantasy)",
        PatternCategory.SEXUAL,
        FilterTier.STRICT,
        "Erotic fiction generation",
    ),
    _p(
        r"\b(?:how\s+to\s+)?(?:sext|send\s+nudes|revenge\s+porn|deepfake\s+porn)",
        PatternCategory.SEXUAL,
        FilterTier.STRICT,
        "Non-consensual sexual content",
    ),
    # ── Gambling ──
    _p(
        r"\bhow\s+to\s+(?:cheat|rig|fix|hack)\s+(?:at\s+)?(?:poker|blackjack|roulette|slots|casino|gambling|betting|lottery)",
        PatternCategory.GAMBLING,
        FilterTier.STRICT,
        "Cheating at gambling",
    ),
    _p(
        r"\b(?:guaranteed|sure[- ]?fire|foolproof)\s+(?:way|method|system)"
        r"\s+to\s+(?:win|beat)\s+(?:at\s+)?(?:gambling|casino|poker|slots|betting)",
        PatternCategory.GAMBLING,
        FilterTier.STRICT,
        "Fraudulent gambling systems",
    ),
    _p(
        r"\b(?:best|top)\s+(?:illegal|unlicensed|offshore)\s+(?:gambling|betting|casino)\s+(?:sites?|apps?|platforms?)",
        PatternCategory.GAMBLING,
        FilterTier.STRICT,
        "Illegal gambling platforms",
    ),
    # ── Hate speech ──
    _p(
        r"\b(?:why\s+(?:are|is)\s+)?(?:race|ethnic\s+group|religion|gender)\s+(?:is\s+)?(?:inferior|superior|evil|subhuman|worthless)",
        PatternCategory.HATE_SPEECH,
        FilterTier.STRICT,
        "Racial/ethnic/religious supremacy",
    ),
    _p(
        r"\bhow\s+to\s+(?:recruit|radicalize|convert)\s+(?:people|someone)"
        r"\s+(?:to|for)\s+(?:extremism|terrorism|white\s+supremac|neo[- ]?nazi|jihad)",
        PatternCategory.HATE_SPEECH,
        FilterTier.STRICT,
        "Extremist recruitment",
    ),
    _p(
        r"\b(?:holocaust|genocide|ethnic\s+cleansing)\s+(?:was\s+)?(?:fake|hoax|didn'?t\s+happen|never\s+happened|a\s+lie)",
        PatternCategory.HATE_SPEECH,
        FilterTier.STRICT,
        "Genocide denial",
    ),
    # ── Manipulation / Fraud ──
    _p(
        r"\bhow\s+to\s+(?:scam|fraud|con|deceive|swindle|catfish|impersonate)\s+(?:people|someone|victims?|customers?)",
        PatternCategory.MANIPULATION,
        FilterTier.STRICT,
        "Fraud and scam instructions",
    ),
    _p(
        r"\bhow\s+to\s+(?:launder|wash)\s+(?:money|cash|funds)",
        PatternCategory.MANIPULATION,
        FilterTier.STRICT,
        "Money laundering instructions",
    ),
    _p(
        r"\bhow\s+to\s+(?:forge|fake|counterfeit)\s+(?:documents?|ids?|passports?|diplomas?|certificates?|currency|money|bills?)",
        PatternCategory.MANIPULATION,
        FilterTier.STRICT,
        "Document forgery/counterfeiting",
    ),
    _p(
        r"\bhow\s+to\s+(?:stalk|surveil|track|spy\s+on)\s+(?:someone|a\s+person|my\s+(?:ex|partner|spouse|wife|husband|girlfriend|boyfriend))",
        PatternCategory.MANIPULATION,
        FilterTier.STRICT,
        "Stalking and surveillance of individuals",
    ),
    _p(
        r"\bhow\s+to\s+(?:blackmail|extort|threaten|intimidate)\s+(?:someone|people|a\s+person)",
        PatternCategory.MANIPULATION,
        FilterTier.STRICT,
        "Blackmail and extortion",
    ),
    _p(
        r"\bhow\s+to\s+(?:manipulate|gaslight|brainwash|coerce)\s+(?:someone|people|a\s+person|my\s+(?:partner|spouse|boss|coworker))",
        PatternCategory.MANIPULATION,
        FilterTier.STRICT,
        "Psychological manipulation",
    ),
    # ── Illegal weapons/explosives access ──
    _p(
        r"\b(?:where|how)\s+(?:to|can\s+i)\s+(?:buy|get|obtain|order)\s+(?:a\s+)?(?:gun|firearm|weapon|rifle|pistol|ammo|ammunition)\s+(?:illegally|without\s+(?:a\s+)?license|on\s+(?:the\s+)?(?:dark\s*web|black\s*market))",
        PatternCategory.WEAPONS,
        FilterTier.STRICT,
        "Illegal weapons procurement",
    ),
    _p(
        r"\b(?:where|how)\s+(?:to|can\s+i)\s+(?:buy|get|obtain)\s+(?:explosives?|detonators?|c4|dynamite|blasting\s+caps?)",
        PatternCategory.WEAPONS,
        FilterTier.STRICT,
        "Obtaining explosives",
    ),
)


# ── Compiled pattern sets ──────────────────────────────────────────────

ALL_STANDARD_PATTERNS: tuple[SafetyPattern, ...] = _STANDARD_PATTERNS
ALL_STRICT_PATTERNS: tuple[SafetyPattern, ...] = _STANDARD_PATTERNS + _STRICT_PATTERNS

# Pre-built frozensets for fast tier lookup
_STANDARD_SET: frozenset[SafetyPattern] = frozenset(ALL_STANDARD_PATTERNS)
_STRICT_SET: frozenset[SafetyPattern] = frozenset(ALL_STRICT_PATTERNS)


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


# Singleton "no match" result
NO_MATCH = FilterMatch(matched=False)


def resolve_patterns(safety: SafetyConfig) -> tuple[SafetyPattern, ...]:
    """Resolve the active pattern set from current safety config.

    Args:
        safety: Current safety configuration.

    Returns:
        Tuple of active SafetyPattern instances.
        Empty tuple when filter is ``"none"`` (and child_safe is off).
    """
    if safety.child_safe_mode:
        # Child-safe uses strict patterns (TASK-323 will extend further)
        return ALL_STRICT_PATTERNS
    if safety.content_filter == "strict":
        return ALL_STRICT_PATTERNS
    if safety.content_filter == "standard":
        return ALL_STANDARD_PATTERNS
    # content_filter == "none"
    return ()


def check_content(text: str, safety: SafetyConfig) -> FilterMatch:
    """Check text against the active safety patterns.

    Short-circuits on first match for performance.
    Returns ``NO_MATCH`` when filter is ``"none"`` (zero overhead).

    Args:
        text: Text to check (user message or LLM response).
        safety: Current safety configuration.

    Returns:
        FilterMatch with match details (or NO_MATCH).
    """
    patterns = resolve_patterns(safety)
    if not patterns:
        return NO_MATCH

    lower = text.lower()
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
        {"standard": N, "strict": M} where strict includes standard.
    """
    return {
        "standard": len(ALL_STANDARD_PATTERNS),
        "strict": len(ALL_STRICT_PATTERNS),
    }
