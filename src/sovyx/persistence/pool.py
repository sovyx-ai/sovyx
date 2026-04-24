"""Sovyx database connection pool.

Async SQLite pool with write serialization (1 writer) and concurrent
reads (N readers) using WAL mode. Extensions (sqlite-vec) loaded on
all connections.
"""

from __future__ import annotations

import asyncio
import importlib
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import aiosqlite

from sovyx.engine.errors import DatabaseConnectionError
from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

logger = get_logger(__name__)

# ── Default pragmas (ADR-004 §2.3 — all 9 non-negotiable) ──────────────────

_DEFAULT_PRAGMAS: dict[str, str | int] = {
    "journal_mode": "WAL",
    "synchronous": "NORMAL",
    "temp_store": "MEMORY",
    "foreign_keys": "ON",
    "busy_timeout": 5000,
    "wal_autocheckpoint": 1000,
    "auto_vacuum": "INCREMENTAL",
}


class DatabasePool:
    """Pool of async SQLite connections.

    Architecture (SPE-005 §3):
        - 1 WRITE connection (WAL allows exactly 1 writer)
        - N READ connections (WAL allows unlimited concurrent readers)
        - All connections share the same pragmas
        - Write serialization via asyncio.Lock eliminates SQLITE_BUSY

    Args:
        db_path: Path to the SQLite database file.
        read_pool_size: Number of read connections.
        pragmas: Extra pragmas to apply (merged with defaults).
        load_extensions: Extension names to load (e.g., ["vec0"]).
    """

    def __init__(
        self,
        db_path: Path,
        read_pool_size: int = 3,
        pragmas: dict[str, str | int] | None = None,
        load_extensions: list[str] | None = None,
    ) -> None:
        self._db_path = db_path
        self._read_pool_size = read_pool_size
        self._pragmas = {**_DEFAULT_PRAGMAS, **(pragmas or {})}
        self._extension_names = load_extensions or []
        self._write_conn: aiosqlite.Connection | None = None
        self._read_conns: list[aiosqlite.Connection] = []
        self._write_lock = asyncio.Lock()
        # Guards the round-robin cursor below. Without it, concurrent
        # reads race on the read-then-bump and can hand the same
        # connection to multiple coroutines — correctness is fine under
        # WAL, but the load balance collapses.
        self._read_index_lock = asyncio.Lock()
        self._read_index = 0
        self._initialized = False
        self._has_sqlite_vec = False

    @property
    def db_path(self) -> Path:
        """Public accessor for the database file path."""
        return self._db_path

    async def initialize(self) -> None:
        """Open connections, apply pragmas, load extensions.

        Raises:
            DatabaseConnectionError: If the database cannot be opened.
        """
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            self._write_conn = await aiosqlite.connect(str(self._db_path))
            await self._setup_connection(self._write_conn)

            for _ in range(self._read_pool_size):
                conn = await aiosqlite.connect(str(self._db_path))
                await self._setup_connection(conn)
                self._read_conns.append(conn)

            self._initialized = True
            logger.info(
                "database_pool_initialized",
                db_path=str(self._db_path),
                read_pool_size=self._read_pool_size,
                has_sqlite_vec=self._has_sqlite_vec,
            )
        except Exception as exc:  # noqa: BLE001
            msg = f"Failed to initialize database pool: {exc}"
            raise DatabaseConnectionError(msg) from exc

    async def close(self) -> None:
        """Close all connections gracefully.

        Shutdown order: readers first → WAL checkpoint → writer last.
        This ensures no readers hold shared locks during checkpoint,
        and the WAL file is truncated before the writer closes.
        """
        # 1. Close readers first (release shared locks)
        for conn in self._read_conns:
            await conn.close()
        self._read_conns.clear()

        # 2. WAL checkpoint before closing writer
        if self._write_conn is not None:
            try:
                await self._write_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                logger.debug("wal_checkpoint_completed", db_path=str(self._db_path))
            except aiosqlite.Error:
                # Checkpoint is best-effort on shutdown — a failure
                # leaves the WAL file intact, which the next startup
                # will replay. Log so persistent checkpoint failures
                # (disk full, locked DB) don't silently accumulate WAL.
                logger.warning(
                    "wal_checkpoint_failed",
                    db_path=str(self._db_path),
                    exc_info=True,
                )

            # 3. Close writer last
            await self._write_conn.close()
            self._write_conn = None

        self._initialized = False
        logger.info("database_pool_closed", db_path=str(self._db_path))

    async def _setup_connection(self, conn: aiosqlite.Connection) -> None:
        """Apply pragmas and load extensions on a single connection."""
        for pragma, value in self._pragmas.items():
            await conn.execute(f"PRAGMA {pragma}={value}")

        if "cache_size" in self._pragmas:
            await conn.execute(f"PRAGMA cache_size={self._pragmas['cache_size']}")
        if "mmap_size" in self._pragmas:
            await conn.execute(f"PRAGMA mmap_size={self._pragmas['mmap_size']}")

        if self._extension_names:
            await self._load_extensions(conn)

    async def _load_extensions(self, conn: aiosqlite.Connection) -> None:
        """Load sqlite extensions (SPE-005 §3.2).

        For each extension name, finds the loadable path and loads it.
        If extension not found, logs warning but does not crash.
        """
        for name in self._extension_names:
            ext_path = self._find_extension_path(name)
            if ext_path is None:
                logger.warning(
                    "extension_not_found",
                    extension=name,
                )
                self._has_sqlite_vec = False
                continue

            try:
                await conn.enable_load_extension(True)
                await conn.load_extension(ext_path)
                await conn.enable_load_extension(False)
                if name == "vec0":
                    self._has_sqlite_vec = True
                logger.debug("extension_loaded", extension=name, path=ext_path)
            except (aiosqlite.Error, OSError):
                # aiosqlite.Error: sqlite rejected the extension (wrong
                # ABI, missing symbols, not built with enable_load_extension).
                # OSError: extension file on disk is unreadable or
                # missing (we found it but something changed between
                # discovery and load). Retrieval degrades to FTS5-only
                # — log with traceback for post-mortem.
                logger.warning(
                    "extension_load_failed",
                    extension=name,
                    path=ext_path,
                    exc_info=True,
                )
                if name == "vec0":
                    self._has_sqlite_vec = False

    @staticmethod
    def _find_extension_path(name: str) -> str | None:
        """Find the loadable path for an extension.

        Search order:
            1. sqlite-vec pip package (sqlite_vec.loadable_path())
            2. Bundled: sovyx/extensions/vec0.{so,dylib,dll}
            3. System: /usr/lib/sqlite3/vec0.so
            4. User: ~/.sovyx/extensions/vec0.so

        Returns:
            Path string if found, None otherwise.
        """
        if name == "vec0":
            # 1. pip package
            try:
                sqlite_vec = importlib.import_module("sqlite_vec")
                loadable = getattr(sqlite_vec, "loadable_path", None)
                if loadable is not None:
                    path = loadable()
                    # ``loadable_path()`` returns the extensionless base
                    # (``<site-packages>/sqlite_vec/vec0``); sqlite appends
                    # the right suffix for the platform at ``load_extension``
                    # time. We must accept all three suffix conventions here
                    # so the existence check matches on every OS:
                    #   * Linux   → vec0.so
                    #   * macOS   → vec0.dylib
                    #   * Windows → vec0.dll
                    # Previously only ``.so`` was tried, so Windows + macOS
                    # ran the fallback branch and returned None, silently
                    # disabling sqlite-vec on those platforms.
                    suffixes = ("", ".so", ".dylib", ".dll")
                    if path and any(os.path.exists(f"{path}{s}") for s in suffixes):
                        return str(path)
            except ImportError:
                pass

        # 2-4. File system search
        candidates = [
            Path(__file__).parent.parent / "extensions" / f"{name}.so",
            Path(__file__).parent.parent / "extensions" / f"{name}.dylib",
            Path(__file__).parent.parent / "extensions" / f"{name}.dll",
            Path(f"/usr/lib/sqlite3/{name}.so"),
            Path.home() / ".sovyx" / "extensions" / f"{name}.so",
        ]
        for candidate in candidates:
            if candidate.exists():
                return str(candidate)

        return None

    @property
    def has_sqlite_vec(self) -> bool:
        """True if sqlite-vec was loaded successfully on all connections."""
        return self._has_sqlite_vec

    @asynccontextmanager
    async def read(self) -> AsyncGenerator[aiosqlite.Connection, None]:
        """Acquire a read connection from the pool (round-robin).

        Raises:
            DatabaseConnectionError: If pool is not initialized.
        """
        if not self._initialized or not self._read_conns:
            msg = "Database pool not initialized"
            raise DatabaseConnectionError(msg)

        async with self._read_index_lock:
            conn = self._read_conns[self._read_index % self._read_pool_size]
            self._read_index = (self._read_index + 1) % self._read_pool_size
        yield conn

    @asynccontextmanager
    async def write(self) -> AsyncGenerator[aiosqlite.Connection, None]:
        """Acquire the write connection (serialized via asyncio.Lock).

        Raises:
            DatabaseConnectionError: If pool is not initialized.
        """
        if not self._initialized or self._write_conn is None:
            msg = "Database pool not initialized"
            raise DatabaseConnectionError(msg)

        async with self._write_lock:
            yield self._write_conn

    @asynccontextmanager
    async def transaction(self) -> AsyncGenerator[aiosqlite.Connection, None]:
        """Write connection with automatic BEGIN/COMMIT/ROLLBACK.

        Commits on success, rolls back on exception.

        Raises:
            DatabaseConnectionError: If pool is not initialized.
        """
        async with self.write() as conn:
            await conn.execute("BEGIN")
            try:
                yield conn
                await conn.execute("COMMIT")
            except Exception:  # noqa: BLE001
                await conn.execute("ROLLBACK")
                raise

    @property
    def is_initialized(self) -> bool:
        """True if the pool has been initialized."""
        return self._initialized
