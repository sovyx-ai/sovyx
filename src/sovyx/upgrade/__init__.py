"""Upgrade — Schema versioning, migration, blue-green upgrade, diagnostics, and backup."""

from __future__ import annotations

from sovyx.upgrade.backup_manager import (
    BackupError,
    BackupInfo,
    BackupIntegrityError,
    BackupManager,
    BackupTrigger,
)
from sovyx.upgrade.blue_green import (
    BlueGreenUpgrader,
    UpgradeError,
    UpgradePhase,
    UpgradeResult,
    VersionInstaller,
)
from sovyx.upgrade.doctor import DiagnosticReport, DiagnosticResult, DiagnosticStatus, Doctor
from sovyx.upgrade.exporter import ExportInfo, ExportManifest, MindExporter
from sovyx.upgrade.importer import ImportInfo, ImportValidationError, MindImporter
from sovyx.upgrade.schema import MigrationRunner, SchemaVersion

__all__ = [
    "BackupError",
    "BackupInfo",
    "BackupIntegrityError",
    "BackupManager",
    "BackupTrigger",
    "BlueGreenUpgrader",
    "DiagnosticReport",
    "DiagnosticResult",
    "DiagnosticStatus",
    "Doctor",
    "ExportInfo",
    "ExportManifest",
    "ImportInfo",
    "ImportValidationError",
    "MigrationRunner",
    "MindExporter",
    "MindImporter",
    "SchemaVersion",
    "UpgradeError",
    "UpgradePhase",
    "UpgradeResult",
    "VersionInstaller",
]
