"""Backup scheduler — asyncio-based scheduling with GFS retention policy.

Manages automatic backup scheduling per license tier and applies
Grandfather-Father-Son (GFS) retention to prune expired backups.

Schedule tiers::

    sync:     daily backups (02:00-04:00 local window)
    cloud:    hourly backups (08:00-00:00 active hours)
    business: hourly backups (24/7, no window restriction)

GFS retention keeps daily(7) + weekly(4) + monthly(12) backups by default,
configurable via ``RetentionPolicy``.

References:
    - SPE-033 §2.6: BackupScheduler
    - SPE-033 §2.4: GFS retention policy
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from sovyx.cloud.backup import BackupInfo, BackupMetadata, BackupService, PruneResult

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Retention Policy — GFS (Grandfather-Father-Son)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """GFS (Grandfather-Father-Son) retention policy.

    Classifies backups into daily, weekly, and monthly buckets and prunes
    those exceeding the configured retention counts.

    Attributes:
        keep_daily: Number of daily backups to retain (one per day).
        keep_weekly: Number of weekly backups to retain (one per week).
        keep_monthly: Number of monthly backups to retain (one per month).
    """

    keep_daily: int = 7
    keep_weekly: int = 4
    keep_monthly: int = 12

    def __post_init__(self) -> None:
        """Validate retention counts are non-negative."""
        if self.keep_daily < 0:
            msg = f"keep_daily must be >= 0, got {self.keep_daily}"
            raise ValueError(msg)
        if self.keep_weekly < 0:
            msg = f"keep_weekly must be >= 0, got {self.keep_weekly}"
            raise ValueError(msg)
        if self.keep_monthly < 0:
            msg = f"keep_monthly must be >= 0, got {self.keep_monthly}"
            raise ValueError(msg)

    def apply(
        self,
        backups: list[BackupInfo],
        *,
        now: datetime | None = None,
    ) -> RetentionResult:
        """Classify backups and determine which to keep or prune.

        The algorithm assigns each backup to the most significant GFS bucket
        it qualifies for (monthly > weekly > daily). Within each bucket, the
        most recent backup per period is kept. Excess backups are marked for
        deletion.

        Args:
            backups: List of backups to evaluate, in any order.
            now: Reference time for bucket calculations. Defaults to UTC now.

        Returns:
            RetentionResult with ``keep`` and ``prune`` lists.
        """
        if not backups:
            return RetentionResult(keep=[], prune=[])

        # Sort newest first for consistent selection
        sorted_backups = sorted(backups, key=lambda b: b.created_at, reverse=True)

        # Assign to GFS buckets: monthly → weekly → daily
        monthly_buckets: dict[str, BackupInfo] = {}
        weekly_buckets: dict[str, BackupInfo] = {}
        daily_buckets: dict[str, BackupInfo] = {}
        keep_set: set[str] = set()

        for backup in sorted_backups:
            month_key = backup.created_at.strftime("%Y-%m")
            week_key = backup.created_at.strftime("%Y-W%W")
            day_key = backup.created_at.strftime("%Y-%m-%d")

            # Monthly bucket — keep first (newest) per month
            if month_key not in monthly_buckets:
                monthly_buckets[month_key] = backup

            # Weekly bucket — keep first (newest) per week
            if week_key not in weekly_buckets:
                weekly_buckets[week_key] = backup

            # Daily bucket — keep first (newest) per day
            if day_key not in daily_buckets:
                daily_buckets[day_key] = backup

        # Select keepers respecting retention limits (most recent periods first)
        monthly_keys = sorted(monthly_buckets.keys(), reverse=True)[: self.keep_monthly]
        for mk in monthly_keys:
            keep_set.add(monthly_buckets[mk].backup_id)

        weekly_keys = sorted(weekly_buckets.keys(), reverse=True)[: self.keep_weekly]
        for wk in weekly_keys:
            keep_set.add(weekly_buckets[wk].backup_id)

        daily_keys = sorted(daily_buckets.keys(), reverse=True)[: self.keep_daily]
        for dk in daily_keys:
            keep_set.add(daily_buckets[dk].backup_id)

        # Partition into keep and prune
        keep: list[BackupInfo] = []
        prune: list[BackupInfo] = []

        for backup in sorted_backups:
            if backup.backup_id in keep_set:
                keep.append(backup)
            else:
                prune.append(backup)

        logger.debug(
            "retention_applied",
            total=len(sorted_backups),
            keep=len(keep),
            prune=len(prune),
            daily_buckets=len(daily_keys),
            weekly_buckets=len(weekly_keys),
            monthly_buckets=len(monthly_keys),
        )

        return RetentionResult(keep=keep, prune=prune)


@dataclass(frozen=True, slots=True)
class RetentionResult:
    """Result of applying a retention policy.

    Attributes:
        keep: Backups to retain.
        prune: Backups to delete.
    """

    keep: list[BackupInfo]
    prune: list[BackupInfo]


# ---------------------------------------------------------------------------
# Tier Configuration
# ---------------------------------------------------------------------------


class ScheduleTier(StrEnum):
    """Backup schedule tiers corresponding to license levels."""

    SYNC = "sync"
    CLOUD = "cloud"
    BUSINESS = "business"


@dataclass(frozen=True, slots=True)
class TierSchedule:
    """Schedule configuration for a backup tier.

    Attributes:
        interval_seconds: Seconds between backup attempts.
        window_start_hour: Start hour of the backup window (0-23), or None for 24/7.
        window_end_hour: End hour of the backup window (0-23), or None for 24/7.
        retention: GFS retention policy for this tier.
    """

    interval_seconds: int
    window_start_hour: int | None = None
    window_end_hour: int | None = None
    retention: RetentionPolicy = field(default_factory=RetentionPolicy)


# SPE-033 §2.6 — Tier schedules
TIER_SCHEDULES: dict[ScheduleTier, TierSchedule] = {
    ScheduleTier.SYNC: TierSchedule(
        interval_seconds=86400,  # 24h
        window_start_hour=2,
        window_end_hour=4,
        retention=RetentionPolicy(keep_daily=30, keep_weekly=0, keep_monthly=0),
    ),
    ScheduleTier.CLOUD: TierSchedule(
        interval_seconds=3600,  # 1h
        window_start_hour=8,
        window_end_hour=0,  # midnight (wraps)
        retention=RetentionPolicy(keep_daily=30, keep_weekly=12, keep_monthly=6),
    ),
    ScheduleTier.BUSINESS: TierSchedule(
        interval_seconds=3600,  # 1h
        window_start_hour=None,
        window_end_hour=None,
        retention=RetentionPolicy(keep_daily=30, keep_weekly=12, keep_monthly=6),
    ),
}


# ---------------------------------------------------------------------------
# Scheduler Callback Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class SchedulerCallback(Protocol):
    """Protocol for scheduler event callbacks."""

    async def on_backup_completed(self, metadata: BackupMetadata) -> None:
        """Called after a successful backup."""
        ...

    async def on_backup_failed(self, error: str) -> None:
        """Called after a failed backup attempt."""
        ...

    async def on_prune_completed(self, result: PruneResult) -> None:
        """Called after pruning completes."""
        ...


class _NullCallback:
    """No-op callback for when none is provided."""

    async def on_backup_completed(self, metadata: BackupMetadata) -> None:
        """No-op."""

    async def on_backup_failed(self, error: str) -> None:
        """No-op."""

    async def on_prune_completed(self, result: PruneResult) -> None:
        """No-op."""


# ---------------------------------------------------------------------------
# BackupScheduler
# ---------------------------------------------------------------------------

# Check interval for the scheduler main loop (5 minutes)
_CHECK_INTERVAL_SECONDS = 300


class BackupScheduler:
    """Asyncio-based backup scheduler with GFS retention.

    Integrates with the engine lifecycle via ``start()`` / ``stop()``.
    Runs a main loop that checks if a backup is due based on the tier
    schedule and backup window, creates backups, and prunes expired ones.

    Usage::

        scheduler = BackupScheduler(
            backup_service=service,
            tier=ScheduleTier.CLOUD,
        )
        await scheduler.start()
        # ... runs until stop
        await scheduler.stop()

    References:
        - SPE-033 §2.6: Scheduling specification
        - SPE-033 §2.4: GFS retention policy
    """

    def __init__(
        self,
        backup_service: BackupService,
        tier: ScheduleTier = ScheduleTier.SYNC,
        *,
        callback: SchedulerCallback | None = None,
        schedule_override: TierSchedule | None = None,
        check_interval_seconds: int = _CHECK_INTERVAL_SECONDS,
    ) -> None:
        self._service = backup_service
        self._tier = tier
        self._schedule = schedule_override or TIER_SCHEDULES[tier]
        self._callback: SchedulerCallback = callback or _NullCallback()
        self._check_interval = check_interval_seconds

        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._last_backup_at: datetime | None = None
        self._consecutive_failures = 0
        self._max_consecutive_failures = 3

    @property
    def tier(self) -> ScheduleTier:
        """Current schedule tier."""
        return self._tier

    @property
    def schedule(self) -> TierSchedule:
        """Active schedule configuration."""
        return self._schedule

    @property
    def is_running(self) -> bool:
        """Whether the scheduler loop is active."""
        return self._running

    @property
    def last_backup_at(self) -> datetime | None:
        """Timestamp of the last successful backup."""
        return self._last_backup_at

    @property
    def consecutive_failures(self) -> int:
        """Count of consecutive backup failures."""
        return self._consecutive_failures

    async def start(self) -> None:
        """Start the scheduler loop as a background task.

        Raises:
            RuntimeError: If the scheduler is already running.
        """
        if self._running:
            msg = "BackupScheduler is already running"
            raise RuntimeError(msg)

        self._running = True
        self._task = asyncio.create_task(self._run_loop(), name="backup-scheduler")
        logger.info(
            "scheduler_started",
            tier=self._tier.value,
            interval_seconds=self._schedule.interval_seconds,
        )

    async def stop(self) -> None:
        """Stop the scheduler loop gracefully.

        Cancels the background task and waits for it to complete.
        """
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

        logger.info("scheduler_stopped", tier=self._tier.value)

    def should_backup(self, *, now: datetime | None = None) -> bool:
        """Check if a backup is due based on schedule and window.

        Args:
            now: Current time for evaluation. Defaults to UTC now.

        Returns:
            ``True`` if a backup should be created now.
        """
        current = now or datetime.now(tz=UTC)

        # Check backup window
        if not self._in_window(current):
            return False

        # Check interval since last backup
        if self._last_backup_at is None:
            return True

        elapsed = (current - self._last_backup_at).total_seconds()
        return elapsed >= self._schedule.interval_seconds

    def _in_window(self, now: datetime) -> bool:
        """Check if current time is within the backup window.

        Args:
            now: Current time to check.

        Returns:
            ``True`` if within window or no window restriction.
        """
        start = self._schedule.window_start_hour
        end = self._schedule.window_end_hour

        if start is None or end is None:
            return True  # No window restriction (business tier)

        hour = now.hour

        if start < end:
            # Normal range: e.g., 02:00-04:00
            return start <= hour < end
        if start > end:
            # Wrapping range: e.g., 08:00-00:00 (8 AM to midnight)
            return hour >= start or hour < end
        # start == end: single-hour window
        return hour == start

    async def run_once(self) -> BackupMetadata | None:
        """Execute a single backup + prune cycle.

        Returns:
            BackupMetadata if backup was created, None if skipped or failed.
        """
        if not self.should_backup():
            logger.debug("scheduler_skip", reason="not_due")
            return None

        return await self._execute_cycle()

    async def _execute_cycle(self) -> BackupMetadata | None:
        """Create a backup and prune old ones.

        Returns:
            BackupMetadata on success, None on failure.
        """
        try:
            metadata = self._service.create_backup()
            self._last_backup_at = metadata.created_at
            self._consecutive_failures = 0

            logger.info(
                "scheduler_backup_created",
                backup_id=metadata.backup_id,
                size_bytes=metadata.size_bytes,
            )

            await self._callback.on_backup_completed(metadata)

            # Prune after successful backup
            await self._prune()

            return metadata

        except Exception as exc:  # noqa: BLE001
            self._consecutive_failures += 1
            error_msg = f"{type(exc).__name__}: {exc}"

            logger.warning(
                "scheduler_backup_failed",
                error=error_msg,
                consecutive_failures=self._consecutive_failures,
            )

            await self._callback.on_backup_failed(error_msg)

            if self._consecutive_failures >= self._max_consecutive_failures:
                logger.error(
                    "scheduler_max_failures_reached",
                    count=self._consecutive_failures,
                    max=self._max_consecutive_failures,
                )

            return None

    async def _prune(self) -> None:
        """Apply GFS retention and delete expired backups."""
        try:
            backups = self._service.list_backups()
            if not backups:
                return

            retention = self._schedule.retention
            result = retention.apply(backups)

            if not result.prune:
                logger.debug("scheduler_prune_skip", reason="nothing_to_prune")
                return

            # Delete pruned backups via R2
            keys_to_delete = [b.r2_key for b in result.prune]
            deleted_count = self._service.r2.delete_objects(
                keys_to_delete,
                self._service.backup_config.r2_bucket,
            )

            from sovyx.cloud.backup import PruneResult

            prune_result = PruneResult(
                deleted_count=deleted_count,
                deleted_keys=keys_to_delete,
                remaining_count=len(result.keep),
            )

            logger.info(
                "scheduler_pruned",
                deleted=deleted_count,
                remaining=len(result.keep),
            )

            await self._callback.on_prune_completed(prune_result)

        except Exception as exc:  # noqa: BLE001
            logger.warning("scheduler_prune_failed", error=str(exc))

    async def _run_loop(self) -> None:
        """Main scheduler loop.

        Checks if a backup is due every ``check_interval`` seconds,
        executes the backup+prune cycle when needed, and sleeps.
        """
        logger.debug("scheduler_loop_started")

        try:
            while self._running:
                await self.run_once()
                await asyncio.sleep(self._check_interval)
        except asyncio.CancelledError:
            logger.debug("scheduler_loop_cancelled")
            raise

    def update_tier(self, tier: ScheduleTier) -> None:
        """Change the schedule tier at runtime.

        Args:
            tier: New schedule tier to apply.
        """
        self._tier = tier
        self._schedule = TIER_SCHEDULES[tier]
        logger.info("scheduler_tier_updated", tier=tier.value)

    def record_last_backup(self, timestamp: datetime) -> None:
        """Set the last backup timestamp (e.g., from persisted state).

        Args:
            timestamp: UTC timestamp of the last known backup.
        """
        self._last_backup_at = timestamp

    def status(self) -> dict[str, Any]:
        """Return scheduler status for observability.

        Returns:
            Dict with tier, running state, last backup time, and failure count.
        """
        return {
            "tier": self._tier.value,
            "running": self._running,
            "interval_seconds": self._schedule.interval_seconds,
            "last_backup_at": self._last_backup_at.isoformat() if self._last_backup_at else None,
            "consecutive_failures": self._consecutive_failures,
            "window": {
                "start_hour": self._schedule.window_start_hour,
                "end_hour": self._schedule.window_end_hour,
            },
            "retention": {
                "keep_daily": self._schedule.retention.keep_daily,
                "keep_weekly": self._schedule.retention.keep_weekly,
                "keep_monthly": self._schedule.retention.keep_monthly,
            },
        }
