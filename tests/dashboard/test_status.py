"""Tests for sovyx.dashboard.status — StatusCollector and DashboardCounters."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sovyx import __version__
from sovyx.dashboard.status import (
    DashboardCounters,
    StatusCollector,
    StatusSnapshot,
    get_counters,
)

if TYPE_CHECKING:
    from pathlib import Path

    from sovyx.persistence.pool import DatabasePool


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

    def test_snapshot_returns_tuple(self) -> None:
        c = DashboardCounters()
        c.record_llm_call(cost=0.1, tokens=200)
        c.record_message()
        calls, cost, tokens, msgs = c.snapshot()
        assert calls == 1
        assert cost == pytest.approx(0.1)
        assert tokens == 200
        assert msgs == 1

    def test_snapshot_resets_on_day_boundary(self) -> None:
        c = DashboardCounters()
        c._day_key = "2020-01-01"
        c.llm_calls = 99
        calls, _, _, _ = c.snapshot()
        assert calls == 0  # Reset happened

    def test_get_counters_singleton(self) -> None:
        c1 = get_counters()
        c2 = get_counters()
        assert c1 is c2


class TestStatusSnapshot:
    def test_to_dict(self) -> None:
        snap = StatusSnapshot(
            version=__version__,
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
        assert d["version"] == __version__
        assert d["uptime_seconds"] == 3661.6  # rounded to 1 decimal
        assert d["mind_name"] == "Nyx"
        assert d["active_conversations"] == 3
        assert d["memory_concepts"] == 150
        assert d["memory_episodes"] == 42
        assert d["llm_cost_today"] == 0.1235  # rounded to 4 decimals
        assert d["llm_calls_today"] == 7
        assert d["tokens_today"] == 1500
        assert d["messages_today"] == 25


class TestStatusCollector:
    @pytest.mark.asyncio()
    async def test_collect_with_empty_registry(self) -> None:
        registry = MagicMock()
        registry.is_registered.return_value = False

        collector = StatusCollector(registry, start_time=time.time() - 100)
        snap = await collector.collect()

        assert snap.version == __version__
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

    @pytest.mark.asyncio()
    async def test_collect_with_concept_and_episode_repos(self) -> None:
        """Cover _get_memory_stats with both repos registered."""
        mock_concept_repo = AsyncMock()
        mock_concept_repo.count = AsyncMock(return_value=42)

        mock_episode_repo = AsyncMock()
        mock_episode_repo.count = AsyncMock(return_value=15)

        registry = MagicMock()

        def is_registered(cls: type) -> bool:
            from sovyx.brain.concept_repo import ConceptRepository
            from sovyx.brain.episode_repo import EpisodeRepository

            return cls in (ConceptRepository, EpisodeRepository)

        registry.is_registered.side_effect = is_registered

        async def resolve(cls: type) -> AsyncMock:
            from sovyx.brain.concept_repo import ConceptRepository
            from sovyx.brain.episode_repo import EpisodeRepository

            if cls is ConceptRepository:
                return mock_concept_repo
            if cls is EpisodeRepository:
                return mock_episode_repo
            msg = f"Unknown: {cls}"
            raise ValueError(msg)

        registry.resolve = AsyncMock(side_effect=resolve)

        collector = StatusCollector(registry)
        snap = await collector.collect()

        assert snap.memory_concepts == 42
        assert snap.memory_episodes == 15

    @pytest.mark.asyncio()
    async def test_collect_episode_repo_error_returns_zero(self) -> None:
        """Cover the episode exception path in _get_memory_stats."""
        mock_concept_repo = AsyncMock()
        mock_concept_repo.count = AsyncMock(return_value=10)

        registry = MagicMock()

        def is_registered(cls: type) -> bool:
            from sovyx.brain.concept_repo import ConceptRepository
            from sovyx.brain.episode_repo import EpisodeRepository

            return cls in (ConceptRepository, EpisodeRepository)

        registry.is_registered.side_effect = is_registered

        async def resolve(cls: type) -> AsyncMock:
            from sovyx.brain.concept_repo import ConceptRepository

            if cls is ConceptRepository:
                return mock_concept_repo
            raise RuntimeError("episode repo broken")

        registry.resolve = AsyncMock(side_effect=resolve)

        collector = StatusCollector(registry)
        snap = await collector.collect()

        assert snap.memory_concepts == 10
        assert snap.memory_episodes == 0  # Fallback on error

    @pytest.mark.asyncio()
    async def test_collect_conversation_count(self) -> None:
        """Cover _get_conversation_count success path."""
        registry = MagicMock()
        registry.is_registered.return_value = False

        with patch(
            "sovyx.dashboard.status.StatusCollector._get_conversation_count",
            new_callable=AsyncMock,
            return_value=7,
        ):
            collector = StatusCollector(registry)
            snap = await collector.collect()

        assert snap.active_conversations == 7

    @pytest.mark.asyncio()
    async def test_collect_conversation_count_error(self) -> None:
        """Cover _get_conversation_count error → returns 0."""
        registry = MagicMock()
        registry.is_registered.return_value = False

        with patch(
            "sovyx.dashboard.conversations.count_active_conversations",
            new_callable=AsyncMock,
            side_effect=RuntimeError("no conversations"),
        ):
            collector = StatusCollector(registry)
            snap = await collector.collect()

        assert snap.active_conversations == 0

    @pytest.mark.asyncio()
    async def test_default_start_time(self) -> None:
        """Cover start_time=None → defaults to time.time()."""
        registry = MagicMock()
        registry.is_registered.return_value = False

        collector = StatusCollector(registry)  # No start_time
        snap = await collector.collect()
        assert snap.uptime_seconds >= 0
        assert snap.uptime_seconds < 5  # Should be very small


class TestCostHistory:
    """StatusCollector.cost_history integration."""

    @pytest.mark.asyncio()
    async def test_cost_history_in_snapshot(self) -> None:
        """StatusSnapshot includes cost_history when CostGuard available."""
        from sovyx.llm.cost import CostGuard

        guard = CostGuard(daily_budget=10.0, per_conversation_budget=2.0)
        await guard.record(0.01, "gpt-4o", "c1", provider="openai", tokens=100)

        registry = MagicMock()
        registry.is_registered.side_effect = lambda t: t is CostGuard
        registry.resolve = AsyncMock(return_value=guard)

        collector = StatusCollector(registry)
        snap = await collector.collect()
        d = snap.to_dict()

        assert "cost_history" in d
        assert len(d["cost_history"]) == 1
        assert d["cost_history"][0]["model"] == "gpt-4o"

    @pytest.mark.asyncio()
    async def test_cost_history_empty_when_no_guard(self) -> None:
        """cost_history is empty list when CostGuard not registered."""
        registry = MagicMock()
        registry.is_registered.return_value = False

        collector = StatusCollector(registry)
        snap = await collector.collect()
        assert snap.cost_history == []
        assert snap.to_dict()["cost_history"] == []

    @pytest.mark.asyncio()
    async def test_cost_history_error_returns_empty(self) -> None:
        """cost_history gracefully returns [] on resolve error."""
        from sovyx.llm.cost import CostGuard

        registry = MagicMock()
        registry.is_registered.side_effect = lambda t: t is CostGuard
        registry.resolve = AsyncMock(side_effect=RuntimeError("broken"))

        collector = StatusCollector(registry)
        snap = await collector.collect()
        assert snap.cost_history == []


class TestCountersPersistence:
    """DashboardCounters persist/restore — mirrors CostGuard pattern."""

    @pytest.fixture
    async def pool(self, tmp_path: Path) -> DatabasePool:
        """Pool with engine_state table."""
        from sovyx.persistence.migrations import MigrationRunner
        from sovyx.persistence.pool import DatabasePool
        from sovyx.persistence.schemas.system import get_system_migrations

        p = DatabasePool(db_path=tmp_path / "system.db", read_pool_size=1)
        await p.initialize()
        runner = MigrationRunner(p)
        await runner.initialize()
        await runner.run_migrations(get_system_migrations())
        yield p  # type: ignore[misc]
        await p.close()

    async def test_persist_writes_state(self, pool: DatabasePool) -> None:
        c = DashboardCounters()
        c._system_pool = pool
        c.record_llm_call(cost=0.05, tokens=500)
        c.record_message()
        c.record_message()

        await c.persist()

        async with pool.read() as conn:
            cursor = await conn.execute(
                "SELECT value FROM engine_state WHERE key = ?",
                ("dashboard_counters_state",),
            )
            row = await cursor.fetchone()
        assert row is not None
        import json

        state = json.loads(row[0])
        assert state["llm_calls"] == 1
        assert state["messages_received"] == 2
        assert state["tokens"] == 500
        assert state["llm_cost"] == pytest.approx(0.05)

    async def test_restore_same_day(self, pool: DatabasePool) -> None:
        """Counters are restored when saved_date == today."""
        c = DashboardCounters()
        c._system_pool = pool
        c.record_llm_call(cost=1.50, tokens=3000)
        c.record_message()
        c.record_message()
        c.record_message()
        await c.persist()

        # Create a fresh instance and restore
        c2 = DashboardCounters()
        c2._system_pool = pool
        await c2.restore()

        assert c2.llm_calls == 1
        assert c2.llm_cost == pytest.approx(1.50)
        assert c2.tokens == 3000
        assert c2.messages_received == 3

    async def test_restore_stale_buffers_snapshot(self, pool: DatabasePool) -> None:
        """Stale data (different day) is buffered for DailyStatsRecorder."""
        import json

        # Write state with yesterday's date
        yesterday = "2020-01-01"
        state = json.dumps(
            {
                "date": yesterday,
                "llm_calls": 10,
                "llm_cost": 2.50,
                "tokens": 5000,
                "messages_received": 20,
            }
        )
        async with pool.write() as conn:
            await conn.execute(
                "INSERT INTO engine_state (key, value) VALUES (?, ?)",
                ("dashboard_counters_state", state),
            )
            await conn.commit()

        c = DashboardCounters()
        c._system_pool = pool
        await c.restore()

        # Counters should be fresh (zeroed)
        assert c.llm_calls == 0
        assert c.messages_received == 0

        # But stale data is buffered
        snap = c.consume_pending_day_snapshot()
        assert snap is not None
        assert snap["date"] == yesterday
        assert snap["messages"] == 20
        assert snap["llm_calls"] == 10
        assert snap["tokens"] == 5000

    async def test_restore_no_pool(self) -> None:
        """No pool → restore is a no-op."""
        c = DashboardCounters()
        await c.restore()  # Should not raise
        assert c.llm_calls == 0

    async def test_restore_no_state(self, pool: DatabasePool) -> None:
        """No saved state → restore is a no-op."""
        c = DashboardCounters()
        c._system_pool = pool
        await c.restore()
        assert c.llm_calls == 0

    async def test_dirty_flag_prevents_redundant_writes(self, pool: DatabasePool) -> None:
        """persist() is a no-op when not dirty."""
        c = DashboardCounters()
        c._system_pool = pool
        c.record_message()
        await c.persist()
        assert not c._dirty

        # Second persist should be a no-op
        await c.persist()  # Should not raise, should not write

    async def test_persist_no_pool_is_noop(self) -> None:
        """No pool → persist is a no-op."""
        c = DashboardCounters()
        c.record_message()
        await c.persist()  # Should not raise

    async def test_persist_retry_on_failure(self, pool: DatabasePool) -> None:
        """Failed persist re-marks dirty for retry."""
        c = DashboardCounters()
        c._system_pool = pool
        c.record_message()

        # Close pool to cause write failure
        await pool.close()
        await c.persist()

        # Should be re-marked as dirty
        assert c._dirty

    async def test_day_boundary_buffers_previous(self) -> None:
        """Day boundary buffers previous day's data."""
        c = DashboardCounters()
        c._day_key = "2026-04-10"
        c.llm_calls = 5
        c.llm_cost = 1.0
        c.tokens = 1000
        c.messages_received = 10

        with patch("sovyx.dashboard.status._now_date_str", return_value="2026-04-11"):
            c.record_message()

        snap = c.consume_pending_day_snapshot()
        assert snap is not None
        assert snap["date"] == "2026-04-10"
        assert snap["messages"] == 10
        assert snap["llm_calls"] == 5
        assert snap["tokens"] == 1000

        # Counters were reset: 0 + 1 = 1
        assert c.messages_received == 1
        assert c.llm_calls == 0

    async def test_consume_pending_clears(self) -> None:
        """consume_pending_day_snapshot returns None after first call."""
        c = DashboardCounters()
        c._day_key = "2026-04-10"
        c.messages_received = 5

        with patch("sovyx.dashboard.status._now_date_str", return_value="2026-04-11"):
            c.record_message()

        assert c.consume_pending_day_snapshot() is not None
        assert c.consume_pending_day_snapshot() is None

    async def test_restart_mid_day_preserves(self, pool: DatabasePool) -> None:
        """Full cycle: record → persist → new instance → restore → check."""
        c1 = DashboardCounters()
        c1._system_pool = pool
        for _ in range(5):
            c1.record_llm_call(cost=0.10, tokens=200)
        for _ in range(8):
            c1.record_message()
        await c1.persist()

        # Simulate restart
        c2 = DashboardCounters()
        c2._system_pool = pool
        await c2.restore()

        assert c2.llm_calls == 5
        assert c2.llm_cost == pytest.approx(0.50)
        assert c2.tokens == 1000
        assert c2.messages_received == 8

        # Continue accumulating
        c2.record_message()
        assert c2.messages_received == 9


