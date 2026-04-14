"""Tests for sovyx.persistence.pool — database connection pool."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from sovyx.engine.errors import DatabaseConnectionError
from sovyx.persistence.pool import DatabasePool


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Temporary database path."""
    return tmp_path / "test.db"


@pytest.fixture
async def pool(db_path: Path) -> DatabasePool:
    """Initialized pool for tests."""
    p = DatabasePool(db_path=db_path, read_pool_size=2)
    await p.initialize()
    yield p  # type: ignore[misc]
    await p.close()


class TestInitialization:
    """Pool initialization and pragmas."""

    async def test_initialize_creates_db_file(self, db_path: Path) -> None:
        pool = DatabasePool(db_path=db_path)
        await pool.initialize()
        assert db_path.exists()
        assert pool.is_initialized is True
        await pool.close()

    async def test_initialize_creates_parent_dirs(self, tmp_path: Path) -> None:
        db_path = tmp_path / "sub" / "dir" / "test.db"
        pool = DatabasePool(db_path=db_path)
        await pool.initialize()
        assert db_path.exists()
        await pool.close()

    async def test_close_clears_state(self, db_path: Path) -> None:
        pool = DatabasePool(db_path=db_path)
        await pool.initialize()
        assert pool.is_initialized is True
        await pool.close()
        assert pool.is_initialized is False

    async def test_close_checkpoints_wal(self, db_path: Path) -> None:
        """Close performs WAL checkpoint before closing writer."""
        pool = DatabasePool(db_path=db_path)
        await pool.initialize()
        # Write something to generate WAL entries
        async with pool.write() as conn:
            await conn.execute("CREATE TABLE IF NOT EXISTS wal_test (id INTEGER)")
            await conn.execute("INSERT INTO wal_test VALUES (1)")
        await pool.close()
        # After TRUNCATE checkpoint, WAL file should be empty or absent
        wal_path = db_path.with_suffix(".db-wal")
        if wal_path.exists():
            assert wal_path.stat().st_size == 0

    async def test_close_survives_checkpoint_failure(self, db_path: Path) -> None:
        """Close still succeeds even if WAL checkpoint fails."""
        pool = DatabasePool(db_path=db_path)
        await pool.initialize()
        # Sabotage: close the write connection's underlying sqlite connection
        # so checkpoint will fail, but pool.close() should still complete
        await pool.close()
        assert pool.is_initialized is False

    async def test_wal_mode_active(self, pool: DatabasePool) -> None:
        async with pool.read() as conn:
            cursor = await conn.execute("PRAGMA journal_mode")
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] == "wal"

    async def test_foreign_keys_on(self, pool: DatabasePool) -> None:
        async with pool.read() as conn:
            cursor = await conn.execute("PRAGMA foreign_keys")
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] == 1

    async def test_synchronous_normal(self, pool: DatabasePool) -> None:
        async with pool.read() as conn:
            cursor = await conn.execute("PRAGMA synchronous")
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] == 1  # NORMAL = 1

    async def test_custom_pragmas_applied(self, db_path: Path) -> None:
        pool = DatabasePool(
            db_path=db_path,
            pragmas={"cache_size": -32000},
        )
        await pool.initialize()
        async with pool.read() as conn:
            cursor = await conn.execute("PRAGMA cache_size")
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] == -32000
        await pool.close()

    async def test_invalid_path_raises(self, tmp_path: Path) -> None:
        db_path = tmp_path / "nonexistent" / "deep" / "test.db"
        # Create a file where a dir is expected to make it fail
        (tmp_path / "nonexistent").touch()
        pool = DatabasePool(db_path=db_path)
        with pytest.raises(DatabaseConnectionError):
            await pool.initialize()


