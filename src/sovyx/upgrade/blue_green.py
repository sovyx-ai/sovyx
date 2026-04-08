"""Blue-green upgrade orchestrator — safe, atomic version upgrades.

Implements the full upgrade pipeline: backup → install → migrate →
swap → verify → cleanup (or rollback on failure).  Designed for
single-user local installations where Sovyx manages its own process
lifecycle.

Ref: SPE-028 §4 (expand-contract), §8 (version compatibility).
"""

from __future__ import annotations

import dataclasses
import enum
import time
from typing import TYPE_CHECKING, Any

from sovyx.engine.errors import MigrationError
from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from sovyx.persistence.pool import DatabasePool
    from sovyx.upgrade.backup_manager import BackupInfo, BackupManager, BackupTrigger
    from sovyx.upgrade.doctor import DiagnosticReport, Doctor
    from sovyx.upgrade.schema import MigrationReport, MigrationRunner, UpgradeMigration

logger = get_logger(__name__)


# ── Enums & Data classes ────────────────────────────────────────────


class UpgradePhase(enum.Enum):
    """Current phase of the blue-green upgrade."""

    PENDING = "pending"
    BACKUP = "backup"
    INSTALL = "install"
    MIGRATE = "migrate"
    VERIFY = "verify"
    SWAP = "swap"
    CLEANUP = "cleanup"
    ROLLBACK = "rollback"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclasses.dataclass
