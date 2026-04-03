"""Tests for sovyx.context.assembler — Context assembly."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from sovyx.context.assembler import AssembledContext, ContextAssembler
from sovyx.context.budget import TokenBudgetManager
from sovyx.context.formatter import ContextFormatter
from sovyx.context.tokenizer import TokenCounter
from sovyx.engine.types import MindId
from sovyx.mind.config import MindConfig
from sovyx.mind.personality import PersonalityEngine

MIND = MindId("aria")


@pytest.fixture
def counter() -> TokenCounter:
    return TokenCounter()


@pytest.fixture
def mind_config() -> MindConfig:
    return MindConfig(name="Aria")


@pytest.fixture
def personality(mind_config: MindConfig) -> PersonalityEngine:
    return PersonalityEngine(mind_config)


@pytest.fixture
def mock_brain() -> AsyncMock:
    brain = AsyncMock()
    brain.recall = AsyncMock(return_value=([], []))
    return brain


@pytest.fixture
def assembler(
    counter: TokenCounter,
    personality: PersonalityEngine,
    mock_brain: AsyncMock,
    mind_config: MindConfig,
) -> ContextAssembler:
    return ContextAssembler(
        token_counter=counter,
        personality_engine=personality,
        brain_service=mock_brain,
        budget_manager=TokenBudgetManager(),
        formatter=ContextFormatter(counter),
        mind_config=mind_config,
    )


class TestAssemble:
    """Context assembly."""

    async def test_basic_assembly(self, assembler: ContextAssembler) -> None:
        ctx = await assembler.assemble(
            mind_id=MIND,
            current_message="Hello!",
            conversation_history=[],
        )
        assert isinstance(ctx, AssembledContext)
        assert len(ctx.messages) >= 2  # system + user  # noqa: PLR2004
        assert ctx.messages[-1]["content"] == "Hello!"
        assert ctx.messages[0]["role"] == "system"

    async def test_system_prompt_always_present(self, assembler: ContextAssembler) -> None:
        ctx = await assembler.assemble(MIND, "test", [], context_window=4096)
        system = ctx.messages[0]["content"]
        assert "Aria" in system

    async def test_current_message_always_present(self, assembler: ContextAssembler) -> None:
        ctx = await assembler.assemble(MIND, "my message", [])
        assert ctx.messages[-1]["content"] == "my message"

    async def test_temporal_in_system(self, assembler: ContextAssembler) -> None:
        ctx = await assembler.assemble(MIND, "test", [])
        system = ctx.messages[0]["content"]
        assert "Current date" in system

    async def test_person_name_appended(self, assembler: ContextAssembler) -> None:
        ctx = await assembler.assemble(MIND, "test", [], person_name="Guipe")
        system = ctx.messages[0]["content"]
        assert "Guipe" in system

    async def test_history_included(self, assembler: ContextAssembler) -> None:
        history = [
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello!"},
        ]
        ctx = await assembler.assemble(MIND, "How are you?", history)
        # system + 2 history + current = 4
        assert len(ctx.messages) >= 4  # noqa: PLR2004

    async def test_history_not_mutated(self, assembler: ContextAssembler) -> None:
        """v12 fix: original history list never mutated."""
        history = [
            {"role": "user", "content": "msg1"},
            {"role": "assistant", "content": "resp1"},
            {"role": "user", "content": "msg2"},
            {"role": "assistant", "content": "resp2"},
        ]
        original_len = len(history)
        await assembler.assemble(MIND, "test", history, context_window=4096)
        assert len(history) == original_len

    async def test_tokens_used_positive(self, assembler: ContextAssembler) -> None:
        ctx = await assembler.assemble(MIND, "test", [])
        assert ctx.tokens_used > 0

    async def test_budget_breakdown_present(self, assembler: ContextAssembler) -> None:
        ctx = await assembler.assemble(MIND, "test", [])
        assert "system_prompt" in ctx.budget_breakdown
        assert "conversation" in ctx.budget_breakdown
        assert "response_reserve" in ctx.budget_breakdown

    async def test_sources_list(self, assembler: ContextAssembler) -> None:
        ctx = await assembler.assemble(MIND, "test", [])
        assert "personality" in ctx.sources


class TestTrimHistory:
    """History trimming."""

    def test_empty_history(self, assembler: ContextAssembler) -> None:
        result = assembler._trim_history([], 1000)
        assert result == []

    def test_fits_all(self, assembler: ContextAssembler) -> None:
        history = [{"role": "user", "content": "hi"}]
        result = assembler._trim_history(history, 1000)
        assert len(result) == 1

    def test_trims_oldest(self, assembler: ContextAssembler) -> None:
        history = [{"role": "user", "content": f"message {i} " * 20} for i in range(20)]
        result = assembler._trim_history(history, 500)
        assert len(result) < len(history)
        # Most recent kept
        assert result[-1] == history[-1]

    def test_returns_new_list(self, assembler: ContextAssembler) -> None:
        history = [{"role": "user", "content": "test"}]
        result = assembler._trim_history(history, 1000)
        assert result is not history