class TestTimezoneCounters:
    """Daily counter reset respects user timezone."""

    def test_utc_default_backwards_compatible(self) -> None:
        """Default timezone is UTC."""
        from zoneinfo import ZoneInfo

        c = DashboardCounters()
        assert c._tz == ZoneInfo("UTC")

    def test_custom_timezone(self) -> None:
        """Can create counters with custom timezone."""
        from zoneinfo import ZoneInfo

        c = DashboardCounters(timezone="America/Sao_Paulo")
        assert c._tz == ZoneInfo("America/Sao_Paulo")

    def test_sao_paulo_same_day_keeps_counters(self) -> None:
        """When _now_date_str returns same day, counters are not reset."""
        from unittest.mock import patch

        c = DashboardCounters(timezone="America/Sao_Paulo")
        c._day_key = "2026-04-10"
        c.llm_calls = 42

        with patch("sovyx.dashboard.status._now_date_str", return_value="2026-04-10"):
            c.record_llm_call(cost=0.01, tokens=10)

        # Should NOT reset — same day in São Paulo
        assert c.llm_calls == 43  # 42 + 1

    def test_sao_paulo_new_day_resets_counters(self) -> None:
        """When _now_date_str returns next day, counters reset."""
        from unittest.mock import patch

        c = DashboardCounters(timezone="America/Sao_Paulo")
        c._day_key = "2026-04-10"
        c.llm_calls = 42

        with patch("sovyx.dashboard.status._now_date_str", return_value="2026-04-11"):
            c.record_llm_call(cost=0.01, tokens=10)

        # Should reset: 0 + 1 = 1
        assert c.llm_calls == 1

    def test_configure_timezone_updates_singleton(self) -> None:
        """configure_timezone updates the global singleton."""
        from zoneinfo import ZoneInfo

        from sovyx.dashboard.status import configure_timezone, get_counters

        original_tz = get_counters()._tz
        try:
            configure_timezone("Asia/Tokyo")
            assert get_counters()._tz == ZoneInfo("Asia/Tokyo")
        finally:
            # Restore
            get_counters()._tz = original_tz

    def test_configure_timezone_invalid_falls_back_to_utc(self) -> None:
        """Invalid timezone string falls back to UTC."""
        from zoneinfo import ZoneInfo

        from sovyx.dashboard.status import configure_timezone, get_counters

        original_tz = get_counters()._tz
        try:
            configure_timezone("Not/A/Timezone")
            assert get_counters()._tz == ZoneInfo("UTC")
        finally:
            get_counters()._tz = original_tz


