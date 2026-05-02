"""Tests for ``compose_wake_variants_for_locale`` — Phase 7 / T7.11-T7.16.

Per-locale wake-variant composition. Covers:

* Locale-agnostic Sovyx phonetic variants are always present.
* Per-locale greeting prefixes compose with each Sovyx variant.
* Unknown / empty locales fall back to ``en``.
* Primary subtag normalisation (``pt-BR`` → ``pt``).
* Backward-compat: ``_WAKE_VARIANTS`` matches English composition.
"""

from __future__ import annotations

import pytest

from sovyx.voice.wake_word import (
    _LOCALE_GREETING_PREFIXES,
    _SOVYX_PHONETIC_VARIANTS,
    _WAKE_VARIANTS,
    compose_wake_variants_for_locale,
)

# ── Locale-agnostic Sovyx variants ───────────────────────────────────


class TestSovyxVariantsAlwaysPresent:
    @pytest.mark.parametrize(
        "locale",
        ["en-US", "es-ES", "pt-BR", "fr-FR", "de-DE", "it-IT", "zh-CN", ""],
    )
    def test_bare_sovyx_in_every_locale(self, locale: str) -> None:
        """Every supported locale (and the unknown-locale fallback)
        must accept the bare wake word "sovyx" without a greeting."""
        variants = compose_wake_variants_for_locale(locale)
        for sovyx_variant in _SOVYX_PHONETIC_VARIANTS:
            assert sovyx_variant in variants, (
                f"locale={locale!r} missing bare variant {sovyx_variant!r}"
            )

    def test_returned_set_is_frozen(self) -> None:
        variants = compose_wake_variants_for_locale("en")
        assert isinstance(variants, frozenset)


# ── Per-locale greeting prefixes ─────────────────────────────────────


class TestEnglish:
    def test_hey_sovyx_present(self) -> None:
        variants = compose_wake_variants_for_locale("en-US")
        assert "hey sovyx" in variants
        assert "hi sovyx" in variants
        assert "ok sovyx" in variants

    def test_yo_sovyx_present(self) -> None:
        variants = compose_wake_variants_for_locale("en-GB")
        assert "yo sovyx" in variants


class TestSpanish:
    def test_hola_sovyx_present(self) -> None:
        # T7.11 — "Hola Sovyx".
        variants = compose_wake_variants_for_locale("es-ES")
        assert "hola sovyx" in variants
        assert "oye sovyx" in variants

    def test_es_mx_inherits_es(self) -> None:
        """Mexican Spanish inherits the same prefix table as ES."""
        variants_es = compose_wake_variants_for_locale("es-ES")
        variants_mx = compose_wake_variants_for_locale("es-MX")
        assert variants_es == variants_mx


class TestPortuguese:
    def test_oi_sovyx_present(self) -> None:
        # T7.12 — "Oi Sovyx".
        variants = compose_wake_variants_for_locale("pt-BR")
        assert "oi sovyx" in variants
        assert "olá sovyx" in variants
        assert "ola sovyx" in variants
        assert "ei sovyx" in variants

    def test_pt_pt_inherits_pt(self) -> None:
        variants_br = compose_wake_variants_for_locale("pt-BR")
        variants_pt = compose_wake_variants_for_locale("pt-PT")
        assert variants_br == variants_pt


class TestFrench:
    def test_bonjour_sovyx_present(self) -> None:
        # T7.13 — "Bonjour Sovyx".
        variants = compose_wake_variants_for_locale("fr-FR")
        assert "bonjour sovyx" in variants
        assert "salut sovyx" in variants
        assert "coucou sovyx" in variants

    def test_fr_ca_inherits_fr(self) -> None:
        variants_fr = compose_wake_variants_for_locale("fr-FR")
        variants_ca = compose_wake_variants_for_locale("fr-CA")
        assert variants_fr == variants_ca


class TestGerman:
    def test_hallo_sovyx_present(self) -> None:
        # T7.14 — "Hallo Sovyx".
        variants = compose_wake_variants_for_locale("de-DE")
        assert "hallo sovyx" in variants
        assert "hey sovyx" in variants  # German also uses "hey"


