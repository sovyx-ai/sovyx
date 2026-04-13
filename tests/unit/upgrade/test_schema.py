"""Tests for upgrade schema versioning and migration runner (V05-29).

Covers: SemVer parsing/comparison, SchemaVersion CRUD,
MigrationRunner pipeline (backup → apply → verify → rollback),
expand-contract phase tracking, migration discovery, and checksum
integrity.
"""

from __future__ import annotations

import hashlib
import textwrap
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sovyx.engine.errors import MigrationError
from sovyx.upgrade.schema import (
    MigrationReport,
    MigrationRunner,
    SchemaVersion,
    SemVer,
    UpgradeMigration,
)

if TYPE_CHECKING:
    from pathlib import Path


# ── SemVer ──────────────────────────────────────────────────────────


class TestSemVer:
    """SemVer parsing, ordering, and edge cases."""

    def test_parse_valid(self) -> None:
        sv = SemVer.parse("1.2.3")
        assert sv.major == 1
        assert sv.minor == 2
        assert sv.patch == 3

    def test_parse_zero(self) -> None:
        sv = SemVer.parse("0.0.0")
        assert sv == SemVer.zero()

    def test_parse_large(self) -> None:
        sv = SemVer.parse("100.200.300")
        assert sv.major == 100

    def test_parse_strips_whitespace(self) -> None:
        sv = SemVer.parse("  1.0.0  ")
        assert sv == SemVer(1, 0, 0)

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "1",
            "1.2",
            "1.2.3.4",
            "a.b.c",
            "1.2.x",
            "v1.2.3",
            "-1.0.0",
        ],
    )
    def test_parse_invalid(self, bad: str) -> None:
        with pytest.raises(ValueError, match="Invalid semver"):
            SemVer.parse(bad)

    def test_str_roundtrip(self) -> None:
        assert str(SemVer(1, 2, 3)) == "1.2.3"
        assert SemVer.parse(str(SemVer(5, 10, 0))) == SemVer(5, 10, 0)

    def test_ordering(self) -> None:
        versions = [
            SemVer(2, 0, 0),
            SemVer(1, 0, 0),
            SemVer(1, 1, 0),
            SemVer(1, 0, 1),
        ]
        assert sorted(versions) == [
            SemVer(1, 0, 0),
            SemVer(1, 0, 1),
            SemVer(1, 1, 0),
            SemVer(2, 0, 0),
        ]

    def test_equality(self) -> None:
        assert SemVer(1, 0, 0) == SemVer(1, 0, 0)
        assert SemVer(1, 0, 0) != SemVer(1, 0, 1)

    def test_gt_lt(self) -> None:
        assert SemVer(1, 1, 0) > SemVer(1, 0, 9)
        assert SemVer(0, 9, 9) < SemVer(1, 0, 0)

    def test_frozen(self) -> None:
        sv = SemVer(1, 0, 0)
        with pytest.raises(AttributeError):
            sv.major = 2  # type: ignore[misc]

    def test_zero_factory(self) -> None:
        z = SemVer.zero()
        assert z == SemVer(0, 0, 0)


# ── UpgradeMigration ───────────────────────────────────────────────


class TestUpgradeMigration:
    """UpgradeMigration dataclass behaviour."""

    def test_checksum_auto_computed(self) -> None:
        m = UpgradeMigration(
            version="1.0.0",
            description="test",
            sql_statements=["CREATE TABLE t (id INT)"],
        )
        expected = hashlib.sha256(b"CREATE TABLE t (id INT)").hexdigest()
        assert m.checksum == expected

    def test_checksum_empty_sql(self) -> None:
        m = UpgradeMigration(version="1.0.0", description="noop")
        assert m.checksum == hashlib.sha256(b"").hexdigest()

    def test_checksum_deterministic(self) -> None:
        kwargs: dict = dict(
            version="1.0.0",
            description="d",
            sql_statements=["SELECT 1", "SELECT 2"],
        )
        assert UpgradeMigration(**kwargs).checksum == UpgradeMigration(**kwargs).checksum

    def test_semver_property(self) -> None:
        m = UpgradeMigration(version="2.3.4", description="x")
        assert m.semver == SemVer(2, 3, 4)

    def test_phase_default(self) -> None:
        m = UpgradeMigration(version="1.0.0", description="x")
        assert m.phase == "expand"

    def test_phase_contract(self) -> None:
        m = UpgradeMigration(version="2.0.0", description="x", phase="contract")
        assert m.phase == "contract"

    def test_frozen(self) -> None:
        m = UpgradeMigration(version="1.0.0", description="x")
        with pytest.raises(AttributeError):
            m.version = "2.0.0"  # type: ignore[misc]

    def test_explicit_checksum_preserved(self) -> None:
        m = UpgradeMigration(
            version="1.0.0",
            description="x",
            sql_statements=["SELECT 1"],
            checksum="custom",
        )
        # __post_init__ only sets checksum if empty string
        # Since "custom" is truthy, it stays
        assert m.checksum == "custom"


