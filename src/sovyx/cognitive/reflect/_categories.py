"""Auto-extracted from cognitive/reflect.py — see __init__.py for the public re-exports."""

from __future__ import annotations

import re

# ── Category mapping ───────────────────────────────────────────────────
# Maps LLM output strings → ConceptCategory enum values.
# Every ConceptCategory MUST have ≥1 key mapping to it.

_CATEGORY_MAP: dict[str, str] = {
    # Direct mappings (1:1 with ConceptCategory enum)
    "entity": "entity",
    "fact": "fact",
    "preference": "preference",
    "skill": "skill",
    "belief": "belief",
    "event": "event",
    "relationship": "relationship",
    # Aliases (LLM may use these synonyms)
    "opinion": "belief",  # opinion IS a belief
    "project": "entity",  # a project is a named entity
    "person": "entity",  # person is an entity
    "tool": "skill",  # knowing a tool is a skill
    "technology": "skill",  # knowing a technology is a skill
    "milestone": "event",  # milestone is a time-bound event
    "connection": "relationship",  # synonym
}

# ── Importance by category ─────────────────────────────────────────────
# Initial importance assigned at concept creation based on category.
# Higher = more likely to survive Ebbinghaus decay.
# These values are used as the category baseline signal in the
# multi-signal importance formula.

_IMPORTANCE: dict[str, float] = {
    "entity": 0.80,  # People, places, orgs — identity-critical
    "relationship": 0.80,  # Social connections — rare, meaningful
    "preference": 0.70,  # Personal taste — defines personality
    "skill": 0.70,  # Capabilities — shapes responses
    "event": 0.70,  # Time-bound — contextual anchors
    "fact": 0.60,  # Verifiable info — common but useful
    "belief": 0.60,  # Opinions — shapes worldview
}

# Default importance for unknown categories
_DEFAULT_IMPORTANCE = 0.5

# ── Source confidence mapping ──────────────────────────────────────────
# Confidence assigned based on extraction quality.
# Key = source type, Value = (floor, ceiling). Midpoint is used.
# Higher confidence = more epistemic certainty about the information.

_SOURCE_CONFIDENCE: dict[str, tuple[float, float]] = {
    "llm_explicit": (0.75, 0.95),  # LLM extracted from clear user statement
    "llm_inferred": (0.45, 0.70),  # LLM inferred (not directly stated)
    "regex_fallback": (0.30, 0.55),  # Regex pattern match (less reliable)
    "system": (0.90, 1.00),  # System-generated (identity, etc.)
    "corroboration": (0.80, 1.00),  # Multiple sources agree
}

# Default confidence for unknown source types
_DEFAULT_SOURCE_CONFIDENCE = (0.40, 0.60)

# ── Explicit importance signal detection ───────────────────────────────
# Regex patterns to detect when user explicitly asks to remember info.
# Supports English and Portuguese phrases. Message-level detection
# applies to ALL concepts extracted from that message.

_EXPLICIT_PATTERNS: list[re.Pattern[str]] = [
    # English
    re.compile(r"\b(?:remember\s+this|don'?t\s+forget|keep\s+in\s+mind)\b", re.I),
    re.compile(r"\b(?:this\s+is\s+(?:very\s+)?important|critical|crucial)\b", re.I),
    re.compile(r"\b(?:note\s+(?:this|that)|make\s+(?:a\s+)?note)\b", re.I),
    re.compile(r"\b(?:never\s+forget|always\s+remember)\b", re.I),
    # Portuguese
    re.compile(r"\b(?:lembra\s+(?:disso|isso)|não\s+esquece)\b", re.I),
    re.compile(r"\b(?:anota\s+(?:isso|aí)|guarda\s+(?:isso|essa\s+info))\b", re.I),
    re.compile(r"\b(?:(?:isso\s+é\s+)?importante|presta\s+atenção)\b", re.I),
    re.compile(r"\b(?:memoriza|nunca\s+esquece|grava\s+(?:isso|aí))\b", re.I),
]
