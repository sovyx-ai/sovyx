"""Tests for ChatGPTImporter — parsing of conversations.json."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sovyx.upgrade.conv_import import (
    ChatGPTImporter,
    ConversationImportError,
)

# ── Fixture helpers ────────────────────────────────────────────────

_FIXTURE_ROOT = Path(__file__).parent.parent.parent.parent / "fixtures" / "chatgpt"
_SAMPLE = _FIXTURE_ROOT / "sample_conversations.json"


def _write_json(tmp_path: Path, payload: object) -> Path:
    """Dump ``payload`` as JSON to a temp file and return the path."""
    p = tmp_path / "conversations.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


# ── Sample fixture ─────────────────────────────────────────────────


class TestSampleFixture:
    """Sanity checks against the checked-in sample file."""

    def test_parses_three_conversations(self) -> None:
        importer = ChatGPTImporter()
        convs = list(importer.parse(_SAMPLE))
        assert len(convs) == 3  # noqa: PLR2004

    def test_platform_is_chatgpt(self) -> None:
        importer = ChatGPTImporter()
        convs = list(importer.parse(_SAMPLE))
        assert all(c.platform == "chatgpt" for c in convs)

    def test_conversation_ids_preserved(self) -> None:
        importer = ChatGPTImporter()
        ids = {c.conversation_id for c in importer.parse(_SAMPLE)}
        assert ids == {
            "simple-linear-uuid",
            "branching-uuid",
            "multimodal-uuid",
        }


# ── Branching / mainline extraction ────────────────────────────────


class TestMainlineWalk:
    """``current_node`` determines the mainline; forks stay abandoned."""

    def test_branching_conversation_keeps_only_winner(self) -> None:
        """The regenerated (losing) branch must NOT appear in the output."""
        importer = ChatGPTImporter()
        by_id = {c.conversation_id: c for c in importer.parse(_SAMPLE)}
        branching = by_id["branching-uuid"]

        texts = [m.text for m in branching.messages]
        # Mainline: user question + winning assistant answer.
        assert any("WAL mode uses a write-ahead log" in t for t in texts)
        # Loser branch text must be absent.
        assert not any("Regenerated answer" in t for t in texts)

    def test_linear_conversation_preserves_order(self) -> None:
        """Root → leaf ordering is chronological after tree walk."""
        importer = ChatGPTImporter()
        by_id = {c.conversation_id: c for c in importer.parse(_SAMPLE)}
        simple = by_id["simple-linear-uuid"]
        roles = [m.role for m in simple.messages]
        assert roles == ["user", "assistant", "user"]

    def test_missing_current_node_falls_back_to_deepest(self, tmp_path: Path) -> None:
        """When ``current_node`` is missing, deepest-child chain wins."""
        payload = [
            {
                "conversation_id": "no-current",
                "title": "t",
                "mapping": {
                    "root": {
                        "id": "root",
                        "parent": None,
                        "children": ["short", "long-a"],
                        "message": None,
                    },
                    "short": {
                        "id": "short",
                        "parent": "root",
                        "children": [],
                        "message": {
                            "author": {"role": "user"},
                            "content": {"content_type": "text", "parts": ["short branch"]},
                        },
                    },
                    "long-a": {
                        "id": "long-a",
                        "parent": "root",
                        "children": ["long-b"],
                        "message": {
                            "author": {"role": "user"},
                            "content": {"content_type": "text", "parts": ["long branch A"]},
                        },
                    },
                    "long-b": {
                        "id": "long-b",
                        "parent": "long-a",
                        "children": [],
                        "message": {
                            "author": {"role": "assistant"},
                            "content": {"content_type": "text", "parts": ["long branch B"]},
                        },
                    },
                },
            },
        ]
        path = _write_json(tmp_path, payload)
        convs = list(ChatGPTImporter().parse(path))
        assert len(convs) == 1
        texts = [m.text for m in convs[0].messages]
        assert "long branch A" in texts
        assert "long branch B" in texts
        assert "short branch" not in texts


# ── Content-type handling ──────────────────────────────────────────


class TestContentExtraction:
    """Non-text content is stringified with a marker rather than dropped."""

    def test_multimodal_stringified(self) -> None:
        """Image parts become ``[multimodal_text:image_asset_pointer]`` markers."""
        importer = ChatGPTImporter()
        by_id = {c.conversation_id: c for c in importer.parse(_SAMPLE)}
        multimodal = by_id["multimodal-uuid"]

        # First user turn had a question string + image pointer dict.
        user_turn = next(m for m in multimodal.messages if m.role == "user")
        assert "What's in this photo?" in user_turn.text
        assert "image_asset_pointer" in user_turn.text

    def test_empty_text_messages_skipped(self, tmp_path: Path) -> None:
        """Messages with empty text are dropped from the output."""
        payload = [
            {
                "conversation_id": "mixed-empty",
                "title": "t",
                "current_node": "b",
                "mapping": {
                    "root": {"id": "root", "parent": None, "children": ["a"], "message": None},
                    "a": {
                        "id": "a",
                        "parent": "root",
                        "children": ["b"],
                        "message": {
                            "author": {"role": "user"},
                            "content": {"content_type": "text", "parts": [""]},
                        },
                    },
                    "b": {
                        "id": "b",
                        "parent": "a",
                        "children": [],
                        "message": {
                            "author": {"role": "user"},
                            "content": {"content_type": "text", "parts": ["real"]},
                        },
                    },
                },
            },
        ]
        convs = list(ChatGPTImporter().parse(_write_json(tmp_path, payload)))
        assert len(convs) == 1
        assert [m.text for m in convs[0].messages] == ["real"]


# ── Malformed inputs ───────────────────────────────────────────────


class TestMalformedHandling:
    """Bad entries are skipped, file-level breakage raises."""

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ConversationImportError, match="not found"):
            list(ChatGPTImporter().parse(tmp_path / "missing.json"))

    def test_invalid_json_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.json"
        path.write_text("{not json at all", encoding="utf-8")
        with pytest.raises(ConversationImportError, match="Invalid"):
            list(ChatGPTImporter().parse(path))

    def test_top_level_not_array_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ConversationImportError, match="array"):
            list(ChatGPTImporter().parse(_write_json(tmp_path, {"key": "value"})))

    def test_entry_without_id_skipped(self, tmp_path: Path) -> None:
        payload = [{"title": "orphan", "mapping": {"root": {"parent": None}}}]
        convs = list(ChatGPTImporter().parse(_write_json(tmp_path, payload)))
        assert convs == []

    def test_entry_with_empty_mapping_skipped(self, tmp_path: Path) -> None:
        payload = [{"conversation_id": "x", "title": "t", "mapping": {}}]
        convs = list(ChatGPTImporter().parse(_write_json(tmp_path, payload)))
        assert convs == []

    def test_non_object_entry_skipped(self, tmp_path: Path) -> None:
        payload = ["not-an-object", {"conversation_id": "ok", "title": "t", "mapping": {}}]
        # Both skipped — the string is non-dict, the second has empty mapping.
        convs = list(ChatGPTImporter().parse(_write_json(tmp_path, payload)))
        assert convs == []


# ── Timestamp handling ─────────────────────────────────────────────


class TestTimestamps:
    def test_epoch_converted_to_datetime(self) -> None:
        importer = ChatGPTImporter()
        by_id = {c.conversation_id: c for c in importer.parse(_SAMPLE)}
        simple = by_id["simple-linear-uuid"]
        assert simple.created_at is not None
        # Timestamps are preserved on messages too.
        first_msg = simple.messages[0]
        assert first_msg.created_at is not None

    def test_missing_timestamps_survive(self, tmp_path: Path) -> None:
        payload = [
            {
                "conversation_id": "no-ts",
                "title": "t",
                "current_node": "m1",
                "mapping": {
                    "root": {"id": "root", "parent": None, "children": ["m1"], "message": None},
                    "m1": {
                        "id": "m1",
                        "parent": "root",
                        "children": [],
                        "message": {
                            "author": {"role": "user"},
                            "content": {"content_type": "text", "parts": ["hi"]},
                        },
                    },
                },
            },
        ]
        convs = list(ChatGPTImporter().parse(_write_json(tmp_path, payload)))
        assert convs[0].created_at is None
        assert convs[0].messages[0].created_at is None


# ── turn_count helper ──────────────────────────────────────────────


class TestTurnCount:
    def test_counts_only_user_and_assistant(self, tmp_path: Path) -> None:
        payload = [
            {
                "conversation_id": "with-system",
                "title": "t",
                "current_node": "c",
                "mapping": {
                    "root": {"id": "root", "parent": None, "children": ["a"], "message": None},
                    "a": {
                        "id": "a",
                        "parent": "root",
                        "children": ["b"],
                        "message": {
                            "author": {"role": "system"},
                            "content": {"content_type": "text", "parts": ["sys prompt"]},
                        },
                    },
                    "b": {
                        "id": "b",
                        "parent": "a",
                        "children": ["c"],
                        "message": {
                            "author": {"role": "user"},
                            "content": {"content_type": "text", "parts": ["q"]},
                        },
                    },
                    "c": {
                        "id": "c",
                        "parent": "b",
                        "children": [],
                        "message": {
                            "author": {"role": "assistant"},
                            "content": {"content_type": "text", "parts": ["a"]},
                        },
                    },
                },
            },
        ]
        conv = next(ChatGPTImporter().parse(_write_json(tmp_path, payload)))
        # 3 messages total (system + user + assistant), but turn_count
        # excludes system.
        assert len(conv.messages) == 3  # noqa: PLR2004
        assert conv.turn_count() == 2  # noqa: PLR2004


# ── Generator behaviour ────────────────────────────────────────────


def test_parse_is_iterator() -> None:
    """``parse()`` yields lazily — not all conversations at once."""
    importer = ChatGPTImporter()
    result = importer.parse(_SAMPLE)
    # Confirm it's an iterator, not a list.
    from collections.abc import Iterator as IterABC

    assert isinstance(result, IterABC)