# ── MigrationReport ────────────────────────────────────────────────


class TestMigrationReport:
    def test_defaults(self) -> None:
        r = MigrationReport()
        assert r.status == "up_to_date"
        assert r.applied == []
        assert r.error == ""


# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture()
async def _pool(tmp_path: Path) -> MagicMock:
    """Create a real in-memory-style pool mock wired to aiosqlite."""
    import aiosqlite

    db_path = tmp_path / "test.db"

    pool = MagicMock()
    pool.db_path = db_path

    # We'll store a real connection reference for consistent reads/writes
    _conn_holder: dict[str, aiosqlite.Connection | None] = {"conn": None}

    async def _get_conn() -> aiosqlite.Connection:
        if _conn_holder["conn"] is None:
            conn = await aiosqlite.connect(str(db_path))
            await conn.execute("PRAGMA journal_mode=WAL")
            _conn_holder["conn"] = conn
        return _conn_holder["conn"]  # type: ignore[return-value]

    class _AsyncCM:
        def __init__(self) -> None:
            self._conn: aiosqlite.Connection | None = None

        async def __aenter__(self) -> aiosqlite.Connection:
            self._conn = await _get_conn()
            return self._conn

        async def __aexit__(self, *a: object) -> None:
            pass

    class _TransactionCM:
        def __init__(self) -> None:
            self._conn: aiosqlite.Connection | None = None

        async def __aenter__(self) -> aiosqlite.Connection:
            self._conn = await _get_conn()
            await self._conn.execute("BEGIN")
            return self._conn

        async def __aexit__(self, exc_type: type | None, *a: object) -> None:
            assert self._conn is not None
            if exc_type is not None:
                await self._conn.execute("ROLLBACK")
            else:
                await self._conn.execute("COMMIT")

    pool.read.return_value = _AsyncCM()
    pool.write.return_value = _AsyncCM()
    pool.transaction.return_value = _TransactionCM()

    # Make read/write/transaction return fresh context managers each call
    pool.read = MagicMock(side_effect=lambda: _AsyncCM())
    pool.write = MagicMock(side_effect=lambda: _AsyncCM())
    pool.transaction = MagicMock(side_effect=lambda: _TransactionCM())

    # For backup/restore we need close/initialize
    async def _close() -> None:
        if _conn_holder["conn"] is not None:
            await _conn_holder["conn"].close()
            _conn_holder["conn"] = None

    async def _initialize() -> None:
        _conn_holder["conn"] = None  # Force new connection

    pool.close = AsyncMock(side_effect=_close)
    pool.initialize = AsyncMock(side_effect=_initialize)

    yield pool
    # Ensure real aiosqlite connection is closed to prevent thread leak
    if _conn_holder["conn"] is not None:
        await _conn_holder["conn"].close()
        _conn_holder["conn"] = None


@pytest.fixture()
async def schema_version(_pool: MagicMock) -> SchemaVersion:
    sv = SchemaVersion(_pool)
    await sv.initialize()
    return sv


def _make_migration(
    version: str = "1.0.0",
    description: str = "test migration",
    sql: list[str] | None = None,
    phase: str = "expand",
    data_migration: object = None,
) -> UpgradeMigration:
    return UpgradeMigration(
        version=version,
        description=description,
        sql_statements=sql or [],
        phase=phase,
        data_migration=data_migration,  # type: ignore[arg-type]
    )


