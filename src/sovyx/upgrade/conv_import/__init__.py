"""Conversation importers — onboarding from external assistants.

First-class importers for ChatGPT / Claude / Gemini / Obsidian exports.
Each platform parses its native archive into a platform-neutral
``RawConversation`` stream, which a summary-first encoder feeds into
the brain as one ``Episode`` per conversation plus LLM-derived concept
rows.

This release ships ChatGPT + Claude; Gemini and Obsidian follow the
same interface in later PRs.

Public surface:

    from sovyx.upgrade.conv_import import (
        ChatGPTImporter,
        ClaudeImporter,
        ConversationImportError,
        ImportJobStatus,
        ImportProgressTracker,
        ImportState,
        RawConversation,
        RawMessage,
        source_hash,
        summarize_and_encode,
    )

Ref: IMPL-SUP-015-IMPORTS-INTERMIND-PAGINATION, docs-internal/modules/upgrade.md §Importers.
"""

from __future__ import annotations

from sovyx.upgrade.conv_import._base import (
    ConversationImporter,
    ConversationImportError,
    RawConversation,
    RawMessage,
)
from sovyx.upgrade.conv_import._hash import source_hash
from sovyx.upgrade.conv_import._summary import summarize_and_encode
from sovyx.upgrade.conv_import._tracker import (
    ImportJobStatus,
    ImportProgressTracker,
    ImportState,
)
from sovyx.upgrade.conv_import.chatgpt import ChatGPTImporter
from sovyx.upgrade.conv_import.claude import ClaudeImporter

__all__ = [
    "ChatGPTImporter",
    "ClaudeImporter",
    "ConversationImportError",
    "ConversationImporter",
    "ImportJobStatus",
    "ImportProgressTracker",
    "ImportState",
    "RawConversation",
    "RawMessage",
    "source_hash",
    "summarize_and_encode",
]
