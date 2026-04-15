"""Grok (xAI) conversation-export parser — **best-guess v0**.

.. warning::

    The exact JSON schema of Grok's "Download your data" export is
    **not publicly documented in detail** at the time this parser
    was written. This implementation assumes a shape that's
    consistent with the common chat-product pattern (Claude, early
    OpenAI) and tolerates a range of field names. If a user brings
    a real Grok export whose shape differs materially from what this
    parser expects, please open an issue with a sanitised sample and
    we'll tighten the schema in v1.

Assumed shape — tolerated variants marked in square brackets:

.. code-block:: json

    [
      {
        "id"           | "conversation_id": "<string>",
        "title"        | "name": "<string>",
        "created_at"   | "create_time" | "started_at": "ISO 8601 | unix epoch",
        "updated_at"   | "update_time": "ISO 8601",
        "messages":    [
          {
            "id"         | "message_id": "<string>",
            "role"       | "author"      | "sender": "user" | "assistant" | "system",
            "content"    | "text"        | "body"   : "<string>" | [<text parts>],
            "created_at" | "create_time" | "timestamp": "ISO 8601 | unix epoch"
          }
        ]
      }
    ]

Design choices (v0):

* **Flat message list**. Grok's UI doesn't expose regeneration
  branches the way ChatGPT does, so we don't attempt tree walking.
  If a future export surfaces branches, the tree walk from
  ``ChatGPTImporter`` is the natural upgrade path.
* **Role normalisation**. ``"user" | "assistant" | "system" | "tool"``
  pass through; anything else lands as ``"system"`` (defensive
  default, dropped by the summary encoder before LLM submission).
* **Timestamp normalisation**. Accepts ISO-8601 strings (with or
  without the ``Z`` suffix) *or* Unix epoch seconds. Both land as
  timezone-aware UTC datetimes.
* **Content flattening**. Works whether ``content`` is a plain
  string or a list of typed parts (``[{"type": "text", "text": ...}]``
  like the newer Claude / Anthropic shape). Non-text parts are
  stringified with a ``[type]`` marker, matching how ``ClaudeImporter``
  handles the typed-parts case.

Ref: IMPL-SUP-015-IMPORTS-INTERMIND-PAGINATION §5 (fifth platform).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
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

# Role strings we recognise directly — everything else falls through
# to "system" and gets dropped before summarisation.
_VALID_ROLES: frozenset[str] = frozenset({"user", "assistant", "system", "tool"})

# Synonyms seen in competing chat products; Grok likely uses one of
# these for the role column.
_ROLE_SYNONYMS: dict[str, MessageRole] = {
    "human": "user",
    "you": "user",
    "ai": "assistant",
    "bot": "assistant",
    "model": "assistant",
    "grok": "assistant",
}


class GrokImporter:
    """Parse a Grok data-export JSON file into ``RawConversation`` objects.

    Instantiate once per import and iterate ``parse(path)`` to stream
    conversations. Fully synchronous — the export file is expected to
    fit comfortably in memory (typical Grok exports are well under
    50 MB).

    Schema tolerance is deliberate: field names may vary as xAI
    evolves the export. A shape we don't recognise yields a debug log
    + the entry is skipped rather than crashing the import.
    """

    platform: str = "grok"

    def parse(self, source: Path) -> Iterator[RawConversation]:
        """Yield every conversation in the export.

        Args:
            source: Path to the Grok ``conversations.json`` (or
                similarly-named file extracted from the user's data
                export).

        Raises:
            ConversationImportError: If the file is missing,
                unreadable, not JSON, or neither a JSON array at the
                top level nor an envelope object wrapping one.
        """
        if not source.is_file():
            msg = f"Grok export file not found: {source}"
            raise ConversationImportError(msg)

        try:
            payload = json.loads(source.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            msg = f"Invalid Grok export JSON: {exc}"
            raise ConversationImportError(msg) from exc

        # Accept two envelopes:
        #   1. Top-level array: ``[conv, conv, ...]``
        #   2. Wrapped object: ``{"conversations": [...]}`` — seen in
        #      Anthropic / xAI exports when the top level also carries
        #      account metadata.
        entries: list[Any] = []
        if isinstance(payload, list):
            entries = payload
        elif isinstance(payload, dict):
            for key in ("conversations", "chats", "threads", "data"):
                candidate = payload.get(key)
                if isinstance(candidate, list):
                    entries = candidate
                    break
            else:
                msg = (
                    "Grok export must be a JSON array, or a JSON object with a "
                    "'conversations' / 'chats' / 'threads' / 'data' list field"
                )
                raise ConversationImportError(msg)
        else:
            msg = "Grok export must be a JSON array or object at the top level"
            raise ConversationImportError(msg)

        for idx, entry in enumerate(entries):
            if not isinstance(entry, dict):
                logger.debug("grok_import_skip_non_object", index=idx)
                continue
            parsed = self._parse_conversation(entry)
            if parsed is not None:
                yield parsed

    # ── Single-conversation parsing ──────────────────────────────

    def _parse_conversation(self, entry: dict[str, Any]) -> RawConversation | None:
        """Convert one conversation entry into the neutral shape.

        Returns ``None`` when the entry has no identifiable
        ``conversation_id`` or no usable text-bearing messages.
        """
        conversation_id = _pick_str(
            entry,
            keys=("id", "conversation_id", "conversationId", "uuid"),
        )
        if not conversation_id:
            logger.debug("grok_import_skip_missing_id")
            return None

        raw_messages = _pick_list(
            entry,
            keys=("messages", "turns", "message_list"),
        )
        if not raw_messages:
            logger.debug(
                "grok_import_skip_empty_messages",
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
                "grok_import_skip_no_text",
                conversation_id=conversation_id,
            )
            return None

        title = _pick_str(entry, keys=("title", "name", "subject"))
        created_at = _pick_timestamp(
            entry,
            keys=("created_at", "create_time", "started_at", "createdAt"),
        )

        return RawConversation(
            platform=self.platform,
            conversation_id=conversation_id,
            title=title,
            created_at=created_at,
            messages=tuple(messages),
        )

    # ── Single-message extraction ────────────────────────────────

    @staticmethod
    def _extract_message(message: dict[str, Any]) -> RawMessage | None:
        """Convert one message dict into :class:`RawMessage`.

        Returns ``None`` when the message has no text after flattening.
        """
        role_raw = _pick_str(message, keys=("role", "author", "sender", "from"))
        role = _normalise_role(role_raw)

        text = _flatten_content(message)
        if not text.strip():
            return None

        created_at = _pick_timestamp(
            message,
            keys=("created_at", "create_time", "timestamp", "ts", "createdAt"),
        )
        return RawMessage(role=role, text=text, created_at=created_at)


# ── Module-level helpers ────────────────────────────────────────────


def _pick_str(entry: dict[str, Any], *, keys: tuple[str, ...]) -> str:
    """Return the first non-empty string value among ``keys``.

    Tolerant to nested author objects: ``author: {"role": "user"}``
    works because the caller can pass the inner dict. For top-level
    string fields, returns ``""`` when nothing matches.
    """
    for key in keys:
        value = entry.get(key)
        if isinstance(value, str):
            stripped = value.strip()
            if stripped:
                return stripped
        # One level of nesting tolerated — Grok's ``author`` field is
        # sometimes a dict: ``author: {"role": "user", "name": "..."}``.
        if isinstance(value, dict):
            for nested_key in ("role", "type", "name"):
                nested = value.get(nested_key)
                if isinstance(nested, str) and nested.strip():
                    return nested.strip()
    return ""


def _pick_list(entry: dict[str, Any], *, keys: tuple[str, ...]) -> list[Any]:
    """Return the first list value among ``keys``, or ``[]``."""
    for key in keys:
        value = entry.get(key)
        if isinstance(value, list):
            return value
    return []


def _pick_timestamp(
    entry: dict[str, Any],
    *,
    keys: tuple[str, ...],
) -> datetime | None:
    """Return the first parseable timestamp value among ``keys``.

    Accepts ISO 8601 strings (with or without ``Z``) and Unix epoch
    floats/ints. Returns ``None`` when nothing parses — chronological
    ordering then falls back to export-file order, matching the
    behaviour of the other importers.
    """
    for key in keys:
        value = entry.get(key)
        parsed = _parse_timestamp(value)
        if parsed is not None:
            return parsed
    return None


def _parse_timestamp(value: object) -> datetime | None:
    """Best-effort coerce to a timezone-aware datetime in UTC."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), UTC)
        except (OSError, ValueError, OverflowError):
            return None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        # Common variants: ``2024-10-20T09:31:00Z``, ``2024-10-20T09:31:00+00:00``.
        try:
            parsed = datetime.fromisoformat(stripped.replace("Z", "+00:00"))
        except ValueError:
            # Numeric-looking string as a fallback.
            try:
                return datetime.fromtimestamp(float(stripped), UTC)
            except (ValueError, OSError, OverflowError):
                return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return None


