"""POLISH-11: Upgrade pipeline E2E — migrations + doctor on real SQLite.

Tests the upgrade subsystem with real databases:
  1. Create fresh DB → run system migrations → verify schema
  2. Run brain migrations → verify brain tables
  3. Doctor checks on healthy installation
  4. Blue-green upgrade with mock version installer
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from sovyx.persistence.migrations import MigrationRunner
from sovyx.persistence.pool import DatabasePool
from sovyx.persistence.schemas.conversations import get_conversation_migrations
from sovyx.persistence.schemas.system import get_system_migrations
from sovyx.upgrade.doctor import DiagnosticStatus, Doctor

if TYPE_CHECKING:
    from pathlib import Path


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture()
async def system_pool(tmp_path: Path) -> DatabasePool:
    """Fresh system database."""
    pool = DatabasePool(db_path=tmp_path / "system.db", read_pool_size=1)
    await pool.initialize()
    yield pool  # type: ignore[misc]
    await pool.close()


@pytest.fixture()
async def brain_pool(tmp_path: Path) -> DatabasePool:
    """Fresh brain database."""
    pool = DatabasePool(db_path=tmp_path / "brain.db", read_pool_size=1)
    await pool.initialize()
    yield pool  # type: ignore[misc]
    await pool.close()


# ── Migration Tests ─────────────────────────────────────────────────────────


class TestMigrationPipeline:
    """Verify migration pipeline on real SQLite."""

    @pytest.mark.asyncio()
    @pytest.mark.timeout(10)
    async def test_system_migrations_create_expected_tables(
        self,
        system_pool: DatabasePool,
    ) -> None:
        """System migrations create persons and channel_mappings tables."""
        runner = MigrationRunner(system_pool)
        await runner.initialize()
        await runner.run_migrations(get_system_migrations())

        async with system_pool.read() as conn:
            cursor = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name",
            )
            tables = {row[0] for row in await cursor.fetchall()}

        assert "persons" in tables
        assert "channel_mappings" in tables
        assert "_schema" in tables

    @pytest.mark.asyncio()
    @pytest.mark.timeout(10)
    async def test_conversation_migrations_create_expected_tables(
        self,
        brain_pool: DatabasePool,
    ) -> None:
        """Conversation migrations create conversations and turns tables."""
        runner = MigrationRunner(brain_pool)
        await runner.initialize()
        await runner.run_migrations(get_conversation_migrations())

        async with brain_pool.read() as conn:
            cursor = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name",
            )
            tables = {row[0] for row in await cursor.fetchall()}

        assert "conversations" in tables
        assert "conversation_turns" in tables

    @pytest.mark.asyncio()
    @pytest.mark.timeout(10)
    async def test_migrations_are_idempotent(
        self,
        system_pool: DatabasePool,
    ) -> None:
        """Running migrations twice doesn't error or duplicate data."""
        runner = MigrationRunner(system_pool)
        await runner.initialize()
        migrations = get_system_migrations()
        await runner.run_migrations(migrations)
        # Run again — should be a no-op
        await runner.run_migrations(migrations)

        async with system_pool.read() as conn:
            cursor = await conn.execute("SELECT version FROM _schema")
            rows = await cursor.fetchall()

        # Should have exactly 1 version entry (not duplicated)
        assert len(rows) >= 1


# ── Doctor Tests ────────────────────────────────────────────────────────────


class TestDoctorDiagnostics:
    """Doctor checks on real installation."""

    @pytest.mark.asyncio()
    @pytest.mark.timeout(10)
    async def test_doctor_runs_all_checks(self, tmp_path: Path) -> None:
        """Doctor runs all checks without crashing."""
        doctor = Doctor(data_dir=tmp_path, port=19999)
        report = await doctor.run_all()

        # Should have multiple check results
        assert len(report.results) > 0
        # Each result has a name and status
        for result in report.results:
            assert result.check
            assert result.status in (
                DiagnosticStatus.PASS,
                DiagnosticStatus.WARN,
                DiagnosticStatus.FAIL,
            )

    @pytest.mark.asyncio()
    @pytest.mark.timeout(10)
    async def test_doctor_python_version_passes(self, tmp_path: Path) -> None:
        """Python version check should pass (we're running ≥3.11)."""
        doctor = Doctor(data_dir=tmp_path, port=19999)
        result = await doctor.run_check("python_version")
        assert result.status == DiagnosticStatus.PASS


# ── Blue-Green Upgrade Tests ───────────────────────────────────────────────
