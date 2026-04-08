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

# ── LLM extraction prompt ──

_EXTRACTION_PROMPT = """Extract key facts, preferences, and entities from the user message.
Return a JSON array of objects with these fields:
- "name": short label (2-5 words)
- "content": one-sentence description of what was learned
- "category": one of "entity", "preference", "fact", "skill", "opinion", "project"

Rules:
- Extract ALL meaningful information (names, tools, preferences, opinions, projects, skills)
- Be specific: "prefers raw SQL over ORMs" not "likes databases"
- Skip greetings, filler words, questions asking for info
- Return [] if the message contains no learnable information
- Return ONLY the JSON array, no other text

User message: {message}"""

# ── Regex fallback patterns ──

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

_IMPORTANCE = {
    "entity": 0.8,
    "preference": 0.7,
    "fact": 0.6,
    "skill": 0.7,
    "opinion": 0.6,
    "project": 0.8,
}


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
        category_map = {
            "entity": ConceptCategory.ENTITY,
            "preference": ConceptCategory.PREFERENCE,
            "fact": ConceptCategory.FACT,
            "skill": ConceptCategory.FACT,
            "opinion": ConceptCategory.PREFERENCE,
            "project": ConceptCategory.ENTITY,
        }

        concept_ids: list[ConceptId] = []
        for name, content, cat_key in extracted:
            try:
                category = category_map.get(cat_key, ConceptCategory.FACT)
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

        # Encode episode — pass new concept IDs so Hebbian cap
        # always includes them (prevents isolated island formation)
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
        """Fallback: extract concepts using regex patterns."""
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

        return extracted
