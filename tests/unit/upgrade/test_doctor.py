"""Tests for Doctor diagnostic suite (V05-31)."""

from __future__ import annotations

import importlib
import json
import socket
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import aiosqlite
import pytest

from sovyx.upgrade import doctor as _doctor_mod  # anti-pattern #11
from sovyx.upgrade.doctor import (
    _DEFAULT_PORT,
    DiagnosticReport,
    DiagnosticResult,
    DiagnosticStatus,
    Doctor,
    _check_brain_consistency,
    _check_config_valid,
    _check_data_dir_writable,
    _check_db_integrity,
    _check_dependency_versions,
    _check_disk_space,
    _check_memory_usage,
    _check_model_files,
    _check_port_available,
    _check_python_version,
    _check_schema_version,
    _check_voice_capture_apo,
    _check_voice_kernel_invalidated,
)

# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    """Create a temporary data directory."""
    d = tmp_path / ".sovyx"
    d.mkdir()
    return d


@pytest.fixture()
async def db_path(data_dir: Path) -> Path:
    """Create a real SQLite database with brain tables."""
    p = data_dir / "brain.db"
    async with aiosqlite.connect(str(p)) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS _schema_version (
                version TEXT NOT NULL,
                applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                checksum TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                duration_ms INTEGER
            )
        """)
        await db.execute(
            "INSERT INTO _schema_version (version, checksum) VALUES (?, ?)",
            ("0.5.0", "abc123"),
        )
        await db.execute("""
            CREATE TABLE IF NOT EXISTS concepts (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                category TEXT DEFAULT 'general'
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS relations (
                id INTEGER PRIMARY KEY,
                source_id INTEGER NOT NULL,
                target_id INTEGER NOT NULL
            )
        """)
        # Add some concepts with relations
        await db.execute("INSERT INTO concepts (id, name) VALUES (1, 'alpha')")
        await db.execute("INSERT INTO concepts (id, name) VALUES (2, 'beta')")
        await db.execute("INSERT INTO concepts (id, name) VALUES (3, 'gamma')")
        await db.execute("INSERT INTO relations (source_id, target_id) VALUES (1, 2)")
        await db.commit()
    return p


@pytest.fixture()
def config_path(data_dir: Path) -> Path:
    """Create a valid system.yaml config file."""
    p = data_dir / "system.yaml"
    p.write_text("log_level: INFO\nhost: 0.0.0.0\nport: 7777\n")
    return p


# ── DiagnosticResult Tests ────────────────────────────────────────────


class TestDiagnosticResult:
    """Tests for DiagnosticResult dataclass."""

    def test_to_dict_minimal(self) -> None:
        result = DiagnosticResult(
            check="test_check",
            status=DiagnosticStatus.PASS,
            message="All good",
        )
        d = result.to_dict()
        assert d["check"] == "test_check"
        assert d["status"] == "pass"
        assert d["message"] == "All good"
        assert "fix_suggestion" not in d
        assert "details" not in d

    def test_to_dict_full(self) -> None:
        result = DiagnosticResult(
            check="test_check",
            status=DiagnosticStatus.FAIL,
            message="Something broke",
            fix_suggestion="Fix it",
            details={"key": "value"},
        )
        d = result.to_dict()
        assert d["fix_suggestion"] == "Fix it"
        assert d["details"] == {"key": "value"}

    def test_frozen(self) -> None:
        result = DiagnosticResult(check="x", status=DiagnosticStatus.PASS, message="y")
        with pytest.raises(AttributeError):
            result.check = "z"  # type: ignore[misc]


class TestDiagnosticStatus:
    """Tests for DiagnosticStatus enum."""

    def test_values(self) -> None:
        assert DiagnosticStatus.PASS.value == "pass"
        assert DiagnosticStatus.WARN.value == "warn"
        assert DiagnosticStatus.FAIL.value == "fail"

    def test_string_value(self) -> None:
        assert isinstance(DiagnosticStatus.PASS.value, str)
        assert DiagnosticStatus.PASS.value == "pass"


# ── DiagnosticReport Tests ────────────────────────────────────────────


class TestDiagnosticReport:
    """Tests for DiagnosticReport dataclass."""

    def test_counts(self) -> None:
        results = (
            DiagnosticResult(check="a", status=DiagnosticStatus.PASS, message="ok"),
            DiagnosticResult(check="b", status=DiagnosticStatus.PASS, message="ok"),
            DiagnosticResult(check="c", status=DiagnosticStatus.WARN, message="meh"),
            DiagnosticResult(check="d", status=DiagnosticStatus.FAIL, message="bad"),
        )
        report = DiagnosticReport(results=results)
        assert report.passed == 2
        assert report.warned == 1
        assert report.failed == 1
        assert report.healthy is False

    def test_healthy_when_no_failures(self) -> None:
        results = (
            DiagnosticResult(check="a", status=DiagnosticStatus.PASS, message="ok"),
            DiagnosticResult(check="b", status=DiagnosticStatus.WARN, message="meh"),
        )
        report = DiagnosticReport(results=results)
        assert report.healthy is True

    def test_empty_report(self) -> None:
        report = DiagnosticReport(results=())
        assert report.passed == 0
        assert report.warned == 0
        assert report.failed == 0
        assert report.healthy is True

    def test_to_dict(self) -> None:
        results = (DiagnosticResult(check="a", status=DiagnosticStatus.PASS, message="ok"),)
        report = DiagnosticReport(results=results)
        d = report.to_dict()
        assert d["healthy"] is True
        assert d["passed"] == 1
        assert d["warned"] == 0
        assert d["failed"] == 0
        assert len(d["results"]) == 1

    def test_to_json(self) -> None:
        results = (DiagnosticResult(check="a", status=DiagnosticStatus.PASS, message="ok"),)
        report = DiagnosticReport(results=results)
        j = report.to_json()
        parsed = json.loads(j)
        assert parsed["healthy"] is True

    def test_to_json_indent(self) -> None:
        results = (DiagnosticResult(check="a", status=DiagnosticStatus.PASS, message="ok"),)
        report = DiagnosticReport(results=results)
        j = report.to_json(indent=4)
        # Check indentation is 4 spaces
        assert "    " in j


# ── Database Integrity Check ──────────────────────────────────────────


class TestDbIntegrity:
    """Tests for _check_db_integrity."""

    @pytest.mark.asyncio()
    async def test_pass_healthy_db(self, db_path: Path) -> None:
        result = await _check_db_integrity(db_path)
        assert result.check == "db_integrity"
        assert result.status == DiagnosticStatus.PASS

    @pytest.mark.asyncio()
    async def test_warn_missing_db(self, tmp_path: Path) -> None:
        result = await _check_db_integrity(tmp_path / "nonexistent.db")
        assert result.status == DiagnosticStatus.WARN
        assert "not found" in result.message

    @pytest.mark.asyncio()
    async def test_fail_corrupt_db(self, data_dir: Path) -> None:
        corrupt = data_dir / "corrupt.db"
        corrupt.write_bytes(b"not a database")
        result = await _check_db_integrity(corrupt)
        assert result.status == DiagnosticStatus.FAIL

    @pytest.mark.asyncio()
    async def test_fail_integrity_not_ok(self, data_dir: Path) -> None:
        """Cover the branch where PRAGMA integrity_check returns non-'ok'."""
        p = data_dir / "bad_integrity.db"
        async with aiosqlite.connect(str(p)) as db:
            await db.execute("CREATE TABLE t (id INTEGER)")
            await db.commit()

        class _FakeConn:
            async def __aenter__(self) -> _FakeConn:
                return self

            async def __aexit__(self, *a: object) -> None:
                pass

            async def execute(self, sql: str) -> _FakeConn:
                return self

            async def fetchone(self) -> tuple[str]:
                return ("*** corruption detected ***",)

        with patch("aiosqlite.connect", return_value=_FakeConn()):
            result = await _check_db_integrity(p)
        assert result.status == DiagnosticStatus.FAIL
        assert "corruption" in result.message

    @pytest.mark.asyncio()
    async def test_fix_suggestion_on_failure(self, data_dir: Path) -> None:
        corrupt = data_dir / "corrupt.db"
        corrupt.write_bytes(b"not a database")
        result = await _check_db_integrity(corrupt)
        assert result.fix_suggestion is not None


# ── Schema Version Check ─────────────────────────────────────────────


class TestSchemaVersion:
    """Tests for _check_schema_version."""

    @pytest.mark.asyncio()
    async def test_pass_valid_version(self, db_path: Path) -> None:
        result = await _check_schema_version(db_path)
        assert result.status == DiagnosticStatus.PASS
        assert result.details is not None
        assert result.details["version"] == "0.5.0"

    @pytest.mark.asyncio()
    async def test_warn_missing_db(self, tmp_path: Path) -> None:
        result = await _check_schema_version(tmp_path / "nonexistent.db")
        assert result.status == DiagnosticStatus.WARN

    @pytest.mark.asyncio()
    async def test_warn_no_table(self, data_dir: Path) -> None:
        p = data_dir / "empty.db"
        async with aiosqlite.connect(str(p)) as db:
            await db.execute("CREATE TABLE dummy (id INTEGER)")
            await db.commit()
        result = await _check_schema_version(p)
        assert result.status == DiagnosticStatus.WARN
        assert "not found" in result.message

    @pytest.mark.asyncio()
    async def test_warn_empty_table(self, data_dir: Path) -> None:
        p = data_dir / "noversion.db"
        async with aiosqlite.connect(str(p)) as db:
            await db.execute("""
                CREATE TABLE _schema_version (
                    version TEXT NOT NULL,
                    applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    checksum TEXT NOT NULL
                )
            """)
            await db.commit()
        result = await _check_schema_version(p)
        assert result.status == DiagnosticStatus.WARN
        assert "no entries" in result.message

    @pytest.mark.asyncio()
    async def test_fail_invalid_version_format(self, data_dir: Path) -> None:
        p = data_dir / "badver.db"
        async with aiosqlite.connect(str(p)) as db:
            await db.execute("""
                CREATE TABLE _schema_version (
                    version TEXT NOT NULL,
                    applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    checksum TEXT NOT NULL
                )
            """)
            await db.execute(
                "INSERT INTO _schema_version (version, checksum) VALUES (?, ?)",
                ("not-semver", "abc"),
            )
            await db.commit()
        result = await _check_schema_version(p)
        assert result.status == DiagnosticStatus.FAIL
        assert "Invalid" in result.message


# ── Brain Consistency Check ───────────────────────────────────────────


class TestBrainConsistency:
    """Tests for _check_brain_consistency."""

    @pytest.mark.asyncio()
    async def test_pass_consistent(self, db_path: Path) -> None:
        result = await _check_brain_consistency(db_path)
        assert result.status == DiagnosticStatus.PASS
        assert result.details is not None
        # concept 3 (gamma) has no relations → orphaned=1
        assert result.details["orphaned"] == 1

    @pytest.mark.asyncio()
    async def test_pass_empty_brain(self, data_dir: Path) -> None:
        p = data_dir / "empty_brain.db"
        async with aiosqlite.connect(str(p)) as db:
            await db.execute("CREATE TABLE concepts (id INTEGER PRIMARY KEY, name TEXT)")
            await db.commit()
        result = await _check_brain_consistency(p)
        assert result.status == DiagnosticStatus.PASS
        assert "empty" in result.message.lower()

    @pytest.mark.asyncio()
    async def test_pass_no_brain_tables(self, data_dir: Path) -> None:
        p = data_dir / "no_brain.db"
        async with aiosqlite.connect(str(p)) as db:
            await db.execute("CREATE TABLE dummy (id INTEGER)")
            await db.commit()
        result = await _check_brain_consistency(p)
        assert result.status == DiagnosticStatus.PASS
        assert "fresh install" in result.message.lower()

    @pytest.mark.asyncio()
    async def test_warn_high_orphan_ratio(self, data_dir: Path) -> None:
        p = data_dir / "orphans.db"
        async with aiosqlite.connect(str(p)) as db:
            await db.execute("CREATE TABLE concepts (id INTEGER PRIMARY KEY, name TEXT)")
            await db.execute(
                "CREATE TABLE relations "
                "(id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER)"
            )
            # 20 concepts, 2 related, 18 orphaned (>50%)
            for i in range(1, 21):
                await db.execute("INSERT INTO concepts (id, name) VALUES (?, ?)", (i, f"c{i}"))
            await db.execute("INSERT INTO relations (source_id, target_id) VALUES (1, 2)")
            await db.commit()
        result = await _check_brain_consistency(p)
        assert result.status == DiagnosticStatus.WARN
        assert "orphan" in result.message.lower()

    @pytest.mark.asyncio()
    async def test_warn_missing_db(self, tmp_path: Path) -> None:
        result = await _check_brain_consistency(tmp_path / "nope.db")
        assert result.status == DiagnosticStatus.WARN


# ── Config Valid Check ────────────────────────────────────────────────


class TestConfigValid:
    """Tests for _check_config_valid."""

    def test_pass_valid_yaml(self, config_path: Path) -> None:
        result = _check_config_valid(config_path)
        assert result.status == DiagnosticStatus.PASS
        assert result.details is not None
        assert "log_level" in result.details["keys"]

    def test_warn_missing_config(self, tmp_path: Path) -> None:
        result = _check_config_valid(tmp_path / "nope.yaml")
        assert result.status == DiagnosticStatus.WARN

    def test_warn_empty_config(self, data_dir: Path) -> None:
        p = data_dir / "empty.yaml"
        p.write_text("")
        result = _check_config_valid(p)
        assert result.status == DiagnosticStatus.WARN
        assert "empty" in result.message.lower()

    def test_fail_invalid_yaml(self, data_dir: Path) -> None:
        p = data_dir / "bad.yaml"
        p.write_text(":::invalid yaml[[[")
        result = _check_config_valid(p)
        assert result.status == DiagnosticStatus.FAIL

    def test_fail_non_dict_yaml(self, data_dir: Path) -> None:
        p = data_dir / "list.yaml"
        p.write_text("- item1\n- item2\n")
        result = _check_config_valid(p)
        assert result.status == DiagnosticStatus.FAIL
        assert "mapping" in result.message.lower()


# ── Disk Space Check ──────────────────────────────────────────────────


class TestDiskSpace:
    """Tests for _check_disk_space."""

    def test_pass_normal(self, data_dir: Path) -> None:
        result = _check_disk_space(data_dir)
        # On any reasonable system, this should pass
        assert result.status in (DiagnosticStatus.PASS, DiagnosticStatus.WARN)
        assert result.details is not None
        assert "free_mb" in result.details

    def test_fail_low_space(self, data_dir: Path) -> None:
        # Mock shutil.disk_usage to return low space
        fake_usage = MagicMock()
        fake_usage.free = 50 * 1024 * 1024  # 50MB
        fake_usage.total = 100_000 * 1024 * 1024
        fake_usage.used = 99_950 * 1024 * 1024
        with patch("sovyx.upgrade.doctor.shutil.disk_usage", return_value=fake_usage):
            result = _check_disk_space(data_dir)
        assert result.status == DiagnosticStatus.FAIL
        assert result.fix_suggestion is not None

    def test_warn_moderate_space(self, data_dir: Path) -> None:
        fake_usage = MagicMock()
        fake_usage.free = 200 * 1024 * 1024  # 200MB
        fake_usage.total = 100_000 * 1024 * 1024
        fake_usage.used = 99_800 * 1024 * 1024
        with patch("sovyx.upgrade.doctor.shutil.disk_usage", return_value=fake_usage):
            result = _check_disk_space(data_dir)
        assert result.status == DiagnosticStatus.WARN

    def test_fallback_to_root(self, tmp_path: Path) -> None:
        nonexistent = tmp_path / "does_not_exist"
        result = _check_disk_space(nonexistent)
        # Should fall back to "/" and still work
        assert result.status in (DiagnosticStatus.PASS, DiagnosticStatus.WARN)


# ── Memory Usage Check ────────────────────────────────────────────────


class TestMemoryUsage:
    """Tests for _check_memory_usage (psutil-based)."""

    def test_pass_normal(self) -> None:
        result = _check_memory_usage()
        assert result.check == "memory_usage"
        assert result.details is not None
        assert "rss_mb" in result.details

    def test_fail_high_memory(self) -> None:
        import psutil

        mock_mem_info = MagicMock()
        mock_mem_info.rss = 3_500 * 1024 * 1024  # 3.5GB
        mock_proc = MagicMock()
        mock_proc.memory_info.return_value = mock_mem_info
        mock_vm = MagicMock()
        mock_vm.total = 4000 * 1024 * 1024  # 4GB
        with (
            patch.object(psutil, "Process", return_value=mock_proc),
            patch.object(psutil, "virtual_memory", return_value=mock_vm),
        ):
            result = _check_memory_usage()
        assert result.status == DiagnosticStatus.FAIL

    def test_warn_elevated_memory(self) -> None:
        import psutil

        mock_mem_info = MagicMock()
        mock_mem_info.rss = 2_900 * 1024 * 1024  # 2.9GB
        mock_proc = MagicMock()
        mock_proc.memory_info.return_value = mock_mem_info
        mock_vm = MagicMock()
        mock_vm.total = 4000 * 1024 * 1024  # 4GB
        with (
            patch.object(psutil, "Process", return_value=mock_proc),
            patch.object(psutil, "virtual_memory", return_value=mock_vm),
        ):
            result = _check_memory_usage()
        assert result.status == DiagnosticStatus.WARN


# ── Model Files Check ─────────────────────────────────────────────────


class TestModelFiles:
    """Tests for _check_model_files."""

    def test_warn_no_models_dir(self, data_dir: Path) -> None:
        result = _check_model_files(data_dir)
        assert result.status == DiagnosticStatus.WARN
        assert "not found" in result.message.lower()

    def test_warn_empty_models_dir(self, data_dir: Path) -> None:
        (data_dir / "models").mkdir()
        result = _check_model_files(data_dir)
        assert result.status == DiagnosticStatus.WARN

    def test_pass_with_model_files(self, data_dir: Path) -> None:
        models = data_dir / "models"
        models.mkdir()
        (models / "vad.onnx").write_bytes(b"fake")
        (models / "stt.bin").write_bytes(b"fake")
        result = _check_model_files(data_dir)
        assert result.status == DiagnosticStatus.PASS
        assert result.details is not None
        assert len(result.details["found"]) == 2

    def test_finds_nested_models(self, data_dir: Path) -> None:
        models = data_dir / "models" / "voice"
        models.mkdir(parents=True)
        (models / "tiny.onnx").write_bytes(b"fake")
        result = _check_model_files(data_dir)
        assert result.status == DiagnosticStatus.PASS
        assert len(result.details["found"]) == 1  # type: ignore[index]


# ── Port Available Check ──────────────────────────────────────────────


class TestPortAvailable:
    """Tests for _check_port_available."""

    def test_pass_free_port(self) -> None:
        # Use a random high port that's likely free
        result = _check_port_available(59999)
        assert result.status == DiagnosticStatus.PASS
        assert result.details is not None
        assert result.details["in_use"] is False

    def test_warn_port_in_use(self) -> None:
        # Bind a port and check
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", 0))
            sock.listen(1)
            port = sock.getsockname()[1]
            result = _check_port_available(port)
        assert result.status == DiagnosticStatus.WARN
        assert result.details is not None
        assert result.details["in_use"] is True


# ── Python Version Check ─────────────────────────────────────────────


class TestPythonVersion:
    """Tests for _check_python_version."""

    def test_pass_current_version(self) -> None:
        result = _check_python_version()
        assert result.status == DiagnosticStatus.PASS
        assert result.details is not None
        assert result.details["current"] == (
            f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        )

    def test_fail_old_version(self) -> None:
        with patch.object(_doctor_mod, "sys") as mock_sys:
            mock_sys.version_info = (3, 9, 0, "final", 0)
            result = _check_python_version()
        assert result.status == DiagnosticStatus.FAIL
        assert "below" in result.message.lower()


# ── Dependency Versions Check ─────────────────────────────────────────


class TestDependencyVersions:
    """Tests for _check_dependency_versions."""

    def test_pass_all_installed(self) -> None:
        result = _check_dependency_versions()
        assert result.status == DiagnosticStatus.PASS
        assert result.details is not None
        assert len(result.details["missing"]) == 0

    def test_fail_missing_dep(self) -> None:
        original_import = importlib.import_module

        def mock_import(name: str) -> object:
            if name == "tiktoken":
                raise ImportError("no tiktoken")
            return original_import(name)

        with patch("sovyx.upgrade.doctor.importlib.import_module", side_effect=mock_import):
            result = _check_dependency_versions()
        assert result.status == DiagnosticStatus.FAIL
        assert "tiktoken" in result.details["missing"]  # type: ignore[index]


# ── Data Dir Writable Check ───────────────────────────────────────────


class TestDataDirWritable:
    """Tests for _check_data_dir_writable."""

    def test_pass_writable(self, data_dir: Path) -> None:
        result = _check_data_dir_writable(data_dir)
        assert result.status == DiagnosticStatus.PASS

    def test_warn_missing_dir(self, tmp_path: Path) -> None:
        result = _check_data_dir_writable(tmp_path / "nonexistent")
        assert result.status == DiagnosticStatus.WARN

    def test_fail_not_writable(self, data_dir: Path) -> None:
        with patch.object(Path, "write_text", side_effect=OSError("Permission denied")):
            result = _check_data_dir_writable(data_dir)
        assert result.status == DiagnosticStatus.FAIL


# ── Doctor Integration ────────────────────────────────────────────────


class TestDoctor:
    """Tests for the Doctor class."""

    def test_init_defaults(self) -> None:
        doc = Doctor()
        assert doc.port == _DEFAULT_PORT
        assert doc.data_dir == Path.home() / ".sovyx"

    def test_init_custom(self, data_dir: Path, config_path: Path) -> None:
        doc = Doctor(data_dir=data_dir, config_path=config_path, port=9999, db_name="test.db")
        assert doc.data_dir == data_dir
        assert doc.config_path == config_path
        assert doc.port == 9999
        assert doc.db_path == data_dir / "test.db"

    @pytest.mark.asyncio()
    async def test_run_all_returns_report(self, db_path: Path, config_path: Path) -> None:
        data_dir = db_path.parent
        doc = Doctor(data_dir=data_dir, config_path=config_path)
        report = await doc.run_all()
        assert isinstance(report, DiagnosticReport)
        assert len(report.results) == 13  # noqa: PLR2004
        assert report.passed > 0

    @pytest.mark.asyncio()
    async def test_run_all_check_names(self, db_path: Path, config_path: Path) -> None:
        data_dir = db_path.parent
        doc = Doctor(data_dir=data_dir, config_path=config_path)
        report = await doc.run_all()
        check_names = {r.check for r in report.results}
        expected = {
            "db_integrity",
            "schema_version_valid",
            "brain_consistency",
            "config_valid",
            "disk_space",
            "memory_usage",
            "model_files_present",
            "port_available",
            "python_version",
            "dependency_versions",
            "data_dir_writable",
            "voice_capture_apo",
            "voice_capture_kernel_invalidated",
        }
        assert check_names == expected

    @pytest.mark.asyncio()
    async def test_run_check_single(self, db_path: Path) -> None:
        data_dir = db_path.parent
        doc = Doctor(data_dir=data_dir)
        result = await doc.run_check("python_version")
        assert result.check == "python_version"
        assert result.status == DiagnosticStatus.PASS

    @pytest.mark.asyncio()
    async def test_run_check_unknown(self, data_dir: Path) -> None:
        doc = Doctor(data_dir=data_dir)
        with pytest.raises(ValueError, match="Unknown check"):
            await doc.run_check("nonexistent_check")

    def test_list_checks(self, data_dir: Path) -> None:
        doc = Doctor(data_dir=data_dir)
        checks = doc.list_checks()
        assert len(checks) == 13  # noqa: PLR2004
        # Should be sorted
        assert checks == sorted(checks)
        assert "db_integrity" in checks
        assert "python_version" in checks

    @pytest.mark.asyncio()
    async def test_report_serialization_roundtrip(self, db_path: Path, config_path: Path) -> None:
        data_dir = db_path.parent
        doc = Doctor(data_dir=data_dir, config_path=config_path)
        report = await doc.run_all()
        j = report.to_json()
        parsed = json.loads(j)
        assert isinstance(parsed["healthy"], bool)
        assert isinstance(parsed["results"], list)
        assert len(parsed["results"]) == 13  # noqa: PLR2004
        for r in parsed["results"]:
            assert "check" in r
            assert "status" in r
            assert "message" in r

    @pytest.mark.asyncio()
    async def test_run_check_all_names(self, db_path: Path) -> None:
        """Ensure every check name from list_checks() is runnable."""
        data_dir = db_path.parent
        doc = Doctor(data_dir=data_dir)
        for name in doc.list_checks():
            result = await doc.run_check(name)
            assert result.check == name

    @pytest.mark.asyncio()
    async def test_healthy_report(self, db_path: Path, config_path: Path) -> None:
        """With valid DB and config, report should be healthy (no failures)."""
        data_dir = db_path.parent
        doc = Doctor(data_dir=data_dir, config_path=config_path)
        report = await doc.run_all()
        # There might be warnings (no models dir, port in use, etc.) but no failures
        for r in report.results:
            if r.status == DiagnosticStatus.FAIL:
                pytest.fail(f"Unexpected failure: {r.check} — {r.message}")


# ── Exception / Edge Path Coverage ────────────────────────────────────


class TestExceptionPaths:
    """Cover error branches and exception handlers."""

    @pytest.mark.asyncio()
    async def test_schema_version_exception(self, data_dir: Path) -> None:
        """Cover exception handler in _check_schema_version."""
        p = data_dir / "exc.db"
        p.write_text("x")  # exists but corrupt
        with patch("aiosqlite.connect", side_effect=Exception("connection failed")):
            result = await _check_schema_version(p)
        assert result.status == DiagnosticStatus.FAIL
        assert "error" in result.message.lower()

    @pytest.mark.asyncio()
    async def test_brain_consistency_exception(self, data_dir: Path) -> None:
        """Cover exception handler in _check_brain_consistency."""
        p = data_dir / "exc.db"
        p.write_text("x")
        with patch("aiosqlite.connect", side_effect=Exception("db error")):
            result = await _check_brain_consistency(p)
        assert result.status == DiagnosticStatus.FAIL
        assert result.fix_suggestion is not None

    def test_config_valid_exception(self, data_dir: Path) -> None:
        """Cover exception in yaml loading."""
        import yaml

        p = data_dir / "broken.yaml"
        p.write_text("valid: true")
        with patch.object(yaml, "safe_load", side_effect=Exception("parse err")):
            result = _check_config_valid(p)
        assert result.status == DiagnosticStatus.FAIL
        assert "error" in result.message.lower()

    def test_disk_space_os_error(self, data_dir: Path) -> None:
        """Cover OSError in disk_space."""
        with patch(
            "sovyx.upgrade.doctor.shutil.disk_usage",
            side_effect=OSError("disk error"),
        ):
            result = _check_disk_space(data_dir)
        assert result.status == DiagnosticStatus.FAIL

    def test_memory_usage_exception(self) -> None:
        """Cover generic exception in memory_usage."""
        import psutil

        with patch.object(psutil, "Process", side_effect=Exception("no psutil")):
            result = _check_memory_usage()
        assert result.status == DiagnosticStatus.FAIL
        assert result.fix_suggestion is not None

    def test_model_files_os_error(self, data_dir: Path) -> None:
        """Cover OSError in model file scanning."""
        models = data_dir / "models"
        models.mkdir()
        with patch.object(Path, "rglob", side_effect=OSError("perm denied")):
            result = _check_model_files(data_dir)
        # Should still return (warn because no files found due to error)
        assert result.status == DiagnosticStatus.WARN

    def test_port_check_os_error(self) -> None:
        """Cover OSError in port check."""
        with patch("sovyx.upgrade.doctor.socket.socket") as mock_sock:
            mock_instance = MagicMock()
            mock_instance.__enter__ = MagicMock(return_value=mock_instance)
            mock_instance.__exit__ = MagicMock(return_value=False)
            mock_instance.connect_ex.side_effect = OSError("socket error")
            mock_sock.return_value = mock_instance
            result = _check_port_available(7777)
        assert result.status == DiagnosticStatus.FAIL


# ── Voice capture APO check ───────────────────────────────────────────


class TestVoiceCaptureApoCheck:
    """Cover the Windows Voice Clarity detector integration."""

    def test_non_windows_passes(self) -> None:
        """Non-Windows platforms skip the check and return PASS."""
        with patch.object(_doctor_mod, "sys") as mock_sys:
            mock_sys.platform = "linux"
            result = _check_voice_capture_apo()
        assert result.status == DiagnosticStatus.PASS
        assert result.check == "voice_capture_apo"
        assert "Windows" in result.message

    def test_no_endpoints_passes(self) -> None:
        """Empty report (common on non-Windows CI) is a PASS."""
        with (
            patch.object(_doctor_mod, "sys") as mock_sys,
            patch(
                "sovyx.voice._apo_detector.detect_capture_apos",
                return_value=[],
            ),
        ):
            mock_sys.platform = "win32"
            result = _check_voice_capture_apo()
        assert result.status == DiagnosticStatus.PASS
        assert result.details is not None
        assert result.details["endpoints"] == []

    def test_voice_clarity_active_warns(self) -> None:
        """Active Voice Clarity on any endpoint yields WARN with fix."""
        from sovyx.voice._apo_detector import CaptureApoReport

        rep = CaptureApoReport(
            endpoint_id="{endpoint-guid}",
            endpoint_name="Microfone (Razer BlackShark V2 Pro)",
            enumerator="USB",
            fx_binding_count=3,
            known_apos=["Windows Voice Clarity"],
            raw_clsids=["{CF1DDA2C-3B93-4EFE-8AA9-DEB6F8D4FDF1}"],
            voice_clarity_active=True,
        )
        with (
            patch.object(_doctor_mod, "sys") as mock_sys,
            patch(
                "sovyx.voice._apo_detector.detect_capture_apos",
                return_value=[rep],
            ),
        ):
            mock_sys.platform = "win32"
            result = _check_voice_capture_apo()
        assert result.status == DiagnosticStatus.WARN
        assert "Razer BlackShark" in result.message
        assert result.fix_suggestion is not None
        assert "CAPTURE_WASAPI_EXCLUSIVE" in result.fix_suggestion
        assert result.details is not None
        assert result.details["affected"] == ["Microfone (Razer BlackShark V2 Pro)"]

    def test_inactive_voice_clarity_passes(self) -> None:
        """APOs present but Voice Clarity inactive → PASS."""
        from sovyx.voice._apo_detector import CaptureApoReport

        rep = CaptureApoReport(
            endpoint_id="{other}",
            endpoint_name="Built-in Microphone",
            enumerator="MMDevAPI",
            fx_binding_count=2,
            known_apos=["MS Voice Focus"],
            raw_clsids=[],
            voice_clarity_active=False,
        )
        with (
            patch.object(_doctor_mod, "sys") as mock_sys,
            patch(
                "sovyx.voice._apo_detector.detect_capture_apos",
                return_value=[rep],
            ),
        ):
            mock_sys.platform = "win32"
            result = _check_voice_capture_apo()
        assert result.status == DiagnosticStatus.PASS
        assert result.details is not None
        assert len(result.details["endpoints"]) == 1

    def test_detector_exception_warns(self) -> None:
        """Registry walk failure is a WARN, not a hard FAIL."""
        with (
            patch.object(_doctor_mod, "sys") as mock_sys,
            patch(
                "sovyx.voice._apo_detector.detect_capture_apos",
                side_effect=RuntimeError("registry locked"),
            ),
        ):
            mock_sys.platform = "win32"
            result = _check_voice_capture_apo()
        assert result.status == DiagnosticStatus.WARN
        assert "registry locked" in result.message

    @pytest.mark.asyncio()
    async def test_run_check_voice_capture_apo(self, data_dir: Path) -> None:
        """The Doctor exposes voice_capture_apo via run_check()."""
        doc = Doctor(data_dir=data_dir)
        result = await doc.run_check("voice_capture_apo")
        assert result.check == "voice_capture_apo"
        # Status is platform-dependent: PASS on non-Windows CI.
        assert result.status in {
            DiagnosticStatus.PASS,
            DiagnosticStatus.WARN,
        }


# ── Voice kernel-invalidated quarantine check ─────────────────────────


class TestKernelInvalidatedCheck:
    """Cover §4.4.7 quarantine surface in ``sovyx doctor``."""

    @pytest.fixture(autouse=True)
    def _reset_singleton(self) -> None:
        """Isolate tests from the process-wide quarantine singleton."""
        from sovyx.voice.health import reset_default_quarantine

        reset_default_quarantine()
        yield
        reset_default_quarantine()

    def test_empty_quarantine_passes(self) -> None:
        """No quarantined endpoints → PASS with zero count."""
        result = _check_voice_kernel_invalidated()
        assert result.status == DiagnosticStatus.PASS
        assert result.check == "voice_capture_kernel_invalidated"
        assert result.details is not None
        assert result.details["quarantined_count"] == 0
        assert result.fix_suggestion is None

    def test_populated_quarantine_warns(self) -> None:
        """Quarantined endpoint → WARN with endpoint detail + replug fix."""
        from sovyx.voice.health import get_default_quarantine

        q = get_default_quarantine(quarantine_s=60.0)
        q.add(
            endpoint_guid="{razer-guid}",
            device_friendly_name="Microfone (Razer BlackShark V2 Pro)",
            device_interface_name=r"\\?\USB#VID_1532&PID_0529",
            host_api="Windows WASAPI",
            reason="probe",
        )

        result = _check_voice_kernel_invalidated()

        assert result.status == DiagnosticStatus.WARN
        assert result.check == "voice_capture_kernel_invalidated"
        assert "Razer BlackShark" in result.message
        assert "kernel-invalidated" in result.message
        assert result.fix_suggestion is not None
        assert "unplug" in result.fix_suggestion.lower()
        assert "reboot" in result.fix_suggestion.lower()
        assert result.details is not None
        assert result.details["quarantined_count"] == 1
        endpoints = result.details["endpoints"]
        assert len(endpoints) == 1
        assert endpoints[0]["endpoint_guid"] == "{razer-guid}"
        assert endpoints[0]["device_friendly_name"] == ("Microfone (Razer BlackShark V2 Pro)")
        assert endpoints[0]["host_api"] == "Windows WASAPI"
        assert endpoints[0]["reason"] == "probe"

    def test_populated_falls_back_to_guid_when_no_friendly_name(self) -> None:
        """Missing friendly name → the GUID appears in the message."""
        from sovyx.voice.health import get_default_quarantine

        q = get_default_quarantine(quarantine_s=60.0)
        q.add(endpoint_guid="{anon-guid}", reason="watchdog_recheck")

        result = _check_voice_kernel_invalidated()
        assert result.status == DiagnosticStatus.WARN
        assert "{anon-guid}" in result.message

    def test_snapshot_exception_warns_with_daemon_hint(self) -> None:
        """A raising ``snapshot()`` → WARN with start-the-daemon hint."""
        with patch(
            "sovyx.voice.health.get_default_quarantine",
            side_effect=RuntimeError("voice subsystem offline"),
        ):
            result = _check_voice_kernel_invalidated()

        assert result.status == DiagnosticStatus.WARN
        assert result.check == "voice_capture_kernel_invalidated"
        assert "voice subsystem offline" in result.message
        assert result.fix_suggestion is not None
        assert "daemon" in result.fix_suggestion.lower()

    @pytest.mark.asyncio()
    async def test_run_check_dispatch(self, data_dir: Path) -> None:
        """``Doctor.run_check`` exposes the kernel_invalidated check."""
        doc = Doctor(data_dir=data_dir)
        result = await doc.run_check("voice_capture_kernel_invalidated")
        assert result.check == "voice_capture_kernel_invalidated"
        assert result.status == DiagnosticStatus.PASS
        assert result.details is not None
        assert result.details["quarantined_count"] == 0

    @pytest.mark.asyncio()
    async def test_run_check_dispatch_warns_when_populated(self, data_dir: Path) -> None:
        """``run_check`` returns WARN when the singleton has entries."""
        from sovyx.voice.health import get_default_quarantine

        get_default_quarantine(quarantine_s=60.0).add(
            endpoint_guid="{stuck}",
            device_friendly_name="Stuck Mic",
            host_api="Windows WASAPI",
        )

        doc = Doctor(data_dir=data_dir)
        result = await doc.run_check("voice_capture_kernel_invalidated")

        assert result.status == DiagnosticStatus.WARN
        assert result.details is not None
        assert result.details["quarantined_count"] == 1
