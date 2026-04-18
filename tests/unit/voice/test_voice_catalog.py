"""Tests for :mod:`sovyx.voice.voice_catalog`.

The catalog is the single source of truth used by the voice-test flow to
answer "given this language / voice id, what phrase and ONNX inputs
should I feed Kokoro?". Three invariants matter here and all are
regression-guarded below:

1. **Every supported language has a recommended voice.** A UI picking a
   language and pressing "Test" must never hit "no voice available".
2. **Every voice exposes a language covered by the catalog.** Otherwise
   phrase lookups silently fall back to English and reintroduce the bug
   this whole module exists to fix.
3. **Language normalisation is lossless across separators and case.**
   The wizard can send ``pt-BR``, ``pt_br``, or ``pt`` — all three must
   resolve to ``pt-br``.
"""

from __future__ import annotations

import pytest

from sovyx.voice import voice_catalog
from sovyx.voice.voice_catalog import (
    SUPPORTED_LANGUAGES,
    VoiceInfo,
    all_voices,
    language_for_voice,
    normalize_language,
    recommended_voice,
    supported_languages,
    voice_info,
    voices_for_language,
)


class TestSupportedLanguages:
    """The set of languages the catalog can serve."""

    def test_frozenset_is_non_empty(self) -> None:
        assert len(SUPPORTED_LANGUAGES) >= 9

    def test_supported_languages_is_sorted(self) -> None:
        result = supported_languages()
        assert result == sorted(result)

    def test_canonical_codes_match_expected_set(self) -> None:
        expected = {
            "en-us",
            "en-gb",
            "es",
            "fr",
            "hi",
            "it",
            "ja",
            "pt-br",
            "zh",
        }
        assert set(supported_languages()) == expected


