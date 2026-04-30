"""Tests for the shared sentence-splitting primitives.

Pins the public ``split_sentences`` contract so future regex /
abbreviation-list updates can land via this single source of
truth. The legacy per-engine ``_split_sentences`` tests in
``test_tts_kokoro.py`` and ``test_tts_piper.py`` remain in place
and exercise the re-export path.
"""

from __future__ import annotations

import pytest

from sovyx.voice._tts_sentence_split import split_sentences


class TestLegacyContract:
    """Pre-T1.38 behaviour — every existing engine test must still pass."""

    def test_single_sentence(self) -> None:
        assert split_sentences("Hello world") == ["Hello world"]

    def test_two_sentences(self) -> None:
        assert split_sentences("Hello. World.") == ["Hello.", "World."]

    def test_question_and_exclamation(self) -> None:
        assert split_sentences("How are you? Great! Thanks.") == [
            "How are you?",
            "Great!",
            "Thanks.",
        ]

    def test_no_space_after_period(self) -> None:
        # "v1.0 is great" has no whitespace after the inner ``.``
        assert split_sentences("v1.0 is great") == ["v1.0 is great"]

    def test_period_glued_to_next_word(self) -> None:
        # Kokoro test pattern — "Hello.World" with no space.
        assert split_sentences("Hello.World") == ["Hello.World"]

    def test_empty_string(self) -> None:
        assert split_sentences("") == [""]

    def test_multiple_spaces(self) -> None:
        assert split_sentences("Hello.   World.") == ["Hello.", "World."]


class TestAbbreviationMergeT138:
    """T1.38 enhancement — known abbreviations don't fragment sentences."""

    def test_dr_keeps_following_sentence_intact(self) -> None:
        assert split_sentences("Dr. Smith said hello.") == ["Dr. Smith said hello."]

    def test_mr_mrs_ms_titles_preserved(self) -> None:
        assert split_sentences("Mr. Brown met Mrs. White and Ms. Green.") == [
            "Mr. Brown met Mrs. White and Ms. Green."
        ]

    def test_usa_with_internal_periods_preserved(self) -> None:
        assert split_sentences("U.S.A. is a country.") == ["U.S.A. is a country."]

    def test_eg_and_ie_preserved(self) -> None:
        assert split_sentences("Use a TTS, e.g. Piper or Kokoro.") == [
            "Use a TTS, e.g. Piper or Kokoro."
        ]

    def test_phd_with_internal_period_preserved(self) -> None:
        assert split_sentences("She has a Ph.D. in linguistics.") == [
            "She has a Ph.D. in linguistics."
        ]

    def test_abbreviation_does_not_swallow_next_sentence(self) -> None:
        """A genuine sentence boundary AFTER the abbreviated sentence
        must still split correctly. ``Dr. Smith said hello. How are
        you?`` is two sentences — the period after ``hello`` is a
        sentence terminator (``hello`` is not in the abbreviation set),
        and the period in ``Dr.`` is the abbreviation that must merge.
        """
        assert split_sentences("Dr. Smith said hello. How are you?") == [
            "Dr. Smith said hello.",
            "How are you?",
        ]

    def test_multiple_abbreviations_in_one_sentence(self) -> None:
        assert split_sentences(
            "Dr. Smith and Mr. Brown went to the U.S.A. yesterday. They had fun."
        ) == [
            "Dr. Smith and Mr. Brown went to the U.S.A. yesterday.",
            "They had fun.",
        ]

    def test_abbreviation_at_end_of_input_with_no_followup(self) -> None:
        """Trailing abbreviation with no further text. The merge buffer
        never resolves; flush as-is so the final output isn't lost.
        """
        assert split_sentences("Dr.") == ["Dr."]

    def test_question_after_abbreviation_splits_correctly(self) -> None:
        # ``?`` is unambiguous — never an abbreviation terminator.
        assert split_sentences("Did Dr. Smith leave? Yes.") == [
            "Did Dr. Smith leave?",
            "Yes.",
        ]

    def test_lowercase_abbreviation_matched(self) -> None:
        # ``etc.`` mid-sentence — the merge keeps the rest attached.
        assert split_sentences("Cats, dogs, etc. are pets.") == ["Cats, dogs, etc. are pets."]


class TestEdgeCases:
    def test_only_whitespace(self) -> None:
        assert split_sentences("   ") == ["   "]

    def test_only_punctuation(self) -> None:
        # No content before the terminator — single-token edge case.
        result = split_sentences(". ?")
        assert result == [". ?"] or result == [".", "?"]  # contract is lenient

    def test_concat_round_trip_preserves_content(self) -> None:
        """Splitting + naive rejoin keeps every non-whitespace character
        — the legacy per-engine property test pattern, generalised.
        """
        text = "Mr. Brown saw Dr. Smith at 3 p.m. They talked."
        parts = split_sentences(text)
        joined = " ".join(parts)
        # All non-whitespace chars from the original must survive.
        assert set(text.replace(" ", "")) <= set(joined.replace(" ", ""))


