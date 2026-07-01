"""Tests for sovyx.cognitive.think — ThinkPhase."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

if TYPE_CHECKING:
    import pytest

from sovyx.cognitive.perceive import Perception
from sovyx.cognitive.think import (
    DEGRADED_MODEL,
    DEGRADED_PROVIDER,
    ThinkPhase,
    is_degraded_llm_response,
)
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
        # W1.2 — the fallback carries the SSoT degradation sentinel so the
        # cognitive loop can mark the ActionResult honestly downstream.
        assert response.model == DEGRADED_MODEL
        assert response.provider == DEGRADED_PROVIDER
        assert is_degraded_llm_response(
            model=response.model,
            provider=response.provider,
            finish_reason=response.finish_reason,
        )

    async def test_llm_failure_attaches_degraded_reason(self) -> None:
        """D1 — the fallback attributes WHICH exception degraded the turn:
        class name in ``degraded_reason``, sanitized summary in
        ``degraded_detail`` — so downstream metadata (and dashboards) can
        tell a provider outage from a real bug."""
        router = _mock_router()
        router.generate = AsyncMock(side_effect=KeyError("missing_provider_config"))
        phase = ThinkPhase(_mock_assembler(), router, MindConfig(name="Aria"))

        response, _ = await phase.process(_perception(), MIND, [])
        assert response.degraded_reason == "KeyError"
        assert response.degraded_detail is not None
        assert "missing_provider_config" in response.degraded_detail

    async def test_pre_llm_failure_attaches_degraded_reason(self) -> None:
        """Failures BEFORE the LLM call (context assembly) are attributed
        too — the whole try block funnels into one fallback."""
        assembler = _mock_assembler()
        assembler.assemble = AsyncMock(side_effect=ValueError("assembler exploded"))
        phase = ThinkPhase(assembler, _mock_router(), MindConfig(name="Aria"))

        response, _ = await phase.process(_perception(), MIND, [])
        assert response.model == DEGRADED_MODEL
        assert response.degraded_reason == "ValueError"

    async def test_degraded_detail_is_single_line_and_bounded(self) -> None:
        """The sanitized summary collapses whitespace and is length-capped."""
        router = _mock_router()
        router.generate = AsyncMock(side_effect=RuntimeError("line one\nline two   " + "x" * 500))
        phase = ThinkPhase(_mock_assembler(), router, MindConfig(name="Aria"))

        response, _ = await phase.process(_perception(), MIND, [])
        assert response.degraded_detail is not None
        assert "\n" not in response.degraded_detail
        assert response.degraded_detail.startswith("line one line two")
        assert len(response.degraded_detail) <= 200  # noqa: PLR2004

    async def test_healthy_response_has_no_degraded_reason(self) -> None:
        phase = ThinkPhase(_mock_assembler(), _mock_router(), MindConfig(name="Aria"))
        response, _ = await phase.process(_perception(), MIND, [])
        assert response.degraded_reason is None
        assert response.degraded_detail is None

    async def test_streaming_failure_attaches_degraded_reason(self) -> None:
        """D1 streaming — the fallback chunk carries the same attribution
        so the loop's reconstructed LLMResponse inherits it."""
        assembler = _mock_assembler()
        assembler.assemble = AsyncMock(side_effect=ValueError("context window\nexploded"))
        phase = ThinkPhase(assembler, _mock_router(), MindConfig(name="Aria"))

        chunk_iter, messages = await phase.process_streaming(_perception(), MIND, [])
        chunks = [c async for c in chunk_iter]
        assert messages == []
        assert len(chunks) == 1
        chunk = chunks[0]
        assert chunk.model == DEGRADED_MODEL
        assert chunk.provider == DEGRADED_PROVIDER
        assert chunk.degraded_reason == "ValueError"
        assert chunk.degraded_detail == "context window exploded"

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