# ── SchemaVersion ───────────────────────────────────────────────────


class TestSchemaVersion:
    """SchemaVersion tracking with real SQLite."""

    @pytest.mark.asyncio()
    async def test_fresh_db_returns_zero(self, schema_version: SchemaVersion) -> None:
        current = await schema_version.get_current()
        assert current == SemVer.zero()

    @pytest.mark.asyncio()
    async def test_record_and_get_current(self, schema_version: SchemaVersion) -> None:
        m = _make_migration("1.0.0", "initial")
        await schema_version.record(m, duration_ms=42)
        current = await schema_version.get_current()
        assert current == SemVer(1, 0, 0)

    @pytest.mark.asyncio()
    async def test_multiple_records_returns_latest(self, schema_version: SchemaVersion) -> None:
        await schema_version.record(_make_migration("1.0.0"), 10)
        await schema_version.record(_make_migration("1.1.0"), 20)
        await schema_version.record(_make_migration("2.0.0"), 30)
        current = await schema_version.get_current()
        assert current == SemVer(2, 0, 0)

    @pytest.mark.asyncio()
    async def test_get_pending_filters_correctly(self, schema_version: SchemaVersion) -> None:
        migrations = [
            _make_migration("1.0.0"),
            _make_migration("1.1.0"),
            _make_migration("2.0.0"),
        ]
        pending = schema_version.get_pending(SemVer(1, 0, 0), migrations)
        assert len(pending) == 2
        assert pending[0].version == "1.1.0"
        assert pending[1].version == "2.0.0"

    @pytest.mark.asyncio()
    async def test_get_pending_empty_when_up_to_date(self, schema_version: SchemaVersion) -> None:
        migrations = [_make_migration("1.0.0")]
        pending = schema_version.get_pending(SemVer(1, 0, 0), migrations)
        assert pending == []

    @pytest.mark.asyncio()
    async def test_get_pending_all_when_fresh(self, schema_version: SchemaVersion) -> None:
        migrations = [_make_migration("1.0.0"), _make_migration("1.1.0")]
        pending = schema_version.get_pending(SemVer.zero(), migrations)
        assert len(pending) == 2

    @pytest.mark.asyncio()
    async def test_get_pending_sorted(self, schema_version: SchemaVersion) -> None:
        migrations = [
            _make_migration("2.0.0"),
            _make_migration("1.0.0"),
            _make_migration("1.5.0"),
        ]
        pending = schema_version.get_pending(SemVer.zero(), migrations)
        assert [m.version for m in pending] == ["1.0.0", "1.5.0", "2.0.0"]

    @pytest.mark.asyncio()
    async def test_get_history(self, schema_version: SchemaVersion) -> None:
        m1 = _make_migration("1.0.0", "first")
        m2 = _make_migration("1.1.0", "second")
        await schema_version.record(m1, 10)
        await schema_version.record(m2, 20)
        history = await schema_version.get_history()
        assert len(history) == 2
        assert history[0]["version"] == "1.0.0"
        assert history[0]["description"] == "first"
        assert history[0]["duration_ms"] == 10
        assert history[1]["version"] == "1.1.0"

    @pytest.mark.asyncio()
    async def test_get_history_empty(self, schema_version: SchemaVersion) -> None:
        history = await schema_version.get_history()
        assert history == []


# ── MigrationRunner ────────────────────────────────────────────────


