"""Schema version tracking and upgrade migration runner.

Provides :class:`SchemaVersion` for semver-based application version
tracking and :class:`MigrationRunner` for orchestrating ordered,
atomic, rollback-safe upgrade migrations with expand-contract support.

Ref: SPE-028 §2-3 — schema versioning, migration runner.
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import importlib
import re
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sovyx.engine.errors import MigrationError
from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from sovyx.persistence.pool import DatabasePool

logger = get_logger(__name__)

# ── SQL ─────────────────────────────────────────────────────────────

_VERSION_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS _schema_version (
    version TEXT NOT NULL,
    applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    checksum TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    duration_ms INTEGER
);
"""

# ── Data classes ────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True, order=True)
class SemVer:
    """Semantic version (major.minor.patch).

    Ordering uses the default dataclass tuple comparison which
    compares fields in declaration order (major → minor → patch).
    """

    major: int
    minor: int
    patch: int

    _PATTERN: str = dataclasses.field(
        default=r"^(\d+)\.(\d+)\.(\d+)$",
        init=False,
        repr=False,
        compare=False,
    )

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"

    @classmethod
    def parse(cls, version_str: str) -> SemVer:
        """Parse a ``MAJOR.MINOR.PATCH`` string.

        Args:
            version_str: Version string like ``"1.2.3"``.

        Returns:
            Parsed :class:`SemVer` instance.

        Raises:
            ValueError: If *version_str* is not valid semver.
        """
        match = re.match(cls._PATTERN, version_str.strip())
        if not match:
            msg = f"Invalid semver string: {version_str!r}"
            raise ValueError(msg)
        return cls(
            major=int(match.group(1)),
            minor=int(match.group(2)),
            patch=int(match.group(3)),
        )

    @classmethod
    def zero(cls) -> SemVer:
        """Return ``0.0.0`` (fresh database)."""
        return cls(0, 0, 0)


@dataclasses.dataclass(frozen=True)
class UpgradeMigration:
    """A single upgrade migration.

    Attributes:
        version: Target semver after this migration is applied.
        description: Human-readable summary.
        sql_statements: SQL statements to execute in order.
        data_migration: Optional async callable for data transforms.
        phase: ``"expand"`` or ``"contract"`` (expand-contract pattern).
        checksum: SHA-256 of all SQL statements concatenated.
    """

    version: str
    description: str
    sql_statements: list[str] = dataclasses.field(default_factory=list)
    data_migration: Callable[[Any], Awaitable[None]] | None = None
    phase: str = "expand"
    checksum: str = ""

    def __post_init__(self) -> None:
        """Compute checksum from SQL statements if not set."""
        if not self.checksum:
            combined = "\n".join(self.sql_statements)
            digest = hashlib.sha256(combined.encode()).hexdigest()
            object.__setattr__(self, "checksum", digest)

    @property
    def semver(self) -> SemVer:
        """Parse *version* string as :class:`SemVer`."""
        return SemVer.parse(self.version)


@dataclasses.dataclass
class MigrationReport:
    """Report of a migration run.

    Attributes:
        status: ``"up_to_date"``, ``"success"``, ``"failed"``.
        applied: List of human-readable descriptions of applied migrations.
        error: Error message if status is ``"failed"``.
    """

    status: str = "up_to_date"
    applied: list[str] = dataclasses.field(default_factory=list)
    error: str = ""


# ── SchemaVersion ───────────────────────────────────────────────────


