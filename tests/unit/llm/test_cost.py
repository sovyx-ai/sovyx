"""Tests for sovyx.llm.cost — CostGuard."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sovyx.llm.cost import CostGuard


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
        stale = json.dumps({
            "date": "1999-01-01",
            "daily_spend": 99.0,
            "conversation_spend": {"old": 50.0},
        })
        async with pool.write() as conn:
            await conn.execute(
                "INSERT INTO engine_state (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
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