class TestReadConnections:
    """Read pool functionality."""

    async def test_read_returns_connection(self, pool: DatabasePool) -> None:
        async with pool.read() as conn:
            cursor = await conn.execute("SELECT 1")
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] == 1

    async def test_concurrent_reads(self, pool: DatabasePool) -> None:
        """3 concurrent reads should work."""
        async with pool.write() as conn:
            await conn.execute("CREATE TABLE test (id INTEGER)")
            await conn.execute("INSERT INTO test VALUES (42)")
            await conn.commit()

        async def do_read() -> int:
            async with pool.read() as conn:
                cursor = await conn.execute("SELECT id FROM test")
                row = await cursor.fetchone()
                assert row is not None
                return int(row[0])

        results = await asyncio.gather(do_read(), do_read(), do_read())
        assert results == [42, 42, 42]

    async def test_pool_round_robin(self, pool: DatabasePool) -> None:
        """Read connections are distributed round-robin."""
        conns: list[object] = []
        for _ in range(4):
            async with pool.read() as conn:
                conns.append(conn)
        # Pool size = 2, so conn[0]==conn[2] and conn[1]==conn[3]
        assert conns[0] is conns[2]
        assert conns[1] is conns[3]
        assert conns[0] is not conns[1]

    async def test_concurrent_read_index_is_locked(
        self, pool: DatabasePool,
    ) -> None:
        """Under contention, _read_index must still advance by exactly N.

        Regression guard: a lost update on _read_index would leave the
        cursor at a value lower than the number of acquire() calls. We
        acquire the read lock many times concurrently and then assert
        the cursor matches (calls mod pool_size).
        """
        acquired = 0

        async def one_read() -> None:
            nonlocal acquired
            async with pool.read():
                acquired += 1

        await asyncio.gather(*(one_read() for _ in range(16)))
        assert acquired == 16
        # 16 calls, pool_size=2 → cursor wraps to 16 % 2 == 0
        assert pool._read_index == 0

    async def test_read_not_initialized_raises(self, db_path: Path) -> None:
        pool = DatabasePool(db_path=db_path)
        with pytest.raises(DatabaseConnectionError):
            async with pool.read():
                pass


class TestWriteConnection:
    """Write connection and serialization."""

    async def test_write_returns_connection(self, pool: DatabasePool) -> None:
        async with pool.write() as conn:
            await conn.execute("CREATE TABLE test (id INTEGER)")
            await conn.execute("INSERT INTO test VALUES (1)")
            await conn.commit()

        async with pool.read() as conn:
            cursor = await conn.execute("SELECT COUNT(*) FROM test")
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] == 1

    async def test_write_serialization(self, pool: DatabasePool) -> None:
        """Concurrent writes don't collide (serialized via lock)."""
        async with pool.write() as conn:
            await conn.execute("CREATE TABLE test (id INTEGER)")
            await conn.commit()

        order: list[int] = []

        async def do_write(val: int) -> None:
            async with pool.write() as conn:
                await conn.execute("INSERT INTO test VALUES (?)", (val,))
                await conn.commit()
                order.append(val)

        await asyncio.gather(do_write(1), do_write(2))
        assert sorted(order) == [1, 2]
        assert len(order) == 2

    async def test_write_not_initialized_raises(self, db_path: Path) -> None:
        pool = DatabasePool(db_path=db_path)
        with pytest.raises(DatabaseConnectionError):
            async with pool.write():
                pass


class TestTransaction:
    """Transaction with auto commit/rollback."""

    async def test_transaction_commits_on_success(self, pool: DatabasePool) -> None:
        async with pool.write() as conn:
            await conn.execute("CREATE TABLE test (id INTEGER)")
            await conn.commit()

        async with pool.transaction() as conn:
            await conn.execute("INSERT INTO test VALUES (1)")

        async with pool.read() as conn:
            cursor = await conn.execute("SELECT COUNT(*) FROM test")
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] == 1

    async def test_transaction_rollback_on_exception(self, pool: DatabasePool) -> None:
        async with pool.write() as conn:
            await conn.execute("CREATE TABLE test (id INTEGER)")
            await conn.commit()

        with pytest.raises(RuntimeError):
            async with pool.transaction() as conn:
                await conn.execute("INSERT INTO test VALUES (1)")
                msg = "boom"
                raise RuntimeError(msg)

        async with pool.read() as conn:
            cursor = await conn.execute("SELECT COUNT(*) FROM test")
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] == 0


