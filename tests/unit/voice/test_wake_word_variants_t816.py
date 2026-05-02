"""Tests for :mod:`sovyx.voice._wake_word_variants` — Phase 8 / T8.16.

Covers:

* Per-language phonetic mishear table look-up (pt/es/fr/de).
* ASCII-fold normalisation of the source key (``"Lúcia"`` ↔ ``"Lucia"``).
* BCP-47 primary subtag extraction (``"pt-BR"`` → ``"pt"``).
* Insertion-ordered dedup against the base variants.
* Hey-prefix mishears appended consistently.
* Unknown-language / unknown-name passes through unchanged.
* Empty inputs return base list verbatim.
* Optional ``matcher`` parameter (currently no-op; signature reserved).
"""

from __future__ import annotations

from sovyx.voice._phonetic_matcher import PhoneticMatcher
from sovyx.voice._wake_word_variants import (
    _ascii_fold,
    _language_prefix,
    expand_wake_word_variants,
)

# ── Helpers ──────────────────────────────────────────────────────────


class TestLanguagePrefix:
    def test_extracts_primary_subtag(self) -> None:
        assert _language_prefix("pt-BR") == "pt"
        assert _language_prefix("en-US") == "en"
        assert _language_prefix("fr-CA") == "fr"

    def test_lowercases(self) -> None:
        assert _language_prefix("PT-BR") == "pt"

    def test_no_subtag(self) -> None:
        assert _language_prefix("en") == "en"

    def test_empty(self) -> None:
        assert _language_prefix("") == ""


# ── Mishear lookup ───────────────────────────────────────────────────


class TestPortugueseMishears:
    def test_lucia_diacritic_matches_table(self) -> None:
        result = expand_wake_word_variants(
            ["lúcia", "lucia"],
            wake_word="Lúcia",
            language="pt-BR",
        )
        # Hand table for pt: lucia → lousha, luchia.
        assert "lousha" in result
        assert "luchia" in result
        # "hey" prefix mishears appended.
        assert "hey lousha" in result
        assert "hey luchia" in result
        # Base preserved.
        assert result[0] == "lúcia"
        assert result[1] == "lucia"

    def test_pt_pt_works_same_as_pt_br(self) -> None:
        result = expand_wake_word_variants(
            ["lucia"],
            wake_word="Lúcia",
            language="pt-PT",
        )
        assert "lousha" in result


class TestSpanishMishears:
    def test_joaquin_mishears(self) -> None:
        result = expand_wake_word_variants(
            ["joaquín", "joaquin"],
            wake_word="Joaquín",
            language="es-ES",
        )
        assert "joaquim" in result
        assert "hwah-keen" in result


class TestFrenchMishears:
    def test_francois_mishears(self) -> None:
        result = expand_wake_word_variants(
            ["françois", "francois"],
            wake_word="François",
            language="fr-FR",
        )
        assert "frahn-swah" in result
        # "francoise" is the female form, also a mishear of François.
        assert "francoise" in result


class TestGermanMishears:
    def test_muller_mishears(self) -> None:
        result = expand_wake_word_variants(
            ["müller", "muller"],
            wake_word="Müller",
            language="de-DE",
        )
        assert "mueller" in result
        assert "miller" in result


# ── Pass-through (no-op cases) ───────────────────────────────────────


class TestPassThrough:
    def test_unknown_language_returns_base_unchanged(self) -> None:
        base = ["sovyx", "hey sovyx"]
        result = expand_wake_word_variants(
            base,
            wake_word="Sovyx",
            language="xx-XX",
        )
        assert result == base

    def test_unknown_name_in_known_language_returns_base(self) -> None:
        base = ["sovyx", "hey sovyx"]
        result = expand_wake_word_variants(
            base,
            wake_word="Sovyx",
            language="pt-BR",
        )
        assert result == base

    def test_empty_wake_word_returns_base(self) -> None:
        base = ["something"]
        result = expand_wake_word_variants(
            base,
            wake_word="",
            language="pt-BR",
        )
        assert result == base

    def test_empty_base_variants_returns_empty(self) -> None:
        result = expand_wake_word_variants(
            [],
            wake_word="Lúcia",
            language="pt-BR",
        )
        assert result == []


# ── Dedup + insertion order ──────────────────────────────────────────


class TestDedup:
    def test_existing_mishear_in_base_not_duplicated(self) -> None:
        # Base already contains "lousha" → no duplicate after expansion.
        base = ["lúcia", "lousha"]
        result = expand_wake_word_variants(
            base,
            wake_word="Lúcia",
            language="pt-BR",
        )
        assert result.count("lousha") == 1

    def test_base_order_preserved(self) -> None:
        base = ["lúcia", "lucia", "hey lúcia", "hey lucia"]
        result = expand_wake_word_variants(
            base,
            wake_word="Lúcia",
            language="pt-BR",
        )
        # Base entries come first in their original order.
        assert result[: len(base)] == base


# ── Optional matcher parameter ───────────────────────────────────────


class TestOptionalMatcher:
    def test_matcher_passes_through_no_op(self) -> None:
        """Passing a matcher must not change the result (current
        implementation uses the hand table; matcher is reserved)."""
        matcher = PhoneticMatcher(enabled=False)
        result_with = expand_wake_word_variants(
            ["lucia"],
            wake_word="Lúcia",
            language="pt-BR",
            matcher=matcher,
        )
        result_without = expand_wake_word_variants(
            ["lucia"],
            wake_word="Lúcia",
            language="pt-BR",
        )
        assert result_with == result_without


# ── ASCII-fold helper ────────────────────────────────────────────────


class TestAsciiFold:
    def test_strips_diacritics(self) -> None:
        assert _ascii_fold("Lúcia") == "lucia"
        assert _ascii_fold("Müller") == "muller"
        assert _ascii_fold("François") == "francois"

    def test_preserves_already_ascii(self) -> None:
        assert _ascii_fold("Sovyx") == "sovyx"
