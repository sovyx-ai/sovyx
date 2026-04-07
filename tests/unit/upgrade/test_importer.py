"""Tests for MindImporter (V05-30).

Covers: SMF import (concepts, episodes, relations), archive import,
manifest validation, overwrite protection, YAML frontmatter parsing,
error handling, and roundtrip with MindExporter.
"""

from __future__ import annotations

import json
import zipfile
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

from sovyx.upgrade.importer import (
    ImportInfo,
    ImportValidationError,
    MindImporter,
    _extract_body_content,
    _split_frontmatter,
)

if TYPE_CHECKING:
    from pathlib import Path


# ── Helpers ─────────────────────────────────────────────────────────


def _make_pool(concept_count: int = 0) -> MagicMock:
    """Create a mock pool for import tests."""
    pool = MagicMock()

    class _ReadCM:
        async def __aenter__(self) -> MagicMock:
            conn = MagicMock()
            cursor = MagicMock()
            cursor.fetchone = AsyncMock(return_value=(concept_count,))
            conn.execute = AsyncMock(return_value=cursor)
            return conn

        async def __aexit__(self, *a: object) -> None:
            pass

    class _WriteCM:
        async def __aenter__(self) -> MagicMock:
            conn = MagicMock()
            conn.execute = AsyncMock()
            conn.commit = AsyncMock()
            return conn

        async def __aexit__(self, *a: object) -> None:
            pass

    pool.read = MagicMock(side_effect=lambda: _ReadCM())
    pool.write = MagicMock(side_effect=lambda: _WriteCM())

    return pool


def _create_smf_dir(
    base: Path,
    *,
    mind_id: str = "test-mind",
    concepts: bool = True,
    relations: bool = True,
    episodes: bool = True,
    format_version: int = 1,
) -> Path:
    """Create a minimal SMF directory structure."""
    smf = base / "smf"
    smf.mkdir()

    manifest = {
        "format_version": format_version,
        "sovyx_version": "0.5.0",
        "mind_id": mind_id,
        "mind_name": "Test Mind",
        "exported_at": "2026-03-25T15:00:00Z",
        "statistics": {"total_concepts": 1 if concepts else 0},
    }
    (smf / "manifest.json").write_text(json.dumps(manifest))

    if concepts:
        cat_dir = smf / "concepts" / "fact"
        cat_dir.mkdir(parents=True)
        concept_md = (
            "---\n"
            'id: "c-001"\n'
            "type: fact\n"
            "category: fact\n"
            'created: "2026-02-10"\n'
            'updated: "2026-03-15"\n'
            'source: "conversation"\n'
            "confidence: 0.9\n"
            "importance: 0.8\n"
            "emotional_valence: 0.1\n"
            "access_count: 3\n"
            "---\n"
            "\n"
            "# Likes Coffee\n"
            "\n"
            "User drinks coffee daily.\n"
        )
        (cat_dir / "likes-coffee.md").write_text(concept_md)

    if relations:
        meta_dir = smf / "metadata"
        meta_dir.mkdir()
        rels = [
            {
                "id": "r-001",
                "source_id": "c-001",
                "target_id": "c-002",
                "relation_type": "related_to",
                "weight": 0.7,
                "co_occurrence_count": 3,
                "last_activated": "2026-03-15",
                "created_at": "2026-02-15",
            }
        ]
        (meta_dir / "synapses.json").write_text(json.dumps(rels))

    if episodes:
        conv_dir = smf / "conversations"
        conv_dir.mkdir()
        ep_md = (
            "---\n"
            'id: "ep-001"\n'
            'conversation_id: "conv-001"\n'
            'created: "2026-03-15"\n'
            "importance: 0.6\n"
            "emotional_valence: 0.2\n"
            "emotional_arousal: 0.1\n"
            "---\n"
            "\n"
            "# Episode — 2026-03-15\n"
            "\n"
            "**User:** Hello\n"
            "\n"
            "**Assistant:** Hi there!\n"
            "\n"
            "**Summary:** A greeting.\n"
        )
        (conv_dir / "ep-001.md").write_text(ep_md)

    return smf


