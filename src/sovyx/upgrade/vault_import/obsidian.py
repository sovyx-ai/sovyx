"""Obsidian vault walker — ZIP archive → ``RawNote`` stream.

Obsidian vaults are directory trees of Markdown files (and
attachments that we ignore in v0). Users export a vault either as a
shared folder or — more reliably for an HTTP import — as a ZIP.

This importer:

1. Opens the ZIP at ``source`` and walks every ``.md`` member.
2. Skips anything under ``.obsidian/``, ``.trash/``, ``node_modules/``
   (some users check in Obsidian plugins), or otherwise hidden
   directories.
3. Reads each note as UTF-8 with ``errors="replace"`` — rarely a
   vault has a file in some legacy encoding, and we'd rather emit
   replacement characters than skip the whole note.
4. Extracts frontmatter, wikilinks, and tags, computes a content hash,
   and yields one :class:`RawNote` per file.

The walker is **synchronous** — ZIP extraction is CPU-bound and
single-threaded in ``zipfile``; wrapping it in ``asyncio.to_thread``
is the caller's job if it needs to cooperate with an event loop
(it doesn't — the dashboard worker drives every importer inside a
background ``asyncio.Task`` and the parse step is never the
bottleneck; summary encoding is).

Malformed notes produce a logger warning and are skipped silently.
A note with a broken frontmatter block is *not* skipped — frontmatter
parsing is lenient and the body is preserved either way (see
``_frontmatter.extract_frontmatter``).
"""

from __future__ import annotations

import hashlib
import posixpath
import zipfile
from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger
from sovyx.upgrade.conv_import._base import ConversationImportError
from sovyx.upgrade.vault_import._frontmatter import (
    extract_frontmatter,
    normalise_aliases,
    normalise_created_at,
    normalise_tags,
)
from sovyx.upgrade.vault_import._models import RawLink, RawNote, RawTag
from sovyx.upgrade.vault_import._tags import extract_body_tags, merge_tags
from sovyx.upgrade.vault_import._wikilinks import extract_links

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

logger = get_logger(__name__)

# Extensions we accept. Obsidian supports ``.md`` + ``.markdown``; in
# practice 99.9 % of vaults use ``.md``.
_NOTE_EXTENSIONS = (".md", ".markdown")

# Directories and single files we always skip. Anything else with a
# ``.md`` extension lands in the import.
_SKIP_DIR_NAMES = frozenset(
    {
        ".obsidian",  # Obsidian's own config + workspace state
        ".trash",  # files the user deleted inside Obsidian
        ".git",  # vaults in version control
        "node_modules",
        "__pycache__",
    }
)
_SKIP_FILE_NAMES = frozenset({".DS_Store", "Thumbs.db"})

# Hard cap on individual note size. A 5 MB single-note Obsidian file
# is already pathological (dump of 200k tokens), and we'd rather skip
# it than burn memory. Vaults up to 50k notes work fine with this
# limit because the overwhelming majority are well under 50 KiB.
_MAX_NOTE_BYTES = 5 * 1024 * 1024


class ObsidianImporter:
    """Walk an Obsidian vault ZIP and yield :class:`RawNote` entries.

    Instantiate once per import and iterate ``parse(zip_path)``. Stateless
    across notes — no caching, no cross-note accumulation. Cross-note
    graph construction (resolving wikilink targets to concrete Concept
    IDs) happens at encode time, not parse time.
    """

    platform: str = "obsidian"

    def parse(self, source: Path) -> Iterator[RawNote]:
        """Yield every note in the vault ZIP.

        Args:
            source: Path to the vault ZIP (streamed into a tempfile by
                the dashboard endpoint before this method is called).

        Raises:
            ConversationImportError: if the file isn't a valid ZIP,
                or if the ZIP is empty (no ``.md`` members at all).

        Yields:
            One :class:`RawNote` per parsed markdown file.
        """
        if not source.is_file():
            msg = f"Obsidian vault archive not found: {source}"
            raise ConversationImportError(msg)

        if not zipfile.is_zipfile(source):
            msg = f"Not a valid ZIP archive: {source}"
            raise ConversationImportError(msg)

        found_any_note = False
        try:
            with zipfile.ZipFile(source) as archive:
                for member in archive.infolist():
                    if not _is_eligible_note(member.filename):
                        continue
                    if member.file_size > _MAX_NOTE_BYTES:
                        logger.debug(
                            "obsidian_import_skip_oversize_note",
                            path=member.filename,
                            size=member.file_size,
                        )
                        continue

                    note = self._parse_member(archive, member.filename)
                    if note is None:
                        continue
                    found_any_note = True
                    yield note
        except zipfile.BadZipFile as exc:
            msg = f"Vault archive is corrupt: {exc}"
            raise ConversationImportError(msg) from exc

        if not found_any_note:
            # Empty vault archive is almost certainly a user error
            # (wrong ZIP, password-protected, etc.) — easier to catch
            # as an import-time error than silently producing zero
            # concepts.
            msg = "No markdown notes found in the vault archive"
            raise ConversationImportError(msg)

    # ── Single-note parsing ──────────────────────────────────────

    def _parse_member(
        self,
        archive: zipfile.ZipFile,
        member_name: str,
    ) -> RawNote | None:
        """Read one ZIP member and assemble its :class:`RawNote`.

        Returns ``None`` on unrecoverable per-note errors (unreadable
        bytes, fully malformed content). Notes with merely unparseable
        frontmatter still return — the body is preserved.
        """
        try:
            raw_bytes = archive.read(member_name)
        except (zipfile.BadZipFile, KeyError, OSError) as exc:
            logger.debug(
                "obsidian_import_read_failed",
                path=member_name,
                error=str(exc),
            )
            return None

        text = raw_bytes.decode("utf-8", errors="replace")
        # Normalise line endings so the content hash is stable across
        # Windows-authored vaults shipped in a POSIX ZIP.
        normalised = text.replace("\r\n", "\n").replace("\r", "\n")

        frontmatter, body = extract_frontmatter(normalised)

        aliases = normalise_aliases(frontmatter.get("aliases"))
        frontmatter_tags = tuple(
            RawTag(name=name) for name in normalise_tags(frontmatter.get("tags"))
        )
        body_tags = extract_body_tags(body)
        all_tags = merge_tags(frontmatter_tags, body_tags)

        links = extract_links(body)

        title = _infer_title(frontmatter, member_name, body)
        created_at = normalise_created_at(frontmatter.get("created"))
        content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()

        return RawNote(
            path=_normalise_path(member_name),
            title=title,
            body=body,
            content_hash=content_hash,
            aliases=aliases,
            tags=all_tags,
            links=links,
            created_at=created_at,
        )


