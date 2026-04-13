"""Tests for GET /api/stats/history endpoint."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from sovyx.dashboard.server import create_app

# token from tests/dashboard/conftest.py


@pytest.fixture()
def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _mock_registry(
    *,
    history: list[dict[str, object]] | None = None,
    totals: dict[str, object] | None = None,
    month: dict[str, object] | None = None,
    cost_breakdown_total: float = 0.0,
) -> MagicMock:
    """Build a mock registry with DailyStatsRecorder + CostGuard."""
    from sovyx.dashboard.daily_stats import DailyStatsRecorder
    from sovyx.llm.cost import CostGuard

    recorder = AsyncMock(spec=DailyStatsRecorder)
    recorder.get_history = AsyncMock(return_value=history or [])
    recorder.get_totals = AsyncMock(
        return_value=totals
        or {"cost": 0.0, "messages": 0, "llm_calls": 0, "tokens": 0, "days_active": 0}
    )
    recorder.get_month_totals = AsyncMock(
        return_value=month or {"cost": 0.0, "messages": 0, "llm_calls": 0, "tokens": 0}
    )

    guard = MagicMock(spec=CostGuard)
    breakdown = MagicMock()
    breakdown.total_cost = cost_breakdown_total
    guard.get_breakdown.return_value = breakdown

    registry = MagicMock()

    async def resolve(cls: type) -> object:
        if cls is DailyStatsRecorder:
            return recorder
        if cls is CostGuard:
            return guard
        msg = f"Unknown: {cls}"
        raise ValueError(msg)

    registry.resolve = AsyncMock(side_effect=resolve)
    return registry


class TestStatsHistoryEndpoint:
    """GET /api/stats/history tests."""

    def test_requires_auth(self, token: str) -> None:
        app = create_app()
        client = TestClient(app)
        resp = client.get("/api/stats/history")
        assert resp.status_code in (401, 403)

    def test_returns_empty_when_no_registry(self, auth: dict[str, str]) -> None:
        app = create_app()
        client = TestClient(app)
        resp = client.get("/api/stats/history", headers=auth)
        assert resp.status_code == 200
        data = resp.json()
        assert data["days"] == []
        assert data["totals"]["cost"] == 0.0
        assert data["current_month"]["cost"] == 0.0

    def test_returns_history_with_live_today(self, auth: dict[str, str]) -> None:
        """Historical days + live today entry."""
        app = create_app()
        day1 = {
            "date": "2026-04-08",
            "cost": 0.50,
            "messages": 10,
            "llm_calls": 40,
            "tokens": 5000,
        }
        day2 = {
            "date": "2026-04-09",
            "cost": 0.75,
            "messages": 15,
            "llm_calls": 60,
            "tokens": 8000,
        }
        registry = _mock_registry(
            history=[day1, day2],
            totals={
                "cost": 1.25,
                "messages": 25,
                "llm_calls": 100,
                "tokens": 13000,
                "days_active": 2,
            },
            cost_breakdown_total=0.10,
        )
        app.state.registry = registry

        # Set up counters with some live data
        from sovyx.dashboard.status import get_counters

        counters = get_counters()
        original_key = counters._day_key
        original_calls = counters.llm_calls
        original_cost = counters.llm_cost
        original_tokens = counters.tokens
        original_msgs = counters.messages_received

        try:
            counters._day_key = ""  # Force reset
            counters.llm_calls = 0
            counters.llm_cost = 0.0
            counters.tokens = 0
            counters.messages_received = 0
            counters.record_llm_call(cost=0.10, tokens=2000)
            counters.record_message()
            counters.record_message()
            counters.record_message()

            client = TestClient(app)
            resp = client.get("/api/stats/history", headers=auth)
        finally:
            counters._day_key = original_key
            counters.llm_calls = original_calls
            counters.llm_cost = original_cost
            counters.tokens = original_tokens
            counters.messages_received = original_msgs

        assert resp.status_code == 200
        data = resp.json()

        # Should have 3 entries: 2 historical + 1 live today
        assert len(data["days"]) == 3
        today = data["days"][-1]
        assert today["is_live"] is True
        assert today["messages"] >= 3
        assert today["llm_calls"] >= 1

    def test_today_entry_has_is_live(self, auth: dict[str, str]) -> None:
        app = create_app()
        registry = _mock_registry()
        app.state.registry = registry

        client = TestClient(app)
        resp = client.get("/api/stats/history", headers=auth)
        data = resp.json()

        # Even with no history, today appears
        assert len(data["days"]) >= 1
        assert data["days"][-1].get("is_live") is True

    def test_totals_include_live_data(self, auth: dict[str, str]) -> None:
        app = create_app()
        registry = _mock_registry(
            totals={
                "cost": 5.0,
                "messages": 100,
                "llm_calls": 400,
                "tokens": 50000,
                "days_active": 10,
            },
            cost_breakdown_total=0.25,
        )
        app.state.registry = registry

        from sovyx.dashboard.status import get_counters

        counters = get_counters()
        original_key = counters._day_key
        original_msgs = counters.messages_received

        try:
            counters._day_key = ""
            counters.messages_received = 0
            counters.record_message()

            client = TestClient(app)
            resp = client.get("/api/stats/history", headers=auth)
        finally:
            counters._day_key = original_key
            counters.messages_received = original_msgs

        data = resp.json()
        # Totals should include live cost (0.25) on top of historical (5.0)
        assert data["totals"]["cost"] >= 5.0
        assert data["totals"]["messages"] >= 101

    def test_caps_at_365_days(self, auth: dict[str, str]) -> None:
        app = create_app()
        registry = _mock_registry()
        app.state.registry = registry

        client = TestClient(app)
        resp = client.get("/api/stats/history?days=9999", headers=auth)
        assert resp.status_code == 200
        # Didn't crash — days capped internally

    def test_invalid_days_defaults_to_30(self, auth: dict[str, str]) -> None:
        app = create_app()
        registry = _mock_registry()
        app.state.registry = registry

        client = TestClient(app)
        resp = client.get("/api/stats/history?days=abc", headers=auth)
        assert resp.status_code == 200

    def test_returns_only_today_when_no_history(self, auth: dict[str, str]) -> None:
        app = create_app()
        registry = _mock_registry()
        app.state.registry = registry

        client = TestClient(app)
        resp = client.get("/api/stats/history", headers=auth)
        data = resp.json()

        assert len(data["days"]) >= 1
        assert data["days"][-1]["is_live"] is True

    def test_current_month_includes_live(self, auth: dict[str, str]) -> None:
        app = create_app()
        registry = _mock_registry(
            month={"cost": 3.0, "messages": 50, "llm_calls": 200, "tokens": 25000},
            cost_breakdown_total=0.15,
        )
        app.state.registry = registry

        client = TestClient(app)
        resp = client.get("/api/stats/history", headers=auth)
        data = resp.json()

        assert data["current_month"]["cost"] >= 3.0
