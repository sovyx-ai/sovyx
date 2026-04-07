"""Tests for MindExporter (V05-30).

Covers: SMF export (concepts, episodes, relations, manifest),
archive export (ZIP with brain.db), file format validation,
sanitized filenames, empty data handling, and roundtrip integrity.
"""

from __future__ import annotations

import json
import zipfile
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

from sovyx.upgrade.exporter import ExportInfo, ExportManifest, MindExporter, _sanitize_filename

if TYPE_CHECKING:
    from pathlib import Path


# ── Fixtures ────────────────────────────────────────────────────────


def _make_pool(
    concepts: list[tuple[object, ...]] | None = None,
    episodes: list[tuple[object, ...]] | None = None,
    relations: list[tuple[object, ...]] | None = None,
) -> MagicMock:
    """Create a mock pool returning pre-defined rows."""
    pool = MagicMock()

    class _ReadCM:
        def __init__(self, rows: list[tuple[object, ...]]) -> None:
            self._rows = rows

        async def __aenter__(self) -> MagicMock:
            conn = MagicMock()
            cursor = MagicMock()
            cursor.fetchall = AsyncMock(return_value=self._rows)
            conn.execute = AsyncMock(return_value=cursor)
            return conn

        async def __aexit__(self, *a: object) -> None:
            pass

    class _WriteCM:
        async def __aenter__(self) -> MagicMock:
            conn = MagicMock()
            conn.execute = AsyncMock()
            return conn

        async def __aexit__(self, *a: object) -> None:
            pass

    concept_rows = concepts or []
    episode_rows = episodes or []
    relation_rows = relations or []

    call_count = {"n": 0}

    def _read_side_effect() -> _ReadCM:
        idx = call_count["n"]
        call_count["n"] += 1
        if idx == 0:
            return _ReadCM(concept_rows)
        if idx == 1:
            return _ReadCM(relation_rows)
        return _ReadCM(episode_rows)

    pool.read = MagicMock(side_effect=_read_side_effect)
    pool.write = MagicMock(return_value=_WriteCM())

    return pool


def _sample_concept_row() -> tuple[object, ...]:
    return (
        "c-001",          # id
        "test-mind",      # mind_id
        "likes-coffee",   # name
        "User likes strong black coffee.",  # content
        "preference",     # category
        0.8,              # importance
        0.9,              # confidence
        5,                # access_count
        "2026-03-15T10:00:00",  # last_accessed
        0.3,              # emotional_valence
        "conversation",   # source
        '{"key": "val"}',  # metadata
        "2026-02-10T08:30:00",  # created_at
        "2026-03-15T14:22:00",  # updated_at
    )


def _sample_episode_row() -> tuple[object, ...]:
    return (
        "ep-001",         # id
        "test-mind",      # mind_id
        "conv-001",       # conversation_id
        "What is coffee?",  # user_input
        "A brewed drink.",  # assistant_response
        "User asked about coffee",  # summary
        0.6,              # importance
        0.2,              # emotional_valence
        0.1,              # emotional_arousal
        '["c-001"]',      # concepts_mentioned
        "{}",             # metadata
        "2026-03-15T07:30:00",  # created_at
    )


def _sample_relation_row() -> tuple[object, ...]:
    return (
        "r-001",          # id
        "c-001",          # source_id
        "c-002",          # target_id
        "related_to",     # relation_type
        0.7,              # weight
        3,                # co_occurrence_count
        "2026-03-15T10:00:00",  # last_activated
        "2026-02-15T09:00:00",  # created_at
    )


# ── ExportManifest ──────────────────────────────────────────────────


class TestExportManifest:
    """ExportManifest serialization."""

    def test_to_dict(self) -> None:
        m = ExportManifest(
            format_version=1,
            sovyx_version="0.5.0",
            mind_id="aria",
            mind_name="Aria",
            exported_at="2026-03-25T15:00:00Z",
            statistics={"total_concepts": 42},
        )
        d = m.to_dict()
        assert d["format_version"] == 1
        assert d["mind_id"] == "aria"
        assert d["statistics"]["total_concepts"] == 42
        assert d["gdpr"]["standard"] == "GDPR Art. 20 compliant"

    def test_to_dict_empty_stats(self) -> None:
        m = ExportManifest(
            format_version=1,
            sovyx_version="0.5.0",
            mind_id="x",
            mind_name="X",
            exported_at="",
        )
        d = m.to_dict()
        assert d["statistics"] == {}


# ── _sanitize_filename ──────────────────────────────────────────────


