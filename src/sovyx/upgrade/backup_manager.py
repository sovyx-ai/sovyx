"""Backup manager — pre-upgrade snapshots with retention and optional encryption.

Provides :class:`BackupManager` for creating, listing, restoring, and pruning
local database backups. Each backup is a SQLite ``VACUUM INTO`` snapshot named
with trigger type and timestamp.  Optional at-rest encryption uses the
an optional crypto provider (Argon2id + AES-256-GCM, provided by sovyx-cloud).

Retention policy (per trigger type)::

    migration  → keep last 5
    daily      → keep last 7
    manual     → keep last 3

Ref: SPE-028 §6-7 — backup management, retention.
"""

from __future__ import annotations

import dataclasses
import shutil
import sqlite3
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from sovyx.engine.errors import PersistenceError
from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from sovyx.persistence.pool import DatabasePool

logger = get_logger(__name__)


# ── Constants ───────────────────────────────────────────────────────

DEFAULT_BACKUP_DIR = Path("~/.sovyx/backups")

_RETENTION_LIMITS: dict[str, int] = {
    "migration": 5,
    "daily": 7,
    "manual": 3,
}

# ── Enums & Data classes ────────────────────────────────────────────


class BackupTrigger(StrEnum):
    """Why this backup was created."""

    MIGRATION = "migration"
    DAILY = "daily"
    MANUAL = "manual"


@dataclasses.dataclass(frozen=True, slots=True)
class BackupInfo:
    """Metadata for a single backup file.

    Attributes:
        path: Absolute path to the ``.db`` (or ``.db.enc``) file.
        size_bytes: File size in bytes.
        created_at: Filesystem mtime as a UTC datetime.
        trigger: The trigger category extracted from the filename.
        encrypted: Whether the backup is encrypted (``.db.enc`` suffix).
    """

    path: Path
    size_bytes: int
    created_at: datetime
    trigger: str
    encrypted: bool = False


class BackupError(PersistenceError):
    """Raised when a backup operation fails."""


class BackupIntegrityError(BackupError):
    """Raised when a backup file fails integrity verification."""


# ── Main class ──────────────────────────────────────────────────────


