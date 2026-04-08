"""Tests for sovyx.llm.cost — CostGuard + CostBreakdown."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sovyx.llm.cost import CostBreakdown, CostGuard


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
        with patch("sovyx.llm.cost.datetime") as mock_dt:
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
        with patch("sovyx.llm.cost.datetime") as mock_dt:
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
        """Restore with broken pool → no crash (except path)."""
        from unittest.mock import AsyncMock, MagicMock

        mock_pool = MagicMock()
        # Make read() context manager raise
        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(side_effect=RuntimeError("db broken"))
        cm.__aexit__ = AsyncMock(return_value=False)
        mock_pool.read.return_value = cm

        g = CostGuard(daily_budget=10.0, per_conversation_budget=2.0, system_pool=mock_pool)
        await g.restore()  # Should not raise
        assert g.get_daily_spend() == 0.0

    async def test_persist_db_error(self) -> None:
        """Persist with broken pool → no crash (except path)."""
        from unittest.mock import AsyncMock, MagicMock

        mock_pool = MagicMock()
        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(side_effect=RuntimeError("db broken"))
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
        with patch("sovyx.llm.cost.datetime") as mock_dt:
            mock_dt.now.return_value = tomorrow
            mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
            bd = g.get_breakdown("day")
            assert bd.total_cost == 0.0
            assert bd.by_provider == {}
            assert bd.by_mind == {}

    async def test_record_without_provider_mind(self) -> None:
        """Record without provider/mind still tracks daily and model."""
        g = CostGuard(daily_budget=10.0, per_conversation_budget=2.0)
        await g.record(0.5, "model", "c1")
        assert g.get_daily_spend() == pytest.approx(0.5)
        bd = g.get_breakdown("day")
        assert bd.by_model["model"] == pytest.approx(0.5)
        assert bd.by_provider == {}
        assert bd.by_mind == {}


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
        with patch("sovyx.llm.cost.datetime") as mock_dt:
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