class SchemaVersion:
    """Track the current application schema version (semver).

    Reads from / writes to the ``_schema_version`` table.  At startup
    the table is created if it does not exist.

    Args:
        pool: Database pool to use for queries.
    """

    def __init__(self, pool: DatabasePool) -> None:
        self._pool = pool

    async def initialize(self) -> None:
        """Create the ``_schema_version`` table if absent."""
        async with self._pool.write() as conn:
            await conn.executescript(_VERSION_TABLE_SQL)

    async def get_current(self) -> SemVer:
        """Return the highest applied version, or ``0.0.0``.

        Returns:
            The current :class:`SemVer`.
        """
        async with self._pool.read() as conn:
            cursor = await conn.execute(
                "SELECT version FROM _schema_version ORDER BY rowid DESC LIMIT 1"
            )
            row = await cursor.fetchone()
        if row is None:
            return SemVer.zero()
        try:
            return SemVer.parse(row[0])
        except ValueError:
            return SemVer.zero()

    def get_pending(
        self,
        current: SemVer,
        migrations: Sequence[UpgradeMigration],
    ) -> list[UpgradeMigration]:
        """Return migrations whose version exceeds *current*, sorted ascending.

        Args:
            current: Currently applied version.
            migrations: All known migrations.

        Returns:
            Sorted list of pending migrations.
        """
        pending = [m for m in migrations if m.semver > current]
        return sorted(pending, key=lambda m: m.semver)

    async def record(
        self,
        migration: UpgradeMigration,
        duration_ms: int,
    ) -> None:
        """Record a successfully applied migration.

        Args:
            migration: The migration that was applied.
            duration_ms: Elapsed wall-clock milliseconds.
        """
        async with self._pool.write() as conn:
            await conn.execute(
                "INSERT INTO _schema_version "
                "(version, checksum, description, duration_ms) "
                "VALUES (?, ?, ?, ?)",
                (
                    migration.version,
                    migration.checksum,
                    migration.description,
                    duration_ms,
                ),
            )
            await conn.commit()

    async def get_history(self) -> list[dict[str, Any]]:
        """Return full migration history, oldest first.

        Returns:
            List of dicts with ``version``, ``applied_at``,
            ``checksum``, ``description``, ``duration_ms``.
        """
        async with self._pool.read() as conn:
            cursor = await conn.execute(
                "SELECT version, applied_at, checksum, description, duration_ms "
                "FROM _schema_version ORDER BY rowid"
            )
            rows = await cursor.fetchall()
        return [
            {
                "version": r[0],
                "applied_at": r[1],
                "checksum": r[2],
                "description": r[3],
                "duration_ms": r[4],
            }
            for r in rows
        ]


# ── MigrationRunner ────────────────────────────────────────────────