class BackupManager:
    """Manage local database backups with retention and optional encryption.

    Args:
        db: Async database pool for executing ``VACUUM INTO``.
        backup_dir: Directory where backups are stored.  Defaults to
            ``~/.sovyx/backups``.
        crypto: Optional :class:`object` instance for at-rest encryption.
        passphrase: Passphrase for encryption (required when *crypto* is set).
    """

    def __init__(
        self,
        db: DatabasePool,
        *,
        backup_dir: Path | None = None,
        crypto: object | None = None,
        passphrase: str | None = None,
    ) -> None:
        self._db = db
        self._backup_dir = (backup_dir or DEFAULT_BACKUP_DIR).expanduser()
        self._crypto = crypto
        self._passphrase = passphrase

        if crypto is not None and not passphrase:
            msg = "passphrase is required when crypto is provided"
            raise ValueError(msg)

    # ── Public API ──────────────────────────────────────────────────

    async def create_backup(self, trigger: BackupTrigger) -> BackupInfo:
        """Create a database snapshot and enforce retention.

        Args:
            trigger: Why this backup was created.

        Returns:
            :class:`BackupInfo` for the newly created backup.

        Raises:
            BackupError: If the snapshot or write fails.
        """
        self._backup_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        backup_path = self._backup_dir / f"sovyx_{trigger.value}_{timestamp}.db"

        try:
            async with self._db.write() as conn:
                await conn.execute(f"VACUUM INTO '{backup_path}'")
        except Exception as exc:
            # Clean up partial file
            backup_path.unlink(missing_ok=True)
            msg = f"Failed to create backup: {exc}"
            raise BackupError(msg) from exc

        # Optional encryption
        if self._crypto is not None and self._passphrase:
            encrypted_path = backup_path.with_suffix(".db.enc")
            try:
                raw = backup_path.read_bytes()
                encrypted = self._crypto.encrypt(raw, self._passphrase)  # type: ignore[attr-defined]
                encrypted_path.write_bytes(encrypted)
                backup_path.unlink()
                backup_path = encrypted_path
            except Exception as exc:
                encrypted_path.unlink(missing_ok=True)
                msg = f"Failed to encrypt backup: {exc}"
                raise BackupError(msg) from exc

        # Enforce retention
        await self._enforce_retention(trigger.value)

        info = _build_backup_info(backup_path)
        logger.info(
            "Backup created",
            path=str(backup_path),
            trigger=trigger.value,
            size=info.size_bytes,
        )
        return info

    async def list_backups(self, trigger: str | None = None) -> list[BackupInfo]:
        """List available backups, newest first.

        Args:
            trigger: Optional filter by trigger type.

        Returns:
            List of :class:`BackupInfo` sorted by creation time descending.
        """
        if not self._backup_dir.exists():
            return []

        backups: list[BackupInfo] = []
        for path in self._backup_dir.iterdir():
            if not path.name.startswith("sovyx_"):
                continue
            if not (path.suffix == ".db" or path.name.endswith(".db.enc")):
                continue
            info = _build_backup_info(path)
            if trigger is not None and info.trigger != trigger:
                continue
            backups.append(info)

        return sorted(backups, key=lambda b: b.created_at, reverse=True)

    async def restore_backup(self, backup_path: Path) -> None:
        """Restore the database from a backup file.

        Verifies integrity before replacing the live database.

        Args:
            backup_path: Path to the backup file.

        Raises:
            FileNotFoundError: If *backup_path* does not exist.
            BackupIntegrityError: If the backup is corrupted.
            BackupError: If the restore operation fails.
        """
        if not backup_path.exists():
            msg = f"Backup not found: {backup_path}"
            raise FileNotFoundError(msg)

        # Decrypt if needed
        data: bytes | None = None
        if backup_path.name.endswith(".db.enc"):
            if self._crypto is None or not self._passphrase:
                msg = "Cannot restore encrypted backup without crypto and passphrase"
                raise BackupError(msg)
            raw = backup_path.read_bytes()
            data = self._crypto.decrypt(raw, self._passphrase)  # type: ignore[attr-defined]

        # Write to temp file for integrity check (if encrypted)
        check_path = backup_path
        if data is not None:
            check_path = backup_path.with_suffix(".db.tmp")
            check_path.write_bytes(data)

        try:
            _verify_integrity(check_path)
        finally:
            if data is not None:
                check_path.unlink(missing_ok=True)

        # Get live database path
        db_path = await self._get_db_path()

        # Replace live database
        try:
            if data is not None:
                db_path.write_bytes(data)
            else:
                shutil.copy2(backup_path, db_path)
        except Exception as exc:
            msg = f"Failed to restore backup: {exc}"
            raise BackupError(msg) from exc

        logger.info("Database restored from backup", path=str(backup_path))

    async def prune(self, trigger: str | None = None) -> int:
        """Manually prune backups beyond retention limits.

        Args:
            trigger: Only prune this trigger type.  If ``None``, prune all.

        Returns:
            Number of backups deleted.
        """
        triggers = [trigger] if trigger else list(_RETENTION_LIMITS.keys())
        total = 0
        for t in triggers:
            total += await self._enforce_retention(t)
        return total

    # ── Internal ────────────────────────────────────────────────────

    async def _enforce_retention(self, trigger: str) -> int:
        """Delete old backups beyond the retention limit for *trigger*.

        Returns:
            Number of backups deleted.
        """
        limit = _RETENTION_LIMITS.get(trigger, 5)
        backups = await self.list_backups(trigger=trigger)

        if len(backups) <= limit:
            return 0

        to_delete = backups[limit:]
        deleted = 0
        for info in to_delete:
            try:
                info.path.unlink()
                deleted += 1
                logger.debug("Pruned old backup", path=str(info.path), trigger=trigger)
            except OSError:
                logger.warning("Failed to delete backup", path=str(info.path))

        if deleted:
            logger.info(
                "Retention enforced",
                trigger=trigger,
                kept=limit,
                deleted=deleted,
            )
        return deleted

    async def _get_db_path(self) -> Path:
        """Resolve the live database file path."""
        async with self._db.read() as conn:
            rows = await conn.execute("PRAGMA database_list")
            result = await rows.fetchall()
            for row in result:
                if row[1] == "main":
                    return Path(row[2])
        msg = "Could not determine database path"
        raise BackupError(msg)


# ── Helpers ─────────────────────────────────────────────────────────


def _verify_integrity(path: Path) -> None:
    """Run ``PRAGMA integrity_check`` on a SQLite file.

    Raises:
        BackupIntegrityError: If the file is not a valid SQLite database
            or integrity check fails.
    """
    try:
        conn = sqlite3.connect(str(path))
        try:
            result = conn.execute("PRAGMA integrity_check").fetchone()
            if result is None or result[0] != "ok":
                detail = result[0] if result else "unknown"
                msg = f"Backup integrity check failed: {detail}"
                raise BackupIntegrityError(msg)
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        msg = f"Backup is not a valid SQLite database: {exc}"
        raise BackupIntegrityError(msg) from exc


def _build_backup_info(path: Path) -> BackupInfo:
    """Build a :class:`BackupInfo` from a backup file path."""
    stat = path.stat()
    stem = path.stem
    encrypted = path.name.endswith(".db.enc")

    # For .db.enc files, stem is "sovyx_trigger_timestamp.db"
    if encrypted:
        stem = Path(path.stem).stem  # strip .db from .db.enc

    # Parse trigger from filename: sovyx_{trigger}_{timestamp}
    parts = stem.split("_", 2)
    trigger = parts[1] if len(parts) >= 2 else "unknown"

    return BackupInfo(
        path=path,
        size_bytes=stat.st_size,
        created_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
        trigger=trigger,
        encrypted=encrypted,
    )