class TestMigrationRunner:
    """MigrationRunner: apply, rollback, integrity, discovery."""

    @pytest.fixture()
    async def runner(
        self, _pool: MagicMock, schema_version: SchemaVersion, tmp_path: Path
    ) -> MigrationRunner:
        return MigrationRunner(
            pool=_pool,
            schema_version=schema_version,
            backup_dir=tmp_path / "backups",
        )

    @pytest.mark.asyncio()
    async def test_no_pending_returns_up_to_date(self, runner: MigrationRunner) -> None:
        report = await runner.run([])
        assert report.status == "up_to_date"
        assert report.applied == []

    @pytest.mark.asyncio()
    async def test_apply_single_migration(
        self,
        runner: MigrationRunner,
        schema_version: SchemaVersion,
    ) -> None:
        m = _make_migration(
            "1.0.0",
            "create table",
            ["CREATE TABLE test_t (id INTEGER PRIMARY KEY)"],
        )
        report = await runner.run([m])
        assert report.status == "success"
        assert len(report.applied) == 1
        assert "1.0.0" in report.applied[0]

        current = await schema_version.get_current()
        assert current == SemVer(1, 0, 0)

    @pytest.mark.asyncio()
    async def test_apply_multiple_in_order(
        self,
        runner: MigrationRunner,
        schema_version: SchemaVersion,
    ) -> None:
        migrations = [
            _make_migration("1.0.0", "v1", ["CREATE TABLE t1 (id INT)"]),
            _make_migration("1.1.0", "v1.1", ["CREATE TABLE t2 (id INT)"]),
            _make_migration("2.0.0", "v2", ["CREATE TABLE t3 (id INT)"]),
        ]
        report = await runner.run(migrations)
        assert report.status == "success"
        assert len(report.applied) == 3

        current = await schema_version.get_current()
        assert current == SemVer(2, 0, 0)

    @pytest.mark.asyncio()
    async def test_idempotent_rerun(
        self,
        runner: MigrationRunner,
    ) -> None:
        m = _make_migration("1.0.0", "v1", ["CREATE TABLE idem_t (id INT)"])
        report1 = await runner.run([m])
        assert report1.status == "success"

        report2 = await runner.run([m])
        assert report2.status == "up_to_date"

    @pytest.mark.asyncio()
    async def test_rollback_on_sql_failure(
        self,
        runner: MigrationRunner,
        schema_version: SchemaVersion,
    ) -> None:
        good = _make_migration("1.0.0", "good", ["CREATE TABLE rollback_t (id INT)"])
        bad = _make_migration("1.1.0", "bad", ["INVALID SQL STATEMENT THAT WILL FAIL"])
        report = await runner.run([good, bad])
        assert report.status == "failed"
        assert "bad" in report.error.lower() or "INVALID" in report.error

    @pytest.mark.asyncio()
    async def test_data_migration_runs(
        self,
        runner: MigrationRunner,
        _pool: MagicMock,
    ) -> None:
        calls: list[str] = []

        async def my_data_migration(conn: object) -> None:
            calls.append("ran")

        m = _make_migration(
            "1.0.0",
            "with data migration",
            ["CREATE TABLE dm_t (id INT)"],
            data_migration=my_data_migration,
        )
        report = await runner.run([m])
        assert report.status == "success"
        assert calls == ["ran"]

    @pytest.mark.asyncio()
    async def test_data_migration_failure_rolls_back(
        self,
        runner: MigrationRunner,
    ) -> None:
        async def failing_dm(conn: object) -> None:
            msg = "data migration boom"
            raise RuntimeError(msg)

        m = _make_migration(
            "1.0.0",
            "fail dm",
            ["CREATE TABLE fldm_t (id INT)"],
            data_migration=failing_dm,
        )
        report = await runner.run([m])
        assert report.status == "failed"
        assert "boom" in report.error

    @pytest.mark.asyncio()
    async def test_backup_created_before_migration(
        self,
        runner: MigrationRunner,
        tmp_path: Path,
    ) -> None:
        m = _make_migration("1.0.0", "v1", ["CREATE TABLE bk_t (id INT)"])
        await runner.run([m])
        backups = list((tmp_path / "backups").glob("sovyx_upgrade_*.db"))
        assert len(backups) >= 1

    @pytest.mark.asyncio()
    async def test_expand_contract_phases(
        self,
        runner: MigrationRunner,
        schema_version: SchemaVersion,
    ) -> None:
        expand = _make_migration(
            "1.0.0",
            "expand: add col",
            ["CREATE TABLE ec_t (id INT, old_col TEXT)"],
            phase="expand",
        )
        contract = _make_migration(
            "1.1.0",
            "contract: drop old",
            # SQLite 3.35+ supports ALTER TABLE DROP COLUMN
            ["ALTER TABLE ec_t DROP COLUMN old_col"],
            phase="contract",
        )
        report = await runner.run([expand, contract])
        assert report.status == "success"
        assert len(report.applied) == 2

        history = await schema_version.get_history()
        assert len(history) == 2

    @pytest.mark.asyncio()
    async def test_verify_applied_checksums_ok(
        self,
        runner: MigrationRunner,
    ) -> None:
        m = _make_migration("1.0.0", "v1", ["CREATE TABLE vc_t (id INT)"])
        await runner.run([m])
        assert await runner.verify_applied([m]) is True

    @pytest.mark.asyncio()
    async def test_verify_applied_detects_mismatch(
        self,
        runner: MigrationRunner,
    ) -> None:
        m = _make_migration("1.0.0", "v1", ["CREATE TABLE vm_t (id INT)"])
        await runner.run([m])

        # Tamper: create a migration with same version but different SQL
        tampered = UpgradeMigration(
            version="1.0.0",
            description="v1",
            sql_statements=["CREATE TABLE totally_different (x INT)"],
        )
        assert await runner.verify_applied([tampered]) is False

    @pytest.mark.asyncio()
    async def test_verify_applied_ignores_unapplied(
        self,
        runner: MigrationRunner,
    ) -> None:
        m = _make_migration("1.0.0", "v1")
        # Never applied — verify should still pass (no mismatch)
        assert await runner.verify_applied([m]) is True

    @pytest.mark.asyncio()
    async def test_skips_already_applied(
        self,
        runner: MigrationRunner,
        schema_version: SchemaVersion,
    ) -> None:
        m1 = _make_migration("1.0.0", "v1", ["CREATE TABLE sk_t (id INT)"])
        m2 = _make_migration("1.1.0", "v1.1", ["CREATE TABLE sk_t2 (id INT)"])

        await runner.run([m1])
        report = await runner.run([m1, m2])

        assert report.status == "success"
        assert len(report.applied) == 1
        assert "1.1.0" in report.applied[0]


