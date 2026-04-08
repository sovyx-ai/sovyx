"""Tests for sovyx.cognitive.reflect — ReflectPhase.

Covers:
- LLM-based concept extraction (mocked)
- Regex fallback extraction for all 7 categories
- Category mapping (canonical + aliases)
- Importance assignment
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
    ReflectPhase,
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


def _mock_llm_response(concepts: list[dict[str, str]]) -> AsyncMock:
    """Create a mock LLM router that returns the given concepts as JSON."""
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
    return brain


# ── Category mapping tests ─────────────────────────────────────────────


class TestCategoryMapping:
    """Every ConceptCategory value must be reachable via _CATEGORY_MAP."""

    def test_all_categories_covered(self) -> None:
        """Property: every ConceptCategory value has >=1 key in _CATEGORY_MAP."""
        mapped_values = set(_CATEGORY_MAP.values())
        for cat in ConceptCategory:
            assert cat.value in mapped_values, (
                f"ConceptCategory.{cat.name} ({cat.value}) has no key in _CATEGORY_MAP"
            )

    def test_direct_mappings(self) -> None:
        """Each canonical category maps to itself."""
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
        """Property: resolve_category never raises for any input."""
        result = resolve_category(raw)
        assert isinstance(result, str)
        assert len(result) > 0


class TestImportance:
    """Importance assignment by category."""

    def test_all_categories_have_importance(self) -> None:
        """Every canonical category has an importance value."""
        for cat in ConceptCategory:
            imp = get_importance(cat.value)
            assert 0.0 < imp <= 1.0, f"{cat.value} importance={imp}"

    def test_unknown_category_gets_default(self) -> None:
        assert get_importance("unknown") == 0.5

    def test_entity_highest_importance(self) -> None:
        assert get_importance("entity") >= get_importance("fact")

    def test_relationship_highest_importance(self) -> None:
        assert get_importance("relationship") >= get_importance("fact")


# ── Regex fallback extraction ──────────────────────────────────────────


class TestRegexExtraction:
    """Regex fallback covers all 7 categories."""

    async def test_entity_english(self, mock_brain: AsyncMock) -> None:
        phase = ReflectPhase(mock_brain)
        await phase.process(_perception("My name is Guipe"), _response(), MIND, CONV)
        mock_brain.learn_concept.assert_called_once()
        call_kwargs = mock_brain.learn_concept.call_args.kwargs
        assert call_kwargs["name"] == "Guipe"
        assert call_kwargs["category"] == ConceptCategory.ENTITY

    async def test_entity_portuguese(self, mock_brain: AsyncMock) -> None:
        phase = ReflectPhase(mock_brain)
        await phase.process(_perception("Meu nome é Renan"), _response(), MIND, CONV)
        mock_brain.learn_concept.assert_called_once()
        call_kwargs = mock_brain.learn_concept.call_args.kwargs
        assert call_kwargs["name"] == "Renan"
        assert call_kwargs["category"] == ConceptCategory.ENTITY

    async def test_preference_english(self, mock_brain: AsyncMock) -> None:
        phase = ReflectPhase(mock_brain)
        await phase.process(_perception("I love coffee"), _response(), MIND, CONV)
        mock_brain.learn_concept.assert_called_once()
        call_kwargs = mock_brain.learn_concept.call_args.kwargs
        assert call_kwargs["category"] == ConceptCategory.PREFERENCE

    async def test_preference_portuguese(self, mock_brain: AsyncMock) -> None:
        phase = ReflectPhase(mock_brain)
        await phase.process(_perception("Eu gosto de café"), _response(), MIND, CONV)
        mock_brain.learn_concept.assert_called_once()

    async def test_fact_english(self, mock_brain: AsyncMock) -> None:
        phase = ReflectPhase(mock_brain)
        await phase.process(_perception("I work at Google"), _response(), MIND, CONV)
        mock_brain.learn_concept.assert_called_once()
        call_kwargs = mock_brain.learn_concept.call_args.kwargs
        assert call_kwargs["category"] == ConceptCategory.FACT

    async def test_skill_english(self, mock_brain: AsyncMock) -> None:
        phase = ReflectPhase(mock_brain)
        await phase.process(_perception("I code in Rust"), _response(), MIND, CONV)
        mock_brain.learn_concept.assert_called_once()
        call_kwargs = mock_brain.learn_concept.call_args.kwargs
        assert call_kwargs["category"] == ConceptCategory.SKILL

    async def test_belief_english(self, mock_brain: AsyncMock) -> None:
        phase = ReflectPhase(mock_brain)
        await phase.process(_perception("I think ORMs are harmful"), _response(), MIND, CONV)
        mock_brain.learn_concept.assert_called_once()
        call_kwargs = mock_brain.learn_concept.call_args.kwargs
        assert call_kwargs["category"] == ConceptCategory.BELIEF

    async def test_belief_portuguese(self, mock_brain: AsyncMock) -> None:
        phase = ReflectPhase(mock_brain)
        await phase.process(
            _perception("Eu acho que microservices são overrated"), _response(), MIND, CONV
        )
        mock_brain.learn_concept.assert_called_once()
        call_kwargs = mock_brain.learn_concept.call_args.kwargs
        assert call_kwargs["category"] == ConceptCategory.BELIEF

    async def test_event_english(self, mock_brain: AsyncMock) -> None:
        phase = ReflectPhase(mock_brain)
        await phase.process(_perception("I migrated to AWS last month"), _response(), MIND, CONV)
        mock_brain.learn_concept.assert_called_once()
        call_kwargs = mock_brain.learn_concept.call_args.kwargs
        assert call_kwargs["category"] == ConceptCategory.EVENT

    async def test_relationship_english(self, mock_brain: AsyncMock) -> None:
        phase = ReflectPhase(mock_brain)
        await phase.process(_perception("I manage a team of 5 engineers"), _response(), MIND, CONV)
        mock_brain.learn_concept.assert_called_once()
        call_kwargs = mock_brain.learn_concept.call_args.kwargs
        assert call_kwargs["category"] == ConceptCategory.RELATIONSHIP

    async def test_no_concepts_extracted(self, mock_brain: AsyncMock) -> None:
        phase = ReflectPhase(mock_brain)
        await phase.process(_perception("What time is it?"), _response(), MIND, CONV)
        mock_brain.learn_concept.assert_not_called()


# ── LLM-based extraction ──────────────────────────────────────────────


class TestLLMExtraction:
    """LLM-based concept extraction with mocked router."""

    async def test_llm_returns_all_categories(self, mock_brain: AsyncMock) -> None:
        """LLM response with all 7 categories → all resolved correctly."""
        concepts = [
            {"name": "John Doe", "content": "User's name is John", "category": "entity"},
            {"name": "Python Expert", "content": "User knows Python", "category": "skill"},
            {"name": "Prefers Vim", "content": "Prefers Vim", "category": "preference"},
            {"name": "Hates ORM", "content": "ORMs add complexity", "category": "belief"},
            {"name": "AWS Migration", "content": "User migrated to AWS", "category": "event"},
            {"name": "Team Lead", "content": "User leads a team", "category": "relationship"},
            {"name": "Remote Worker", "content": "User works remotely", "category": "fact"},
        ]
        router = _mock_llm_response(concepts)
        phase = ReflectPhase(mock_brain, llm_router=router)
        await phase.process(_perception("test message"), _response(), MIND, CONV)
        assert mock_brain.learn_concept.call_count == 7  # noqa: PLR2004

        # Collect all categories passed to learn_concept
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

    async def test_llm_alias_opinion_becomes_belief(self, mock_brain: AsyncMock) -> None:
        """LLM returning 'opinion' category → resolved to BELIEF."""
        concepts = [
            {"name": "GraphQL Bad", "content": "GraphQL is bad", "category": "opinion"},
        ]
        router = _mock_llm_response(concepts)
        phase = ReflectPhase(mock_brain, llm_router=router)
        await phase.process(_perception("test"), _response(), MIND, CONV)
        call_kwargs = mock_brain.learn_concept.call_args.kwargs
        assert call_kwargs["category"] == ConceptCategory.BELIEF

    async def test_llm_alias_project_becomes_entity(self, mock_brain: AsyncMock) -> None:
        """LLM returning 'project' category → resolved to ENTITY."""
        concepts = [
            {"name": "Sovyx", "content": "User is building Sovyx", "category": "project"},
        ]
        router = _mock_llm_response(concepts)
        phase = ReflectPhase(mock_brain, llm_router=router)
        await phase.process(_perception("test"), _response(), MIND, CONV)
        call_kwargs = mock_brain.learn_concept.call_args.kwargs
        assert call_kwargs["category"] == ConceptCategory.ENTITY

    async def test_llm_skill_not_collapsed_to_fact(self, mock_brain: AsyncMock) -> None:
        """TASK-01 fix: 'skill' category must map to SKILL, not FACT."""
        concepts = [
            {"name": "Rust Expert", "content": "User knows Rust", "category": "skill"},
        ]
        router = _mock_llm_response(concepts)
        phase = ReflectPhase(mock_brain, llm_router=router)
        await phase.process(_perception("test"), _response(), MIND, CONV)
        call_kwargs = mock_brain.learn_concept.call_args.kwargs
        assert call_kwargs["category"] == ConceptCategory.SKILL

    async def test_llm_unknown_category_defaults_to_fact(self, mock_brain: AsyncMock) -> None:
        """Unknown LLM category → defaults to FACT."""
        concepts = [
            {"name": "Something", "content": "Unknown category", "category": "xyzzy"},
        ]
        router = _mock_llm_response(concepts)
        phase = ReflectPhase(mock_brain, llm_router=router)
        await phase.process(_perception("test"), _response(), MIND, CONV)
        call_kwargs = mock_brain.learn_concept.call_args.kwargs
        assert call_kwargs["category"] == ConceptCategory.FACT

    async def test_llm_markdown_code_block(self, mock_brain: AsyncMock) -> None:
        """LLM wrapping response in markdown code block."""
        router = AsyncMock()
        router.generate = AsyncMock(
            return_value=LLMResponse(
                content='```json\n[{"name": "Test", "content": "test", "category": "fact"}]\n```',
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
        """LLM returning empty array → falls back to regex."""
        router = _mock_llm_response([])
        phase = ReflectPhase(mock_brain, llm_router=router)
        await phase.process(_perception("My name is Guipe"), _response(), MIND, CONV)
        # Should fall back to regex and extract entity
        mock_brain.learn_concept.assert_called_once()

    async def test_llm_failure_falls_back_to_regex(self, mock_brain: AsyncMock) -> None:
        """LLM error → graceful fallback to regex."""
        router = AsyncMock()
        router.generate = AsyncMock(side_effect=RuntimeError("API error"))
        phase = ReflectPhase(mock_brain, llm_router=router)
        await phase.process(_perception("My name is Guipe"), _response(), MIND, CONV)
        # Regex fallback should still extract
        mock_brain.learn_concept.assert_called_once()

    async def test_llm_returns_non_list(self, mock_brain: AsyncMock) -> None:
        """LLM returning a dict instead of array → fallback to regex."""
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
        # Regex fallback
        mock_brain.learn_concept.assert_called_once()

    async def test_llm_non_dict_items_skipped(self, mock_brain: AsyncMock) -> None:
        """LLM returning array with non-dict items → skipped."""
        router = AsyncMock()
        router.generate = AsyncMock(
            return_value=LLMResponse(
                content='["not a dict", {"name": "OK", "content": "ok", "category": "fact"}]',
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
        # Only the valid dict item should be learned
        mock_brain.learn_concept.assert_called_once()

    async def test_llm_extract_without_router(self, mock_brain: AsyncMock) -> None:
        """_extract_with_llm returns None when no router configured."""
        phase = ReflectPhase(mock_brain, llm_router=None)
        result = await phase._extract_with_llm("test")
        assert result is None

    async def test_llm_invalid_json(self, mock_brain: AsyncMock) -> None:
        """LLM returning invalid JSON → fallback to regex."""
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
        # Regex fallback
        mock_brain.learn_concept.assert_called_once()


# ── Episode encoding ──────────────────────────────────────────────────


class TestEpisodeEncoding:
    """Episode creation."""

    async def test_episode_always_created(self, mock_brain: AsyncMock) -> None:
        phase = ReflectPhase(mock_brain)
        await phase.process(_perception("Hello"), _response("Hi"), MIND, CONV)
        mock_brain.encode_episode.assert_called_once()
        call_kwargs = mock_brain.encode_episode.call_args.kwargs
        assert call_kwargs["user_input"] == "Hello"
        assert call_kwargs["assistant_response"] == "Hi"

    async def test_episode_failure_no_crash(self, mock_brain: AsyncMock) -> None:
        mock_brain.encode_episode = AsyncMock(side_effect=RuntimeError("fail"))
        phase = ReflectPhase(mock_brain)
        # Should not raise
        await phase.process(_perception("Hello"), _response(), MIND, CONV)


# ── Hebbian learning ──────────────────────────────────────────────────


class TestHebbianLearning:
    """Hebbian strengthening between co-mentioned concepts."""

    async def test_hebbian_with_multiple_concepts(self, mock_brain: AsyncMock) -> None:
        phase = ReflectPhase(mock_brain)
        # Both entity and preference
        await phase.process(
            _perception("My name is Guipe and I love coding"), _response(), MIND, CONV
        )
        # learn_concept called twice
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
            _perception("My name is Guipe and I love coding"), _response(), MIND, CONV
        )
        # Should not crash


# ── Failure resilience ────────────────────────────────────────────────


class TestConceptExtractionFailure:
    """Graceful handling of concept learning failures."""

    async def test_learn_failure_continues(self, mock_brain: AsyncMock) -> None:
        mock_brain.learn_concept = AsyncMock(side_effect=RuntimeError("fail"))
        phase = ReflectPhase(mock_brain)
        # Should not crash, episode still created
        await phase.process(_perception("My name is Test"), _response(), MIND, CONV)
        mock_brain.encode_episode.assert_called_once()
