"""Tests for sovyx.cognitive.think — ThinkPhase."""

from __future__ import annotations

from unittest.mock import AsyncMock

from sovyx.cognitive.perceive import Perception
from sovyx.cognitive.think import ThinkPhase
from sovyx.context.assembler import AssembledContext
from sovyx.engine.types import MindId, PerceptionType
from sovyx.llm.models import LLMResponse
from sovyx.mind.config import MindConfig

MIND = MindId("aria")


def _perception(content: str = "Hello", complexity: float = 0.5) -> Perception:
    return Perception(
        id="p1",
        type=PerceptionType.USER_MESSAGE,
        source="telegram",
        content=content,
        metadata={"complexity": complexity},
    )


def _mock_assembler() -> AsyncMock:
    a = AsyncMock()
    a.assemble = AsyncMock(
        return_value=AssembledContext(
            messages=[
                {"role": "system", "content": "You are Aria"},
                {"role": "user", "content": "Hello"},
            ],
            tokens_used=50,
            token_budget=128_000,
            sources=["personality"],
            budget_breakdown={},
        )
    )
    return a


def _mock_router(response: LLMResponse | None = None) -> AsyncMock:
    r = AsyncMock()
    r.get_context_window = lambda m=None: 128_000
    r.generate = AsyncMock(
        return_value=response
        or LLMResponse(
            content="Hi there!",
            model="claude-sonnet-4-20250514",
            tokens_in=10,
            tokens_out=5,
            latency_ms=200,
            cost_usd=0.001,
            finish_reason="stop",
            provider="anthropic",
        )
    )
    return r


class TestThinkPhase:
    """ThinkPhase processing."""

    async def test_returns_response_and_messages(self) -> None:
        phase = ThinkPhase(_mock_assembler(), _mock_router(), MindConfig(name="Aria"))
        response, messages = await phase.process(_perception(), MIND, [])
        assert response.content == "Hi there!"
        assert len(messages) == 2  # noqa: PLR2004

    async def test_includes_person_name(self) -> None:
        assembler = _mock_assembler()
        phase = ThinkPhase(assembler, _mock_router(), MindConfig(name="Aria"))
        await phase.process(_perception(), MIND, [], person_name="Guipe")
        call_kwargs = assembler.assemble.call_args.kwargs
        assert call_kwargs["person_name"] == "Guipe"

    async def test_brain_recall_via_assembler(self) -> None:
        assembler = _mock_assembler()
        phase = ThinkPhase(assembler, _mock_router(), MindConfig(name="Aria"))
        await phase.process(_perception(), MIND, [{"role": "user", "content": "prev"}])
        assembler.assemble.assert_called_once()

    async def test_llm_failure_returns_degraded(self) -> None:
        router = _mock_router()
        router.generate = AsyncMock(side_effect=RuntimeError("LLM down"))
        phase = ThinkPhase(_mock_assembler(), router, MindConfig(name="Aria"))

        response, messages = await phase.process(_perception(), MIND, [])
        assert response.finish_reason == "error"
        assert "trouble" in response.content
        assert messages == []

    async def test_custom_degradation_message(self) -> None:
        router = _mock_router()
        router.generate = AsyncMock(side_effect=RuntimeError("fail"))
        phase = ThinkPhase(
            _mock_assembler(),
            router,
            MindConfig(name="Aria"),
            degradation_message="Oops!",
        )
        response, _ = await phase.process(_perception(), MIND, [])
        assert response.content == "Oops!"


class TestModelSelection:
    """Model routing by complexity."""

    def test_low_complexity_fast_model(self) -> None:
        phase = ThinkPhase(_mock_assembler(), _mock_router(), MindConfig(name="Aria"))
        assert phase._select_model(0.1) == "claude-3-5-haiku-20241022"

    def test_high_complexity_default_model(self) -> None:
        phase = ThinkPhase(_mock_assembler(), _mock_router(), MindConfig(name="Aria"))
        assert phase._select_model(0.5) == "claude-sonnet-4-20250514"

    def test_boundary_complexity(self) -> None:
        phase = ThinkPhase(_mock_assembler(), _mock_router(), MindConfig(name="Aria"))
        assert phase._select_model(0.3) == "claude-sonnet-4-20250514"
        assert phase._select_model(0.29) == "claude-3-5-haiku-20241022"

    async def test_model_passed_to_router(self) -> None:
        router = _mock_router()
        phase = ThinkPhase(_mock_assembler(), router, MindConfig(name="Aria"))
        await phase.process(_perception(complexity=0.1), MIND, [])
        call_kwargs = router.generate.call_args.kwargs
        assert call_kwargs["model"] == "claude-3-5-haiku-20241022"