class TestMigrationRunnerDiscovery:
    """Test migration auto-discovery."""

    def test_discover_empty_package(self, tmp_path: Path) -> None:
        """Discovery on a package with no migration files returns empty."""
        pkg_dir = tmp_path / "empty_pkg"
        pkg_dir.mkdir()
        (pkg_dir / "__init__.py").write_text("# empty")

        with patch("sovyx.upgrade.schema.importlib.import_module") as mock_import:
            mock_mod = MagicMock()
            mock_mod.__file__ = str(pkg_dir / "__init__.py")
            mock_import.return_value = mock_mod

            result = MigrationRunner.discover_migrations("fake.package")
            assert result == []

    def test_discover_finds_and_sorts(self, tmp_path: Path) -> None:
        """Discovery finds numbered .py files and sorts by semver."""
        pkg_dir = tmp_path / "mig_pkg"
        pkg_dir.mkdir()
        (pkg_dir / "__init__.py").write_text("# pkg")

        # Create two migration files
        (pkg_dir / "001_initial.py").write_text(
            textwrap.dedent("""\
            from sovyx.upgrade.schema import UpgradeMigration
            MIGRATION = UpgradeMigration(
                version="1.0.0",
                description="initial",
                sql_statements=["CREATE TABLE d (id INT)"],
            )
            """)
        )
        (pkg_dir / "002_add_col.py").write_text(
            textwrap.dedent("""\
            from sovyx.upgrade.schema import UpgradeMigration
            MIGRATION = UpgradeMigration(
                version="1.1.0",
                description="add col",
                sql_statements=["ALTER TABLE d ADD COLUMN name TEXT"],
            )
            """)
        )

        import sys

        # Make the package importable
        sys.path.insert(0, str(tmp_path))
        try:
            result = MigrationRunner.discover_migrations("mig_pkg")
            assert len(result) == 2
            assert result[0].version == "1.0.0"
            assert result[1].version == "1.1.0"
        finally:
            sys.path.pop(0)
            # Clean up imported modules
            for key in list(sys.modules):
                if key.startswith("mig_pkg"):
                    del sys.modules[key]

    def test_discover_skips_non_migration_files(self, tmp_path: Path) -> None:
        """Files without a MIGRATION attribute are skipped."""
        pkg_dir = tmp_path / "skip_pkg"
        pkg_dir.mkdir()
        (pkg_dir / "__init__.py").write_text("# pkg")
        (pkg_dir / "001_no_attr.py").write_text("# no MIGRATION here\nx = 42")

        import sys

        sys.path.insert(0, str(tmp_path))
        try:
            result = MigrationRunner.discover_migrations("skip_pkg")
            assert result == []
        finally:
            sys.path.pop(0)
            for key in list(sys.modules):
                if key.startswith("skip_pkg"):
                    del sys.modules[key]

    def test_discover_handles_import_error(self, tmp_path: Path) -> None:
        """Discovery skips files that raise on import."""
        pkg_dir = tmp_path / "err_pkg"
        pkg_dir.mkdir()
        (pkg_dir / "__init__.py").write_text("# pkg")
        (pkg_dir / "001_bad.py").write_text("raise RuntimeError('broken')")

        import sys

        sys.path.insert(0, str(tmp_path))
        try:
            result = MigrationRunner.discover_migrations("err_pkg")
            assert result == []
        finally:
            sys.path.pop(0)
            for key in list(sys.modules):
                if key.startswith("err_pkg"):
                    del sys.modules[key]