class TestSanitizeFilename:
    """Filename sanitization utility."""

    def test_simple_name(self) -> None:
        assert _sanitize_filename("hello-world") == "hello-world"

    def test_spaces_and_special(self) -> None:
        result = _sanitize_filename("user likes coffee!")
        assert " " not in result
        assert "!" not in result

    def test_empty_string(self) -> None:
        assert _sanitize_filename("") == "unnamed"

    def test_all_special_chars(self) -> None:
        assert _sanitize_filename("!!!@@@###") == "unnamed"

    def test_truncation(self) -> None:
        long_name = "a" * 200
        assert len(_sanitize_filename(long_name)) <= 100

    def test_consecutive_hyphens(self) -> None:
        result = _sanitize_filename("a  b  c")
        assert "--" not in result


# ── SMF Export ──────────────────────────────────────────────────────


class TestExportSmf:
    """SMF directory export."""

    @pytest.mark.asyncio()
    async def test_empty_mind(self, tmp_path: Path) -> None:
        pool = _make_pool()
        exporter = MindExporter(pool, sovyx_version="0.5.0")

        info = await exporter.export_smf("empty-mind", tmp_path / "out")

        assert info.format == "smf"
        assert info.manifest.mind_id == "empty-mind"
        assert info.manifest.statistics["total_concepts"] == 0

        manifest_file = tmp_path / "out" / "manifest.json"
        assert manifest_file.exists()
        data = json.loads(manifest_file.read_text())
        assert data["format_version"] == 1

    @pytest.mark.asyncio()
    async def test_exports_concepts(self, tmp_path: Path) -> None:
        pool = _make_pool(concepts=[_sample_concept_row()])
        exporter = MindExporter(pool)

        info = await exporter.export_smf(
            "test-mind", tmp_path / "smf", mind_name="Test"
        )

        assert info.manifest.statistics["total_concepts"] == 1
        # Concept should be under concepts/preference/
        pref_dir = tmp_path / "smf" / "concepts" / "preference"
        assert pref_dir.is_dir()
        md_files = list(pref_dir.glob("*.md"))
        assert len(md_files) == 1

        content = md_files[0].read_text()
        assert "likes-coffee" in content
        assert "confidence: 0.9" in content
        assert "User likes strong black coffee." in content

    @pytest.mark.asyncio()
    async def test_exports_episodes(self, tmp_path: Path) -> None:
        pool = _make_pool(episodes=[_sample_episode_row()])
        exporter = MindExporter(pool)

        info = await exporter.export_smf("test-mind", tmp_path / "smf")

        assert info.manifest.statistics["total_episodes"] == 1
        convos_dir = tmp_path / "smf" / "conversations"
        assert convos_dir.is_dir()
        md_files = list(convos_dir.glob("*.md"))
        assert len(md_files) == 1

        content = md_files[0].read_text()
        assert "What is coffee?" in content
        assert "A brewed drink." in content
        assert "**Summary:**" in content

    @pytest.mark.asyncio()
    async def test_exports_relations(self, tmp_path: Path) -> None:
        pool = _make_pool(relations=[_sample_relation_row()])
        exporter = MindExporter(pool)

        info = await exporter.export_smf("test-mind", tmp_path / "smf")

        assert info.manifest.statistics["total_relations"] == 1
        synapses = tmp_path / "smf" / "metadata" / "synapses.json"
        assert synapses.exists()
        data = json.loads(synapses.read_text())
        assert len(data) == 1
        assert data[0]["source_id"] == "c-001"
        assert data[0]["weight"] == 0.7

    @pytest.mark.asyncio()
    async def test_skip_conversations(self, tmp_path: Path) -> None:
        pool = _make_pool(episodes=[_sample_episode_row()])
        exporter = MindExporter(pool)

        info = await exporter.export_smf(
            "test-mind",
            tmp_path / "smf",
            include_conversations=False,
        )

        assert info.manifest.statistics["total_episodes"] == 0
        assert not (tmp_path / "smf" / "conversations").exists()

    @pytest.mark.asyncio()
    async def test_manifest_content(self, tmp_path: Path) -> None:
        pool = _make_pool(
            concepts=[_sample_concept_row()],
            relations=[_sample_relation_row()],
            episodes=[_sample_episode_row()],
        )
        exporter = MindExporter(pool, sovyx_version="0.5.0")

        await exporter.export_smf("my-mind", tmp_path / "out", mind_name="My Mind")

        manifest = json.loads((tmp_path / "out" / "manifest.json").read_text())
        assert manifest["mind_name"] == "My Mind"
        assert manifest["sovyx_version"] == "0.5.0"
        assert manifest["gdpr"]["license"] == "User owns all data. No restrictions on portability."

    @pytest.mark.asyncio()
    async def test_creates_output_dir(self, tmp_path: Path) -> None:
        pool = _make_pool()
        exporter = MindExporter(pool)
        deep = tmp_path / "a" / "b" / "c"
        assert not deep.exists()

        await exporter.export_smf("m", deep)
        assert deep.is_dir()

    @pytest.mark.asyncio()
    async def test_mind_name_defaults_to_id(self, tmp_path: Path) -> None:
        pool = _make_pool()
        exporter = MindExporter(pool)

        info = await exporter.export_smf("aria-2026", tmp_path / "out")
        assert info.manifest.mind_name == "aria-2026"


