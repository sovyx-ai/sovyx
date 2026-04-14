"""Auto-extracted from cognitive/reflect.py — see __init__.py for the public re-exports."""

from __future__ import annotations

from dataclasses import dataclass

# ── Extracted concept data ─────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ExtractedConcept:
    """A concept extracted from user input (LLM or regex).

    Extended in TASK-02 with importance, confidence, explicit, and
    source_quality fields for multi-signal scoring.
    """

    name: str
    content: str
    category: str
    sentiment: float = 0.0  # -1.0 (negative) to 1.0 (positive)
    importance: float = 0.5  # LLM-assessed importance (0.0-1.0)
    confidence: float = 0.7  # LLM-assessed confidence (0.0-1.0)
    explicit: bool = False  # User explicitly asked to remember
    source_quality: str = "explicit"  # "explicit" or "inferred"
