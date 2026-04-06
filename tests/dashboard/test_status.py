"""Tests for sovyx.dashboard.status — StatusCollector and DashboardCounters."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from sovyx.dashboard.status import (
    DashboardCounters,
    StatusCollector,
    StatusSnapshot,
    get_counters,
)


class TestDashboardCounters:
    def test_initial_values(self) -> None:
        c = DashboardCounters()
        assert c.llm_calls == 0
        assert c.llm_cost == 0.0
        assert c.tokens == 0
        assert c.messages_received == 0

    def test_record_llm_call(self) -> None:
        c = DashboardCounters()
        c.record_llm_call(cost=0.05, tokens=500)
        assert c.llm_calls == 1
        assert c.llm_cost == 0.05
        assert c.tokens == 500

    def test_record_message(self) -> None:
        c = DashboardCounters()
        c.record_message()
        c.record_message()
        assert c.messages_received == 2

    def test_accumulation(self) -> None:
        c = DashboardCounters()
        c.record_llm_call(cost=0.01, tokens=100)
        c.record_llm_call(cost=0.02, tokens=200)
        assert c.llm_calls == 2
        assert c.llm_cost == pytest.approx(0.03)
        assert c.tokens == 300

    def test_day_boundary_reset(self) -> None:
        c = DashboardCounters()
        c._day_key = "2020-01-01"  # Force old date
        c.llm_calls = 99
        c.record_llm_call(cost=0.01, tokens=10)
        # Should have reset: 0 + 1 = 1
        assert c.llm_calls == 1

    def test_get_counters_singleton(self) -> None:
        c1 = get_counters()
        c2 = get_counters()
        assert c1 is c2


class TestStatusSnapshot:
    def test_to_dict(self) -> None:
        snap = StatusSnapshot(
            version="0.1.0",
            uptime_seconds=3661.5678,
            mind_name="Nyx",
            active_conversations=3,
            memory_concepts=150,
            memory_episodes=42,
            llm_cost_today=0.12345,
            llm_calls_today=7,
            tokens_today=1500,
            messages_today=25,
        )
        d = snap.to_dict()
        assert d["version"] == "0.1.0"
        assert d["uptime_seconds"] == 3661.6  # rounded to 1 decimal
        assert d["mind_name"] == "Nyx"
        assert d["active_conversations"] == 3
        assert d["memory_concepts"] == 150
        assert d["memory_episodes"] == 42
        assert d["llm_cost_today"] == 0.1235  # rounded to 4 decimals
        assert d["llm_calls_today"] == 7
        assert d["tokens_today"] == 1500


class TestStatusCollector:
    @pytest.mark.asyncio()
    async def test_collect_with_empty_registry(self) -> None:
        registry = MagicMock()
        registry.is_registered.return_value = False

        collector = StatusCollector(registry, start_time=time.time() - 100)
        snap = await collector.collect()

        assert snap.version == "0.1.0"
        assert snap.uptime_seconds >= 99  # at least 99 seconds
        assert snap.mind_name == "sovyx"  # default fallback
        assert snap.memory_concepts == 0
        assert snap.memory_episodes == 0
        assert snap.active_conversations == 0

    @pytest.mark.asyncio()
    async def test_collect_with_mind_manager(self) -> None:
        mock_manager = MagicMock()
        mock_manager.get_active_minds.return_value = ["Aria"]

        registry = MagicMock()

        def is_registered(cls: type) -> bool:
            from sovyx.engine.bootstrap import MindManager

            return cls is MindManager

        registry.is_registered.side_effect = is_registered
        registry.resolve = AsyncMock(return_value=mock_manager)

        collector = StatusCollector(registry)
        snap = await collector.collect()
        assert snap.mind_name == "Aria"

    @pytest.mark.asyncio()
    async def test_collect_with_counters(self) -> None:
        counters = get_counters()
        # Reset
        counters._day_key = ""
        counters.record_llm_call(cost=0.05, tokens=500)
        counters.record_message()

        registry = MagicMock()
        registry.is_registered.return_value = False

        collector = StatusCollector(registry)
        snap = await collector.collect()

        assert snap.llm_cost_today >= 0.05
        assert snap.llm_calls_today >= 1
        assert snap.tokens_today >= 500

    @pytest.mark.asyncio()
    async def test_collect_survives_service_errors(self) -> None:
        """If a service throws, collect should still return valid snapshot."""
        registry = MagicMock()
        registry.is_registered.return_value = True
        registry.resolve = AsyncMock(side_effect=RuntimeError("boom"))

        collector = StatusCollector(registry)
        snap = await collector.collect()

        # Should not raise — returns defaults
        assert snap.mind_name == "sovyx"
        assert snap.memory_concepts == 0
