"""Sovyx ReflectPhase — encode episode + extract concepts + Hebbian learning.

Uses LLM-based concept extraction for rich, accurate knowledge capture.
Falls back to regex-based extraction if LLM is unavailable.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from sovyx.brain.service import BrainService
    from sovyx.cognitive.perceive import Perception
    from sovyx.engine.types import ConceptId, ConversationId, MindId
    from sovyx.llm.models import LLMResponse
    from sovyx.llm.router import LLMRouter

logger = get_logger(__name__)


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
    sentiment: float = 0.0       # -1.0 (negative) to 1.0 (positive)
    importance: float = 0.5      # LLM-assessed importance (0.0-1.0)
    confidence: float = 0.7      # LLM-assessed confidence (0.0-1.0)
    explicit: bool = False       # User explicitly asked to remember
    source_quality: str = "explicit"  # "explicit" or "inferred"


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
    "entity": 0.80,       # People, places, orgs — identity-critical
    "relationship": 0.80,  # Social connections — rare, meaningful
    "preference": 0.70,    # Personal taste — defines personality
    "skill": 0.70,         # Capabilities — shapes responses
    "event": 0.70,         # Time-bound — contextual anchors
    "fact": 0.60,          # Verifiable info — common but useful
    "belief": 0.60,        # Opinions — shapes worldview
}

# Default importance for unknown categories
_DEFAULT_IMPORTANCE = 0.5

# ── Source confidence mapping ──────────────────────────────────────────
# Confidence assigned based on extraction quality.
# Key = source type, Value = (floor, ceiling). Midpoint is used.
# Higher confidence = more epistemic certainty about the information.

_SOURCE_CONFIDENCE: dict[str, tuple[float, float]] = {
    "llm_explicit": (0.75, 0.95),    # LLM extracted from clear user statement
    "llm_inferred": (0.45, 0.70),    # LLM inferred (not directly stated)
    "regex_fallback": (0.30, 0.55),  # Regex pattern match (less reliable)
    "system": (0.90, 1.00),          # System-generated (identity, etc.)
    "corroboration": (0.80, 1.00),   # Multiple sources agree
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

# ── Sentiment heuristics for regex fallback ────────────────────────────
# Maps pattern groups to default sentiment when LLM is unavailable.

_POSITIVE_WORDS = frozenset(
    {
        "love",
        "like",
        "prefer",
        "enjoy",
        "great",
        "awesome",
        "excellent",
        "best",
        "amazing",
        "adoro",
        "gosto",
        "curto",
    }
)
_NEGATIVE_WORDS = frozenset(
    {
        "hate",
        "dislike",
        "avoid",
        "terrible",
        "worst",
        "awful",
        "frustrating",
        "harmful",
        "bad",
        "odeio",
        "detesto",
    }
)


def _estimate_sentiment(text: str) -> float:
    """Heuristic sentiment estimation for regex fallback.

    Returns a rough sentiment score based on keyword presence.
    """
    lower = text.lower()
    words = set(lower.split())
    pos = len(words & _POSITIVE_WORDS)
    neg = len(words & _NEGATIVE_WORDS)
    if pos > neg:
        return min(0.6, 0.3 * pos)
    if neg > pos:
        return max(-0.6, -0.3 * neg)
    return 0.0


# ── Regex fallback patterns ────────────────────────────────────────────

_ENTITY_PATTERNS = [
    re.compile(r"(?:my name is|i'm|i am)\s+(\w+)", re.IGNORECASE),
    re.compile(r"(?:meu nome é|me chamo|sou o|sou a)\s+(\w+)", re.IGNORECASE),
]

_PREFERENCE_PATTERNS = [
    re.compile(
        r"(?:i (?:like|love|prefer|enjoy|use|work with))\s+(.+?)(?:\.|,|!|$)",
        re.IGNORECASE,
    ),
    re.compile(r"(?:i (?:hate|dislike|avoid))\s+(.+?)(?:\.|,|!|$)", re.IGNORECASE),
    re.compile(
        r"(?:eu (?:gosto|adoro|prefiro|curto|uso))"
        r"\s+(?:de |do |da )?(.+?)(?:\.|,|!|$)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:my (?:stack|tools?|setup) (?:is|includes?|:))"
        r"\s*(.+?)(?:\.|!|$)",
        re.IGNORECASE,
    ),
]

_FACT_PATTERNS = [
    re.compile(
        r"(?:i (?:work at|work for|live in|study at|am building|built))"
        r"\s+(.+?)(?:\.|,|!|$)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:i'm (?:building|developing|working on|learning))"
        r"\s+(.+?)(?:\.|,|!|$)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:(?:trabalho|moro|estudo) (?:na|no|em|na))"
        r"\s+(.+?)(?:\.|,|!|$)",
        re.IGNORECASE,
    ),
]

_SKILL_PATTERNS = [
    re.compile(
        r"(?:i (?:code|program|develop) (?:in|with))\s+(.+?)(?:\.|,|!|$)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:my (?:primary |main )?(?:language|stack|framework)s?"
        r"\s+(?:is|are|:))\s*(.+?)(?:\.|!|$)",
        re.IGNORECASE,
    ),
]

_BELIEF_PATTERNS = [
    re.compile(
        r"(?:i (?:think|believe|feel that|consider))\s+(.+?)(?:\.|,|!|$)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:in my (?:opinion|view|experience))\s*[,:]?\s*(.+?)(?:\.|!|$)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:eu (?:acho|acredito|penso) que)\s+(.+?)(?:\.|,|!|$)",
        re.IGNORECASE,
    ),
]

_EVENT_PATTERNS = [
    re.compile(
        r"(?:i (?:started|finished|completed|launched|migrated"
        r"|deployed|graduated))"
        r"\s+(.+?)(?:\.|,|!|$)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:last (?:week|month|year)|recently|yesterday|in \d{4})"
        r"\s*[,:]?\s*"
        r"(?:i |we )?(.+?)(?:\.|,|!|$)",
        re.IGNORECASE,
    ),
]

_RELATIONSHIP_PATTERNS = [
    re.compile(
        r"(?:i (?:manage|lead|report to|work with|mentor))"
        r"\s+(.+?)(?:\.|,|!|$)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:my (?:team|manager|boss|colleague|partner) (?:is|are))"
        r"\s+(.+?)(?:\.|,|!|$)",
        re.IGNORECASE,
    ),
]


def resolve_category(raw_category: str) -> str:
    """Resolve a raw LLM category string to a canonical ConceptCategory value.

    Uses ``_CATEGORY_MAP`` for alias resolution. Falls back to ``"fact"``
    for unknown categories.

    Args:
        raw_category: The raw category string from LLM or regex extraction.

    Returns:
        A canonical category string matching a ``ConceptCategory`` enum value.
    """
    return _CATEGORY_MAP.get(raw_category.strip().lower(), "fact")


def get_importance(category: str) -> float:
    """Return initial importance for a concept category.

    Args:
        category: Canonical category string (after ``resolve_category``).

    Returns:
        Importance value in [0.0, 1.0].
    """
    return _IMPORTANCE.get(category, _DEFAULT_IMPORTANCE)


def get_source_confidence(source: str) -> float:
    """Return midpoint confidence for extraction source quality.

    Maps the extraction method to an epistemic certainty score.
    LLM explicit extraction → high confidence; regex fallback → lower.

    Args:
        source: Source type key (e.g. ``"llm_explicit"``, ``"regex_fallback"``).

    Returns:
        Confidence midpoint in [0.0, 1.0].
    """
    low, high = _SOURCE_CONFIDENCE.get(source, _DEFAULT_SOURCE_CONFIDENCE)
    return (low + high) / 2


def detect_explicit_importance(message: str) -> bool:
    """Detect if user explicitly asks to remember information.

    Checks for phrases like "remember this", "don't forget",
    "lembra disso", etc. in both English and Portuguese.

    When True, ALL concepts from this message get their importance
    floor raised to 0.85 and confidence floor raised to 0.75.

    Args:
        message: User message text.

    Returns:
        True if explicit importance signal detected.
    """
    return any(p.search(message) for p in _EXPLICIT_PATTERNS)


def compute_episode_importance(
    message: str,
    num_concepts: int,
    max_valence: float,
) -> float:
    """Compute dynamic episode importance from message characteristics.

    Scoring formula:
    - Base: 0.3 + message_length / 500 (longer = more content)
    - Concepts: +0.05 per concept (up to 6)
    - Emotion: +0.1 * |max_valence| (emotional = memorable)
    - Clamped to [0.1, 1.0]

    Args:
        message: The user's input message.
        num_concepts: Number of concepts extracted from the message.
        max_valence: Maximum absolute sentiment across concepts.

    Returns:
        Episode importance in [0.1, 1.0].
    """
    base = min(0.7, 0.3 + len(message) / 500)
    concept_bonus = 0.05 * min(num_concepts, 6)
    emotion_bonus = 0.1 * abs(max_valence)
    return max(0.1, min(1.0, base + concept_bonus + emotion_bonus))


def clamp_sentiment(value: float) -> float:
    """Clamp a sentiment value to [-1.0, 1.0].

    Args:
        value: Raw sentiment value.

    Returns:
        Clamped value in [-1.0, 1.0].
    """
    return max(-1.0, min(1.0, value))


class ReflectPhase:
    """After response: encode episode + extract concepts + Hebbian.

    1. Create episode with user_input + assistant_response
    2. Extract concepts via LLM (falls back to regex if unavailable)
    3. Hebbian learning between co-mentioned concepts
    4. Update working memory with emotional valence
    """

    def __init__(
        self,
        brain_service: BrainService,
        llm_router: LLMRouter | None = None,
        fast_model: str = "",
    ) -> None:
        self._brain = brain_service
        self._router = llm_router
        self._fast_model = fast_model

    async def process(
        self,
        perception: Perception,
        response: LLMResponse,
        mind_id: MindId,
        conversation_id: ConversationId,
    ) -> None:
        """Reflect on the interaction: learn concepts + encode episode."""
        from sovyx.engine.types import ConceptCategory

        # Extract concepts — LLM first, regex fallback
        extracted: list[ExtractedConcept] = []
        extraction_source: str = "regex_fallback"

        if self._router:
            llm_extracted = await self._extract_with_llm(perception.content)
            if llm_extracted is not None:
                extracted = llm_extracted
                extraction_source = "llm_explicit"

        if not extracted:
            extracted = self._extract_with_regex(perception.content)
            extraction_source = "regex_fallback"

        # Detect message-level explicit importance signal.
        # Overrides per-concept explicit flag: if the user said "remember this"
        # in the message, ALL concepts from it are treated as explicit.
        message_explicit = detect_explicit_importance(perception.content)

        # Learn extracted concepts with multi-signal importance + confidence.
        # LLM path: combine LLM assessment with category baseline.
        # Regex path: use category importance + source confidence only.
        concept_ids: list[ConceptId] = []
        sentiments: list[float] = []

        # Pre-compute novelty for all concepts in batch
        # Uses embedding cosine distance → FTS5 fallback → cold start
        novelty_map = await self._compute_novelty_batch(extracted, mind_id)

        for ec in extracted:
            try:
                resolved = resolve_category(ec.category)
                category = ConceptCategory(resolved)
                category_importance = get_importance(resolved)

                # Explicit signal: per-concept OR message-level
                is_explicit = ec.explicit or message_explicit

                # Novelty: 1.0 = completely new, 0.0 = exact duplicate
                novelty = novelty_map.get(ec.name, 0.5)

                if extraction_source == "llm_explicit":
                    # Combine LLM assessment with category baseline
                    # LLM (0.35) + category (0.15) + emotion (0.10) +
                    # novelty (0.15) + explicit (0.25)
                    combined_importance = (
                        0.35 * ec.importance
                        + 0.15 * category_importance
                        + 0.10 * abs(ec.sentiment)
                        + 0.15 * novelty
                        + 0.25 * (1.0 if is_explicit else 0.0)
                    )
                    if is_explicit:
                        combined_importance = max(combined_importance, 0.85)

                    # Combine LLM confidence with source quality
                    source_tag = f"llm_{ec.source_quality}"
                    source_conf = get_source_confidence(source_tag)
                    combined_confidence = (
                        0.40 * ec.confidence
                        + 0.35 * source_conf
                        + 0.15 * (1.0 if ec.source_quality == "explicit" else 0.3)
                        + 0.10 * min(1.0, len(ec.content) / 100)
                    )
                    # Explicit signal boosts confidence floor too
                    if is_explicit:
                        combined_confidence = max(combined_confidence, 0.75)
                else:
                    # Regex fallback: category (0.60) + novelty (0.40) + source confidence
                    combined_importance = 0.60 * category_importance + 0.40 * novelty
                    combined_confidence = get_source_confidence("regex_fallback")
                    # Message-level explicit applies to regex path too
                    if is_explicit:
                        combined_importance = max(combined_importance, 0.85)
                        combined_confidence = max(combined_confidence, 0.75)

                # Clamp to valid range
                combined_importance = max(0.05, min(1.0, combined_importance))
                combined_confidence = max(0.05, min(1.0, combined_confidence))

                cid = await self._brain.learn_concept(
                    mind_id=mind_id,
                    name=ec.name,
                    content=ec.content,
                    category=category,
                    source="conversation",
                    importance=combined_importance,
                    confidence=combined_confidence,
                    emotional_valence=ec.sentiment,
                )
                concept_ids.append(cid)
                sentiments.append(ec.sentiment)
            except Exception:
                logger.warning(
                    "concept_extraction_failed",
                    name=ec.name,
                    exc_info=True,
                )

        # Hebbian learning between co-mentioned concepts
        # Classify relation types via LLM for within-turn pairs
        relation_types: dict[tuple[str, str], str] | None = None
        if len(concept_ids) >= 2 and self._router:  # noqa: PLR2004
            relation_types = await self._classify_relations(extracted, concept_ids)

        if len(concept_ids) >= 2:  # noqa: PLR2004
            try:
                await self._brain.strengthen_connection(concept_ids, relation_types=relation_types)
            except Exception:
                logger.warning("hebbian_failed", exc_info=True)

        # Compute episode emotional signals from extracted concepts
        episode_valence = 0.0
        episode_arousal = 0.0
        if sentiments:
            episode_valence = clamp_sentiment(sum(sentiments) / len(sentiments))
            episode_arousal = clamp_sentiment(max(abs(s) for s in sentiments))

        # Dynamic episode importance based on message characteristics
        episode_importance = compute_episode_importance(
            message=perception.content,
            num_concepts=len(concept_ids),
            max_valence=episode_arousal,  # arousal = max |sentiment|
        )

        # Generate episode summary via LLM (optional)
        summary = await self._generate_summary(perception.content, response.content)

        # Encode episode — pass new concept IDs as star topology hubs
        # (each connects to top-K existing by activation)
        try:
            await self._brain.encode_episode(
                mind_id=mind_id,
                conversation_id=conversation_id,
                user_input=perception.content,
                assistant_response=response.content,
                importance=episode_importance,
                new_concept_ids=concept_ids or None,
                emotional_valence=episode_valence,
                emotional_arousal=episode_arousal,
                concepts_mentioned=concept_ids or None,
                summary=summary,
            )
        except Exception:
            logger.warning("episode_encoding_failed", exc_info=True)

        logger.debug(
            "reflect_complete",
            concepts_extracted=len(extracted),
            concepts_learned=len(concept_ids),
            episode_valence=round(episode_valence, 2),
            episode_arousal=round(episode_arousal, 2),
        )

    async def _compute_novelty_batch(
        self,
        extracted: list[ExtractedConcept],
        mind_id: MindId,
    ) -> dict[str, float]:
        """Compute novelty score for each extracted concept.

        Delegates to BrainService.compute_novelty() which uses a 3-tier
        strategy: embedding cosine distance → FTS5 fallback → cold start.

        Args:
            extracted: List of extracted concepts (name + category needed).
            mind_id: Mind to compare against.

        Returns:
            Dict mapping concept name → novelty score [0.05, 1.0].
        """
        result: dict[str, float] = {}
        for ec in extracted:
            try:
                resolved = resolve_category(ec.category)
                novelty = await self._brain.compute_novelty(
                    text=f"{ec.name}: {ec.content}" if ec.content else ec.name,
                    category=resolved,
                    mind_id=mind_id,
                )
                result[ec.name] = novelty
            except Exception:
                logger.debug("novelty_check_failed", name=ec.name)
                result[ec.name] = 0.5
        return result

    async def _extract_with_llm(self, message: str) -> list[ExtractedConcept] | None:
        """Extract concepts using LLM. Returns None on failure."""
        if not self._router:
            return None

        try:
            prompt = _EXTRACTION_PROMPT.format(message=message)
            resp = await self._router.generate(
                messages=[{"role": "user", "content": prompt}],
                model=self._fast_model or None,
                temperature=0.1,
                max_tokens=1024,
            )

            # Parse JSON response
            text = resp.content.strip()
            # Handle markdown code blocks
            if text.startswith("```"):
                text = re.sub(r"^```(?:json)?\n?", "", text)
                text = re.sub(r"\n?```$", "", text)

            concepts_raw = json.loads(text)
            if not isinstance(concepts_raw, list):
                return None

            result: list[ExtractedConcept] = []
            for item in concepts_raw:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name", "")).strip()
                content = str(item.get("content", "")).strip()
                category = str(item.get("category", "fact")).strip().lower()

                # Parse sentiment — default 0.0 if missing or invalid
                raw_sentiment = item.get("sentiment", 0.0)
                try:
                    sentiment = clamp_sentiment(float(raw_sentiment))
                except (TypeError, ValueError):
                    sentiment = 0.0

                # Parse importance — LLM-assessed, default 0.5
                raw_importance = item.get("importance", 0.5)
                try:
                    llm_importance = max(0.0, min(1.0, float(raw_importance)))
                except (TypeError, ValueError):
                    llm_importance = 0.5

                # Parse confidence — LLM-assessed, default 0.7
                raw_confidence = item.get("confidence", 0.7)
                try:
                    llm_confidence = max(0.0, min(1.0, float(raw_confidence)))
                except (TypeError, ValueError):
                    llm_confidence = 0.7

                # Parse explicit — user asked to remember
                llm_explicit = bool(item.get("explicit", False))

                # Parse source_quality — explicit vs. inferred
                raw_sq = str(item.get("source_quality", "explicit")).strip().lower()
                source_quality = raw_sq if raw_sq in ("explicit", "inferred") else "explicit"

                if name and content and len(name) > 1:
                    result.append(
                        ExtractedConcept(
                            name=name,
                            content=content,
                            category=category,
                            sentiment=sentiment,
                            importance=llm_importance,
                            confidence=llm_confidence,
                            explicit=llm_explicit,
                            source_quality=source_quality,
                        )
                    )

            logger.debug(
                "llm_concept_extraction",
                concepts_count=len(result),
                model=resp.model,
                cost=round(resp.cost_usd, 6),
            )
            return result

        except Exception:
            logger.debug("llm_extraction_failed_using_regex", exc_info=True)
            return None

    async def _classify_relations(
        self,
        extracted: list[ExtractedConcept],
        concept_ids: list[ConceptId],
    ) -> dict[tuple[str, str], str] | None:
        """Classify relation types between within-turn concept pairs.

        Uses a second LLM call to determine how concepts relate.
        Only called when ≥2 concepts were extracted and LLM is available.

        Args:
            extracted: The extracted concepts (for names).
            concept_ids: The corresponding concept IDs (parallel array).

        Returns:
            Dict mapping (canonical_id_a, canonical_id_b) → relation type
            string, or None on failure.
        """
        if not self._router or len(extracted) < 2:  # noqa: PLR2004
            return None

        try:
            # Build concept list for the prompt
            names = [ec.name for ec in extracted[: len(concept_ids)]]
            concepts_str = ", ".join(f'"{n}"' for n in names)

            # Reconstruct the original message from first concept
            # (we don't have it directly, so use concept names)
            prompt = _RELATION_PROMPT.format(
                concepts=concepts_str,
                message=concepts_str,  # names suffice for classification
            )

            resp = await self._router.generate(
                messages=[{"role": "user", "content": prompt}],
                model=self._fast_model or None,
                temperature=0.1,
                max_tokens=512,
            )

            text = resp.content.strip()
            if text.startswith("```"):
                text = re.sub(r"^```(?:json)?\n?", "", text)
                text = re.sub(r"\n?```$", "", text)

            pairs_raw = json.loads(text)
            if not isinstance(pairs_raw, list):
                return None

            # Build name → concept_id mapping
            name_to_id: dict[str, str] = {}
            for ec, cid in zip(extracted, concept_ids, strict=False):
                name_to_id[ec.name.lower()] = str(cid)

            result: dict[tuple[str, str], str] = {}
            for pair in pairs_raw:
                if not isinstance(pair, dict):
                    continue
                a_name = str(pair.get("a", "")).strip().lower()
                b_name = str(pair.get("b", "")).strip().lower()
                rel = str(pair.get("relation", "related_to")).strip().lower()

                if rel not in _VALID_RELATIONS:
                    rel = "related_to"

                a_id = name_to_id.get(a_name)
                b_id = name_to_id.get(b_name)
                if a_id and b_id and a_id != b_id:
                    # Canonical order
                    key = (min(a_id, b_id), max(a_id, b_id))
                    result[key] = rel

            logger.debug(
                "llm_relation_classification",
                pairs=len(result),
                cost=round(resp.cost_usd, 6),
            )
            return result if result else None

        except Exception:
            logger.debug("relation_classification_failed", exc_info=True)
            return None

    async def _generate_summary(
        self,
        user_input: str,
        assistant_response: str,
    ) -> str | None:
        """Generate a 1-sentence summary of the exchange via LLM.

        Returns None if LLM is unavailable or fails (graceful fallback
        to raw truncation in format_episode).
        """
        if not self._router:
            return None

        try:
            prompt = (
                "Summarize this exchange in ONE concise sentence "
                "(max 30 words):\n"
                f"User: {user_input[:200]}\n"
                f"Assistant: {assistant_response[:200]}"
            )
            resp = await self._router.generate(
                messages=[{"role": "user", "content": prompt}],
                model=self._fast_model or None,
                temperature=0.1,
                max_tokens=64,
            )
            summary = resp.content.strip()
            # Remove wrapping quotes if present
            if summary.startswith('"') and summary.endswith('"'):
                summary = summary[1:-1]
            if len(summary) > 200:  # noqa: PLR2004
                summary = summary[:197] + "..."
            return summary if summary else None
        except Exception:
            logger.debug("summary_generation_failed", exc_info=True)
            return None

    @staticmethod
    def _extract_with_regex(message: str) -> list[ExtractedConcept]:
        """Fallback: extract concepts using regex patterns.

        Covers all 7 categories with pattern-based extraction.
        Less accurate than LLM but works offline.
        Includes heuristic sentiment estimation.
        """
        extracted: list[ExtractedConcept] = []

        for pattern in _ENTITY_PATTERNS:
            match = pattern.search(message)
            if match:
                name = match.group(1).strip()
                if len(name) > 1:
                    extracted.append(
                        ExtractedConcept(name, f"User's name is {name}", "entity", 0.0)
                    )

        for pattern in _PREFERENCE_PATTERNS:
            match = pattern.search(message)
            if match:
                pref = match.group(1).strip()
                if len(pref) > 1:
                    sentiment = _estimate_sentiment(message)
                    extracted.append(
                        ExtractedConcept(
                            pref,
                            f"User prefers {pref}",
                            "preference",
                            sentiment,
                        )
                    )

        for pattern in _FACT_PATTERNS:
            match = pattern.search(message)
            if match:
                fact = match.group(1).strip()
                if len(fact) > 1:
                    extracted.append(ExtractedConcept(fact, f"User {fact}", "fact", 0.0))

        for pattern in _SKILL_PATTERNS:
            match = pattern.search(message)
            if match:
                skill = match.group(1).strip()
                if len(skill) > 1:
                    extracted.append(
                        ExtractedConcept(
                            skill,
                            f"User codes with {skill}",
                            "skill",
                            0.0,
                        )
                    )

        for pattern in _BELIEF_PATTERNS:
            match = pattern.search(message)
            if match:
                belief = match.group(1).strip()
                if len(belief) > 1:
                    sentiment = _estimate_sentiment(message)
                    extracted.append(
                        ExtractedConcept(
                            belief,
                            f"User believes {belief}",
                            "belief",
                            sentiment,
                        )
                    )

        for pattern in _EVENT_PATTERNS:
            match = pattern.search(message)
            if match:
                event = match.group(1).strip()
                if len(event) > 1:
                    extracted.append(ExtractedConcept(event, f"User {event}", "event", 0.0))

        for pattern in _RELATIONSHIP_PATTERNS:
            match = pattern.search(message)
            if match:
                rel = match.group(1).strip()
                if len(rel) > 1:
                    extracted.append(
                        ExtractedConcept(
                            rel,
                            f"User's relationship: {rel}",
                            "relationship",
                            0.0,
                        )
                    )

        return extracted
