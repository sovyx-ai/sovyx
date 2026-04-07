"""Tests for sovyx.llm.router — LLM Router with failover."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from sovyx.engine.errors import CostLimitExceededError, ProviderUnavailableError
from sovyx.llm.cost import CostGuard
from sovyx.llm.models import LLMResponse
from sovyx.llm.router import LLMRouter


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


class TestNonLLMResponseConversion:
    """Provider returns a non-LLMResponse object (converted via vars())."""

    async def test_raw_object_converted(
        self, cost_guard: CostGuard, event_bus: AsyncMock
    ) -> None:
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
        assert LLMRouter._get_pricing("gpt-4o") == (5.0, 15.0)

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