class TestNormalizeLanguage:
    """UI-shaped tags must converge on Kokoro's canonical codes."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("en", "en-us"),
            ("EN", "en-us"),
            ("en_US", "en-us"),
            ("en-US", "en-us"),
            ("en-us", "en-us"),
            ("en-gb", "en-gb"),
            ("en_GB", "en-gb"),
            ("pt", "pt-br"),
            ("pt-BR", "pt-br"),
            ("pt_BR", "pt-br"),
            ("pt-br", "pt-br"),
            ("es-ES", "es"),
            ("es-mx", "es"),
            ("es_MX", "es"),
            ("fr", "fr"),
            ("fr-FR", "fr"),
            ("hi", "hi"),
            ("hi-IN", "hi"),
            ("it", "it"),
            ("it-IT", "it"),
            ("ja", "ja"),
            ("ja-JP", "ja"),
            ("zh", "zh"),
            ("zh-CN", "zh"),
            ("zh-TW", "zh"),
        ],
    )
    def test_known_aliases_canonicalise(self, raw: str, expected: str) -> None:
        assert normalize_language(raw) == expected

    def test_unknown_language_is_lowercased_but_returned(self) -> None:
        # Caller decides how to handle — we don't silently fall back to
        # English, otherwise the wizard's "voice in wrong language" bug
        # reappears for any unseen tag.
        assert normalize_language("KLINGON") == "klingon"

    def test_whitespace_is_stripped(self) -> None:
        assert normalize_language("  en-US  ") == "en-us"


class TestVoicesForLanguage:
    """``voices_for_language`` must return only voices matching the tag."""

    @pytest.mark.parametrize("language", sorted(SUPPORTED_LANGUAGES))
    def test_every_supported_language_has_at_least_one_voice(
        self,
        language: str,
    ) -> None:
        voices = voices_for_language(language)
        assert voices, f"{language!r} has no voices — catalog regression"
        assert all(v.language == language for v in voices)

    def test_unknown_language_returns_empty_list(self) -> None:
        assert voices_for_language("klingon") == []

    def test_alias_resolves_through_normalisation(self) -> None:
        # Hitting the alias path (pt → pt-br) must yield the same set as
        # hitting the canonical code directly; otherwise UI code that
        # forgets to normalise silently sees fewer voices.
        assert voices_for_language("pt") == voices_for_language("pt-br")


class TestRecommendedVoice:
    """Every language must expose a hand-picked default voice."""

    @pytest.mark.parametrize("language", sorted(SUPPORTED_LANGUAGES))
    def test_every_language_has_a_default(self, language: str) -> None:
        info = recommended_voice(language)
        assert info is not None, f"{language!r} missing recommended voice"
        assert info.language == language

    def test_unknown_language_returns_none(self) -> None:
        assert recommended_voice("klingon") is None

    def test_recommended_voice_is_present_in_catalog(self) -> None:
        for language in SUPPORTED_LANGUAGES:
            info = recommended_voice(language)
            assert info is not None
            assert voice_info(info.id) == info


class TestLanguageForVoice:
    def test_known_voice_returns_its_language(self) -> None:
        # af_heart is American English (a* prefix)
        assert language_for_voice("af_heart") == "en-us"
        # pf_dora is Brazilian Portuguese (p* prefix)
        assert language_for_voice("pf_dora") == "pt-br"

    def test_unknown_voice_returns_none(self) -> None:
        assert language_for_voice("not_a_voice") is None


class TestVoiceInfo:
    def test_known_voice_returns_full_info(self) -> None:
        info = voice_info("af_bella")
        assert info is not None
        assert isinstance(info, VoiceInfo)
        assert info.id == "af_bella"
        assert info.display_name == "Bella"
        assert info.language == "en-us"
        assert info.gender == "female"

    def test_unknown_voice_returns_none(self) -> None:
        assert voice_info("nope_nope") is None


class TestCatalogInvariants:
    """Whole-catalog regressions the UI depends on."""

    def test_all_voices_language_is_in_supported_set(self) -> None:
        for v in all_voices():
            assert v.language in SUPPORTED_LANGUAGES, (
                f"{v.id} speaks {v.language!r} which is not in SUPPORTED_LANGUAGES"
            )

    def test_voice_ids_are_unique(self) -> None:
        ids = [v.id for v in all_voices()]
        assert len(ids) == len(set(ids))

    def test_voice_ids_follow_kokoro_convention(self) -> None:
        # Kokoro id format: {lang}{gender}_{name} where lang ∈ a/b/e/f/h/i/j/p/z
        # and gender ∈ f/m. Any drift here means a voice was added without
        # updating the catalog docstring — worth catching early.
        valid_lang = set("abefhijpz")
        valid_gender = set("fm")
        for v in all_voices():
            assert "_" in v.id, f"{v.id} missing underscore separator"
            prefix, _rest = v.id.split("_", 1)
            assert len(prefix) == 2, f"{v.id} prefix {prefix!r} not 2 chars"
            assert prefix[0] in valid_lang, f"{v.id} unknown lang char {prefix[0]!r}"
            assert prefix[1] in valid_gender, f"{v.id} unknown gender {prefix[1]!r}"

    def test_voice_prefix_matches_declared_language(self) -> None:
        # Second-line defence: if the id prefix says "a" (en-us) but the
        # language field says "pt-br", something got mis-pasted in the
        # catalog literal. Fail loudly.
        prefix_to_language = {
            "a": "en-us",
            "b": "en-gb",
            "e": "es",
            "f": "fr",
            "h": "hi",
            "i": "it",
            "j": "ja",
            "p": "pt-br",
            "z": "zh",
        }
        for v in all_voices():
            expected = prefix_to_language[v.id[0]]
            assert v.language == expected, (
                f"{v.id}: prefix implies {expected!r} but catalog says {v.language!r}"
            )


class TestModuleExports:
    """Sanity check that the public surface hasn't silently changed."""

    def test_all_contains_expected_symbols(self) -> None:
        assert set(voice_catalog.__all__) == {
            "SUPPORTED_LANGUAGES",
            "VoiceInfo",
            "all_voices",
            "language_for_voice",
            "normalize_language",
            "recommended_voice",
            "supported_languages",
            "voice_info",
            "voices_for_language",
        }
