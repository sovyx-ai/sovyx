"""VAL-29: Cognitive pipeline expanded — perceive, context, tokenizer integration.

Tests the perception → gate → context assembly flow with real components.
"""

from __future__ import annotations

import pytest

from sovyx.cognitive.perceive import PerceivePhase, Perception
from sovyx.context.budget import TokenBudgetManager
from sovyx.context.tokenizer import TokenCounter
from sovyx.engine.types import PerceptionType


class TestPerceivePhase:
    """Perception validation and enrichment with real components."""

    async def test_valid_perception_passes(self) -> None:
        phase = PerceivePhase()
        p = Perception(
            id="p-1",
            type=PerceptionType.USER_MESSAGE,
            source="telegram",
            content="Hello, how are you?",
            person_id="user-1",
        )
        result = await phase.process(p)
        assert result is not None
        assert result.content == "Hello, how are you?"

    async def test_empty_content_raises(self) -> None:
        from sovyx.engine.errors import PerceptionError

        phase = PerceivePhase()
        p = Perception(
            id="p-2",
            type=PerceptionType.USER_MESSAGE,
            source="telegram",
            content="",
            person_id="user-1",
        )
        with pytest.raises(PerceptionError, match="empty"):
            await phase.process(p)

    async def test_long_content_truncated(self) -> None:
        phase = PerceivePhase()
        long_text = "x" * 100_000
        p = Perception(
            id="p-3",
            type=PerceptionType.USER_MESSAGE,
            source="telegram",
            content=long_text,
            person_id="user-1",
        )
        result = await phase.process(p)
        assert result is not None
        assert len(result.content) < len(long_text)

    async def test_whitespace_stripped(self) -> None:
        phase = PerceivePhase()
        p = Perception(
            id="p-4",
            type=PerceptionType.USER_MESSAGE,
            source="cli",
            content="  Hello world  \n\t",
            person_id="user-1",
        )
        result = await phase.process(p)
        assert result is not None
        assert result.content == "Hello world"


class TestTokenBudgetIntegration:
    """TokenBudgetManager with real TokenCounter."""

    def test_budget_allocation(self) -> None:
        budget = TokenBudgetManager()
        alloc = budget.allocate(
            conversation_length=5,
            brain_result_count=3,
            complexity=0.5,
            context_window=4096,
        )
        assert alloc.system_prompt > 0
        assert alloc.conversation > 0
        assert alloc.memory_concepts > 0
        assert alloc.total <= 4096

    def test_token_counting_accuracy(self) -> None:
        counter = TokenCounter()
        # Known English text should tokenize consistently
        text = "Hello, world! This is a test."
        tokens = counter.count(text)
        assert tokens > 0
        assert tokens < 20  # Should be ~8-10 tokens

    def test_truncation_preserves_meaning(self) -> None:
        counter = TokenCounter()
        text = "This is a long sentence that should be truncated at some point"
        truncated = counter.truncate(text, 5)
        assert counter.count(truncated) <= 5
        assert len(truncated) > 0
        assert truncated.startswith("This")
