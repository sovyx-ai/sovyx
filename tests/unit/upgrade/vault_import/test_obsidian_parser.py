"""End-to-end tests for ``ObsidianImporter.parse``.

Each test builds a synthetic vault ZIP in ``tmp_path`` and asserts
what ``parse()`` yields. Using an in-memory / tmp ZIP is important
because Obsidian's real exports are directory trees — we always ZIP
before uploading, and that packaging is exactly what the importer
reads.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from sovyx.upgrade.conv_import._base import ConversationImportError
from sovyx.upgrade.vault_import.obsidian import ObsidianImporter


def _make_vault(tmp_path: Path, files: dict[str, str]) -> Path:
    """Build a ZIP at ``tmp_path/vault.zip`` from the given filename→content map.

    ``tmp_path`` is created if it doesn't exist so tests can scope
    multiple vaults under sub-directories (``tmp_path / "v1"``) with
    a single helper call.
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    zip_path = tmp_path / "vault.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, body in files.items():
            archive.writestr(name, body)
    return zip_path


class TestParseHappyPath:
    def test_single_note(self, tmp_path: Path) -> None:
        vault = _make_vault(tmp_path, {"Welcome.md": "# Welcome\nHello world."})
        notes = list(ObsidianImporter().parse(vault))
        assert len(notes) == 1
        assert notes[0].title == "Welcome"
        # H1 overrides the filename stem.
        assert notes[0].path == "Welcome.md"
        assert notes[0].body.startswith("# Welcome")

    def test_frontmatter_title_wins_over_h1(self, tmp_path: Path) -> None:
        note = "---\ntitle: Display Title\n---\n# H1 heading\nbody\n"
        vault = _make_vault(tmp_path, {"file.md": note})
        notes = list(ObsidianImporter().parse(vault))
        assert notes[0].title == "Display Title"

    def test_filename_title_fallback(self, tmp_path: Path) -> None:
        """No frontmatter title, no H1 → filename stem is the title."""
        vault = _make_vault(tmp_path, {"portuguese.md": "Just body text.\n"})
        notes = list(ObsidianImporter().parse(vault))
        assert notes[0].title == "portuguese"

    def test_nested_directory(self, tmp_path: Path) -> None:
        vault = _make_vault(
            tmp_path,
            {
                "topics/language.md": "body",
                "topics/linguistics/phonology.md": "body",
            },
        )
        notes = list(ObsidianImporter().parse(vault))
        paths = sorted(note.path for note in notes)
        assert paths == ["topics/language.md", "topics/linguistics/phonology.md"]

    def test_wikilinks_and_tags_extracted(self, tmp_path: Path) -> None:
        body = (
            "---\ntags: [linguistics]\n---\n"
            "# Portuguese\n"
            "See [[Spanish]] and [[Romance Languages|Romance]].\n"
            "Study #language #q1-2024.\n"
        )
        vault = _make_vault(tmp_path, {"portuguese.md": body})
        notes = list(ObsidianImporter().parse(vault))
        note = notes[0]
        assert note.link_targets() == ("Spanish", "Romance Languages")
        # Obsidian's own rule: body tags must start with a letter or
        # underscore — ``#2024/q1`` isn't a valid tag either in Obsidian
        # or here, so we use ``#q1-2024`` instead.
        tag_names = {t.name for t in note.tags}
        assert tag_names == {"linguistics", "language", "q1-2024"}

    def test_aliases_captured(self, tmp_path: Path) -> None:
        body = "---\naliases: [PT, Port.]\n---\nbody\n"
        vault = _make_vault(tmp_path, {"portuguese.md": body})
        notes = list(ObsidianImporter().parse(vault))
        assert notes[0].aliases == ("PT", "Port.")

    def test_content_hash_changes_with_body(self, tmp_path: Path) -> None:
        v1 = _make_vault(tmp_path / "v1", {"n.md": "---\ntitle: n\n---\nBody A"})
        v2 = _make_vault(tmp_path / "v2", {"n.md": "---\ntitle: n\n---\nBody B"})
        h1 = list(ObsidianImporter().parse(v1))[0].content_hash
        h2 = list(ObsidianImporter().parse(v2))[0].content_hash
        assert h1 != h2

    def test_content_hash_stable_for_identical_body(self, tmp_path: Path) -> None:
        v1 = _make_vault(tmp_path / "a", {"n.md": "Same body"})
        v2 = _make_vault(tmp_path / "b", {"n.md": "Same body"})
        h1 = list(ObsidianImporter().parse(v1))[0].content_hash
        h2 = list(ObsidianImporter().parse(v2))[0].content_hash
        assert h1 == h2

    def test_h1_with_wikilink_stripped(self, tmp_path: Path) -> None:
        body = "# [[Portuguese Grammar]]\nbody\n"
        vault = _make_vault(tmp_path, {"whatever.md": body})
        notes = list(ObsidianImporter().parse(vault))
        assert notes[0].title == "Portuguese Grammar"

    def test_windows_crlf_normalised(self, tmp_path: Path) -> None:
        body = "---\r\ntitle: X\r\n---\r\nbody\r\n"
        vault = _make_vault(tmp_path, {"n.md": body})
        notes = list(ObsidianImporter().parse(vault))
        assert notes[0].title == "X"
        assert "\r" not in notes[0].body

    def test_link_count_preserved_for_weight(self, tmp_path: Path) -> None:
        body = "Linking [[Foo]] once, [[Foo]] twice, [[Foo]] thrice.\n"
        vault = _make_vault(tmp_path, {"n.md": body})
        note = list(ObsidianImporter().parse(vault))[0]
        assert len(note.links) == 3


