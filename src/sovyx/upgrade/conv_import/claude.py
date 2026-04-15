"""Claude data-export parser.

Reads the ``conversations.json`` that Anthropic emails users when they
request a data export (Settings → Account → Export data). The format
is a JSON array of conversation objects; each conversation holds a
**flat** ``chat_messages`` list in chronological order. No branching,
no tree walk — a substantially simpler shape than ChatGPT's
regeneration-capable ``mapping`` DAG.

Shape (2024-era export — fields tolerated optionally):

    [
      {
        "uuid": "conversation-uuid",
        "name": "Conversation title",               # may be "" or missing
        "created_at": "2024-01-15T10:30:45.123456Z", # ISO 8601 UTC
        "updated_at": "2024-01-15T11:45:00.123456Z",
        "account": { "uuid": "..." },
        "chat_messages": [
          {
            "uuid": "message-uuid",
            "sender": "human",                       # or "assistant"
            "text": "message body",                  # legacy field
            "content": [                              # newer typed parts
              {"type": "text", "text": "message body"},
              {"type": "image", ...}
            ],
            "created_at": "2024-01-15T10:30:45.123456Z",
            "attachments": [ ... ],                   # ignored (v1 non-goal)
            "files": [ ... ]                          # ignored (v1 non-goal)
          }
        ]
      }
    ]

Key differences from ChatGPT handled here:

* ``sender: "human"`` is mapped to ``role: "user"`` — Sovyx's internal
  :data:`MessageRole` uses ``"user"`` consistently. ``"assistant"``
  passes through. Unknown sender values fall through to ``"system"``
  (defensive default, same as the ChatGPT parser).
* Timestamps are ISO 8601 strings, not Unix-epoch floats. Parsed via
  :func:`datetime.fromisoformat`, which accepts the ``Z`` suffix
  natively on Python 3.11+.
* Newer exports carry ``content: [{type: "text", text: "..."}]``
  typed parts. Legacy exports only carry a flat ``text`` string. The
  parser prefers ``content[]`` when present and falls back to
  ``text``; both can appear simultaneously.
* Attachments (``attachments[].extracted_content``) and file metadata
  are NOT included in the message body. This matches the v1 non-goal
  of ignoring attachments — when attachments become first-class, the
  treatment should be consistent across all platforms.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sovyx.observability.logging import get_logger
from sovyx.upgrade.conv_import._base import (
    ConversationImportError,
    MessageRole,
    RawConversation,
    RawMessage,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

logger = get_logger(__name__)

# Claude exports use only "human" and "assistant". Anything else is
# treated as "system" by the defensive fall-through below.
_SENDER_TO_ROLE: dict[str, MessageRole] = {
    "human": "user",
    "assistant": "assistant",
}


class ClaudeImporter:
    """Parse a Claude ``conversations.json`` into RawConversations.

    Instantiate once per import and iterate ``parse(path)`` to stream
    conversations. Fully synchronous; the export file is typically
    1-10 MB so a full JSON load is acceptable in v1.
    """

    platform: str = "claude"

    def parse(self, source: Path) -> Iterator[RawConversation]:
        """Yield every conversation in the archive.

        Args:
            source: Path to ``conversations.json`` (already extracted
                from the user's Claude data-export ZIP).

        Raises:
            ConversationImportError: If the file is missing, unreadable,
                not JSON, or not a top-level JSON array.

        Yields:
            One ``RawConversation`` per entry with a non-empty
            ``chat_messages`` list and at least one text-bearing turn.
        """
        if not source.is_file():
            msg = f"Claude conversations.json not found: {source}"
            raise ConversationImportError(msg)

        try:
            payload = json.loads(source.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            msg = f"Invalid conversations.json: {exc}"
            raise ConversationImportError(msg) from exc

        if not isinstance(payload, list):
            msg = "conversations.json must be a JSON array at the top level"
            raise ConversationImportError(msg)

        for idx, entry in enumerate(payload):
            if not isinstance(entry, dict):
                logger.debug("claude_import_skip_non_object", index=idx)
                continue
            parsed = self._parse_conversation(entry)
            if parsed is not None:
                yield parsed

    # ── Single-conversation parsing ─────────────────────────────────

    def _parse_conversation(self, entry: dict[str, Any]) -> RawConversation | None:
        """Convert one conversation entry into the neutral shape.

        Returns ``None`` when the entry has no ``uuid`` or no usable
        messages after text extraction.
        """
        conversation_id = str(entry.get("uuid") or "").strip()
        if not conversation_id:
            logger.debug("claude_import_skip_missing_uuid")
            return None

        raw_messages = entry.get("chat_messages")
        if not isinstance(raw_messages, list) or not raw_messages:
            logger.debug(
                "claude_import_skip_empty_chat_messages",
                conversation_id=conversation_id,
            )
            return None

        messages: list[RawMessage] = []
        for m in raw_messages:
            if not isinstance(m, dict):
                continue
            parsed = self._extract_message(m)
            if parsed is not None:
                messages.append(parsed)

        if not messages:
            logger.debug(
                "claude_import_skip_no_text",
                conversation_id=conversation_id,
            )
            return None

        title = str(entry.get("name") or "").strip()
        created_at = _iso_to_datetime(entry.get("created_at"))

        return RawConversation(
            platform=self.platform,
            conversation_id=conversation_id,
            title=title,
            created_at=created_at,
            messages=tuple(messages),
        )

    # ── Single-message extraction ───────────────────────────────────

    @staticmethod
    def _extract_message(message: dict[str, Any]) -> RawMessage | None:
        """Convert one ``chat_messages`` entry into a ``RawMessage``.

        Returns ``None`` for messages whose text is empty after
        flattening both the legacy ``text`` field and the newer
        ``content[]`` typed parts.
        """
        sender_raw = str(message.get("sender") or "").strip().lower()
        role: MessageRole = _SENDER_TO_ROLE.get(sender_raw, "system")

        text = _flatten_claude_content(message)
        if not text.strip():
            return None

        created_at = _iso_to_datetime(message.get("created_at"))
        return RawMessage(role=role, text=text, created_at=created_at)


def _flatten_claude_content(message: dict[str, Any]) -> str:
    """Extract plain text from a Claude message.

    Prefers the newer typed ``content[]`` array — concatenates every
    ``{type: "text", text: "..."}`` entry and emits ``[type]`` markers
    for non-text fragments (images, tool results) so the summary LLM
    can still see *something* for those turns.

    Falls back to the legacy ``text`` field for older exports that
    predate the typed-content format. When both are present (some
    transition-era exports carry both), ``content[]`` wins because
    it's the structured source of truth going forward.
    """
    content = message.get("content")
    if isinstance(content, list) and content:
        chunks: list[str] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            part_type = str(part.get("type") or "").strip().lower()
            if part_type == "text":
                text = part.get("text")
                if isinstance(text, str) and text:
                    chunks.append(text)
            elif part_type:
                chunks.append(f"[{part_type}]")
        if chunks:
            return "\n".join(chunks).strip()

    # Legacy format / missing content[] — fall back to the flat field.
    text = message.get("text")
    if isinstance(text, str):
        return text.strip()

    return ""


def _iso_to_datetime(value: object) -> datetime | None:
    """Parse an ISO 8601 timestamp string to a timezone-aware datetime.

    Accepts the ``Z`` suffix (UTC shorthand, valid in Python 3.11+)
    and microsecond precision. Returns ``None`` for missing/malformed
    values — chronological ordering then falls back to export-file
    order for those messages.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
