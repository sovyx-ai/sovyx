"""Sovyx ReflectPhase — encode episode + extract concepts + Hebbian learning.

Uses LLM-based concept extraction for rich, accurate knowledge capture.
Falls back to regex-based extraction if the LLM is unavailable.

This subpackage was split out of the previous monolithic ``reflect.py``
god file. The public API is unchanged: every existing
``from sovyx.cognitive.reflect import X`` import continues to work via
the re-exports below.

Module layout:
    _models.py      — ExtractedConcept dataclass
    _prompts.py     — LLM prompts (extraction + relation classification)
    _categories.py  — Category map / importance / source confidence /
                      explicit-importance regex constants
    _fallback.py    — Sentiment heuristics + regex fallback patterns
    _scoring.py     — resolve_category / get_importance /
                      get_source_confidence / detect_explicit_importance /
                      compute_episode_importance / clamp_sentiment
    phase.py        — ReflectPhase class (orchestrator)
"""

from __future__ import annotations

from sovyx.cognitive.reflect._categories import _CATEGORY_MAP
from sovyx.cognitive.reflect._fallback import _estimate_sentiment
from sovyx.cognitive.reflect._models import ExtractedConcept
from sovyx.cognitive.reflect._prompts import _VALID_RELATIONS
from sovyx.cognitive.reflect._scoring import (
    clamp_sentiment,
    compute_episode_importance,
    detect_explicit_importance,
    get_importance,
    get_source_confidence,
    resolve_category,
)
from sovyx.cognitive.reflect.phase import ReflectPhase

# Some test files import the leading-underscore helpers directly. Keep them
# in __all__ so the public re-exports from this package remain stable for
# the existing test surface.
__all__ = [
    "ExtractedConcept",
    "ReflectPhase",
    "_CATEGORY_MAP",
    "_VALID_RELATIONS",
    "_estimate_sentiment",
    "clamp_sentiment",
    "compute_episode_importance",
    "detect_explicit_importance",
    "get_importance",
    "get_source_confidence",
    "resolve_category",
]
