"""Sovyx migration framework.

Forward-only, idempotent, checksummed schema migrations for SQLite.
Each migration runs in a transaction with integrity verification.
"""

from __future__ import annotations

import dataclasses
import hashlib
import time
from typing import TYPE_CHECKING

from sovyx.engine.errors import MigrationError
from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sovyx.persistence.pool import DatabasePool

logger = get_logger(__name__)


@dataclasses.dataclass(frozen=True)
class Migration:
    """A single schema migration.

    Attributes:
        version: Migration version number (sequential, 1-based).
        description: Human-readable description.
        sql_up: SQL to apply (forward-only, no down).
        checksum: SHA-256 hex digest of sql_up for tamper detection.
    """

    version: int
    description: str
    sql_up: str
    checksum: str

    @staticmethod
    def compute_checksum(sql: str) -> str:
        """Compute SHA-256 checksum for migration SQL.

        Args:
            sql: The SQL text to hash.

        Returns:
            Hex digest of the SHA-256 hash.
        """
        return hashlib.sha256(sql.encode()).hexdigest()


_SCHEMA_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS _schema (
    version INTEGER PRIMARY KEY,
    description TEXT NOT NULL,
    checksum TEXT NOT NULL,
    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    duration_ms INTEGER
);
"""


class MigrationRunner:
    """Execute schema migrations with integrity verification.

    Characteristics:
        - Forward-only (no down migrations)
        - Idempotent: running twice applies nothing new
        - Checksum: detects tampered migrations
        - Transactional: each migration in its own transaction
    """

    def __init__(self, pool: DatabasePool) -> None:
        self._pool = pool

    async def initialize(self) -> None:
        """Create the _schema tracking table if it doesn't exist."""
        async with self._pool.write() as conn:
            await conn.executescript(_SCHEMA_TABLE_SQL)

    async def get_current_version(self) -> int:
        """Return the current schema version (0 if empty).

        Returns:
            The highest applied migration version, or 0.
        """
        async with self._pool.read() as conn:
            cursor = await conn.execute("SELECT MAX(version) FROM _schema")
            row = await cursor.fetchone()
            if row is None or row[0] is None:
                return 0
            return int(row[0])

    async def run_migrations(self, migrations: Sequence[Migration]) -> int:
        """Apply pending migrations in version order.

        Args:
            migrations: All known migrations (applied + pending).

        Returns:
            Number of migrations applied.

        Raises:
            MigrationError: If a previously applied migration has a
                mismatched checksum, or if a migration SQL fails.
        """
        current = await self.get_current_version()
        applied = 0

        # Sort by version to ensure correct order
        sorted_migrations = sorted(migrations, key=lambda m: m.version)

        for migration in sorted_migrations:
            if migration.version <= current:
                # Verify integrity of already-applied migrations
                await self._verify_applied(migration)
                continue

            await self._apply(migration)
            applied += 1

        if applied > 0:
            logger.info(
                "migrations_applied",
                count=applied,
                new_version=sorted_migrations[-1].version if sorted_migrations else 0,
            )

        return applied

    async def verify_integrity(self, migrations: Sequence[Migration]) -> bool:
        """Verify all applied migrations have correct checksums.

        Args:
            migrations: All known migrations.

        Returns:
            True if all checksums match.
        """
        for migration in migrations:
            current = await self.get_current_version()
            if migration.version > current:
                continue
            try:
                await self._verify_applied(migration)
            except MigrationError:
                return False
        return True

    async def _verify_applied(self, migration: Migration) -> None:
        """Check that an applied migration's checksum matches.

        Raises:
            MigrationError: If the stored checksum doesn't match.
        """
        async with self._pool.read() as conn:
            cursor = await conn.execute(
                "SELECT checksum FROM _schema WHERE version = ?",
                (migration.version,),
            )
            row = await cursor.fetchone()

        if row is None:
            return  # Not yet applied, nothing to verify

        stored_checksum = row[0]
        if stored_checksum != migration.checksum:
            msg = (
                f"Migration v{migration.version} ({migration.description}) "
                f"checksum mismatch: expected {migration.checksum}, "
                f"got {stored_checksum}"
            )
            raise MigrationError(msg)

    async def _apply(self, migration: Migration) -> None:
        """Apply a single migration in a transaction.

        Raises:
            MigrationError: If the SQL execution fails.
        """
        start = time.monotonic()
        logger.info(
            "migration_applying",
            version=migration.version,
            description=migration.description,
        )

        try:
            async with self._pool.transaction() as conn:
                await conn.executescript(migration.sql_up)
                duration_ms = int((time.monotonic() - start) * 1000)
                await conn.execute(
                    "INSERT INTO _schema (version, description, checksum, duration_ms) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        migration.version,
                        migration.description,
                        migration.checksum,
                        duration_ms,
                    ),
                )
        except MigrationError:
            raise
        except Exception as exc:
            msg = f"Migration v{migration.version} ({migration.description}) failed: {exc}"
            raise MigrationError(msg) from exc

        logger.info(
            "migration_applied",
            version=migration.version,
            description=migration.description,
            duration_ms=duration_ms,
        )