class TestSchemaVersionEdgeCases:
    """Edge cases for SchemaVersion."""

    @pytest.mark.asyncio()
    async def test_corrupted_version_returns_zero(
        self, _pool: MagicMock, schema_version: SchemaVersion
    ) -> None:
        """If stored version is not valid semver, return 0.0.0."""
        async with _pool.write() as conn:
            await conn.execute(
                "INSERT INTO _schema_version (version, checksum, description) VALUES (?, ?, ?)",
                ("not-semver", "abc", "corrupted"),
            )
            await conn.commit()
        current = await schema_version.get_current()
        assert current == SemVer.zero()


class TestMigrationRunnerEdgeCases:
    """Edge cases and error paths."""

    @pytest.fixture()
    async def runner(
        self, _pool: MagicMock, schema_version: SchemaVersion, tmp_path: Path
    ) -> MigrationRunner:
        return MigrationRunner(
            pool=_pool,
            schema_version=schema_version,
            backup_dir=tmp_path / "backups",
        )

    @pytest.mark.asyncio()
    async def test_multiple_sql_statements(
        self,
        runner: MigrationRunner,
        schema_version: SchemaVersion,
    ) -> None:
        m = _make_migration(
            "1.0.0",
            "multi sql",
            [
                "CREATE TABLE multi_a (id INT)",
                "CREATE TABLE multi_b (id INT)",
                "CREATE TABLE multi_c (id INT)",
            ],
        )
        report = await runner.run([m])
        assert report.status == "success"

    @pytest.mark.asyncio()
    async def test_empty_sql_list(
        self,
        runner: MigrationRunner,
        schema_version: SchemaVersion,
    ) -> None:
        m = _make_migration("1.0.0", "noop migration")
        report = await runner.run([m])
        assert report.status == "success"
        assert await schema_version.get_current() == SemVer(1, 0, 0)

    @pytest.mark.asyncio()
    async def test_migrations_applied_in_version_order(
        self,
        runner: MigrationRunner,
    ) -> None:
        """Even if passed out of order, applied in semver order."""
        order: list[str] = []

        async def track_v1(conn: object) -> None:
            order.append("1.0.0")

        async def track_v2(conn: object) -> None:
            order.append("2.0.0")

        async def track_v15(conn: object) -> None:
            order.append("1.5.0")

        m1 = _make_migration("2.0.0", "v2", data_migration=track_v2)
        m2 = _make_migration("1.0.0", "v1", data_migration=track_v1)
        m3 = _make_migration("1.5.0", "v1.5", data_migration=track_v15)

        await runner.run([m1, m2, m3])
        assert order == ["1.0.0", "1.5.0", "2.0.0"]

    @pytest.mark.asyncio()
    async def test_backup_failure_raises_migration_error(
        self,
        schema_version: SchemaVersion,
        _pool: MagicMock,
        tmp_path: Path,
    ) -> None:
        """If VACUUM INTO fails, MigrationError is raised."""
        # Create runner with a read-only backup dir (simulated by patching)
        runner = MigrationRunner(
            pool=_pool,
            schema_version=schema_version,
            backup_dir=tmp_path / "backups",
        )
        m = _make_migration("1.0.0", "v1", ["CREATE TABLE bf_t (id INT)"])

        # Patch pool.write to fail on VACUUM INTO
        original_write = _pool.write

        class _FailVacuumCM:
            async def __aenter__(self) -> object:
                cm = original_write()
                self._conn = await cm.__aenter__()

                original_execute = self._conn.execute

                async def patched_execute(sql: str, *args: object) -> object:
                    if "VACUUM INTO" in sql:
                        msg = "disk full"
                        raise OSError(msg)
                    return await original_execute(sql, *args)

                self._conn.execute = patched_execute
                return self._conn

            async def __aexit__(self, *a: object) -> None:
                pass

        _pool.write = MagicMock(side_effect=lambda: _FailVacuumCM())

        with pytest.raises(MigrationError, match="backup failed"):
            await runner.run([m])

    @pytest.mark.asyncio()
    async def test_discover_none_pkg_file(self) -> None:
        """Discovery returns empty if package has no __file__."""
        with patch("sovyx.upgrade.schema.importlib.import_module") as mock_import:
            mock_mod = MagicMock()
            mock_mod.__file__ = None
            mock_import.return_value = mock_mod
            result = MigrationRunner.discover_migrations("no.file.pkg")
            assert result == []

    @pytest.mark.asyncio()
    async def test_data_migration_raises_migration_error(
        self,
        runner: MigrationRunner,
    ) -> None:
        """Line 417: MigrationError from data_migration re-raised directly."""

        async def dm_raises_migration_error(conn: object) -> None:
            raise MigrationError("deliberate migration error")

        m = _make_migration(
            "1.0.0",
            "dm reraise",
            ["CREATE TABLE dm_reraise_t (id INT)"],
            data_migration=dm_raises_migration_error,
        )
        report = await runner.run([m])
        assert report.status == "failed"
        assert "deliberate migration error" in report.error

    @pytest.mark.asyncio()
    async def test_sql_raises_migration_error(
        self,
        runner: MigrationRunner,
    ) -> None:
        """Generic Exception from SQL wraps in MigrationError."""
        m = _make_migration(
            "1.0.0",
            "bad sql",
            ["THIS IS NOT VALID SQL AT ALL"],
        )
        report = await runner.run([m])
        assert report.status == "failed"

    @pytest.mark.asyncio()
    async def test_integrity_check_failure(
        self,
        runner: MigrationRunner,
    ) -> None:
        """Lines 480-482: integrity_check returns non-ok result."""
        m = _make_migration("1.0.0", "ok migration", ["CREATE TABLE ic_t (id INT)"])

        original_verify = runner._verify_integrity  # noqa: SLF001

        async def bad_integrity() -> None:
            # Patch the pool's read to return a corrupt integrity check
            from unittest.mock import AsyncMock as _AM  # noqa: N814
            from unittest.mock import MagicMock as _MM  # noqa: N814

            old_read = runner._pool.read  # noqa: SLF001

            class _BadReadCM:
                async def __aenter__(self_inner) -> object:  # noqa: N805
                    conn = _MM()
                    cursor = _MM()
                    cursor.fetchone = _AM(return_value=("database disk image is malformed",))
                    conn.execute = _AM(return_value=cursor)
                    return conn

                async def __aexit__(self_inner, *a: object) -> None:  # noqa: N805
                    pass

            runner._pool.read = _MM(return_value=_BadReadCM())  # noqa: SLF001
            try:
                await original_verify()
            finally:
                runner._pool.read = old_read  # noqa: SLF001

        runner._verify_integrity = bad_integrity  # noqa: SLF001
        report = await runner.run([m])
        assert report.status == "failed"
        assert "integrity check failed" in report.error.lower()


class TestMigrationsInit:
    """Verify migrations package imports."""

    def test_migrations_package_importable(self) -> None:
        """upgrade.migrations package can be imported."""
        import sovyx.upgrade.migrations

        assert sovyx.upgrade.migrations.__doc__ is not None
