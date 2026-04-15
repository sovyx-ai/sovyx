"""Tests for ClaudeImporter — parsing of Claude's conversations.json."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sovyx.upgrade.conv_import import (
    ClaudeImporter,
    ConversationImportError,
)

# ── Fixture helpers ────────────────────────────────────────────────

_FIXTURE_ROOT = Path(__file__).parent.parent.parent.parent / "fixtures" / "claude"
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
        importer = ClaudeImporter()
        convs = list(importer.parse(_SAMPLE))
        assert len(convs) == 3  # noqa: PLR2004

    def test_platform_is_claude(self) -> None:
        importer = ClaudeImporter()
        convs = list(importer.parse(_SAMPLE))
        assert all(c.platform == "claude" for c in convs)

    def test_conversation_ids_preserved(self) -> None:
        importer = ClaudeImporter()
        ids = {c.conversation_id for c in importer.parse(_SAMPLE)}
        assert ids == {
            "claude-text-only-uuid",
            "claude-multimodal-uuid",
            "claude-legacy-text-only-uuid",
        }


# ── Role mapping ───────────────────────────────────────────────────


class TestRoleMapping:
    """Claude uses ``human``/``assistant``; Sovyx uses ``user``/``assistant``."""

    def test_human_mapped_to_user(self) -> None:
        importer = ClaudeImporter()
        by_id = {c.conversation_id: c for c in importer.parse(_SAMPLE)}
        linear = by_id["claude-text-only-uuid"]
        # Fixture alternates human/assistant/human; confirm the mapping.
        assert [m.role for m in linear.messages] == ["user", "assistant", "user"]

    def test_assistant_role_preserved(self) -> None:
        importer = ClaudeImporter()
        by_id = {c.conversation_id: c for c in importer.parse(_SAMPLE)}
        linear = by_id["claude-text-only-uuid"]
        assert any(m.role == "assistant" for m in linear.messages)

    def test_unknown_sender_falls_through_to_system(self, tmp_path: Path) -> None:
        """Defensive default — unknown sender doesn't drop the message."""
        payload = [
            {
                "uuid": "strange",
                "name": "",
                "chat_messages": [
                    {
                        "uuid": "m1",
                        "sender": "robot",
                        "text": "beep",
                        "created_at": "2024-01-01T00:00:00Z",
                    },
                ],
            },
        ]
        conv = next(ClaudeImporter().parse(_write_json(tmp_path, payload)))
        assert conv.messages[0].role == "system"


# ── Content extraction ─────────────────────────────────────────────


class TestContentExtraction:
    """Messages can carry text in ``content[]``, ``text``, or both."""

    def test_typed_content_extracted(self) -> None:
        """When ``content[{type:"text"}]`` is present, it is used."""
        importer = ClaudeImporter()
        by_id = {c.conversation_id: c for c in importer.parse(_SAMPLE)}
        linear = by_id["claude-text-only-uuid"]
        assert any("useEffect runs side effects" in m.text for m in linear.messages)

    def test_legacy_text_fallback(self) -> None:
        """No ``content[]`` → fall back to ``text`` field."""
        importer = ClaudeImporter()
        by_id = {c.conversation_id: c for c in importer.parse(_SAMPLE)}
        legacy = by_id["claude-legacy-text-only-uuid"]
        assert legacy.messages[0].text.startswith("Explain SQLite WAL")

    def test_multimodal_non_text_marked(self) -> None:
        """Non-text typed parts produce ``[type]`` markers."""
        importer = ClaudeImporter()
        by_id = {c.conversation_id: c for c in importer.parse(_SAMPLE)}
        multimodal = by_id["claude-multimodal-uuid"]
        first_user = next(m for m in multimodal.messages if m.role == "user")
        assert "What's in this screenshot?" in first_user.text
        assert "[image]" in first_user.text

    def test_both_text_and_content_prefers_content(self, tmp_path: Path) -> None:
        """Transition-era exports with both fields → content[] wins."""
        payload = [
            {
                "uuid": "both",
                "name": "",
                "chat_messages": [
                    {
                        "sender": "human",
                        "text": "legacy body",
                        "content": [{"type": "text", "text": "typed body"}],
                        "created_at": "2024-01-01T00:00:00Z",
                    },
                ],
            },
        ]
        conv = next(ClaudeImporter().parse(_write_json(tmp_path, payload)))
        assert conv.messages[0].text == "typed body"

    def test_empty_message_skipped(self, tmp_path: Path) -> None:
        """Messages with neither text nor content are dropped."""
        payload = [
            {
                "uuid": "mixed",
                "name": "",
                "chat_messages": [
                    {"sender": "human", "text": ""},
                    {"sender": "assistant", "text": "real"},
                ],
            },
        ]
        conv = next(ClaudeImporter().parse(_write_json(tmp_path, payload)))
        assert [m.text for m in conv.messages] == ["real"]


