"""Summary-first encoder — one LLM call per imported conversation.

Option C of IMPL-SUP-015: instead of running the full REFLECT phase on
every turn (prohibitively expensive at onboarding scale), we send the
entire conversation to a fast model once, extract a summary + top
concepts + emotional signal, and encode a single ``Episode`` with that
synthesised metadata. The mind ends up knowing "the user talked a lot
about X, Y, Z" across their imported history without paying per-turn
LLM costs.

Cost budget target: ~1 fast-model call per conversation, $0.001-0.003
with claude-haiku-4.5 or equivalent. A 1000-conversation import lands
inside $3 and ~20 minutes at a 10-rpm rate limit.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sovyx.engine.errors import LLMError
from sovyx.engine.types import ConceptCategory, ConceptId
from sovyx.observability.logging import get_logger
from sovyx.upgrade.conv_import._prompts import (
    MAX_CONCEPTS,
    MAX_HEAD_TURNS,
    MAX_TAIL_TURNS,
    MAX_TURN_CHARS,
    SUMMARY_PROMPT_TEMPLATE,
    VALID_CATEGORIES,
)

if TYPE_CHECKING:
    from sovyx.brain.service import BrainService
    from sovyx.engine.types import EpisodeId, MindId
    from sovyx.llm.router import LLMRouter
    from sovyx.upgrade.conv_import._base import RawConversation, RawMessage

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class SummaryResult:
    """Outcome of encoding a single conversation.

    The caller (conversation-import worker) uses ``concept_ids`` to
    bump its running ``concepts_learned`` counter and ``warnings`` to
    surface non-fatal quirks (bad JSON, unknown categories, missing
    turns) via the progress tracker.
    """

    episode_id: EpisodeId
    concept_ids: tuple[ConceptId, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _ParsedSummary:
    """Validated shape of what the LLM returned."""

    summary: str
    concepts: tuple[_ParsedConcept, ...]
    emotional_valence: float
    emotional_arousal: float
    emotional_dominance: float
    importance: float


@dataclass(frozen=True, slots=True)
class _ParsedConcept:
    name: str
    category: ConceptCategory
    content: str
    importance: float


# Fallback values used when the LLM is unavailable, returns malformed
# JSON, or raises. Keeps the Episode importable even without
# summarisation — the conversation shows up in history with a thin
# fallback summary, and the user can always re-run a deeper import
# later.
_FALLBACK_IMPORTANCE = 0.3
_FALLBACK_VALENCE = 0.0
_FALLBACK_AROUSAL = 0.0
_FALLBACK_DOMINANCE = 0.0


async def summarize_and_encode(
    conv: RawConversation,
    brain: BrainService,
    llm_router: LLMRouter | None,
    mind_id: MindId,
    *,
    fast_model: str | None = None,
) -> SummaryResult:
    """Encode one imported conversation as an Episode + concept rows.

    Flow:
        1. Format the conversation into the summarisation prompt
           (head/tail truncation for long conversations).
        2. Call the LLM router with ``fast_model`` and low temperature.
        3. Parse the JSON response; on any shape issue, fall back to a
           minimal summary derived from the conversation title.
        4. ``brain.learn_concept`` each extracted concept (deduped
           against existing).
        5. ``brain.encode_episode`` with synthesised metadata.

    Args:
        conv: The parsed conversation to encode.
        brain: Brain service to receive the Episode + concepts.
        llm_router: Optional — when ``None``, skips the LLM call and
            uses the fallback path (useful for offline bulk imports).
        mind_id: Destination mind.
        fast_model: Optional model override for the summariser (per
            caller's router config).

    Returns:
        :class:`SummaryResult` with the new Episode ID, concept IDs,
        and any warnings accumulated during encoding.
    """
    warnings: list[str] = []

    # ── Step 1: ask the LLM for a summary, or use fallback ─────────
    parsed: _ParsedSummary | None = None
    if llm_router is not None and conv.turn_count() > 0:
        parsed = await _call_summariser(conv, llm_router, fast_model, warnings)

    if parsed is None:
        parsed = _build_fallback(conv, warnings)

    # ── Step 2: create concept rows via BrainService ───────────────
    concept_ids: list[ConceptId] = []
    for c in parsed.concepts:
        try:
            cid = await brain.learn_concept(
                mind_id=mind_id,
                name=c.name,
                content=c.content,
                category=c.category,
                source=f"import:{conv.platform}",
                importance=c.importance,
                confidence=0.6,
                emotional_valence=parsed.emotional_valence,
                emotional_arousal=parsed.emotional_arousal,
                emotional_dominance=parsed.emotional_dominance,
            )
            concept_ids.append(cid)
        except (ValueError, AttributeError) as exc:
            warnings.append(f"concept '{c.name}' rejected: {exc}")

    # ── Step 3: encode the Episode ─────────────────────────────────
    episode_id = await brain.encode_episode(
        mind_id=mind_id,
        conversation_id=conv.conversation_id,  # type: ignore[arg-type]
        user_input=_truncate(conv.first_user_text(), MAX_TURN_CHARS * 2),
        assistant_response=_truncate(conv.last_assistant_text(), MAX_TURN_CHARS * 2),
        importance=parsed.importance,
        emotional_valence=parsed.emotional_valence,
        emotional_arousal=parsed.emotional_arousal,
        emotional_dominance=parsed.emotional_dominance,
        new_concept_ids=concept_ids,
        concepts_mentioned=concept_ids,
        summary=parsed.summary,
    )

    return SummaryResult(
        episode_id=episode_id,
        concept_ids=tuple(concept_ids),
        warnings=tuple(warnings),
    )


# ── LLM call + parsing ─────────────────────────────────────────────


async def _call_summariser(
    conv: RawConversation,
    llm_router: LLMRouter,
    fast_model: str | None,
    warnings: list[str],
) -> _ParsedSummary | None:
    """Run the LLM summariser; return None on any failure.

    Failures are appended to ``warnings`` so the caller can surface
    them to the progress endpoint. Never raises.
    """
    prompt = SUMMARY_PROMPT_TEMPLATE.format(
        max_concepts=MAX_CONCEPTS,
        platform=conv.platform,
        title=conv.title or "(untitled)",
        turns_count=conv.turn_count(),
        formatted_turns=_format_turns(conv),
    )
    try:
        response = await llm_router.generate(
            messages=[{"role": "user", "content": prompt}],
            model=fast_model,
            temperature=0.1,
            max_tokens=512,
        )
    except (LLMError, AttributeError) as exc:
        warnings.append(f"summariser LLM failure: {exc}")
        return None

    text = (response.content or "").strip()
    if not text:
        warnings.append("summariser returned empty response")
        return None

    # Strip markdown code fences the model sometimes adds.
    if text.startswith("```"):
        lines = text.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        warnings.append(f"summariser JSON parse failed: {exc}")
        return None

    return _validate_summary(data, warnings)


def _validate_summary(
    data: object,
    warnings: list[str],
) -> _ParsedSummary | None:
    """Coerce raw JSON into a :class:`_ParsedSummary` with clamps."""
    if not isinstance(data, dict):
        warnings.append("summariser returned non-object JSON")
        return None

    summary = _as_str(data.get("summary"), limit=300).strip()
    if not summary:
        warnings.append("summariser returned empty summary")
        return None

    concepts_raw = data.get("concepts")
    concepts: list[_ParsedConcept] = []
    if isinstance(concepts_raw, list):
        for item in concepts_raw[:MAX_CONCEPTS]:
            parsed = _parse_concept(item, warnings)
            if parsed is not None:
                concepts.append(parsed)

    return _ParsedSummary(
        summary=summary,
        concepts=tuple(concepts),
        emotional_valence=_clamp(data.get("emotional_valence"), -1.0, 1.0, 0.0),
        emotional_arousal=_clamp(data.get("emotional_arousal"), 0.0, 1.0, 0.0),
        # PAD 3D dominance axis — range [-1, +1] (ADR-001). Negative =
        # hedging/submissive, positive = assertive/in-control.
        emotional_dominance=_clamp(data.get("emotional_dominance"), -1.0, 1.0, 0.0),
        importance=_clamp(data.get("importance"), 0.0, 1.0, 0.5),
    )


def _parse_concept(
    item: object,
    warnings: list[str],
) -> _ParsedConcept | None:
    """Validate one concept dict from the LLM output."""
    if not isinstance(item, dict):
        return None
    name = _as_str(item.get("name"), limit=100).strip()
    if not name:
        return None
    content = _as_str(item.get("content"), limit=500).strip() or name

    category_raw = str(item.get("category") or "fact").strip().lower()
    if category_raw not in VALID_CATEGORIES:
        warnings.append(f"unknown concept category '{category_raw}' → 'fact'")
        category_raw = "fact"

    return _ParsedConcept(
        name=name,
        category=ConceptCategory(category_raw),
        content=content,
        importance=_clamp(item.get("importance"), 0.0, 1.0, 0.5),
    )


# ── Fallback path ──────────────────────────────────────────────────


def _build_fallback(
    conv: RawConversation,
    warnings: list[str],
) -> _ParsedSummary:
    """Minimal summary when the LLM is unavailable or misbehaves.

    The Episode still lands — just without extracted concepts and with
    a conservative importance of 0.3. The user can always re-run the
    import against a working LLM later.
    """
    title = conv.title.strip() or "Imported conversation"
    summary = f"{title} ({conv.turn_count()} turns, imported from {conv.platform})"
    warnings.append("used fallback summary — no concepts extracted")
    return _ParsedSummary(
        summary=summary[:300],
        concepts=(),
        emotional_valence=_FALLBACK_VALENCE,
        emotional_arousal=_FALLBACK_AROUSAL,
        emotional_dominance=_FALLBACK_DOMINANCE,
        importance=_FALLBACK_IMPORTANCE,
    )


# ── Transcript formatting ──────────────────────────────────────────


def _format_turns(conv: RawConversation) -> str:
    """Render conversation turns into the prompt body.

    Keeps ``MAX_HEAD_TURNS`` head + ``MAX_TAIL_TURNS`` tail turns for
    long conversations, marking the omitted middle explicitly so the
    LLM doesn't confuse abrupt context shifts with topic changes.
    System and tool turns are skipped — they're not conversational.
    """
    user_assist = [m for m in conv.messages if m.role in ("user", "assistant")]
    if not user_assist:
        return "(empty conversation)"

    head_max = MAX_HEAD_TURNS
    tail_max = MAX_TAIL_TURNS
    if len(user_assist) <= head_max + tail_max:
        return "\n\n".join(_format_turn(m) for m in user_assist)

    head = user_assist[:head_max]
    tail = user_assist[-tail_max:]
    omitted = len(user_assist) - head_max - tail_max
    middle = f"... [{omitted} turns omitted for brevity] ..."
    return "\n\n".join(
        [*(_format_turn(m) for m in head), middle, *(_format_turn(m) for m in tail)],
    )


def _format_turn(msg: RawMessage) -> str:
    label = "User" if msg.role == "user" else "Assistant"
    return f"[{label}]: {_truncate(msg.text, MAX_TURN_CHARS)}"


# ── Small helpers ──────────────────────────────────────────────────


def _truncate(text: str, limit: int) -> str:
    """Hard-truncate ``text`` to ``limit`` chars with an ellipsis."""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _as_str(value: object, *, limit: int) -> str:
    """Coerce an LLM-emitted value to a bounded string."""
    if value is None:
        return ""
    s = str(value)
    if len(s) > limit:
        s = s[:limit]
    return s


def _clamp(value: Any, lo: float, hi: float, default: float) -> float:  # noqa: ANN401
    """Clamp a numeric-ish value into [lo, hi], falling back on default."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    if f != f:  # NaN guard — NaN is the only value that != itself
        return default
    return max(lo, min(hi, f))
