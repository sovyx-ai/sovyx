"""Auto-extracted from cognitive/reflect.py — see __init__.py for the public re-exports."""

from __future__ import annotations

# ── LLM extraction prompt ──────────────────────────────────────────────
# Covers all 7 ConceptCategory values with clear definitions and examples
# so the LLM can reliably distinguish between them.

_EXTRACTION_PROMPT = (
    "Extract knowledge from the user message into structured concepts.\n"
    "Return a JSON array of objects with these fields:\n"
    '- "name": short label (2-5 words)\n'
    '- "content": one-sentence description of what was learned\n'
    '- "category": one of the categories below\n'
    '- "sentiment": float -1.0 to 1.0 (emotional tone)\n'
    '- "importance": float 0.0-1.0 (how critical to remember?)\n'
    '- "confidence": float 0.0-1.0 (how certain is this info?)\n'
    '- "explicit": boolean (did user ask to remember this?)\n'
    '- "source_quality": "explicit" if directly stated, '
    '"inferred" if deduced\n'
    "\n"
    "Categories (pick the MOST specific one):\n"
    '- "entity": person, org, place, or named thing '
    '(e.g. "John", "Google")\n'
    '- "fact": objective, verifiable info '
    '(e.g. "works remotely", "3 years experience")\n'
    '- "preference": like, dislike, or personal taste '
    '(e.g. "prefers dark mode", "loves PostgreSQL")\n'
    '- "skill": technical ability or competency '
    '(e.g. "knows Rust", "expert in K8s")\n'
    '- "belief": subjective opinion or value judgment '
    '(e.g. "thinks ORMs are harmful")\n'
    '- "event": time-bound occurrence or milestone '
    '(e.g. "migrated to AWS last month")\n'
    '- "relationship": connection between entities '
    '(e.g. "manages a team of 5", "reports to CTO")\n'
    "\n"
    "Importance guide:\n"
    "- 0.1-0.3: trivial/passing mention (oh btw, it's raining)\n"
    "- 0.4-0.6: useful fact worth noting (I use Python daily)\n"
    "- 0.7-0.8: significant personal info (I'm building a startup)\n"
    "- 0.9-1.0: core identity/critical (my name is X, I have Y)\n"
    '- If user says "remember/note/important/don\'t forget": 0.9+\n'
    "\n"
    "Confidence guide:\n"
    "- 0.1-0.3: very uncertain, ambiguous, might be sarcasm\n"
    "- 0.4-0.6: inferred/implied, not directly stated\n"
    "- 0.7-0.8: clearly stated but could change\n"
    "- 0.9-1.0: definitively stated, identity, strong assertion\n"
    "\n"
    "Sentiment guide:\n"
    "- Positive (0.3 to 1.0): love, enjoy, excited, great\n"
    "- Neutral (~0.0): factual statements, introductions\n"
    "- Negative (-1.0 to -0.3): hate, frustrate, terrible\n"
    "\n"
    "Rules:\n"
    "- Extract ALL meaningful information\n"
    "- Be specific: "
    '"thinks GraphQL adds complexity" not "dislikes GraphQL"\n'
    "- Distinguish: "
    '"prefers X"=preference, "thinks X is bad"=belief, '
    '"knows X"=skill\n'
    "- Skip greetings, filler, questions asking for info\n"
    "- Return [] if no learnable information\n"
    "- Return ONLY the JSON array, no other text\n"
    "\n"
    "User message: {message}"
)

# ── Relation classification prompt ──────────────────────────────────────
# Classifies the relationship between concept pairs extracted from
# the same message. Only used for within-turn pairs (≤C(n,2) where n~4-5).

_RELATION_PROMPT = (
    "Given these concepts extracted from a user message, "
    "classify the relationship between each pair.\n"
    "Return a JSON array of objects with:\n"
    '- "a": name of first concept\n'
    '- "b": name of second concept\n'
    '- "relation": one of the types below\n'
    "\n"
    "Relation types:\n"
    '- "related_to": general association (default)\n'
    '- "part_of": A is a component/subset of B\n'
    '- "causes": A leads to or causes B\n'
    '- "contradicts": A conflicts with or opposes B\n'
    '- "example_of": A is an instance/example of B\n'
    '- "temporal": A happened before/after/during B\n'
    '- "emotional": A has an emotional connection to B\n'
    "\n"
    "Rules:\n"
    "- Pick the MOST specific relation, not related_to\n"
    "- If unsure, use related_to\n"
    "- Return ONLY the JSON array\n"
    "\n"
    "Concepts: {concepts}\n"
    "User message: {message}"
)

_VALID_RELATIONS = frozenset(
    {
        "related_to",
        "part_of",
        "causes",
        "contradicts",
        "example_of",
        "temporal",
        "emotional",
    }
)
