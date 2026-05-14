"""Tests for sovyx.llm.cost — CostGuard + CostBreakdown."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from sovyx.llm import cost as _cost_mod  # anti-pattern #11
from sovyx.llm.cost import CostBreakdown, CostGuard

if TYPE_CHECKING:
    from pathlib import Path


class TestCanAfford:
    """Budget checking."""

    async def test_can_afford_under_budget(self) -> None:
        g = CostGuard(daily_budget=10.0, per_conversation_budget=2.0)
        assert g.can_afford(1.0) is True

    async def test_cannot_afford_over_daily(self) -> None:
        g = CostGuard(daily_budget=1.0, per_conversation_budget=2.0)
        await g.record(0.9, "model", "conv1")
        assert g.can_afford(0.2) is False

    async def test_cannot_afford_over_conversation(self) -> None:
        g = CostGuard(daily_budget=10.0, per_conversation_budget=0.5)
        await g.record(0.4, "model", "conv1")
        assert g.can_afford(0.2, "conv1") is False

    async def test_other_conversation_ok(self) -> None:
        g = CostGuard(daily_budget=10.0, per_conversation_budget=0.5)
        await g.record(0.4, "model", "conv1")
        assert g.can_afford(0.2, "conv2") is True

    async def test_no_conversation_id_skips_conv_check(self) -> None:
        g = CostGuard(daily_budget=10.0, per_conversation_budget=0.5)
        await g.record(0.4, "model", "conv1")
        assert g.can_afford(0.2) is True


class TestRecord:
    """Spending recording."""

    async def test_record_increases_daily(self) -> None:
        g = CostGuard(daily_budget=10.0, per_conversation_budget=2.0)
        await g.record(1.5, "model", "conv1")
        assert g.get_daily_spend() == 1.5

    async def test_record_increases_conversation(self) -> None:
        g = CostGuard(daily_budget=10.0, per_conversation_budget=2.0)
        await g.record(0.5, "model", "conv1")
        await g.record(0.3, "model", "conv1")
        assert g.get_conversation_spend("conv1") == pytest.approx(0.8)


class TestBudgetQueries:
    """Budget query methods."""

    async def test_remaining_budget(self) -> None:
        g = CostGuard(daily_budget=10.0, per_conversation_budget=2.0)
        await g.record(3.0, "model", "conv1")
        assert g.get_remaining_budget() == 7.0

    async def test_conversation_remaining(self) -> None:
        g = CostGuard(daily_budget=10.0, per_conversation_budget=2.0)
        await g.record(1.5, "model", "conv1")
        assert g.get_conversation_remaining("conv1") == 0.5

    async def test_unknown_conversation(self) -> None:
        g = CostGuard(daily_budget=10.0, per_conversation_budget=2.0)
        assert g.get_conversation_spend("unknown") == 0.0
        assert g.get_conversation_remaining("unknown") == 2.0


class TestDailyReset:
    """Daily reset clears spend at midnight UTC."""

    async def test_reset_clears_daily_spend(self) -> None:
        from datetime import timedelta
        from unittest.mock import patch

        g = CostGuard(daily_budget=10.0, per_conversation_budget=2.0)
        await g.record(5.0, "model", "conv1")
        assert g.get_daily_spend() == 5.0

        # Simulate next day
        tomorrow = datetime.now(tz=UTC) + timedelta(days=1)
        with patch.object(_cost_mod, "datetime") as mock_dt:
            mock_dt.now.return_value = tomorrow
            mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
            assert g.get_daily_spend() == 0.0

    async def test_reset_clears_conversation_spend(self) -> None:
        from datetime import timedelta
        from unittest.mock import patch

        g = CostGuard(daily_budget=10.0, per_conversation_budget=2.0)
        await g.record(1.0, "model", "conv1")
        assert g.get_conversation_spend("conv1") == 1.0

        tomorrow = datetime.now(tz=UTC) + timedelta(days=1)
        with patch.object(_cost_mod, "datetime") as mock_dt:
            mock_dt.now.return_value = tomorrow
            mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
            assert g.can_afford(2.0, "conv1") is True

    async def test_no_reset_same_day(self) -> None:
        g = CostGuard(daily_budget=10.0, per_conversation_budget=2.0)
        await g.record(5.0, "model", "conv1")
        g._maybe_reset()
        g._maybe_reset()
        assert g.get_daily_spend() == 5.0


class TestPersistence:
    """Persist and restore spend state via SQLite."""

    async def test_persist_and_restore(self, tmp_path: object) -> None:
        """Record spend → persist → new guard → restore → same spend."""
        from pathlib import Path

        from sovyx.persistence.migrations import MigrationRunner
        from sovyx.persistence.pool import DatabasePool
        from sovyx.persistence.schemas.system import get_system_migrations

        db_path = Path(str(tmp_path)) / "system.db"
        pool = DatabasePool(db_path=db_path, read_pool_size=1)
        await pool.initialize()
        runner = MigrationRunner(pool)
        await runner.initialize()
        await runner.run_migrations(get_system_migrations())

        # Record some spend
        g1 = CostGuard(daily_budget=10.0, per_conversation_budget=2.0, system_pool=pool)
        await g1.record(3.5, "claude", "conv-a")
        await g1.record(1.2, "gpt-4o", "conv-b")

        # New guard, restore from same DB
        g2 = CostGuard(daily_budget=10.0, per_conversation_budget=2.0, system_pool=pool)
        await g2.restore()

        assert g2.get_daily_spend() == pytest.approx(4.7)
        assert g2.get_conversation_spend("conv-a") == pytest.approx(3.5)
        assert g2.get_conversation_spend("conv-b") == pytest.approx(1.2)

        await pool.close()

    async def test_no_pool_no_crash(self) -> None:
        """Without pool, persist/restore are no-ops (no crash)."""
        g = CostGuard(daily_budget=10.0, per_conversation_budget=2.0)
        await g.restore()
        await g.record(1.0, "model", "conv1")
        # No crash — persist silently skipped
        assert g.get_daily_spend() == 1.0

    async def test_restore_empty_table(self, tmp_path: object) -> None:
        """Restore with no saved state → starts fresh (row is None path)."""
        from pathlib import Path

        from sovyx.persistence.migrations import MigrationRunner
        from sovyx.persistence.pool import DatabasePool
        from sovyx.persistence.schemas.system import get_system_migrations

        db_path = Path(str(tmp_path)) / "empty.db"
        pool = DatabasePool(db_path=db_path, read_pool_size=1)
        await pool.initialize()
        runner = MigrationRunner(pool)
        await runner.initialize()
        await runner.run_migrations(get_system_migrations())

        g = CostGuard(daily_budget=10.0, per_conversation_budget=2.0, system_pool=pool)
        await g.restore()
        assert g.get_daily_spend() == 0.0
        await pool.close()

    async def test_restore_stale_date(self, tmp_path: object) -> None:
        """Restore with state from a different day → starts fresh."""
        import json
        from pathlib import Path

        from sovyx.persistence.migrations import MigrationRunner
        from sovyx.persistence.pool import DatabasePool
        from sovyx.persistence.schemas.system import get_system_migrations

        db_path = Path(str(tmp_path)) / "stale.db"
        pool = DatabasePool(db_path=db_path, read_pool_size=1)
        await pool.initialize()
        runner = MigrationRunner(pool)
        await runner.initialize()
        await runner.run_migrations(get_system_migrations())

        # Insert stale state (yesterday)
        stale = json.dumps(
            {
                "date": "1999-01-01",
                "daily_spend": 99.0,
                "conversation_spend": {"old": 50.0},
            }
        )
        async with pool.write() as conn:
            await conn.execute(
                "INSERT INTO engine_state (key, value, updated_at)"
                " VALUES (?, ?, CURRENT_TIMESTAMP)",
                ("cost_guard_state", stale),
            )
            await conn.commit()

        g = CostGuard(daily_budget=10.0, per_conversation_budget=2.0, system_pool=pool)
        await g.restore()
        # Should NOT restore stale data
        assert g.get_daily_spend() == 0.0
        await pool.close()

    async def test_restore_db_error(self) -> None:
        """Typed DB failure during restore → no crash.

        Uses ``sqlite3.OperationalError`` because the narrow catch in
        ``CostGuard.restore`` only recovers from typed DB failures.
        A bare ``RuntimeError`` would now surface as a programmer
        error, which is the correct new contract.
        """
        import sqlite3
        from unittest.mock import AsyncMock, MagicMock

        mock_pool = MagicMock()
        # Make read() context manager raise
        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(side_effect=sqlite3.OperationalError("db broken"))
        cm.__aexit__ = AsyncMock(return_value=False)
        mock_pool.read.return_value = cm

        g = CostGuard(daily_budget=10.0, per_conversation_budget=2.0, system_pool=mock_pool)
        await g.restore()  # Should not raise
        assert g.get_daily_spend() == 0.0

    async def test_persist_db_error(self) -> None:
        """Typed DB failure during persist → spend still in-memory.

        Same typed-contract alignment as the restore sibling.
        """
        import sqlite3
        from unittest.mock import AsyncMock, MagicMock

        mock_pool = MagicMock()
        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(side_effect=sqlite3.OperationalError("db broken"))
        cm.__aexit__ = AsyncMock(return_value=False)
        mock_pool.write.return_value = cm

        g = CostGuard(daily_budget=10.0, per_conversation_budget=2.0, system_pool=mock_pool)
        # record sets _dirty=True and calls persist
        await g.record(1.0, "model", "conv1")
        # Should not raise, spend still tracked in-memory
        assert g.get_daily_spend() == 1.0

    async def test_persist_and_restore_breakdown(self, tmp_path: object) -> None:
        """Record with provider/mind → persist → restore → breakdown intact."""
        from pathlib import Path

        from sovyx.persistence.migrations import MigrationRunner
        from sovyx.persistence.pool import DatabasePool
        from sovyx.persistence.schemas.system import get_system_migrations

        db_path = Path(str(tmp_path)) / "breakdown.db"
        pool = DatabasePool(db_path=db_path, read_pool_size=1)
        await pool.initialize()
        runner = MigrationRunner(pool)
        await runner.initialize()
        await runner.run_migrations(get_system_migrations())

        g1 = CostGuard(daily_budget=10.0, per_conversation_budget=2.0, system_pool=pool)
        await g1.record(
            1.5,
            "claude-3-opus",
            "conv-a",
            provider="anthropic",
            mind_id="main",
            tokens=500,
        )
        await g1.record(
            0.3,
            "gpt-4o",
            "conv-b",
            provider="openai",
            mind_id="research",
            tokens=200,
        )

        g2 = CostGuard(daily_budget=10.0, per_conversation_budget=2.0, system_pool=pool)
        await g2.restore()

        bd = g2.get_breakdown("day")
        assert bd.total_cost == pytest.approx(1.8)
        assert bd.total_tokens == 700
        assert bd.by_provider["anthropic"] == pytest.approx(1.5)
        assert bd.by_provider["openai"] == pytest.approx(0.3)
        assert bd.by_mind["main"] == pytest.approx(1.5)
        assert bd.by_mind["research"] == pytest.approx(0.3)
        assert bd.tokens_by_provider["anthropic"] == 500
        assert bd.tokens_by_provider["openai"] == 200

        await pool.close()


class TestRecordCost:
    """Tests for record_cost convenience method."""

    async def test_record_cost_tracks_provider(self) -> None:
        g = CostGuard(daily_budget=10.0, per_conversation_budget=2.0)
        await g.record_cost(
            provider="anthropic",
            mind_id="default",
            tokens=1000,
            cost=0.5,
            model="claude-3-opus",
        )
        assert g.get_provider_spend("anthropic") == pytest.approx(0.5)

    async def test_record_cost_tracks_mind(self) -> None:
        g = CostGuard(daily_budget=10.0, per_conversation_budget=2.0)
        await g.record_cost(
            provider="openai",
            mind_id="research",
            tokens=500,
            cost=0.2,
            model="gpt-4o",
        )
        assert g.get_mind_spend("research") == pytest.approx(0.2)

    async def test_record_cost_accumulates(self) -> None:
        g = CostGuard(daily_budget=10.0, per_conversation_budget=2.0)
        await g.record_cost("anthropic", "main", 100, 0.1)
        await g.record_cost("anthropic", "main", 200, 0.2)
        assert g.get_provider_spend("anthropic") == pytest.approx(0.3)
        assert g.get_mind_spend("main") == pytest.approx(0.3)

    async def test_record_cost_also_tracks_daily(self) -> None:
        g = CostGuard(daily_budget=10.0, per_conversation_budget=2.0)
        await g.record_cost("anthropic", "main", 100, 0.5)
        assert g.get_daily_spend() == pytest.approx(0.5)

    async def test_record_cost_with_conversation(self) -> None:
        g = CostGuard(daily_budget=10.0, per_conversation_budget=2.0)
        await g.record_cost(
            "anthropic",
            "main",
            100,
            0.5,
            conversation_id="conv1",
        )
        assert g.get_conversation_spend("conv1") == pytest.approx(0.5)


class TestCostBreakdown:
    """Tests for get_breakdown and CostBreakdown."""

    async def test_empty_breakdown(self) -> None:
        g = CostGuard(daily_budget=10.0, per_conversation_budget=2.0)
        bd = g.get_breakdown("day")
        assert bd.total_cost == 0.0
        assert bd.total_tokens == 0
        assert bd.by_provider == {}
        assert bd.by_mind == {}
        assert bd.by_model == {}

    async def test_breakdown_multiple_providers(self) -> None:
        g = CostGuard(daily_budget=10.0, per_conversation_budget=2.0)
        await g.record(1.0, "claude-3-opus", "c1", provider="anthropic", tokens=500)
        await g.record(0.5, "gpt-4o", "c2", provider="openai", tokens=300)
        await g.record(0.0, "llama3", "c3", provider="ollama", tokens=100)

        bd = g.get_breakdown("day")
        assert bd.total_cost == pytest.approx(1.5)
        assert bd.total_tokens == 900
        assert len(bd.by_provider) == 3
        assert bd.by_provider["anthropic"] == pytest.approx(1.0)
        assert bd.by_provider["openai"] == pytest.approx(0.5)
        assert bd.by_provider["ollama"] == pytest.approx(0.0)

    async def test_breakdown_multiple_minds(self) -> None:
        g = CostGuard(daily_budget=10.0, per_conversation_budget=2.0)
        await g.record(0.8, "model", "c1", mind_id="main", tokens=400)
        await g.record(0.3, "model", "c2", mind_id="research", tokens=150)
        await g.record(0.1, "model", "c3", mind_id="creative", tokens=50)

        bd = g.get_breakdown("day")
        assert bd.by_mind["main"] == pytest.approx(0.8)
        assert bd.by_mind["research"] == pytest.approx(0.3)
        assert bd.by_mind["creative"] == pytest.approx(0.1)
        assert bd.tokens_by_mind["main"] == 400
        assert bd.tokens_by_mind["research"] == 150

    async def test_breakdown_by_model(self) -> None:
        g = CostGuard(daily_budget=10.0, per_conversation_budget=2.0)
        await g.record(1.0, "claude-3-opus", "c1", provider="anthropic")
        await g.record(0.5, "claude-3-opus", "c2", provider="anthropic")
        await g.record(0.2, "claude-3-haiku", "c3", provider="anthropic")

        bd = g.get_breakdown("day")
        assert bd.by_model["claude-3-opus"] == pytest.approx(1.5)
        assert bd.by_model["claude-3-haiku"] == pytest.approx(0.2)

    async def test_breakdown_unsupported_period(self) -> None:
        g = CostGuard(daily_budget=10.0, per_conversation_budget=2.0)
        with pytest.raises(ValueError, match="Unsupported period"):
            g.get_breakdown("week")

    async def test_breakdown_resets_on_new_day(self) -> None:
        from datetime import timedelta
        from unittest.mock import patch

        g = CostGuard(daily_budget=10.0, per_conversation_budget=2.0)
        await g.record(1.0, "model", "c1", provider="anthropic", mind_id="main", tokens=100)

        bd = g.get_breakdown("day")
        assert bd.total_cost == pytest.approx(1.0)

        tomorrow = datetime.now(tz=UTC) + timedelta(days=1)
        with patch.object(_cost_mod, "datetime") as mock_dt:
            mock_dt.now.return_value = tomorrow
            mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
            bd = g.get_breakdown("day")
            assert bd.total_cost == 0.0
            assert bd.by_provider == {}
            assert bd.by_mind == {}

    async def test_record_without_provider_mind(self) -> None:
        """Record without provider/mind tracks daily/model AND buckets the
        missing-axis attribution into ``_unresolved`` (v0.41.3 defensive fix
        — anti-pattern #35 surface in cost-tracking layer per
        ``GAPS-CONSOLIDATED-2026-05-13.md`` §2.5).
        """
        g = CostGuard(daily_budget=10.0, per_conversation_budget=2.0)
        await g.record(0.5, "model", "c1")
        assert g.get_daily_spend() == pytest.approx(0.5)
        bd = g.get_breakdown("day")
        assert bd.by_model["model"] == pytest.approx(0.5)
        # v0.41.3: empty provider/mind no longer silently dropped — they
        # land in the ``_unresolved`` bucket + emit a structured WARN.
        assert bd.by_provider == {"_unresolved": pytest.approx(0.5)}
        assert bd.by_mind == {"_unresolved": pytest.approx(0.5)}


class TestProviderMindQueries:
    """Tests for per-provider and per-mind query methods."""

    async def test_get_provider_spend_unknown(self) -> None:
        g = CostGuard(daily_budget=10.0, per_conversation_budget=2.0)
        assert g.get_provider_spend("unknown") == 0.0

    async def test_get_mind_spend_unknown(self) -> None:
        g = CostGuard(daily_budget=10.0, per_conversation_budget=2.0)
        assert g.get_mind_spend("unknown") == 0.0

    async def test_get_model_spend_unknown(self) -> None:
        g = CostGuard(daily_budget=10.0, per_conversation_budget=2.0)
        assert g.get_model_spend("unknown") == 0.0

    async def test_get_provider_spend_accumulated(self) -> None:
        g = CostGuard(daily_budget=10.0, per_conversation_budget=2.0)
        await g.record(0.5, "m1", "c1", provider="anthropic")
        await g.record(0.3, "m2", "c2", provider="anthropic")
        assert g.get_provider_spend("anthropic") == pytest.approx(0.8)

    async def test_get_mind_spend_accumulated(self) -> None:
        g = CostGuard(daily_budget=10.0, per_conversation_budget=2.0)
        await g.record(0.2, "m1", "c1", mind_id="main")
        await g.record(0.4, "m2", "c2", mind_id="main")
        assert g.get_mind_spend("main") == pytest.approx(0.6)


class TestCostBreakdownDataclass:
    """CostBreakdown dataclass behavior."""

    def test_frozen(self) -> None:
        bd = CostBreakdown(total_cost=1.0)
        with pytest.raises(AttributeError):
            bd.total_cost = 2.0  # type: ignore[misc]

    def test_defaults(self) -> None:
        bd = CostBreakdown()
        assert bd.total_cost == 0.0
        assert bd.total_tokens == 0
        assert bd.by_provider == {}
        assert bd.by_mind == {}
        assert bd.by_model == {}
        assert bd.tokens_by_provider == {}
        assert bd.tokens_by_mind == {}


class TestCostHistory:
    """Cost log ring buffer for dashboard charts."""

    async def test_empty_history(self) -> None:
        g = CostGuard(daily_budget=10.0, per_conversation_budget=2.0)
        assert g.get_cost_history() == []

    async def test_record_populates_history(self) -> None:
        g = CostGuard(daily_budget=10.0, per_conversation_budget=2.0)
        await g.record(0.01, "gpt-4o", "c1", provider="openai", tokens=100)
        history = g.get_cost_history()
        assert len(history) == 1
        entry = history[0]
        assert entry["cost"] == 0.01
        assert entry["model"] == "gpt-4o"
        assert entry["cumulative"] == 0.01
        assert isinstance(entry["time"], int)
        assert entry["time"] > 0

    async def test_cumulative_grows(self) -> None:
        g = CostGuard(daily_budget=10.0, per_conversation_budget=2.0)
        await g.record(0.01, "gpt-4o", "c1", provider="openai", tokens=100)
        await g.record(0.02, "gpt-4o", "c1", provider="openai", tokens=200)
        await g.record(0.03, "claude", "c1", provider="anthropic", tokens=150)
        history = g.get_cost_history()
        assert len(history) == 3  # noqa: PLR2004
        assert history[0]["cumulative"] == 0.01
        assert history[1]["cumulative"] == 0.03
        assert history[2]["cumulative"] == 0.06

    async def test_ring_buffer_max_size(self) -> None:
        from sovyx.llm.cost import _MAX_COST_LOG

        g = CostGuard(daily_budget=1000.0, per_conversation_budget=1000.0)
        for i in range(_MAX_COST_LOG + 50):
            await g.record(0.001, f"m{i}", "c1")
        history = g.get_cost_history()
        assert len(history) == _MAX_COST_LOG
        # Oldest entries evicted — first entry model should be "m50"
        assert history[0]["model"] == "m50"

    async def test_daily_reset_clears_history(self) -> None:
        from datetime import date as datemod
        from unittest.mock import patch

        g = CostGuard(daily_budget=10.0, per_conversation_budget=2.0)
        await g.record(0.01, "gpt-4o", "c1")
        assert len(g.get_cost_history()) == 1

        # Simulate next day
        tomorrow = datemod(2099, 1, 2)
        with patch.object(_cost_mod, "datetime") as mock_dt:
            mock_dt.now.return_value = type("DT", (), {"date": lambda self: tomorrow})()
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            # Force reset by accessing
            history = g.get_cost_history()
            assert len(history) == 0

    async def test_history_entries_have_correct_structure(self) -> None:
        g = CostGuard(daily_budget=10.0, per_conversation_budget=2.0)
        await g.record(0.005, "claude-sonnet-4-20250514", "c1", provider="anthropic", tokens=500)
        entry = g.get_cost_history()[0]
        assert set(entry.keys()) == {"time", "cost", "model", "cumulative"}


class TestCostGuardTimezone:
    """CostGuard timezone-aware day boundary."""

    async def test_uses_custom_timezone(self) -> None:
        """Day boundary uses user timezone, not UTC."""
        g = CostGuard(daily_budget=10.0, per_conversation_budget=2.0, timezone="America/Sao_Paulo")
        from zoneinfo import ZoneInfo

        assert g._tz == ZoneInfo("America/Sao_Paulo")

    async def test_default_utc(self) -> None:
        """Default timezone is UTC."""
        g = CostGuard(daily_budget=10.0, per_conversation_budget=2.0)
        from zoneinfo import ZoneInfo

        assert g._tz == ZoneInfo("UTC")


class TestCostGuardDaySnapshot:
    """Day boundary snapshot buffering + flush."""

    async def test_maybe_reset_buffers_snapshot(self) -> None:
        """_maybe_reset stores previous day's data."""
        from datetime import date as datemod
        from unittest.mock import patch

        g = CostGuard(daily_budget=10.0, per_conversation_budget=2.0)
        # Record some data
        await g.record(0.50, "gpt-4o", "c1", provider="openai", tokens=1000)
        await g.record(0.25, "gpt-4o-mini", "c1", provider="openai", tokens=500)

        assert g._pending_day_snapshot is None

        # Force day change
        tomorrow = datemod(2099, 1, 2)
        with patch.object(_cost_mod, "datetime") as mock_dt:
            mock_dt.now.return_value = type("DT", (), {"date": lambda self: tomorrow})()
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            g._maybe_reset()

        snap = g._pending_day_snapshot
        assert snap is not None
        assert snap["cost_usd"] == pytest.approx(0.75)
        assert snap["tokens"] == 1500
        assert snap["cost_by_provider"]["openai"] == pytest.approx(0.75)
        assert "gpt-4o" in snap["cost_by_model"]

    async def test_no_snapshot_on_first_reset(self) -> None:
        """First _maybe_reset (init) should NOT buffer a snapshot."""
        g = CostGuard(daily_budget=10.0, per_conversation_budget=2.0)
        g._maybe_reset()
        assert g._pending_day_snapshot is None

    async def test_persist_flushes_combined_snapshot(self, tmp_path: Path) -> None:
        """persist() merges CostGuard + DashboardCounters into daily_stats."""
        from sovyx.dashboard.daily_stats import DailyStatsRecorder
        from sovyx.persistence.migrations import MigrationRunner
        from sovyx.persistence.pool import DatabasePool
        from sovyx.persistence.schemas.system import get_system_migrations

        pool = DatabasePool(db_path=tmp_path / "system.db", read_pool_size=1)
        await pool.initialize()
        runner = MigrationRunner(pool)
        await runner.initialize()
        await runner.run_migrations(get_system_migrations())

        recorder = DailyStatsRecorder(pool)
        g = CostGuard(
            daily_budget=10.0,
            per_conversation_budget=2.0,
            system_pool=pool,
            stats_recorder=recorder,
        )

        # Simulate: had data yesterday, now day changed
        g._pending_day_snapshot = {
            "date": "2026-04-09",
            "cost_usd": 1.50,
            "tokens": 3000,
            "cost_by_provider": {"openai": 1.0, "anthropic": 0.5},
            "cost_by_model": {"gpt-4o": 1.0, "gpt-4o-mini": 0.5},
        }

        # Also simulate DashboardCounters having pending data
        from sovyx.dashboard.status import get_counters

        counters = get_counters()
        original_pending = counters._pending_day_snapshot
        counters._pending_day_snapshot = {
            "date": "2026-04-09",
            "messages": 20,
            "llm_calls": 8,
            "tokens": 3000,
            "llm_cost": 1.50,
        }

        g._dirty = True
        await g.persist()

        # Verify daily_stats row was created with merged data
        async with pool.read() as conn:
            cursor = await conn.execute(
                "SELECT messages, llm_calls, cost_usd FROM daily_stats WHERE date = '2026-04-09'"
            )
            row = await cursor.fetchone()

        assert row is not None
        assert row[0] == 20  # messages from DashboardCounters
        assert row[1] == 8  # llm_calls from DashboardCounters
        assert row[2] == pytest.approx(1.50)  # cost from CostGuard

        # Cleanup
        counters._pending_day_snapshot = original_pending
        await pool.close()

    async def test_restore_snapshots_stale_data(self, tmp_path: Path) -> None:
        """Stale data in engine_state is snapshotted to daily_stats on restore."""
        from sovyx.dashboard.daily_stats import DailyStatsRecorder
        from sovyx.persistence.migrations import MigrationRunner
        from sovyx.persistence.pool import DatabasePool
        from sovyx.persistence.schemas.system import get_system_migrations

        pool = DatabasePool(db_path=tmp_path / "system.db", read_pool_size=1)
        await pool.initialize()
        runner = MigrationRunner(pool)
        await runner.initialize()
        await runner.run_migrations(get_system_migrations())

        # Write stale state (yesterday)
        import json

        state = json.dumps(
            {
                "date": "2020-01-01",
                "daily_spend": 2.50,
                "total_tokens": 5000,
                "provider_spend": {"openai": 2.50},
                "model_spend": {"gpt-4o": 2.50},
                "conversation_spend": {},
                "mind_spend": {},
                "provider_tokens": {},
                "mind_tokens": {},
                "cost_log": [],
            }
        )
        async with pool.write() as conn:
            await conn.execute(
                "INSERT INTO engine_state (key, value) VALUES (?, ?)",
                ("cost_guard_state", state),
            )
            await conn.commit()

        recorder = DailyStatsRecorder(pool)
        g = CostGuard(
            daily_budget=10.0,
            per_conversation_budget=2.0,
            system_pool=pool,
            stats_recorder=recorder,
        )
        await g.restore()

        # Stale data should be snapshotted to daily_stats
        async with pool.read() as conn:
            cursor = await conn.execute(
                "SELECT cost_usd, tokens FROM daily_stats WHERE date = '2020-01-01'"
            )
            row = await cursor.fetchone()

        assert row is not None
        assert row[0] == pytest.approx(2.50)
        assert row[1] == 5000

        # CostGuard should be fresh (today)
        assert g._daily_spend == 0.0

        await pool.close()

    async def test_restore_no_recorder_skips_snapshot(self, tmp_path: Path) -> None:
        """Without stats_recorder, stale data is silently discarded."""
        from sovyx.persistence.migrations import MigrationRunner
        from sovyx.persistence.pool import DatabasePool
        from sovyx.persistence.schemas.system import get_system_migrations

        pool = DatabasePool(db_path=tmp_path / "system.db", read_pool_size=1)
        await pool.initialize()
        runner = MigrationRunner(pool)
        await runner.initialize()
        await runner.run_migrations(get_system_migrations())

        import json

        state = json.dumps(
            {
                "date": "2020-01-01",
                "daily_spend": 2.50,
                "total_tokens": 5000,
                "conversation_spend": {},
                "provider_spend": {},
                "mind_spend": {},
                "model_spend": {},
                "provider_tokens": {},
                "mind_tokens": {},
                "cost_log": [],
            }
        )
        async with pool.write() as conn:
            await conn.execute(
                "INSERT INTO engine_state (key, value) VALUES (?, ?)",
                ("cost_guard_state", state),
            )
            await conn.commit()

        # No stats_recorder
        g = CostGuard(
            daily_budget=10.0,
            per_conversation_budget=2.0,
            system_pool=pool,
        )
        await g.restore()  # Should not raise
        assert g._daily_spend == 0.0

        # No row in daily_stats
        async with pool.read() as conn:
            cursor = await conn.execute("SELECT COUNT(*) FROM daily_stats")
            row = await cursor.fetchone()
        assert row is not None
        assert row[0] == 0

        await pool.close()


