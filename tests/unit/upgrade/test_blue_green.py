"""Tests for BlueGreenUpgrader — zero-downtime upgrade with rollback (V05-33)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

from sovyx.upgrade.blue_green import (
    BlueGreenUpgrader,
    InstallError,
    UpgradeError,
    UpgradePhase,
    UpgradeResult,
    VerificationError,
    VersionInstaller,
)

if TYPE_CHECKING:
    from sovyx.upgrade.backup_manager import BackupInfo


# ── Helpers ───────────────────────────────────────────────────────────


def _make_backup_info(tmp_path: Path) -> BackupInfo:
    """Create a real BackupInfo for testing."""
    from sovyx.upgrade.backup_manager import BackupInfo, BackupTrigger

    bk_path = tmp_path / "backup_migration_20260407.db"
    bk_path.write_bytes(b"backup_data")
    return BackupInfo(
        path=bk_path,
        trigger=BackupTrigger.MIGRATION,
        created_at="2026-04-07T17:00:00Z",
        size_bytes=1024,
        encrypted=False,
    )


def _make_upgrader(
    tmp_path: Path,
    *,
    backup_ok: bool = True,
    migration_status: str = "success",
    migration_error: str = "",
    migration_applied: list[str] | None = None,
    doctor_healthy: bool = True,
    doctor_failed_checks: list[str] | None = None,
    installer: VersionInstaller | None = None,
) -> BlueGreenUpgrader:
    """Build a BlueGreenUpgrader with mocked dependencies."""
    info = _make_backup_info(tmp_path)

    # Pool mock
    pool = AsyncMock()

    # BackupManager mock
    backup_mgr = AsyncMock()
    if backup_ok:
        backup_mgr.create_backup = AsyncMock(return_value=info)
    else:
        backup_mgr.create_backup = AsyncMock(side_effect=RuntimeError("Disk full"))
    backup_mgr.restore_backup = AsyncMock()
    backup_mgr.prune = AsyncMock(return_value=2)

    # MigrationRunner mock
    migration_runner = AsyncMock()
    mig_report = MagicMock()
    mig_report.status = migration_status
    mig_report.error = migration_error
    mig_report.applied = migration_applied or ["v0.6.0: add column"]
    migration_runner.run = AsyncMock(return_value=mig_report)
    migration_runner._version = AsyncMock()  # noqa: SLF001
    migration_runner._version.get_current = AsyncMock(  # noqa: SLF001
        return_value=MagicMock(__str__=lambda self: "0.5.0")
    )
    migration_runner._version.get_pending = MagicMock(return_value=[])  # noqa: SLF001

    # Doctor mock
    doctor = AsyncMock()
    diag_report = MagicMock()
    diag_report.healthy = doctor_healthy
    diag_report.passed = 10 if doctor_healthy else 8
    diag_report.warned = 1
    diag_report.failed = 0 if doctor_healthy else 2
    if doctor_failed_checks:
        results = []
        for check_name in doctor_failed_checks:
            r = MagicMock()
            r.check = check_name
            r.status = MagicMock()
            r.status.value = "fail"
            results.append(r)
        diag_report.results = results
    else:
        diag_report.results = []
    doctor.run_all = AsyncMock(return_value=diag_report)

    return BlueGreenUpgrader(
        pool=pool,
        backup_manager=backup_mgr,
        migration_runner=migration_runner,
        doctor=doctor,
        installer=installer,
    )


# ── UpgradePhase Tests ───────────────────────────────────────────────


class TestUpgradePhase:
    """Tests for UpgradePhase enum."""

    def test_all_values(self) -> None:
        assert len(UpgradePhase) == 10  # noqa: PLR2004

    def test_specific_values(self) -> None:
        assert UpgradePhase.PENDING.value == "pending"
        assert UpgradePhase.COMPLETED.value == "completed"
        assert UpgradePhase.ROLLBACK.value == "rollback"
        assert UpgradePhase.FAILED.value == "failed"


# ── UpgradeResult Tests ──────────────────────────────────────────────


class TestUpgradeResult:
    """Tests for UpgradeResult dataclass."""

    def test_defaults(self) -> None:
        result = UpgradeResult()
        assert result.status == "pending"
        assert result.from_version == ""
        assert result.to_version == ""
        assert result.phases_completed == []
        assert result.error == ""
        assert result.duration_ms == 0
        assert result.rolled_back is False

    def test_to_dict_minimal(self) -> None:
        result = UpgradeResult(
            status="completed",
            from_version="0.5.0",
            to_version="0.6.0",
        )
        d = result.to_dict()
        assert d["status"] == "completed"
        assert d["from_version"] == "0.5.0"
        assert d["to_version"] == "0.6.0"
        assert d["rolled_back"] is False
        assert "backup_path" not in d

    def test_to_dict_with_backup(self, tmp_path: Path) -> None:
        result = UpgradeResult(
            backup_path=tmp_path / "backup.db",
        )
        d = result.to_dict()
        assert "backup_path" in d
        assert str(tmp_path / "backup.db") in d["backup_path"]

    def test_to_dict_with_migration_report(self) -> None:
        mig = MagicMock()
        mig.applied = ["v0.6.0: test"]
        result = UpgradeResult(migration_report=mig)
        d = result.to_dict()
        assert d["migrations_applied"] == ["v0.6.0: test"]

    def test_to_dict_with_diagnostic_report(self) -> None:
        diag = MagicMock()
        diag.healthy = True
        result = UpgradeResult(diagnostic_report=diag)
        d = result.to_dict()
        assert d["diagnostic_healthy"] is True

    def test_to_dict_json_serializable(self) -> None:
        result = UpgradeResult(
            status="completed",
            from_version="0.5.0",
            to_version="0.6.0",
            duration_ms=500,
        )
        j = json.dumps(result.to_dict())
        parsed = json.loads(j)
        assert parsed["status"] == "completed"


# ── Error Classes ─────────────────────────────────────────────────────


class TestErrors:
    """Tests for upgrade error hierarchy."""

    def test_upgrade_error_is_migration_error(self) -> None:
        from sovyx.engine.errors import MigrationError

        assert issubclass(UpgradeError, MigrationError)

    def test_install_error_is_upgrade_error(self) -> None:
        assert issubclass(InstallError, UpgradeError)

    def test_verification_error_is_upgrade_error(self) -> None:
        assert issubclass(VerificationError, UpgradeError)


# ── VersionInstaller Tests ────────────────────────────────────────────


class TestVersionInstaller:
    """Tests for default VersionInstaller (no-op)."""

    @pytest.mark.asyncio()
    async def test_install_returns_none(self) -> None:
        installer = VersionInstaller()
        result = await installer.install("0.6.0")
        assert result is None

    @pytest.mark.asyncio()
    async def test_swap_no_op(self) -> None:
        installer = VersionInstaller()
        await installer.swap(None)

    @pytest.mark.asyncio()
    async def test_swap_back_no_op(self) -> None:
        installer = VersionInstaller()
        await installer.swap_back(None)

    @pytest.mark.asyncio()
    async def test_cleanup_no_op(self) -> None:
        installer = VersionInstaller()
        await installer.cleanup(None)


# ── BlueGreenUpgrader — Happy Path ───────────────────────────────────


class TestHappyPath:
    """Tests for successful upgrade flow."""

    @pytest.mark.asyncio()
    async def test_full_upgrade_success(self, tmp_path: Path) -> None:
        upgrader = _make_upgrader(tmp_path)
        result = await upgrader.upgrade("0.5.0", "0.6.0")
        assert result.status == "completed"
        assert result.from_version == "0.5.0"
        assert result.to_version == "0.6.0"

    @pytest.mark.asyncio()
    async def test_all_phases_completed(self, tmp_path: Path) -> None:
        upgrader = _make_upgrader(tmp_path)
        result = await upgrader.upgrade("0.5.0", "0.6.0")
        expected = ["backup", "install", "migrate", "verify", "swap", "cleanup"]
        assert result.phases_completed == expected

    @pytest.mark.asyncio()
    async def test_duration_positive(self, tmp_path: Path) -> None:
        upgrader = _make_upgrader(tmp_path)
        result = await upgrader.upgrade("0.5.0", "0.6.0")
        assert result.duration_ms >= 0

    @pytest.mark.asyncio()
    async def test_not_rolled_back(self, tmp_path: Path) -> None:
        upgrader = _make_upgrader(tmp_path)
        result = await upgrader.upgrade("0.5.0", "0.6.0")
        assert result.rolled_back is False

    @pytest.mark.asyncio()
    async def test_backup_path_set(self, tmp_path: Path) -> None:
        upgrader = _make_upgrader(tmp_path)
        result = await upgrader.upgrade("0.5.0", "0.6.0")
        assert result.backup_path is not None

    @pytest.mark.asyncio()
    async def test_migration_report_set(self, tmp_path: Path) -> None:
        upgrader = _make_upgrader(tmp_path)
        result = await upgrader.upgrade("0.5.0", "0.6.0")
        assert result.migration_report is not None

    @pytest.mark.asyncio()
    async def test_diagnostic_report_set(self, tmp_path: Path) -> None:
        upgrader = _make_upgrader(tmp_path)
        result = await upgrader.upgrade("0.5.0", "0.6.0")
        assert result.diagnostic_report is not None

    @pytest.mark.asyncio()
    async def test_no_pending_migrations(self, tmp_path: Path) -> None:
        upgrader = _make_upgrader(tmp_path, migration_status="up_to_date")
        result = await upgrader.upgrade("0.5.0", "0.6.0")
        assert result.status == "completed"

    @pytest.mark.asyncio()
    async def test_with_custom_installer(self, tmp_path: Path) -> None:
        installer = AsyncMock(spec=VersionInstaller)
        installer.install = AsyncMock(return_value=None)
        installer.swap = AsyncMock()
        installer.cleanup = AsyncMock()
        upgrader = _make_upgrader(tmp_path, installer=installer)
        result = await upgrader.upgrade("0.5.0", "0.6.0")
        assert result.status == "completed"
        installer.install.assert_awaited_once_with("0.6.0")
        installer.swap.assert_awaited_once()


# ── BlueGreenUpgrader — Rollback Scenarios ────────────────────────────


class TestRollback:
    """Tests for rollback on failure."""

    @pytest.mark.asyncio()
    async def test_backup_failure_fails(self, tmp_path: Path) -> None:
        upgrader = _make_upgrader(tmp_path, backup_ok=False)
        result = await upgrader.upgrade("0.5.0", "0.6.0")
        assert result.status == "failed"
        assert "disk full" in result.error.lower()
        assert result.rolled_back is True

    @pytest.mark.asyncio()
    async def test_install_failure_rollback(self, tmp_path: Path) -> None:
        installer = AsyncMock(spec=VersionInstaller)
        installer.install = AsyncMock(side_effect=InstallError("pip failed"))
        installer.swap_back = AsyncMock()
        upgrader = _make_upgrader(tmp_path, installer=installer)
        result = await upgrader.upgrade("0.5.0", "0.6.0")
        assert result.status == "failed"
        assert result.rolled_back is True

    @pytest.mark.asyncio()
    async def test_migration_failure_rollback(self, tmp_path: Path) -> None:
        upgrader = _make_upgrader(
            tmp_path,
            migration_status="failed",
            migration_error="column conflict",
        )
        result = await upgrader.upgrade("0.5.0", "0.6.0")
        assert result.status == "failed"
        assert result.rolled_back is True
        assert "migration failed" in result.error.lower()

    @pytest.mark.asyncio()
    async def test_verify_failure_rollback(self, tmp_path: Path) -> None:
        upgrader = _make_upgrader(
            tmp_path,
            doctor_healthy=False,
            doctor_failed_checks=["db_integrity", "schema_version_valid"],
        )
        result = await upgrader.upgrade("0.5.0", "0.6.0")
        assert result.status == "failed"
        assert result.rolled_back is True
        assert "verification failed" in result.error.lower()

    @pytest.mark.asyncio()
    async def test_rollback_restores_backup(self, tmp_path: Path) -> None:
        upgrader = _make_upgrader(
            tmp_path,
            doctor_healthy=False,
            doctor_failed_checks=["db_integrity"],
        )
        result = await upgrader.upgrade("0.5.0", "0.6.0")
        assert result.rolled_back is True
        upgrader._backup_manager.restore_backup.assert_awaited()

    @pytest.mark.asyncio()
    async def test_rollback_calls_swap_back(self, tmp_path: Path) -> None:
        installer = AsyncMock(spec=VersionInstaller)
        installer.install = AsyncMock(return_value=Path("/tmp/v0.6.0"))
        installer.swap = AsyncMock(side_effect=RuntimeError("Swap broke"))
        installer.swap_back = AsyncMock()
        upgrader = _make_upgrader(tmp_path, installer=installer)
        result = await upgrader.upgrade("0.5.0", "0.6.0")
        assert result.rolled_back is True
        installer.swap_back.assert_awaited()

    @pytest.mark.asyncio()
    async def test_rollback_restore_failure_logged(self, tmp_path: Path) -> None:
        """When restore fails during rollback, it's logged but doesn't crash."""
        upgrader = _make_upgrader(
            tmp_path,
            doctor_healthy=False,
            doctor_failed_checks=["db_integrity"],
        )
        upgrader._backup_manager.restore_backup = AsyncMock(
            side_effect=RuntimeError("Restore failed")
        )
        result = await upgrader.upgrade("0.5.0", "0.6.0")
        assert result.status == "failed"
        assert result.rolled_back is True

    @pytest.mark.asyncio()
    async def test_cleanup_failure_non_fatal(self, tmp_path: Path) -> None:
        """Cleanup failure should not prevent completed status."""
        installer = AsyncMock(spec=VersionInstaller)
        installer.install = AsyncMock(return_value=None)
        installer.swap = AsyncMock()
        installer.cleanup = AsyncMock(side_effect=RuntimeError("Cleanup err"))
        upgrader = _make_upgrader(tmp_path, installer=installer)
        result = await upgrader.upgrade("0.5.0", "0.6.0")
        assert result.status == "completed"

    @pytest.mark.asyncio()
    async def test_unexpected_error_rollback(self, tmp_path: Path) -> None:
        """Non-UpgradeError exceptions still trigger rollback (line 263-271).

        We must bypass phase-level try/except wrappers so the bare Exception
        propagates to the top-level ``except Exception`` block in ``upgrade()``.
        """
        upgrader = _make_upgrader(tmp_path)
        # Patch the phase method itself (not an inner call) so the phase
        # wrapper is skipped and the raw Exception reaches lines 263-271.
        upgrader._phase_verify = AsyncMock(  # noqa: SLF001
            side_effect=RuntimeError("totally unexpected"),
        )
        result = await upgrader.upgrade("0.5.0", "0.6.0")
        assert result.status == "failed"
        assert result.rolled_back is True
        assert "unexpected" in result.error.lower()


# ── check_upgrade_available ───────────────────────────────────────────


class TestCheckUpgrade:
    """Tests for check_upgrade_available utility."""

    @pytest.mark.asyncio()
    async def test_no_pending(self, tmp_path: Path) -> None:
        upgrader = _make_upgrader(tmp_path)
        info = await upgrader.check_upgrade_available("0.5.0")
        assert info["current_version"] == "0.5.0"
        assert info["has_pending"] is False
        assert info["pending_count"] == 0

    @pytest.mark.asyncio()
    async def test_invalid_version_raises(self, tmp_path: Path) -> None:
        upgrader = _make_upgrader(tmp_path)
        with pytest.raises(ValueError, match="Invalid semver"):
            await upgrader.check_upgrade_available("not-semver")

    @pytest.mark.asyncio()
    async def test_with_pending(self, tmp_path: Path) -> None:
        upgrader = _make_upgrader(tmp_path)
        mock_migration = MagicMock()
        mock_migration.version = "0.6.0"
        mock_migration.description = "add feature"
        upgrader._migration_runner._version.get_pending = MagicMock(  # noqa: SLF001
            return_value=[mock_migration]
        )
        info = await upgrader.check_upgrade_available("0.5.0")
        assert info["has_pending"] is True
        assert info["pending_count"] == 1
        assert info["migrations"][0]["version"] == "0.6.0"


# ── Edge-case exception paths ────────────────────────────────────────


class TestEdgeCaseExceptions:
    """Cover exception wrapping paths in individual phases."""

    @pytest.mark.asyncio()
    async def test_install_raw_exception_becomes_install_error(
        self,
        tmp_path: Path,
    ) -> None:
        """Non-UpgradeError from installer.install becomes InstallError."""
        installer = AsyncMock(spec=VersionInstaller)
        installer.install = AsyncMock(side_effect=OSError("permission denied"))
        installer.swap_back = AsyncMock()

        upgrader = _make_upgrader(tmp_path, installer=installer)
        result = await upgrader.upgrade("0.4.0", "0.5.0")

        assert result.status == "failed"
        assert "Installation failed" in result.error
        assert result.rolled_back is True

    @pytest.mark.asyncio()
    async def test_migrate_raw_exception_wrapped(
        self,
        tmp_path: Path,
    ) -> None:
        """Non-UpgradeError from migration_runner.run becomes UpgradeError."""
        upgrader = _make_upgrader(tmp_path)
        # Replace migration runner's run with raw exception
        upgrader._migration_runner.run = AsyncMock(  # noqa: SLF001
            side_effect=RuntimeError("sqlite locked"),
        )
        result = await upgrader.upgrade("0.4.0", "0.5.0")

        assert result.status == "failed"
        assert "Migration error" in result.error
        assert result.rolled_back is True

    @pytest.mark.asyncio()
    async def test_swap_back_failure_during_rollback(
        self,
        tmp_path: Path,
    ) -> None:
        """If swap_back fails during rollback, rollback still completes."""
        installer = AsyncMock(spec=VersionInstaller)
        installer.install = AsyncMock(return_value=None)
        installer.swap = AsyncMock()
        installer.swap_back = AsyncMock(side_effect=OSError("cannot revert"))

        upgrader = _make_upgrader(
            tmp_path,
            doctor_healthy=False,
            doctor_failed_checks=["db_integrity"],
            installer=installer,
        )
        result = await upgrader.upgrade("0.4.0", "0.5.0")

        assert result.status == "failed"
        assert result.rolled_back is True
        assert "rollback" in result.phases_completed
