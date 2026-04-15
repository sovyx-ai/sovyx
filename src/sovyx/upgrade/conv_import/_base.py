"""Platform-neutral data model for imported conversations.

Each platform's native archive (ChatGPT ``conversations.json`` tree,
Claude flat ``chat_messages``, Gemini Takeout activity stream) is
flattened into the same ``RawConversation`` shape by its importer.
The downstream summary encoder operates only on this neutral shape,
so a new platform parser only needs to yield these dataclasses.

This module carries no I/O and no brain-subsystem imports — it's pure
data definitions and the Protocol the parsers satisfy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

from sovyx.engine.errors import SovyxError

if TYPE_CHECKING:
    from collections.abc import Iterator
    from datetime import datetime
    from pathlib import Path

MessageRole = Literal["user", "assistant", "system", "tool"]


class ConversationImportError(SovyxError):
    """Raised when an import source is malformed or unreadable."""


@dataclass(frozen=True, slots=True)
class RawMessage:
    """A single turn in an imported conversation.

    Attributes:
        role: The speaker. "system" and "tool" turns are preserved so
            importers can decide per-platform whether to drop them
            before summary encoding.
        text: The message body, already flattened to plain text. Code
            blocks and multimodal fragments are stringified by the
            platform parser before reaching this dataclass.
        created_at: Original platform timestamp, or ``None`` when the
            platform doesn't expose per-message timestamps (e.g. older
            Gemini Takeout exports).
    """

    role: MessageRole
    text: str
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class RawConversation:
    """One imported conversation, ready for summary encoding.

    Attributes:
        platform: Lowercase platform identifier — ``"chatgpt"``,
            ``"claude"``, ``"gemini"``. Used as the first component of
            :func:`source_hash` for dedup and stored on the generated
            Episode's metadata.
        conversation_id: The platform's own stable identifier. For
            ChatGPT that's the ``conversation_id`` UUID; for Claude
            the conversation ``uuid``. Used as the dedup key together
            with ``platform``.
        title: Display title. Empty string when the platform doesn't
            carry one (the summary encoder falls back to a generated
            label in that case).
        created_at: When the conversation was started on the source
            platform. Used for Episode ``created_at`` so chronological
            retrieval ordering matches the user's real history.
        messages: Chronological tuple of turns. Immutable because the
            summary encoder scans it multiple times and we don't want
            accidental mutation between scans.
    """

    platform: str
    conversation_id: str
    title: str
    created_at: datetime | None
    messages: tuple[RawMessage, ...]

    def turn_count(self) -> int:
        """Number of user + assistant turns (excluding system/tool)."""
        return sum(1 for m in self.messages if m.role in ("user", "assistant"))

    def first_user_text(self) -> str:
        """First user turn text, or empty string when none exists."""
        for m in self.messages:
            if m.role == "user" and m.text:
                return m.text
        return ""

    def last_assistant_text(self) -> str:
        """Last assistant turn text, or empty string when none exists."""
        for m in reversed(self.messages):
            if m.role == "assistant" and m.text:
                return m.text
        return ""


class ConversationImporter(Protocol):
    """Interface every platform parser satisfies.

    Implementations are responsible for opening the source artefact,
    walking its platform-specific structure, and yielding one
    ``RawConversation`` per coherent conversation. They MUST NOT touch
    the brain subsystem — the caller drives ``summarize_and_encode``.
    """

    platform: str
    """Lowercase platform identifier embedded into each yielded
    RawConversation and used as the first component of source_hash."""

    def parse(self, source: Path) -> Iterator[RawConversation]:
        """Yield conversations from the given source artefact.

        Args:
            source: Path to the platform's export file (e.g. the
                ChatGPT ``conversations.json`` extracted from the
                user's data-export ZIP).

        Yields:
            One ``RawConversation`` per conversation found. Malformed
            entries are skipped silently — logging is the parser's
            responsibility, and skipped-count should surface via the
            caller's progress tracker.
        """