class TestPhaseBreakdown:
    """Issue #43 — cost attributed to cognitive phases."""

    async def test_record_with_phase_tag(self) -> None:
        g = CostGuard(daily_budget=10.0, per_conversation_budget=2.0)
        await g.record(0.5, "m", "c1", phase="think", tokens=1000)
        await g.record(0.2, "m", "c1", phase="reflect", tokens=400)
        await g.record(0.1, "m", "c1", phase="dream", tokens=200)

        breakdown = g.get_breakdown("day")
        assert breakdown.by_phase["think"] == pytest.approx(0.5)
        assert breakdown.by_phase["reflect"] == pytest.approx(0.2)
        assert breakdown.by_phase["dream"] == pytest.approx(0.1)
        assert breakdown.tokens_by_phase["think"] == 1000
        assert breakdown.tokens_by_phase["reflect"] == 400
        assert breakdown.tokens_by_phase["dream"] == 200

    async def test_empty_phase_buckets_as_unknown(self) -> None:
        # Calls without an explicit phase tag (chat, plugin tooling)
        # MUST always have an answer in the breakdown, not silently
        # disappear from the dashboard.
        g = CostGuard(daily_budget=10.0, per_conversation_budget=2.0)
        await g.record(0.3, "m", "c1", tokens=500)  # phase omitted

        breakdown = g.get_breakdown("day")
        assert breakdown.by_phase == {"unknown": pytest.approx(0.3)}
        assert breakdown.tokens_by_phase == {"unknown": 500}

    async def test_phase_spend_aggregates_across_calls(self) -> None:
        g = CostGuard(daily_budget=10.0, per_conversation_budget=10.0)
        for _ in range(3):
            await g.record(0.1, "m", "c1", phase="think", tokens=100)

        breakdown = g.get_breakdown("day")
        assert breakdown.by_phase["think"] == pytest.approx(0.3)
        assert breakdown.tokens_by_phase["think"] == 300

    async def test_maybe_reset_clears_phase_totals(self) -> None:
        from datetime import timedelta

        g = CostGuard(daily_budget=10.0, per_conversation_budget=10.0)
        await g.record(0.5, "m", "c1", phase="think", tokens=500)

        # Force a day rollover.
        g._last_reset = datetime.now(tz=UTC).date() - timedelta(days=1)
        g._maybe_reset()

        breakdown = g.get_breakdown("day")
        assert breakdown.by_phase == {}
        assert breakdown.tokens_by_phase == {}

    async def test_total_cost_is_sum_of_phase_costs(self) -> None:
        g = CostGuard(daily_budget=10.0, per_conversation_budget=10.0)
        await g.record(0.5, "m", "c1", phase="think")
        await g.record(0.3, "m", "c1", phase="reflect")
        await g.record(0.1, "m", "c1")  # bucketed as "unknown"

        breakdown = g.get_breakdown("day")
        assert sum(breakdown.by_phase.values()) == pytest.approx(0.9)
        assert breakdown.total_cost == pytest.approx(0.9)