class MigrationRunner:
    """Execute upgrade migrations with backup and rollback.

    Pipeline (SPE-028 §3.2):
        1. Check pending migrations
        2. Backup database (``VACUUM INTO``)
        3. For each migration (in version order):
           a. Begin transaction
           b. Execute SQL statements
           c. Run optional data migration
           d. Record in ``_schema_version``
           e. Commit
        4. Verify integrity (``PRAGMA integrity_check``)
        5. On failure: restore from backup

    Args:
        pool: Database pool to operate on.
        schema_version: :class:`SchemaVersion` tracker.
        backup_dir: Directory for pre-migration backups.
    """

    def __init__(
        self,
        pool: DatabasePool,
        schema_version: SchemaVersion,
        backup_dir: Path | None = None,
    ) -> None:
        self._pool = pool
        self._version = schema_version
        self._backup_dir = backup_dir or Path.home() / ".sovyx" / "backups"

    # ── public API ──────────────────────────────────────────────

    async def run(
        self,
        migrations: Sequence[UpgradeMigration],
    ) -> MigrationReport:
        """Apply all pending migrations.

        Args:
            migrations: All known upgrade migrations.

        Returns:
            :class:`MigrationReport` describing what happened.
        """
        current = await self._version.get_current()
        pending = self._version.get_pending(current, migrations)

        if not pending:
            return MigrationReport(status="up_to_date")

        report = MigrationReport(status="migrating")

        # 1. Backup
        backup_path = await self._backup()
        logger.info("pre_migration_backup", path=str(backup_path))

        try:
            for migration in pending:
                await self._apply_one(migration)
                report.applied.append(f"v{migration.version}: {migration.description}")

            # 2. Integrity check
            await self._verify_integrity()
            report.status = "success"

        except Exception as exc:
            logger.error(
                "migration_failed_restoring_backup",
                error=str(exc),
            )
            await self._restore(backup_path)
            report.status = "failed"
            report.error = str(exc)

        return report

    async def verify_applied(
        self,
        migrations: Sequence[UpgradeMigration],
    ) -> bool:
        """Check that all applied migrations have correct checksums.

        Args:
            migrations: All known migrations.

        Returns:
            ``True`` if every applied migration's checksum matches.
        """
        history = await self._version.get_history()
        checksum_map = {h["version"]: h["checksum"] for h in history}
        for migration in migrations:
            stored = checksum_map.get(migration.version)
            if stored is not None and stored != migration.checksum:
                logger.warning(
                    "checksum_mismatch",
                    version=migration.version,
                    expected=migration.checksum,
                    stored=stored,
                )
                return False
        return True

    # ── discovery ───────────────────────────────────────────────

    @staticmethod
    def discover_migrations(
        package: str = "sovyx.upgrade.migrations",
    ) -> list[UpgradeMigration]:
        """Auto-discover migration files by numeric prefix.

        Scans the package directory for ``[0-9]*.py`` files, imports
        each one, and collects the module-level ``MIGRATION`` attribute.

        Args:
            package: Dotted package name to scan.

        Returns:
            Sorted list of discovered :class:`UpgradeMigration` objects.
        """
        pkg = importlib.import_module(package)
        pkg_dir = Path(pkg.__file__).parent if pkg.__file__ else None
        if pkg_dir is None:
            return []

        migrations: list[UpgradeMigration] = []
        for py_file in sorted(pkg_dir.glob("[0-9]*.py")):
            module_name = f"{package}.{py_file.stem}"
            try:
                mod = importlib.import_module(module_name)
            except Exception:
                logger.warning("migration_import_failed", module=module_name, exc_info=True)
                continue
            migration = getattr(mod, "MIGRATION", None)
            if isinstance(migration, UpgradeMigration):
                migrations.append(migration)

        return sorted(migrations, key=lambda m: m.semver)

    # ── internals ───────────────────────────────────────────────

    async def _apply_one(self, migration: UpgradeMigration) -> None:
        """Apply a single migration inside a transaction."""
        start = time.monotonic()
        logger.info(
            "upgrade_migration_applying",
            version=migration.version,
            description=migration.description,
            phase=migration.phase,
        )

        try:
            async with self._pool.transaction() as conn:
                for sql in migration.sql_statements:
                    await conn.execute(sql)

                if migration.data_migration is not None:
                    await migration.data_migration(conn)

        except MigrationError:
            raise
        except Exception as exc:
            msg = f"Upgrade migration v{migration.version} ({migration.description}) failed: {exc}"
            raise MigrationError(msg) from exc

        duration_ms = int((time.monotonic() - start) * 1000)
        await self._version.record(migration, duration_ms)

        logger.info(
            "upgrade_migration_applied",
            version=migration.version,
            duration_ms=duration_ms,
        )

    async def _backup(self) -> Path:
        """Create a database backup via ``VACUUM INTO``.

        Returns:
            Path to the backup file.

        Raises:
            MigrationError: If the backup cannot be created.
        """
        self._backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.datetime.now(tz=datetime.UTC).strftime("%Y%m%d_%H%M%S")
        # Append monotonic counter to avoid collisions within the same second
        suffix = int(time.monotonic() * 1_000_000) % 1_000_000
        backup_path = self._backup_dir / f"sovyx_upgrade_{timestamp}_{suffix:06d}.db"

        try:
            async with self._pool.write() as conn:
                await conn.execute(f"VACUUM INTO '{backup_path}'")
        except Exception as exc:
            msg = f"Pre-migration backup failed: {exc}"
            raise MigrationError(msg) from exc

        return backup_path

    async def _restore(self, backup_path: Path) -> None:
        """Restore database from *backup_path*.

        Closes the pool, copies the backup over the original, then
        re-initializes.
        """
        db_path = self._pool._db_path  # noqa: SLF001
        await self._pool.close()
        shutil.copy2(backup_path, db_path)
        await self._pool.initialize()
        logger.info("database_restored_from_backup", path=str(backup_path))

    async def _verify_integrity(self) -> None:
        """Run ``PRAGMA integrity_check`` after migrations.

        Raises:
            MigrationError: If the integrity check fails.
        """
        async with self._pool.read() as conn:
            cursor = await conn.execute("PRAGMA integrity_check")
            row = await cursor.fetchone()
        if row is None or row[0] != "ok":
            result = row[0] if row else "no result"
            msg = f"Integrity check failed after migration: {result}"
            raise MigrationError(msg)