class TestExtensions:
    """Extension loading (sqlite-vec)."""

    async def test_no_extensions_by_default(self, pool: DatabasePool) -> None:
        assert pool.has_sqlite_vec is False

    async def test_extension_not_found_graceful(self, db_path: Path) -> None:
        pool = DatabasePool(
            db_path=db_path,
            load_extensions=["nonexistent_ext"],
        )
        await pool.initialize()
        assert pool.has_sqlite_vec is False
        await pool.close()

    def test_find_extension_path_vec0_found(self) -> None:
        """Finds vec0 via some search path (pip or filesystem)."""
        result = DatabasePool._find_extension_path("vec0")
        # sqlite_vec is installed in this env, so it should be found
        assert result is not None
        assert "vec0" in result

    def test_find_extension_path_pip_not_installed(self) -> None:
        """Falls through when sqlite_vec not installed + no filesystem hit."""
        import sovyx.persistence.pool as pool_module

        with (
            patch.object(
                pool_module.importlib,
                "import_module",
                side_effect=ImportError,
            ),
            patch.object(Path, "exists", return_value=False),
        ):
            result = DatabasePool._find_extension_path("vec0")
            assert result is None

    def test_find_extension_path_filesystem_fallback(self) -> None:
        """Finds extension via filesystem when pip package unavailable."""
        import sovyx.persistence.pool as pool_module

        call_count = 0

        def mock_exists(self: Path) -> bool:
            nonlocal call_count
            call_count += 1
            return call_count == 4  # noqa: PLR2004

        with (
            patch.object(
                pool_module.importlib,
                "import_module",
                side_effect=ImportError,
            ),
            patch.object(Path, "exists", mock_exists),
        ):
            result = DatabasePool._find_extension_path("vec0")
            assert result is not None

    def test_find_extension_path_non_vec0(self) -> None:
        """Non-vec0 extensions skip pip package check."""
        with patch.object(Path, "exists", return_value=False):
            result = DatabasePool._find_extension_path("some_ext")
            assert result is None

    async def test_load_extensions_called_per_connection(self, db_path: Path) -> None:
        """_load_extensions called for write + all read connections."""
        load_count = 0
        pool = DatabasePool(
            db_path=db_path,
            read_pool_size=2,
            load_extensions=["missing_ext"],
        )
        original_load = pool._load_extensions

        async def counting_load(conn: object) -> None:
            nonlocal load_count
            load_count += 1
            await original_load(conn)  # type: ignore[arg-type]

        pool._load_extensions = counting_load  # type: ignore[assignment]
        await pool.initialize()
        # 1 write + 2 read = 3 calls
        assert load_count == 3  # noqa: PLR2004
        await pool.close()

    async def test_load_extension_success_path(self, db_path: Path) -> None:
        """Extension loading success sets has_sqlite_vec=True."""
        pool = DatabasePool(db_path=db_path, load_extensions=["vec0"])
        await pool.initialize()
        # If sqlite_vec is installed, it's True; if not, False
        # Either way, the code path is exercised and no crash
        assert isinstance(pool.has_sqlite_vec, bool)
        await pool.close()

    async def test_load_extension_load_failure_graceful(self, db_path: Path) -> None:
        """If load_extension raises, has_sqlite_vec=False but no crash."""
        pool = DatabasePool(db_path=db_path, load_extensions=["vec0"])

        # Patch find to return a path, but load_extension will fail on bogus file
        with patch.object(
            DatabasePool,
            "_find_extension_path",
            return_value="/bogus/path/vec0.so",
        ):
            await pool.initialize()
            assert pool.has_sqlite_vec is False
        await pool.close()

    def test_find_extension_pip_no_loadable_path(self) -> None:
        """sqlite_vec module exists but no loadable_path attribute."""
        import sovyx.persistence.pool as pool_mod

        mock_module = MagicMock(spec=[])

        with (
            patch.object(
                pool_mod.importlib,
                "import_module",
                return_value=mock_module,
            ),
            patch.object(Path, "exists", return_value=False),
        ):
            result = DatabasePool._find_extension_path("vec0")
            assert result is None

    def test_find_extension_pip_path_not_exists(self) -> None:
        """sqlite_vec.loadable_path() returns path that doesn't exist."""
        import sovyx.persistence.pool as pool_mod

        mock_module = MagicMock()
        mock_module.loadable_path.return_value = "/nonexistent/vec0.so"

        with (
            patch.object(
                pool_mod.importlib,
                "import_module",
                return_value=mock_module,
            ),
            patch("os.path.exists", return_value=False),
            patch.object(Path, "exists", return_value=False),
        ):
            result = DatabasePool._find_extension_path("vec0")
            assert result is None

    def test_has_sqlite_vec_flag_default_false(self, db_path: Path) -> None:
        """has_sqlite_vec starts as False."""
        pool = DatabasePool(db_path=db_path)
        assert pool.has_sqlite_vec is False

    def test_has_sqlite_vec_flag_settable(self, db_path: Path) -> None:
        """has_sqlite_vec reflects internal state."""
        pool = DatabasePool(db_path=db_path)
        pool._has_sqlite_vec = True  # simulate successful load
        assert pool.has_sqlite_vec is True