def _create_archive(
    base: Path,
    *,
    mind_id: str = "test-mind",
    include_manifest: bool = True,
    include_db: bool = True,
    include_config: bool = False,
    format_version: int = 1,
) -> Path:
    """Create a .sovyx-mind ZIP archive."""
    archive_path = base / "mind.sovyx-mind"
    with zipfile.ZipFile(archive_path, "w") as zf:
        if include_manifest:
            manifest = {
                "format_version": format_version,
                "sovyx_version": "0.5.0",
                "mind_id": mind_id,
            }
            zf.writestr("manifest.json", json.dumps(manifest))

        if include_db:
            zf.writestr("brain.db", b"SQLite fake data")

        if include_config:
            zf.writestr("mind.yaml", json.dumps({"name": "Test", "language": "en"}))

    return archive_path


# ── _split_frontmatter ──────────────────────────────────────────────


class TestSplitFrontmatter:
    """YAML frontmatter splitting utility."""

    def test_valid_frontmatter(self) -> None:
        text = "---\nid: 1\n---\n# Title\nBody"
        fm, body = _split_frontmatter(text)
        assert fm == "id: 1"
        assert body.startswith("# Title")

    def test_no_frontmatter(self) -> None:
        text = "Just plain text"
        fm, body = _split_frontmatter(text)
        assert fm is None
        assert body == "Just plain text"

    def test_no_closing_delimiter(self) -> None:
        text = "---\nid: 1\nNo closing"
        fm, body = _split_frontmatter(text)
        assert fm is None

    def test_empty_frontmatter(self) -> None:
        text = "---\n---\nBody here"
        fm, body = _split_frontmatter(text)
        assert fm == ""
        assert body == "Body here"


# ── _extract_body_content ───────────────────────────────────────────


class TestExtractBodyContent:
    """Body content extraction (skip heading)."""

    def test_skips_heading(self) -> None:
        body = "# Title\n\nActual content here."
        result = _extract_body_content(body)
        assert result == "Actual content here."

    def test_no_heading(self) -> None:
        body = "Just content."
        result = _extract_body_content(body)
        assert result == "Just content."

    def test_empty(self) -> None:
        assert _extract_body_content("") == ""


# ── ImportInfo ──────────────────────────────────────────────────────


class TestImportInfo:
    """ImportInfo dataclass defaults."""

    def test_defaults(self) -> None:
        info = ImportInfo(mind_id="x", source_format="smf")
        assert info.concepts_imported == 0
        assert info.episodes_imported == 0
        assert info.relations_imported == 0
        assert info.migrations_applied == []
        assert info.warnings == []


# ── SMF Import ──────────────────────────────────────────────────────


