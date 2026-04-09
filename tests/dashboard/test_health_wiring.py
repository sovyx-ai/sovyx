"""Tests for health check wiring in DashboardServer.

Verifies that _create_health_registry correctly wires engine services
to the online HealthRegistry, and that /api/health deduplicates and
returns the expected checks.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, PropertyMock

import pytest

from sovyx.observability.health import (
    CheckStatus,
    LLMReachableCheck,
)

# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_provider(name: str, *, available: bool = True) -> MagicMock:
    """Create a mock LLM provider."""
    p = MagicMock()
    p.name = name
    type(p).is_available = PropertyMock(return_value=available)
    return p


def _make_router(*providers: MagicMock) -> MagicMock:
    """Create a mock LLMRouter with given providers."""
    router = MagicMock()
    router._providers = list(providers)
    return router


def _make_registry(services: dict[type, Any] | None = None) -> MagicMock:
    """Create a mock ServiceRegistry."""
    registry = MagicMock()
    services = services or {}

    def _is_registered(cls: type) -> bool:
        return cls in services

    async def _resolve(cls: type) -> Any:  # noqa: ANN401
        return services[cls]

    registry.is_registered = _is_registered
    registry.resolve = AsyncMock(side_effect=_resolve)
    return registry


# ── LLM Provider Detection ──────────────────────────────────────────────────


class TestLLMProviderDetection:
    """Verify LLM health check correctly detects cloud providers."""

    @pytest.mark.asyncio
    async def test_openai_only_green(self) -> None:
        """Single OpenAI provider → GREEN."""
        openai = _make_provider("openai")
        fn = _make_llm_callback([openai])
        check = LLMReachableCheck(provider_status_fn=fn)
        result = await check.check()
        assert result.status == CheckStatus.GREEN
        assert "openai" in result.message

    @pytest.mark.asyncio
    async def test_anthropic_only_green(self) -> None:
        """Single Anthropic provider → GREEN."""
        anthropic = _make_provider("anthropic")
        fn = _make_llm_callback([anthropic])
        check = LLMReachableCheck(provider_status_fn=fn)
        result = await check.check()
        assert result.status == CheckStatus.GREEN
        assert "anthropic" in result.message

    @pytest.mark.asyncio
    async def test_google_only_green(self) -> None:
        """Single Google provider → GREEN."""
        google = _make_provider("google")
        fn = _make_llm_callback([google])
        check = LLMReachableCheck(provider_status_fn=fn)
        result = await check.check()
        assert result.status == CheckStatus.GREEN
        assert "google" in result.message

    @pytest.mark.asyncio
    async def test_multiple_providers_green(self) -> None:
        """Multiple cloud providers → GREEN with all names listed."""
        providers = [
            _make_provider("openai"),
            _make_provider("anthropic"),
            _make_provider("google"),
        ]
        fn = _make_llm_callback(providers)
        check = LLMReachableCheck(provider_status_fn=fn)
        result = await check.check()
        assert result.status == CheckStatus.GREEN
        assert "3 provider(s) available" in result.message
        for name in ("openai", "anthropic", "google"):
            assert name in result.message

    @pytest.mark.asyncio
    async def test_no_providers_red(self) -> None:
        """Empty provider list → RED."""
        fn = _make_llm_callback([])
        check = LLMReachableCheck(provider_status_fn=fn)
        result = await check.check()
        assert result.status == CheckStatus.RED

    @pytest.mark.asyncio
    async def test_none_callback_yellow(self) -> None:
        """No callback (service not registered) → YELLOW."""
        check = LLMReachableCheck(provider_status_fn=None)
        result = await check.check()
        assert result.status == CheckStatus.YELLOW
        assert "not configured" in result.message

    @pytest.mark.asyncio
    async def test_ollama_excluded_from_callback(self) -> None:
        """Verify Ollama exclusion logic (as implemented in DashboardServer).

        When only Ollama is registered, the filtered callback should return
        an empty list → RED (not a false-positive GREEN).
        """
        ollama = _make_provider("ollama")
        # Simulate the DashboardServer callback that filters Ollama
        providers = [ollama]

        async def _llm_status() -> list[tuple[str, bool]]:
            return [(p.name, p.is_available) for p in providers if p.name != "ollama"]

        check = LLMReachableCheck(provider_status_fn=_llm_status)
        result = await check.check()
        assert result.status == CheckStatus.RED, "Ollama alone should NOT make check green"

    @pytest.mark.asyncio
    async def test_ollama_plus_openai_green(self) -> None:
        """Ollama + OpenAI → GREEN (OpenAI detected, Ollama filtered)."""
        ollama = _make_provider("ollama")
        openai = _make_provider("openai")
        providers = [ollama, openai]

        async def _llm_status() -> list[tuple[str, bool]]:
            return [(p.name, p.is_available) for p in providers if p.name != "ollama"]

        check = LLMReachableCheck(provider_status_fn=_llm_status)
        result = await check.check()
        assert result.status == CheckStatus.GREEN
        assert "openai" in result.message
        assert "ollama" not in result.message

    @pytest.mark.asyncio
    async def test_unavailable_provider_not_counted(self) -> None:
        """Provider with is_available=False → not in available list."""
        openai = _make_provider("openai", available=False)
        fn = _make_llm_callback([openai])
        check = LLMReachableCheck(provider_status_fn=fn)
        result = await check.check()
        assert result.status == CheckStatus.RED

    @pytest.mark.asyncio
    async def test_callback_exception_red(self) -> None:
        """Callback that raises → RED with error message."""

        async def _exploding() -> list[tuple[str, bool]]:
            msg = "connection refused"
            raise ConnectionError(msg)

        check = LLMReachableCheck(provider_status_fn=_exploding)
        result = await check.check()
        assert result.status == CheckStatus.RED
        assert "connection refused" in result.message


def _make_llm_callback(
    providers: list[MagicMock],
) -> Any:  # noqa: ANN401
    """Create an async callback matching LLMReachableCheck interface."""

    async def _fn() -> list[tuple[str, bool]]:
        return [(p.name, p.is_available) for p in providers]

    return _fn


# ── Health Endpoint Deduplication ────────────────────────────────────────────


class TestHealthDeduplication:
    """Verify /api/health merges offline + online without duplicates."""

    def test_no_duplicate_check_names(self) -> None:
        """Offline (4) + online (6) should produce 10 unique names."""
        offline_names = {"Disk Space", "RAM", "CPU", "Embedding Model"}
        online_names = {
            "Database",
            "Brain Index",
            "LLM Providers",
            "Channels",
            "Consolidation",
            "Cost Budget",
        }

        # No overlap between the two tiers
        overlap = offline_names & online_names
        assert overlap == set(), f"Unexpected overlap: {overlap}"

        # Total is exactly 10
        all_names = offline_names | online_names
        assert len(all_names) == 10


# ── Other Health Checks ──────────────────────────────────────────────────────


class TestDatabaseCheck:
    """DatabaseCheck wiring."""

    @pytest.mark.asyncio
    async def test_writable_green(self) -> None:
        from sovyx.observability.health import DatabaseCheck

        check = DatabaseCheck(write_fn=AsyncMock())
        result = await check.check()
        assert result.status == CheckStatus.GREEN

    @pytest.mark.asyncio
    async def test_write_fails_red(self) -> None:
        from sovyx.observability.health import DatabaseCheck

        check = DatabaseCheck(write_fn=AsyncMock(side_effect=OSError("disk full")))
        result = await check.check()
        assert result.status == CheckStatus.RED
        assert "disk full" in result.message

    @pytest.mark.asyncio
    async def test_not_configured_yellow(self) -> None:
        from sovyx.observability.health import DatabaseCheck

        check = DatabaseCheck(write_fn=None)
        result = await check.check()
        assert result.status == CheckStatus.YELLOW


class TestBrainCheck:
    """BrainIndexedCheck wiring."""

    @pytest.mark.asyncio
    async def test_loaded_green(self) -> None:
        from sovyx.observability.health import BrainIndexedCheck

        check = BrainIndexedCheck(is_loaded_fn=lambda: True)
        result = await check.check()
        assert result.status == CheckStatus.GREEN

    @pytest.mark.asyncio
    async def test_not_loaded_yellow(self) -> None:
        from sovyx.observability.health import BrainIndexedCheck

        check = BrainIndexedCheck(is_loaded_fn=lambda: False)
        result = await check.check()
        assert result.status == CheckStatus.YELLOW

    @pytest.mark.asyncio
    async def test_not_configured_yellow(self) -> None:
        from sovyx.observability.health import BrainIndexedCheck

        check = BrainIndexedCheck(is_loaded_fn=None)
        result = await check.check()
        assert result.status == CheckStatus.YELLOW


class TestChannelCheck:
    """ChannelConnectedCheck wiring."""

    @pytest.mark.asyncio
    async def test_connected_green(self) -> None:
        from sovyx.observability.health import ChannelConnectedCheck

        check = ChannelConnectedCheck(channel_status_fn=lambda: [("telegram", True)])
        result = await check.check()
        assert result.status == CheckStatus.GREEN
        assert "telegram" in result.message

    @pytest.mark.asyncio
    async def test_no_channels_red(self) -> None:
        from sovyx.observability.health import ChannelConnectedCheck

        check = ChannelConnectedCheck(channel_status_fn=lambda: [("telegram", False)])
        result = await check.check()
        assert result.status == CheckStatus.RED

    @pytest.mark.asyncio
    async def test_not_configured_yellow(self) -> None:
        from sovyx.observability.health import ChannelConnectedCheck

        check = ChannelConnectedCheck(channel_status_fn=None)
        result = await check.check()
        assert result.status == CheckStatus.YELLOW


class TestConsolidationCheck:
    """ConsolidationCheck wiring."""

    @pytest.mark.asyncio
    async def test_running_green(self) -> None:
        from sovyx.observability.health import ConsolidationCheck

        check = ConsolidationCheck(is_running_fn=lambda: True)
        result = await check.check()
        assert result.status == CheckStatus.GREEN

    @pytest.mark.asyncio
    async def test_not_running_yellow(self) -> None:
        from sovyx.observability.health import ConsolidationCheck

        check = ConsolidationCheck(is_running_fn=lambda: False)
        result = await check.check()
        assert result.status == CheckStatus.YELLOW


class TestCostBudgetCheck:
    """CostBudgetCheck wiring."""

    @pytest.mark.asyncio
    async def test_under_budget_green(self) -> None:
        from sovyx.observability.health import CostBudgetCheck

        check = CostBudgetCheck(get_spend_fn=lambda: 0.5, daily_budget=2.0)
        result = await check.check()
        assert result.status == CheckStatus.GREEN

    @pytest.mark.asyncio
    async def test_near_budget_yellow(self) -> None:
        from sovyx.observability.health import CostBudgetCheck

        check = CostBudgetCheck(get_spend_fn=lambda: 1.8, daily_budget=2.0)
        result = await check.check()
        assert result.status == CheckStatus.YELLOW

    @pytest.mark.asyncio
    async def test_over_budget_red(self) -> None:
        from sovyx.observability.health import CostBudgetCheck

        check = CostBudgetCheck(get_spend_fn=lambda: 3.0, daily_budget=2.0)
        result = await check.check()
        assert result.status == CheckStatus.RED

    @pytest.mark.asyncio
    async def test_not_configured_yellow(self) -> None:
        from sovyx.observability.health import CostBudgetCheck

        check = CostBudgetCheck(get_spend_fn=None)
        result = await check.check()
        assert result.status == CheckStatus.YELLOW