def _normalise_role(raw: str) -> MessageRole:
    """Map the incoming role string to the canonical :data:`MessageRole`.

    Unrecognised roles fall back to ``"system"`` — matching every
    other importer in this subpackage and ensuring the turn is
    dropped before reaching the summary encoder.
    """
    if not raw:
        return "system"
    lower = raw.strip().lower()
    if lower in _VALID_ROLES:
        return lower  # type: ignore[return-value]
    synonym = _ROLE_SYNONYMS.get(lower)
    if synonym is not None:
        return synonym
    return "system"


def _flatten_content(message: dict[str, Any]) -> str:
    """Extract plain text from Grok's content payload.

    Three shapes tolerated:

    1. Plain string — ``"content": "hello"``.
    2. Typed parts list — ``"content": [{"type": "text", "text": "..."}, ...]``.
       Non-text parts get a ``[type]`` marker so the summary LLM sees
       *something* for them.
    3. Legacy ``"text"``/``"body"`` — same as case 1 but under a
       different key.
    """
    # Case 1 / 3: plain string under one of the expected keys.
    for key in ("content", "text", "body", "message"):
        value = message.get(key)
        if isinstance(value, str):
            return value.strip()
        # Case 2: typed parts list.
        if isinstance(value, list):
            return _flatten_parts(value)
    return ""


def _flatten_parts(parts: list[Any]) -> str:
    """Flatten a typed-parts list (Claude-style) to text."""
    chunks: list[str] = []
    for part in parts:
        if isinstance(part, str):
            if part:
                chunks.append(part)
            continue
        if not isinstance(part, dict):
            continue
        part_type = str(part.get("type") or "").strip().lower()
        if part_type == "text":
            text = part.get("text") or part.get("content")
            if isinstance(text, str) and text:
                chunks.append(text)
        elif part_type:
            # Unknown part type — note its presence for the summariser
            # so it doesn't mistake the gap for an abrupt topic shift.
            chunks.append(f"[{part_type}]")
    return "\n".join(chunks).strip()