class TestIsDegradedLlmResponse:
    """W1.2 — the producer↔consumer SSoT helper (anti-pattern #53)."""

    def test_true_for_sentinel(self) -> None:
        assert is_degraded_llm_response(
            model=DEGRADED_MODEL, provider=DEGRADED_PROVIDER, finish_reason="error"
        )

    def test_false_for_normal_response(self) -> None:
        assert not is_degraded_llm_response(
            model="claude-opus-4-8", provider="anthropic", finish_reason="stop"
        )

    def test_false_when_only_finish_reason_error(self) -> None:
        # A real provider returning finish_reason="error" (e.g. content
        # filter) is NOT the ThinkPhase degradation sentinel.
        assert not is_degraded_llm_response(
            model="gpt-4o", provider="openai", finish_reason="error"
        )

    def test_false_when_provider_present_but_model_degraded(self) -> None:
        assert not is_degraded_llm_response(
            model=DEGRADED_MODEL, provider="anthropic", finish_reason="error"
        )


class TestModelSelection:
    """Model routing by complexity."""

    def test_low_complexity_fast_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        phase = ThinkPhase(_mock_assembler(), _mock_router(), MindConfig(name="Aria"))
        assert phase._select_model(0.1) == "claude-3-5-haiku-20241022"

    def test_high_complexity_default_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        phase = ThinkPhase(_mock_assembler(), _mock_router(), MindConfig(name="Aria"))
        assert phase._select_model(0.5) == "claude-sonnet-4-20250514"

    def test_boundary_complexity(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        phase = ThinkPhase(_mock_assembler(), _mock_router(), MindConfig(name="Aria"))
        assert phase._select_model(0.3) == "claude-sonnet-4-20250514"
        assert phase._select_model(0.29) == "claude-3-5-haiku-20241022"

    async def test_model_passed_to_router(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        router = _mock_router()
        phase = ThinkPhase(_mock_assembler(), router, MindConfig(name="Aria"))
        await phase.process(_perception(complexity=0.1), MIND, [])
        call_kwargs = router.generate.call_args.kwargs
        assert call_kwargs["model"] == "claude-3-5-haiku-20241022"


class TestThinkPhaseWithPlugins:
    """ThinkPhase passes tools= when PluginManager has plugins."""

    async def test_tools_passed_to_generate(self) -> None:
        from unittest.mock import MagicMock

        from sovyx.plugins.sdk import ToolDefinition

        assembler = _mock_assembler()
        router = _mock_router()
        config = MindConfig(name="Aria")

        # Mock PluginManager with tools
        pm = MagicMock()
        pm.plugin_count = 1
        pm.get_tool_definitions.return_value = [
            ToolDefinition(
                name="calculator.calculate",
                description="Calculate a math expression",
                parameters={"type": "object", "properties": {"expression": {"type": "string"}}},
            ),
        ]

        think = ThinkPhase(
            context_assembler=assembler,
            llm_router=router,
            mind_config=config,
            plugin_manager=pm,
        )

        response, _msgs = await think.process(
            _perception(), MIND, [{"role": "user", "content": "Hello"}]
        )

        # Verify tools= was passed to generate
        call_kwargs = router.generate.call_args[1]
        assert "tools" in call_kwargs
        assert call_kwargs["tools"] is not None
        assert len(call_kwargs["tools"]) == 1
        assert call_kwargs["tools"][0]["name"] == "calculator.calculate"

    async def test_no_tools_without_plugin_manager(self) -> None:
        assembler = _mock_assembler()
        router = _mock_router()
        config = MindConfig(name="Aria")

        think = ThinkPhase(
            context_assembler=assembler,
            llm_router=router,
            mind_config=config,
        )

        await think.process(_perception(), MIND, [{"role": "user", "content": "Hello"}])

        call_kwargs = router.generate.call_args[1]
        assert call_kwargs.get("tools") is None

    async def test_no_tools_with_empty_plugin_manager(self) -> None:
        from unittest.mock import MagicMock

        pm = MagicMock()
        pm.plugin_count = 0  # No plugins loaded

        assembler = _mock_assembler()
        router = _mock_router()
        config = MindConfig(name="Aria")

        think = ThinkPhase(
            context_assembler=assembler,
            llm_router=router,
            mind_config=config,
            plugin_manager=pm,
        )

        await think.process(_perception(), MIND, [{"role": "user", "content": "Hello"}])

        call_kwargs = router.generate.call_args[1]
        assert call_kwargs.get("tools") is None