class TestImportSmf:
    """SMF directory import."""

    @pytest.mark.asyncio()
    async def test_imports_full_smf(self, tmp_path: Path) -> None:
        smf_dir = _create_smf_dir(tmp_path)
        pool = _make_pool(concept_count=0)
        importer = MindImporter(pool)

        info = await importer.import_smf(smf_dir)

        assert info.mind_id == "test-mind"
        assert info.source_format == "smf"
        assert info.concepts_imported == 1
        assert info.relations_imported == 1
        assert info.episodes_imported == 1

    @pytest.mark.asyncio()
    async def test_concepts_only(self, tmp_path: Path) -> None:
        smf_dir = _create_smf_dir(tmp_path, relations=False, episodes=False)
        pool = _make_pool()
        importer = MindImporter(pool)

        info = await importer.import_smf(smf_dir)
        assert info.concepts_imported == 1
        assert info.relations_imported == 0
        assert info.episodes_imported == 0

    @pytest.mark.asyncio()
    async def test_overwrite_protection(self, tmp_path: Path) -> None:
        smf_dir = _create_smf_dir(tmp_path)
        pool = _make_pool(concept_count=5)  # existing data
        importer = MindImporter(pool)

        with pytest.raises(ImportValidationError, match="already has 5 concepts"):
            await importer.import_smf(smf_dir, overwrite=False)

    @pytest.mark.asyncio()
    async def test_overwrite_allowed(self, tmp_path: Path) -> None:
        smf_dir = _create_smf_dir(tmp_path)
        pool = _make_pool(concept_count=5)
        importer = MindImporter(pool)

        # Should succeed with overwrite=True
        info = await importer.import_smf(smf_dir, overwrite=True)
        assert info.concepts_imported == 1

    @pytest.mark.asyncio()
    async def test_missing_directory(self, tmp_path: Path) -> None:
        pool = _make_pool()
        importer = MindImporter(pool)

        with pytest.raises(FileNotFoundError, match="not found"):
            await importer.import_smf(tmp_path / "nonexistent")

    @pytest.mark.asyncio()
    async def test_missing_manifest(self, tmp_path: Path) -> None:
        smf_dir = tmp_path / "empty-smf"
        smf_dir.mkdir()
        pool = _make_pool()
        importer = MindImporter(pool)

        with pytest.raises(ImportValidationError, match="Manifest not found"):
            await importer.import_smf(smf_dir)

    @pytest.mark.asyncio()
    async def test_invalid_manifest_json(self, tmp_path: Path) -> None:
        smf_dir = tmp_path / "bad-smf"
        smf_dir.mkdir()
        (smf_dir / "manifest.json").write_text("{bad json")
        pool = _make_pool()
        importer = MindImporter(pool)

        with pytest.raises(ImportValidationError, match="Invalid manifest JSON"):
            await importer.import_smf(smf_dir)

    @pytest.mark.asyncio()
    async def test_manifest_missing_mind_id(self, tmp_path: Path) -> None:
        smf_dir = tmp_path / "no-id"
        smf_dir.mkdir()
        (smf_dir / "manifest.json").write_text('{"format_version": 1}')
        pool = _make_pool()
        importer = MindImporter(pool)

        with pytest.raises(ImportValidationError, match="mind_id"):
            await importer.import_smf(smf_dir)

    @pytest.mark.asyncio()
    async def test_manifest_not_dict(self, tmp_path: Path) -> None:
        smf_dir = tmp_path / "arr"
        smf_dir.mkdir()
        (smf_dir / "manifest.json").write_text("[1,2,3]")
        pool = _make_pool()
        importer = MindImporter(pool)

        with pytest.raises(ImportValidationError, match="JSON object"):
            await importer.import_smf(smf_dir)

    @pytest.mark.asyncio()
    async def test_invalid_relations_json(self, tmp_path: Path) -> None:
        smf_dir = _create_smf_dir(tmp_path, concepts=False, episodes=False)
        # Corrupt the synapses file
        (smf_dir / "metadata" / "synapses.json").write_text("{not a list}")
        pool = _make_pool()
        importer = MindImporter(pool)

        info = await importer.import_smf(smf_dir)
        assert info.relations_imported == 0  # Gracefully skipped


# ── Archive Import ──────────────────────────────────────────────────


