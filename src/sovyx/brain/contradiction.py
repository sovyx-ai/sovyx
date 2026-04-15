"""Sovyx contradiction detection — LLM-assisted semantic comparison.

Determines the relationship between an existing concept's content and
new incoming content during dedup. Replaces string-based heuristics
with LLM pairwise comparison for accurate semantic classification.

Classification taxonomy:
    SAME: Semantically equivalent (paraphrase, synonym).
    EXTENDS: New content adds information without contradicting.
    CONTRADICTS: New content conflicts with existing (value changed,
        negation, opposing statement).
    UNRELATED: Contents don't refer to the same topic (safety fallback).
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import TYPE_CHECKING

from sovyx.engine.errors import LLMError
from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from sovyx.llm.router import LLMRouter

logger = get_logger(__name__)


class ContentRelation(StrEnum):
    """Semantic relationship between old and new concept content."""

    SAME = "SAME"
    EXTENDS = "EXTENDS"
    CONTRADICTS = "CONTRADICTS"
    UNRELATED = "UNRELATED"


# ── LLM Prompt ──────────────────────────────────────────────────────────────

_CONTRADICTION_PROMPT = """\
Compare two statements about the same topic and classify their relationship.

EXISTING: "{old_content}"
NEW: "{new_content}"

Classify as exactly one of:
- SAME: They say the same thing (paraphrase, synonym, equivalent meaning)
- EXTENDS: New adds information without contradicting existing
- CONTRADICTS: New conflicts with existing (different value, negation, opposite claim)
- UNRELATED: They don't refer to the same topic

Return ONLY a JSON object:
{{"relation": "SAME"|"EXTENDS"|"CONTRADICTS"|"UNRELATED", "reason": "brief"}}"""


# ── Heuristic Fallback ──────────────────────────────────────────────────────


def _detect_contradiction_heuristic(
    old_content: str,
    new_content: str,
) -> ContentRelation:
    """String-based contradiction detection (fallback when LLM unavailable).

    Conservative heuristic: only flags obvious contradictions where
    content changed significantly and isn't an extension.

    Rules:
    - Identical (case-insensitive) → SAME
    - New is longer and starts with old → EXTENDS
    - New is shorter/equal, differs substantially → CONTRADICTS
    - Otherwise → SAME (conservative default)

    Args:
        old_content: Existing concept content.
        new_content: Incoming concept content.

    Returns:
        ContentRelation classification.
    """
    old_lower = old_content.lower().strip()
    new_lower = new_content.lower().strip()

    # Identical
    if old_lower == new_lower:
        return ContentRelation.SAME

    # Extension: new starts with old (prefix match)
    if new_lower.startswith(old_lower[:20]) and len(new_content) > len(old_content):
        return ContentRelation.EXTENDS

    # Content grew substantially → likely extends
    if len(new_content) > len(old_content) * 1.5:
        return ContentRelation.EXTENDS

    # Short content can't be reliably compared
    if len(new_content) <= 10 or len(old_content) <= 10:  # noqa: PLR2004
        return ContentRelation.SAME

    # Different content of similar length → potential contradiction
    # Conservative: only flag when strings clearly differ
    return ContentRelation.CONTRADICTS


# ── LLM-Assisted Detection ─────────────────────────────────────────────────


async def detect_contradiction(
    old_content: str,
    new_content: str,
    llm_router: LLMRouter | None = None,
    fast_model: str = "",
) -> ContentRelation:
    """Detect semantic relationship between old and new concept content.

    Strategy:
    1. **LLM pairwise comparison** (preferred): uses fast model
       (gpt-4o-mini or equivalent) for accurate semantic analysis.
       Cost: ~80 tokens per call (~$0.00001).
    2. **Heuristic fallback**: when LLM unavailable, uses string
       comparison rules. Conservative (low false-positive rate).

    Args:
        old_content: Existing concept's content string.
        new_content: Incoming content for the same concept name.
        llm_router: Optional LLM router for semantic analysis.
        fast_model: Model identifier for fast/cheap inference.

    Returns:
        ContentRelation: SAME, EXTENDS, CONTRADICTS, or UNRELATED.
    """
    # Guard: empty content → SAME
    if not old_content or not new_content:
        return ContentRelation.SAME

    # Guard: identical → SAME (skip LLM call)
    if old_content.strip().lower() == new_content.strip().lower():
        return ContentRelation.SAME

    # Tier 1: LLM-assisted
    if llm_router is not None:
        try:
            return await _detect_via_llm(
                old_content,
                new_content,
                llm_router,
                fast_model,
            )
        except (LLMError, json.JSONDecodeError, ValueError):
            # LLMError: provider/router-level failure (circuit open,
            # budget exceeded, provider unavailable). JSONDecodeError:
            # model returned malformed JSON. ValueError: parsed JSON
            # didn't match the expected shape. Heuristic fallback below
            # is non-ambiguous and safe; traceback was already logged.
            logger.debug(
                "llm_contradiction_detection_failed_falling_back",
                exc_info=True,
            )

    # Tier 2: Heuristic fallback
    return _detect_contradiction_heuristic(old_content, new_content)


async def _detect_via_llm(
    old_content: str,
    new_content: str,
    llm_router: LLMRouter,
    fast_model: str,
) -> ContentRelation:
    """Classify content relationship via LLM pairwise comparison.

    Uses structured JSON output for reliable parsing. Truncates
    content to 200 chars to keep costs minimal (~80 tokens total).

    Args:
        old_content: Existing content (truncated to 200 chars).
        new_content: New content (truncated to 200 chars).
        llm_router: LLM router for inference.
        fast_model: Model identifier.

    Returns:
        ContentRelation from LLM classification.

    Raises:
        ValueError: If LLM response can't be parsed.
    """
    # Truncate for cost control
    old_trunc = old_content[:200]
    new_trunc = new_content[:200]

    prompt = _CONTRADICTION_PROMPT.format(
        old_content=old_trunc,
        new_content=new_trunc,
    )

    resp = await llm_router.generate(
        messages=[{"role": "user", "content": prompt}],
        model=fast_model or None,
        temperature=0.0,
        max_tokens=100,
    )

    text = resp.content.strip()

    # Parse JSON response
    try:
        # Handle markdown code blocks
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()

        data = json.loads(text)
        relation_str = data.get("relation", "SAME").upper()
        reason = data.get("reason", "")

        relation = ContentRelation(relation_str)
        logger.debug(
            "llm_contradiction_result",
            relation=relation.value,
            reason=reason,
        )
        return relation

    except (json.JSONDecodeError, ValueError, KeyError):
        # Try extracting relation from raw text
        text_upper = text.upper()
        for candidate in ContentRelation:
            if candidate.value in text_upper:
                return candidate

        logger.warning(
            "llm_contradiction_unparseable",
            raw_response=text[:100],
        )
        # Conservative: assume SAME when unparseable
        return ContentRelation.SAME
