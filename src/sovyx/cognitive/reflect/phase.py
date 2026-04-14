"""Auto-extracted from cognitive/reflect.py — see __init__.py for the public re-exports."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from sovyx.cognitive.reflect._fallback import (
    _BELIEF_PATTERNS,
    _ENTITY_PATTERNS,
    _EVENT_PATTERNS,
    _FACT_PATTERNS,
    _PREFERENCE_PATTERNS,
    _RELATIONSHIP_PATTERNS,
    _SKILL_PATTERNS,
    _estimate_sentiment,
)
from sovyx.cognitive.reflect._models import ExtractedConcept
from sovyx.cognitive.reflect._prompts import (
    _EXTRACTION_PROMPT,
    _RELATION_PROMPT,
    _VALID_RELATIONS,
)
from sovyx.cognitive.reflect._scoring import (
    clamp_sentiment,
    compute_episode_importance,
    detect_explicit_importance,
    get_importance,
    get_source_confidence,
    resolve_category,
)
from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from sovyx.brain.service import BrainService
    from sovyx.cognitive.perceive import Perception
    from sovyx.engine.types import ConceptId, ConversationId, MindId
    from sovyx.llm.models import LLMResponse
    from sovyx.llm.router import LLMRouter

logger = get_logger(__name__)


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

        concept_importances: list[float] = []

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
                concept_importances.append(combined_importance)
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

        # Dynamic episode importance based on message + concept scores
        episode_importance = compute_episode_importance(
            message=perception.content,
            num_concepts=len(concept_ids),
            max_valence=episode_arousal,
            concept_importances=concept_importances or None,
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