class TestImportArchive:
    """ZIP archive import."""

    @pytest.mark.asyncio()
    async def test_import_valid_archive(self, tmp_path: Path) -> None:
        archive = _create_archive(tmp_path)
        pool = _make_pool()
        importer = MindImporter(pool)

        restore_dir = tmp_path / "restore"
        info = await importer.import_archive(archive, db_restore_dir=restore_dir)

        assert info.mind_id == "test-mind"
        assert info.source_format == "sovyx-mind"
        assert (restore_dir / "brain.db").exists()

    @pytest.mark.asyncio()
    async def test_import_with_config(self, tmp_path: Path) -> None:
        archive = _create_archive(tmp_path, include_config=True)
        pool = _make_pool()
        importer = MindImporter(pool)

        restore_dir = tmp_path / "restore"
        await importer.import_archive(archive, db_restore_dir=restore_dir)

        assert (restore_dir / "mind.yaml").exists()

    @pytest.mark.asyncio()
    async def test_overwrite_protection(self, tmp_path: Path) -> None:
        archive = _create_archive(tmp_path)
        restore_dir = tmp_path / "restore"
        restore_dir.mkdir(parents=True)
        (restore_dir / "brain.db").write_bytes(b"existing")

        pool = _make_pool()
        importer = MindImporter(pool)

        with pytest.raises(ImportValidationError, match="already exists"):
            await importer.import_archive(archive, db_restore_dir=restore_dir)

    @pytest.mark.asyncio()
    async def test_overwrite_allowed(self, tmp_path: Path) -> None:
        archive = _create_archive(tmp_path)
        restore_dir = tmp_path / "restore"
        restore_dir.mkdir(parents=True)
        (restore_dir / "brain.db").write_bytes(b"old")

        pool = _make_pool()
        importer = MindImporter(pool)

        info = await importer.import_archive(archive, db_restore_dir=restore_dir, overwrite=True)
        assert info.mind_id == "test-mind"

    @pytest.mark.asyncio()
    async def test_missing_archive(self, tmp_path: Path) -> None:
        pool = _make_pool()
        importer = MindImporter(pool)

        with pytest.raises(FileNotFoundError, match="not found"):
            await importer.import_archive(tmp_path / "nonexistent.zip")

    @pytest.mark.asyncio()
    async def test_missing_manifest_in_zip(self, tmp_path: Path) -> None:
        archive = tmp_path / "bad.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("brain.db", b"data")

        pool = _make_pool()
        importer = MindImporter(pool)

        with pytest.raises(ImportValidationError, match="missing manifest"):
            await importer.import_archive(archive)

    @pytest.mark.asyncio()
    async def test_missing_db_in_zip(self, tmp_path: Path) -> None:
        archive = tmp_path / "nodb.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            manifest = {"format_version": 1, "mind_id": "x"}
            zf.writestr("manifest.json", json.dumps(manifest))

        pool = _make_pool()
        importer = MindImporter(pool)

        with pytest.raises(ImportValidationError, match="missing brain.db"):
            await importer.import_archive(archive)

    @pytest.mark.asyncio()
    async def test_unsupported_format_version(self, tmp_path: Path) -> None:
        archive = _create_archive(tmp_path, format_version=99)
        pool = _make_pool()
        importer = MindImporter(pool)

        with pytest.raises(ImportValidationError, match="Unsupported format"):
            await importer.import_archive(archive, db_restore_dir=tmp_path / "r")

    @pytest.mark.asyncio()
    async def test_migrations_applied(self, tmp_path: Path) -> None:
        archive = _create_archive(tmp_path)
        pool = _make_pool()

        mock_runner = MagicMock()
        mock_report = MagicMock()
        mock_report.applied = ["v0.5.0: add voice table"]
        mock_runner.run = AsyncMock(return_value=mock_report)

        migrations = [MagicMock()]
        importer = MindImporter(pool, migration_runner=mock_runner, migrations=migrations)

        info = await importer.import_archive(archive, db_restore_dir=tmp_path / "r")
        assert info.migrations_applied == ["v0.5.0: add voice table"]


# ── Manifest Validation ────────────────────────────────────────────


class TestManifestValidation:
    """Manifest validation edge cases."""

    def test_validate_missing_format_version(self) -> None:
        with pytest.raises(ImportValidationError, match="format_version"):
            MindImporter._validate_manifest({"mind_id": "x"})

    def test_validate_missing_mind_id(self) -> None:
        with pytest.raises(ImportValidationError, match="mind_id"):
            MindImporter._validate_manifest({"format_version": 1})

    def test_validate_future_version(self) -> None:
        with pytest.raises(ImportValidationError, match="Unsupported"):
            MindImporter._validate_manifest({"format_version": 999, "mind_id": "x"})

    def test_validate_valid(self) -> None:
        # Should not raise
        MindImporter._validate_manifest({"format_version": 1, "mind_id": "x"})


# ── Concept Parsing ─────────────────────────────────────────────────