class TestParseSkips:
    def test_dotted_directories_skipped(self, tmp_path: Path) -> None:
        vault = _make_vault(
            tmp_path,
            {
                ".obsidian/workspace.json": "{}",
                ".obsidian/plugins/foo/main.md": "# nope",
                ".trash/deleted.md": "# deleted",
                "real-note.md": "# Real",
            },
        )
        notes = list(ObsidianImporter().parse(vault))
        paths = [n.path for n in notes]
        assert paths == ["real-note.md"]

    def test_git_directory_skipped(self, tmp_path: Path) -> None:
        vault = _make_vault(
            tmp_path,
            {
                ".git/config": "ignore",
                ".git/COMMIT_EDITMSG": "msg",
                "note.md": "body",
            },
        )
        notes = list(ObsidianImporter().parse(vault))
        assert [n.path for n in notes] == ["note.md"]

    def test_non_markdown_files_skipped(self, tmp_path: Path) -> None:
        vault = _make_vault(
            tmp_path,
            {
                "image.png": "binary",
                "data.json": "{}",
                "note.md": "body",
            },
        )
        notes = list(ObsidianImporter().parse(vault))
        assert [n.path for n in notes] == ["note.md"]

    def test_markdown_extension_variant(self, tmp_path: Path) -> None:
        vault = _make_vault(
            tmp_path,
            {
                "note.md": "body",
                "other.markdown": "body",
            },
        )
        notes = list(ObsidianImporter().parse(vault))
        assert len(notes) == 2


class TestParseErrors:
    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(ConversationImportError, match="not found"):
            list(ObsidianImporter().parse(tmp_path / "missing.zip"))

    def test_not_a_zip(self, tmp_path: Path) -> None:
        """A text file with a .zip extension must be rejected."""
        path = tmp_path / "fake.zip"
        path.write_text("I am not a ZIP")
        with pytest.raises(ConversationImportError, match="Not a valid ZIP"):
            list(ObsidianImporter().parse(path))

    def test_empty_zip_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.zip"
        with zipfile.ZipFile(path, "w"):
            pass
        with pytest.raises(ConversationImportError, match="No markdown notes"):
            list(ObsidianImporter().parse(path))

    def test_zip_with_only_skipped_files_rejected(self, tmp_path: Path) -> None:
        """A ZIP with only .obsidian/ contents has no eligible notes."""
        vault = _make_vault(
            tmp_path,
            {".obsidian/workspace.json": "{}"},
        )
        with pytest.raises(ConversationImportError, match="No markdown notes"):
            list(ObsidianImporter().parse(vault))


class TestLinkTargetsHelper:
    """``RawNote.link_targets`` de-duplicates while preserving order."""

    def test_dedup(self, tmp_path: Path) -> None:
        body = "[[Foo]] [[Bar]] [[Foo]] [[Baz]] [[Bar]]\n"
        vault = _make_vault(tmp_path, {"n.md": body})
        note = list(ObsidianImporter().parse(vault))[0]
        assert note.link_targets() == ("Foo", "Bar", "Baz")
