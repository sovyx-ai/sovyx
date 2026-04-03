"""Tests for sovyx.cognitive.reflect — ReflectPhase."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from sovyx.cognitive.perceive import Perception
from sovyx.cognitive.reflect import ReflectPhase
from sovyx.engine.types import (
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


@pytest.fixture
def mock_brain() -> AsyncMock:
    brain = AsyncMock()
    brain.learn_concept = AsyncMock(return_value=ConceptId("c1"))
    brain.encode_episode = AsyncMock()
    brain.strengthen_connection = AsyncMock()
    return brain


class TestConceptExtraction:
    """Concept extraction from user input."""

    async def test_entity_english(self, mock_brain: AsyncMock) -> None:
        phase = ReflectPhase(mock_brain)
        await phase.process(_perception("My name is Guipe"), _response(), MIND, CONV)
        mock_brain.learn_concept.assert_called_once()
        call_kwargs = mock_brain.learn_concept.call_args.kwargs
        assert call_kwargs["name"] == "Guipe"

    async def test_entity_portuguese(self, mock_brain: AsyncMock) -> None:
        phase = ReflectPhase(mock_brain)
        await phase.process(_perception("Meu nome é Renan"), _response(), MIND, CONV)
        mock_brain.learn_concept.assert_called_once()
        call_kwargs = mock_brain.learn_concept.call_args.kwargs
        assert call_kwargs["name"] == "Renan"

    async def test_preference_english(self, mock_brain: AsyncMock) -> None:
        phase = ReflectPhase(mock_brain)
        await phase.process(_perception("I love coffee"), _response(), MIND, CONV)
        mock_brain.learn_concept.assert_called_once()

    async def test_preference_portuguese(self, mock_brain: AsyncMock) -> None:
        phase = ReflectPhase(mock_brain)
        await phase.process(_perception("Eu gosto de café"), _response(), MIND, CONV)
        mock_brain.learn_concept.assert_called_once()

    async def test_fact_english(self, mock_brain: AsyncMock) -> None:
        phase = ReflectPhase(mock_brain)
        await phase.process(_perception("I work at Google"), _response(), MIND, CONV)
        mock_brain.learn_concept.assert_called_once()

    async def test_no_concepts_extracted(self, mock_brain: AsyncMock) -> None:
        phase = ReflectPhase(mock_brain)
        await phase.process(_perception("What time is it?"), _response(), MIND, CONV)
        mock_brain.learn_concept.assert_not_called()


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


class TestConceptExtractionFailure:
    """Graceful handling of concept learning failures."""

    async def test_learn_failure_continues(self, mock_brain: AsyncMock) -> None:
        mock_brain.learn_concept = AsyncMock(side_effect=RuntimeError("fail"))
        phase = ReflectPhase(mock_brain)
        # Should not crash, episode still created
        await phase.process(_perception("My name is Test"), _response(), MIND, CONV)
        mock_brain.encode_episode.assert_called_once()