# ── Module-level helpers ────────────────────────────────────────────


def _is_eligible_note(member_name: str) -> bool:
    """Return True if ``member_name`` is a note we want to parse."""
    lower = member_name.lower()
    if not lower.endswith(_NOTE_EXTENSIONS):
        return False
    # Directory entries in a ZIP look like ``foo/`` — skip.
    if member_name.endswith("/"):
        return False

    parts = member_name.replace("\\", "/").split("/")
    for part in parts[:-1]:
        if part in _SKIP_DIR_NAMES:
            return False
        # Hidden directories (Unix-style leading dot) that aren't in
        # the explicit skip set are also skipped — vault-unrelated
        # noise.
        if part.startswith(".") and part not in {".", ".."}:
            return False

    basename = parts[-1]
    return basename not in _SKIP_FILE_NAMES


def _normalise_path(raw: str) -> str:
    """Convert ZIP path (which may have backslashes on some clients) to POSIX form.

    The ``path`` we store on :class:`RawNote` is the dedup key, so
    stability across OSes matters. Backslash separators become forward
    slashes; duplicate slashes collapse; leading ``./`` and drive
    letters are stripped.
    """
    cleaned = raw.replace("\\", "/")
    # posixpath.normpath collapses ``a//b`` and resolves ``./a``.
    normalised = posixpath.normpath(cleaned)
    # normpath leaves a leading ``./`` only for ".", nothing else.
    return normalised.lstrip("/")


def _infer_title(frontmatter: dict[str, object], member_name: str, body: str) -> str:
    """Pick a sensible display title for the note.

    Priority:
        1. ``title:`` in frontmatter (stripped, if non-empty).
        2. First-line ``# H1`` heading in the body (stripped of the
           leading hashes).
        3. Filename without extension.
        4. Literal ``"(untitled)"`` as last resort (can only happen
           with a file named ``".md"`` etc.).
    """
    raw_fm = frontmatter.get("title")
    if isinstance(raw_fm, str) and raw_fm.strip():
        return raw_fm.strip()

    h1 = _first_h1(body)
    if h1:
        return h1

    basename = posixpath.basename(member_name.replace("\\", "/"))
    stem = basename.rsplit(".", 1)[0]
    if stem:
        return stem

    return "(untitled)"


def _first_h1(body: str) -> str:
    """Return the first ``# ...`` heading (ATX style) at the start of a line.

    Rejects headings with more than one leading hash (``## foo`` isn't
    a top-level title) and trims inline wikilinks from the heading
    — a common Obsidian pattern like ``# [[Foo]]`` should yield
    ``"Foo"``, not ``"[[Foo]]"``.
    """
    for line in body.splitlines():
        stripped = line.lstrip()
        if not stripped.startswith("# "):
            continue
        # Reject level-2+ headings.
        if stripped.startswith("## "):
            continue
        title = stripped[2:].strip()
        # Strip wrapping wikilink brackets if the whole heading is a link.
        if title.startswith("[[") and title.endswith("]]"):
            inner = title[2:-2].split("|", 1)[0].split("#", 1)[0].strip()
            if inner:
                return inner
        return title
    return ""


# ``RawLink`` is re-exported here purely for type-checker convenience
# inside ``obsidian.py`` — the symbol is the one from ``_models``.
_ = RawLink
