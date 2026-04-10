"""Tests for sovyx.dashboard.daily_stats — DailyStatsRecorder."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from sovyx.dashboard.daily_stats import DailyStatsRecorder
from sovyx.persistence.migrations import MigrationRunner
from sovyx.persistence.pool import DatabasePool
from sovyx.persistence.schemas.system import get_system_migrations


@pytest.fixture
async def pool(tmp_path: Path) -> DatabasePool:
    """Pool with system schema (v1 + v2) applied."""
    p = DatabasePool(db_path=tmp_path / "system.db", read_pool_size=1)
    await p.initialize()
    runner = MigrationRunner(p)
    await runner.initialize()
    await runner.run_migrations(get_system_migrations())
    yield p  # type: ignore[misc]
    await p.close()


@pytest.fixture
def recorder(pool: DatabasePool) -> DailyStatsRecorder:
    """DailyStatsRecorder with a migrated pool."""
    return DailyStatsRecorder(pool)


class TestSnapshotDay:
    """snapshot_day — upsert a day's usage stats."""

    async def test_inserts_row(self, recorder: DailyStatsRecorder, pool: DatabasePool) -> None:
        await recorder.snapshot_day(
            date="2026-04-10",
            messages=32,
            llm_calls=128,
            tokens=45000,
            cost_usd=1.50,
            cost_by_provider={"openai": 1.0, "anthropic": 0.5},
            cost_by_model={"gpt-4o": 1.0, "gpt-4o-mini": 0.5},
            conversations=3,
        )

        async with pool.read() as conn:
            cursor = await conn.execute("SELECT * FROM daily_stats WHERE date = '2026-04-10'")
            row = await cursor.fetchone()
        assert row is not None
        assert row[2] == 32  # messages
        assert row[3] == 128  # llm_calls
        assert row[4] == 45000  # tokens
        assert row[5] == pytest.approx(1.50)  # cost_usd

    async def test_upsert_overwrites(
        self, recorder: DailyStatsRecorder, pool: DatabasePool
    ) -> None:
        """Same date+mind_id → second call overwrites first."""
        await recorder.snapshot_day(date="2026-04-10", messages=10, cost_usd=1.0)
        await recorder.snapshot_day(date="2026-04-10", messages=20, cost_usd=2.0)

        async with pool.read() as conn:
            cursor = await conn.execute(
                "SELECT messages, cost_usd FROM daily_stats WHERE date = '2026-04-10'"
            )
            row = await cursor.fetchone()
        assert row is not None
        assert row[0] == 20
        assert row[1] == pytest.approx(2.0)

    async def test_default_mind_id(self, recorder: DailyStatsRecorder, pool: DatabasePool) -> None:
        """Default mind_id is 'aria'."""
        await recorder.snapshot_day(date="2026-04-10", cost_usd=0.5)

        async with pool.read() as conn:
            cursor = await conn.execute(
                "SELECT mind_id FROM daily_stats WHERE date = '2026-04-10'"
            )
            row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "aria"

    async def test_json_serialisation(
        self, recorder: DailyStatsRecorder, pool: DatabasePool
    ) -> None:
        """cost_by_provider and cost_by_model are JSON strings."""
        import json

        providers = {"openai": 1.23456789, "anthropic": 0.00000001}
        models = {"gpt-4o": 1.23456789}

        await recorder.snapshot_day(
            date="2026-04-10",
            cost_by_provider=providers,
            cost_by_model=models,
        )

        async with pool.read() as conn:
            cursor = await conn.execute(
                "SELECT cost_by_provider, cost_by_model FROM daily_stats WHERE date = '2026-04-10'"
            )
            row = await cursor.fetchone()
        assert row is not None
        p_data = json.loads(row[0])
        m_data = json.loads(row[1])
        assert "openai" in p_data
        assert p_data["openai"] == pytest.approx(1.23456789)
        assert "gpt-4o" in m_data

    async def test_none_breakdowns_default_empty(
        self, recorder: DailyStatsRecorder, pool: DatabasePool
    ) -> None:
        """None breakdowns serialize as '{}'."""
        import json

        await recorder.snapshot_day(date="2026-04-10")

        async with pool.read() as conn:
            cursor = await conn.execute(
                "SELECT cost_by_provider, cost_by_model FROM daily_stats WHERE date = '2026-04-10'"
            )
            row = await cursor.fetchone()
        assert row is not None
        assert json.loads(row[0]) == {}
        assert json.loads(row[1]) == {}


