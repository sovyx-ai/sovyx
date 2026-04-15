"""ChatGPT data-export parser.

Reads the ``conversations.json`` that OpenAI emails users when they
request a data export (Settings → Data controls → Export). The format
is a JSON array of conversation objects; each conversation holds a
``mapping`` keyed by node UUID that forms a TREE — branching happens
whenever the user regenerated a response. Only the mainline from the
``current_node`` tip back up to the root is exposed, so forks/dead
ends aren't imported.

Shape (2025-era export — fields tolerated optionally):

    [
      {
        "conversation_id": "uuid",          # may also appear as "id"
        "title": "string",
        "create_time": 1711111111.111,      # Unix epoch float
        "update_time": 1711111111.222,
        "current_node": "uuid",              # tip of mainline
        "mapping": {
          "uuid": {
            "id": "uuid",
            "parent": "parent-uuid" | null,
            "children": ["uuid", ...],
            "message": {                     # may be null for root
              "id": "uuid",
              "author": {"role": "user"|"assistant"|"system"|"tool"},
              "create_time": 1711111111.333,
              "content": {
                "content_type": "text"|"code"|"multimodal_text"|...,
                "parts": ["..."]             # typed per content_type
              }
            }
          }
        }
      }
    ]

Anything unexpected is tolerated — malformed conversations are skipped
with a logger warning, non-text content types are stringified with a
marker, and unknown roles fall through as "system" (safest default
— gets dropped before summary encoding anyway).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

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

_VALID_ROLES: frozenset[str] = frozenset({"user", "assistant", "system", "tool"})


class ChatGPTImporter:
    """Parse a ChatGPT ``conversations.json`` into RawConversations.

    Instantiate once per import and iterate ``parse(path)`` to stream
    conversations. The parser is fully synchronous — all I/O is a
    single JSON load of the source file. If the file is huge
    (hundreds of MB) a streaming JSON parser would be an upgrade, but
    the data-export files are typically 1-50 MB so a full load is
    acceptable in v1.
    """

    platform: str = "chatgpt"

    def parse(self, source: Path) -> Iterator[RawConversation]:
        """Yield every mainline conversation in the archive.

        Args:
            source: Path to ``conversations.json`` (already extracted
                from the user's data-export ZIP).

        Raises:
            ConversationImportError: If the file is missing, unreadable,
                not JSON, or not a top-level JSON array.

        Yields:
            One ``RawConversation`` per entry with a non-empty mainline.
        """
        if not source.is_file():
            msg = f"ChatGPT conversations.json not found: {source}"
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
                logger.debug(
                    "chatgpt_import_skip_non_object",
                    index=idx,
                    exc_info=False,
                )
                continue
            parsed = self._parse_conversation(entry)
            if parsed is not None:
                yield parsed

    # ── Single-conversation parsing ─────────────────────────────────

    def _parse_conversation(self, entry: dict[str, Any]) -> RawConversation | None:
        """Flatten one conversation entry into the neutral shape.

        Returns ``None`` when the entry has no usable mainline (empty
        mapping, missing current_node without children fallback, or
        zero text messages after role filtering).
        """
        conversation_id = str(
            entry.get("conversation_id") or entry.get("id") or "",
        ).strip()
        if not conversation_id:
            logger.debug("chatgpt_import_skip_missing_id")
            return None

        mapping = entry.get("mapping")
        if not isinstance(mapping, dict) or not mapping:
            logger.debug(
                "chatgpt_import_skip_empty_mapping",
                conversation_id=conversation_id,
            )
            return None

        node_ids = self._walk_mainline(entry, mapping)
        if not node_ids:
            logger.debug(
                "chatgpt_import_skip_no_mainline",
                conversation_id=conversation_id,
            )
            return None

        messages: list[RawMessage] = []
        for node_id in node_ids:
            node = mapping.get(node_id)
            if not isinstance(node, dict):
                continue
            msg = self._extract_message(node)
            if msg is not None:
                messages.append(msg)

        if not messages:
            logger.debug(
                "chatgpt_import_skip_no_text",
                conversation_id=conversation_id,
            )
            return None

        title = str(entry.get("title") or "").strip()
        created_at = _epoch_to_datetime(entry.get("create_time"))

        return RawConversation(
            platform=self.platform,
            conversation_id=conversation_id,
            title=title,
            created_at=created_at,
            messages=tuple(messages),
        )

    # ── Tree walk ───────────────────────────────────────────────────

    def _walk_mainline(
        self,
        entry: dict[str, Any],
        mapping: dict[str, Any],
    ) -> list[str]:
        """Return node IDs from the root to the mainline tip, in order.

        Primary path: walk parents from ``current_node`` up to the
        root, then reverse. Falls back to a deepest-child DFS from any
        root node when ``current_node`` is missing or dangling.
        """
        current = entry.get("current_node")
        if isinstance(current, str) and current in mapping:
            ids_reverse: list[str] = []
            cursor: str | None = current
            visited: set[str] = set()
            while cursor is not None and cursor not in visited:
                visited.add(cursor)
                ids_reverse.append(cursor)
                node = mapping.get(cursor)
                if not isinstance(node, dict):
                    break
                parent = node.get("parent")
                cursor = parent if isinstance(parent, str) else None
            return list(reversed(ids_reverse))

        # Fallback: find the root (parent is None) and DFS the deepest
        # child chain. Handles older or truncated exports that omit
        # ``current_node``.
        roots = [
            nid
            for nid, node in mapping.items()
            if isinstance(node, dict) and node.get("parent") is None
        ]
        if not roots:
            return []
        return self._deepest_chain(roots[0], mapping)

    def _deepest_chain(
        self,
        start: str,
        mapping: dict[str, Any],
    ) -> list[str]:
        """Return the longest root-to-leaf chain from ``start``."""
        best: list[str] = []

        def walk(nid: str, chain: list[str]) -> None:
            nonlocal best
            node = mapping.get(nid)
            if not isinstance(node, dict):
                return
            new_chain = [*chain, nid]
            children = node.get("children") or []
            if not isinstance(children, list) or not children:
                if len(new_chain) > len(best):
                    best = new_chain
                return
            for child in children:
                if isinstance(child, str) and child in mapping:
                    walk(child, new_chain)

        walk(start, [])
        return best

    # ── Node → RawMessage ───────────────────────────────────────────

    @staticmethod
    def _extract_message(node: dict[str, Any]) -> RawMessage | None:
        """Convert one mapping-tree node into a ``RawMessage``.

        Returns ``None`` for nodes without a ``message`` (root) and for
        messages whose text is empty after stringification.
        """
        message = node.get("message")
        if not isinstance(message, dict):
            return None

        author = message.get("author") or {}
        raw_role = author.get("role") if isinstance(author, dict) else None
        role_str = str(raw_role).strip().lower() if raw_role else ""
        role: MessageRole = cast("MessageRole", role_str) if role_str in _VALID_ROLES else "system"

        text = _flatten_content(message.get("content"))
        if not text.strip():
            return None

        created_at = _epoch_to_datetime(message.get("create_time"))
        return RawMessage(role=role, text=text, created_at=created_at)


def _flatten_content(content: object) -> str:
    """Convert a message's ``content`` field into plain text.

    Text messages return the joined ``parts``. Non-text content types
    (code, multimodal, images) are stringified with a marker so the
    summary LLM sees *something* meaningful and the author can still
    distinguish "she sent an image" from an empty turn.
    """
    if not isinstance(content, dict):
        return ""

    content_type = str(content.get("content_type") or "text").lower()
    parts = content.get("parts")

    if content_type == "text" and isinstance(parts, list):
        return "\n".join(str(p) for p in parts if isinstance(p, str) and p).strip()

    # Non-text content type — best-effort stringify.
    if isinstance(parts, list):
        chunks: list[str] = []
        for p in parts:
            if isinstance(p, str) and p:
                chunks.append(p)
            elif isinstance(p, dict):
                # Typical multimodal shape: {"content_type": "image_asset_pointer", ...}
                ct = p.get("content_type", "unknown")
                chunks.append(f"[{content_type}:{ct}]")
        if chunks:
            return "\n".join(chunks).strip()

    # Some older exports stash text in content.text instead of parts.
    text = content.get("text")
    if isinstance(text, str):
        return text.strip()

    return f"[{content_type}]"


def _epoch_to_datetime(value: object) -> datetime | None:
    """Convert ChatGPT's float-seconds epoch to a UTC datetime.

    Returns ``None`` for missing/malformed timestamps — chronological
    ordering falls back to export-file order for those messages.
    """
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(float(value), UTC)  # type: ignore[arg-type]
    except (TypeError, ValueError, OSError):
        return None
