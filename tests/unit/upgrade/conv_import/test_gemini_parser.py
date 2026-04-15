"""Tests for GeminiImporter — Google Takeout activity reconstruction.

Gemini's format doesn't ship conversations as first-class entities;
the parser reconstructs them from a flat activity stream using
role-prefix detection + time-gap session boundaries. These tests
exercise both the classification layer (prefix catalog, HTML
handling, meta-event filtering) and the grouping layer (stable
conversation IDs, session-gap heuristics).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sovyx.upgrade.conv_import import (
    ConversationImportError,
    GeminiImporter,
)

_FIXTURE_ROOT = Path(__file__).parent.parent.parent.parent / "fixtures" / "gemini"
_SAMPLE = _FIXTURE_ROOT / "sample_activity.json"


def _write_json(tmp_path: Path, payload: object) -> Path:
    """Dump ``payload`` as JSON to a temp file and return the path."""
    p = tmp_path / "MyActivity.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


# ── Sample fixture ────────────────────────────────────────────────


class TestSampleFixture:
    """The checked-in fixture exercises every major code path."""

    def test_parses_four_conversations(self) -> None:
        """Fixture has 10 activity entries → 4 inferred conversations.

        The two "You used Gemini" / "You searched for: ..." entries
        are meta-events with no role prefix; they're dropped during
        classification. The remaining 8 turns group into 4 sessions
        separated by hours-to-months of gap.
        """
        convs = list(GeminiImporter().parse(_SAMPLE))
        assert len(convs) == 4  # noqa: PLR2004

    def test_platform_is_gemini(self) -> None:
        convs = list(GeminiImporter().parse(_SAMPLE))
        assert all(c.platform == "gemini" for c in convs)

    def test_each_conversation_has_two_turns(self) -> None:
        """Fixture uses 1 user + 1 assistant per session."""
        for c in GeminiImporter().parse(_SAMPLE):
            assert len(c.messages) == 2  # noqa: PLR2004

    def test_conversations_ordered_chronologically(self) -> None:
        """Output order follows each session's first-turn timestamp.

        Takeout ships reverse-chronological; the parser must sort
        ascending before grouping so the earliest conversation
        (2023-06-01 Bard) appears first.
        """
        convs = list(GeminiImporter().parse(_SAMPLE))
        assert convs[0].created_at is not None
        assert convs[-1].created_at is not None
        assert convs[0].created_at.year == 2023  # noqa: PLR2004
        assert convs[-1].created_at.year == 2024  # noqa: PLR2004


# ── Role-prefix detection ─────────────────────────────────────────


class TestRoleDetection:
    """Locale-dependent prefix catalog + role mapping."""

    def test_english_you_said_is_user(self, tmp_path: Path) -> None:
        payload = [
            {
                "header": "Gemini Apps",
                "title": "You said: hello",
                "time": "2024-01-01T00:00:00Z",
                "products": ["Gemini Apps"],
            },
        ]
        conv = next(GeminiImporter().parse(_write_json(tmp_path, payload)))
        assert conv.messages[0].role == "user"
        assert conv.messages[0].text == "hello"

    def test_english_gemini_said_is_assistant(self, tmp_path: Path) -> None:
        payload = [
            {
                "header": "Gemini Apps",
                "title": "Gemini said: hi there",
                "time": "2024-01-01T00:00:00Z",
                "products": ["Gemini Apps"],
            },
        ]
        conv = next(GeminiImporter().parse(_write_json(tmp_path, payload)))
        assert conv.messages[0].role == "assistant"
        assert conv.messages[0].text == "hi there"

    def test_legacy_bard_said_is_assistant(self) -> None:
        """Older exports use the pre-rename 'Bard said:' prefix."""
        # Fixture's 2023-06-01 Bard conversation.
        convs = list(GeminiImporter().parse(_SAMPLE))
        bard = next(c for c in convs if c.created_at and c.created_at.year == 2023)  # noqa: PLR2004
        assistant = next(m for m in bard.messages if m.role == "assistant")
        # Prefix stripped, HTML entity decoded.
        assert "can't index a tuple" in assistant.text

    def test_portuguese_voce_disse_is_user(self) -> None:
        """The fixture's PT session uses 'Você disse:' / 'O Gemini respondeu:'."""
        convs = list(GeminiImporter().parse(_SAMPLE))
        pt = next(c for c in convs if c.messages[0].text.startswith("Como está o clima"))
        assert pt.messages[0].role == "user"

    def test_portuguese_gemini_respondeu_is_assistant(self) -> None:
        convs = list(GeminiImporter().parse(_SAMPLE))
        pt = next(c for c in convs if any("Curitiba" in m.text for m in c.messages))
        assistant = next(m for m in pt.messages if m.role == "assistant")
        assert "Curitiba" in assistant.text

    def test_unknown_prefix_entry_skipped(self, tmp_path: Path) -> None:
        """Activity entries that don't match any prefix are dropped.

        This is how meta-events like 'You used Gemini' naturally get
        filtered out — no prefix match, no turn emitted.
        """
        payload = [
            {
                "header": "Gemini Apps",
                "title": "You used Gemini",
                "time": "2024-01-01T00:00:00Z",
                "products": ["Gemini Apps"],
            },
        ]
        convs = list(GeminiImporter().parse(_write_json(tmp_path, payload)))
        assert convs == []

    def test_prefix_match_is_case_insensitive(self, tmp_path: Path) -> None:
        """Prefixes match irrespective of the title's casing."""
        payload = [
            {
                "header": "Gemini Apps",
                "title": "YOU SAID: shouting",
                "time": "2024-01-01T00:00:00Z",
                "products": ["Gemini Apps"],
            },
        ]
        conv = next(GeminiImporter().parse(_write_json(tmp_path, payload)))
        assert conv.messages[0].role == "user"
        assert conv.messages[0].text == "shouting"