class TestConceptParsing:
    """Concept markdown file parsing."""

    def test_parse_valid_concept(self, tmp_path: Path) -> None:
        md = (
            "---\n"
            'id: "c-test"\n'
            "category: preference\n"
            "confidence: 0.95\n"
            "importance: 0.8\n"
            "---\n"
            "\n"
            "# Coffee Lover\n"
            "\n"
            "Drinks espresso daily.\n"
        )
        filepath = tmp_path / "test.md"
        filepath.write_text(md)

        result = MindImporter._parse_concept_file(filepath, "my-mind")
        assert result is not None
        assert result["id"] == "c-test"
        assert result["mind_id"] == "my-mind"
        assert result["name"] == "Coffee Lover"
        assert result["content"] == "Drinks espresso daily."
        assert result["category"] == "preference"
        assert result["confidence"] == 0.95

    def test_parse_no_frontmatter(self, tmp_path: Path) -> None:
        filepath = tmp_path / "plain.md"
        filepath.write_text("Just plain text.")

        result = MindImporter._parse_concept_file(filepath, "m")
        assert result is None

    def test_parse_bad_yaml(self, tmp_path: Path) -> None:
        filepath = tmp_path / "bad.md"
        filepath.write_text("---\n: invalid: yaml: here\n---\nBody")

        result = MindImporter._parse_concept_file(filepath, "m")
        assert result is None

    def test_parse_missing_file(self, tmp_path: Path) -> None:
        result = MindImporter._parse_concept_file(tmp_path / "ghost.md", "m")
        assert result is None


# ── Episode Parsing ─────────────────────────────────────────────────


class TestEpisodeParsing:
    """Episode markdown file parsing."""

    def test_parse_valid_episode(self, tmp_path: Path) -> None:
        md = (
            "---\n"
            'id: "ep-test"\n'
            'conversation_id: "conv-1"\n'
            'created: "2026-03-15"\n'
            "importance: 0.7\n"
            "---\n"
            "\n"
            "# Episode\n"
            "\n"
            "**User:** Hello world\n"
            "\n"
            "**Assistant:** Hi there!\n"
            "\n"
            "**Summary:** A greeting exchange.\n"
        )
        filepath = tmp_path / "ep.md"
        filepath.write_text(md)

        result = MindImporter._parse_episode_file(filepath, "my-mind")
        assert result is not None
        assert result["id"] == "ep-test"
        assert result["user_input"] == "Hello world"
        assert result["assistant_response"] == "Hi there!"
        assert result["summary"] == "A greeting exchange."
        assert result["importance"] == 0.7

    def test_parse_no_frontmatter(self, tmp_path: Path) -> None:
        filepath = tmp_path / "plain.md"
        filepath.write_text("No frontmatter here.")

        result = MindImporter._parse_episode_file(filepath, "m")
        assert result is None


# ── Coverage Gap Tests (lines 339, 366, 422, 475, 506-519) ──────────


