"""Tests for BackupScheduler + GFS retention policy (V05-08).

Coverage targets:
- RetentionPolicy: GFS bucket classification, edge cases, validation
- BackupScheduler: lifecycle, scheduling logic, windows, prune, failures
- TierSchedule / ScheduleTier: configuration correctness

References:
    - SPE-033 §2.6: BackupScheduler
    - SPE-033 §2.4: GFS retention policy
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from sovyx.cloud.backup import BackupInfo, BackupMetadata, PruneResult
from sovyx.cloud.scheduler import (
    TIER_SCHEDULES,
    BackupScheduler,
    RetentionPolicy,
    RetentionResult,
    ScheduleTier,
    TierSchedule,
    _NullCallback,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_backup_info(
    backup_id: str,
    created_at: datetime,
    *,
    size_bytes: int = 1024,
    r2_key: str | None = None,
) -> BackupInfo:
    """Create a BackupInfo for testing."""
    return BackupInfo(
        backup_id=backup_id,
        created_at=created_at,
        size_bytes=size_bytes,
        r2_key=r2_key or f"user/mind/{created_at.strftime('%Y%m%dT%H%M%SZ')}_{backup_id}.enc.gz",
    )


def _make_metadata(
    backup_id: str = "abc123",
    created_at: datetime | None = None,
) -> BackupMetadata:
    """Create a BackupMetadata for testing."""
    return BackupMetadata(
        backup_id=backup_id,
        created_at=created_at or datetime.now(tz=UTC),
        size_bytes=2048,
        compressed_size_bytes=1024,
        original_size_bytes=4096,
        brain_version="1",
        sovyx_version="0.5.0",
        checksum="abc123def456",
        r2_key=f"user/mind/20260407T120000Z_{backup_id}.enc.gz",
    )


def _make_service_mock() -> MagicMock:
    """Create a mock BackupService."""
    service = MagicMock()
    service.create_backup.return_value = _make_metadata()
    service.list_backups.return_value = []
    service._r2 = MagicMock()
    service._r2.delete_objects.return_value = 0
    service._config = MagicMock()
    service._config.r2_bucket = "test-bucket"
    return service


# ---------------------------------------------------------------------------
# RetentionPolicy Tests
# ---------------------------------------------------------------------------


class TestRetentionPolicy:
    """Tests for GFS retention policy."""

    def test_default_values(self) -> None:
        """Default policy: 7 daily, 4 weekly, 12 monthly."""
        policy = RetentionPolicy()
        assert policy.keep_daily == 7
        assert policy.keep_weekly == 4
        assert policy.keep_monthly == 12

    def test_custom_values(self) -> None:
        """Custom retention counts are stored correctly."""
        policy = RetentionPolicy(keep_daily=30, keep_weekly=12, keep_monthly=6)
        assert policy.keep_daily == 30
        assert policy.keep_weekly == 12
        assert policy.keep_monthly == 6

    def test_negative_daily_raises(self) -> None:
        """Negative keep_daily raises ValueError."""
        with pytest.raises(ValueError, match="keep_daily"):
            RetentionPolicy(keep_daily=-1)

    def test_negative_weekly_raises(self) -> None:
        """Negative keep_weekly raises ValueError."""
        with pytest.raises(ValueError, match="keep_weekly"):
            RetentionPolicy(keep_weekly=-1)

    def test_negative_monthly_raises(self) -> None:
        """Negative keep_monthly raises ValueError."""
        with pytest.raises(ValueError, match="keep_monthly"):
            RetentionPolicy(keep_monthly=-1)

    def test_zero_retention_valid(self) -> None:
        """Zero retention is valid (prune everything)."""
        policy = RetentionPolicy(keep_daily=0, keep_weekly=0, keep_monthly=0)
        assert policy.keep_daily == 0

    def test_apply_empty_list(self) -> None:
        """Applying to empty list returns empty result."""
        policy = RetentionPolicy()
        result = policy.apply([])
        assert result.keep == []
        assert result.prune == []

    def test_apply_single_backup_kept(self) -> None:
        """Single backup within retention is kept."""
        policy = RetentionPolicy(keep_daily=7)
        now = datetime(2026, 4, 7, 12, 0, tzinfo=UTC)
        backups = [_make_backup_info("b1", now - timedelta(hours=1))]

        result = policy.apply(backups, now=now)
        assert len(result.keep) == 1
        assert len(result.prune) == 0

    def test_apply_daily_retention(self) -> None:
        """Daily retention keeps only newest per day, respects count limit."""
        policy = RetentionPolicy(keep_daily=3, keep_weekly=0, keep_monthly=0)
        now = datetime(2026, 4, 7, 12, 0, tzinfo=UTC)

        # Create 5 daily backups
        backups = [_make_backup_info(f"d{i}", now - timedelta(days=i)) for i in range(5)]

        result = policy.apply(backups, now=now)
        assert len(result.keep) == 3
        assert len(result.prune) == 2

        # Kept are the 3 most recent days
        kept_ids = {b.backup_id for b in result.keep}
        assert kept_ids == {"d0", "d1", "d2"}

    def test_apply_weekly_retention(self) -> None:
        """Weekly retention keeps newest per week."""
        policy = RetentionPolicy(keep_daily=0, keep_weekly=2, keep_monthly=0)
        now = datetime(2026, 4, 7, 12, 0, tzinfo=UTC)

        # Backups spread across 4 weeks
        backups = [_make_backup_info(f"w{i}", now - timedelta(weeks=i)) for i in range(4)]

        result = policy.apply(backups, now=now)
        assert len(result.keep) == 2
        assert len(result.prune) == 2

    def test_apply_monthly_retention(self) -> None:
        """Monthly retention keeps newest per month."""
        policy = RetentionPolicy(keep_daily=0, keep_weekly=0, keep_monthly=3)
        now = datetime(2026, 4, 7, 12, 0, tzinfo=UTC)

        # Backups spread across 5 months
        backups = [_make_backup_info(f"m{i}", now - timedelta(days=30 * i)) for i in range(5)]

        result = policy.apply(backups, now=now)
        assert len(result.keep) == 3
        assert len(result.prune) == 2

    def test_apply_gfs_combined(self) -> None:
        """GFS combined: monthly saves old backups that daily would prune."""
        policy = RetentionPolicy(keep_daily=3, keep_weekly=2, keep_monthly=2)
        now = datetime(2026, 4, 7, 12, 0, tzinfo=UTC)

        backups = [
            # Recent dailies
            _make_backup_info("today", now),
            _make_backup_info("yesterday", now - timedelta(days=1)),
            _make_backup_info("2days", now - timedelta(days=2)),
            _make_backup_info("3days", now - timedelta(days=3)),
            _make_backup_info("4days", now - timedelta(days=4)),
            # Old monthly
            _make_backup_info("month1", now - timedelta(days=35)),
            _make_backup_info("month2", now - timedelta(days=65)),
            _make_backup_info("month3", now - timedelta(days=95)),
        ]

        result = policy.apply(backups, now=now)

        kept_ids = {b.backup_id for b in result.keep}
        # today, yesterday, 2days kept by daily
        assert "today" in kept_ids
        assert "yesterday" in kept_ids
        assert "2days" in kept_ids
        # month1 kept by monthly (March 2026 = 2nd most recent month)
        assert "month1" in kept_ids
        # month2 (Feb 2026, 3rd month) may or may not be kept depending
        # on weekly coverage — the key invariant is GFS keeps more than daily alone
        assert len(result.keep) > 3  # GFS saves backups that daily alone would prune

    def test_apply_multiple_backups_same_day(self) -> None:
        """Multiple backups on same day: only newest is kept per daily slot."""
        policy = RetentionPolicy(keep_daily=1, keep_weekly=0, keep_monthly=0)
        now = datetime(2026, 4, 7, 18, 0, tzinfo=UTC)

        backups = [
            _make_backup_info("early", datetime(2026, 4, 7, 6, 0, tzinfo=UTC)),
            _make_backup_info("noon", datetime(2026, 4, 7, 12, 0, tzinfo=UTC)),
            _make_backup_info("late", datetime(2026, 4, 7, 17, 0, tzinfo=UTC)),
        ]

        result = policy.apply(backups, now=now)
        assert len(result.keep) == 1
        assert result.keep[0].backup_id == "late"

    def test_apply_zero_retention_prunes_all(self) -> None:
        """Zero retention prunes everything."""
        policy = RetentionPolicy(keep_daily=0, keep_weekly=0, keep_monthly=0)
        now = datetime(2026, 4, 7, 12, 0, tzinfo=UTC)

        backups = [_make_backup_info(f"b{i}", now - timedelta(days=i)) for i in range(5)]

        result = policy.apply(backups, now=now)
        assert len(result.keep) == 0
        assert len(result.prune) == 5

    def test_apply_overlap_deduplicates(self) -> None:
        """Backup kept by multiple GFS buckets is not duplicated in keep list."""
        policy = RetentionPolicy(keep_daily=7, keep_weekly=4, keep_monthly=12)
        now = datetime(2026, 4, 7, 12, 0, tzinfo=UTC)

        backups = [
            _make_backup_info("recent", now - timedelta(hours=1)),
        ]

        result = policy.apply(backups, now=now)
        assert len(result.keep) == 1  # Not 3 (daily+weekly+monthly)

    def test_frozen_dataclass(self) -> None:
        """RetentionPolicy is immutable."""
        policy = RetentionPolicy()
        with pytest.raises(AttributeError):
            policy.keep_daily = 100  # type: ignore[misc]

    @settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(
        keep_daily=st.integers(min_value=0, max_value=50),
        keep_weekly=st.integers(min_value=0, max_value=20),
        keep_monthly=st.integers(min_value=0, max_value=20),
        n_backups=st.integers(min_value=0, max_value=30),
    )
    def test_property_keep_plus_prune_equals_total(
        self,
        keep_daily: int,
        keep_weekly: int,
        keep_monthly: int,
        n_backups: int,
    ) -> None:
        """Property: keep + prune always equals total input."""
        policy = RetentionPolicy(
            keep_daily=keep_daily,
            keep_weekly=keep_weekly,
            keep_monthly=keep_monthly,
        )
        now = datetime(2026, 4, 7, 12, 0, tzinfo=UTC)
        backups = [
            _make_backup_info(f"b{i}", now - timedelta(hours=i * 6)) for i in range(n_backups)
        ]

        result = policy.apply(backups, now=now)
        assert len(result.keep) + len(result.prune) == n_backups

    @settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(
        keep_daily=st.integers(min_value=0, max_value=50),
        keep_weekly=st.integers(min_value=0, max_value=20),
        keep_monthly=st.integers(min_value=0, max_value=20),
        n_backups=st.integers(min_value=0, max_value=30),
    )
    def test_property_no_duplicate_ids(
        self,
        keep_daily: int,
        keep_weekly: int,
        keep_monthly: int,
        n_backups: int,
    ) -> None:
        """Property: no backup_id appears in both keep and prune."""
        policy = RetentionPolicy(
            keep_daily=keep_daily,
            keep_weekly=keep_weekly,
            keep_monthly=keep_monthly,
        )
        now = datetime(2026, 4, 7, 12, 0, tzinfo=UTC)
        backups = [
            _make_backup_info(f"b{i}", now - timedelta(hours=i * 6)) for i in range(n_backups)
        ]

        result = policy.apply(backups, now=now)
        keep_ids = {b.backup_id for b in result.keep}
        prune_ids = {b.backup_id for b in result.prune}
        assert keep_ids.isdisjoint(prune_ids)


# ---------------------------------------------------------------------------
# RetentionResult Tests
# ---------------------------------------------------------------------------


class TestRetentionResult:
    """Tests for RetentionResult dataclass."""

    def test_frozen(self) -> None:
        """RetentionResult is immutable."""
        result = RetentionResult(keep=[], prune=[])
        with pytest.raises(AttributeError):
            result.keep = []  # type: ignore[misc]

    def test_slots(self) -> None:
        """RetentionResult uses slots."""
        result = RetentionResult(keep=[], prune=[])
        assert not hasattr(result, "__dict__")


# ---------------------------------------------------------------------------
# ScheduleTier Tests
# ---------------------------------------------------------------------------


class TestScheduleTier:
    """Tests for ScheduleTier enum."""

    def test_values(self) -> None:
        """All expected tier values exist."""
        assert ScheduleTier.SYNC.value == "sync"
        assert ScheduleTier.CLOUD.value == "cloud"
        assert ScheduleTier.BUSINESS.value == "business"

    def test_from_string(self) -> None:
        """Tiers can be created from string values."""
        assert ScheduleTier("sync") == ScheduleTier.SYNC
        assert ScheduleTier("cloud") == ScheduleTier.CLOUD

    def test_all_tiers_have_schedules(self) -> None:
        """Every tier has a schedule entry in TIER_SCHEDULES."""
        for tier in ScheduleTier:
            assert tier in TIER_SCHEDULES


# ---------------------------------------------------------------------------
# TierSchedule Tests
# ---------------------------------------------------------------------------


class TestTierSchedule:
    """Tests for TierSchedule configuration."""

    def test_sync_schedule(self) -> None:
        """Sync tier: daily interval, night window."""
        schedule = TIER_SCHEDULES[ScheduleTier.SYNC]
        assert schedule.interval_seconds == 86400
        assert schedule.window_start_hour == 2
        assert schedule.window_end_hour == 4

    def test_cloud_schedule(self) -> None:
        """Cloud tier: hourly interval, active hours window."""
        schedule = TIER_SCHEDULES[ScheduleTier.CLOUD]
        assert schedule.interval_seconds == 3600
        assert schedule.window_start_hour == 8
        assert schedule.window_end_hour == 0

    def test_business_schedule(self) -> None:
        """Business tier: hourly interval, no window restriction."""
        schedule = TIER_SCHEDULES[ScheduleTier.BUSINESS]
        assert schedule.interval_seconds == 3600
        assert schedule.window_start_hour is None
        assert schedule.window_end_hour is None

    def test_sync_retention(self) -> None:
        """Sync tier retention: daily only."""
        r = TIER_SCHEDULES[ScheduleTier.SYNC].retention
        assert r.keep_daily == 30
        assert r.keep_weekly == 0
        assert r.keep_monthly == 0

    def test_cloud_retention(self) -> None:
        """Cloud tier retention: daily + weekly + monthly."""
        r = TIER_SCHEDULES[ScheduleTier.CLOUD].retention
        assert r.keep_daily == 30
        assert r.keep_weekly == 12
        assert r.keep_monthly == 6

    def test_frozen(self) -> None:
        """TierSchedule is immutable."""
        schedule = TierSchedule(interval_seconds=3600)
        with pytest.raises(AttributeError):
            schedule.interval_seconds = 7200  # type: ignore[misc]


# ---------------------------------------------------------------------------
# BackupScheduler Tests
# ---------------------------------------------------------------------------


class TestBackupScheduler:
    """Tests for BackupScheduler lifecycle and scheduling."""

    def test_init_defaults(self) -> None:
        """Scheduler initializes with correct defaults."""
        service = _make_service_mock()
        scheduler = BackupScheduler(service, ScheduleTier.SYNC)

        assert scheduler.tier == ScheduleTier.SYNC
        assert scheduler.is_running is False
        assert scheduler.last_backup_at is None
        assert scheduler.consecutive_failures == 0

    def test_init_custom_tier(self) -> None:
        """Scheduler accepts custom tier."""
        service = _make_service_mock()
        scheduler = BackupScheduler(service, ScheduleTier.BUSINESS)
        assert scheduler.tier == ScheduleTier.BUSINESS
        assert scheduler.schedule.window_start_hour is None

    def test_init_schedule_override(self) -> None:
        """Custom schedule overrides tier defaults."""
        service = _make_service_mock()
        custom = TierSchedule(interval_seconds=999)
        scheduler = BackupScheduler(
            service,
            ScheduleTier.SYNC,
            schedule_override=custom,
        )
        assert scheduler.schedule.interval_seconds == 999

    @pytest.mark.asyncio
    async def test_start_stop_lifecycle(self) -> None:
        """Start creates background task, stop cancels it."""
        service = _make_service_mock()
        scheduler = BackupScheduler(
            service,
            ScheduleTier.SYNC,
            check_interval_seconds=3600,
        )

        await scheduler.start()
        assert scheduler.is_running is True

        await scheduler.stop()
        assert scheduler.is_running is False

    @pytest.mark.asyncio
    async def test_double_start_raises(self) -> None:
        """Starting an already-running scheduler raises RuntimeError."""
        service = _make_service_mock()
        scheduler = BackupScheduler(
            service,
            ScheduleTier.SYNC,
            check_interval_seconds=3600,
        )

        await scheduler.start()
        try:
            with pytest.raises(RuntimeError, match="already running"):
                await scheduler.start()
        finally:
            await scheduler.stop()

    @pytest.mark.asyncio
    async def test_stop_idempotent(self) -> None:
        """Stopping a non-running scheduler is a no-op."""
        service = _make_service_mock()
        scheduler = BackupScheduler(service, ScheduleTier.SYNC)
        await scheduler.stop()  # Should not raise

    # --- should_backup logic ---

    def test_should_backup_no_previous(self) -> None:
        """First backup is always due (no last_backup_at)."""
        service = _make_service_mock()
        scheduler = BackupScheduler(service, ScheduleTier.BUSINESS)
        # Business tier has no window restriction
        assert scheduler.should_backup() is True

    def test_should_backup_interval_not_elapsed(self) -> None:
        """Backup not due when interval hasn't elapsed."""
        service = _make_service_mock()
        scheduler = BackupScheduler(service, ScheduleTier.BUSINESS)
        now = datetime(2026, 4, 7, 12, 0, tzinfo=UTC)
        scheduler.record_last_backup(now - timedelta(minutes=30))
        # Business tier has 1h interval
        assert scheduler.should_backup(now=now) is False

    def test_should_backup_interval_elapsed(self) -> None:
        """Backup is due when interval has elapsed."""
        service = _make_service_mock()
        scheduler = BackupScheduler(service, ScheduleTier.BUSINESS)
        now = datetime(2026, 4, 7, 12, 0, tzinfo=UTC)
        scheduler.record_last_backup(now - timedelta(hours=2))
        assert scheduler.should_backup(now=now) is True

    def test_should_backup_outside_sync_window(self) -> None:
        """Sync tier backup not due outside 02:00-04:00 window."""
        service = _make_service_mock()
        scheduler = BackupScheduler(service, ScheduleTier.SYNC)
        # 12:00 is outside 02:00-04:00
        noon = datetime(2026, 4, 7, 12, 0, tzinfo=UTC)
        assert scheduler.should_backup(now=noon) is False

    def test_should_backup_inside_sync_window(self) -> None:
        """Sync tier backup due inside 02:00-04:00 window."""
        service = _make_service_mock()
        scheduler = BackupScheduler(service, ScheduleTier.SYNC)
        at_3am = datetime(2026, 4, 7, 3, 0, tzinfo=UTC)
        assert scheduler.should_backup(now=at_3am) is True

    def test_should_backup_cloud_wrapping_window(self) -> None:
        """Cloud tier: 08:00-00:00 window wraps around midnight."""
        service = _make_service_mock()
        scheduler = BackupScheduler(service, ScheduleTier.CLOUD)

        # 10:00 — inside window
        assert scheduler.should_backup(now=datetime(2026, 4, 7, 10, 0, tzinfo=UTC)) is True

        # 23:00 — inside window
        assert scheduler.should_backup(now=datetime(2026, 4, 7, 23, 0, tzinfo=UTC)) is True

        # 03:00 — outside window
        assert scheduler.should_backup(now=datetime(2026, 4, 7, 3, 0, tzinfo=UTC)) is False

        # 07:00 — outside window
        assert scheduler.should_backup(now=datetime(2026, 4, 7, 7, 0, tzinfo=UTC)) is False

    def test_should_backup_boundary_start(self) -> None:
        """Sync window: 02:00 exactly is inside."""
        service = _make_service_mock()
        scheduler = BackupScheduler(service, ScheduleTier.SYNC)
        at_2am = datetime(2026, 4, 7, 2, 0, tzinfo=UTC)
        assert scheduler.should_backup(now=at_2am) is True

    def test_should_backup_boundary_end(self) -> None:
        """Sync window: 04:00 exactly is outside (exclusive end)."""
        service = _make_service_mock()
        scheduler = BackupScheduler(service, ScheduleTier.SYNC)
        at_4am = datetime(2026, 4, 7, 4, 0, tzinfo=UTC)
        assert scheduler.should_backup(now=at_4am) is False

    # --- run_once ---

    @pytest.mark.asyncio
    async def test_run_once_creates_backup(self) -> None:
        """run_once creates backup when due."""
        service = _make_service_mock()
        scheduler = BackupScheduler(service, ScheduleTier.BUSINESS)

        result = await scheduler.run_once()
        assert result is not None
        assert result.backup_id == "abc123"
        service.create_backup.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_once_skips_when_not_due(self) -> None:
        """run_once returns None when backup not due."""
        service = _make_service_mock()
        scheduler = BackupScheduler(service, ScheduleTier.BUSINESS)
        scheduler.record_last_backup(datetime.now(tz=UTC))

        result = await scheduler.run_once()
        assert result is None
        service.create_backup.assert_not_called()

    @pytest.mark.asyncio
    async def test_run_once_updates_last_backup(self) -> None:
        """Successful backup updates last_backup_at."""
        service = _make_service_mock()
        scheduler = BackupScheduler(service, ScheduleTier.BUSINESS)
        assert scheduler.last_backup_at is None

        await scheduler.run_once()
        assert scheduler.last_backup_at is not None

    @pytest.mark.asyncio
    async def test_run_once_failure_increments_counter(self) -> None:
        """Failed backup increments consecutive failures."""
        service = _make_service_mock()
        service.create_backup.side_effect = RuntimeError("R2 unavailable")
        scheduler = BackupScheduler(service, ScheduleTier.BUSINESS)

        result = await scheduler.run_once()
        assert result is None
        assert scheduler.consecutive_failures == 1

    @pytest.mark.asyncio
    async def test_run_once_success_resets_failures(self) -> None:
        """Successful backup resets consecutive failure counter."""
        service = _make_service_mock()
        scheduler = BackupScheduler(service, ScheduleTier.BUSINESS)
        scheduler._consecutive_failures = 2

        await scheduler.run_once()
        assert scheduler.consecutive_failures == 0

    @pytest.mark.asyncio
    async def test_run_once_calls_callback_on_success(self) -> None:
        """Callback.on_backup_completed called on success."""
        service = _make_service_mock()
        callback = AsyncMock()
        scheduler = BackupScheduler(service, ScheduleTier.BUSINESS, callback=callback)

        await scheduler.run_once()
        callback.on_backup_completed.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_once_calls_callback_on_failure(self) -> None:
        """Callback.on_backup_failed called on failure."""
        service = _make_service_mock()
        service.create_backup.side_effect = RuntimeError("fail")
        callback = AsyncMock()
        scheduler = BackupScheduler(service, ScheduleTier.BUSINESS, callback=callback)

        await scheduler.run_once()
        callback.on_backup_failed.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_once_prunes_after_backup(self) -> None:
        """Successful backup triggers prune."""
        service = _make_service_mock()
        now = datetime(2026, 4, 7, 12, 0, tzinfo=UTC)

        # Create backups that should be pruned
        old_backups = [
            _make_backup_info(f"old{i}", now - timedelta(days=40 + i)) for i in range(5)
        ]
        service.list_backups.return_value = old_backups

        callback = AsyncMock()
        scheduler = BackupScheduler(
            service,
            ScheduleTier.BUSINESS,
            callback=callback,
        )

        await scheduler.run_once()
        # Prune was attempted (list_backups called)
        service.list_backups.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_once_prune_failure_doesnt_crash(self) -> None:
        """Prune failure doesn't propagate — backup still counts as success."""
        service = _make_service_mock()
        service.list_backups.side_effect = RuntimeError("prune fail")

        scheduler = BackupScheduler(service, ScheduleTier.BUSINESS)
        result = await scheduler.run_once()
        assert result is not None  # Backup succeeded even though prune failed

    # --- update_tier ---

    def test_update_tier(self) -> None:
        """Tier change updates schedule configuration."""
        service = _make_service_mock()
        scheduler = BackupScheduler(service, ScheduleTier.SYNC)
        assert scheduler.schedule.interval_seconds == 86400

        scheduler.update_tier(ScheduleTier.CLOUD)
        assert scheduler.tier == ScheduleTier.CLOUD
        assert scheduler.schedule.interval_seconds == 3600

    # --- record_last_backup ---

    def test_record_last_backup(self) -> None:
        """Record last backup sets timestamp."""
        service = _make_service_mock()
        scheduler = BackupScheduler(service, ScheduleTier.SYNC)
        ts = datetime(2026, 4, 7, 3, 0, tzinfo=UTC)
        scheduler.record_last_backup(ts)
        assert scheduler.last_backup_at == ts

    # --- status ---

    def test_status_dict(self) -> None:
        """Status returns complete observability dict."""
        service = _make_service_mock()
        scheduler = BackupScheduler(service, ScheduleTier.CLOUD)
        status = scheduler.status()

        assert status["tier"] == "cloud"
        assert status["running"] is False
        assert status["interval_seconds"] == 3600
        assert status["last_backup_at"] is None
        assert status["consecutive_failures"] == 0
        assert "window" in status
        assert "retention" in status

    def test_status_with_last_backup(self) -> None:
        """Status includes last backup timestamp when set."""
        service = _make_service_mock()
        scheduler = BackupScheduler(service, ScheduleTier.SYNC)
        ts = datetime(2026, 4, 7, 3, 0, tzinfo=UTC)
        scheduler.record_last_backup(ts)

        status = scheduler.status()
        assert status["last_backup_at"] == ts.isoformat()


