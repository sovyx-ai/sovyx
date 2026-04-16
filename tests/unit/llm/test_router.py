"""Tests for sovyx.llm.router — LLM Router with failover."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from sovyx.engine.errors import CostLimitExceededError, ProviderUnavailableError
from sovyx.llm.cost import CostGuard
from sovyx.llm.models import LLMResponse
from sovyx.llm.router import (
    ComplexityLevel,
    ComplexitySignals,
    LLMRouter,
    classify_complexity,
    extract_signals,
    select_model_for_complexity,
)


def _mock_provider(
    name: str = "test",
    available: bool = True,
    supports: bool = True,
    response: LLMResponse | None = None,
    error: Exception | None = None,
) -> AsyncMock:
    p = AsyncMock()
    p.name = name
    p.is_available = available
    p.supports_model = lambda m: supports
    p.get_context_window = lambda m=None: 128_000
    p.close = AsyncMock()

    if error:
        p.generate = AsyncMock(side_effect=error)
    else:
        p.generate = AsyncMock(
            return_value=response
            or LLMResponse(
                content="Hello",
                model="test-model",
                tokens_in=10,
                tokens_out=5,
                latency_ms=100,
                cost_usd=0.001,
                finish_reason="stop",
                provider=name,
            )
        )
    return p


@pytest.fixture
def cost_guard() -> CostGuard:
    return CostGuard(daily_budget=10.0, per_conversation_budget=2.0)


@pytest.fixture
def event_bus() -> AsyncMock:
    bus = AsyncMock()
    bus.emit = AsyncMock()
    return bus


class TestBasicRouting:
    """Basic provider routing."""

    async def test_route_to_first_provider(
        self, cost_guard: CostGuard, event_bus: AsyncMock
    ) -> None:
        p1 = _mock_provider("anthropic")
        router = LLMRouter([p1], cost_guard, event_bus)

        result = await router.generate([{"role": "user", "content": "Hi"}])
        assert result.content == "Hello"
        assert result.provider == "anthropic"

    async def test_model_routing(self, cost_guard: CostGuard, event_bus: AsyncMock) -> None:
        p1 = _mock_provider("anthropic", supports=True)
        p2 = _mock_provider("openai", supports=False)
        router = LLMRouter([p2, p1], cost_guard, event_bus)

        result = await router.generate([{"role": "user", "content": "Hi"}], model="claude-sonnet")
        assert result.provider == "anthropic"


class TestFailover:
    """Provider failover."""

    async def test_failover_to_second(self, cost_guard: CostGuard, event_bus: AsyncMock) -> None:
        p1 = _mock_provider("anthropic", error=RuntimeError("down"))
        p2 = _mock_provider("openai")
        router = LLMRouter([p1, p2], cost_guard, event_bus)

        result = await router.generate([{"role": "user", "content": "Hi"}])
        assert result.provider == "openai"

    async def test_all_failed(self, cost_guard: CostGuard, event_bus: AsyncMock) -> None:
        p1 = _mock_provider("anthropic", error=RuntimeError("down"))
        p2 = _mock_provider("openai", error=RuntimeError("down"))
        router = LLMRouter([p1, p2], cost_guard, event_bus)

        with pytest.raises(ProviderUnavailableError, match="All providers"):
            await router.generate([{"role": "user", "content": "Hi"}])

    async def test_skip_unavailable(self, cost_guard: CostGuard, event_bus: AsyncMock) -> None:
        p1 = _mock_provider("anthropic", available=False)
        p2 = _mock_provider("openai")
        router = LLMRouter([p1, p2], cost_guard, event_bus)

        result = await router.generate([{"role": "user", "content": "Hi"}])
        assert result.provider == "openai"

    async def test_no_providers(self, cost_guard: CostGuard, event_bus: AsyncMock) -> None:
        router = LLMRouter([], cost_guard, event_bus)
        with pytest.raises(ProviderUnavailableError):
            await router.generate([{"role": "user", "content": "Hi"}])


class TestCostGuard:
    """Cost control integration."""

    async def test_blocks_when_over_budget(self, event_bus: AsyncMock) -> None:
        guard = CostGuard(daily_budget=0.005, per_conversation_budget=0.005)
        await guard.record(0.005, "model", "conv1")

        p1 = _mock_provider("test")
        router = LLMRouter([p1], guard, event_bus)

        with pytest.raises(CostLimitExceededError, match="Budget"):
            await router.generate([{"role": "user", "content": "Hi"}])

    async def test_records_cost_after_success(self, event_bus: AsyncMock) -> None:
        guard = CostGuard(daily_budget=10.0, per_conversation_budget=2.0)
        p1 = _mock_provider("test")
        router = LLMRouter([p1], guard, event_bus)

        await router.generate([{"role": "user", "content": "Hi"}], conversation_id="conv1")
        assert guard.get_daily_spend() > 0


class TestCircuitBreaker:
    """Circuit breaker integration."""

    async def test_circuit_opens_after_failures(
        self, cost_guard: CostGuard, event_bus: AsyncMock
    ) -> None:
        p1 = _mock_provider("anthropic", error=RuntimeError("fail"))
        p2 = _mock_provider("openai")
        router = LLMRouter([p1, p2], cost_guard, event_bus, circuit_breaker_failures=2)

        # 2 failures open the circuit
        await router.generate([{"role": "user", "content": "1"}])
        await router.generate([{"role": "user", "content": "2"}])

        # 3rd call: p1 circuit open, goes to p2
        circuit = router._circuits["anthropic"]
        assert circuit.state == "open"


class TestContextWindow:
    """get_context_window() resolution."""

    def test_resolve_by_model(self) -> None:
        p1 = AsyncMock()
        p1.name = "anthropic"
        p1.supports_model = lambda m: m.startswith("claude")
        p1.get_context_window = lambda m=None: 200_000
        router = LLMRouter([p1], CostGuard(10, 2), AsyncMock())

        assert router.get_context_window("claude-sonnet") == 200_000

    def test_fallback(self) -> None:
        router = LLMRouter([], CostGuard(10, 2), AsyncMock())
        assert router.get_context_window("unknown") == 128_000


class TestStop:
    """Router stop/cleanup."""

    async def test_stop_closes_all(self, cost_guard: CostGuard, event_bus: AsyncMock) -> None:
        p1 = _mock_provider("p1")
        p2 = _mock_provider("p2")
        router = LLMRouter([p1, p2], cost_guard, event_bus)

        await router.stop()
        p1.close.assert_called_once()
        p2.close.assert_called_once()

    async def test_stop_best_effort(self, cost_guard: CostGuard, event_bus: AsyncMock) -> None:
        p1 = _mock_provider("p1")
        p1.close = AsyncMock(side_effect=RuntimeError("fail"))
        p2 = _mock_provider("p2")
        router = LLMRouter([p1, p2], cost_guard, event_bus)

        # Should not raise
        await router.stop()
        p2.close.assert_called_once()


class TestEventEmission:
    """Event emission after LLM call."""

    async def test_emits_think_completed(
        self, cost_guard: CostGuard, event_bus: AsyncMock
    ) -> None:
        p1 = _mock_provider("test")
        router = LLMRouter([p1], cost_guard, event_bus)

        await router.generate([{"role": "user", "content": "Hi"}])
        event_bus.emit.assert_called_once()

    async def test_think_completed_includes_cost(
        self, cost_guard: CostGuard, event_bus: AsyncMock
    ) -> None:
        """ThinkCompleted event must include cost_usd from provider response."""

        p1 = _mock_provider("test")
        router = LLMRouter([p1], cost_guard, event_bus)

        await router.generate([{"role": "user", "content": "Hi"}])
        event_bus.emit.assert_called_once()

        emitted_event = event_bus.emit.call_args[0][0]
        # Anti-pattern #8: isinstance unreliable under pytest-cov reimport.
        assert type(emitted_event).__name__ == "ThinkCompleted"
        assert emitted_event.cost_usd == 0.001, (  # noqa: PLR2004
            f"cost_usd should be 0.001 from mock, got {emitted_event.cost_usd}"
        )


class TestNonLLMResponseConversion:
    """Provider returns a non-LLMResponse object (converted via vars())."""

    async def test_raw_object_converted(self, cost_guard: CostGuard, event_bus: AsyncMock) -> None:
        """When provider returns a non-LLMResponse, router wraps it."""
        import dataclasses

        @dataclasses.dataclass
        class RawResponse:
            content: str = "raw"
            model: str = "m"
            tokens_in: int = 5
            tokens_out: int = 3
            latency_ms: int = 50
            cost_usd: float = 0.0005
            finish_reason: str = "stop"
            provider: str = "raw-prov"
            tool_calls: list[object] | None = None

        p1 = _mock_provider("test")
        p1.generate = AsyncMock(return_value=RawResponse())
        router = LLMRouter([p1], cost_guard, event_bus)

        result = await router.generate([{"role": "user", "content": "Hi"}])
        assert isinstance(result, LLMResponse)
        assert result.content == "raw"
        assert result.provider == "raw-prov"


class TestCircuitOpenSkip:
    """Circuit open causes provider skip with error message."""

    async def test_circuit_open_skips_and_reports(
        self, cost_guard: CostGuard, event_bus: AsyncMock
    ) -> None:
        p1 = _mock_provider("anthropic", error=RuntimeError("fail"))
        router = LLMRouter([p1], cost_guard, event_bus, circuit_breaker_failures=1)

        # First call fails, opens circuit
        with pytest.raises(ProviderUnavailableError, match="All providers"):
            await router.generate([{"role": "user", "content": "1"}])

        # Second call: circuit open, no provider available
        with pytest.raises(ProviderUnavailableError, match="circuit open"):
            await router.generate([{"role": "user", "content": "2"}])


class TestPricingTable:
    """_get_pricing coverage for known and unknown models."""

    def test_known_model(self) -> None:
        assert LLMRouter._get_pricing("gpt-4o") == (2.5, 10.0)

    def test_unknown_model(self) -> None:
        assert LLMRouter._get_pricing("some-unknown-model") == (3.0, 15.0)

    def test_none_model(self) -> None:
        assert LLMRouter._get_pricing(None) == (3.0, 15.0)

    def test_all_known_models_have_pricing(self) -> None:
        known = [
            "claude-sonnet-4-20250514",
            "claude-3-5-haiku-20241022",
            "claude-opus-4-20250514",
            "gpt-4o",
            "gpt-4o-mini",
            "o1",
            "o3-mini",
        ]
        for m in known:
            inp, out = LLMRouter._get_pricing(m)
            assert inp > 0
            assert out > 0


class TestGetContextWindowNoModel:
    """get_context_window with no model arg."""

    def test_no_model_returns_default(self) -> None:
        router = LLMRouter([], CostGuard(10, 2), AsyncMock())
        assert router.get_context_window() == 128_000

    def test_no_model_none_explicit(self) -> None:
        router = LLMRouter([], CostGuard(10, 2), AsyncMock())
        assert router.get_context_window(None) == 128_000


class TestComplexitySignals:
    """Tests for ComplexitySignals dataclass."""

    def test_defaults(self) -> None:
        s = ComplexitySignals()
        assert s.message_length == 0
        assert s.turn_count == 0
        assert s.has_tool_use is False
        assert s.has_code is False
        assert s.explicit_model is False


class TestClassifyComplexity:
    """Tests for classify_complexity function."""

    def test_short_message_is_simple(self) -> None:
        result = classify_complexity(ComplexitySignals(message_length=100, turn_count=1))
        assert result == ComplexityLevel.SIMPLE

    def test_long_message_is_complex(self) -> None:
        result = classify_complexity(ComplexitySignals(message_length=3000, turn_count=10))
        assert result == ComplexityLevel.COMPLEX

    def test_tool_use_is_complex(self) -> None:
        result = classify_complexity(ComplexitySignals(has_tool_use=True))
        assert result == ComplexityLevel.COMPLEX

    def test_code_is_complex(self) -> None:
        result = classify_complexity(ComplexitySignals(has_code=True))
        assert result == ComplexityLevel.COMPLEX

    def test_medium_is_moderate(self) -> None:
        result = classify_complexity(ComplexitySignals(message_length=1000, turn_count=5))
        assert result == ComplexityLevel.MODERATE

    def test_explicit_model_is_moderate(self) -> None:
        result = classify_complexity(ComplexitySignals(explicit_model=True))
        assert result == ComplexityLevel.MODERATE

    def test_many_turns_is_complex(self) -> None:
        result = classify_complexity(ComplexitySignals(message_length=1000, turn_count=10))
        assert result == ComplexityLevel.COMPLEX

    def test_thresholds_come_from_tuning_config(self) -> None:
        """Thresholds are sourced from LLMTuningConfig, not hardcoded."""
        from sovyx.engine.config import LLMTuningConfig

        cfg = LLMTuningConfig()
        assert cfg.simple_max_length == 500  # noqa: PLR2004
        assert cfg.simple_max_turns == 3  # noqa: PLR2004
        assert cfg.complex_min_length == 2000  # noqa: PLR2004
        assert cfg.complex_min_turns == 8  # noqa: PLR2004


class TestExtractSignals:
    """Tests for extract_signals function."""

    def test_basic(self) -> None:
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
        ]
        signals = extract_signals(messages)
        assert signals.message_length == len("Hello") + len("Hi there")
        assert signals.turn_count == 2
        assert signals.has_code is False

    def test_code_detection(self) -> None:
        messages = [{"role": "user", "content": "```python\ndef foo():\n    pass\n```"}]
        signals = extract_signals(messages)
        assert signals.has_code is True

    def test_def_detection(self) -> None:
        messages = [{"role": "user", "content": "def calculate_tax(amount):"}]
        signals = extract_signals(messages)
        assert signals.has_code is True

    def test_system_not_counted_as_turn(self) -> None:
        messages = [
            {"role": "system", "content": "You are helpful"},
            {"role": "user", "content": "Hello"},
        ]
        signals = extract_signals(messages)
        assert signals.turn_count == 1

    def test_empty_messages(self) -> None:
        signals = extract_signals([])
        assert signals.message_length == 0
        assert signals.turn_count == 0


class TestSelectModel:
    """Tests for select_model_for_complexity function."""

    def test_simple_selects_cheap(self) -> None:
        available = ["claude-sonnet-4-20250514", "gemini-2.0-flash", "gpt-4o"]
        model = select_model_for_complexity(ComplexityLevel.SIMPLE, available)
        assert model == "gemini-2.0-flash"

    def test_complex_selects_powerful(self) -> None:
        available = ["gemini-2.0-flash", "claude-sonnet-4-20250514", "gpt-4o-mini"]
        model = select_model_for_complexity(ComplexityLevel.COMPLEX, available)
        assert model == "claude-sonnet-4-20250514"

    def test_moderate_selects_first(self) -> None:
        available = ["gemini-2.0-flash", "gpt-4o"]
        model = select_model_for_complexity(ComplexityLevel.MODERATE, available)
        assert model == "gemini-2.0-flash"

    def test_empty_available_returns_none(self) -> None:
        model = select_model_for_complexity(ComplexityLevel.SIMPLE, [])
        assert model is None

    def test_no_tier_match_returns_first(self) -> None:
        available = ["some-custom-model"]
        model = select_model_for_complexity(ComplexityLevel.SIMPLE, available)
        assert model == "some-custom-model"


# ── Tools Pass-Through (TASK-437) ───────────────────────────────────


class TestToolsPassThrough:
    """Tests for tools parameter in LLMRouter.generate()."""

    @pytest.mark.anyio()
    async def test_tools_passed_to_provider(self) -> None:
        """Tools dict is forwarded to provider.generate()."""
        provider = _mock_provider()
        cg = CostGuard(daily_budget=10.0, per_conversation_budget=2.0)
        bus = AsyncMock()
        bus.emit = AsyncMock()
        router = LLMRouter([provider], cg, bus)

        tools = [
            {"name": "weather.get", "description": "Get weather", "parameters": {}},
        ]
        await router.generate(
            [{"role": "user", "content": "hi"}],
            model="test-model",
            tools=tools,
        )

        # Verify tools were passed to provider
        call_kwargs = provider.generate.call_args
        assert call_kwargs[1].get("tools") == tools

    @pytest.mark.anyio()
    async def test_no_tools_passes_none(self) -> None:
        """Without tools, None is passed to provider."""
        provider = _mock_provider()
        cg = CostGuard(daily_budget=10.0, per_conversation_budget=2.0)
        bus = AsyncMock()
        bus.emit = AsyncMock()
        router = LLMRouter([provider], cg, bus)

        await router.generate(
            [{"role": "user", "content": "hi"}],
            model="test-model",
        )

        call_kwargs = provider.generate.call_args
        assert call_kwargs[1].get("tools") is None

    @pytest.mark.anyio()
    async def test_tool_calls_in_response(self) -> None:
        """LLMResponse with tool_calls is returned intact."""
        from sovyx.llm.models import ToolCall

        provider = _mock_provider(
            response=LLMResponse(
                content="",
                model="test-model",
                tokens_in=10,
                tokens_out=5,
                latency_ms=100,
                cost_usd=0.001,
                finish_reason="tool_use",
                provider="test",
                tool_calls=[
                    ToolCall(id="c1", function_name="weather.get", arguments={"city": "X"})
                ],
            ),
        )
        cg = CostGuard(daily_budget=10.0, per_conversation_budget=2.0)
        bus = AsyncMock()
        bus.emit = AsyncMock()
        router = LLMRouter([provider], cg, bus)

        result = await router.generate(
            [{"role": "user", "content": "hi"}],
            model="test-model",
            tools=[{"name": "weather.get", "description": "Get", "parameters": {}}],
        )

        assert result.tool_calls is not None
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].function_name == "weather.get"


class TestToolDefinitionsToDicts:
    """Tests for LLMRouter.tool_definitions_to_dicts()."""

    def test_basic_conversion(self) -> None:
        """ToolDefinition-like objects converted to dicts."""

        class FakeToolDef:
            def __init__(self, name: str, description: str, parameters: dict) -> None:
                self.name = name
                self.description = description
                self.parameters = parameters

        tools = [
            FakeToolDef("weather.get", "Get weather", {"type": "object"}),
            FakeToolDef("timer.set", "Set timer", {}),
        ]
        result = LLMRouter.tool_definitions_to_dicts(tools)
        assert len(result) == 2
        assert result[0]["name"] == "weather.get"
        assert result[0]["parameters"] == {"type": "object"}

    def test_empty(self) -> None:
        assert LLMRouter.tool_definitions_to_dicts([]) == []

    def test_missing_attributes(self) -> None:
        """Objects missing attributes get empty defaults."""

        class Bare:
            pass

        result = LLMRouter.tool_definitions_to_dicts([Bare()])
        assert result[0]["name"] == ""
        assert result[0]["description"] == ""
        assert result[0]["parameters"] == {}


class TestContextWindowEquivalent:
    """Test context window lookup via equivalent models."""

    def test_equivalent_model_context_window(self) -> None:
        """Context window falls back to equivalent model's provider."""
        # Provider supports gpt-4o but NOT claude-sonnet
        provider = _mock_provider(name="openai")
        provider.supports_model = lambda m: m.startswith("gpt-")
        provider.get_context_window = lambda m=None: 128_000

        cg = CostGuard(daily_budget=10.0, per_conversation_budget=2.0)
        bus = AsyncMock()
        bus.emit = AsyncMock()
        router = LLMRouter([provider], cg, bus)

        # claude-sonnet → equivalent gpt-4o → OpenAI provider
        result = router.get_context_window("claude-sonnet-4-20250514")
        assert result == 128_000
