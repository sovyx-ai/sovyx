"""Tests for sovyx.cognitive.reflect — ReflectPhase.

Covers:
- LLM-based concept extraction (mocked) with sentiment
- Regex fallback extraction for all 7 categories
- Category mapping (canonical + aliases)
- Importance assignment
- Sentiment extraction and clamping
- Episode emotional valence/arousal computation
- Episode encoding
- Hebbian learning trigger
- Failure resilience
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
from hypothesis import given
from hypothesis import strategies as st

from sovyx.cognitive.perceive import Perception
from sovyx.cognitive.reflect import (
    _CATEGORY_MAP,
    ExtractedConcept,
    ReflectPhase,
    _estimate_sentiment,
    clamp_sentiment,
    compute_episode_importance,
    detect_explicit_importance,
    get_importance,
    resolve_category,
)
from sovyx.engine.types import (
    ConceptCategory,
    ConceptId,
    ConversationId,
    MindId,
    PerceptionType,
)
from sovyx.llm.models import LLMResponse

MIND = MindId("aria")
CONV = ConversationId("conv1")


def _perception(content: str) -> Perception:
    return Perception(
        id="p1",
        type=PerceptionType.USER_MESSAGE,
        source="telegram",
        content=content,
    )


def _response(content: str = "OK") -> LLMResponse:
    return LLMResponse(
        content=content,
        model="test",
        tokens_in=10,
        tokens_out=5,
        latency_ms=100,
        cost_usd=0.0,
        finish_reason="stop",
        provider="test",
    )


def _mock_llm_response(concepts: list[dict[str, object]]) -> AsyncMock:
    """Create a mock LLM router that returns concepts as JSON."""
    router = AsyncMock()
    router.generate = AsyncMock(
        return_value=LLMResponse(
            content=json.dumps(concepts),
            model="gpt-4o-mini",
            tokens_in=100,
            tokens_out=50,
            latency_ms=200,
            cost_usd=0.0001,
            finish_reason="stop",
            provider="openai",
        )
    )
    return router


@pytest.fixture
def mock_brain() -> AsyncMock:
    brain = AsyncMock()
    brain.learn_concept = AsyncMock(return_value=ConceptId("c1"))
    brain.encode_episode = AsyncMock()
    brain.strengthen_connection = AsyncMock()
    # Default novelty: moderate (0.50) — override in specific tests
    brain.compute_novelty = AsyncMock(return_value=0.50)
    return brain


# ── Category mapping tests ─────────────────────────────────────────────


class TestCategoryMapping:
    """Every ConceptCategory value must be reachable via _CATEGORY_MAP."""

    def test_all_categories_covered(self) -> None:
        mapped_values = set(_CATEGORY_MAP.values())
        for cat in ConceptCategory:
            assert cat.value in mapped_values, (
                f"ConceptCategory.{cat.name} ({cat.value}) has no key in _CATEGORY_MAP"
            )

    def test_direct_mappings(self) -> None:
        for cat in ConceptCategory:
            assert resolve_category(cat.value) == cat.value

    def test_alias_opinion_maps_to_belief(self) -> None:
        assert resolve_category("opinion") == "belief"

    def test_alias_project_maps_to_entity(self) -> None:
        assert resolve_category("project") == "entity"

    def test_alias_person_maps_to_entity(self) -> None:
        assert resolve_category("person") == "entity"

    def test_alias_tool_maps_to_skill(self) -> None:
        assert resolve_category("tool") == "skill"

    def test_alias_technology_maps_to_skill(self) -> None:
        assert resolve_category("technology") == "skill"

    def test_alias_milestone_maps_to_event(self) -> None:
        assert resolve_category("milestone") == "event"

    def test_alias_connection_maps_to_relationship(self) -> None:
        assert resolve_category("connection") == "relationship"

    def test_unknown_category_defaults_to_fact(self) -> None:
        assert resolve_category("xyzzy") == "fact"
        assert resolve_category("") == "fact"

    def test_case_insensitive(self) -> None:
        assert resolve_category("ENTITY") == "entity"
        assert resolve_category("Belief") == "belief"
        assert resolve_category("SKILL") == "skill"

    def test_whitespace_stripped(self) -> None:
        assert resolve_category("  skill  ") == "skill"
        assert resolve_category("\tbelief\n") == "belief"

    @given(st.text(min_size=0, max_size=50))
    def test_resolve_never_crashes(self, raw: str) -> None:
        result = resolve_category(raw)
        assert isinstance(result, str)
        assert len(result) > 0


class TestImportance:
    """Importance assignment by category."""

    def test_all_categories_have_importance(self) -> None:
        for cat in ConceptCategory:
            imp = get_importance(cat.value)
            assert 0.0 < imp <= 1.0, f"{cat.value} importance={imp}"

    def test_unknown_category_gets_default(self) -> None:
        assert get_importance("unknown") == 0.5

    def test_entity_highest_importance(self) -> None:
        assert get_importance("entity") >= get_importance("fact")

    def test_relationship_highest_importance(self) -> None:
        assert get_importance("relationship") >= get_importance("fact")


# ── Sentiment tests ────────────────────────────────────────────────────


class TestSourceConfidence:
    """Source confidence mapping."""

    def test_llm_explicit_highest(self) -> None:
        from sovyx.cognitive.reflect import get_source_confidence

        conf = get_source_confidence("llm_explicit")
        assert 0.80 <= conf <= 0.90

    def test_regex_fallback_lower(self) -> None:
        from sovyx.cognitive.reflect import get_source_confidence

        llm_conf = get_source_confidence("llm_explicit")
        regex_conf = get_source_confidence("regex_fallback")
        assert regex_conf < llm_conf

    def test_unknown_source_gets_default(self) -> None:
        from sovyx.cognitive.reflect import get_source_confidence

        conf = get_source_confidence("unknown_source")
        assert 0.40 <= conf <= 0.60

    def test_all_sources_in_range(self) -> None:
        from sovyx.cognitive.reflect import get_source_confidence

        sources = ("llm_explicit", "llm_inferred", "regex_fallback", "system", "corroboration")
        for source in sources:
            conf = get_source_confidence(source)
            assert 0.0 <= conf <= 1.0, f"{source} confidence={conf}"

    def test_system_highest_confidence(self) -> None:
        from sovyx.cognitive.reflect import get_source_confidence

        system_conf = get_source_confidence("system")
        llm_conf = get_source_confidence("llm_explicit")
        assert system_conf >= llm_conf


class TestNoveltyDetection:
    """Novelty-based importance modulation (TASK-04 + refinement TASK-01).

    Tests use brain.compute_novelty() which encapsulates the 3-tier
    strategy: embedding cosine → FTS5 → cold start.
    """

    async def test_high_novelty_new_concept(self, mock_brain: AsyncMock) -> None:
        """New concept via compute_novelty → high novelty."""
        mock_brain.compute_novelty = AsyncMock(return_value=0.95)
        phase = ReflectPhase(mock_brain)
        from sovyx.cognitive.reflect import ExtractedConcept

        ec = ExtractedConcept(name="quantum physics", content="QM basics", category="fact")
        novelty = await phase._compute_novelty_batch([ec], MIND)
        assert novelty["quantum physics"] == pytest.approx(0.95, abs=0.01)

    async def test_low_novelty_known_concept(self, mock_brain: AsyncMock) -> None:
        """Known concept → low novelty."""
        mock_brain.compute_novelty = AsyncMock(return_value=0.05)
        phase = ReflectPhase(mock_brain)
        from sovyx.cognitive.reflect import ExtractedConcept

        ec = ExtractedConcept(name="quantum physics", content="QM basics", category="fact")
        novelty = await phase._compute_novelty_batch([ec], MIND)
        assert novelty["quantum physics"] == pytest.approx(0.05)

    async def test_cold_start_novelty(self, mock_brain: AsyncMock) -> None:
        """Cold start (few concepts in category) → 0.70."""
        mock_brain.compute_novelty = AsyncMock(return_value=0.70)
        phase = ReflectPhase(mock_brain)
        from sovyx.cognitive.reflect import ExtractedConcept

        ec = ExtractedConcept(name="new topic", content="something", category="fact")
        novelty = await phase._compute_novelty_batch([ec], MIND)
        assert novelty["new topic"] == pytest.approx(0.70)

    async def test_novelty_error_defaults_moderate(self, mock_brain: AsyncMock) -> None:
        """On error, novelty defaults to 0.5."""
        mock_brain.compute_novelty = AsyncMock(side_effect=RuntimeError("db error"))
        phase = ReflectPhase(mock_brain)
        from sovyx.cognitive.reflect import ExtractedConcept

        ec = ExtractedConcept(name="test", content="test", category="fact")
        novelty = await phase._compute_novelty_batch([ec], MIND)
        assert novelty["test"] == pytest.approx(0.5)

    async def test_novelty_affects_llm_importance(self, mock_brain: AsyncMock) -> None:
        """High novelty boosts LLM-path importance."""
        mock_brain.compute_novelty = AsyncMock(return_value=0.95)

        router = _mock_llm_response(
            [
                {
                    "name": "wormholes",
                    "content": "Space tunnels",
                    "category": "fact",
                    "sentiment": 0.0,
                    "importance": 0.7,
                    "confidence": 0.8,
                    "explicit": False,
                    "source_quality": "explicit",
                }
            ]
        )

        phase = ReflectPhase(mock_brain, router, "fast")
        await phase.process(_perception("Tell me about wormholes"), _response(), MIND, CONV)
        kw = mock_brain.learn_concept.call_args.kwargs
        # 0.35*0.7 + 0.15*0.50 + 0.10*0 + 0.15*0.95 + 0.25*0 ≈ 0.46
        assert kw["importance"] > 0.40  # noqa: PLR2004

    async def test_novelty_affects_regex_importance(self, mock_brain: AsyncMock) -> None:
        """Known concept via regex → lower importance from low novelty."""
        mock_brain.compute_novelty = AsyncMock(return_value=0.05)

        phase = ReflectPhase(mock_brain)
        await phase.process(_perception("My name is Guipe"), _response(), MIND, CONV)
        kw = mock_brain.learn_concept.call_args.kwargs
        # 0.60*0.80 + 0.40*0.05 = 0.50
        assert kw["importance"] == pytest.approx(0.50, abs=0.02)


class TestExplicitImportanceDetection:
    """Message-level explicit importance signal detection."""

    # English patterns
    def test_remember_this(self) -> None:
        assert detect_explicit_importance("Please remember this fact") is True

    def test_dont_forget(self) -> None:
        assert detect_explicit_importance("Don't forget about the meeting") is True

    def test_keep_in_mind(self) -> None:
        assert detect_explicit_importance("Keep in mind that I'm allergic") is True

    def test_this_is_important(self) -> None:
        assert detect_explicit_importance("This is important information") is True

    def test_this_is_very_important(self) -> None:
        assert detect_explicit_importance("This is very important") is True

    def test_note_this(self) -> None:
        assert detect_explicit_importance("Note this: my address changed") is True

    def test_never_forget(self) -> None:
        assert detect_explicit_importance("Never forget my birthday") is True

    # Portuguese patterns
    def test_lembra_disso(self) -> None:
        assert detect_explicit_importance("Lembra disso pra mim") is True

    def test_nao_esquece(self) -> None:
        assert detect_explicit_importance("Não esquece esse detalhe") is True

    def test_anota_isso(self) -> None:
        assert detect_explicit_importance("Anota isso aí") is True

    def test_importante_pt(self) -> None:
        assert detect_explicit_importance("Isso é importante") is True

    def test_memoriza(self) -> None:
        assert detect_explicit_importance("Memoriza esse número") is True

    def test_grava_isso(self) -> None:
        assert detect_explicit_importance("Grava isso aí") is True

    # Negative cases
    def test_normal_message_no_signal(self) -> None:
        assert detect_explicit_importance("I work at Google") is False

    def test_question_no_signal(self) -> None:
        assert detect_explicit_importance("What time is it?") is False

    def test_greeting_no_signal(self) -> None:
        assert detect_explicit_importance("Hi, how are you?") is False

    def test_empty_string(self) -> None:
        assert detect_explicit_importance("") is False

    # Integration: message-level overrides concept-level
    async def test_message_explicit_overrides_concept(self, mock_brain: AsyncMock) -> None:
        """Message-level 'remember this' boosts ALL concepts."""
        concepts = [
            {
                "name": "Coffee",
                "content": "likes coffee",
                "category": "preference",
                "importance": 0.3,
                "explicit": False,
            },
        ]
        router = _mock_llm_response(concepts)
        phase = ReflectPhase(mock_brain, llm_router=router)
        await phase.process(
            _perception("Remember this: I like coffee"),
            _response(),
            MIND,
            CONV,
        )
        kw = mock_brain.learn_concept.call_args.kwargs
        # Message-level explicit → importance floor 0.85
        assert kw["importance"] >= 0.85

    async def test_message_explicit_boosts_confidence(self, mock_brain: AsyncMock) -> None:
        """Message-level explicit also boosts confidence floor to 0.75."""
        concepts = [
            {
                "name": "Allergy",
                "content": "allergic to peanuts",
                "category": "fact",
                "confidence": 0.5,
            },
        ]
        router = _mock_llm_response(concepts)
        phase = ReflectPhase(mock_brain, llm_router=router)
        await phase.process(
            _perception("This is important: I'm allergic to peanuts"),
            _response(),
            MIND,
            CONV,
        )
        kw = mock_brain.learn_concept.call_args.kwargs
        assert kw["confidence"] >= 0.75

    async def test_regex_path_explicit_boost(self, mock_brain: AsyncMock) -> None:
        """Explicit signal works on regex path too."""
        phase = ReflectPhase(mock_brain)
        await phase.process(
            _perception("Remember this: my name is Alex"),
            _response(),
            MIND,
            CONV,
        )
        kw = mock_brain.learn_concept.call_args.kwargs
        assert kw["importance"] >= 0.85
        assert kw["confidence"] >= 0.75


class TestSentiment:
    """Sentiment extraction and clamping."""

    def test_clamp_in_range(self) -> None:
        assert clamp_sentiment(0.5) == 0.5
        assert clamp_sentiment(-0.5) == -0.5

    def test_clamp_above_max(self) -> None:
        assert clamp_sentiment(2.0) == 1.0

    def test_clamp_below_min(self) -> None:
        assert clamp_sentiment(-2.0) == -1.0

    @given(st.floats(allow_nan=False, allow_infinity=False))
    def test_clamp_always_bounded(self, v: float) -> None:
        result = clamp_sentiment(v)
        assert -1.0 <= result <= 1.0

    def test_estimate_positive(self) -> None:
        assert _estimate_sentiment("I love this great tool") > 0.0

    def test_estimate_negative(self) -> None:
        assert _estimate_sentiment("I hate this terrible thing") < 0.0

    def test_estimate_neutral(self) -> None:
        assert _estimate_sentiment("I work at Google") == 0.0

    def test_extracted_concept_default_sentiment(self) -> None:
        ec = ExtractedConcept(name="Test", content="test", category="fact")
        assert ec.sentiment == 0.0

    def test_extracted_concept_with_sentiment(self) -> None:
        ec = ExtractedConcept(
            name="Python",
            content="loves Python",
            category="preference",
            sentiment=0.8,
        )
        assert ec.sentiment == 0.8


# ── Regex fallback extraction ──────────────────────────────────────────


class TestRegexExtraction:
    """Regex fallback covers all 7 categories."""

    async def test_entity_english(self, mock_brain: AsyncMock) -> None:
        phase = ReflectPhase(mock_brain)
        await phase.process(_perception("My name is Guipe"), _response(), MIND, CONV)
        mock_brain.learn_concept.assert_called_once()
        kw = mock_brain.learn_concept.call_args.kwargs
        assert kw["name"] == "Guipe"
        assert kw["category"] == ConceptCategory.ENTITY

    async def test_entity_portuguese(self, mock_brain: AsyncMock) -> None:
        phase = ReflectPhase(mock_brain)
        await phase.process(_perception("Meu nome é Renan"), _response(), MIND, CONV)
        mock_brain.learn_concept.assert_called_once()
        kw = mock_brain.learn_concept.call_args.kwargs
        assert kw["name"] == "Renan"
        assert kw["category"] == ConceptCategory.ENTITY

    async def test_preference_english(self, mock_brain: AsyncMock) -> None:
        phase = ReflectPhase(mock_brain)
        await phase.process(_perception("I love coffee"), _response(), MIND, CONV)
        mock_brain.learn_concept.assert_called_once()
        kw = mock_brain.learn_concept.call_args.kwargs
        assert kw["category"] == ConceptCategory.PREFERENCE

    async def test_preference_has_positive_sentiment(self, mock_brain: AsyncMock) -> None:
        """Regex preference extraction includes sentiment."""
        phase = ReflectPhase(mock_brain)
        await phase.process(_perception("I love great coffee"), _response(), MIND, CONV)
        kw = mock_brain.learn_concept.call_args.kwargs
        assert kw["emotional_valence"] > 0.0

    async def test_preference_portuguese(self, mock_brain: AsyncMock) -> None:
        phase = ReflectPhase(mock_brain)
        await phase.process(_perception("Eu gosto de café"), _response(), MIND, CONV)
        mock_brain.learn_concept.assert_called_once()

    async def test_fact_english(self, mock_brain: AsyncMock) -> None:
        phase = ReflectPhase(mock_brain)
        await phase.process(_perception("I work at Google"), _response(), MIND, CONV)
        mock_brain.learn_concept.assert_called_once()
        kw = mock_brain.learn_concept.call_args.kwargs
        assert kw["category"] == ConceptCategory.FACT

    async def test_skill_english(self, mock_brain: AsyncMock) -> None:
        phase = ReflectPhase(mock_brain)
        await phase.process(_perception("I code in Rust"), _response(), MIND, CONV)
        mock_brain.learn_concept.assert_called_once()
        kw = mock_brain.learn_concept.call_args.kwargs
        assert kw["category"] == ConceptCategory.SKILL

    async def test_belief_english(self, mock_brain: AsyncMock) -> None:
        phase = ReflectPhase(mock_brain)
        await phase.process(_perception("I think ORMs are harmful"), _response(), MIND, CONV)
        mock_brain.learn_concept.assert_called_once()
        kw = mock_brain.learn_concept.call_args.kwargs
        assert kw["category"] == ConceptCategory.BELIEF

    async def test_belief_portuguese(self, mock_brain: AsyncMock) -> None:
        phase = ReflectPhase(mock_brain)
        await phase.process(
            _perception("Eu acho que microservices são overrated"),
            _response(),
            MIND,
            CONV,
        )
        mock_brain.learn_concept.assert_called_once()
        kw = mock_brain.learn_concept.call_args.kwargs
        assert kw["category"] == ConceptCategory.BELIEF

    async def test_event_english(self, mock_brain: AsyncMock) -> None:
        phase = ReflectPhase(mock_brain)
        await phase.process(
            _perception("I migrated to AWS last month"),
            _response(),
            MIND,
            CONV,
        )
        mock_brain.learn_concept.assert_called_once()
        kw = mock_brain.learn_concept.call_args.kwargs
        assert kw["category"] == ConceptCategory.EVENT

    async def test_relationship_english(self, mock_brain: AsyncMock) -> None:
        phase = ReflectPhase(mock_brain)
        await phase.process(
            _perception("I manage a team of 5 engineers"),
            _response(),
            MIND,
            CONV,
        )
        mock_brain.learn_concept.assert_called_once()
        kw = mock_brain.learn_concept.call_args.kwargs
        assert kw["category"] == ConceptCategory.RELATIONSHIP

    async def test_regex_passes_category_importance(self, mock_brain: AsyncMock) -> None:
        """Regex extraction passes category × novelty importance to learn_concept."""
        # compute_novelty returns high novelty (new concept)
        mock_brain.compute_novelty = AsyncMock(return_value=0.95)
        phase = ReflectPhase(mock_brain)
        await phase.process(_perception("My name is Guipe"), _response(), MIND, CONV)
        kw = mock_brain.learn_concept.call_args.kwargs
        # Entity category=0.80 → 0.60*0.80 + 0.40*0.95 = 0.86
        assert kw["importance"] == pytest.approx(0.86, abs=0.02)

    async def test_regex_passes_source_confidence(self, mock_brain: AsyncMock) -> None:
        """Regex extraction passes regex_fallback confidence to learn_concept."""
        phase = ReflectPhase(mock_brain)
        await phase.process(_perception("My name is Guipe"), _response(), MIND, CONV)
        kw = mock_brain.learn_concept.call_args.kwargs
        # regex_fallback → midpoint of (0.30, 0.55) = 0.425
        assert kw["confidence"] == pytest.approx(0.425, abs=0.01)

    async def test_no_concepts_extracted(self, mock_brain: AsyncMock) -> None:
        phase = ReflectPhase(mock_brain)
        await phase.process(_perception("What time is it?"), _response(), MIND, CONV)
        mock_brain.learn_concept.assert_not_called()


# ── LLM-based extraction ──────────────────────────────────────────────


class TestLLMExtraction:
    """LLM-based concept extraction with mocked router."""

    async def test_llm_combines_importance_signals(self, mock_brain: AsyncMock) -> None:
        """LLM extraction combines LLM assessment + category baseline."""
        concepts = [
            {
                "name": "John",
                "content": "User name is John",
                "category": "entity",
                "sentiment": 0.0,
                "importance": 0.9,
                "confidence": 0.95,
                "source_quality": "explicit",
            },
        ]
        router = _mock_llm_response(concepts)
        phase = ReflectPhase(mock_brain, llm_router=router)
        await phase.process(_perception("test"), _response(), MIND, CONV)
        kw = mock_brain.learn_concept.call_args.kwargs
        # Combined: 0.40*0.9 + 0.25*0.80 + 0.10*0 + 0.25*0 = 0.56
        assert kw["importance"] > 0.5
        assert kw["importance"] < 0.7

    async def test_llm_combines_confidence_signals(self, mock_brain: AsyncMock) -> None:
        """LLM extraction combines LLM confidence + source quality."""
        concepts = [
            {
                "name": "Test",
                "content": "test content for length",
                "category": "fact",
                "sentiment": 0.0,
                "confidence": 0.9,
                "source_quality": "explicit",
            },
        ]
        router = _mock_llm_response(concepts)
        phase = ReflectPhase(mock_brain, llm_router=router)
        await phase.process(_perception("test"), _response(), MIND, CONV)
        kw = mock_brain.learn_concept.call_args.kwargs
        # High LLM confidence + explicit source → high combined
        assert kw["confidence"] > 0.7

    async def test_llm_explicit_signal_raises_importance_floor(
        self, mock_brain: AsyncMock
    ) -> None:
        """explicit=true from LLM → importance floor at 0.85."""
        concepts = [
            {
                "name": "Remember",
                "content": "important thing",
                "category": "fact",
                "sentiment": 0.0,
                "importance": 0.3,
                "explicit": True,
            },
        ]
        router = _mock_llm_response(concepts)
        phase = ReflectPhase(mock_brain, llm_router=router)
        await phase.process(_perception("test"), _response(), MIND, CONV)
        kw = mock_brain.learn_concept.call_args.kwargs
        assert kw["importance"] >= 0.85

    async def test_llm_inferred_source_lower_confidence(self, mock_brain: AsyncMock) -> None:
        """inferred source_quality → lower confidence than explicit."""
        explicit_concepts = [
            {
                "name": "Stated",
                "content": "stated fact",
                "category": "fact",
                "confidence": 0.8,
                "source_quality": "explicit",
            },
        ]
        inferred_concepts = [
            {
                "name": "Inferred",
                "content": "inferred fact",
                "category": "fact",
                "confidence": 0.8,
                "source_quality": "inferred",
            },
        ]
        router1 = _mock_llm_response(explicit_concepts)
        phase1 = ReflectPhase(mock_brain, llm_router=router1)
        await phase1.process(_perception("test1"), _response(), MIND, CONV)
        kw1 = mock_brain.learn_concept.call_args.kwargs
        explicit_conf = kw1["confidence"]

        mock_brain.learn_concept.reset_mock()
        router2 = _mock_llm_response(inferred_concepts)
        phase2 = ReflectPhase(mock_brain, llm_router=router2)
        await phase2.process(_perception("test2"), _response(), MIND, CONV)
        kw2 = mock_brain.learn_concept.call_args.kwargs
        inferred_conf = kw2["confidence"]

        assert explicit_conf > inferred_conf

    async def test_llm_different_categories_different_importance(
        self, mock_brain: AsyncMock
    ) -> None:
        """Different categories produce different importance values (same LLM score)."""
        concepts = [
            {
                "name": "Person",
                "content": "a person",
                "category": "entity",
                "sentiment": 0.0,
                "importance": 0.5,
            },
            {
                "name": "Fact",
                "content": "a fact",
                "category": "fact",
                "sentiment": 0.0,
                "importance": 0.5,
            },
        ]
        router = _mock_llm_response(concepts)
        mock_brain.learn_concept = AsyncMock(side_effect=[ConceptId("c1"), ConceptId("c2")])
        phase = ReflectPhase(mock_brain, llm_router=router)
        await phase.process(_perception("test"), _response(), MIND, CONV)

        calls = mock_brain.learn_concept.call_args_list
        entity_imp = calls[0].kwargs["importance"]
        fact_imp = calls[1].kwargs["importance"]
        # Entity category base (0.80) > Fact category base (0.60)
        assert entity_imp > fact_imp

    async def test_llm_returns_all_categories(self, mock_brain: AsyncMock) -> None:
        concepts = [
            {
                "name": "John Doe",
                "content": "User's name is John",
                "category": "entity",
                "sentiment": 0.0,
            },
            {
                "name": "Python Expert",
                "content": "Knows Python",
                "category": "skill",
                "sentiment": 0.3,
            },
            {
                "name": "Prefers Vim",
                "content": "Prefers Vim",
                "category": "preference",
                "sentiment": 0.5,
            },
            {
                "name": "Hates ORM",
                "content": "ORMs add complexity",
                "category": "belief",
                "sentiment": -0.7,
            },
            {
                "name": "AWS Migration",
                "content": "Migrated to AWS",
                "category": "event",
                "sentiment": 0.2,
            },
            {
                "name": "Team Lead",
                "content": "Leads a team",
                "category": "relationship",
                "sentiment": 0.1,
            },
            {
                "name": "Remote Worker",
                "content": "Works remotely",
                "category": "fact",
                "sentiment": 0.0,
            },
        ]
        router = _mock_llm_response(concepts)
        phase = ReflectPhase(mock_brain, llm_router=router)
        await phase.process(_perception("test message"), _response(), MIND, CONV)
        assert mock_brain.learn_concept.call_count == 7  # noqa: PLR2004

        categories_used = set()
        for call in mock_brain.learn_concept.call_args_list:
            categories_used.add(call.kwargs["category"])

        assert ConceptCategory.ENTITY in categories_used
        assert ConceptCategory.SKILL in categories_used
        assert ConceptCategory.PREFERENCE in categories_used
        assert ConceptCategory.BELIEF in categories_used
        assert ConceptCategory.EVENT in categories_used
        assert ConceptCategory.RELATIONSHIP in categories_used
        assert ConceptCategory.FACT in categories_used

    async def test_llm_sentiment_passed_through(self, mock_brain: AsyncMock) -> None:
        """LLM sentiment values are passed to learn_concept."""
        concepts = [
            {
                "name": "Loves Rust",
                "content": "loves Rust",
                "category": "preference",
                "sentiment": 0.9,
            },
        ]
        router = _mock_llm_response(concepts)
        phase = ReflectPhase(mock_brain, llm_router=router)
        await phase.process(_perception("test"), _response(), MIND, CONV)
        kw = mock_brain.learn_concept.call_args.kwargs
        assert kw["emotional_valence"] == pytest.approx(0.9, abs=0.01)

    async def test_llm_sentiment_clamped(self, mock_brain: AsyncMock) -> None:
        """Out-of-range sentiment values are clamped."""
        concepts = [
            {
                "name": "Extreme",
                "content": "extreme",
                "category": "belief",
                "sentiment": 5.0,
            },
        ]
        router = _mock_llm_response(concepts)
        phase = ReflectPhase(mock_brain, llm_router=router)
        await phase.process(_perception("test"), _response(), MIND, CONV)
        kw = mock_brain.learn_concept.call_args.kwargs
        assert kw["emotional_valence"] == pytest.approx(1.0, abs=0.01)

    async def test_llm_missing_importance_defaults(self, mock_brain: AsyncMock) -> None:
        """Missing importance field → default 0.5."""
        concepts = [{"name": "Test", "content": "test", "category": "fact"}]
        router = _mock_llm_response(concepts)
        phase = ReflectPhase(mock_brain, llm_router=router)
        await phase.process(_perception("test"), _response(), MIND, CONV)
        kw = mock_brain.learn_concept.call_args.kwargs
        # Default LLM importance 0.5, fact category 0.60:
        # combined = 0.40*0.5 + 0.25*0.60 + ... = reasonable value
        assert 0.2 < kw["importance"] < 0.6

    async def test_llm_missing_confidence_defaults(self, mock_brain: AsyncMock) -> None:
        """Missing confidence field → default 0.7."""
        concepts = [{"name": "Test", "content": "test content here", "category": "fact"}]
        router = _mock_llm_response(concepts)
        phase = ReflectPhase(mock_brain, llm_router=router)
        await phase.process(_perception("test"), _response(), MIND, CONV)
        kw = mock_brain.learn_concept.call_args.kwargs
        assert 0.5 < kw["confidence"] < 0.9

    async def test_llm_invalid_importance_clamped(self, mock_brain: AsyncMock) -> None:
        """Out-of-range importance → clamped to [0, 1]."""
        concepts = [
            {"name": "Test", "content": "test", "category": "fact", "importance": 5.0},
        ]
        router = _mock_llm_response(concepts)
        phase = ReflectPhase(mock_brain, llm_router=router)
        await phase.process(_perception("test"), _response(), MIND, CONV)
        kw = mock_brain.learn_concept.call_args.kwargs
        assert kw["importance"] <= 1.0

    async def test_llm_invalid_confidence_clamped(self, mock_brain: AsyncMock) -> None:
        """Non-numeric confidence → default 0.7."""
        concepts = [
            {
                "name": "Test",
                "content": "test",
                "category": "fact",
                "confidence": "not a number",
            },
        ]
        router = _mock_llm_response(concepts)
        phase = ReflectPhase(mock_brain, llm_router=router)
        await phase.process(_perception("test"), _response(), MIND, CONV)
        kw = mock_brain.learn_concept.call_args.kwargs
        assert 0.4 < kw["confidence"] < 0.9  # Uses default 0.7

    async def test_llm_invalid_source_quality_defaults_explicit(
        self, mock_brain: AsyncMock
    ) -> None:
        """Invalid source_quality → defaults to 'explicit'."""
        concepts = [
            {
                "name": "Test",
                "content": "test",
                "category": "fact",
                "source_quality": "INVALID",
            },
        ]
        router = _mock_llm_response(concepts)
        phase = ReflectPhase(mock_brain, llm_router=router)
        result = await phase._extract_with_llm("test")  # noqa: SLF001
        assert result is not None
        assert result[0].source_quality == "explicit"

    async def test_llm_extracted_concept_has_all_fields(self, mock_brain: AsyncMock) -> None:
        """Full extraction with all new fields."""
        concepts = [
            {
                "name": "Core Info",
                "content": "very important fact",
                "category": "entity",
                "sentiment": 0.5,
                "importance": 0.9,
                "confidence": 0.95,
                "explicit": True,
                "source_quality": "explicit",
            },
        ]
        router = _mock_llm_response(concepts)
        phase = ReflectPhase(mock_brain, llm_router=router)
        result = await phase._extract_with_llm("test")  # noqa: SLF001
        assert result is not None
        ec = result[0]
        assert ec.importance == pytest.approx(0.9)
        assert ec.confidence == pytest.approx(0.95)
        assert ec.explicit is True
        assert ec.source_quality == "explicit"

    async def test_llm_missing_sentiment_defaults_zero(self, mock_brain: AsyncMock) -> None:
        """Missing sentiment field defaults to 0.0."""
        concepts = [
            {"name": "Test", "content": "test", "category": "fact"},
        ]
        router = _mock_llm_response(concepts)
        phase = ReflectPhase(mock_brain, llm_router=router)
        await phase.process(_perception("test"), _response(), MIND, CONV)
        kw = mock_brain.learn_concept.call_args.kwargs
        assert kw["emotional_valence"] == 0.0

    async def test_llm_invalid_sentiment_defaults_zero(self, mock_brain: AsyncMock) -> None:
        """Non-numeric sentiment defaults to 0.0."""
        concepts = [
            {
                "name": "Test",
                "content": "test",
                "category": "fact",
                "sentiment": "not a number",
            },
        ]
        router = _mock_llm_response(concepts)
        phase = ReflectPhase(mock_brain, llm_router=router)
        await phase.process(_perception("test"), _response(), MIND, CONV)
        kw = mock_brain.learn_concept.call_args.kwargs
        assert kw["emotional_valence"] == 0.0

    async def test_llm_alias_opinion_becomes_belief(self, mock_brain: AsyncMock) -> None:
        concepts = [
            {
                "name": "GraphQL Bad",
                "content": "GraphQL is bad",
                "category": "opinion",
                "sentiment": -0.5,
            },
        ]
        router = _mock_llm_response(concepts)
        phase = ReflectPhase(mock_brain, llm_router=router)
        await phase.process(_perception("test"), _response(), MIND, CONV)
        kw = mock_brain.learn_concept.call_args.kwargs
        assert kw["category"] == ConceptCategory.BELIEF

    async def test_llm_alias_project_becomes_entity(self, mock_brain: AsyncMock) -> None:
        concepts = [
            {
                "name": "Sovyx",
                "content": "Building Sovyx",
                "category": "project",
                "sentiment": 0.4,
            },
        ]
        router = _mock_llm_response(concepts)
        phase = ReflectPhase(mock_brain, llm_router=router)
        await phase.process(_perception("test"), _response(), MIND, CONV)
        kw = mock_brain.learn_concept.call_args.kwargs
        assert kw["category"] == ConceptCategory.ENTITY

    async def test_llm_skill_not_collapsed_to_fact(self, mock_brain: AsyncMock) -> None:
        concepts = [
            {
                "name": "Rust Expert",
                "content": "Knows Rust",
                "category": "skill",
                "sentiment": 0.3,
            },
        ]
        router = _mock_llm_response(concepts)
        phase = ReflectPhase(mock_brain, llm_router=router)
        await phase.process(_perception("test"), _response(), MIND, CONV)
        kw = mock_brain.learn_concept.call_args.kwargs
        assert kw["category"] == ConceptCategory.SKILL

    async def test_llm_unknown_category_defaults_to_fact(self, mock_brain: AsyncMock) -> None:
        concepts = [
            {
                "name": "Something",
                "content": "Unknown",
                "category": "xyzzy",
                "sentiment": 0.0,
            },
        ]
        router = _mock_llm_response(concepts)
        phase = ReflectPhase(mock_brain, llm_router=router)
        await phase.process(_perception("test"), _response(), MIND, CONV)
        kw = mock_brain.learn_concept.call_args.kwargs
        assert kw["category"] == ConceptCategory.FACT

    async def test_llm_markdown_code_block(self, mock_brain: AsyncMock) -> None:
        router = AsyncMock()
        router.generate = AsyncMock(
            return_value=LLMResponse(
                content=(
                    "```json\n"
                    '[{"name":"Test","content":"test",'
                    '"category":"fact","sentiment":0.0}]\n'
                    "```"
                ),
                model="gpt-4o-mini",
                tokens_in=10,
                tokens_out=10,
                latency_ms=100,
                cost_usd=0.0,
                finish_reason="stop",
                provider="openai",
            )
        )
        phase = ReflectPhase(mock_brain, llm_router=router)
        await phase.process(_perception("test"), _response(), MIND, CONV)
        mock_brain.learn_concept.assert_called_once()

    async def test_llm_empty_array(self, mock_brain: AsyncMock) -> None:
        router = _mock_llm_response([])
        phase = ReflectPhase(mock_brain, llm_router=router)
        await phase.process(_perception("My name is Guipe"), _response(), MIND, CONV)
        mock_brain.learn_concept.assert_called_once()

    async def test_llm_returns_non_list(self, mock_brain: AsyncMock) -> None:
        router = AsyncMock()
        router.generate = AsyncMock(
            return_value=LLMResponse(
                content='{"name": "Test"}',
                model="gpt-4o-mini",
                tokens_in=10,
                tokens_out=10,
                latency_ms=100,
                cost_usd=0.0,
                finish_reason="stop",
                provider="openai",
            )
        )
        phase = ReflectPhase(mock_brain, llm_router=router)
        await phase.process(_perception("I love Python"), _response(), MIND, CONV)
        mock_brain.learn_concept.assert_called_once()

    async def test_llm_non_dict_items_skipped(self, mock_brain: AsyncMock) -> None:
        router = AsyncMock()
        router.generate = AsyncMock(
            return_value=LLMResponse(
                content=(
                    '["not a dict", '
                    '{"name":"OK","content":"ok",'
                    '"category":"fact","sentiment":0.0}]'
                ),
                model="gpt-4o-mini",
                tokens_in=10,
                tokens_out=10,
                latency_ms=100,
                cost_usd=0.0,
                finish_reason="stop",
                provider="openai",
            )
        )
        phase = ReflectPhase(mock_brain, llm_router=router)
        await phase.process(_perception("test"), _response(), MIND, CONV)
        mock_brain.learn_concept.assert_called_once()

    async def test_llm_extract_without_router(self, mock_brain: AsyncMock) -> None:
        phase = ReflectPhase(mock_brain, llm_router=None)
        result = await phase._extract_with_llm("test")  # noqa: SLF001
        assert result is None

    async def test_llm_failure_falls_back_to_regex(self, mock_brain: AsyncMock) -> None:
        router = AsyncMock()
        router.generate = AsyncMock(side_effect=RuntimeError("API error"))
        phase = ReflectPhase(mock_brain, llm_router=router)
        await phase.process(_perception("My name is Guipe"), _response(), MIND, CONV)
        mock_brain.learn_concept.assert_called_once()

    async def test_llm_invalid_json(self, mock_brain: AsyncMock) -> None:
        router = AsyncMock()
        router.generate = AsyncMock(
            return_value=LLMResponse(
                content="This is not JSON at all",
                model="gpt-4o-mini",
                tokens_in=10,
                tokens_out=10,
                latency_ms=100,
                cost_usd=0.0,
                finish_reason="stop",
                provider="openai",
            )
        )
        phase = ReflectPhase(mock_brain, llm_router=router)
        await phase.process(_perception("I love Python"), _response(), MIND, CONV)
        mock_brain.learn_concept.assert_called_once()


# ── Relation type classification ───────────────────────────────────────


class TestRelationClassification:
    """LLM-based relation type classification between concept pairs."""

    async def test_relation_types_passed_to_strengthen(self, mock_brain: AsyncMock) -> None:
        """Classified relation types are passed to strengthen_connection."""
        # First call: concept extraction
        # Second call: relation classification
        concepts_resp = LLMResponse(
            content=json.dumps(
                [
                    {
                        "name": "Python",
                        "content": "knows Python",
                        "category": "skill",
                        "sentiment": 0.3,
                    },
                    {
                        "name": "Django",
                        "content": "uses Django",
                        "category": "skill",
                        "sentiment": 0.2,
                    },
                ]
            ),
            model="gpt-4o-mini",
            tokens_in=50,
            tokens_out=50,
            latency_ms=100,
            cost_usd=0.0001,
            finish_reason="stop",
            provider="openai",
        )
        relations_resp = LLMResponse(
            content=json.dumps(
                [
                    {
                        "a": "Python",
                        "b": "Django",
                        "relation": "part_of",
                    },
                ]
            ),
            model="gpt-4o-mini",
            tokens_in=30,
            tokens_out=30,
            latency_ms=80,
            cost_usd=0.0001,
            finish_reason="stop",
            provider="openai",
        )
        router = AsyncMock()
        router.generate = AsyncMock(side_effect=[concepts_resp, relations_resp])

        # Make learn_concept return different IDs
        mock_brain.learn_concept = AsyncMock(side_effect=[ConceptId("c1"), ConceptId("c2")])

        phase = ReflectPhase(mock_brain, llm_router=router)
        await phase.process(_perception("test"), _response(), MIND, CONV)

        # strengthen_connection should be called with relation_types
        mock_brain.strengthen_connection.assert_called_once()
        call_kwargs = mock_brain.strengthen_connection.call_args.kwargs
        assert call_kwargs["relation_types"] is not None
        # The key uses canonical order (min, max) of string IDs
        rt = call_kwargs["relation_types"]
        key = (min("c1", "c2"), max("c1", "c2"))
        assert rt[key] == "part_of"

    async def test_relation_classification_failure_still_strengthens(
        self, mock_brain: AsyncMock
    ) -> None:
        """If relation classification fails, Hebbian still runs."""
        concepts_resp = LLMResponse(
            content=json.dumps(
                [
                    {
                        "name": "Alpha",
                        "content": "concept Alpha",
                        "category": "fact",
                        "sentiment": 0.0,
                    },
                    {
                        "name": "Beta",
                        "content": "concept Beta",
                        "category": "fact",
                        "sentiment": 0.0,
                    },
                ]
            ),
            model="gpt-4o-mini",
            tokens_in=50,
            tokens_out=50,
            latency_ms=100,
            cost_usd=0.0001,
            finish_reason="stop",
            provider="openai",
        )
        router = AsyncMock()
        router.generate = AsyncMock(side_effect=[concepts_resp, RuntimeError("API error")])
        mock_brain.learn_concept = AsyncMock(side_effect=[ConceptId("c1"), ConceptId("c2")])

        phase = ReflectPhase(mock_brain, llm_router=router)
        await phase.process(_perception("test"), _response(), MIND, CONV)

        # strengthen_connection still called (with None relation_types)
        mock_brain.strengthen_connection.assert_called_once()
        call_kwargs = mock_brain.strengthen_connection.call_args.kwargs
        assert call_kwargs["relation_types"] is None

    async def test_no_classification_without_router(self, mock_brain: AsyncMock) -> None:
        """No relation classification when LLM router is None."""
        phase = ReflectPhase(mock_brain)
        await phase.process(
            _perception("My name is Guipe and I love coding"),
            _response(),
            MIND,
            CONV,
        )
        # strengthen_connection called with None relation_types
        call_kwargs = mock_brain.strengthen_connection.call_args.kwargs
        assert call_kwargs["relation_types"] is None

    async def test_invalid_relation_defaults_to_related(self, mock_brain: AsyncMock) -> None:
        """Unknown relation type defaults to related_to."""
        concepts_resp = LLMResponse(
            content=json.dumps(
                [
                    {
                        "name": "Xray",
                        "content": "concept x",
                        "category": "fact",
                        "sentiment": 0.0,
                    },
                    {
                        "name": "Yank",
                        "content": "concept y",
                        "category": "fact",
                        "sentiment": 0.0,
                    },
                ]
            ),
            model="gpt-4o-mini",
            tokens_in=50,
            tokens_out=50,
            latency_ms=100,
            cost_usd=0.0001,
            finish_reason="stop",
            provider="openai",
        )
        relations_resp = LLMResponse(
            content=json.dumps(
                [
                    {"a": "Xray", "b": "Yank", "relation": "INVALID_TYPE"},
                ]
            ),
            model="gpt-4o-mini",
            tokens_in=30,
            tokens_out=30,
            latency_ms=80,
            cost_usd=0.0001,
            finish_reason="stop",
            provider="openai",
        )
        router = AsyncMock()
        router.generate = AsyncMock(side_effect=[concepts_resp, relations_resp])
        mock_brain.learn_concept = AsyncMock(side_effect=[ConceptId("c1"), ConceptId("c2")])

        phase = ReflectPhase(mock_brain, llm_router=router)
        await phase.process(_perception("test"), _response(), MIND, CONV)

        call_kwargs = mock_brain.strengthen_connection.call_args.kwargs
        rt = call_kwargs["relation_types"]
        key = (min("c1", "c2"), max("c1", "c2"))
        assert rt[key] == "related_to"

    async def test_all_valid_relation_types_accepted(self, mock_brain: AsyncMock) -> None:
        """All 7 relation types are accepted."""
        from sovyx.cognitive.reflect import _VALID_RELATIONS
        from sovyx.engine.types import RelationType

        # Every RelationType value should be in _VALID_RELATIONS
        for rt in RelationType:
            assert rt.value in _VALID_RELATIONS, f"RelationType.{rt.name} not in _VALID_RELATIONS"

    async def test_classify_markdown_code_block(self, mock_brain: AsyncMock) -> None:
        """Relation response wrapped in markdown code block."""
        concepts_resp = LLMResponse(
            content=json.dumps(
                [
                    {
                        "name": "Python",
                        "content": "knows Python",
                        "category": "skill",
                        "sentiment": 0.0,
                    },
                    {
                        "name": "Django",
                        "content": "uses Django",
                        "category": "skill",
                        "sentiment": 0.0,
                    },
                ]
            ),
            model="gpt-4o-mini",
            tokens_in=50,
            tokens_out=50,
            latency_ms=100,
            cost_usd=0.0001,
            finish_reason="stop",
            provider="openai",
        )
        relations_resp = LLMResponse(
            content=('```json\n[{"a":"Python","b":"Django","relation":"part_of"}]\n```'),
            model="gpt-4o-mini",
            tokens_in=30,
            tokens_out=30,
            latency_ms=80,
            cost_usd=0.0001,
            finish_reason="stop",
            provider="openai",
        )
        router = AsyncMock()
        router.generate = AsyncMock(side_effect=[concepts_resp, relations_resp])
        mock_brain.learn_concept = AsyncMock(side_effect=[ConceptId("c1"), ConceptId("c2")])

        phase = ReflectPhase(mock_brain, llm_router=router)
        await phase.process(_perception("test"), _response(), MIND, CONV)
        rt = mock_brain.strengthen_connection.call_args.kwargs["relation_types"]
        assert rt is not None

    async def test_classify_non_list_response(self, mock_brain: AsyncMock) -> None:
        """Relation response that is not a list → returns None."""
        concepts_resp = LLMResponse(
            content=json.dumps(
                [
                    {
                        "name": "Alpha",
                        "content": "alpha",
                        "category": "fact",
                        "sentiment": 0.0,
                    },
                    {
                        "name": "Beta",
                        "content": "beta",
                        "category": "fact",
                        "sentiment": 0.0,
                    },
                ]
            ),
            model="gpt-4o-mini",
            tokens_in=50,
            tokens_out=50,
            latency_ms=100,
            cost_usd=0.0001,
            finish_reason="stop",
            provider="openai",
        )
        relations_resp = LLMResponse(
            content='{"not": "a list"}',
            model="gpt-4o-mini",
            tokens_in=10,
            tokens_out=10,
            latency_ms=80,
            cost_usd=0.0001,
            finish_reason="stop",
            provider="openai",
        )
        router = AsyncMock()
        router.generate = AsyncMock(side_effect=[concepts_resp, relations_resp])
        mock_brain.learn_concept = AsyncMock(side_effect=[ConceptId("c1"), ConceptId("c2")])

        phase = ReflectPhase(mock_brain, llm_router=router)
        await phase.process(_perception("test"), _response(), MIND, CONV)
        rt = mock_brain.strengthen_connection.call_args.kwargs["relation_types"]
        assert rt is None

    async def test_classify_non_dict_items_skipped(self, mock_brain: AsyncMock) -> None:
        """Non-dict items in relation list are skipped."""
        concepts_resp = LLMResponse(
            content=json.dumps(
                [
                    {
                        "name": "Alpha",
                        "content": "alpha",
                        "category": "fact",
                        "sentiment": 0.0,
                    },
                    {
                        "name": "Beta",
                        "content": "beta",
                        "category": "fact",
                        "sentiment": 0.0,
                    },
                ]
            ),
            model="gpt-4o-mini",
            tokens_in=50,
            tokens_out=50,
            latency_ms=100,
            cost_usd=0.0001,
            finish_reason="stop",
            provider="openai",
        )
        relations_resp = LLMResponse(
            content=('["not_a_dict", {"a":"Alpha","b":"Beta","relation":"causes"}]'),
            model="gpt-4o-mini",
            tokens_in=30,
            tokens_out=30,
            latency_ms=80,
            cost_usd=0.0001,
            finish_reason="stop",
            provider="openai",
        )
        router = AsyncMock()
        router.generate = AsyncMock(side_effect=[concepts_resp, relations_resp])
        mock_brain.learn_concept = AsyncMock(side_effect=[ConceptId("c1"), ConceptId("c2")])

        phase = ReflectPhase(mock_brain, llm_router=router)
        await phase.process(_perception("test"), _response(), MIND, CONV)
        rt = mock_brain.strengthen_connection.call_args.kwargs["relation_types"]
        assert rt is not None
        key = (min("c1", "c2"), max("c1", "c2"))
        assert rt[key] == "causes"

    async def test_classify_with_single_concept_returns_none(self, mock_brain: AsyncMock) -> None:
        """Classification not called with < 2 concepts."""
        phase = ReflectPhase(mock_brain, llm_router=AsyncMock())
        result = await phase._classify_relations(  # noqa: SLF001
            [ExtractedConcept("A", "a", "fact")],
            [ConceptId("c1")],
        )
        assert result is None


# ── Episode emotional signals ─────────────────────────────────────────


class TestEpisodeEmotional:
    """Episode emotional_valence and emotional_arousal from concepts."""

    async def test_positive_sentiment_sets_episode_valence(self, mock_brain: AsyncMock) -> None:
        """Positive concepts → positive episode valence."""
        concepts = [
            {
                "name": "Loves Rust",
                "content": "loves Rust",
                "category": "preference",
                "sentiment": 0.8,
            },
        ]
        router = _mock_llm_response(concepts)
        phase = ReflectPhase(mock_brain, llm_router=router)
        await phase.process(_perception("test"), _response(), MIND, CONV)
        kw = mock_brain.encode_episode.call_args.kwargs
        assert kw["emotional_valence"] > 0.0

    async def test_negative_sentiment_sets_episode_valence(self, mock_brain: AsyncMock) -> None:
        """Negative concepts → negative episode valence."""
        concepts = [
            {
                "name": "Hates ORM",
                "content": "hates ORM",
                "category": "belief",
                "sentiment": -0.7,
            },
        ]
        router = _mock_llm_response(concepts)
        phase = ReflectPhase(mock_brain, llm_router=router)
        await phase.process(_perception("test"), _response(), MIND, CONV)
        kw = mock_brain.encode_episode.call_args.kwargs
        assert kw["emotional_valence"] < 0.0

    async def test_arousal_is_max_abs_sentiment(self, mock_brain: AsyncMock) -> None:
        """Arousal = max |sentiment| across concepts."""
        concepts = [
            {
                "name": "Neutral",
                "content": "neutral",
                "category": "fact",
                "sentiment": 0.1,
            },
            {
                "name": "Strong",
                "content": "strong",
                "category": "belief",
                "sentiment": -0.9,
            },
        ]
        router = _mock_llm_response(concepts)
        phase = ReflectPhase(mock_brain, llm_router=router)
        await phase.process(_perception("test"), _response(), MIND, CONV)
        kw = mock_brain.encode_episode.call_args.kwargs
        assert kw["emotional_arousal"] == pytest.approx(0.9, abs=0.01)

    async def test_no_concepts_zero_valence(self, mock_brain: AsyncMock) -> None:
        """No concepts → zero valence and arousal."""
        phase = ReflectPhase(mock_brain)
        await phase.process(_perception("What time is it?"), _response(), MIND, CONV)
        kw = mock_brain.encode_episode.call_args.kwargs
        assert kw["emotional_valence"] == 0.0
        assert kw["emotional_arousal"] == 0.0

    async def test_mixed_sentiment_averages(self, mock_brain: AsyncMock) -> None:
        """Mixed sentiments → averaged valence."""
        concepts = [
            {
                "name": "Good",
                "content": "good",
                "category": "preference",
                "sentiment": 0.6,
            },
            {
                "name": "Bad",
                "content": "bad",
                "category": "belief",
                "sentiment": -0.4,
            },
        ]
        router = _mock_llm_response(concepts)
        phase = ReflectPhase(mock_brain, llm_router=router)
        await phase.process(_perception("test"), _response(), MIND, CONV)
        kw = mock_brain.encode_episode.call_args.kwargs
        # Average of 0.6 and -0.4 = 0.1
        assert kw["emotional_valence"] == pytest.approx(0.1, abs=0.01)
        # Arousal = max(0.6, 0.4) = 0.6
        assert kw["emotional_arousal"] == pytest.approx(0.6, abs=0.01)


# ── Episode encoding ──────────────────────────────────────────────────


class TestEpisodeEncoding:
    """Episode creation."""

    async def test_episode_always_created(self, mock_brain: AsyncMock) -> None:
        phase = ReflectPhase(mock_brain)
        await phase.process(_perception("Hello"), _response("Hi"), MIND, CONV)
        mock_brain.encode_episode.assert_called_once()
        kw = mock_brain.encode_episode.call_args.kwargs
        assert kw["user_input"] == "Hello"
        assert kw["assistant_response"] == "Hi"

    async def test_episode_failure_no_crash(self, mock_brain: AsyncMock) -> None:
        mock_brain.encode_episode = AsyncMock(side_effect=RuntimeError("fail"))
        phase = ReflectPhase(mock_brain)
        await phase.process(_perception("Hello"), _response(), MIND, CONV)


# ── Hebbian learning ──────────────────────────────────────────────────


class TestHebbianLearning:
    """Hebbian strengthening between co-mentioned concepts."""

    async def test_hebbian_with_multiple_concepts(self, mock_brain: AsyncMock) -> None:
        phase = ReflectPhase(mock_brain)
        await phase.process(
            _perception("My name is Guipe and I love coding"),
            _response(),
            MIND,
            CONV,
        )
        assert mock_brain.learn_concept.call_count == 2  # noqa: PLR2004
        mock_brain.strengthen_connection.assert_called_once()

    async def test_no_hebbian_with_single_concept(self, mock_brain: AsyncMock) -> None:
        phase = ReflectPhase(mock_brain)
        await phase.process(_perception("My name is Guipe"), _response(), MIND, CONV)
        mock_brain.strengthen_connection.assert_not_called()

    async def test_hebbian_failure_no_crash(self, mock_brain: AsyncMock) -> None:
        mock_brain.strengthen_connection = AsyncMock(side_effect=RuntimeError("fail"))
        phase = ReflectPhase(mock_brain)
        await phase.process(
            _perception("My name is Guipe and I love coding"),
            _response(),
            MIND,
            CONV,
        )


# ── Failure resilience ────────────────────────────────────────────────


class TestConceptExtractionFailure:
    """Graceful handling of concept learning failures."""

    async def test_learn_failure_continues(self, mock_brain: AsyncMock) -> None:
        mock_brain.learn_concept = AsyncMock(side_effect=RuntimeError("fail"))
        phase = ReflectPhase(mock_brain)
        await phase.process(_perception("My name is Test"), _response(), MIND, CONV)
        mock_brain.encode_episode.assert_called_once()


# ── Episode importance + concepts_mentioned ───────────────────────────


class TestEpisodeImportance:
    """Dynamic episode importance scoring."""

    def test_short_neutral_message_low_importance(self) -> None:
        """Short message, no concepts, no emotion → low importance."""
        imp = compute_episode_importance("hi", 0, 0.0)
        assert imp < 0.4

    def test_long_message_higher_importance(self) -> None:
        """Longer message → higher importance."""
        short_imp = compute_episode_importance("hi", 0, 0.0)
        long_imp = compute_episode_importance("x" * 400, 0, 0.0)
        assert long_imp > short_imp

    def test_more_concepts_higher_importance(self) -> None:
        """More extracted concepts → higher importance."""
        imp_0 = compute_episode_importance("test", 0, 0.0)
        imp_5 = compute_episode_importance("test", 5, 0.0)
        assert imp_5 > imp_0

    def test_emotional_message_higher_importance(self) -> None:
        """High emotional valence → higher importance."""
        neutral = compute_episode_importance("test", 1, 0.0)
        emotional = compute_episode_importance("test", 1, 0.9)
        assert emotional > neutral

    def test_importance_always_bounded(self) -> None:
        """Property: importance always in [0.1, 1.0]."""
        # Minimum case
        assert compute_episode_importance("", 0, 0.0) >= 0.1
        # Maximum case
        assert compute_episode_importance("x" * 10000, 100, 1.0) <= 1.0

    def test_importance_capped_at_one(self) -> None:
        """Even extreme inputs stay ≤ 1.0."""
        imp = compute_episode_importance("x" * 5000, 20, 1.0)
        assert imp == pytest.approx(1.0, abs=0.01)

    def test_realistic_hi_message(self) -> None:
        """'hi' → ~0.3 importance."""
        imp = compute_episode_importance("hi", 0, 0.0)
        assert 0.1 <= imp <= 0.4

    def test_realistic_rich_message(self) -> None:
        """Long opinionated message → ~0.8."""
        msg = (
            "I've been using Rust for 3 years and I absolutely love it. "
            "It changed how I think about memory safety."
        )
        imp = compute_episode_importance(msg, 4, 0.8)
        assert imp >= 0.7

    def test_high_concept_importance_raises_episode(self) -> None:
        """Concepts with high importance → episode importance rises."""
        base = compute_episode_importance("test message", 2, 0.0)
        boosted = compute_episode_importance(
            "test message",
            2,
            0.0,
            concept_importances=[0.95, 0.90],
        )
        assert boosted > base

    def test_low_concept_importance_lowers_episode(self) -> None:
        """Concepts with low importance → episode stays low."""
        imp = compute_episode_importance(
            "test message",
            2,
            0.0,
            concept_importances=[0.1, 0.1],
        )
        assert imp < 0.5  # noqa: PLR2004

    def test_concept_importance_bounded(self) -> None:
        """With concept importances, still in [0.1, 1.0]."""
        low = compute_episode_importance(
            "",
            0,
            0.0,
            concept_importances=[0.0],
        )
        high = compute_episode_importance(
            "x" * 1000,
            10,
            1.0,
            concept_importances=[1.0] * 10,
        )
        assert low >= 0.1
        assert high <= 1.0

    def test_empty_concept_importances_fallback(self) -> None:
        """Empty list → falls back to original formula."""
        with_empty = compute_episode_importance("hi", 1, 0.0, concept_importances=[])
        without = compute_episode_importance("hi", 1, 0.0)
        assert with_empty == pytest.approx(without, abs=0.01)


class TestConceptsMentioned:
    """concepts_mentioned wiring from reflect to episode."""

    async def test_concepts_passed_to_encode(self, mock_brain: AsyncMock) -> None:
        """Extracted concept IDs are passed as concepts_mentioned."""
        concepts = [
            {
                "name": "Python",
                "content": "knows Python",
                "category": "skill",
                "sentiment": 0.3,
            },
        ]
        router = _mock_llm_response(concepts)
        phase = ReflectPhase(mock_brain, llm_router=router)
        await phase.process(_perception("test"), _response(), MIND, CONV)

        kw = mock_brain.encode_episode.call_args.kwargs
        assert kw["concepts_mentioned"] is not None
        assert len(kw["concepts_mentioned"]) == 1

    async def test_no_concepts_none_mentioned(self, mock_brain: AsyncMock) -> None:
        """No concepts extracted → concepts_mentioned is None."""
        phase = ReflectPhase(mock_brain)
        await phase.process(_perception("What time is it?"), _response(), MIND, CONV)
        kw = mock_brain.encode_episode.call_args.kwargs
        assert kw["concepts_mentioned"] is None

    async def test_importance_not_hardcoded(self, mock_brain: AsyncMock) -> None:
        """Episode importance is dynamic, not always 0.5."""
        concepts = [
            {
                "name": "Rust Expert",
                "content": "loves Rust",
                "category": "skill",
                "sentiment": 0.8,
                "importance": 0.9,
            },
            {
                "name": "Memory Safety",
                "content": "cares about memory",
                "category": "belief",
                "sentiment": 0.6,
                "importance": 0.85,
            },
        ]
        router = _mock_llm_response(concepts)
        mock_brain.learn_concept = AsyncMock(side_effect=[ConceptId("c1"), ConceptId("c2")])
        phase = ReflectPhase(mock_brain, llm_router=router)
        long_msg = (
            "I love Rust because of memory safety. It completely changed how I build systems."
        )
        await phase.process(_perception(long_msg), _response(), MIND, CONV)

        kw = mock_brain.encode_episode.call_args.kwargs
        # Should NOT be 0.5 — dynamic scoring
        assert kw["importance"] != pytest.approx(0.5, abs=0.01)
        assert kw["importance"] > 0.3


# ── Episode summary generation ────────────────────────────────────────


class TestEpisodeSummary:
    """LLM-based episode summary generation."""

    async def test_summary_passed_to_encode(self, mock_brain: AsyncMock) -> None:
        """Summary from LLM is passed through to encode_episode."""
        # 3 calls: extraction, relation classification, summary
        concepts_resp = LLMResponse(
            content=json.dumps(
                [
                    {
                        "name": "Python",
                        "content": "knows Python",
                        "category": "skill",
                        "sentiment": 0.3,
                    },
                    {
                        "name": "Django",
                        "content": "uses Django",
                        "category": "skill",
                        "sentiment": 0.2,
                    },
                ]
            ),
            model="gpt-4o-mini",
            tokens_in=50,
            tokens_out=50,
            latency_ms=100,
            cost_usd=0.0001,
            finish_reason="stop",
            provider="openai",
        )
        relations_resp = LLMResponse(
            content="[]",
            model="gpt-4o-mini",
            tokens_in=10,
            tokens_out=10,
            latency_ms=50,
            cost_usd=0.0,
            finish_reason="stop",
            provider="openai",
        )
        summary_resp = LLMResponse(
            content="User discussed their Python and Django expertise.",
            model="gpt-4o-mini",
            tokens_in=20,
            tokens_out=15,
            latency_ms=80,
            cost_usd=0.0001,
            finish_reason="stop",
            provider="openai",
        )
        router = AsyncMock()
        router.generate = AsyncMock(side_effect=[concepts_resp, relations_resp, summary_resp])
        mock_brain.learn_concept = AsyncMock(side_effect=[ConceptId("c1"), ConceptId("c2")])

        phase = ReflectPhase(mock_brain, llm_router=router)
        await phase.process(_perception("test"), _response(), MIND, CONV)

        kw = mock_brain.encode_episode.call_args.kwargs
        assert kw["summary"] == "User discussed their Python and Django expertise."

    async def test_summary_failure_passes_none(self, mock_brain: AsyncMock) -> None:
        """LLM failure for summary → None (graceful fallback)."""
        concepts_resp = LLMResponse(
            content="[]",
            model="gpt-4o-mini",
            tokens_in=10,
            tokens_out=10,
            latency_ms=50,
            cost_usd=0.0,
            finish_reason="stop",
            provider="openai",
        )
        router = AsyncMock()
        router.generate = AsyncMock(side_effect=[concepts_resp, RuntimeError("fail")])

        phase = ReflectPhase(mock_brain, llm_router=router)
        await phase.process(_perception("My name is Guipe"), _response(), MIND, CONV)

        kw = mock_brain.encode_episode.call_args.kwargs
        assert kw["summary"] is None

    async def test_no_summary_without_router(self, mock_brain: AsyncMock) -> None:
        """No router → no summary."""
        phase = ReflectPhase(mock_brain, llm_router=None)
        await phase.process(_perception("hi"), _response(), MIND, CONV)

        kw = mock_brain.encode_episode.call_args.kwargs
        assert kw["summary"] is None

    async def test_summary_strips_quotes(self, mock_brain: AsyncMock) -> None:
        """Summary wrapped in quotes → stripped."""
        phase = ReflectPhase(mock_brain, llm_router=AsyncMock())
        # Mock the internal _generate_summary directly
        summary_resp = LLMResponse(
            content='"User likes coding."',
            model="gpt-4o-mini",
            tokens_in=10,
            tokens_out=10,
            latency_ms=50,
            cost_usd=0.0,
            finish_reason="stop",
            provider="openai",
        )
        phase._router = AsyncMock()  # type: ignore[assignment]
        phase._router.generate = AsyncMock(return_value=summary_resp)  # type: ignore[union-attr]
        result = await phase._generate_summary("test", "ok")
        assert result == "User likes coding."

    async def test_summary_truncates_long_response(self, mock_brain: AsyncMock) -> None:
        """Very long summary → truncated to 200 chars."""
        phase = ReflectPhase(mock_brain, llm_router=AsyncMock())
        long_text = "x" * 300
        summary_resp = LLMResponse(
            content=long_text,
            model="gpt-4o-mini",
            tokens_in=10,
            tokens_out=100,
            latency_ms=50,
            cost_usd=0.0,
            finish_reason="stop",
            provider="openai",
        )
        phase._router = AsyncMock()  # type: ignore[assignment]
        phase._router.generate = AsyncMock(return_value=summary_resp)  # type: ignore[union-attr]
        result = await phase._generate_summary("test", "ok")
        assert result is not None
        assert len(result) == 200
        assert result.endswith("...")
