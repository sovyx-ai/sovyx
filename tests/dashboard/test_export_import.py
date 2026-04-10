"""Tests for sovyx.dashboard.export_import — export/import API helpers."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sovyx.dashboard.export_import import export_mind, import_mind

# ── Fixtures ──


@pytest.fixture()
def mock_registry() -> MagicMock:
    """Create a mock ServiceRegistry."""
    registry = MagicMock()
    registry.is_registered = MagicMock(return_value=True)
    return registry


@pytest.fixture()
def sample_archive(tmp_path: Path) -> Path:
    """Create a sample .sovyx-mind archive for import tests."""
    archive_path = tmp_path / "test.sovyx-mind"
    manifest = {
        "format_version": 1,
        "sovyx_version": "0.5.0",
        "mind_id": "test-mind",
        "mind_name": "Test Mind",
        "exported_at": "2026-01-01T00:00:00+00:00",
        "statistics": {"concepts": 0, "episodes": 0, "relations": 0},
        "gdpr": {
            "format": "structured, commonly used, machine-readable",
            "standard": "GDPR Art. 20 compliant",
            "license": "User owns all data. No restrictions on portability.",
        },
    }
    db_path = tmp_path / "brain.db"
    db_path.write_bytes(b"SQLite format 3\x00" + b"\x00" * 100)

    with zipfile.ZipFile(archive_path, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.write(db_path, "brain.db")

    return archive_path


# ── Export Tests ──


class TestExportMind:
    """Tests for export_mind()."""

    @pytest.mark.asyncio()
    async def test_export_raises_if_no_db_manager(self, mock_registry: MagicMock) -> None:
        """RuntimeError when DatabaseManager not registered."""
        mock_registry.is_registered.return_value = False
        with pytest.raises(RuntimeError, match="Database manager not available"):
            await export_mind(mock_registry, "test-mind")

    @pytest.mark.asyncio()
    async def test_export_calls_exporter(self, mock_registry: MagicMock, tmp_path: Path) -> None:
        """Exporter is called with correct params and returns a path."""
        from sovyx.upgrade.exporter import ExportInfo, ExportManifest

        fake_path = tmp_path / "exported.sovyx-mind"
        fake_path.write_bytes(b"fake")

        fake_info = ExportInfo(
            path=fake_path,
            manifest=ExportManifest(
                format_version=1,
                sovyx_version="0.5.0",
                mind_id="test-mind",
                mind_name="test-mind",
                exported_at="2026-01-01T00:00:00+00:00",
            ),
            format="sovyx-mind",
            size_bytes=4,
        )

        mock_pool = MagicMock()
        mock_db_mgr = MagicMock()
        mock_db_mgr.get_brain_pool.return_value = mock_pool
        mock_registry.resolve = AsyncMock(return_value=mock_db_mgr)

        mock_exporter = MagicMock()
        mock_exporter.export_archive = AsyncMock(return_value=fake_info)

        with patch(
            "sovyx.upgrade.exporter.MindExporter",
            return_value=mock_exporter,
        ):
            result = await export_mind(mock_registry, "test-mind")

        assert isinstance(result, Path)
        assert result.suffix == ".sovyx-mind"
        mock_exporter.export_archive.assert_awaited_once()

    @pytest.mark.asyncio()
    async def test_export_returns_path_object(
        self, mock_registry: MagicMock, tmp_path: Path
    ) -> None:
        """Return value is a Path."""
        from sovyx.upgrade.exporter import ExportInfo, ExportManifest

        fake_path = tmp_path / "out.sovyx-mind"
        fake_path.write_bytes(b"zip")

        fake_info = ExportInfo(
            path=fake_path,
            manifest=ExportManifest(
                format_version=1,
                sovyx_version="0.5.0",
                mind_id="m",
                mind_name="m",
                exported_at="2026-01-01T00:00:00+00:00",
            ),
            format="sovyx-mind",
            size_bytes=3,
        )

        mock_db_mgr = MagicMock()
        mock_db_mgr.get_brain_pool.return_value = MagicMock()
        mock_registry.resolve = AsyncMock(return_value=mock_db_mgr)

        with patch(
            "sovyx.upgrade.exporter.MindExporter",
            return_value=MagicMock(export_archive=AsyncMock(return_value=fake_info)),
        ):
            result = await export_mind(mock_registry, "m")

        assert isinstance(result, Path)


# ── Import Tests ──


class TestImportMind:
    """Tests for import_mind()."""

    @pytest.mark.asyncio()
    async def test_import_raises_if_no_db_manager(
        self, mock_registry: MagicMock, sample_archive: Path
    ) -> None:
        """RuntimeError when DatabaseManager not registered."""
        mock_registry.is_registered.return_value = False
        with pytest.raises(RuntimeError, match="Database manager not available"):
            await import_mind(mock_registry, sample_archive)

    @pytest.mark.asyncio()
    async def test_import_calls_importer(
        self, mock_registry: MagicMock, sample_archive: Path
    ) -> None:
        """Importer is called with correct params."""
        from sovyx.upgrade.importer import ImportInfo

        fake_info = ImportInfo(mind_id="test-mind", source_format="sovyx-mind")
        fake_info.concepts_imported = 5
        fake_info.episodes_imported = 3
        fake_info.relations_imported = 2
        fake_info.migrations_applied = []
        fake_info.warnings = []

        mock_db_mgr = MagicMock()
        mock_db_mgr.get_system_pool.return_value = MagicMock()
        mock_registry.resolve = AsyncMock(return_value=mock_db_mgr)

        mock_importer = MagicMock()
        mock_importer.import_archive = AsyncMock(return_value=fake_info)

        with patch(
            "sovyx.upgrade.importer.MindImporter",
            return_value=mock_importer,
        ):
            result = await import_mind(mock_registry, sample_archive)

        assert result["mind_id"] == "test-mind"
        assert result["concepts_imported"] == 5
        assert result["episodes_imported"] == 3
        assert result["relations_imported"] == 2
        mock_importer.import_archive.assert_awaited_once()

    @pytest.mark.asyncio()
    async def test_import_overwrite_flag_passed(
        self, mock_registry: MagicMock, sample_archive: Path
    ) -> None:
        """The overwrite flag is forwarded to the importer."""
        from sovyx.upgrade.importer import ImportInfo

        fake_info = ImportInfo(mind_id="m", source_format="sovyx-mind")

        mock_db_mgr = MagicMock()
        mock_db_mgr.get_system_pool.return_value = MagicMock()
        mock_registry.resolve = AsyncMock(return_value=mock_db_mgr)

        mock_importer = MagicMock()
        mock_importer.import_archive = AsyncMock(return_value=fake_info)

        with patch(
            "sovyx.upgrade.importer.MindImporter",
            return_value=mock_importer,
        ):
            await import_mind(mock_registry, sample_archive, overwrite=True)

        call_kwargs = mock_importer.import_archive.call_args.kwargs
        assert call_kwargs.get("overwrite") is True

    @pytest.mark.asyncio()
    async def test_import_result_keys(
        self, mock_registry: MagicMock, sample_archive: Path
    ) -> None:
        """Result dict contains all expected keys."""
        from sovyx.upgrade.importer import ImportInfo

        fake_info = ImportInfo(mind_id="m", source_format="sovyx-mind")

        mock_db_mgr = MagicMock()
        mock_db_mgr.get_system_pool.return_value = MagicMock()
        mock_registry.resolve = AsyncMock(return_value=mock_db_mgr)

        with patch(
            "sovyx.upgrade.importer.MindImporter",
            return_value=MagicMock(import_archive=AsyncMock(return_value=fake_info)),
        ):
            result = await import_mind(mock_registry, sample_archive)

        expected_keys = {
            "mind_id",
            "concepts_imported",
            "episodes_imported",
            "relations_imported",
            "warnings",
        }
        assert set(result.keys()) == expected_keys