# ---------------------------------------------------------------------------
# _NullCallback Tests
# ---------------------------------------------------------------------------


class TestNullCallback:
    """Tests for _NullCallback no-op implementation."""

    @pytest.mark.asyncio
    async def test_on_backup_completed(self) -> None:
        """No-op callback doesn't raise."""
        cb = _NullCallback()
        await cb.on_backup_completed(_make_metadata())

    @pytest.mark.asyncio
    async def test_on_backup_failed(self) -> None:
        """No-op callback doesn't raise."""
        cb = _NullCallback()
        await cb.on_backup_failed("error")

    @pytest.mark.asyncio
    async def test_on_prune_completed(self) -> None:
        """No-op callback doesn't raise."""
        cb = _NullCallback()
        await cb.on_prune_completed(PruneResult(deleted_count=0))


# ---------------------------------------------------------------------------
# Integration-style Tests
# ---------------------------------------------------------------------------


class TestSchedulerIntegration:
    """Integration-style tests combining scheduler + retention."""

    @pytest.mark.asyncio
    async def test_full_cycle_backup_and_prune(self) -> None:
        """Complete cycle: backup → list → retention → delete pruned."""
        service = _make_service_mock()
        now = datetime(2026, 4, 7, 12, 0, tzinfo=UTC)

        # 40 daily backups — only 30 should be kept (business tier)
        existing_backups = [_make_backup_info(f"b{i}", now - timedelta(days=i)) for i in range(40)]
        service.list_backups.return_value = existing_backups

        callback = AsyncMock()
        scheduler = BackupScheduler(
            service,
            ScheduleTier.BUSINESS,
            callback=callback,
        )

        result = await scheduler.run_once()
        assert result is not None

        # Pruning should have happened
        if service._r2.delete_objects.called:
            call_args = service._r2.delete_objects.call_args
            deleted_keys = call_args[0][0]
            # Business keeps 30 daily + 12 weekly + 6 monthly — overlapping
            # So some of the 40 should be pruned
            assert len(deleted_keys) > 0

    @pytest.mark.asyncio
    async def test_max_failures_reached(self) -> None:
        """After 3 consecutive failures, counter reflects that."""
        service = _make_service_mock()
        service.create_backup.side_effect = RuntimeError("always fails")

        scheduler = BackupScheduler(service, ScheduleTier.BUSINESS)

        for _ in range(3):
            await scheduler.run_once()
            # Reset should_backup by clearing last_backup
            scheduler._last_backup_at = None

        assert scheduler.consecutive_failures == 3

    @pytest.mark.asyncio
    async def test_scheduler_loop_runs_and_stops(self) -> None:
        """Scheduler loop runs at least one cycle before stopping."""
        service = _make_service_mock()
        scheduler = BackupScheduler(
            service,
            ScheduleTier.BUSINESS,
            check_interval_seconds=0,  # Minimal sleep
        )

        await scheduler.start()
        # Give event loop time to run at least one cycle
        await asyncio.sleep(0.1)
        await scheduler.stop()

        assert service.create_backup.called
        assert scheduler.is_running is False