class UpgradeResult:
    """Outcome of a blue-green upgrade attempt.

    Attributes:
        status: Final status — ``"completed"`` or ``"failed"``.
        from_version: Version before the upgrade.
        to_version: Target version.
        phases_completed: Ordered list of phases that finished.
        migration_report: Migration runner report (if migrations ran).
        diagnostic_report: Doctor report from verification.
        backup_path: Path to the pre-upgrade backup.
        error: Error message if the upgrade failed.
        duration_ms: Total wall-clock milliseconds.
        rolled_back: Whether a rollback was performed.
    """

    status: str = "pending"
    from_version: str = ""
    to_version: str = ""
    phases_completed: list[str] = dataclasses.field(default_factory=list)
    migration_report: MigrationReport | None = None
    diagnostic_report: DiagnosticReport | None = None
    backup_path: Path | None = None
    error: str = ""
    duration_ms: int = 0
    rolled_back: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dictionary."""
        result: dict[str, Any] = {
            "status": self.status,
            "from_version": self.from_version,
            "to_version": self.to_version,
            "phases_completed": list(self.phases_completed),
            "error": self.error,
            "duration_ms": self.duration_ms,
            "rolled_back": self.rolled_back,
        }
        if self.backup_path is not None:
            result["backup_path"] = str(self.backup_path)
        if self.migration_report is not None:
            result["migrations_applied"] = self.migration_report.applied
        if self.diagnostic_report is not None:
            result["diagnostic_healthy"] = self.diagnostic_report.healthy
        return result


class UpgradeError(MigrationError):
    """Raised when the blue-green upgrade pipeline fails."""


class InstallError(UpgradeError):
    """Raised when version installation fails."""


class VerificationError(UpgradeError):
    """Raised when post-upgrade verification fails."""


class RollbackError(UpgradeError):
    """Raised when rollback itself fails (critical)."""


# ── Installer protocol ──────────────────────────────────────────────


class VersionInstaller:
    """Abstract installer for new Sovyx versions.

    Subclass to implement actual installation (pip, container swap, etc.).
    The default implementation is a no-op suitable for schema-only upgrades.
    """

    async def install(self, target_version: str) -> Path | None:
        """Install *target_version* and return the install path.

        Args:
            target_version: Semver string of the target version.

        Returns:
            Path to the installed version, or ``None`` for in-place.
        """
        return None

    async def swap(self, install_path: Path | None) -> None:
        """Make the new version the active one.

        Args:
            install_path: Path returned by :meth:`install`.
        """

    async def swap_back(self, install_path: Path | None) -> None:
        """Revert to the previous version.

        Args:
            install_path: Path returned by :meth:`install`.
        """

    async def cleanup(self, install_path: Path | None) -> None:
        """Remove old version artifacts after successful upgrade.

        Args:
            install_path: Path returned by :meth:`install`.
        """


# ── Blue-green orchestrator ─────────────────────────────────────────


class BlueGreenUpgrader:
    """Orchestrate a safe blue-green upgrade pipeline.

    Pipeline (SPE-028):

        1. **Backup** — snapshot via :class:`BackupManager`
        2. **Install** — install new version via :class:`VersionInstaller`
        3. **Migrate** — run pending schema migrations via :class:`MigrationRunner`
        4. **Verify** — run :class:`Doctor` diagnostics
        5. **Swap** — make new version active
        6. **Cleanup** — remove old version artifacts

    On failure at any phase, the pipeline:
        - Restores the database from backup
        - Reverts to the previous version (swap_back)
        - Logs the failure

    Args:
        pool: Database pool for backup/migration operations.
        backup_manager: Creates pre-upgrade backups.
        migration_runner: Applies schema migrations.
        doctor: Runs post-upgrade diagnostics.
        installer: Handles version installation and swap.
        migrations: Known upgrade migrations.
        backup_trigger: Trigger type for the backup (default: migration).
    """

    def __init__(
        self,
        pool: DatabasePool,
        backup_manager: BackupManager,
        migration_runner: MigrationRunner,
        doctor: Doctor,
        *,
        installer: VersionInstaller | None = None,
        migrations: Sequence[UpgradeMigration] | None = None,
        backup_trigger: BackupTrigger | None = None,
    ) -> None:
        self._pool = pool
        self._backup_manager = backup_manager
        self._migration_runner = migration_runner
        self._doctor = doctor
        self._installer = installer or VersionInstaller()
        self._migrations = list(migrations or [])
        self._backup_trigger = backup_trigger

    # ── Public API ──────────────────────────────────────────────

    async def upgrade(
        self,
        from_version: str,
        to_version: str,
    ) -> UpgradeResult:
        """Execute the full blue-green upgrade pipeline.

        Args:
            from_version: Current application version (semver string).
            to_version: Target application version (semver string).

        Returns:
            :class:`UpgradeResult` describing the outcome.
        """
        start = time.monotonic()
        result = UpgradeResult(
            from_version=from_version,
            to_version=to_version,
        )

        backup_info: BackupInfo | None = None
        install_path: Path | None = None

        try:
            # ── Phase 1: Backup ──
            backup_info = await self._phase_backup(result)

            # ── Phase 2: Install ──
            install_path = await self._phase_install(result, to_version)

            # ── Phase 3: Migrate ──
            await self._phase_migrate(result)

            # ── Phase 4: Verify ──
            await self._phase_verify(result)

            # ── Phase 5: Swap ──
            await self._phase_swap(result, install_path)

            # ── Phase 6: Cleanup ──
            await self._phase_cleanup(result, install_path)

            result.status = "completed"

        except UpgradeError as exc:
            result.status = "failed"
            result.error = str(exc)
            logger.error(
                "upgrade_failed",
                phase=result.phases_completed[-1] if result.phases_completed else "init",
                error=str(exc),
            )
            await self._rollback(result, backup_info, install_path)

        except Exception as exc:
            result.status = "failed"
            result.error = f"Unexpected error: {exc}"
            logger.error(
                "upgrade_unexpected_error",
                error=str(exc),
                exc_info=True,
            )
            await self._rollback(result, backup_info, install_path)

        result.duration_ms = int((time.monotonic() - start) * 1000)

        logger.info(
            "upgrade_complete",
            status=result.status,
            from_version=from_version,
            to_version=to_version,
            duration_ms=result.duration_ms,
            rolled_back=result.rolled_back,
        )

        return result

    # ── Phase implementations ───────────────────────────────────

    async def _phase_backup(self, result: UpgradeResult) -> BackupInfo:
        """Create a pre-upgrade database backup."""
        logger.info("upgrade_phase_backup")
        try:
            from sovyx.upgrade.backup_manager import BackupTrigger as _BackupTrigger

            trigger = self._backup_trigger or _BackupTrigger.MIGRATION
            backup_info = await self._backup_manager.create_backup(trigger)
            result.backup_path = backup_info.path
            result.phases_completed.append(UpgradePhase.BACKUP.value)
            return backup_info
        except Exception as exc:
            msg = f"Backup failed: {exc}"
            raise UpgradeError(msg) from exc

    async def _phase_install(
        self,
        result: UpgradeResult,
        to_version: str,
    ) -> Path | None:
        """Install the target version."""
        logger.info("upgrade_phase_install", to_version=to_version)
        try:
            install_path = await self._installer.install(to_version)
            result.phases_completed.append(UpgradePhase.INSTALL.value)
            return install_path
        except UpgradeError:
            raise
        except Exception as exc:
            msg = f"Installation failed: {exc}"
            raise InstallError(msg) from exc

    async def _phase_migrate(self, result: UpgradeResult) -> None:
        """Run pending schema migrations."""
        logger.info("upgrade_phase_migrate")
        try:
            report = await self._migration_runner.run(self._migrations)
            result.migration_report = report

            if report.status == "failed":
                msg = f"Migration failed: {report.error}"
                raise UpgradeError(msg)

            result.phases_completed.append(UpgradePhase.MIGRATE.value)

        except UpgradeError:
            raise
        except Exception as exc:
            msg = f"Migration error: {exc}"
            raise UpgradeError(msg) from exc

    async def _phase_verify(self, result: UpgradeResult) -> None:
        """Run Doctor diagnostics to verify upgrade health."""
        logger.info("upgrade_phase_verify")
        try:
            report = await self._doctor.run_all()
            result.diagnostic_report = report

            if not report.healthy:
                failed_checks = [r.check for r in report.results if r.status.value == "fail"]
                msg = (
                    f"Verification failed: {report.failed} check(s) failed "
                    f"({', '.join(failed_checks)})"
                )
                raise VerificationError(msg)

            result.phases_completed.append(UpgradePhase.VERIFY.value)

        except VerificationError:
            raise
        except Exception as exc:
            msg = f"Verification error: {exc}"
            raise VerificationError(msg) from exc

    async def _phase_swap(
        self,
        result: UpgradeResult,
        install_path: Path | None,
    ) -> None:
        """Activate the new version."""
        logger.info("upgrade_phase_swap")
        try:
            await self._installer.swap(install_path)
            result.phases_completed.append(UpgradePhase.SWAP.value)
        except Exception as exc:
            msg = f"Swap failed: {exc}"
            raise UpgradeError(msg) from exc

    async def _phase_cleanup(
        self,
        result: UpgradeResult,
        install_path: Path | None,
    ) -> None:
        """Remove old version artifacts."""
        logger.info("upgrade_phase_cleanup")
        try:
            await self._installer.cleanup(install_path)
            result.phases_completed.append(UpgradePhase.CLEANUP.value)
        except Exception as exc:
            # Cleanup failure is non-fatal — log but don't fail upgrade
            logger.warning("upgrade_cleanup_failed", error=str(exc))
            result.phases_completed.append(UpgradePhase.CLEANUP.value)

    # ── Rollback ────────────────────────────────────────────────

    async def _rollback(
        self,
        result: UpgradeResult,
        backup_info: BackupInfo | None,
        install_path: Path | None,
    ) -> None:
        """Restore database and revert version on failure."""
        logger.info("upgrade_rollback_starting")

        # 1. Restore database from backup
        if backup_info is not None:
            try:
                await self._backup_manager.restore_backup(backup_info.path)
                logger.info(
                    "upgrade_rollback_db_restored",
                    path=str(backup_info.path),
                )
            except Exception as exc:
                logger.error(
                    "upgrade_rollback_db_restore_failed",
                    error=str(exc),
                )

        # 2. Swap back to old version
        try:
            await self._installer.swap_back(install_path)
        except Exception as exc:
            logger.error(
                "upgrade_rollback_swap_back_failed",
                error=str(exc),
            )

        result.rolled_back = True
        result.phases_completed.append(UpgradePhase.ROLLBACK.value)
        logger.info("upgrade_rollback_complete")

    # ── Utility ─────────────────────────────────────────────────

    async def check_upgrade_available(
        self,
        current_version: str,
    ) -> dict[str, Any]:
        """Check if there are pending migrations.

        Args:
            current_version: Current application version.

        Returns:
            Dict with ``has_pending``, ``pending_count``, and ``migrations``.
        """
        from sovyx.upgrade.schema import SemVer

        SemVer.parse(current_version)  # validate format
        schema_version = self._migration_runner.schema_version
        db_version = await schema_version.get_current()
        pending = schema_version.get_pending(db_version, self._migrations)

        return {
            "current_version": current_version,
            "db_schema_version": str(db_version),
            "has_pending": len(pending) > 0,
            "pending_count": len(pending),
            "migrations": [{"version": m.version, "description": m.description} for m in pending],
        }