# ── Archive Export ──────────────────────────────────────────────────


class TestExportArchive:
    """ZIP archive export."""

    @pytest.mark.asyncio()
    async def test_creates_valid_zip(self, tmp_path: Path) -> None:
        pool = _make_pool()
        # Mock VACUUM INTO by creating the db copy file
        db_copy = tmp_path / "_export_test-mind.db"

        async def _fake_execute(sql: str) -> None:
            if "VACUUM INTO" in sql:
                db_copy.write_bytes(b"SQLite3 fake db content")

        write_cm = MagicMock()
        conn = MagicMock()
        conn.execute = AsyncMock(side_effect=_fake_execute)
        write_cm.__aenter__ = AsyncMock(return_value=conn)
        write_cm.__aexit__ = AsyncMock(return_value=None)
        pool.write = MagicMock(return_value=write_cm)

        exporter = MindExporter(pool, sovyx_version="0.5.0")
        archive_path = tmp_path / "mind.sovyx-mind"

        info = await exporter.export_archive("test-mind", archive_path)

        assert info.format == "sovyx-mind"
        assert info.size_bytes > 0
        assert archive_path.exists()

        with zipfile.ZipFile(archive_path) as zf:
            names = zf.namelist()
            assert "manifest.json" in names
            assert "brain.db" in names

            manifest = json.loads(zf.read("manifest.json"))
            assert manifest["mind_id"] == "test-mind"

    @pytest.mark.asyncio()
    async def test_includes_mind_config(self, tmp_path: Path) -> None:
        pool = _make_pool()
        db_copy = tmp_path / "_export_cfg-mind.db"

        async def _fake_execute(sql: str) -> None:
            if "VACUUM INTO" in sql:
                db_copy.write_bytes(b"fake db")

        write_cm = MagicMock()
        conn = MagicMock()
        conn.execute = AsyncMock(side_effect=_fake_execute)
        write_cm.__aenter__ = AsyncMock(return_value=conn)
        write_cm.__aexit__ = AsyncMock(return_value=None)
        pool.write = MagicMock(return_value=write_cm)

        exporter = MindExporter(pool)
        archive_path = tmp_path / "out.sovyx-mind"

        await exporter.export_archive(
            "cfg-mind",
            archive_path,
            mind_config={"name": "Aria", "language": "en"},
        )

        with zipfile.ZipFile(archive_path) as zf:
            assert "mind.yaml" in zf.namelist()
            config = json.loads(zf.read("mind.yaml"))
            assert config["name"] == "Aria"

    @pytest.mark.asyncio()
    async def test_cleans_up_temp_db(self, tmp_path: Path) -> None:
        pool = _make_pool()
        db_copy = tmp_path / "_export_cleanup-mind.db"

        async def _fake_execute(sql: str) -> None:
            if "VACUUM INTO" in sql:
                db_copy.write_bytes(b"fake")

        write_cm = MagicMock()
        conn = MagicMock()
        conn.execute = AsyncMock(side_effect=_fake_execute)
        write_cm.__aenter__ = AsyncMock(return_value=conn)
        write_cm.__aexit__ = AsyncMock(return_value=None)
        pool.write = MagicMock(return_value=write_cm)

        exporter = MindExporter(pool)
        await exporter.export_archive("cleanup-mind", tmp_path / "out.sovyx-mind")

        # Temp db should be cleaned up
        assert not db_copy.exists()


# ── ExportInfo ──────────────────────────────────────────────────────


class TestExportInfo:
    """ExportInfo dataclass."""

    def test_defaults(self) -> None:
        info = ExportInfo(
            path=__import__("pathlib").Path("/tmp/x"),
            manifest=ExportManifest(
                format_version=1,
                sovyx_version="0.5.0",
                mind_id="x",
                mind_name="X",
                exported_at="",
            ),
            format="smf",
        )
        assert info.size_bytes == 0
        assert info.format == "smf"