# ── Timestamps ─────────────────────────────────────────────────────


class TestTimestamps:
    """ISO 8601 parsing — microseconds + Z suffix both supported."""

    def test_conversation_created_at_parsed(self) -> None:
        importer = ClaudeImporter()
        by_id = {c.conversation_id: c for c in importer.parse(_SAMPLE)}
        linear = by_id["claude-text-only-uuid"]
        assert linear.created_at is not None
        assert linear.created_at.year == 2024  # noqa: PLR2004

    def test_message_timestamps_parsed(self) -> None:
        importer = ClaudeImporter()
        by_id = {c.conversation_id: c for c in importer.parse(_SAMPLE)}
        linear = by_id["claude-text-only-uuid"]
        assert all(m.created_at is not None for m in linear.messages)

    def test_missing_timestamps_survive(self, tmp_path: Path) -> None:
        payload = [
            {
                "uuid": "no-ts",
                "name": "",
                "chat_messages": [
                    {"sender": "human", "text": "hi"},
                ],
            },
        ]
        conv = next(ClaudeImporter().parse(_write_json(tmp_path, payload)))
        assert conv.created_at is None
        assert conv.messages[0].created_at is None

    def test_malformed_timestamp_tolerated(self, tmp_path: Path) -> None:
        """Bad ISO string → ``None``, not a crash."""
        payload = [
            {
                "uuid": "bad-ts",
                "name": "",
                "created_at": "not-a-date",
                "chat_messages": [
                    {
                        "sender": "human",
                        "text": "hi",
                        "created_at": "also-bad",
                    },
                ],
            },
        ]
        conv = next(ClaudeImporter().parse(_write_json(tmp_path, payload)))
        assert conv.created_at is None
        assert conv.messages[0].created_at is None


# ── Malformed inputs ───────────────────────────────────────────────


class TestMalformedHandling:
    """File-level breakage raises, entry-level breakage is skipped silently."""

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ConversationImportError, match="not found"):
            list(ClaudeImporter().parse(tmp_path / "missing.json"))

    def test_invalid_json_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.json"
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(ConversationImportError, match="Invalid"):
            list(ClaudeImporter().parse(path))

    def test_top_level_not_array_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ConversationImportError, match="array"):
            list(ClaudeImporter().parse(_write_json(tmp_path, {"key": "value"})))

    def test_entry_without_uuid_skipped(self, tmp_path: Path) -> None:
        payload = [
            {
                "name": "no uuid",
                "chat_messages": [{"sender": "human", "text": "hi"}],
            },
        ]
        convs = list(ClaudeImporter().parse(_write_json(tmp_path, payload)))
        assert convs == []

    def test_empty_chat_messages_skipped(self, tmp_path: Path) -> None:
        payload = [{"uuid": "x", "name": "t", "chat_messages": []}]
        convs = list(ClaudeImporter().parse(_write_json(tmp_path, payload)))
        assert convs == []

    def test_missing_chat_messages_skipped(self, tmp_path: Path) -> None:
        payload = [{"uuid": "x", "name": "t"}]
        convs = list(ClaudeImporter().parse(_write_json(tmp_path, payload)))
        assert convs == []

    def test_non_object_entry_skipped(self, tmp_path: Path) -> None:
        payload = [
            "not an object",
            {"uuid": "ok", "name": "", "chat_messages": [{"sender": "human", "text": "hi"}]},
        ]
        convs = list(ClaudeImporter().parse(_write_json(tmp_path, payload)))
        # Only the valid second entry yields.
        assert len(convs) == 1
        assert convs[0].conversation_id == "ok"

    def test_all_messages_empty_skipped(self, tmp_path: Path) -> None:
        payload = [
            {
                "uuid": "all-empty",
                "name": "",
                "chat_messages": [
                    {"sender": "human", "text": ""},
                    {"sender": "assistant", "text": ""},
                ],
            },
        ]
        convs = list(ClaudeImporter().parse(_write_json(tmp_path, payload)))
        assert convs == []


# ── Base-class helpers (turn_count etc.) ──────────────────────────


class TestTurnCount:
    def test_counts_user_and_assistant(self) -> None:
        importer = ClaudeImporter()
        by_id = {c.conversation_id: c for c in importer.parse(_SAMPLE)}
        linear = by_id["claude-text-only-uuid"]
        # All three messages are user/assistant, none are system.
        assert linear.turn_count() == len(linear.messages) == 3  # noqa: PLR2004


# ── Generator behaviour ────────────────────────────────────────────


def test_parse_is_iterator() -> None:
    """``parse()`` yields lazily."""
    importer = ClaudeImporter()
    result = importer.parse(_SAMPLE)
    from collections.abc import Iterator as IterABC

    assert isinstance(result, IterABC)
