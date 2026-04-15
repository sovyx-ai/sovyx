"""Obsidian vault importer — knowledge-first, not conversation-first.

This subpackage is **parallel** to ``sovyx.upgrade.conv_import``, not a
layer on top of it. Obsidian notes are already distilled knowledge —
the user wrote them as atomic concepts and declared the relations via
``[[wikilinks]]`` — so the encoding path differs fundamentally from
the summary-first flow that imports chat transcripts:

=====================  ==================  =====================
Dimension              conv_import         vault_import
=====================  ==================  =====================
Source                 single JSON file    directory (ZIP upload)
Unit                   conversation        note
Needs LLM summary?     yes                 no (note body *is* the
                                           concept content)
Graph shape            per-turn Hebbian    explicit wikilinks →
                                           Relations declared by
                                           the author
Cost per unit          $0.001 – $0.003     $0 (heuristic category
                                           inference)
Dedup key              (platform,          (path, content_hash)
                        conversation_id)
=====================  ==================  =====================

Public surface::

    from sovyx.upgrade.vault_import import (
        ObsidianImporter,
        RawNote,
        RawLink,
        RawTag,
        VaultSource,
        encode_note,
    )

The HTTP endpoint (``POST /api/import/conversations``) dispatches on
``platform="obsidian"`` to :class:`ObsidianImporter`, which streams
``RawNote`` instances out of the ZIP, and then :func:`encode_note`
writes concepts + relations directly to :class:`BrainService`. The
existing :class:`ImportProgressTracker` and the ``conversation_imports``
dedup table are reused unchanged — dedup keys are name-spaced
(``obsidian:<path>:<content_hash>``) so there's no collision with chat
imports.

Ref: IMPL-SUP-015-IMPORTS-INTERMIND-PAGINATION §4.
"""

from __future__ import annotations

from sovyx.upgrade.vault_import._encoder import encode_note
from sovyx.upgrade.vault_import._models import (
    RawLink,
    RawNote,
    RawTag,
    VaultSource,
)
from sovyx.upgrade.vault_import.obsidian import ObsidianImporter

__all__ = [
    "ObsidianImporter",
    "RawLink",
    "RawNote",
    "RawTag",
    "VaultSource",
    "encode_note",
]
