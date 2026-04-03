"""Tests for sovyx.llm.cost — CostGuard."""

from __future__ import annotations

import pytest

from sovyx.llm.cost import CostGuard


class TestCanAfford:
    """Budget checking."""

    def test_can_afford_under_budget(self) -> None:
        g = CostGuard(daily_budget=10.0, per_conversation_budget=2.0)
        assert g.can_afford(1.0) is True

    def test_cannot_afford_over_daily(self) -> None:
        g = CostGuard(daily_budget=1.0, per_conversation_budget=2.0)
        g.record(0.9, "model", "conv1")
        assert g.can_afford(0.2) is False

    def test_cannot_afford_over_conversation(self) -> None:
        g = CostGuard(daily_budget=10.0, per_conversation_budget=0.5)
        g.record(0.4, "model", "conv1")
        assert g.can_afford(0.2, "conv1") is False

    def test_other_conversation_ok(self) -> None:
        g = CostGuard(daily_budget=10.0, per_conversation_budget=0.5)
        g.record(0.4, "model", "conv1")
        assert g.can_afford(0.2, "conv2") is True

    def test_no_conversation_id_skips_conv_check(self) -> None:
        g = CostGuard(daily_budget=10.0, per_conversation_budget=0.5)
        g.record(0.4, "model", "conv1")
        assert g.can_afford(0.2) is True


class TestRecord:
    """Spending recording."""

    def test_record_increases_daily(self) -> None:
        g = CostGuard(daily_budget=10.0, per_conversation_budget=2.0)
        g.record(1.5, "model", "conv1")
        assert g.get_daily_spend() == 1.5

    def test_record_increases_conversation(self) -> None:
        g = CostGuard(daily_budget=10.0, per_conversation_budget=2.0)
        g.record(0.5, "model", "conv1")
        g.record(0.3, "model", "conv1")
        assert g.get_conversation_spend("conv1") == pytest.approx(0.8)


class TestBudgetQueries:
    """Budget query methods."""

    def test_remaining_budget(self) -> None:
        g = CostGuard(daily_budget=10.0, per_conversation_budget=2.0)
        g.record(3.0, "model", "conv1")
        assert g.get_remaining_budget() == 7.0

    def test_conversation_remaining(self) -> None:
        g = CostGuard(daily_budget=10.0, per_conversation_budget=2.0)
        g.record(1.5, "model", "conv1")
        assert g.get_conversation_remaining("conv1") == 0.5

    def test_unknown_conversation(self) -> None:
        g = CostGuard(daily_budget=10.0, per_conversation_budget=2.0)
        assert g.get_conversation_spend("unknown") == 0.0
        assert g.get_conversation_remaining("unknown") == 2.0