class TestStatusSnapshotContextFields:
    """StatusSnapshot timezone/today_date/has_lifetime_activity fields."""

    def test_to_dict_includes_context_fields(self) -> None:
        snap = StatusSnapshot(
            version="0.5.27",
            uptime_seconds=100.0,
            mind_name="aria",
            active_conversations=0,
            memory_concepts=5,
            memory_episodes=3,
            llm_cost_today=0.5,
            llm_calls_today=10,
            tokens_today=5000,
            messages_today=2,
            timezone="America/Sao_Paulo",
            today_date="2026-04-10",
            has_lifetime_activity=True,
        )
        d = snap.to_dict()
        assert d["timezone"] == "America/Sao_Paulo"
        assert d["today_date"] == "2026-04-10"
        assert d["has_lifetime_activity"] is True

    def test_has_lifetime_false_on_fresh(self) -> None:
        snap = StatusSnapshot(
            version="0.5.27",
            uptime_seconds=10.0,
            mind_name="aria",
            active_conversations=0,
            memory_concepts=0,
            memory_episodes=0,
            llm_cost_today=0.0,
            llm_calls_today=0,
            tokens_today=0,
            messages_today=0,
            has_lifetime_activity=False,
        )
        assert snap.has_lifetime_activity is False

    def test_has_lifetime_true_with_concepts(self) -> None:
        snap = StatusSnapshot(
            version="0.5.27",
            uptime_seconds=10.0,
            mind_name="aria",
            active_conversations=0,
            memory_concepts=3,
            memory_episodes=0,
            llm_cost_today=0.0,
            llm_calls_today=0,
            tokens_today=0,
            messages_today=0,
            has_lifetime_activity=True,
        )
        assert snap.has_lifetime_activity is True

    def test_defaults(self) -> None:
        snap = StatusSnapshot(
            version="0.5.27",
            uptime_seconds=0.0,
            mind_name="x",
            active_conversations=0,
            memory_concepts=0,
            memory_episodes=0,
            llm_cost_today=0.0,
            llm_calls_today=0,
            tokens_today=0,
            messages_today=0,
        )
        assert snap.timezone == "UTC"
        assert snap.today_date == ""
        assert snap.has_lifetime_activity is False
