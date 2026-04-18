"""Tests for :mod:`sovyx.voice.phrases`.

The phrase catalog is the other half of the voice-coherence fix — the
wizard plays a localised sentence so the user can *hear* that the right
language is actually active. These tests guard two invariants:

1. **The default phrase key covers every supported language.**
   Otherwise the wizard would fall into the "language not available"
   branch for a legitimate pick.
2. **``PHRASES`` is read-only.** A runtime caller mutating the catalog
   would silently drift the displayed text from translators' QA copy.
"""

from __future__ import annotations

from types import MappingProxyType

import pytest

from sovyx.voice import phrases
from sovyx.voice.phrases import (
    DEFAULT_PHRASE_KEY,
    PHRASES,
    is_complete,
    resolve_phrase,
)
from sovyx.voice.voice_catalog import SUPPORTED_LANGUAGES


class TestDefaultPhraseCompleteness:
    """Every supported language must have the default phrase translated."""

    def test_default_key_exists(self) -> None:
        assert DEFAULT_PHRASE_KEY in PHRASES

    def test_default_covers_every_supported_language(self) -> None:
        entry = PHRASES[DEFAULT_PHRASE_KEY]
        missing = SUPPORTED_LANGUAGES - set(entry.keys())
        assert not missing, f"Default phrase missing translations for: {sorted(missing)}"

    def test_default_phrases_are_non_empty_strings(self) -> None:
        entry = PHRASES[DEFAULT_PHRASE_KEY]
        for language, text in entry.items():
            assert isinstance(text, str), f"{language}: not a string"
            assert text.strip(), f"{language}: empty / whitespace phrase"


class TestResolvePhrase:
    @pytest.mark.parametrize("language", sorted(SUPPORTED_LANGUAGES))
    def test_default_key_resolves_for_every_supported_language(
        self,
        language: str,
    ) -> None:
        text = resolve_phrase(DEFAULT_PHRASE_KEY, language)
        assert text is not None
        assert text.strip()

    def test_unknown_key_returns_none(self) -> None:
        assert resolve_phrase("not_a_key", "en-us") is None

    def test_unknown_language_returns_none(self) -> None:
        # ``resolve_phrase`` takes an already-normalised language tag; a
        # bogus tag must propagate as None so the caller can decide
        # whether to reject or fall back, rather than silently landing
        # on an English phrase that contradicts the user's pick.
        assert resolve_phrase(DEFAULT_PHRASE_KEY, "klingon") is None

    def test_resolved_text_differs_across_languages(self) -> None:
        # Catch "everyone copy-pasted the English sentence" regressions.
        en = resolve_phrase(DEFAULT_PHRASE_KEY, "en-us")
        pt = resolve_phrase(DEFAULT_PHRASE_KEY, "pt-br")
        ja = resolve_phrase(DEFAULT_PHRASE_KEY, "ja")
        assert en is not None and pt is not None and ja is not None
        assert en != pt
        assert en != ja
        assert pt != ja


class TestIsComplete:
    def test_default_key_is_complete(self) -> None:
        assert is_complete(DEFAULT_PHRASE_KEY) is True

    def test_unknown_key_is_not_complete(self) -> None:
        assert is_complete("not_a_key") is False


class TestImmutability:
    """PHRASES must be a read-only view. Mutation has to raise."""

    def test_outer_map_is_read_only(self) -> None:
        assert isinstance(PHRASES, MappingProxyType)
        with pytest.raises(TypeError):
            PHRASES["hax"] = {"en-us": "nope"}  # type: ignore[index]

    def test_inner_map_is_read_only(self) -> None:
        inner = PHRASES[DEFAULT_PHRASE_KEY]
        assert isinstance(inner, MappingProxyType)
        with pytest.raises(TypeError):
            inner["en-us"] = "hijacked"  # type: ignore[index]


class TestModuleExports:
    def test_all_contains_expected_symbols(self) -> None:
        assert set(phrases.__all__) == {
            "DEFAULT_PHRASE_KEY",
            "PHRASES",
            "resolve_phrase",
        }
