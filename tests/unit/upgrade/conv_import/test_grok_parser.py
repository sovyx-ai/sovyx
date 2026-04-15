"""Tests for :class:`GrokImporter` — the **best-guess v0** shape.

The parser is deliberately tolerant of field-name variance because
the real Grok export schema is not publicly pinned. These tests
exercise every branch the tolerant lookups can take, so any future
adjustment against a real sample will surface as specific test-case
updates rather than a surprise in production.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from sovyx.upgrade.conv_import._base import ConversationImportError
from sovyx.upgrade.conv_import.grok import GrokImporter


def _write(tmp_path: Path, payload: Any, name: str = "grok.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _msg(**kwargs: Any) -> dict[str, Any]:
    """Build a message dict with sensible defaults."""
    defaults = {"role": "user", "content": "hello"}
    defaults.update(kwargs)
    return defaults


def _conv(**kwargs: Any) -> dict[str, Any]:
    """Build a conversation dict with sensible defaults."""
    defaults = {
        "id": "conv-1",
        "title": "Conv 1",
        "created_at": "2024-10-20T09:30:00Z",
        "messages": [_msg(role="user", content="q"), _msg(role="assistant", content="a")],
    }
    defaults.update(kwargs)
    return defaults


# ── Happy path ──────────────────────────────────────────────────────


class TestHappyPath:
    def test_top_level_array(self, tmp_path: Path) -> None:
        path = _write(tmp_path, [_conv(), _conv(id="conv-2")])
        convs = list(GrokImporter().parse(path))
        assert [c.conversation_id for c in convs] == ["conv-1", "conv-2"]
        assert convs[0].platform == "grok"

    def test_wrapped_object_with_conversations_key(self, tmp_path: Path) -> None:
        payload = {
            "account_email": "x@example.com",
            "conversations": [_conv()],
        }
        path = _write(tmp_path, payload)
        convs = list(GrokImporter().parse(path))
        assert len(convs) == 1

    @pytest.mark.parametrize("wrapper_key", ["chats", "threads", "data"])
    def test_other_wrapper_keys(self, tmp_path: Path, wrapper_key: str) -> None:
        path = _write(tmp_path, {wrapper_key: [_conv()]})
        convs = list(GrokImporter().parse(path))
        assert len(convs) == 1

    def test_title_rendered(self, tmp_path: Path) -> None:
        path = _write(tmp_path, [_conv(title="My Chat")])
        assert list(GrokImporter().parse(path))[0].title == "My Chat"

    def test_role_mapping(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            [
                _conv(
                    messages=[
                        _msg(role="human", content="hi"),
                        _msg(role="grok", content="hello"),
                    ]
                )
            ],
        )
        conv = list(GrokImporter().parse(path))[0]
        assert [m.role for m in conv.messages] == ["user", "assistant"]

    def test_alternative_field_names(self, tmp_path: Path) -> None:
        """``conversation_id`` / ``name`` / ``turns`` instead of id/title/messages."""
        payload = [
            {
                "conversation_id": "c1",
                "name": "Alt Fields",
                "create_time": 1729416600,  # epoch-seconds alternative
                "turns": [
                    {"author": "user", "text": "hi"},
                    {"author": "assistant", "text": "hello"},
                ],
            }
        ]
        path = _write(tmp_path, payload)
        conv = list(GrokImporter().parse(path))[0]
        assert conv.conversation_id == "c1"
        assert conv.title == "Alt Fields"
        assert conv.created_at is not None
        assert [m.text for m in conv.messages] == ["hi", "hello"]

    def test_author_object_nesting(self, tmp_path: Path) -> None:
        """``author: {"role": "user"}`` should resolve to role='user'."""
        payload = [
            _conv(
                messages=[
                    {"author": {"role": "user", "name": "Alice"}, "content": "hi"},
                    {"author": {"role": "assistant"}, "content": "hello"},
                ]
            )
        ]
        path = _write(tmp_path, payload)
        conv = list(GrokImporter().parse(path))[0]
        assert [m.role for m in conv.messages] == ["user", "assistant"]


# ── Content flattening ─────────────────────────────────────────────


class TestContentFlattening:
    def test_plain_string_content(self, tmp_path: Path) -> None:
        path = _write(tmp_path, [_conv(messages=[_msg(role="user", content="hello world")])])
        conv = list(GrokImporter().parse(path))[0]
        assert conv.messages[0].text == "hello world"

    def test_typed_parts_text_only(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            [
                _conv(
                    messages=[
                        _msg(
                            role="user",
                            content=[
                                {"type": "text", "text": "part one"},
                                {"type": "text", "text": "part two"},
                            ],
                        )
                    ]
                )
            ],
        )
        conv = list(GrokImporter().parse(path))[0]
        assert conv.messages[0].text == "part one\npart two"

    def test_typed_parts_with_non_text(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            [
                _conv(
                    messages=[
                        _msg(
                            role="user",
                            content=[
                                {"type": "text", "text": "caption:"},
                                {"type": "image", "url": "https://x.com/img"},
                            ],
                        )
                    ]
                )
            ],
        )
        conv = list(GrokImporter().parse(path))[0]
        # Non-text parts surface as ``[type]`` markers.
        assert "[image]" in conv.messages[0].text
        assert "caption:" in conv.messages[0].text

    def test_empty_content_message_dropped(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            [
                _conv(
                    messages=[
                        _msg(role="user", content=""),
                        _msg(role="assistant", content="real answer"),
                    ]
                )
            ],
        )
        conv = list(GrokImporter().parse(path))[0]
        assert len(conv.messages) == 1
        assert conv.messages[0].text == "real answer"

    def test_legacy_text_field(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            [
                _conv(
                    messages=[
                        {"role": "user", "text": "legacy shape"},
                    ]
                )
            ],
        )
        conv = list(GrokImporter().parse(path))[0]
        assert conv.messages[0].text == "legacy shape"


# ── Timestamps ─────────────────────────────────────────────────────


class TestTimestamps:
    def test_iso_string_with_z(self, tmp_path: Path) -> None:
        path = _write(tmp_path, [_conv(created_at="2024-10-20T09:30:00Z")])
        conv = list(GrokImporter().parse(path))[0]
        assert conv.created_at == datetime(2024, 10, 20, 9, 30, tzinfo=UTC)

    def test_epoch_seconds(self, tmp_path: Path) -> None:
        path = _write(tmp_path, [_conv(created_at=1729416600)])
        conv = list(GrokImporter().parse(path))[0]
        assert conv.created_at is not None
        assert conv.created_at.tzinfo is UTC

    def test_naive_iso(self, tmp_path: Path) -> None:
        """Naive ISO strings attach UTC by default."""
        path = _write(tmp_path, [_conv(created_at="2024-10-20T09:30:00")])
        conv = list(GrokImporter().parse(path))[0]
        assert conv.created_at == datetime(2024, 10, 20, 9, 30, tzinfo=UTC)

    def test_invalid_timestamp_becomes_none(self, tmp_path: Path) -> None:
        path = _write(tmp_path, [_conv(created_at="not a date")])
        conv = list(GrokImporter().parse(path))[0]
        assert conv.created_at is None

    def test_missing_timestamp_becomes_none(self, tmp_path: Path) -> None:
        payload = [{"id": "c1", "messages": [_msg(), _msg(role="assistant")]}]
        path = _write(tmp_path, payload)
        conv = list(GrokImporter().parse(path))[0]
        assert conv.created_at is None


# ── Rejection paths ────────────────────────────────────────────────


class TestRejections:
    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(ConversationImportError, match="not found"):
            list(GrokImporter().parse(tmp_path / "missing.json"))

    def test_invalid_json(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.json"
        path.write_text("{this is not json", encoding="utf-8")
        with pytest.raises(ConversationImportError, match="Invalid Grok export"):
            list(GrokImporter().parse(path))

    def test_top_level_string_rejected(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "just a string")
        with pytest.raises(ConversationImportError, match="must be a JSON array"):
            list(GrokImporter().parse(path))

    def test_object_without_known_wrapper_rejected(self, tmp_path: Path) -> None:
        path = _write(tmp_path, {"foo": []})
        with pytest.raises(ConversationImportError, match="conversations"):
            list(GrokImporter().parse(path))

    def test_conversation_without_id_skipped(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            [{"title": "no id", "messages": [_msg(), _msg(role="assistant")]}],
        )
        convs = list(GrokImporter().parse(path))
        assert convs == []

    def test_conversation_without_messages_skipped(self, tmp_path: Path) -> None:
        path = _write(tmp_path, [{"id": "c1", "title": "empty"}])
        convs = list(GrokImporter().parse(path))
        assert convs == []

    def test_non_dict_entry_skipped(self, tmp_path: Path) -> None:
        path = _write(tmp_path, [_conv(), "garbage string", 42, _conv(id="c2")])
        convs = list(GrokImporter().parse(path))
        assert [c.conversation_id for c in convs] == ["conv-1", "c2"]


# ── Contract with the importer Protocol ────────────────────────────


class TestImporterContract:
    def test_platform_attribute(self) -> None:
        assert GrokImporter.platform == "grok"

    def test_first_user_text_helper(self, tmp_path: Path) -> None:
        """``RawConversation.first_user_text`` should return the first user turn."""
        path = _write(
            tmp_path,
            [
                _conv(
                    messages=[
                        _msg(role="user", content="first q"),
                        _msg(role="assistant", content="answer"),
                        _msg(role="user", content="follow-up"),
                    ]
                )
            ],
        )
        conv = list(GrokImporter().parse(path))[0]
        assert conv.first_user_text() == "first q"
        assert conv.last_assistant_text() == "answer"