class TestImporterCoverageGaps:
    """Tests targeting uncovered lines in importer.py."""

    @pytest.mark.asyncio()
    async def test_import_synapses_unicode_error(self, tmp_path: Path) -> None:
        """Line 339: UnicodeDecodeError in synapses file."""
        pool = AsyncMock()
        importer = MindImporter(pool=pool)
        synapses_file = tmp_path / "synapses.json"
        synapses_file.write_bytes(b"\x80\x81\x82")  # invalid UTF-8
        count = await importer._import_relations_from_file(synapses_file)
        assert count == 0

    @pytest.mark.asyncio()
    async def test_import_synapses_non_list(self, tmp_path: Path) -> None:
        """Line 343: synapses data is not a list."""
        pool = AsyncMock()
        importer = MindImporter(pool=pool)
        synapses_file = tmp_path / "synapses.json"
        synapses_file.write_text('{"not": "a list"}')
        count = await importer._import_relations_from_file(synapses_file)
        assert count == 0

    @pytest.mark.asyncio()
    async def test_import_episodes_from_dir(self, tmp_path: Path) -> None:
        """Line 366: episode file that fails to parse returns None."""
        pool = AsyncMock()
        pool.write = _fake_write_ctx
        importer = MindImporter(pool=pool)

        convos_dir = tmp_path / "conversations"
        convos_dir.mkdir()

        # Valid episode
        ep1 = convos_dir / "ep1.md"
        ep1.write_text("---\nid: ep1\n---\n**User:** hello\n**Assistant:** hi\n")

        # Invalid episode (no frontmatter)
        ep2 = convos_dir / "ep2.md"
        ep2.write_text("No frontmatter here")

        count = await importer._import_episodes_from_dir(convos_dir, "mind1")
        assert count == 1

    def test_parse_episode_with_summary(self, tmp_path: Path) -> None:
        """Line 506-507: episode body with **Summary:** line."""
        pool = AsyncMock()
        importer = MindImporter(pool=pool)
        ep = tmp_path / "ep.md"
        ep.write_text(
            "---\nid: ep1\nimportance: 0.8\n---\n"
            "**User:** test question\n"
            "**Assistant:** test answer\n"
            "**Summary:** a brief summary\n"
        )
        result = importer._parse_episode_file(ep, "mind1")
        assert result is not None
        assert result["summary"] == "a brief summary"
        assert result["user_input"] == "test question"
        assert result["assistant_response"] == "test answer"

    def test_parse_episode_os_error(self, tmp_path: Path) -> None:
        """Line 475: OSError reading episode file."""
        pool = AsyncMock()
        importer = MindImporter(pool=pool)
        result = importer._parse_episode_file(tmp_path / "nonexistent.md", "m1")
        assert result is None

    def test_parse_episode_invalid_yaml(self, tmp_path: Path) -> None:
        """Line 515-516: YAML parse error in episode frontmatter."""
        pool = AsyncMock()
        importer = MindImporter(pool=pool)
        ep = tmp_path / "bad.md"
        ep.write_text("---\n:::invalid[yaml\n---\nBody\n")
        result = importer._parse_episode_file(ep, "mind1")
        assert result is None

    def test_parse_episode_non_dict_yaml(self, tmp_path: Path) -> None:
        """Line 519: YAML parses to non-dict (e.g. list)."""
        pool = AsyncMock()
        importer = MindImporter(pool=pool)
        ep = tmp_path / "list.md"
        ep.write_text("---\n- item1\n- item2\n---\nBody\n")
        result = importer._parse_episode_file(ep, "mind1")
        assert result is None


def _fake_write_ctx() -> AsyncMock:
    """Create a mock async context manager for pool.write()."""
    mock_conn = AsyncMock()
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)
    return mock_ctx


class TestImporterMetadataEdgeCases:
    """Test metadata serialization edge cases."""

    @pytest.mark.asyncio()
    async def test_insert_concept_with_dict_metadata(self, tmp_path: Path) -> None:
        """Line 366: metadata as dict gets JSON-serialized."""
        pool = AsyncMock()
        mock_conn = AsyncMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        pool.write = MagicMock(return_value=mock_ctx)

        importer = MindImporter(pool=pool)
        concept = {
            "id": "c1",
            "mind_id": "m1",
            "name": "test",
            "content": "content",
            "category": "fact",
            "importance": 0.5,
            "confidence": 0.5,
            "access_count": 0,
            "emotional_valence": 0.0,
            "source": "import",
            "metadata": {"key": "value"},  # dict, not string
            "created_at": "",
            "updated_at": "",
        }
        await importer._insert_concept(concept)
        mock_conn.execute.assert_awaited_once()

    @pytest.mark.asyncio()
    async def test_insert_episode_with_dict_metadata(self, tmp_path: Path) -> None:
        """Line 422: episode metadata as dict gets JSON-serialized."""
        pool = AsyncMock()
        mock_conn = AsyncMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        pool.write = MagicMock(return_value=mock_ctx)

        importer = MindImporter(pool=pool)
        episode = {
            "id": "e1",
            "mind_id": "m1",
            "conversation_id": "conv1",
            "user_input": "hello",
            "assistant_response": "hi",
            "summary": None,
            "importance": 0.5,
            "emotional_valence": 0.0,
            "emotional_arousal": 0.0,
            "concepts_mentioned": ["c1", "c2"],
            "metadata": {"nested": True},  # dict, not string
            "created_at": "",
        }
        await importer._insert_episode(episode)
        mock_conn.execute.assert_awaited_once()
