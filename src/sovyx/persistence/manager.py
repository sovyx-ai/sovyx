"""Sovyx database manager.

Manages all database pools, applies migrations, and exposes unified
access to system.db and per-mind brain.db/conversations.db.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sovyx.engine.errors import DatabaseConnectionError
from sovyx.observability.logging import get_logger
from sovyx.persistence.migrations import MigrationRunner
from sovyx.persistence.pool import DatabasePool
from sovyx.persistence.schemas.brain import get_brain_migrations
from sovyx.persistence.schemas.conversations import get_conversation_migrations
from sovyx.persistence.schemas.system import get_system_migrations

if TYPE_CHECKING:
    from sovyx.engine.config import EngineConfig
    from sovyx.engine.events import EventBus
    from sovyx.engine.types import MindId

logger = get_logger(__name__)


class DatabaseManager:
    """Manages all databases for the Sovyx engine.

    Responsibilities:
        - Create data directories if they don't exist
        - Initialize pools for system.db (global) and per-mind DBs
        - Apply migrations automatically on startup
        - Expose pools via get_brain_pool, get_conversation_pool, get_system_pool
        - Verify sqlite-vec extension availability

    Implements the Lifecycle protocol (start/stop).
    """

    def __init__(self, config: EngineConfig, event_bus: EventBus) -> None:
        self._config = config
        self._event_bus = event_bus
        self._data_dir = config.database.data_dir
        self._system_pool: DatabasePool | None = None
        self._brain_pools: dict[str, DatabasePool] = {}
        self._conversation_pools: dict[str, DatabasePool] = {}
        self._running = False

    async def start(self) -> None:
        """Initialize system.db and apply migrations.

        Creates data directories and the global system database.

        Raises:
            DatabaseConnectionError: If system.db cannot be initialized.
        """
        self._data_dir.mkdir(parents=True, exist_ok=True)

        self._system_pool = DatabasePool(
            db_path=self._data_dir / "system.db",
            read_pool_size=self._config.database.read_pool_size,
        )
        await self._system_pool.initialize()

        runner = MigrationRunner(self._system_pool)
        await runner.initialize()
        applied = await runner.run_migrations(get_system_migrations())

        self._running = True
        logger.info(
            "database_manager_started",
            data_dir=str(self._data_dir),
            system_migrations_applied=applied,
        )

    async def stop(self) -> None:
        """Close all database pools gracefully."""
        for mind_id, pool in self._brain_pools.items():
            await pool.close()
            logger.debug("brain_pool_closed", mind_id=mind_id)

        for mind_id, pool in self._conversation_pools.items():
            await pool.close()
            logger.debug("conversation_pool_closed", mind_id=mind_id)

        if self._system_pool is not None:
            await self._system_pool.close()

        self._brain_pools.clear()
        self._conversation_pools.clear()
        self._system_pool = None
        self._running = False
        logger.info("database_manager_stopped")

    async def initialize_mind_databases(self, mind_id: MindId) -> None:
        """Create and migrate brain.db and conversations.db for a mind.

        Args:
            mind_id: The mind identifier.

        Raises:
            DatabaseConnectionError: If databases cannot be initialized.
        """
        mind_dir = self._data_dir / str(mind_id)
        mind_dir.mkdir(parents=True, exist_ok=True)

        # Brain pool (with sqlite-vec extension)
        brain_pool = DatabasePool(
            db_path=mind_dir / "brain.db",
            read_pool_size=self._config.database.read_pool_size,
            load_extensions=["vec0"],
        )
        await brain_pool.initialize()

        brain_runner = MigrationRunner(brain_pool)
        await brain_runner.initialize()
        brain_applied = await brain_runner.run_migrations(
            get_brain_migrations(has_sqlite_vec=brain_pool.has_sqlite_vec)
        )

        self._brain_pools[str(mind_id)] = brain_pool

        # Conversation pool (no extensions needed)
        conv_pool = DatabasePool(
            db_path=mind_dir / "conversations.db",
            read_pool_size=self._config.database.read_pool_size,
        )
        await conv_pool.initialize()

        conv_runner = MigrationRunner(conv_pool)
        await conv_runner.initialize()
        conv_applied = await conv_runner.run_migrations(get_conversation_migrations())

        self._conversation_pools[str(mind_id)] = conv_pool

        logger.info(
            "mind_databases_initialized",
            mind_id=str(mind_id),
            brain_migrations=brain_applied,
            conversation_migrations=conv_applied,
            has_sqlite_vec=brain_pool.has_sqlite_vec,
        )

    def get_system_pool(self) -> DatabasePool:
        """Return the system database pool.

        Raises:
            DatabaseConnectionError: If not initialized.
        """
        if self._system_pool is None:
            msg = "Database manager not started"
            raise DatabaseConnectionError(msg)
        return self._system_pool

    def get_brain_pool(self, mind_id: MindId) -> DatabasePool:
        """Return the brain database pool for a mind.

        Args:
            mind_id: The mind identifier.

        Raises:
            DatabaseConnectionError: If mind databases not initialized.
        """
        pool = self._brain_pools.get(str(mind_id))
        if pool is None:
            msg = f"Brain database not initialized for mind: {mind_id}"
            raise DatabaseConnectionError(msg)
        return pool

    def get_conversation_pool(self, mind_id: MindId) -> DatabasePool:
        """Return the conversation database pool for a mind.

        Args:
            mind_id: The mind identifier.

        Raises:
            DatabaseConnectionError: If mind databases not initialized.
        """
        pool = self._conversation_pools.get(str(mind_id))
        if pool is None:
            msg = f"Conversation database not initialized for mind: {mind_id}"
            raise DatabaseConnectionError(msg)
        return pool

    @property
    def has_sqlite_vec(self) -> bool:
        """True if any brain pool has sqlite-vec loaded."""
        return any(p.has_sqlite_vec for p in self._brain_pools.values())

    @property
    def is_running(self) -> bool:
        """True if the database manager is running."""
        return self._running