# ── Session grouping ──────────────────────────────────────────────


class TestSessionGrouping:
    """Turns separated by more than _SESSION_GAP split into sessions."""

    def test_turns_within_gap_same_session(self, tmp_path: Path) -> None:
        """Two turns 5 minutes apart → one conversation."""
        payload = [
            {
                "header": "Gemini Apps",
                "title": "You said: first",
                "time": "2024-01-01T10:00:00Z",
                "products": ["Gemini Apps"],
            },
            {
                "header": "Gemini Apps",
                "title": "Gemini said: reply",
                "time": "2024-01-01T10:05:00Z",
                "products": ["Gemini Apps"],
            },
        ]
        convs = list(GeminiImporter().parse(_write_json(tmp_path, payload)))
        assert len(convs) == 1
        assert len(convs[0].messages) == 2  # noqa: PLR2004

    def test_turns_beyond_gap_new_session(self, tmp_path: Path) -> None:
        """Two turns >30 min apart → two separate conversations."""
        payload = [
            {
                "header": "Gemini Apps",
                "title": "You said: first",
                "time": "2024-01-01T10:00:00Z",
                "products": ["Gemini Apps"],
            },
            {
                "header": "Gemini Apps",
                "title": "You said: later",
                "time": "2024-01-01T11:30:00Z",
                "products": ["Gemini Apps"],
            },
        ]
        convs = list(GeminiImporter().parse(_write_json(tmp_path, payload)))
        assert len(convs) == 2  # noqa: PLR2004

    def test_reverse_chronological_input_gets_sorted(self, tmp_path: Path) -> None:
        """Takeout ships newest-first; parser must sort ascending."""
        payload = [
            # Listed newest-first as Takeout does.
            {
                "header": "Gemini Apps",
                "title": "Gemini said: second reply",
                "time": "2024-01-01T10:05:30Z",
                "products": ["Gemini Apps"],
            },
            {
                "header": "Gemini Apps",
                "title": "You said: second question",
                "time": "2024-01-01T10:05:00Z",
                "products": ["Gemini Apps"],
            },
            {
                "header": "Gemini Apps",
                "title": "Gemini said: first reply",
                "time": "2024-01-01T10:00:30Z",
                "products": ["Gemini Apps"],
            },
            {
                "header": "Gemini Apps",
                "title": "You said: first question",
                "time": "2024-01-01T10:00:00Z",
                "products": ["Gemini Apps"],
            },
        ]
        conv = next(GeminiImporter().parse(_write_json(tmp_path, payload)))
        texts = [m.text for m in conv.messages]
        # Chronological order preserved after sort.
        assert texts == [
            "first question",
            "first reply",
            "second question",
            "second reply",
        ]

    def test_single_turn_session_produces_valid_conversation(
        self,
        tmp_path: Path,
    ) -> None:
        """Orphaned single turn still yields a RawConversation."""
        payload = [
            {
                "header": "Gemini Apps",
                "title": "You said: lonely",
                "time": "2024-01-01T10:00:00Z",
                "products": ["Gemini Apps"],
            },
        ]
        conv = next(GeminiImporter().parse(_write_json(tmp_path, payload)))
        assert len(conv.messages) == 1
        assert conv.title.startswith("lonely")

    def test_gap_exactly_at_threshold_same_session(self, tmp_path: Path) -> None:
        """Boundary test: exactly _SESSION_GAP apart → same session.

        Gap larger than threshold splits; equal-to threshold does not.
        """
        payload = [
            {
                "header": "Gemini Apps",
                "title": "You said: first",
                "time": "2024-01-01T10:00:00Z",
                "products": ["Gemini Apps"],
            },
            {
                "header": "Gemini Apps",
                "title": "Gemini said: exactly 30 min later",
                "time": "2024-01-01T10:30:00Z",
                "products": ["Gemini Apps"],
            },
        ]
        convs = list(GeminiImporter().parse(_write_json(tmp_path, payload)))
        assert len(convs) == 1