class TestMonthlyBudget:
    """Issue #42 — monthly budget cap on top of daily + per-conversation."""

    async def test_default_monthly_budget_is_disabled(self) -> None:
        # Backwards-compat: omitting monthly_budget keeps the cap off.
        g = CostGuard(daily_budget=10.0, per_conversation_budget=2.0)
        assert g._monthly_budget is None
        assert g.get_remaining_monthly_budget() is None
        # A spend much larger than any sensible monthly cap is allowed.
        await g.record(5.0, "m", "c1")
        assert g.can_afford(5.0) is True

    async def test_monthly_cap_blocks_spend_over_limit(self) -> None:
        # Simulate spending across multiple days within the month.
        # Daily 10 USD, monthly 15 USD: after one full daily spend the
        # monthly counter sits at 10/15. A daily rollover (next day)
        # zeroes daily but keeps monthly at 10 — the next 6 USD call
        # exceeds the monthly cap even though daily has full headroom.
        from datetime import timedelta

        g = CostGuard(
            daily_budget=10.0,
            per_conversation_budget=10.0,
            monthly_budget=15.0,
        )
        await g.record(10.0, "m", "c1")
        # Force a day rollover (within the same month).
        g._last_reset = datetime.now(tz=UTC).date() - timedelta(days=1)
        g._maybe_reset()

        assert g.get_daily_spend() == 0.0
        assert g.get_monthly_spend() == pytest.approx(10.0)

        # Daily has 10 USD headroom; monthly only has 5 USD left.
        assert g.can_afford(6.0) is False  # blocked by monthly
        assert g.can_afford(4.0) is True  # under monthly cap

    async def test_monthly_spend_persists_across_day_boundary(self) -> None:
        g = CostGuard(
            daily_budget=10.0,
            per_conversation_budget=10.0,
            monthly_budget=20.0,
        )
        await g.record(8.0, "m", "c1")
        assert g.get_monthly_spend() == pytest.approx(8.0)

        # Force a day rollover within the same month.
        from datetime import timedelta

        g._last_reset = datetime.now(tz=UTC).date() - timedelta(days=1)
        g._maybe_reset()
        # Daily zeroed, monthly preserved.
        assert g.get_daily_spend() == 0.0
        assert g.get_monthly_spend() == pytest.approx(8.0)

    async def test_month_boundary_zeroes_monthly_spend(self) -> None:
        g = CostGuard(
            daily_budget=10.0,
            per_conversation_budget=10.0,
            monthly_budget=20.0,
        )
        await g.record(8.0, "m", "c1")

        # Force a month rollover by clearing the reset key to a past month.
        prev_year, prev_month = g._last_month_reset
        prev = (
            prev_year - 1 if prev_month == 1 else prev_year,
            12 if prev_month == 1 else prev_month - 1,
        )
        g._last_month_reset = prev
        g._maybe_reset_month()

        assert g.get_monthly_spend() == 0.0

    async def test_remaining_monthly_budget_with_cap(self) -> None:
        g = CostGuard(
            daily_budget=10.0,
            per_conversation_budget=10.0,
            monthly_budget=30.0,
        )
        await g.record(7.5, "m", "c1")
        assert g.get_remaining_monthly_budget() == pytest.approx(22.5)

    async def test_remaining_monthly_clamps_at_zero(self) -> None:
        g = CostGuard(
            daily_budget=10.0,
            per_conversation_budget=10.0,
            monthly_budget=5.0,
        )
        # The cap rejects this, but if the spend gets through some other
        # path (manual record), get_remaining must clamp at zero.
        await g.record(7.0, "m", "c1")
        assert g.get_remaining_monthly_budget() == 0.0

    async def test_daily_cap_still_enforced_with_monthly_set(self) -> None:
        g = CostGuard(
            daily_budget=2.0,
            per_conversation_budget=10.0,
            monthly_budget=100.0,
        )
        await g.record(1.5, "m", "c1")
        # 1.0 USD > remaining daily 0.5 — daily MUST reject regardless of
        # how much monthly budget remains.
        assert g.can_afford(1.0) is False


