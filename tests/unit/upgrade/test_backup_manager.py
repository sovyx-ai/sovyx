"""Tests for upgrade BackupManager (V05-32).

Covers: create + restore, retention enforcement (keep 5 migration, 7 daily,
3 manual), prune oldest, list + filter, encrypted at-rest backups,
integrity verification, error handling.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

from sovyx.upgrade.backup_manager import (
    BackupError,
    BackupIntegrityError,
    BackupManager,
    BackupTrigger,
    _build_backup_info,
    _verify_integrity,
)

# ── Fixtures ────────────────────────────────────────────────────────


def _create_test_db(path: Path) -> None:
    """Create a minimal valid SQLite database."""
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE test (id INTEGER PRIMARY KEY, val TEXT)")
    conn.execute("INSERT INTO test VALUES (1, 'hello')")
    conn.commit()
    conn.close()


def _make_mock_pool(db_path: Path) -> MagicMock:
    """Create a mock DatabasePool that supports VACUUM INTO and PRAGMA database_list."""
    pool = MagicMock()

    @asynccontextmanager
    async def write_ctx() -> AsyncGenerator[AsyncMock, None]:
        conn = AsyncMock()

        async def mock_execute(sql: str, *args: object) -> AsyncMock:
            if sql.startswith("VACUUM INTO"):
                # Extract path and do real VACUUM INTO
                target = sql.split("'")[1]
                real_conn = sqlite3.connect(str(db_path))
                real_conn.execute(f"VACUUM INTO '{target}'")
                real_conn.close()
            return AsyncMock()

        conn.execute = mock_execute
        yield conn

    @asynccontextmanager
    async def read_ctx() -> AsyncGenerator[AsyncMock, None]:
        conn = AsyncMock()
        result = AsyncMock()
        result.fetchall = AsyncMock(return_value=[(0, "main", str(db_path))])
        conn.execute = AsyncMock(return_value=result)
        yield conn

    pool.write = write_ctx
    pool.read = read_ctx
    return pool


# ── Unit tests: _verify_integrity ───────────────────────────────────


class TestVerifyIntegrity:
    """PRAGMA integrity_check on backup files."""

    def test_valid_db(self, tmp_path: Path) -> None:
        db = tmp_path / "valid.db"
        _create_test_db(db)
        _verify_integrity(db)  # should not raise

    def test_corrupt_file(self, tmp_path: Path) -> None:
        bad = tmp_path / "corrupt.db"
        bad.write_bytes(b"not a database at all")
        with pytest.raises(BackupIntegrityError, match="not a valid SQLite"):
            _verify_integrity(bad)

    def test_empty_file(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty.db"
        empty.write_bytes(b"")
        # Empty file is treated as empty database by sqlite3; integrity_check
        # returns "ok" for it, so we just verify it doesn't crash.
        _verify_integrity(empty)  # should not raise


# ── Unit tests: _build_backup_info ──────────────────────────────────


class TestBuildBackupInfo:
    """Parse backup metadata from filenames."""

    def test_plain_backup(self, tmp_path: Path) -> None:
        f = tmp_path / "sovyx_migration_20260407_120000.db"
        f.write_bytes(b"x" * 100)
        info = _build_backup_info(f)
        assert info.trigger == "migration"
        assert info.size_bytes == 100
        assert info.encrypted is False

    def test_encrypted_backup(self, tmp_path: Path) -> None:
        f = tmp_path / "sovyx_daily_20260407_120000.db.enc"
        f.write_bytes(b"x" * 200)
        info = _build_backup_info(f)
        assert info.trigger == "daily"
        assert info.size_bytes == 200
        assert info.encrypted is True

    def test_manual_trigger(self, tmp_path: Path) -> None:
        f = tmp_path / "sovyx_manual_20260407_120000.db"
        f.write_bytes(b"x")
        info = _build_backup_info(f)
        assert info.trigger == "manual"


# ── Unit tests: BackupManager init ──────────────────────────────────


class TestBackupManagerInit:
    """Constructor validation."""

    def test_crypto_without_passphrase_raises(self) -> None:
        pool = MagicMock()
        crypto = MagicMock()
        with pytest.raises(ValueError, match="passphrase is required"):
            BackupManager(pool, crypto=crypto)

    def test_crypto_with_empty_passphrase_raises(self) -> None:
        pool = MagicMock()
        crypto = MagicMock()
        with pytest.raises(ValueError, match="passphrase is required"):
            BackupManager(pool, crypto=crypto, passphrase="")

    def test_default_backup_dir(self) -> None:
        pool = MagicMock()
        mgr = BackupManager(pool)
        assert mgr._backup_dir == Path("~/.sovyx/backups").expanduser()

    def test_custom_backup_dir(self, tmp_path: Path) -> None:
        pool = MagicMock()
        mgr = BackupManager(pool, backup_dir=tmp_path / "custom")
        assert mgr._backup_dir == tmp_path / "custom"


# ── Integration tests: create + list + restore ──────────────────────


class TestCreateBackup:
    """BackupManager.create_backup end-to-end."""

    @pytest.mark.asyncio
    async def test_create_migration_backup(self, tmp_path: Path) -> None:
        db_path = tmp_path / "brain.db"
        _create_test_db(db_path)
        pool = _make_mock_pool(db_path)
        backup_dir = tmp_path / "backups"

        mgr = BackupManager(pool, backup_dir=backup_dir)
        info = await mgr.create_backup(BackupTrigger.MIGRATION)

        assert info.path.exists()
        assert info.trigger == "migration"
        assert info.size_bytes > 0
        assert info.encrypted is False
        assert "sovyx_migration_" in info.path.name

    @pytest.mark.asyncio
    async def test_create_daily_backup(self, tmp_path: Path) -> None:
        db_path = tmp_path / "brain.db"
        _create_test_db(db_path)
        pool = _make_mock_pool(db_path)
        backup_dir = tmp_path / "backups"

        mgr = BackupManager(pool, backup_dir=backup_dir)
        info = await mgr.create_backup(BackupTrigger.DAILY)

        assert info.trigger == "daily"
        assert info.path.suffix == ".db"

    @pytest.mark.asyncio
    async def test_create_manual_backup(self, tmp_path: Path) -> None:
        db_path = tmp_path / "brain.db"
        _create_test_db(db_path)
        pool = _make_mock_pool(db_path)
        backup_dir = tmp_path / "backups"

        mgr = BackupManager(pool, backup_dir=backup_dir)
        info = await mgr.create_backup(BackupTrigger.MANUAL)

        assert info.trigger == "manual"

    @pytest.mark.asyncio
    async def test_backup_is_valid_sqlite(self, tmp_path: Path) -> None:
        """Created backup must pass integrity check."""
        db_path = tmp_path / "brain.db"
        _create_test_db(db_path)
        pool = _make_mock_pool(db_path)
        backup_dir = tmp_path / "backups"

        mgr = BackupManager(pool, backup_dir=backup_dir)
        info = await mgr.create_backup(BackupTrigger.MIGRATION)

        # Verify backup is a valid SQLite file with data
        conn = sqlite3.connect(str(info.path))
        result = conn.execute("PRAGMA integrity_check").fetchone()
        assert result is not None
        assert result[0] == "ok"
        rows = conn.execute("SELECT * FROM test").fetchall()
        assert len(rows) == 1
        assert rows[0] == (1, "hello")
        conn.close()

    @pytest.mark.asyncio
    async def test_creates_backup_dir(self, tmp_path: Path) -> None:
        """Backup dir is created automatically if missing."""
        db_path = tmp_path / "brain.db"
        _create_test_db(db_path)
        pool = _make_mock_pool(db_path)
        backup_dir = tmp_path / "deep" / "nested" / "backups"

        mgr = BackupManager(pool, backup_dir=backup_dir)
        await mgr.create_backup(BackupTrigger.MANUAL)

        assert backup_dir.exists()

    @pytest.mark.asyncio
    async def test_vacuum_failure_cleans_up(self, tmp_path: Path) -> None:
        """Partial file is removed on VACUUM failure."""
        pool = MagicMock()

        @asynccontextmanager
        async def bad_write() -> AsyncGenerator[AsyncMock, None]:
            conn = AsyncMock()
            conn.execute = AsyncMock(side_effect=RuntimeError("disk full"))
            yield conn

        pool.write = bad_write
        backup_dir = tmp_path / "backups"

        mgr = BackupManager(pool, backup_dir=backup_dir)
        with pytest.raises(BackupError, match="disk full"):
            await mgr.create_backup(BackupTrigger.MIGRATION)

        # No leftover files
        if backup_dir.exists():
            assert list(backup_dir.iterdir()) == []


class TestListBackups:
    """BackupManager.list_backups."""

    @pytest.mark.asyncio
    async def test_empty_dir(self, tmp_path: Path) -> None:
        pool = MagicMock()
        mgr = BackupManager(pool, backup_dir=tmp_path / "nonexistent")
        assert await mgr.list_backups() == []

    @pytest.mark.asyncio
    async def test_list_all(self, tmp_path: Path) -> None:
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        (backup_dir / "sovyx_migration_20260401_100000.db").write_bytes(b"x")
        (backup_dir / "sovyx_daily_20260402_100000.db").write_bytes(b"y")
        (backup_dir / "sovyx_manual_20260403_100000.db").write_bytes(b"z")
        (backup_dir / "unrelated.txt").write_bytes(b"ignore")

        pool = MagicMock()
        mgr = BackupManager(pool, backup_dir=backup_dir)
        backups = await mgr.list_backups()

        assert len(backups) == 3
        triggers = {b.trigger for b in backups}
        assert triggers == {"migration", "daily", "manual"}

    @pytest.mark.asyncio
    async def test_filter_by_trigger(self, tmp_path: Path) -> None:
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        (backup_dir / "sovyx_migration_20260401_100000.db").write_bytes(b"x")
        (backup_dir / "sovyx_daily_20260402_100000.db").write_bytes(b"y")

        pool = MagicMock()
        mgr = BackupManager(pool, backup_dir=backup_dir)
        backups = await mgr.list_backups(trigger="migration")

        assert len(backups) == 1
        assert backups[0].trigger == "migration"

    @pytest.mark.asyncio
    async def test_sorted_newest_first(self, tmp_path: Path) -> None:
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        old = backup_dir / "sovyx_migration_20260401_100000.db"
        old.write_bytes(b"x")
        time.sleep(0.05)
        new = backup_dir / "sovyx_migration_20260402_100000.db"
        new.write_bytes(b"y")

        pool = MagicMock()
        mgr = BackupManager(pool, backup_dir=backup_dir)
        backups = await mgr.list_backups()

        assert backups[0].path.name == new.name

    @pytest.mark.asyncio
    async def test_includes_encrypted(self, tmp_path: Path) -> None:
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        (backup_dir / "sovyx_migration_20260401_100000.db.enc").write_bytes(b"x")

        pool = MagicMock()
        mgr = BackupManager(pool, backup_dir=backup_dir)
        backups = await mgr.list_backups()

        assert len(backups) == 1
        assert backups[0].encrypted is True


class TestRestoreBackup:
    """BackupManager.restore_backup."""

    @pytest.mark.asyncio
    async def test_restore_replaces_db(self, tmp_path: Path) -> None:
        db_path = tmp_path / "brain.db"
        _create_test_db(db_path)

        # Create a different backup DB
        backup_path = tmp_path / "sovyx_migration_20260401_100000.db"
        backup_conn = sqlite3.connect(str(backup_path))
        backup_conn.execute("CREATE TABLE restored (id INTEGER PRIMARY KEY)")
        backup_conn.execute("INSERT INTO restored VALUES (42)")
        backup_conn.commit()
        backup_conn.close()

        pool = _make_mock_pool(db_path)
        mgr = BackupManager(pool, backup_dir=tmp_path)
        await mgr.restore_backup(backup_path)

        # Verify the DB was replaced
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute("SELECT * FROM restored").fetchall()
        assert rows == [(42,)]
        conn.close()

    @pytest.mark.asyncio
    async def test_restore_nonexistent_raises(self, tmp_path: Path) -> None:
        pool = MagicMock()
        mgr = BackupManager(pool, backup_dir=tmp_path)
        with pytest.raises(FileNotFoundError, match="Backup not found"):
            await mgr.restore_backup(tmp_path / "nope.db")

    @pytest.mark.asyncio
    async def test_restore_corrupt_raises(self, tmp_path: Path) -> None:
        corrupt = tmp_path / "sovyx_migration_20260401_100000.db"
        corrupt.write_bytes(b"not a database")

        db_path = tmp_path / "brain.db"
        _create_test_db(db_path)
        pool = _make_mock_pool(db_path)

        mgr = BackupManager(pool, backup_dir=tmp_path)
        with pytest.raises(BackupIntegrityError):
            await mgr.restore_backup(corrupt)

    @pytest.mark.asyncio
    async def test_restore_encrypted_without_crypto_raises(self, tmp_path: Path) -> None:
        enc = tmp_path / "sovyx_migration_20260401_100000.db.enc"
        enc.write_bytes(b"encrypted blob")

        pool = MagicMock()
        mgr = BackupManager(pool, backup_dir=tmp_path)
        with pytest.raises(BackupError, match="Cannot restore encrypted"):
            await mgr.restore_backup(enc)

    @pytest.mark.asyncio
    async def test_restore_encrypted_backup(self, tmp_path: Path) -> None:
        """Full cycle: encrypt backup → restore from encrypted."""
        db_path = tmp_path / "brain.db"
        _create_test_db(db_path)

        # Create a valid backup DB and encrypt it
        backup_db = tmp_path / "source.db"
        conn = sqlite3.connect(str(backup_db))
        conn.execute("CREATE TABLE enc_test (v TEXT)")
        conn.execute("INSERT INTO enc_test VALUES ('encrypted_data')")
        conn.commit()
        conn.close()
        raw = backup_db.read_bytes()

        crypto = MagicMock()
        # encrypt returns prefixed bytes, decrypt strips prefix
        crypto.encrypt = MagicMock(side_effect=lambda data, pw: b"ENC:" + data)
        crypto.decrypt = MagicMock(side_effect=lambda data, pw: data[4:])

        enc_path = tmp_path / "sovyx_migration_20260401_100000.db.enc"
        enc_path.write_bytes(b"ENC:" + raw)

        pool = _make_mock_pool(db_path)
        mgr = BackupManager(pool, backup_dir=tmp_path, crypto=crypto, passphrase="test")
        await mgr.restore_backup(enc_path)

        # Verify restore wrote the decrypted data
        result_conn = sqlite3.connect(str(db_path))
        rows = result_conn.execute("SELECT * FROM enc_test").fetchall()
        assert rows == [("encrypted_data",)]
        result_conn.close()


class TestRetention:
    """Retention enforcement: keep N newest, prune oldest."""

    @pytest.mark.asyncio
    async def test_migration_keeps_5(self, tmp_path: Path) -> None:
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        for i in range(8):
            f = backup_dir / f"sovyx_migration_202604{i + 1:02d}_100000.db"
            f.write_bytes(b"x" * (i + 1))
            time.sleep(0.02)

        pool = MagicMock()
        mgr = BackupManager(pool, backup_dir=backup_dir)
        deleted = await mgr._enforce_retention("migration")

        assert deleted == 3
        remaining = await mgr.list_backups(trigger="migration")
        assert len(remaining) == 5

    @pytest.mark.asyncio
    async def test_daily_keeps_7(self, tmp_path: Path) -> None:
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        for i in range(10):
            f = backup_dir / f"sovyx_daily_202604{i + 1:02d}_100000.db"
            f.write_bytes(b"x")
            time.sleep(0.02)

        pool = MagicMock()
        mgr = BackupManager(pool, backup_dir=backup_dir)
        deleted = await mgr._enforce_retention("daily")

        assert deleted == 3
        remaining = await mgr.list_backups(trigger="daily")
        assert len(remaining) == 7

    @pytest.mark.asyncio
    async def test_manual_keeps_3(self, tmp_path: Path) -> None:
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        for i in range(6):
            f = backup_dir / f"sovyx_manual_202604{i + 1:02d}_100000.db"
            f.write_bytes(b"x")
            time.sleep(0.02)

        pool = MagicMock()
        mgr = BackupManager(pool, backup_dir=backup_dir)
        deleted = await mgr._enforce_retention("manual")

        assert deleted == 3
        remaining = await mgr.list_backups(trigger="manual")
        assert len(remaining) == 3

    @pytest.mark.asyncio
    async def test_under_limit_no_prune(self, tmp_path: Path) -> None:
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        for i in range(3):
            f = backup_dir / f"sovyx_migration_202604{i + 1:02d}_100000.db"
            f.write_bytes(b"x")

        pool = MagicMock()
        mgr = BackupManager(pool, backup_dir=backup_dir)
        deleted = await mgr._enforce_retention("migration")

        assert deleted == 0

    @pytest.mark.asyncio
    async def test_prune_all_triggers(self, tmp_path: Path) -> None:
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        # 7 migration (keep 5), 9 daily (keep 7), 5 manual (keep 3)
        for i in range(7):
            f = backup_dir / f"sovyx_migration_2026040{i + 1}_100000.db"
            f.write_bytes(b"x")
            time.sleep(0.01)
        for i in range(9):
            f = backup_dir / f"sovyx_daily_2026041{i}_100000.db"
            f.write_bytes(b"x")
            time.sleep(0.01)
        for i in range(5):
            f = backup_dir / f"sovyx_manual_2026042{i}_100000.db"
            f.write_bytes(b"x")
            time.sleep(0.01)

        pool = MagicMock()
        mgr = BackupManager(pool, backup_dir=backup_dir)
        total = await mgr.prune()

        assert total == (7 - 5) + (9 - 7) + (5 - 3)  # 2 + 2 + 2 = 6

    @pytest.mark.asyncio
    async def test_retention_after_create(self, tmp_path: Path) -> None:
        """create_backup auto-prunes old backups."""
        db_path = tmp_path / "brain.db"
        _create_test_db(db_path)
        pool = _make_mock_pool(db_path)
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()

        # Pre-populate with 5 manual backups
        for i in range(5):
            f = backup_dir / f"sovyx_manual_202604{i + 1:02d}_100000.db"
            f.write_bytes(b"x")
            time.sleep(0.02)

        mgr = BackupManager(pool, backup_dir=backup_dir)
        # Creating one more should trigger retention (keep 3)
        await mgr.create_backup(BackupTrigger.MANUAL)

        remaining = await mgr.list_backups(trigger="manual")
        assert len(remaining) <= 3


class TestEncryptedCreate:
    """Create backup with encryption enabled."""

    @pytest.mark.asyncio
    async def test_encrypted_backup_created(self, tmp_path: Path) -> None:
        db_path = tmp_path / "brain.db"
        _create_test_db(db_path)
        pool = _make_mock_pool(db_path)
        backup_dir = tmp_path / "backups"

        crypto = MagicMock()
        crypto.encrypt = MagicMock(side_effect=lambda data, pw: b"ENC:" + data)

        mgr = BackupManager(pool, backup_dir=backup_dir, crypto=crypto, passphrase="secret")
        info = await mgr.create_backup(BackupTrigger.MIGRATION)

        assert info.encrypted is True
        assert info.path.name.endswith(".db.enc")
        # Plain .db should not exist
        plain = info.path.with_name(info.path.name.replace(".db.enc", ".db"))
        assert not plain.exists()

    @pytest.mark.asyncio
    async def test_encrypt_failure_cleans_up(self, tmp_path: Path) -> None:
        db_path = tmp_path / "brain.db"
        _create_test_db(db_path)
        pool = _make_mock_pool(db_path)
        backup_dir = tmp_path / "backups"

        crypto = MagicMock()
        crypto.encrypt = MagicMock(side_effect=RuntimeError("encrypt failed"))

        mgr = BackupManager(pool, backup_dir=backup_dir, crypto=crypto, passphrase="secret")
        with pytest.raises(BackupError, match="encrypt"):
            await mgr.create_backup(BackupTrigger.MIGRATION)

        # No leftover .enc files
        if backup_dir.exists():
            enc_files = [f for f in backup_dir.iterdir() if f.name.endswith(".enc")]
            assert enc_files == []


class TestEdgeCases:
    """Cover remaining edge cases for full coverage."""

    @pytest.mark.asyncio
    async def test_restore_copy_failure(self, tmp_path: Path) -> None:
        """Restore fails when shutil.copy2 raises."""
        db_path = tmp_path / "brain.db"
        _create_test_db(db_path)

        backup_path = tmp_path / "sovyx_migration_20260401_100000.db"
        _create_test_db(backup_path)

        pool = _make_mock_pool(db_path)
        mgr = BackupManager(pool, backup_dir=tmp_path)

        with (
            patch("sovyx.upgrade.backup_manager.shutil.copy2", side_effect=OSError("denied")),
            pytest.raises(BackupError, match="Failed to restore"),
        ):
            await mgr.restore_backup(backup_path)

    @pytest.mark.asyncio
    async def test_retention_delete_failure(self, tmp_path: Path) -> None:
        """OSError during unlink is logged, not raised."""
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        for i in range(8):
            f = backup_dir / f"sovyx_migration_202604{i + 1:02d}_100000.db"
            f.write_bytes(b"x")
            time.sleep(0.02)

        pool = MagicMock()
        mgr = BackupManager(pool, backup_dir=backup_dir)

        def failing_unlink(self_path: Path, *args: object, **kwargs: object) -> None:
            raise OSError("read-only filesystem")

        with patch.object(Path, "unlink", failing_unlink):
            deleted = await mgr._enforce_retention("migration")

        assert deleted == 0

    @pytest.mark.asyncio
    async def test_get_db_path_not_found(self, tmp_path: Path) -> None:
        """Raise BackupError when PRAGMA database_list has no main."""
        pool = MagicMock()

        @asynccontextmanager
        async def read_ctx() -> AsyncGenerator[AsyncMock, None]:
            conn = AsyncMock()
            result = AsyncMock()
            result.fetchall = AsyncMock(return_value=[(0, "temp", "/tmp/temp.db")])
            conn.execute = AsyncMock(return_value=result)
            yield conn

        pool.read = read_ctx
        mgr = BackupManager(pool, backup_dir=tmp_path)
        with pytest.raises(BackupError, match="Could not determine database path"):
            await mgr._get_db_path()

    def test_verify_integrity_failed_check(self, tmp_path: Path) -> None:
        """Integrity check returns non-ok result."""
        db = tmp_path / "bad.db"
        _create_test_db(db)

        with patch("sovyx.upgrade.backup_manager.sqlite3.connect") as mock_connect:
            mock_conn = MagicMock()
            mock_conn.execute.return_value.fetchone.return_value = ("malformed page",)
            mock_connect.return_value = mock_conn
            with pytest.raises(BackupIntegrityError, match="integrity check failed"):
                _verify_integrity(db)

    def test_verify_integrity_none_result(self, tmp_path: Path) -> None:
        """Integrity check returns None."""
        db = tmp_path / "bad2.db"
        _create_test_db(db)

        with patch("sovyx.upgrade.backup_manager.sqlite3.connect") as mock_connect:
            mock_conn = MagicMock()
            mock_conn.execute.return_value.fetchone.return_value = None
            mock_connect.return_value = mock_conn
            with pytest.raises(BackupIntegrityError, match="integrity check failed"):
                _verify_integrity(db)

    @pytest.mark.asyncio
    async def test_list_skips_non_db_files(self, tmp_path: Path) -> None:
        """Files without .db or .db.enc suffix are ignored."""
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        (backup_dir / "sovyx_migration_20260401_100000.db").write_bytes(b"x")
        (backup_dir / "sovyx_migration_20260402_100000.log").write_bytes(b"x")
        (backup_dir / "readme.txt").write_bytes(b"x")

        pool = MagicMock()
        mgr = BackupManager(pool, backup_dir=backup_dir)
        backups = await mgr.list_backups()

        assert len(backups) == 1


class TestBackupTriggerEnum:
    """BackupTrigger enum values."""

    def test_values(self) -> None:
        assert BackupTrigger.MIGRATION.value == "migration"
        assert BackupTrigger.DAILY.value == "daily"
        assert BackupTrigger.MANUAL.value == "manual"

    def test_all_triggers_have_retention(self) -> None:
        from sovyx.upgrade.backup_manager import _RETENTION_LIMITS

        for t in BackupTrigger:
            assert t.value in _RETENTION_LIMITS
