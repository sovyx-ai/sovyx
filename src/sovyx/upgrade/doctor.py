"""Doctor diagnostic suite — 10+ health checks for Sovyx installations.

Provides :class:`Doctor` which runs a comprehensive set of checks covering
database integrity, schema validation, brain consistency, configuration,
disk space, memory usage, model files, port availability, Python version,
and dependency versions.

Each check produces a :class:`DiagnosticResult` with structured output
suitable for CLI display and JSON serialization.

Ref: SPE-028 §7, SPE-015 §doctor.
"""

from __future__ import annotations

import dataclasses
import importlib
import json
import platform
import re
import shutil
import socket
import sys
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

from sovyx.observability.logging import get_logger

logger = get_logger(__name__)


# ── Enums & Data classes ──────────────────────────────────────────────


class DiagnosticStatus(StrEnum):
    """Outcome of a single diagnostic check."""

    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


@dataclasses.dataclass(frozen=True)
class DiagnosticResult:
    """Result of a single diagnostic check.

    Attributes:
        check: Machine-readable check identifier (e.g. ``"db_integrity"``).
        status: Outcome — pass, warn, or fail.
        message: Human-readable description of the result.
        fix_suggestion: Optional suggestion for resolving a warn/fail.
        details: Optional extra structured data.
    """

    check: str
    status: DiagnosticStatus
    message: str
    fix_suggestion: str | None = None
    details: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dictionary."""
        result: dict[str, Any] = {
            "check": self.check,
            "status": self.status.value,
            "message": self.message,
        }
        if self.fix_suggestion is not None:
            result["fix_suggestion"] = self.fix_suggestion
        if self.details is not None:
            result["details"] = self.details
        return result


@dataclasses.dataclass(frozen=True)
class DiagnosticReport:
    """Aggregated results from all diagnostic checks.

    Attributes:
        results: List of individual check results.
        passed: Count of passed checks.
        warned: Count of warned checks.
        failed: Count of failed checks.
    """

    results: tuple[DiagnosticResult, ...]

    @property
    def passed(self) -> int:
        """Number of checks that passed."""
        return sum(1 for r in self.results if r.status == DiagnosticStatus.PASS)

    @property
    def warned(self) -> int:
        """Number of checks with warnings."""
        return sum(1 for r in self.results if r.status == DiagnosticStatus.WARN)

    @property
    def failed(self) -> int:
        """Number of checks that failed."""
        return sum(1 for r in self.results if r.status == DiagnosticStatus.FAIL)

    @property
    def healthy(self) -> bool:
        """True when no checks failed."""
        return self.failed == 0

    def to_dict(self) -> dict[str, Any]:
        """Serialize full report to JSON-compatible dictionary."""
        return {
            "healthy": self.healthy,
            "passed": self.passed,
            "warned": self.warned,
            "failed": self.failed,
            "results": [r.to_dict() for r in self.results],
        }

    def to_json(self, *, indent: int = 2) -> str:
        """Serialize full report to JSON string."""
        return json.dumps(self.to_dict(), indent=indent)


# ── Thresholds ────────────────────────────────────────────────────────

_MIN_DISK_MB = 100
_WARN_DISK_MB = 500
_MAX_RSS_PERCENT = 85
_WARN_RSS_PERCENT = 70
_MIN_PYTHON_VERSION = (3, 11)
_DEFAULT_PORT = 7777
_DEFAULT_DATA_DIR = Path.home() / ".sovyx"


# ── Individual Checks ─────────────────────────────────────────────────


async def _check_db_integrity(db_path: Path) -> DiagnosticResult:
    """Run ``PRAGMA integrity_check`` on the database.

    Args:
        db_path: Path to the SQLite database file.

    Returns:
        Diagnostic result indicating database health.
    """
    check_name = "db_integrity"
    if not db_path.exists():
        return DiagnosticResult(
            check=check_name,
            status=DiagnosticStatus.WARN,
            message=f"Database not found at {db_path}",
            fix_suggestion="Run 'sovyx init' to create the database.",
        )
    try:
        import aiosqlite as _aiosqlite

        async with _aiosqlite.connect(str(db_path)) as db:
            cursor = await db.execute("PRAGMA integrity_check")
            row = await cursor.fetchone()
            result_text = row[0] if row else "unknown"
            if result_text == "ok":
                return DiagnosticResult(
                    check=check_name,
                    status=DiagnosticStatus.PASS,
                    message="Database integrity check passed.",
                )
            return DiagnosticResult(
                check=check_name,
                status=DiagnosticStatus.FAIL,
                message=f"Database integrity check failed: {result_text}",
                fix_suggestion="Back up the database and run 'sovyx doctor --fix-db'.",
            )
    except Exception as exc:  # noqa: BLE001
        return DiagnosticResult(
            check=check_name,
            status=DiagnosticStatus.FAIL,
            message=f"Database integrity check error: {exc}",
            fix_suggestion="Ensure aiosqlite is installed and the database is accessible.",
        )


async def _check_schema_version(db_path: Path) -> DiagnosticResult:
    """Validate schema version table exists and contains valid entries.

    Args:
        db_path: Path to the SQLite database file.

    Returns:
        Diagnostic result for schema version validity.
    """
    check_name = "schema_version_valid"
    if not db_path.exists():
        return DiagnosticResult(
            check=check_name,
            status=DiagnosticStatus.WARN,
            message="Database not found — cannot check schema version.",
            fix_suggestion="Run 'sovyx init' to create the database.",
        )
    try:
        import aiosqlite as _aiosqlite

        async with _aiosqlite.connect(str(db_path)) as db:
            cursor = await db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='_schema_version'"
            )
            if not await cursor.fetchone():
                return DiagnosticResult(
                    check=check_name,
                    status=DiagnosticStatus.WARN,
                    message="Schema version table '_schema_version' not found.",
                    fix_suggestion="Run 'sovyx upgrade' to initialize schema tracking.",
                )
            cursor = await db.execute(
                "SELECT version, applied_at FROM _schema_version ORDER BY rowid DESC LIMIT 1"
            )
            row = await cursor.fetchone()
            if not row:
                return DiagnosticResult(
                    check=check_name,
                    status=DiagnosticStatus.WARN,
                    message="Schema version table exists but has no entries.",
                    fix_suggestion="Run 'sovyx upgrade' to apply migrations.",
                )
            version_str = row[0]
            # Validate semver pattern
            if not re.match(r"^\d+\.\d+\.\d+$", version_str):
                return DiagnosticResult(
                    check=check_name,
                    status=DiagnosticStatus.FAIL,
                    message=f"Invalid schema version format: '{version_str}'",
                    fix_suggestion="Schema version must follow MAJOR.MINOR.PATCH format.",
                )
            return DiagnosticResult(
                check=check_name,
                status=DiagnosticStatus.PASS,
                message=f"Schema version: {version_str} (applied: {row[1]})",
                details={"version": version_str, "applied_at": str(row[1])},
            )
    except Exception as exc:  # noqa: BLE001
        return DiagnosticResult(
            check=check_name,
            status=DiagnosticStatus.FAIL,
            message=f"Schema version check error: {exc}",
            fix_suggestion="Ensure the database is accessible.",
        )


async def _check_brain_consistency(db_path: Path) -> DiagnosticResult:
    """Check for orphaned concepts in the brain database.

    Looks for concepts that have no relations and no episode links.

    Args:
        db_path: Path to the SQLite database file.

    Returns:
        Diagnostic result for brain consistency.
    """
    check_name = "brain_consistency"
    if not db_path.exists():
        return DiagnosticResult(
            check=check_name,
            status=DiagnosticStatus.WARN,
            message="Database not found — cannot check brain consistency.",
            fix_suggestion="Run 'sovyx init' to create the database.",
        )
    try:
        import aiosqlite as _aiosqlite

        async with _aiosqlite.connect(str(db_path)) as db:
            # Check if concepts table exists
            cursor = await db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='concepts'"
            )
            if not await cursor.fetchone():
                return DiagnosticResult(
                    check=check_name,
                    status=DiagnosticStatus.PASS,
                    message="No brain tables found (fresh install).",
                )
            # Count total concepts
            cursor = await db.execute("SELECT COUNT(*) FROM concepts")
            row = await cursor.fetchone()
            total_concepts = row[0] if row else 0

            if total_concepts == 0:
                return DiagnosticResult(
                    check=check_name,
                    status=DiagnosticStatus.PASS,
                    message="Brain is empty (0 concepts).",
                    details={"total_concepts": 0, "orphaned": 0},
                )

            # Check for orphans: concepts with no relations at all
            # (not in source_id or target_id of relations table)
            cursor = await db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='relations'"
            )
            has_relations = bool(await cursor.fetchone())

            orphaned = 0
            if has_relations:
                cursor = await db.execute("""
                    SELECT COUNT(*) FROM concepts c
                    WHERE NOT EXISTS (
                        SELECT 1 FROM relations r
                        WHERE r.source_id = c.id OR r.target_id = c.id
                    )
                """)
                row = await cursor.fetchone()
                orphaned = row[0] if row else 0

            details = {"total_concepts": total_concepts, "orphaned": orphaned}

            if orphaned > total_concepts * 0.5 and total_concepts > 10:  # noqa: PLR2004
                return DiagnosticResult(
                    check=check_name,
                    status=DiagnosticStatus.WARN,
                    message=(
                        f"High orphan ratio: {orphaned}/{total_concepts} "
                        f"concepts have no relations."
                    ),
                    fix_suggestion="Run 'sovyx brain consolidate' to clean up orphans.",
                    details=details,
                )
            return DiagnosticResult(
                check=check_name,
                status=DiagnosticStatus.PASS,
                message=f"Brain consistent: {total_concepts} concepts, {orphaned} orphaned.",
                details=details,
            )
    except Exception as exc:  # noqa: BLE001
        return DiagnosticResult(
            check=check_name,
            status=DiagnosticStatus.FAIL,
            message=f"Brain consistency check error: {exc}",
            fix_suggestion="Ensure the database is accessible.",
        )


def _check_config_valid(config_path: Path) -> DiagnosticResult:
    """Validate the Sovyx configuration file.

    Args:
        config_path: Path to ``system.yaml``.

    Returns:
        Diagnostic result for configuration validity.
    """
    check_name = "config_valid"
    if not config_path.exists():
        return DiagnosticResult(
            check=check_name,
            status=DiagnosticStatus.WARN,
            message=f"Config file not found at {config_path}",
            fix_suggestion="Run 'sovyx init' to create a default configuration.",
        )
    try:
        import yaml

        with config_path.open() as fh:
            data = yaml.safe_load(fh)
        if data is None:
            return DiagnosticResult(
                check=check_name,
                status=DiagnosticStatus.WARN,
                message="Config file is empty.",
                fix_suggestion="Add configuration to system.yaml.",
            )
        if not isinstance(data, dict):
            return DiagnosticResult(
                check=check_name,
                status=DiagnosticStatus.FAIL,
                message="Config file does not contain a YAML mapping.",
                fix_suggestion="system.yaml must be a YAML dictionary at root level.",
            )
        return DiagnosticResult(
            check=check_name,
            status=DiagnosticStatus.PASS,
            message="Configuration file is valid YAML.",
            details={"keys": sorted(data.keys())},
        )
    except Exception as exc:  # noqa: BLE001
        return DiagnosticResult(
            check=check_name,
            status=DiagnosticStatus.FAIL,
            message=f"Config validation error: {exc}",
            fix_suggestion="Check system.yaml for YAML syntax errors.",
        )


def _check_disk_space(data_dir: Path) -> DiagnosticResult:
    """Check available disk space.

    Args:
        data_dir: Data directory to check disk space for.

    Returns:
        Diagnostic result for disk space.
    """
    check_name = "disk_space"
    try:
        target = data_dir if data_dir.exists() else Path("/")
        usage = shutil.disk_usage(target)
        free_mb = usage.free // (1024 * 1024)
        total_mb = usage.total // (1024 * 1024)
        used_percent = round((usage.used / usage.total) * 100, 1) if usage.total > 0 else 0.0
        details = {
            "free_mb": free_mb,
            "total_mb": total_mb,
            "used_percent": used_percent,
        }
        if free_mb < _MIN_DISK_MB:
            return DiagnosticResult(
                check=check_name,
                status=DiagnosticStatus.FAIL,
                message=f"Critically low disk space: {free_mb}MB free.",
                fix_suggestion="Free up disk space. Sovyx needs at least 100MB.",
                details=details,
            )
        if free_mb < _WARN_DISK_MB:
            return DiagnosticResult(
                check=check_name,
                status=DiagnosticStatus.WARN,
                message=f"Low disk space: {free_mb}MB free.",
                fix_suggestion="Consider freeing disk space.",
                details=details,
            )
        return DiagnosticResult(
            check=check_name,
            status=DiagnosticStatus.PASS,
            message=f"Disk space OK: {free_mb}MB free ({used_percent}% used).",
            details=details,
        )
    except OSError as exc:
        return DiagnosticResult(
            check=check_name,
            status=DiagnosticStatus.FAIL,
            message=f"Disk space check error: {exc}",
            fix_suggestion="Ensure the data directory is accessible.",
        )


def _check_memory_usage() -> DiagnosticResult:
    """Check process memory usage relative to total RAM.

    Returns:
        Diagnostic result for memory usage.
    """
    check_name = "memory_usage"
    try:
        import psutil  # noqa: PLC0415

        proc = psutil.Process()
        rss_mb = proc.memory_info().rss // (1024 * 1024)

        total_mb = psutil.virtual_memory().total // (1024 * 1024)
        rss_percent = round((rss_mb / total_mb) * 100, 1) if total_mb > 0 else 0.0

        details = {
            "rss_mb": rss_mb,
            "total_mb": total_mb,
            "rss_percent": rss_percent,
        }

        if rss_percent > _MAX_RSS_PERCENT:
            return DiagnosticResult(
                check=check_name,
                status=DiagnosticStatus.FAIL,
                message=f"High memory usage: {rss_mb}MB ({rss_percent}% of {total_mb}MB).",
                fix_suggestion="Consider restarting Sovyx or freeing memory.",
                details=details,
            )
        if rss_percent > _WARN_RSS_PERCENT:
            return DiagnosticResult(
                check=check_name,
                status=DiagnosticStatus.WARN,
                message=f"Elevated memory usage: {rss_mb}MB ({rss_percent}% of {total_mb}MB).",
                fix_suggestion="Monitor memory usage.",
                details=details,
            )
        return DiagnosticResult(
            check=check_name,
            status=DiagnosticStatus.PASS,
            message=f"Memory usage OK: {rss_mb}MB ({rss_percent}% of {total_mb}MB).",
            details=details,
        )
    except Exception as exc:  # noqa: BLE001
        return DiagnosticResult(
            check=check_name,
            status=DiagnosticStatus.FAIL,
            message=f"Memory usage check error: {exc}",
            fix_suggestion="Ensure the 'psutil' package is installed.",
        )


def _check_model_files(data_dir: Path) -> DiagnosticResult:
    """Check for presence of expected model files.

    Args:
        data_dir: Sovyx data directory (``~/.sovyx``).

    Returns:
        Diagnostic result for model file presence.
    """
    check_name = "model_files_present"
    models_dir = data_dir / "models"

    if not models_dir.exists():
        return DiagnosticResult(
            check=check_name,
            status=DiagnosticStatus.WARN,
            message="Models directory not found.",
            fix_suggestion="Models will be downloaded on first use.",
            details={"path": str(models_dir), "found": []},
        )

    # Look for any .onnx, .bin, .pt, .gguf model files
    model_extensions = {".onnx", ".bin", ".pt", ".gguf", ".model"}
    found_models: list[str] = []
    try:
        for item in models_dir.rglob("*"):
            if item.suffix in model_extensions and item.is_file():
                found_models.append(str(item.relative_to(models_dir)))
    except OSError:
        pass

    details = {"path": str(models_dir), "found": found_models}

    if not found_models:
        return DiagnosticResult(
            check=check_name,
            status=DiagnosticStatus.WARN,
            message="No model files found in models directory.",
            fix_suggestion="Models download on first use or run 'sovyx models download'.",
            details=details,
        )
    return DiagnosticResult(
        check=check_name,
        status=DiagnosticStatus.PASS,
        message=f"Found {len(found_models)} model file(s).",
        details=details,
    )


def _check_port_available(port: int) -> DiagnosticResult:
    """Check if the Sovyx API port is available.

    Args:
        port: Port number to check.

    Returns:
        Diagnostic result for port availability.
    """
    check_name = "port_available"
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(1.0)
            result = sock.connect_ex(("127.0.0.1", port))
            if result == 0:
                # Port is in use — could be Sovyx already running (that's OK)
                return DiagnosticResult(
                    check=check_name,
                    status=DiagnosticStatus.WARN,
                    message=f"Port {port} is already in use.",
                    fix_suggestion=(
                        f"Another process is using port {port}. "
                        "This is expected if Sovyx is already running."
                    ),
                    details={"port": port, "in_use": True},
                )
            return DiagnosticResult(
                check=check_name,
                status=DiagnosticStatus.PASS,
                message=f"Port {port} is available.",
                details={"port": port, "in_use": False},
            )
    except OSError as exc:
        return DiagnosticResult(
            check=check_name,
            status=DiagnosticStatus.FAIL,
            message=f"Port check error: {exc}",
            fix_suggestion=f"Ensure port {port} is accessible.",
            details={"port": port},
        )


def _check_python_version() -> DiagnosticResult:
    """Validate Python version meets minimum requirements.

    Returns:
        Diagnostic result for Python version.
    """
    check_name = "python_version"
    current = sys.version_info[:3]
    current_str = f"{current[0]}.{current[1]}.{current[2]}"
    min_str = f"{_MIN_PYTHON_VERSION[0]}.{_MIN_PYTHON_VERSION[1]}"
    details = {
        "current": current_str,
        "minimum": min_str,
        "platform": platform.platform(),
    }

    if (current[0], current[1]) < _MIN_PYTHON_VERSION:
        return DiagnosticResult(
            check=check_name,
            status=DiagnosticStatus.FAIL,
            message=f"Python {current_str} is below minimum {min_str}.",
            fix_suggestion=f"Upgrade to Python {min_str} or newer.",
            details=details,
        )
    return DiagnosticResult(
        check=check_name,
        status=DiagnosticStatus.PASS,
        message=f"Python {current_str} meets minimum {min_str}.",
        details=details,
    )


def _check_dependency_versions() -> DiagnosticResult:
    """Verify critical dependencies are installed and importable.

    Returns:
        Diagnostic result for dependency availability.
    """
    check_name = "dependency_versions"

    # Critical dependencies that must be importable
    required_deps: dict[str, str] = {
        "aiosqlite": "aiosqlite",
        "yaml": "pyyaml",
        "pydantic": "pydantic",
        "starlette": "starlette",
        "uvicorn": "uvicorn",
        "tiktoken": "tiktoken",
    }

    found: dict[str, str] = {}
    missing: list[str] = []

    for module_name, package_name in required_deps.items():
        try:
            mod = importlib.import_module(module_name)
            version = getattr(mod, "__version__", getattr(mod, "VERSION", "unknown"))
            found[package_name] = str(version)
        except ImportError:
            missing.append(package_name)

    details = {"found": found, "missing": missing}

    if missing:
        return DiagnosticResult(
            check=check_name,
            status=DiagnosticStatus.FAIL,
            message=f"Missing dependencies: {', '.join(missing)}",
            fix_suggestion="Run 'pip install sovyx' to install all dependencies.",
            details=details,
        )
    return DiagnosticResult(
        check=check_name,
        status=DiagnosticStatus.PASS,
        message=f"All {len(found)} critical dependencies installed.",
        details=details,
    )


def _check_voice_capture_apo() -> DiagnosticResult:
    """Scan the Windows capture-APO chain for the Voice Clarity package.

    Windows Voice Clarity (``VocaEffectPack`` / ``voiceclarityep``) began
    shipping via Windows Update in early 2026 and destroys the signal
    for downstream VAD on a significant fraction of hardware. When
    detected, the doctor surfaces the condition as ``WARN`` and points
    the operator at the durable fix — enabling
    ``capture_wasapi_exclusive`` (which the orchestrator also flips
    automatically via ``voice_clarity_autofix=True``).

    Always returns ``PASS`` on non-Windows platforms.

    Returns:
        Diagnostic result for the capture-APO chain.
    """
    check_name = "voice_capture_apo"
    if sys.platform != "win32":
        return DiagnosticResult(
            check=check_name,
            status=DiagnosticStatus.PASS,
            message="Capture APOs only apply on Windows — skipped.",
            details={"platform": sys.platform},
        )
    try:
        from sovyx.voice._apo_detector import detect_capture_apos

        reports = detect_capture_apos()
    except Exception as exc:  # noqa: BLE001
        return DiagnosticResult(
            check=check_name,
            status=DiagnosticStatus.WARN,
            message=f"Capture-APO scan failed: {exc}",
            fix_suggestion="Ensure the HKLM MMDevices registry key is readable.",
        )

    endpoints = [
        {
            "endpoint_id": r.endpoint_id,
            "endpoint_name": r.endpoint_name,
            "device_interface_name": r.device_interface_name,
            "enumerator": r.enumerator,
            "known_apos": list(r.known_apos),
            "voice_clarity_active": r.voice_clarity_active,
            "fx_binding_count": r.fx_binding_count,
        }
        for r in reports
    ]
    affected = [r.endpoint_name or r.endpoint_id for r in reports if r.voice_clarity_active]

    if affected:
        names = ", ".join(affected)
        return DiagnosticResult(
            check=check_name,
            status=DiagnosticStatus.WARN,
            message=(
                f"Windows Voice Clarity APO active on: {names}. "
                "Post-APO signal frequently blocks VAD."
            ),
            fix_suggestion=(
                "Set SOVYX_TUNING__VOICE__CAPTURE_WASAPI_EXCLUSIVE=true "
                "(or leave voice_clarity_autofix=true — default) so Sovyx "
                "opens the mic in WASAPI exclusive mode and bypasses the APO "
                "chain. Alternatively, disable 'Voice isolation' / 'Voice "
                "Clarity' in Windows Sound settings for the affected device."
            ),
            details={"endpoints": endpoints, "affected": affected},
        )
    return DiagnosticResult(
        check=check_name,
        status=DiagnosticStatus.PASS,
        message=f"No Voice Clarity APO detected across {len(reports)} active endpoint(s).",
        details={"endpoints": endpoints},
    )


def _check_data_dir_writable(data_dir: Path) -> DiagnosticResult:
    """Check that the data directory exists and is writable.

    Args:
        data_dir: Sovyx data directory.

    Returns:
        Diagnostic result for data directory writability.
    """
    check_name = "data_dir_writable"
    if not data_dir.exists():
        return DiagnosticResult(
            check=check_name,
            status=DiagnosticStatus.WARN,
            message=f"Data directory does not exist: {data_dir}",
            fix_suggestion="Run 'sovyx init' to create the data directory.",
            details={"path": str(data_dir)},
        )
    test_file = data_dir / ".doctor_write_test"
    try:
        test_file.write_text("ok")
        test_file.unlink()
        return DiagnosticResult(
            check=check_name,
            status=DiagnosticStatus.PASS,
            message=f"Data directory is writable: {data_dir}",
            details={"path": str(data_dir)},
        )
    except OSError as exc:
        return DiagnosticResult(
            check=check_name,
            status=DiagnosticStatus.FAIL,
            message=f"Data directory is not writable: {exc}",
            fix_suggestion=f"Check permissions on {data_dir}.",
            details={"path": str(data_dir)},
        )


# ── Doctor ────────────────────────────────────────────────────────────


class Doctor:
    """Comprehensive diagnostic suite for Sovyx installations.

    Runs 12 checks covering database, configuration, system resources,
    dependencies, and Windows capture-APO health. Results are aggregated
    into a :class:`DiagnosticReport`.

    Args:
        data_dir: Sovyx data directory (default: ``~/.sovyx``).
        config_path: Path to system.yaml (default: ``~/.sovyx/system.yaml``).
        port: API port to check (default: 7777).
        db_name: Database filename (default: ``brain.db``).
    """

    def __init__(
        self,
        data_dir: Path | None = None,
        config_path: Path | None = None,
        port: int = _DEFAULT_PORT,
        db_name: str = "brain.db",
    ) -> None:
        self._data_dir = data_dir or _DEFAULT_DATA_DIR
        self._config_path = config_path or (self._data_dir / "system.yaml")
        self._port = port
        self._db_path = self._data_dir / db_name

    @property
    def data_dir(self) -> Path:
        """Configured data directory."""
        return self._data_dir

    @property
    def db_path(self) -> Path:
        """Database file path."""
        return self._db_path

    @property
    def config_path(self) -> Path:
        """Configuration file path."""
        return self._config_path

    @property
    def port(self) -> int:
        """API port being checked."""
        return self._port

    async def run_all(self) -> DiagnosticReport:
        """Run all diagnostic checks.

        Returns:
            :class:`DiagnosticReport` with all check results.
        """
        results: list[DiagnosticResult] = []

        # Async checks (database)
        results.append(await _check_db_integrity(self._db_path))
        results.append(await _check_schema_version(self._db_path))
        results.append(await _check_brain_consistency(self._db_path))

        # Sync checks
        results.append(_check_config_valid(self._config_path))
        results.append(_check_disk_space(self._data_dir))
        results.append(_check_memory_usage())
        results.append(_check_model_files(self._data_dir))
        results.append(_check_port_available(self._port))
        results.append(_check_python_version())
        results.append(_check_dependency_versions())
        results.append(_check_data_dir_writable(self._data_dir))
        results.append(_check_voice_capture_apo())

        report = DiagnosticReport(results=tuple(results))
        logger.info(
            "Doctor diagnostic complete",
            passed=report.passed,
            warned=report.warned,
            failed=report.failed,
            healthy=report.healthy,
        )
        return report

    async def run_check(self, check_name: str) -> DiagnosticResult:
        """Run a single diagnostic check by name.

        Args:
            check_name: Machine-readable check identifier.

        Returns:
            :class:`DiagnosticResult` for the specified check.

        Raises:
            ValueError: If *check_name* is not a valid check.
        """
        check_map = self._build_check_map()
        if check_name not in check_map:
            valid = sorted(check_map.keys())
            msg = f"Unknown check '{check_name}'. Valid checks: {valid}"
            raise ValueError(msg)
        result: DiagnosticResult = await check_map[check_name]()
        return result

    def list_checks(self) -> list[str]:
        """Return sorted list of available check names.

        Returns:
            List of machine-readable check identifiers.
        """
        return sorted(self._build_check_map().keys())

    def _build_check_map(
        self,
    ) -> dict[str, Callable[[], Awaitable[DiagnosticResult]]]:
        """Build mapping of check names to callables.

        Returns:
            Dictionary of check_name → async callable.
        """

        async def _config_valid() -> DiagnosticResult:
            return _check_config_valid(self._config_path)

        async def _disk_space() -> DiagnosticResult:
            return _check_disk_space(self._data_dir)

        async def _memory_usage() -> DiagnosticResult:
            return _check_memory_usage()

        async def _model_files() -> DiagnosticResult:
            return _check_model_files(self._data_dir)

        async def _port_available() -> DiagnosticResult:
            return _check_port_available(self._port)

        async def _python_version() -> DiagnosticResult:
            return _check_python_version()

        async def _dependency_versions() -> DiagnosticResult:
            return _check_dependency_versions()

        async def _data_dir_writable() -> DiagnosticResult:
            return _check_data_dir_writable(self._data_dir)

        async def _voice_capture_apo() -> DiagnosticResult:
            return _check_voice_capture_apo()

        return {
            "brain_consistency": lambda: _check_brain_consistency(self._db_path),
            "config_valid": _config_valid,
            "data_dir_writable": _data_dir_writable,
            "db_integrity": lambda: _check_db_integrity(self._db_path),
            "dependency_versions": _dependency_versions,
            "disk_space": _disk_space,
            "memory_usage": _memory_usage,
            "model_files_present": _model_files,
            "port_available": _port_available,
            "python_version": _python_version,
            "schema_version_valid": lambda: _check_schema_version(self._db_path),
            "voice_capture_apo": _voice_capture_apo,
        }
