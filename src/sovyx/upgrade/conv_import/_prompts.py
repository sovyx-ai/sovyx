"""Prompts for the summary-first conversation import encoder.

Kept in a dedicated module so the prompt text can be reviewed,
re-tuned, or localised without touching the encoder's control flow.
"""

from __future__ import annotations

# Token budgeting knobs. Applied by ``_format_turns`` before the
# transcript is stitched into the prompt body.
MAX_HEAD_TURNS = 15
"""Number of leading turns kept verbatim when the conversation is long."""

MAX_TAIL_TURNS = 15
"""Number of trailing turns kept verbatim when the conversation is long."""

MAX_TURN_CHARS = 800
"""Per-turn character cap — truncates long assistant replies."""

MAX_CONCEPTS = 5
"""How many concepts the LLM is asked to surface per conversation."""

# The summariser is English-facing but Claude / GPT handle multilingual
# input natively, so the conversation body can arrive in any language
# and the output JSON will still be structurally valid.
SUMMARY_PROMPT_TEMPLATE = """\
You are building long-term memory for a personal AI companion from a
conversation imported from another assistant. Extract:

1. SUMMARY — a single sentence (max 30 words) describing what the
   conversation was about.
2. CONCEPTS — up to {max_concepts} key concepts worth remembering
   (facts, preferences, skills, goals, entities, beliefs, events,
   relationships). Each carries a category, a short content line,
   and an importance in [0.0, 1.0].
3. EMOTIONAL tone: valence in [-1.0, 1.0] (negative = sad/angry,
   positive = happy/excited) and arousal in [0.0, 1.0] (low = calm,
   high = intense).
4. IMPORTANCE — how important this conversation is for long-term
   memory, in [0.0, 1.0]. High for conversations that establish user
   preferences, goals, or relationships; low for throwaway requests.

Conversation metadata:
- Platform: {platform}
- Title: {title}
- Turns: {turns_count}

Transcript:

{formatted_turns}

Respond with valid JSON only. No markdown fences, no commentary. Use
exactly this shape:

{{"summary": "...", "concepts": [{{"name": "...", "category": "fact", "content": "...", "importance": 0.7}}, ...], "emotional_valence": 0.0, "emotional_arousal": 0.3, "importance": 0.5}}
"""  # noqa: E501 — JSON example on one line matches how the model should emit it
"""Prompt fed to the LLM for each conversation. The encoder fills the
format placeholders and calls ``llm_router.generate``."""


VALID_CATEGORIES: frozenset[str] = frozenset(
    {
        "fact",
        "preference",
        "entity",
        "skill",
        "belief",
        "event",
        "relationship",
    }
)
"""Concept categories the brain subsystem accepts. Anything the LLM
emits outside this set is mapped to ``"fact"`` with a warning."""