class TestCacheTokenTracking:
    """Issue #44 — CostGuard separately tracks cache tokens."""

    async def test_record_accepts_cache_kwargs(self) -> None:
        g = CostGuard(daily_budget=10.0, per_conversation_budget=2.0)
        await g.record(
            0.5,
            "claude-sonnet-4-20250514",
            "conv1",
            tokens=1000,
            cache_read_tokens=900,
            cache_creation_tokens=200,
        )
        assert g._cache_read_tokens == 900
        assert g._cache_creation_tokens == 200

    async def test_breakdown_exposes_cache_totals(self) -> None:
        g = CostGuard(daily_budget=10.0, per_conversation_budget=2.0)
        await g.record(
            0.5,
            "claude-sonnet-4-20250514",
            "conv1",
            cache_read_tokens=500,
            cache_creation_tokens=100,
        )
        await g.record(
            0.3,
            "claude-sonnet-4-20250514",
            "conv1",
            cache_read_tokens=200,
        )

        breakdown = g.get_breakdown("day")
        assert breakdown.cache_read_tokens == 700
        assert breakdown.cache_creation_tokens == 100

    async def test_default_cache_kwargs_are_zero(self) -> None:
        # Pre-issue-#44 callers (router stream/generate without cache
        # extraction yet) record with no cache kwargs — counters stay 0.
        g = CostGuard(daily_budget=10.0, per_conversation_budget=2.0)
        await g.record(0.5, "model", "conv1", tokens=100)
        assert g._cache_read_tokens == 0
        assert g._cache_creation_tokens == 0

    async def test_maybe_reset_clears_cache_totals(self) -> None:
        g = CostGuard(daily_budget=10.0, per_conversation_budget=2.0)
        await g.record(
            0.5,
            "claude-sonnet-4-20250514",
            "conv1",
            cache_read_tokens=500,
            cache_creation_tokens=100,
        )
        # Force a date rollover.
        from datetime import timedelta

        g._last_reset = datetime.now(tz=UTC).date() - timedelta(days=1)
        g._maybe_reset()

        assert g._cache_read_tokens == 0
        assert g._cache_creation_tokens == 0
        assert g.get_breakdown("day").cache_read_tokens == 0


