"""Auto-extracted from cognitive/reflect.py — see __init__.py for the public re-exports."""

from __future__ import annotations

from dataclasses import dataclass

# ── Extracted concept data ─────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ExtractedConcept:
    """A concept extracted from user input (LLM or regex).

    Extended in TASK-02 with importance, confidence, explicit, and
    source_quality fields for multi-signal scoring. Extended for
    ADR-001 with PAD 3D emotional fields (``sentiment`` is the
    pleasure axis kept for compat; ``arousal`` and ``dominance`` are
    the new additions, default 0.0 so legacy extractors that only
    yield sentiment land as-if with no arousal/dominance signal).
    """

    name: str
    content: str
    category: str
    sentiment: float = 0.0  # pleasure axis, -1.0 (negative) to 1.0 (positive)
    importance: float = 0.5  # LLM-assessed importance (0.0-1.0)
    confidence: float = 0.7  # LLM-assessed confidence (0.0-1.0)
    explicit: bool = False  # User explicitly asked to remember
    source_quality: str = "explicit"  # "explicit" or "inferred"
    arousal: float = 0.0  # activation axis, -1.0 (calm) to 1.0 (intense) — ADR-001
    dominance: float = 0.0  # agency axis, -1.0 (hedging) to 1.0 (assertive) — ADR-001
