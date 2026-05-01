"""Tests for :class:`sovyx.mind.retention.MindRetentionService` — Phase 8 / T8.21 step 6.

Sibling test file to ``test_forget.py``. Covers:

* Horizon resolution: global default vs mind-config override; 0 = disabled.
* Per-surface prune: episodes / conversations (cascade) / consolidation_log /
  daily_stats / consent ledger.
* Cross-mind isolation under retention prune.
* Dry-run reports counts without writing.
* Empty / whitespace mind_id rejected.
* Tombstone shape (RETENTION_PURGE not DELETE).
* ``effective_horizons`` field accuracy.
* Disabled surface (horizon=0) is fully skipped (no count, no delete).

Reference: master mission ``MISSION-voice-final-skype-grade-2026.md``
§Phase 8 / T8.21 (step 6 of the per-mind compliance staged adoption);
``OPERATOR-DEBT-MASTER-2026-05-01.md`` D9/D17/D18 (defaults
ratified).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from sovyx.engine.config import (
    DatabaseConfig,
    EngineConfig,
    RetentionTuningConfig,
    TuningConfig,
)
from sovyx.engine.types import MindId
from sovyx.mind.config import MindConfig, MindRetentionConfig
from sovyx.mind.retention import MindRetentionReport, MindRetentionService
from sovyx.persistence.migrations import MigrationRunner
from sovyx.persistence.pool import DatabasePool
from sovyx.persistence.schemas.brain import get_brain_migrations
from sovyx.persistence.schemas.conversations import get_conversation_migrations
from sovyx.persistence.schemas.system import get_system_migrations
from sovyx.voice._consent_ledger import ConsentAction, ConsentLedger

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from sovyx.brain.embedding import EmbeddingEngine


MIND_A = MindId("aria")
MIND_B = MindId("luna")

# Fixed "now" for deterministic cutoff math throughout this file.
_NOW = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
async def brain_pool(tmp_path: Path) -> AsyncIterator[DatabasePool]:
    p = DatabasePool(
        db_path=tmp_path / "brain.db",
        read_pool_size=1,
        load_extensions=["vec0"],
    )
    await p.initialize()
    runner = MigrationRunner(p)
    await runner.initialize()
    await runner.run_migrations(get_brain_migrations(has_sqlite_vec=p.has_sqlite_vec))
    yield p
    await p.close()


@pytest.fixture
async def conv_pool(tmp_path: Path) -> AsyncIterator[DatabasePool]:
    p = DatabasePool(db_path=tmp_path / "conversations.db", read_pool_size=1)
    await p.initialize()
    runner = MigrationRunner(p)
    await runner.initialize()
    await runner.run_migrations(get_conversation_migrations())
    yield p
    await p.close()


@pytest.fixture
async def system_pool(tmp_path: Path) -> AsyncIterator[DatabasePool]:
    p = DatabasePool(db_path=tmp_path / "system.db", read_pool_size=1)
    await p.initialize()
    runner = MigrationRunner(p)
    await runner.initialize()
    await runner.run_migrations(get_system_migrations())
    yield p
    await p.close()


@pytest.fixture
def mock_embedding() -> EmbeddingEngine:
    engine = AsyncMock()
    engine.has_embeddings = False
    engine.encode = AsyncMock(return_value=[0.1] * 384)
    return engine


def _engine_config(
    *,
    tmp_path: Path,
    episodes_days: int = 30,
    conversations_days: int = 30,
    consolidation_log_days: int = 90,
    daily_stats_days: int = 365,
    consent_ledger_days: int = 0,
) -> EngineConfig:
    """Build EngineConfig with custom retention horizons."""
    retention = RetentionTuningConfig(
        episodes_days=episodes_days,
        conversations_days=conversations_days,
        consolidation_log_days=consolidation_log_days,
        daily_stats_days=daily_stats_days,
        consent_ledger_days=consent_ledger_days,
    )
    tuning = TuningConfig(retention=retention)
    return EngineConfig(
        data_dir=tmp_path,
        database=DatabaseConfig(data_dir=tmp_path),
        tuning=tuning,
    )


# ── Helpers — seed data with explicit timestamps ─────────────────────


async def _insert_episode_with_timestamp(
    *,
    pool: DatabasePool,
    mind_id: MindId,
    created_at: datetime,
    suffix: str = "",
) -> None:
    """Insert an episode with an explicit created_at timestamp."""
    async with pool.transaction() as conn:
        await conn.execute(
            """INSERT INTO episodes
               (id, mind_id, conversation_id, user_input, assistant_response,
                summary, importance, emotional_valence, emotional_arousal,
                concepts_mentioned, metadata, created_at)
               VALUES (?, ?, ?, ?, ?, NULL, 0.5, 0.0, 0.0, '[]', '{}', ?)""",
            (
                f"ep-{mind_id}-{created_at.isoformat()}-{suffix}",
                str(mind_id),
                f"conv-{mind_id}",
                "hi",
                "hello",
                created_at.isoformat(),
            ),
        )


async def _insert_consolidation_log(
    *,
    pool: DatabasePool,
    mind_id: MindId,
    started_at: datetime,
) -> None:
    async with pool.transaction() as conn:
        await conn.execute(
            """INSERT INTO consolidation_log
               (mind_id, started_at, completed_at, concepts_created, duration_ms)
               VALUES (?, ?, ?, 0, 1)""",
            (str(mind_id), started_at.isoformat(), started_at.isoformat()),
        )


async def _insert_conversation_with_last_message(
    *,
    pool: DatabasePool,
    mind_id: MindId,
    last_message_at: datetime,
    suffix: str = "",
) -> None:
    """Insert a conversation with explicit last_message_at + 2 turns."""
    conv_id = f"conv-{mind_id}-{last_message_at.isoformat()}-{suffix}"
    async with pool.transaction() as conn:
        await conn.execute(
            """INSERT INTO conversations
               (id, mind_id, channel, started_at, last_message_at)
               VALUES (?, ?, 'test', ?, ?)""",
            (conv_id, str(mind_id), last_message_at.isoformat(), last_message_at.isoformat()),
        )
        for i in range(2):
            await conn.execute(
                """INSERT INTO conversation_turns
                   (id, conversation_id, role, content)
                   VALUES (?, ?, 'user', ?)""",
                (f"{conv_id}-t{i}", conv_id, f"hi {i}"),
            )


async def _insert_daily_stat(
    *,
    pool: DatabasePool,
    mind_id: MindId,
    date_str: str,
) -> None:
    async with pool.transaction() as conn:
        await conn.execute(
            "INSERT INTO daily_stats (date, mind_id, messages) VALUES (?, ?, 0)",
            (date_str, str(mind_id)),
        )


async def _table_count(pool: DatabasePool, sql: str, params: tuple) -> int:
    async with pool.read() as conn:
        cursor = await conn.execute(sql, params)
        row = await cursor.fetchone()
        return int(row[0]) if row else 0


# ── Validation guards ────────────────────────────────────────────────


class TestValidation:
    @pytest.mark.parametrize("bad", ["", "   ", "\t", "\n"])
    @pytest.mark.asyncio
    async def test_empty_mind_id_rejected(
        self,
        tmp_path: Path,
        brain_pool: DatabasePool,
        bad: str,
    ) -> None:
        config = _engine_config(tmp_path=tmp_path)
        service = MindRetentionService(
            engine_config=config,
            brain_pool=brain_pool,
        )
        with pytest.raises(ValueError, match="non-empty mind_id"):
            await service.prune_mind(MindId(bad))


# ── Horizon resolution ───────────────────────────────────────────────


class TestHorizonResolution:
    @pytest.mark.asyncio
    async def test_global_defaults_applied_when_no_mind_config(
        self,
        tmp_path: Path,
        brain_pool: DatabasePool,
    ) -> None:
        config = _engine_config(
            tmp_path=tmp_path,
            episodes_days=42,
            conversations_days=7,
            consolidation_log_days=14,
            daily_stats_days=180,
            consent_ledger_days=0,
        )
        service = MindRetentionService(
            engine_config=config,
            brain_pool=brain_pool,
        )
        report = await service.prune_mind(MIND_A, dry_run=True, now=_NOW)
        assert report.effective_horizons == {
            "episodes": 42,
            "conversations": 7,
            "consolidation_log": 14,
            "daily_stats": 180,
            "consent_ledger": 0,
        }

    @pytest.mark.asyncio
    async def test_mind_config_override_takes_precedence(
        self,
        tmp_path: Path,
        brain_pool: DatabasePool,
    ) -> None:
        config = _engine_config(tmp_path=tmp_path, episodes_days=30)
        service = MindRetentionService(
            engine_config=config,
            brain_pool=brain_pool,
        )
        mind_config = MindConfig(
            name="aria",
            id=MIND_A,
            retention=MindRetentionConfig(episodes_days=90),
        )
        report = await service.prune_mind(
            MIND_A,
            mind_config=mind_config,
            dry_run=True,
            now=_NOW,
        )
        # Episodes overridden to 90, others inherit from global.
        assert report.effective_horizons["episodes"] == 90  # noqa: PLR2004
        assert report.effective_horizons["conversations"] == 30  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_none_in_mind_config_inherits_global(
        self,
        tmp_path: Path,
        brain_pool: DatabasePool,
    ) -> None:
        config = _engine_config(tmp_path=tmp_path, episodes_days=30)
        service = MindRetentionService(
            engine_config=config,
            brain_pool=brain_pool,
        )
        mind_config = MindConfig(
            name="aria",
            id=MIND_A,
            retention=MindRetentionConfig(episodes_days=None),
        )
        report = await service.prune_mind(
            MIND_A,
            mind_config=mind_config,
            dry_run=True,
            now=_NOW,
        )
        assert report.effective_horizons["episodes"] == 30  # noqa: PLR2004


# ── Episodes prune ───────────────────────────────────────────────────


class TestEpisodesPrune:
    @pytest.mark.asyncio
    async def test_old_episodes_pruned_recent_kept(
        self,
        tmp_path: Path,
        brain_pool: DatabasePool,
    ) -> None:
        # Seed: 1 old (60 days ago), 1 recent (5 days ago).
        old_ts = _NOW - timedelta(days=60)
        recent_ts = _NOW - timedelta(days=5)
        await _insert_episode_with_timestamp(
            pool=brain_pool,
            mind_id=MIND_A,
            created_at=old_ts,
            suffix="old",
        )
        await _insert_episode_with_timestamp(
            pool=brain_pool,
            mind_id=MIND_A,
            created_at=recent_ts,
            suffix="recent",
        )

        config = _engine_config(tmp_path=tmp_path, episodes_days=30)
        service = MindRetentionService(
            engine_config=config,
            brain_pool=brain_pool,
        )
        report = await service.prune_mind(MIND_A, now=_NOW)

        assert report.episodes_purged == 1
        # Recent episode survives.
        remaining = await _table_count(
            brain_pool,
            "SELECT COUNT(*) FROM episodes WHERE mind_id = ?",
            (str(MIND_A),),
        )
        assert remaining == 1

    @pytest.mark.asyncio
    async def test_horizon_zero_skips_surface(
        self,
        tmp_path: Path,
        brain_pool: DatabasePool,
    ) -> None:
        """``episodes_days = 0`` means retention disabled — old
        episodes are KEPT, count stays 0."""
        old_ts = _NOW - timedelta(days=365)
        await _insert_episode_with_timestamp(
            pool=brain_pool,
            mind_id=MIND_A,
            created_at=old_ts,
        )

        config = _engine_config(tmp_path=tmp_path, episodes_days=0)
        service = MindRetentionService(
            engine_config=config,
            brain_pool=brain_pool,
        )
        report = await service.prune_mind(MIND_A, now=_NOW)

        assert report.episodes_purged == 0
        remaining = await _table_count(
            brain_pool,
            "SELECT COUNT(*) FROM episodes WHERE mind_id = ?",
            (str(MIND_A),),
        )
        assert remaining == 1  # old episode survives — surface skipped

    @pytest.mark.asyncio
    async def test_cross_mind_isolation_under_prune(
        self,
        tmp_path: Path,
        brain_pool: DatabasePool,
    ) -> None:
        """Pruning mind_a doesn't touch mind_b's episodes — even
        when both have old episodes."""
        old_ts = _NOW - timedelta(days=60)
        await _insert_episode_with_timestamp(
            pool=brain_pool,
            mind_id=MIND_A,
            created_at=old_ts,
        )
        await _insert_episode_with_timestamp(
            pool=brain_pool,
            mind_id=MIND_B,
            created_at=old_ts,
        )

        config = _engine_config(tmp_path=tmp_path, episodes_days=30)
        service = MindRetentionService(
            engine_config=config,
            brain_pool=brain_pool,
        )
        await service.prune_mind(MIND_A, now=_NOW)

        # Mind B old episode survives.
        b_count = await _table_count(
            brain_pool,
            "SELECT COUNT(*) FROM episodes WHERE mind_id = ?",
            (str(MIND_B),),
        )
        assert b_count == 1


# ── Consolidation log prune ──────────────────────────────────────────


class TestConsolidationLogPrune:
    @pytest.mark.asyncio
    async def test_old_consolidation_log_pruned(
        self,
        tmp_path: Path,
        brain_pool: DatabasePool,
    ) -> None:
        old_ts = _NOW - timedelta(days=120)
        recent_ts = _NOW - timedelta(days=30)
        await _insert_consolidation_log(
            pool=brain_pool,
            mind_id=MIND_A,
            started_at=old_ts,
        )
        await _insert_consolidation_log(
            pool=brain_pool,
            mind_id=MIND_A,
            started_at=recent_ts,
        )

        config = _engine_config(tmp_path=tmp_path, consolidation_log_days=90)
        service = MindRetentionService(
            engine_config=config,
            brain_pool=brain_pool,
        )
        report = await service.prune_mind(MIND_A, now=_NOW)

        assert report.consolidation_log_purged == 1


# ── Conversations prune (cascade turns) ──────────────────────────────


class TestConversationsPrune:
    @pytest.mark.asyncio
    async def test_old_conversations_purged_with_cascade(
        self,
        tmp_path: Path,
        brain_pool: DatabasePool,
        conv_pool: DatabasePool,
    ) -> None:
        old_ts = _NOW - timedelta(days=60)
        recent_ts = _NOW - timedelta(days=5)
        await _insert_conversation_with_last_message(
            pool=conv_pool,
            mind_id=MIND_A,
            last_message_at=old_ts,
            suffix="old",
        )
        await _insert_conversation_with_last_message(
            pool=conv_pool,
            mind_id=MIND_A,
            last_message_at=recent_ts,
            suffix="recent",
        )

        config = _engine_config(tmp_path=tmp_path, conversations_days=30)
        service = MindRetentionService(
            engine_config=config,
            brain_pool=brain_pool,
            conversations_pool=conv_pool,
        )
        report = await service.prune_mind(MIND_A, now=_NOW)

        assert report.conversations_purged == 1
        # 2 turns per conversation; old conversation contributed 2 turns.
        assert report.conversation_turns_purged == 2  # noqa: PLR2004


# ── Daily stats prune ────────────────────────────────────────────────


class TestDailyStatsPrune:
    @pytest.mark.asyncio
    async def test_old_daily_stats_pruned(
        self,
        tmp_path: Path,
        brain_pool: DatabasePool,
        system_pool: DatabasePool,
    ) -> None:
        # Two date strings: one well past 365d horizon, one recent.
        old_date = (_NOW - timedelta(days=400)).date().isoformat()
        recent_date = (_NOW - timedelta(days=10)).date().isoformat()
        await _insert_daily_stat(
            pool=system_pool,
            mind_id=MIND_A,
            date_str=old_date,
        )
        await _insert_daily_stat(
            pool=system_pool,
            mind_id=MIND_A,
            date_str=recent_date,
        )

        config = _engine_config(tmp_path=tmp_path, daily_stats_days=365)
        service = MindRetentionService(
            engine_config=config,
            brain_pool=brain_pool,
            system_pool=system_pool,
        )
        report = await service.prune_mind(MIND_A, now=_NOW)

        assert report.daily_stats_purged == 1


# ── Consent ledger prune ─────────────────────────────────────────────


class TestConsentLedgerPrune:
    @pytest.mark.asyncio
    async def test_consent_ledger_pruned_with_retention_purge_tombstone(
        self,
        tmp_path: Path,
        brain_pool: DatabasePool,
    ) -> None:
        # Seed ledger with old + recent records via injectable clocks.
        ledger_path = tmp_path / "voice" / "consent.jsonl"
        ledger_path.parent.mkdir(parents=True, exist_ok=True)

        old_ts = _NOW - timedelta(days=400)
        recent_ts = _NOW - timedelta(days=10)
        # Old record.
        ledger_old = ConsentLedger(ledger_path, clock=lambda: old_ts)
        ledger_old.append(user_id="u1", action=ConsentAction.WAKE, mind_id="aria")
        # Recent record.
        ledger_new = ConsentLedger(ledger_path, clock=lambda: recent_ts)
        ledger_new.append(user_id="u1", action=ConsentAction.LISTEN, mind_id="aria")

        config = _engine_config(tmp_path=tmp_path, consent_ledger_days=365)
        service = MindRetentionService(
            engine_config=config,
            brain_pool=brain_pool,
            ledger=ledger_new,
        )
        report = await service.prune_mind(MIND_A, now=_NOW)

        assert report.consent_ledger_purged == 1
        # Tombstone is RETENTION_PURGE not DELETE.
        history = ledger_new.history(mind_id="aria")
        actions = {r.action for r in history}
        assert ConsentAction.RETENTION_PURGE in actions
        assert ConsentAction.DELETE not in actions
        assert ConsentAction.LISTEN in actions  # recent survived
        assert ConsentAction.WAKE not in actions  # old purged


# ── Dry run ──────────────────────────────────────────────────────────


class TestDryRun:
    @pytest.mark.asyncio
    async def test_dry_run_returns_counts_without_writing(
        self,
        tmp_path: Path,
        brain_pool: DatabasePool,
    ) -> None:
        old_ts = _NOW - timedelta(days=60)
        await _insert_episode_with_timestamp(
            pool=brain_pool,
            mind_id=MIND_A,
            created_at=old_ts,
        )

        config = _engine_config(tmp_path=tmp_path, episodes_days=30)
        service = MindRetentionService(
            engine_config=config,
            brain_pool=brain_pool,
        )
        report = await service.prune_mind(MIND_A, dry_run=True, now=_NOW)

        assert report.dry_run is True
        assert report.episodes_purged == 1
        # No actual delete.
        remaining = await _table_count(
            brain_pool,
            "SELECT COUNT(*) FROM episodes WHERE mind_id = ?",
            (str(MIND_A),),
        )
        assert remaining == 1

    @pytest.mark.asyncio
    async def test_dry_run_does_not_touch_consent_ledger(
        self,
        tmp_path: Path,
        brain_pool: DatabasePool,
    ) -> None:
        ledger_path = tmp_path / "voice" / "consent.jsonl"
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        old_ts = _NOW - timedelta(days=400)
        ledger = ConsentLedger(ledger_path, clock=lambda: old_ts)
        ledger.append(user_id="u1", action=ConsentAction.WAKE, mind_id="aria")

        config = _engine_config(tmp_path=tmp_path, consent_ledger_days=365)
        service = MindRetentionService(
            engine_config=config,
            brain_pool=brain_pool,
            ledger=ledger,
        )
        report = await service.prune_mind(MIND_A, dry_run=True, now=_NOW)

        # Dry run: ledger purged count is always 0; original record survives.
        assert report.consent_ledger_purged == 0
        history = ledger.history(mind_id="aria")
        assert len(history) == 1
        assert history[0].action is ConsentAction.WAKE


# ── Report shape ─────────────────────────────────────────────────────


class TestReport:
    def test_total_aggregates_compose_correctly(self) -> None:
        report = MindRetentionReport(
            mind_id=MIND_A,
            cutoff_utc="2026-04-01T00:00:00+00:00",
            episodes_purged=5,
            conversations_purged=3,
            conversation_turns_purged=10,
            daily_stats_purged=2,
            consolidation_log_purged=1,
            consent_ledger_purged=99,
            effective_horizons={
                "episodes": 30,
                "conversations": 30,
                "consolidation_log": 90,
                "daily_stats": 365,
                "consent_ledger": 0,
            },
            dry_run=False,
        )
        # 5 episodes + 1 consolidation_log = 6 brain
        assert report.total_brain_rows_purged == 6  # noqa: PLR2004
        # 3 conversations + 10 turns = 13 conversations-pool
        assert report.total_conversations_rows_purged == 13  # noqa: PLR2004
        # 2 daily_stats = 2 system-pool
        assert report.total_system_rows_purged == 2  # noqa: PLR2004
        # 6 + 13 + 2 = 21 (excludes consent ledger)
        assert report.total_rows_purged == 21  # noqa: PLR2004

    def test_report_is_immutable(self) -> None:
        report = MindRetentionReport(
            mind_id=MIND_A,
            cutoff_utc="x",
            episodes_purged=0,
            conversations_purged=0,
            conversation_turns_purged=0,
            daily_stats_purged=0,
            consolidation_log_purged=0,
            consent_ledger_purged=0,
            effective_horizons={},
            dry_run=True,
        )
        with pytest.raises((AttributeError, TypeError)):
            report.episodes_purged = 99  # type: ignore[misc]


# ── RetentionScheduler — auto-prune cycle (Phase 8 / T8.21 step 6) ───


class TestRetentionScheduler:
    """``RetentionScheduler`` lifecycle + timing arithmetic.

    Mirrors ``TestDreamScheduler`` patterns from the brain test
    suite — start/stop idempotency + injectable ``now`` for
    deterministic seconds-until-next-prune assertions.
    """

    @pytest.mark.asyncio
    async def test_seconds_until_next_prune_today_future(
        self,
        tmp_path: Path,
        brain_pool: DatabasePool,
    ) -> None:
        """When ``prune_time`` is later today, returns the delta."""
        from sovyx.mind.retention import RetentionScheduler

        config = _engine_config(tmp_path=tmp_path)
        service = MindRetentionService(engine_config=config, brain_pool=brain_pool)
        scheduler = RetentionScheduler(service, prune_time="03:00", timezone="UTC")
        # 02:00 → 03:00 today is 3600 seconds.
        now = datetime(2026, 5, 1, 2, 0, 0, tzinfo=UTC)
        delta = scheduler._seconds_until_next_prune(now=now)  # noqa: SLF001
        assert delta == 3600.0  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_seconds_until_next_prune_rolls_to_tomorrow(
        self,
        tmp_path: Path,
        brain_pool: DatabasePool,
    ) -> None:
        from sovyx.mind.retention import RetentionScheduler

        config = _engine_config(tmp_path=tmp_path)
        service = MindRetentionService(engine_config=config, brain_pool=brain_pool)
        scheduler = RetentionScheduler(service, prune_time="03:00", timezone="UTC")
        # 04:00 → 03:00 tomorrow = 23 hours.
        now = datetime(2026, 5, 1, 4, 0, 0, tzinfo=UTC)
        delta = scheduler._seconds_until_next_prune(now=now)  # noqa: SLF001
        assert delta == 23 * 3600.0  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_malformed_prune_time_falls_back_to_03_00(
        self,
        tmp_path: Path,
        brain_pool: DatabasePool,
    ) -> None:
        """A garbage ``prune_time`` MUST NOT prevent the scheduler from
        instantiating — retention is privacy-sensitive; failing closed
        = no retention = storage limitation breach risk."""
        from sovyx.mind.retention import RetentionScheduler

        config = _engine_config(tmp_path=tmp_path)
        service = MindRetentionService(engine_config=config, brain_pool=brain_pool)
        scheduler = RetentionScheduler(service, prune_time="not-a-time", timezone="UTC")
        # Fallback 03:00 → from 02:00 next prune is in 1 hour.
        now = datetime(2026, 5, 1, 2, 0, 0, tzinfo=UTC)
        delta = scheduler._seconds_until_next_prune(now=now)  # noqa: SLF001
        assert delta == 3600.0  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_unknown_timezone_falls_back_to_utc(
        self,
        tmp_path: Path,
        brain_pool: DatabasePool,
    ) -> None:
        from sovyx.mind.retention import RetentionScheduler

        config = _engine_config(tmp_path=tmp_path)
        service = MindRetentionService(engine_config=config, brain_pool=brain_pool)
        scheduler = RetentionScheduler(
            service,
            prune_time="03:00",
            timezone="Not/AReal_Timezone",
        )
        now = datetime(2026, 5, 1, 2, 0, 0, tzinfo=UTC)
        delta = scheduler._seconds_until_next_prune(now=now)  # noqa: SLF001
        assert delta == 3600.0  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_start_idempotent_does_not_spawn_two_tasks(
        self,
        tmp_path: Path,
        brain_pool: DatabasePool,
    ) -> None:
        """Calling ``start`` twice is a no-op (matches DreamScheduler)."""
        from sovyx.mind.retention import RetentionScheduler

        config = _engine_config(tmp_path=tmp_path)
        service = MindRetentionService(engine_config=config, brain_pool=brain_pool)
        scheduler = RetentionScheduler(service, prune_time="03:00")
        await scheduler.start(MIND_A)
        first_task = scheduler._task  # noqa: SLF001
        await scheduler.start(MIND_A)
        assert scheduler._task is first_task  # noqa: SLF001
        await scheduler.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_task_and_is_idempotent(
        self,
        tmp_path: Path,
        brain_pool: DatabasePool,
    ) -> None:
        from sovyx.mind.retention import RetentionScheduler

        config = _engine_config(tmp_path=tmp_path)
        service = MindRetentionService(engine_config=config, brain_pool=brain_pool)
        scheduler = RetentionScheduler(service, prune_time="03:00")
        await scheduler.start(MIND_A)
        assert scheduler.is_running is True
        await scheduler.stop()
        assert scheduler.is_running is False
        # Second stop is a no-op.
        await scheduler.stop()
        assert scheduler.is_running is False

    @pytest.mark.asyncio
    async def test_stop_without_start_is_safe(
        self,
        tmp_path: Path,
        brain_pool: DatabasePool,
    ) -> None:
        from sovyx.mind.retention import RetentionScheduler

        config = _engine_config(tmp_path=tmp_path)
        service = MindRetentionService(engine_config=config, brain_pool=brain_pool)
        scheduler = RetentionScheduler(service, prune_time="03:00")
        # Never started — stop should not raise.
        await scheduler.stop()