class TestUnresolvedAttribution:
    """``record()`` defensive behaviour when caller omits mind_id/provider.

    Closes ``GAPS-CONSOLIDATED-2026-05-13.md`` §2.5 — pre-v0.41.3 the
    method silently dropped per-mind / per-provider attribution when the
    field was empty. v0.41.3 buckets to ``_UNRESOLVED_BUCKET`` and emits
    a structured WARN so the silent surface becomes visible.
    """

    async def test_empty_mind_id_buckets_unresolved_and_warns(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        warnings: list[tuple[str, dict[str, object]]] = []
        monkeypatch.setattr(
            _cost_mod.logger,
            "warning",
            lambda event, **kw: warnings.append((event, kw)),
        )

        g = CostGuard(daily_budget=10.0, per_conversation_budget=2.0)
        await g.record(
            0.5,
            "claude-sonnet-4-20250514",
            "conv1",
            provider="anthropic",
            tokens=200,
            # mind_id intentionally omitted — sentinel default ""
        )

        assert g._mind_spend["_unresolved"] == pytest.approx(0.5)
        assert g._mind_tokens["_unresolved"] == 200
        assert "_unresolved" not in g._provider_spend  # provider was explicit
        assert any(
            event == "llm.cost.unresolved_attribution"
            and kw.get("mind_unresolved") is True
            and kw.get("provider_unresolved") is False
            for event, kw in warnings
        )

    async def test_empty_provider_buckets_unresolved_and_warns(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        warnings: list[tuple[str, dict[str, object]]] = []
        monkeypatch.setattr(
            _cost_mod.logger,
            "warning",
            lambda event, **kw: warnings.append((event, kw)),
        )

        g = CostGuard(daily_budget=10.0, per_conversation_budget=2.0)
        await g.record(
            0.3,
            "gpt-4o",
            "conv1",
            mind_id="jonny",
            tokens=100,
            # provider intentionally omitted — sentinel default ""
        )

        assert g._provider_spend["_unresolved"] == pytest.approx(0.3)
        assert g._provider_tokens["_unresolved"] == 100
        assert g._mind_spend["jonny"] == pytest.approx(0.3)  # mind was explicit
        assert "_unresolved" not in g._mind_spend
        assert any(
            event == "llm.cost.unresolved_attribution"
            and kw.get("provider_unresolved") is True
            and kw.get("mind_unresolved") is False
            for event, kw in warnings
        )

    async def test_both_empty_buckets_both_with_single_warn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        warnings: list[tuple[str, dict[str, object]]] = []
        monkeypatch.setattr(
            _cost_mod.logger,
            "warning",
            lambda event, **kw: warnings.append((event, kw)),
        )

        g = CostGuard(daily_budget=10.0, per_conversation_budget=2.0)
        await g.record(0.7, "model-x", "conv1", tokens=50)

        # Both sentinels triggered → both buckets carry the attribution.
        assert g._provider_spend["_unresolved"] == pytest.approx(0.7)
        assert g._mind_spend["_unresolved"] == pytest.approx(0.7)
        # Single WARN emitted (not one per axis) — caller path is one slip,
        # one log entry.
        attr_warns = [w for w in warnings if w[0] == "llm.cost.unresolved_attribution"]
        assert len(attr_warns) == 1
        assert attr_warns[0][1]["provider_unresolved"] is True
        assert attr_warns[0][1]["mind_unresolved"] is True

    async def test_explicit_mind_and_provider_no_warn_no_unresolved(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        warnings: list[tuple[str, dict[str, object]]] = []
        monkeypatch.setattr(
            _cost_mod.logger,
            "warning",
            lambda event, **kw: warnings.append((event, kw)),
        )

        g = CostGuard(daily_budget=10.0, per_conversation_budget=2.0)
        await g.record(
            0.4,
            "claude-haiku-4-5-20251001",
            "conv1",
            provider="anthropic",
            mind_id="jonny",
            tokens=400,
        )

        # Happy path preserved — explicit attribution flows into the
        # right buckets, no WARN fires, no _unresolved leakage.
        assert g._provider_spend["anthropic"] == pytest.approx(0.4)
        assert g._mind_spend["jonny"] == pytest.approx(0.4)
        assert "_unresolved" not in g._provider_spend
        assert "_unresolved" not in g._mind_spend
        assert not any(w[0] == "llm.cost.unresolved_attribution" for w in warnings)

    async def test_unresolved_persists_through_breakdown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Silence the WARN spam so the test only exercises bucket behaviour.
        monkeypatch.setattr(_cost_mod.logger, "warning", lambda *a, **kw: None)

        g = CostGuard(daily_budget=10.0, per_conversation_budget=2.0)
        await g.record(0.2, "m", "conv1", tokens=10)
        bd = g.get_breakdown("day")
        # The fallback bucket surfaces on the dashboard breakdown — operators
        # see a non-zero `_unresolved` row + know to thread mind_id upstream.
        assert bd.by_mind.get("_unresolved") == pytest.approx(0.2)
        assert bd.by_provider.get("_unresolved") == pytest.approx(0.2)