# ── Meta-activity filtering ───────────────────────────────────────


class TestMetaActivity:
    """Non-conversation entries are filtered during classification."""

    def test_you_used_gemini_skipped(self) -> None:
        """The fixture's 'You used Gemini' entry doesn't create a turn."""
        convs = list(GeminiImporter().parse(_SAMPLE))
        all_texts = [m.text for c in convs for m in c.messages]
        assert not any("used Gemini" in t for t in all_texts)

    def test_search_query_skipped(self) -> None:
        """The fixture's 'You searched for:' entry is filtered."""
        convs = list(GeminiImporter().parse(_SAMPLE))
        all_texts = [m.text for c in convs for m in c.messages]
        assert not any("chocolate cake" in t for t in all_texts)


# ── HTML handling ─────────────────────────────────────────────────


class TestHtmlHandling:
    """HTML entities decoded, tags stripped before prefix matching."""

    def test_html_entities_decoded(self) -> None:
        """Fixture's PT session has ``&aacute;`` — becomes 'á'."""
        convs = list(GeminiImporter().parse(_SAMPLE))
        pt = next(c for c in convs if any("Curitiba" in m.text for m in c.messages))
        assistant = next(m for m in pt.messages if m.role == "assistant")
        assert "está" in assistant.text

    def test_numeric_html_entity_decoded(self) -> None:
        """Fixture's legacy Bard session has ``&#39;`` → apostrophe."""
        convs = list(GeminiImporter().parse(_SAMPLE))
        bard = next(c for c in convs if c.created_at and c.created_at.year == 2023)  # noqa: PLR2004
        assistant = next(m for m in bard.messages if m.role == "assistant")
        assert "can't" in assistant.text

    def test_html_tags_stripped(self) -> None:
        """Fixture's React session has ``<b>useEffect</b>`` in the title."""
        convs = list(GeminiImporter().parse(_SAMPLE))
        react = next(c for c in convs if any("useEffect" in m.text for m in c.messages))
        user = next(m for m in react.messages if m.role == "user")
        assert "<b>" not in user.text
        assert "useEffect" in user.text


# ── Timestamps ────────────────────────────────────────────────────


class TestTimestamps:
    """ISO 8601 parsing with Z suffix + timezone handling."""

    def test_z_suffix_parsed(self, tmp_path: Path) -> None:
        payload = [
            {
                "header": "Gemini Apps",
                "title": "You said: hi",
                "time": "2024-01-01T10:00:00.000Z",
                "products": ["Gemini Apps"],
            },
        ]
        conv = next(GeminiImporter().parse(_write_json(tmp_path, payload)))
        assert conv.created_at is not None
        assert conv.messages[0].created_at is not None

    def test_missing_time_skips_entry(self, tmp_path: Path) -> None:
        """Entries without ``time`` are dropped during classification."""
        payload = [
            {
                "header": "Gemini Apps",
                "title": "You said: hi",
                "products": ["Gemini Apps"],
            },
        ]
        convs = list(GeminiImporter().parse(_write_json(tmp_path, payload)))
        assert convs == []

    def test_malformed_time_skips_entry(self, tmp_path: Path) -> None:
        payload = [
            {
                "header": "Gemini Apps",
                "title": "You said: hi",
                "time": "not-a-date",
                "products": ["Gemini Apps"],
            },
        ]
        convs = list(GeminiImporter().parse(_write_json(tmp_path, payload)))
        assert convs == []


# ── Conversation_id stability ─────────────────────────────────────


class TestConversationIdStability:
    """Synthesised IDs must be deterministic for dedup to work."""

    def test_same_input_same_ids(self, tmp_path: Path) -> None:
        """Parsing the same file twice produces identical IDs."""
        ids_first = [c.conversation_id for c in GeminiImporter().parse(_SAMPLE)]
        ids_second = [c.conversation_id for c in GeminiImporter().parse(_SAMPLE)]
        assert ids_first == ids_second

    def test_earlier_ids_stable_when_new_conversation_appended(
        self,
        tmp_path: Path,
    ) -> None:
        """Adding a new conversation later shouldn't shift earlier IDs.

        This is what makes re-importing Takeout (with new activity
        accumulated since the last export) idempotent for the old
        conversations.
        """
        base = [
            {
                "header": "Gemini Apps",
                "title": "You said: first",
                "time": "2024-01-01T10:00:00Z",
                "products": ["Gemini Apps"],
            },
            {
                "header": "Gemini Apps",
                "title": "Gemini said: first reply",
                "time": "2024-01-01T10:00:30Z",
                "products": ["Gemini Apps"],
            },
        ]
        extended = [
            *base,
            {
                "header": "Gemini Apps",
                "title": "You said: later",
                "time": "2024-06-01T10:00:00Z",
                "products": ["Gemini Apps"],
            },
        ]
        first_ids = [
            c.conversation_id for c in GeminiImporter().parse(_write_json(tmp_path, base))
        ]
        # Use a different tmp path to avoid filename collision.
        extended_path = tmp_path / "extended.json"
        extended_path.write_text(json.dumps(extended), encoding="utf-8")
        extended_ids = [c.conversation_id for c in GeminiImporter().parse(extended_path)]
        # The earlier conversation ID is preserved; a new one is appended.
        assert first_ids[0] == extended_ids[0]
        assert len(extended_ids) == len(first_ids) + 1