class TestGetHistory:
    """get_history — retrieve last N days."""

    async def _seed_days(self, recorder: DailyStatsRecorder, count: int) -> None:
        """Insert count days of data starting from 2026-04-01."""
        for i in range(1, count + 1):
            await recorder.snapshot_day(
                date=f"2026-04-{i:02d}",
                messages=i * 10,
                llm_calls=i * 4,
                tokens=i * 1000,
                cost_usd=i * 0.10,
            )

    async def test_returns_sorted_chronologically(self, recorder: DailyStatsRecorder) -> None:
        await self._seed_days(recorder, 5)
        history = await recorder.get_history(days=10)
        dates = [d["date"] for d in history]
        assert dates == sorted(dates)
        assert len(history) == 5

    async def test_limits_to_n_days(self, recorder: DailyStatsRecorder) -> None:
        await self._seed_days(recorder, 10)
        history = await recorder.get_history(days=3)
        assert len(history) == 3
        # Should be the LAST 3 days
        assert history[0]["date"] == "2026-04-08"
        assert history[-1]["date"] == "2026-04-10"

    async def test_caps_at_365(self, recorder: DailyStatsRecorder) -> None:
        """days > 365 is capped."""
        history = await recorder.get_history(days=9999)
        assert len(history) == 0  # No data, but didn't crash

    async def test_empty_returns_empty_list(self, recorder: DailyStatsRecorder) -> None:
        history = await recorder.get_history()
        assert history == []

    async def test_entry_shape(self, recorder: DailyStatsRecorder) -> None:
        await recorder.snapshot_day(
            date="2026-04-10",
            messages=32,
            llm_calls=128,
            tokens=45000,
            cost_usd=1.50,
            cost_by_provider={"openai": 1.0},
            cost_by_model={"gpt-4o": 1.0},
            conversations=3,
        )
        history = await recorder.get_history()
        entry = history[0]
        assert entry["date"] == "2026-04-10"
        assert entry["messages"] == 32
        assert entry["llm_calls"] == 128
        assert entry["tokens"] == 45000
        assert entry["cost"] == pytest.approx(1.50)
        assert entry["cost_by_provider"] == {"openai": 1.0}
        assert entry["cost_by_model"] == {"gpt-4o": 1.0}
        assert entry["conversations"] == 3

    async def test_filters_by_mind_id(self, recorder: DailyStatsRecorder) -> None:
        """Only returns rows for the requested mind_id."""
        await recorder.snapshot_day(date="2026-04-10", mind_id="aria", messages=10)
        await recorder.snapshot_day(date="2026-04-10", mind_id="bob", messages=20)

        aria = await recorder.get_history(mind_id="aria")
        bob = await recorder.get_history(mind_id="bob")

        assert len(aria) == 1
        assert aria[0]["messages"] == 10
        assert len(bob) == 1
        assert bob[0]["messages"] == 20


class TestGetTotals:
    """get_totals — all-time aggregates."""

    async def test_aggregates_all_days(self, recorder: DailyStatsRecorder) -> None:
        for i in range(1, 4):
            await recorder.snapshot_day(
                date=f"2026-04-{i:02d}",
                messages=10,
                llm_calls=4,
                tokens=1000,
                cost_usd=0.50,
            )

        totals = await recorder.get_totals()
        assert totals["cost"] == pytest.approx(1.50)
        assert totals["messages"] == 30
        assert totals["llm_calls"] == 12
        assert totals["tokens"] == 3000
        assert totals["days_active"] == 3

    async def test_empty_returns_zeros(self, recorder: DailyStatsRecorder) -> None:
        totals = await recorder.get_totals()
        assert totals["cost"] == 0.0
        assert totals["messages"] == 0
        assert totals["days_active"] == 0

    async def test_filters_by_mind_id(self, recorder: DailyStatsRecorder) -> None:
        await recorder.snapshot_day(date="2026-04-01", mind_id="aria", cost_usd=1.0)
        await recorder.snapshot_day(date="2026-04-01", mind_id="bob", cost_usd=2.0)

        aria = await recorder.get_totals(mind_id="aria")
        bob = await recorder.get_totals(mind_id="bob")

        assert aria["cost"] == pytest.approx(1.0)
        assert bob["cost"] == pytest.approx(2.0)


class TestGetMonthTotals:
    """get_month_totals — aggregates for a specific month."""

    async def test_month_filter(self, recorder: DailyStatsRecorder) -> None:
        """Only sums rows from the requested month."""
        await recorder.snapshot_day(date="2026-03-31", cost_usd=1.0, messages=10)
        await recorder.snapshot_day(date="2026-04-01", cost_usd=2.0, messages=20)
        await recorder.snapshot_day(date="2026-04-15", cost_usd=3.0, messages=30)
        await recorder.snapshot_day(date="2026-05-01", cost_usd=4.0, messages=40)

        april = await recorder.get_month_totals(2026, 4)
        assert april["cost"] == pytest.approx(5.0)
        assert april["messages"] == 50

        march = await recorder.get_month_totals(2026, 3)
        assert march["cost"] == pytest.approx(1.0)
        assert march["messages"] == 10

    async def test_empty_month(self, recorder: DailyStatsRecorder) -> None:
        result = await recorder.get_month_totals(2026, 12)
        assert result["cost"] == 0.0
        assert result["messages"] == 0

    async def test_filters_by_mind_id(self, recorder: DailyStatsRecorder) -> None:
        await recorder.snapshot_day(date="2026-04-01", mind_id="aria", cost_usd=1.0)
        await recorder.snapshot_day(date="2026-04-01", mind_id="bob", cost_usd=5.0)

        aria = await recorder.get_month_totals(2026, 4, mind_id="aria")
        assert aria["cost"] == pytest.approx(1.0)


class TestErrorHandling:
    """Graceful degradation on DB errors."""

    async def test_snapshot_day_survives_closed_pool(self, pool: DatabasePool) -> None:
        """snapshot_day logs warning but doesn't raise."""
        recorder = DailyStatsRecorder(pool)
        await pool.close()
        # Should not raise
        await recorder.snapshot_day(date="2026-04-10", cost_usd=1.0)

    async def test_get_history_returns_empty_on_error(self, pool: DatabasePool) -> None:
        recorder = DailyStatsRecorder(pool)
        await pool.close()
        result = await recorder.get_history()
        assert result == []

    async def test_get_totals_returns_zeros_on_error(self, pool: DatabasePool) -> None:
        recorder = DailyStatsRecorder(pool)
        await pool.close()
        result = await recorder.get_totals()
        assert result["cost"] == 0.0
        assert result["days_active"] == 0

    async def test_get_month_returns_zeros_on_error(self, pool: DatabasePool) -> None:
        recorder = DailyStatsRecorder(pool)
        await pool.close()
        result = await recorder.get_month_totals(2026, 4)
        assert result["cost"] == 0.0