class TestItalian:
    def test_ciao_sovyx_present(self) -> None:
        variants = compose_wake_variants_for_locale("it-IT")
        assert "ciao sovyx" in variants
        assert "ehi sovyx" in variants


class TestMandarin:
    def test_pinyin_and_hanzi_both_present(self) -> None:
        # T7.15 — "你好 Sovyx" (Mandarin) covered in both
        # Pinyin (ni hao / nihao) and Han characters (你好).
        variants = compose_wake_variants_for_locale("zh-CN")
        assert "你好 sovyx" in variants
        assert "ni hao sovyx" in variants
        assert "nihao sovyx" in variants
        assert "嗨 sovyx" in variants
        assert "hai sovyx" in variants

    def test_zh_tw_inherits_zh(self) -> None:
        variants_cn = compose_wake_variants_for_locale("zh-CN")
        variants_tw = compose_wake_variants_for_locale("zh-TW")
        assert variants_cn == variants_tw


# ── Fallback semantics ──────────────────────────────────────────────


class TestUnknownLocaleFallback:
    def test_unknown_locale_falls_back_to_english(self) -> None:
        """Unknown locales get the English variant set so operators
        with novel languages still have a working match path."""
        variants_unknown = compose_wake_variants_for_locale("xx-YY")
        variants_en = compose_wake_variants_for_locale("en")
        assert variants_unknown == variants_en

    def test_empty_locale_falls_back_to_english(self) -> None:
        variants = compose_wake_variants_for_locale("")
        variants_en = compose_wake_variants_for_locale("en")
        assert variants == variants_en

    def test_lowercased_lookup(self) -> None:
        """Primary subtag matching is case-insensitive (BCP-47
        spec recommends lowercase but real-world data is mixed)."""
        variants_lower = compose_wake_variants_for_locale("pt-BR")
        variants_upper = compose_wake_variants_for_locale("PT-BR")
        assert variants_lower == variants_upper


# ── Composition contract ────────────────────────────────────────────


class TestCompositionContract:
    def test_size_matches_formula(self) -> None:
        """For a known locale, |variants| = |sovyx variants| +
        |prefixes| × |sovyx variants|."""
        variants = compose_wake_variants_for_locale("pt-BR")
        n_sovyx = len(_SOVYX_PHONETIC_VARIANTS)
        n_prefixes = len(_LOCALE_GREETING_PREFIXES["pt"])
        # Worst-case (no collisions across locales): n_sovyx +
        # n_prefixes × n_sovyx. Greetings don't appear in
        # _SOVYX_PHONETIC_VARIANTS so no collision.
        assert len(variants) == n_sovyx + n_prefixes * n_sovyx

    def test_no_empty_strings_in_set(self) -> None:
        for locale_subtag in _LOCALE_GREETING_PREFIXES:
            variants = compose_wake_variants_for_locale(locale_subtag)
            assert "" not in variants

    def test_all_lowercased(self) -> None:
        """Stage-2 STT verifier expects lowercased variants —
        matching is case-insensitive only when the variants are
        stored lowercased + the transcript is lowercased before
        substring scan."""
        for locale_subtag in _LOCALE_GREETING_PREFIXES:
            variants = compose_wake_variants_for_locale(locale_subtag)
            for v in variants:
                # Allow non-ASCII characters (Han, etc.) but no
                # uppercase Latin letters.
                latin_chars = [c for c in v if "a" <= c.lower() <= "z"]
                for c in latin_chars:
                    assert c == c.lower(), (
                        f"locale={locale_subtag!r} variant {v!r} has uppercase {c!r}"
                    )


# ── Backward compatibility ──────────────────────────────────────────


class TestBackwardCompat:
    def test_wake_variants_module_constant_matches_english(self) -> None:
        """Pre-T7.11 callers import ``_WAKE_VARIANTS`` directly. The
        constant must continue to equal the English-locale composition
        so existing single-mind code paths aren't broken."""
        assert compose_wake_variants_for_locale("en") == _WAKE_VARIANTS