class TestUnicodeWhitespaceContract:
    """Pin the post-fix contract: the splitter consumes ONLY ASCII
    whitespace (``[ \\t\\n\\r]+``) at sentence boundaries. Unicode
    separators are CONTENT and must round-trip unchanged.

    Pre-fix bug (Hypothesis-found): ``_GREEDY_SPLIT_RE = r"(?<=[.!?])\\s+"``
    treated NBSP / em-space / zero-width-space as boundaries and
    silently consumed the character. Real-world impact: Portuguese
    typographic conventions (``Sr.\\xa0Silva``, ``10\\xa0000``) lost
    the NBSP at TTS time, fragmenting prosody. This test class pins
    the canonical behaviour so a future regex regression is caught.
    """

    def test_nbsp_preserved_as_content(self) -> None:
        """``\\xa0`` (NO-BREAK SPACE) is intentional author markup —
        ``Sr.\\xa0Silva`` keeps the title bound to the surname. The
        splitter must NOT split here; the NBSP stays in the chunk.
        """
        result = split_sentences("Sr.\xa0Silva said hello.")
        assert result == ["Sr.\xa0Silva said hello."]

    def test_lone_nbsp_after_terminator_preserved(self) -> None:
        """Hypothesis falsifying example: ``".\\xa0"`` round-trips
        with the NBSP intact rather than being consumed.
        """
        result = split_sentences(".\xa0")
        assert result == [".\xa0"]

    def test_em_space_preserved_as_content(self) -> None:
        """``\\u2003`` (EM SPACE) is typographic, never a sentence
        boundary. Must round-trip as content."""
        result = split_sentences("Hello. More text")
        assert result == ["Hello. More text"]

    def test_en_space_preserved_as_content(self) -> None:
        """``\\u2002`` (EN SPACE) — typographic separator."""
        result = split_sentences("Hello. More text")
        assert result == ["Hello. More text"]

    def test_zero_width_space_preserved_as_content(self) -> None:
        """``\\u200b`` (ZERO WIDTH SPACE) — invisible, never a
        sentence boundary. CSS / Unicode use it for joining."""
        result = split_sentences("Hello.​More text")
        assert result == ["Hello.​More text"]

    def test_ideographic_space_preserved_as_content(self) -> None:
        """``\\u3000`` (IDEOGRAPHIC SPACE) — CJK convention, not a
        boundary in our ASCII-Latin sentence model."""
        result = split_sentences("Hello.　More text")
        assert result == ["Hello.　More text"]

    def test_newline_still_splits_normally(self) -> None:
        """Regression guard: ``\\n`` is ASCII whitespace and MUST
        still trigger a sentence boundary."""
        assert split_sentences("Hello.\nWorld.") == ["Hello.", "World."]

    def test_tab_still_splits_normally(self) -> None:
        """``\\t`` is ASCII whitespace and MUST still trigger."""
        assert split_sentences("Hello.\tWorld.") == ["Hello.", "World."]

    def test_crlf_still_splits_normally(self) -> None:
        """CRLF (``\\r\\n``) is ASCII whitespace and MUST still
        trigger; the regex matches the whole CRLF run as one
        boundary."""
        assert split_sentences("Hello.\r\nWorld.") == ["Hello.", "World."]

    def test_regular_space_still_splits(self) -> None:
        """Sanity: the canonical between-sentence space still works."""
        assert split_sentences("Hello. World.") == ["Hello.", "World."]

    def test_mixed_ascii_and_nbsp_partial_split(self) -> None:
        """Period + regular space + NBSP + word: split on the regular
        space, leave the NBSP attached to the second sentence as
        content.
        """
        result = split_sentences("Hello. \xa0World.")
        assert result == ["Hello.", "\xa0World."]

    def test_portuguese_nbsp_business_pattern(self) -> None:
        """Real-world PT pattern: ``"Sr. Silva foi à reunião."`` with
        NBSP preserves the title-name binding through TTS chunking.
        Combined with the abbreviation merge (``Sr.`` is in the
        abbreviation set), the entire sentence stays in one chunk.
        """
        text = "Sr.\xa0Silva foi à reunião com a Dra.\xa0Costa."
        result = split_sentences(text)
        # No mid-sentence split — the NBSP keeps title+name attached
        # AND the abbreviation merge keeps the period attached.
        assert result == [text]


@pytest.mark.parametrize(
    ("text", "expected_count"),
    [
        ("One sentence.", 1),
        ("One. Two. Three.", 3),
        ("Dr. Smith. Mr. Brown.", 2),  # 2 sentences after merge
        ("U.S.A. is fine. Done.", 2),
        ("Hello!", 1),
        ("?", 1),
        ("", 1),  # legacy contract — empty → [""]
    ],
)
def test_split_sentences_count(text: str, expected_count: int) -> None:
    """Sentence count under abbreviation-aware splitting."""
    assert len(split_sentences(text)) == expected_count
