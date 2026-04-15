"""Platform-neutral data model for imported Obsidian notes.

Intentionally narrower than :mod:`conv_import._base`: no Protocol for
multiple implementations (we only have Obsidian for now), no
``MessageRole`` enum (notes don't have speakers). If a second vault
format appears (Foam, Dendron, Roam) and the shape matches this
closely, the types here promote to a shared ``_base``.

All models are frozen dataclasses with ``slots=True`` — ~2× memory
win over plain classes at scale (a large vault easily runs 5 000
notes × dozens of links each).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path


@dataclass(frozen=True, slots=True)
class RawLink:
    """One ``[[wikilink]]`` inside a note body.

    Attributes:
        target: Name of the note being linked to — the part before any
            ``|`` (display override) or ``#`` (heading fragment).
            Resolution to a real ``Concept`` happens at encode time,
            not parse time: the parser records raw link targets and
            the encoder resolves them after every note has been seen,
            so forward references work without a second file pass.
        is_embed: ``True`` if the source form was ``![[...]]``.
            Embeds get a stronger relation type (``PART_OF``, weight
            0.7) than plain wikilinks (``RELATED_TO``, 0.5).
    """

    target: str
    is_embed: bool = False


@dataclass(frozen=True, slots=True)
class RawTag:
    """One ``#tag`` inside a note body or frontmatter.

    Nested tags (``#project/alpha``) are stored here as the full
    ``name`` (``"project/alpha"``); the encoder is responsible for
    expanding them into a chain of tag Concepts with ``PART_OF``
    relations (``alpha`` → ``project``).
    """

    name: str


@dataclass(frozen=True, slots=True)
class RawNote:
    """One parsed note, ready for encoding into concepts + relations.

    Attributes:
        path: Relative POSIX path inside the vault, used as the stable
            identity for dedup (``obsidian:<path>:<content_hash>``).
            Preferred to a UUID because the user reorganises their
            vault by moving files, and path-based identity lets us
            detect those moves as "content unchanged" on re-import.
        title: Display title — ``title:`` frontmatter key if present,
            else the filename stem. Never empty; a note without a
            title gets ``"(untitled)"``.
        body: Markdown body with frontmatter stripped. This becomes
            the ``Concept.content`` verbatim — no LLM distillation.
        aliases: Alternative names from ``aliases:`` frontmatter.
            Stored on ``Concept.metadata["aliases"]`` and surfaced in
            hybrid retrieval so ``"PT Grammar"`` matches the concept
            named ``"Portuguese Grammar Notes"``.
        tags: Every ``#tag`` found, including those from the frontmatter
            ``tags:`` field. De-duplicated by the parser.
        links: Every ``[[wikilink]]`` in the body, in source order.
            Duplicates are *kept* — a note that links to the same
            target three times signals stronger affinity than one
            linking once, and the encoder uses that count to bump
            relation weight.
        content_hash: SHA-256 of ``body`` (normalised line endings).
            Combined with ``path`` to form the dedup key, so an
            unchanged note on re-import is a no-op, but editing the
            body causes a re-encode.
        created_at: ``created:`` frontmatter key, or ``None`` if the
            frontmatter doesn't declare one. Used for the generated
            Concept's ``created_at`` timestamp so chronological
            retrieval ordering matches the user's real note history.
    """

    path: str
    title: str
    body: str
    content_hash: str
    aliases: tuple[str, ...] = ()
    tags: tuple[RawTag, ...] = ()
    links: tuple[RawLink, ...] = ()
    created_at: datetime | None = None

    def link_targets(self) -> tuple[str, ...]:
        """Unique link targets (order-preserving), useful for Relation creation."""
        seen: set[str] = set()
        out: list[str] = []
        for link in self.links:
            if link.target not in seen:
                seen.add(link.target)
                out.append(link.target)
        return tuple(out)


@dataclass(frozen=True, slots=True)
class VaultSource:
    """Metadata for the vault archive being imported."""

    root_path: Path
    note_count: int = 0
    skipped_files: tuple[str, ...] = field(default_factory=tuple)
