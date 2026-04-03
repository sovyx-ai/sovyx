"""Sovyx ReflectPhase — encode episode + extract concepts + Hebbian learning."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from sovyx.brain.service import BrainService
    from sovyx.cognitive.perceive import Perception
    from sovyx.engine.types import ConceptId, ConversationId, MindId
    from sovyx.llm.models import LLMResponse

logger = get_logger(__name__)

# Concept extraction patterns (EN + PT, case-insensitive)
_ENTITY_PATTERNS = [
    re.compile(r"(?:my name is|i'm|i am)\s+(\w+)", re.IGNORECASE),
    re.compile(r"(?:meu nome é|me chamo|sou o|sou a)\s+(\w+)", re.IGNORECASE),
]

_PREFERENCE_PATTERNS = [
    re.compile(r"(?:i (?:like|love|prefer|enjoy))\s+(.+?)(?:\.|,|!|$)", re.IGNORECASE),
    re.compile(
        r"(?:eu (?:gosto|adoro|prefiro|curto))\s+(?:de |do |da )?(.+?)(?:\.|,|!|$)",
        re.IGNORECASE,
    ),
]

_FACT_PATTERNS = [
    re.compile(r"(?:i (?:work at|work for|live in|study at))\s+(.+?)(?:\.|,|!|$)", re.IGNORECASE),
    re.compile(r"(?:(?:trabalho|moro|estudo) (?:na|no|em|na))\s+(.+?)(?:\.|,|!|$)", re.IGNORECASE),
]

# Importance by category
_IMPORTANCE = {
    "entity": 0.8,
    "preference": 0.7,
    "fact": 0.6,
}


class ReflectPhase:
    """After response: encode episode + extract concepts + Hebbian.

    1. Create episode with user_input + assistant_response
    2. Extract concepts via heuristic patterns (EN + PT)
    3. Hebbian learning between co-mentioned concepts
    4. Update working memory
    """

    def __init__(self, brain_service: BrainService) -> None:
        self._brain = brain_service

    async def process(
        self,
        perception: Perception,
        response: LLMResponse,
        mind_id: MindId,
        conversation_id: ConversationId,
    ) -> None:
        """Reflect on the interaction: learn concepts + encode episode."""
        from sovyx.engine.types import ConceptCategory

        # Extract concepts from user input
        extracted: list[tuple[str, str, str]] = []  # (name, content, category)

        for pattern in _ENTITY_PATTERNS:
            match = pattern.search(perception.content)
            if match:
                name = match.group(1).strip()
                if len(name) > 1:
                    extracted.append((name, f"User's name is {name}", "entity"))

        for pattern in _PREFERENCE_PATTERNS:
            match = pattern.search(perception.content)
            if match:
                pref = match.group(1).strip()
                if len(pref) > 1:
                    extracted.append((pref, f"User likes {pref}", "preference"))

        for pattern in _FACT_PATTERNS:
            match = pattern.search(perception.content)
            if match:
                fact = match.group(1).strip()
                if len(fact) > 1:
                    extracted.append((fact, f"User {fact}", "fact"))

        # Learn extracted concepts
        category_map = {
            "entity": ConceptCategory.ENTITY,
            "preference": ConceptCategory.PREFERENCE,
            "fact": ConceptCategory.FACT,
        }

        concept_ids: list[ConceptId] = []
        for name, content, cat_key in extracted:
            try:
                cid = await self._brain.learn_concept(
                    mind_id=mind_id,
                    name=name,
                    content=content,
                    category=category_map[cat_key],
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

        # Encode episode
        try:
            await self._brain.encode_episode(
                mind_id=mind_id,
                conversation_id=conversation_id,
                user_input=perception.content,
                assistant_response=response.content,
                importance=0.5,
            )
        except Exception:
            logger.warning("episode_encoding_failed", exc_info=True)

        logger.debug(
            "reflect_complete",
            concepts_extracted=len(extracted),
            concepts_learned=len(concept_ids),
        )
