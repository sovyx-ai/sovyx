"""Auto-extracted safety patterns. See safety_patterns.py for the public API."""

from __future__ import annotations

from sovyx.cognitive.safety._pattern_types import (
    FilterTier,
    PatternCategory,
    SafetyPattern,
    _p,
)

# ── CHILD_SAFE tier patterns ──────────────────────────────────────────
# Superset of strict. Blocks content that is legal/educational for adults
# but inappropriate for children under 10. Zero tolerance.

_CHILD_SAFE_PATTERNS: tuple[SafetyPattern, ...] = (
    # ── Violence (even contextual/historical) ──
    _p(
        r"\b(?:soldiers?|troops?|armies?)\s+(?:killed|murdered|slaughtered|massacred|executed)",
        PatternCategory.VIOLENCE,
        FilterTier.CHILD_SAFE,
        "Explicit historical violence",
    ),
    _p(
        r"\b(?:graphic|brutal|gory|bloody|gruesome)\s+(?:details?|descriptions?|scenes?|violence|death|murder)",
        PatternCategory.VIOLENCE,
        FilterTier.CHILD_SAFE,
        "Graphic violence descriptions",
    ),
    _p(
        r"\b(?:war\s+)?(?:crimes?|atrocit(?:y|ies)|genocide|massacre|holocaust)\s+(?:details|descriptions?|victims?|deaths?)",
        PatternCategory.VIOLENCE,
        FilterTier.CHILD_SAFE,
        "War crime details",
    ),
    _p(
        r"\b(?:serial\s+killer|mass\s+(?:murder|shooting)|school\s+shooting|terrorist\s+attack)\b",
        PatternCategory.VIOLENCE,
        FilterTier.CHILD_SAFE,
        "Mass violence references",
    ),
    _p(
        r"\b(?:execution|beheading|hanging|lethal\s+injection|electric\s+chair|firing\s+squad)\b",
        PatternCategory.VIOLENCE,
        FilterTier.CHILD_SAFE,
        "Execution methods",
    ),
    # ── Substance references (even educational) ──
    _p(
        r"\b(?:what\s+(?:are|is)\s+)?(?:drugs?|cocaine|heroin|meth|marijuana|weed|lsd|ecstasy|mdma|ketamine)\b",
        PatternCategory.SUBSTANCE,
        FilterTier.CHILD_SAFE,
        "Drug references (any context)",
    ),
    _p(
        r"\b(?:alcohol|beer|wine|vodka|whiskey|cocktail|drunk|intoxicat|hangover)\b",
        PatternCategory.SUBSTANCE,
        FilterTier.CHILD_SAFE,
        "Alcohol references",
    ),
    _p(
        r"\b(?:smoking|cigarettes?|tobacco|vaping|nicotine|e-?cig)\b",
        PatternCategory.SUBSTANCE,
        FilterTier.CHILD_SAFE,
        "Tobacco/smoking references",
    ),
    # ── Sexual references (any) ──
    _p(
        r"\b(?:sex|sexual|intercourse|orgasm|genitals?|penis|vagina|breasts?|naked|nude|nudity)\b",
        PatternCategory.SEXUAL,
        FilterTier.CHILD_SAFE,
        "Sexual/anatomical references",
    ),
    _p(
        r"\b(?:pregnancy|pregnant|conception|birth\s+control|condom|contracepti)",
        PatternCategory.SEXUAL,
        FilterTier.CHILD_SAFE,
        "Reproductive health references",
    ),
    _p(
        r"\b(?:dating|hookup|one[- ]?night\s+stand|affair|cheating\s+on)\b",
        PatternCategory.SEXUAL,
        FilterTier.CHILD_SAFE,
        "Adult relationship references",
    ),
    # ── Gambling (any) ──
    _p(
        r"\b(?:gambling|casino|poker|blackjack|roulette|slots?|betting|wager|bookmaker)\b",
        PatternCategory.GAMBLING,
        FilterTier.CHILD_SAFE,
        "Gambling references (any context)",
    ),
    # ── Dark humor / inappropriate tone ──
    _p(
        r"\b(?:dark\s+humor|black\s+comedy|gallows\s+humor|dead\s+baby\s+joke)",
        PatternCategory.VIOLENCE,
        FilterTier.CHILD_SAFE,
        "Dark humor",
    ),
    _p(
        r"\b(?:damn|hell|crap|ass|bastard|bitch|shit|fuck|wtf|stfu|lmao)\b",
        PatternCategory.HATE_SPEECH,
        FilterTier.CHILD_SAFE,
        "Profanity",
    ),
    # ── Horror / fear ──
    _p(
        r"\b(?:horror|scary|terrifying|nightmare|demon|possessed|haunted|creepy\s+pasta)\b",
        PatternCategory.VIOLENCE,
        FilterTier.CHILD_SAFE,
        "Horror content",
    ),
    # ── Death (explicit) ──
    _p(
        r"\b(?:died|death|dead|corpse|morgue|funeral|cremation|burial|coffin|autopsy)\b",
        PatternCategory.VIOLENCE,
        FilterTier.CHILD_SAFE,
        "Death references (explicit)",
    ),
)