# ── Malformed inputs ──────────────────────────────────────────────


class TestMalformedHandling:
    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ConversationImportError, match="not found"):
            list(GeminiImporter().parse(tmp_path / "missing.json"))

    def test_invalid_json_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.json"
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(ConversationImportError, match="Invalid"):
            list(GeminiImporter().parse(path))

    def test_top_level_not_array_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ConversationImportError, match="array"):
            list(GeminiImporter().parse(_write_json(tmp_path, {"key": "v"})))

    def test_empty_array_yields_nothing(self, tmp_path: Path) -> None:
        convs = list(GeminiImporter().parse(_write_json(tmp_path, [])))
        assert convs == []

    def test_non_gemini_headers_ignored(self, tmp_path: Path) -> None:
        """Entries from other Google products pass through Takeout too."""
        payload = [
            {
                "header": "YouTube",
                "title": "You said: watched a video",
                "time": "2024-01-01T10:00:00Z",
                "products": ["YouTube"],
            },
        ]
        convs = list(GeminiImporter().parse(_write_json(tmp_path, payload)))
        assert convs == []


# ── Unsupported locale path ───────────────────────────────────────


class TestUnsupportedLocale:
    """When no prefix matches, the whole import yields nothing.

    Documents the miss-path so that if a user imports a Takeout
    archive in an unsupported locale, the progress endpoint can
    surface a useful warning rather than silently claim success.
    """

    def test_all_unsupported_locale_yields_empty(self, tmp_path: Path) -> None:
        # Hypothetical Dutch — not in the seeded catalog.
        payload = [
            {
                "header": "Gemini Apps",
                "title": "Jij zei: hallo",
                "time": "2024-01-01T10:00:00Z",
                "products": ["Gemini Apps"],
            },
            {
                "header": "Gemini Apps",
                "title": "Gemini antwoordde: hoi",
                "time": "2024-01-01T10:00:30Z",
                "products": ["Gemini Apps"],
            },
        ]
        convs = list(GeminiImporter().parse(_write_json(tmp_path, payload)))
        assert convs == []


# ── Title synthesis ───────────────────────────────────────────────


class TestTitleSynthesis:
    """The synthesised title derives from the first user turn."""

    def test_title_from_first_user_turn(self, tmp_path: Path) -> None:
        payload = [
            {
                "header": "Gemini Apps",
                "title": "You said: A question about chess openings",
                "time": "2024-01-01T10:00:00Z",
                "products": ["Gemini Apps"],
            },
        ]
        conv = next(GeminiImporter().parse(_write_json(tmp_path, payload)))
        assert conv.title == "A question about chess openings"

    def test_title_truncated_when_long(self, tmp_path: Path) -> None:
        long_text = "x" * 200
        payload = [
            {
                "header": "Gemini Apps",
                "title": f"You said: {long_text}",
                "time": "2024-01-01T10:00:00Z",
                "products": ["Gemini Apps"],
            },
        ]
        conv = next(GeminiImporter().parse(_write_json(tmp_path, payload)))
        assert len(conv.title) <= 60  # noqa: PLR2004
        assert conv.title.endswith("…")

    def test_title_fallback_when_no_user_turn(self, tmp_path: Path) -> None:
        """Orphaned assistant-only session gets a date-based title."""
        payload = [
            {
                "header": "Gemini Apps",
                "title": "Gemini said: unsolicited wisdom",
                "time": "2024-05-17T10:00:00Z",
                "products": ["Gemini Apps"],
            },
        ]
        conv = next(GeminiImporter().parse(_write_json(tmp_path, payload)))
        assert conv.title == "Gemini conversation 2024-05-17"


# ── Generator behaviour ───────────────────────────────────────────


def test_parse_is_iterator() -> None:
    """``parse()`` yields lazily."""
    from collections.abc import Iterator as IterABC

    assert isinstance(GeminiImporter().parse(_SAMPLE), IterABC)
