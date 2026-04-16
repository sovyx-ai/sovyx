"""VAL-09: Coverage gaps for router.py, circuit.py, cost.py."""

from __future__ import annotations

import json
import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest

from sovyx.llm.cost import CostGuard


class TestCostGuardRestoreGaps:
    @pytest.mark.asyncio()
    async def test_restore_no_pool(self) -> None:
        """restore() with no pool returns immediately."""
        guard = CostGuard(daily_budget=10.0, per_conversation_budget=2.0, system_pool=None)
        await guard.restore()
        assert guard.get_daily_spend() == 0.0

    @pytest.mark.asyncio()
    async def test_restore_stale_date(self) -> None:
        """restore() with state from a different day starts fresh."""
        cursor = AsyncMock()
        cursor.fetchone = AsyncMock(
            return_value=(json.dumps({"date": "2020-01-01", "daily_spend": 5.0}),)
        )
        conn = AsyncMock()
        conn.execute = AsyncMock(return_value=cursor)

        read_ctx = AsyncMock()
        read_ctx.__aenter__ = AsyncMock(return_value=conn)
        read_ctx.__aexit__ = AsyncMock(return_value=False)

        pool = MagicMock()
        pool.read = MagicMock(return_value=read_ctx)

        guard = CostGuard(daily_budget=10.0, per_conversation_budget=2.0, system_pool=pool)
        await guard.restore()
        assert guard.get_daily_spend() == 0.0

    @pytest.mark.asyncio()
    async def test_restore_exception(self) -> None:
        """restore() recovers from a typed DB exception.

        sqlite3.OperationalError is what aiosqlite forwards when the
        underlying DB is unreachable; the narrow catch in
        ``CostGuard.restore`` is scoped to sqlite3.Error + data-shape
        errors, so using the typed exception documents the real
        contract (programmer RuntimeErrors bubble up).
        """
        pool = MagicMock()
        pool.read = MagicMock(side_effect=sqlite3.OperationalError("db broken"))

        guard = CostGuard(daily_budget=10.0, per_conversation_budget=2.0, system_pool=pool)
        await guard.restore()
        assert guard.get_daily_spend() == 0.0


class TestCostGuardPersistGaps:
    @pytest.mark.asyncio()
    async def test_persist_exception(self) -> None:
        """persist() recovers from a typed DB exception."""
        pool = MagicMock()
        pool.write = MagicMock(side_effect=sqlite3.OperationalError("db broken"))

        guard = CostGuard(daily_budget=10.0, per_conversation_budget=2.0, system_pool=pool)
        guard._daily_spend = 1.0
        guard._dirty = True

        await guard.persist()
        assert guard._dirty is True


class TestRouterCircuitOpen:
    @pytest.mark.asyncio()
    async def test_skips_circuit_open_provider(self) -> None:
        """Router skips providers with open circuit breaker."""
        from sovyx.llm.router import LLMRouter

        provider1 = MagicMock()
        provider1.name = "anthropic"
        provider1.is_available = True
        provider1.supports_model.return_value = True

        provider2 = MagicMock()
        provider2.name = "openai"
        provider2.is_available = True
        provider2.supports_model.return_value = True

        from sovyx.llm.models import LLMResponse

        mock_response = LLMResponse(
            content="hello",
            model="gpt-4o",
            tokens_in=10,
            tokens_out=5,
            latency_ms=100,
            cost_usd=0.001,
            finish_reason="stop",
            provider="openai",
        )
        provider2.generate = AsyncMock(return_value=mock_response)

        cost_guard = MagicMock()
        cost_guard.can_afford.return_value = True
        cost_guard.record = AsyncMock()
        cost_guard.get_remaining_budget.return_value = 9.99

        event_bus = MagicMock()
        event_bus.emit = AsyncMock()

        router = LLMRouter(
            providers=[provider1, provider2],
            cost_guard=cost_guard,
            event_bus=event_bus,
            circuit_breaker_failures=1,
        )

        router._circuits["anthropic"].record_failure()

        result = await router.generate(
            messages=[{"role": "user", "content": "hello"}],
        )

        assert result.provider == "openai"
        provider2.generate.assert_called_once()

    @pytest.mark.asyncio()
    async def test_all_providers_circuit_open(self) -> None:
        """All circuits open → ProviderUnavailableError."""
        from sovyx.engine.errors import ProviderUnavailableError
        from sovyx.llm.router import LLMRouter

        provider = MagicMock()
        provider.name = "anthropic"
        provider.is_available = True
        provider.supports_model.return_value = True

        cost_guard = MagicMock()
        cost_guard.can_afford.return_value = True
        cost_guard.get_remaining_budget.return_value = 9.99

        event_bus = MagicMock()

        router = LLMRouter(
            providers=[provider],
            cost_guard=cost_guard,
            event_bus=event_bus,
            circuit_breaker_failures=1,
        )

        router._circuits["anthropic"].record_failure()

        with pytest.raises(ProviderUnavailableError, match="circuit open"):
            await router.generate(
                messages=[{"role": "user", "content": "hello"}],
            )


class TestRouterPricingLookup:
    def test_known_model_pricing(self) -> None:
        """Known model returns specific pricing."""
        from sovyx.llm.router import LLMRouter

        pricing = LLMRouter._get_pricing("gpt-4o")
        assert pricing == (2.5, 10.0)

    def test_unknown_model_default_pricing(self) -> None:
        """Unknown model returns conservative default."""
        from sovyx.llm.router import LLMRouter

        pricing = LLMRouter._get_pricing("future-model-9000")
        assert pricing == (3.0, 15.0)

    def test_none_model_default_pricing(self) -> None:
        """None model returns default."""
        from sovyx.llm.router import LLMRouter

        pricing = LLMRouter._get_pricing(None)
        assert pricing == (3.0, 15.0)
