"""Sovyx ReflectPhase — encode episode + extract concepts + Hebbian learning.

Uses LLM-based concept extraction for rich, accurate knowledge capture.
Falls back to regex-based extraction if LLM is unavailable.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from sovyx.brain.service import BrainService
    from sovyx.cognitive.perceive import Perception
    from sovyx.engine.types import ConceptId, ConversationId, MindId
    from sovyx.llm.models import LLMResponse
    from sovyx.llm.router import LLMRouter

logger = get_logger(__name__)

# ── LLM extraction prompt ──────────────────────────────────────────────
# Covers all 7 ConceptCategory values with clear definitions and examples
# so the LLM can reliably distinguish between them.

_EXTRACTION_PROMPT = (  # noqa: E501
    "Extract knowledge from the user message into structured concepts.\n"
    "Return a JSON array of objects with these fields:\n"
    '- "name": short label (2-5 words)\n'
    '- "content": one-sentence description of what was learned\n'
    '- "category": one of the categories below\n'
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
# Initial importance assigned at concept creation.
# Higher = more likely to survive Ebbinghaus decay.

_IMPORTANCE: dict[str, float] = {
    "entity": 0.8,
    "fact": 0.6,
    "preference": 0.7,
    "skill": 0.7,
    "belief": 0.6,
    "event": 0.7,
    "relationship": 0.8,
}

# Default importance for unknown categories
_DEFAULT_IMPORTANCE = 0.5

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
        r"(?:eu (?:gosto|adoro|prefiro|curto|uso))\s+(?:de |do |da )?(.+?)(?:\.|,|!|$)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:my (?:stack|tools?|setup) (?:is|includes?|:))\s*(.+?)(?:\.|!|$)",
        re.IGNORECASE,
    ),
]

_FACT_PATTERNS = [
    re.compile(
        r"(?:i (?:work at|work for|live in|study at|am building|built))\s+(.+?)(?:\.|,|!|$)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:i'm (?:building|developing|working on|learning))\s+(.+?)(?:\.|,|!|$)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:(?:trabalho|moro|estudo) (?:na|no|em|na))\s+(.+?)(?:\.|,|!|$)",
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
        r"(?:i (?:started|finished|completed|launched|migrated|deployed|graduated))"
        r"\s+(.+?)(?:\.|,|!|$)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:last (?:week|month|year)|recently|yesterday|in \d{4})\s*[,:]?\s*"
        r"(?:i |we )?(.+?)(?:\.|,|!|$)",
        re.IGNORECASE,
    ),
]

_RELATIONSHIP_PATTERNS = [
    re.compile(
        r"(?:i (?:manage|lead|report to|work with|mentor))\s+(.+?)(?:\.|,|!|$)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:my (?:team|manager|boss|colleague|partner) (?:is|are))\s+(.+?)(?:\.|,|!|$)",
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


class ReflectPhase:
    """After response: encode episode + extract concepts + Hebbian.

    1. Create episode with user_input + assistant_response
    2. Extract concepts via LLM (falls back to regex if unavailable)
    3. Hebbian learning between co-mentioned concepts
    4. Update working memory
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
        extracted: list[tuple[str, str, str]] = []

        if self._router:
            llm_extracted = await self._extract_with_llm(perception.content)
            if llm_extracted is not None:
                extracted = llm_extracted

        if not extracted:
            extracted = self._extract_with_regex(perception.content)

        # Learn extracted concepts
        concept_ids: list[ConceptId] = []
        for name, content, cat_key in extracted:
            try:
                resolved = resolve_category(cat_key)
                category = ConceptCategory(resolved)
                cid = await self._brain.learn_concept(
                    mind_id=mind_id,
                    name=name,
                    content=content,
                    category=category,
                    source="conversation",
                )
                concept_ids.append(cid)
            except Exception:
                logger.warning(
                    "concept_extraction_failed",
                    name=name,
                    exc_info=True,
                )

        # Hebbian learning between co-mentioned concepts
        if len(concept_ids) >= 2:  # noqa: PLR2004
            try:
                await self._brain.strengthen_connection(concept_ids)
            except Exception:
                logger.warning("hebbian_failed", exc_info=True)

        # Encode episode — pass new concept IDs as star topology hubs
        # (each connects to top-K existing by activation)
        try:
            await self._brain.encode_episode(
                mind_id=mind_id,
                conversation_id=conversation_id,
                user_input=perception.content,
                assistant_response=response.content,
                importance=0.5,
                new_concept_ids=concept_ids or None,
            )
        except Exception:
            logger.warning("episode_encoding_failed", exc_info=True)

        logger.debug(
            "reflect_complete",
            concepts_extracted=len(extracted),
            concepts_learned=len(concept_ids),
        )

    async def _extract_with_llm(self, message: str) -> list[tuple[str, str, str]] | None:
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

            result: list[tuple[str, str, str]] = []
            for item in concepts_raw:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name", "")).strip()
                content = str(item.get("content", "")).strip()
                category = str(item.get("category", "fact")).strip().lower()
                if name and content and len(name) > 1:
                    result.append((name, content, category))

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

    @staticmethod
    def _extract_with_regex(message: str) -> list[tuple[str, str, str]]:
        """Fallback: extract concepts using regex patterns.

        Covers all 7 categories with pattern-based extraction.
        Less accurate than LLM but works offline.
        """
        extracted: list[tuple[str, str, str]] = []

        for pattern in _ENTITY_PATTERNS:
            match = pattern.search(message)
            if match:
                name = match.group(1).strip()
                if len(name) > 1:
                    extracted.append((name, f"User's name is {name}", "entity"))

        for pattern in _PREFERENCE_PATTERNS:
            match = pattern.search(message)
            if match:
                pref = match.group(1).strip()
                if len(pref) > 1:
                    extracted.append((pref, f"User prefers {pref}", "preference"))

        for pattern in _FACT_PATTERNS:
            match = pattern.search(message)
            if match:
                fact = match.group(1).strip()
                if len(fact) > 1:
                    extracted.append((fact, f"User {fact}", "fact"))

        for pattern in _SKILL_PATTERNS:
            match = pattern.search(message)
            if match:
                skill = match.group(1).strip()
                if len(skill) > 1:
                    extracted.append((skill, f"User codes with {skill}", "skill"))

        for pattern in _BELIEF_PATTERNS:
            match = pattern.search(message)
            if match:
                belief = match.group(1).strip()
                if len(belief) > 1:
                    extracted.append((belief, f"User believes {belief}", "belief"))

        for pattern in _EVENT_PATTERNS:
            match = pattern.search(message)
            if match:
                event = match.group(1).strip()
                if len(event) > 1:
                    extracted.append((event, f"User {event}", "event"))

        for pattern in _RELATIONSHIP_PATTERNS:
            match = pattern.search(message)
            if match:
                rel = match.group(1).strip()
                if len(rel) > 1:
                    extracted.append((rel, f"User's relationship: {rel}", "relationship"))

        return extracted
