"""Tests for :class:`sovyx.voice._phonetic_matcher.PhoneticMatcher` — Phase 8 / T8.12.

espeak-ng is an OPTIONAL dependency. These tests cover both branches:

* When espeak-ng is NOT on PATH (most CI environments): matcher
  reports ``is_available == False``; ``to_phonemes`` returns empty;
  ``find_closest`` returns ``None``. The pure-string fallback path
  via ASCII-fold + Levenshtein is exercised independently.
* When espeak-ng IS available: subprocess invocation is mocked so
  the test doesn't depend on the host machine's espeak-ng version
  / language packs.

Reference: master mission ``MISSION-voice-final-skype-grade-2026.md``
§Phase 8 / T8.12.
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from sovyx.voice import _phonetic_matcher
from sovyx.voice._phonetic_matcher import PhoneticMatcher, _ascii_fold, _levenshtein

# ── Pure helpers (no espeak-ng dependency) ───────────────────────────


class TestAsciiFold:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Lúcia", "lucia"),
            ("Joaquín", "joaquin"),
            ("Müller", "muller"),
            ("François", "francois"),
            ("Sovyx", "sovyx"),
            ("", ""),
        ],
    )
    def test_strips_diacritics_and_lowercases(self, raw: str, expected: str) -> None:
        assert _ascii_fold(raw) == expected


class TestLevenshtein:
    @pytest.mark.parametrize(
        ("a", "b", "expected"),
        [
            ("", "", 0),
            ("abc", "abc", 0),
            ("kitten", "sitting", 3),
            ("jonny", "jhonatan", 5),
            ("abc", "", 3),
            ("a", "abc", 2),
        ],
    )
    def test_distances_known_pairs(self, a: str, b: str, expected: int) -> None:
        assert _levenshtein(a, b) == expected

    def test_symmetric(self) -> None:
        assert _levenshtein("hello", "world") == _levenshtein("world", "hello")


# ── PhoneticMatcher availability + disabled paths ────────────────────


class TestAvailability:
    def test_disabled_explicit_returns_unavailable(self) -> None:
        matcher = PhoneticMatcher(enabled=False)
        assert matcher.is_available is False
        # All operations gracefully degrade.
        assert matcher.to_phonemes("anything") == ""
        assert matcher.find_closest("a", ["b", "c"], max_distance=1) is None

    def test_enabled_true_raises_when_binary_absent(self) -> None:
        with (
            patch.object(_phonetic_matcher.shutil, "which", return_value=None),
            pytest.raises(RuntimeError, match="espeak-ng binary not found"),
        ):
            PhoneticMatcher(enabled=True)

    def test_auto_detect_off_when_binary_absent(self) -> None:
        """Default ``enabled=None`` → graceful unavailability when
        espeak-ng is not on PATH."""
        with patch.object(_phonetic_matcher.shutil, "which", return_value=None):
            matcher = PhoneticMatcher()
            assert matcher.is_available is False

    def test_auto_detect_on_when_binary_present(self) -> None:
        with patch.object(
            _phonetic_matcher.shutil,
            "which",
            return_value="/fake/bin/espeak-ng",
        ):
            matcher = PhoneticMatcher()
            assert matcher.is_available is True


# ── to_phonemes — subprocess mocked for determinism ──────────────────


class TestToPhonemesWithMockedSubprocess:
    def _matcher(self) -> PhoneticMatcher:
        with patch.object(
            _phonetic_matcher.shutil,
            "which",
            return_value="/fake/bin/espeak-ng",
        ):
            return PhoneticMatcher()

    def test_empty_input_returns_empty(self) -> None:
        matcher = self._matcher()
        assert matcher.to_phonemes("") == ""
        assert matcher.to_phonemes("   ") == ""

    def test_subprocess_success_returns_stripped_stdout(self) -> None:
        matcher = self._matcher()
        result = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="  ʤˈɒni  \n",
            stderr="",
        )
        with patch.object(_phonetic_matcher.subprocess, "run", return_value=result):
            assert matcher.to_phonemes("Jonny") == "ʤˈɒni"

    def test_subprocess_nonzero_returns_empty(self) -> None:
        matcher = self._matcher()
        result = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="error: language not found",
        )
        with patch.object(_phonetic_matcher.subprocess, "run", return_value=result):
            assert matcher.to_phonemes("Anything") == ""

    def test_subprocess_timeout_returns_empty(self) -> None:
        matcher = self._matcher()
        with patch.object(
            _phonetic_matcher.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd="x", timeout=5),
        ):
            assert matcher.to_phonemes("Anything") == ""

    def test_subprocess_oserror_returns_empty(self) -> None:
        matcher = self._matcher()
        with patch.object(
            _phonetic_matcher.subprocess,
            "run",
            side_effect=OSError("permission denied"),
        ):
            assert matcher.to_phonemes("Anything") == ""


# ── find_closest — pure logic, no subprocess ─────────────────────────


class TestFindClosest:
    """Exercises the matching logic with phonemes mocked to fixed
    values so the test isolates the algorithm from espeak-ng
    behaviour."""

    def _matcher(self) -> PhoneticMatcher:
        with patch.object(
            _phonetic_matcher.shutil,
            "which",
            return_value="/fake/bin/espeak-ng",
        ):
            return PhoneticMatcher()

    def test_unavailable_matcher_returns_none(self) -> None:
        matcher = PhoneticMatcher(enabled=False)
        assert matcher.find_closest("Jhonatan", ["jonny"], max_distance=3) is None

    def test_empty_candidates_returns_none(self) -> None:
        matcher = self._matcher()
        assert matcher.find_closest("Jhonatan", [], max_distance=3) is None

    def test_finds_closest_within_threshold(self) -> None:
        """When phoneme conversion fails (subprocess returns ""),
        the matcher falls back to ASCII-fold comparison — which is
        sufficient for the typical short-name case + makes the test
        deterministic without relying on espeak-ng's actual phoneme
        output."""
        matcher = self._matcher()
        # All to_phonemes calls return "" → falls back to ASCII-fold.
        with patch.object(
            _phonetic_matcher.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr=""),
        ):
            result = matcher.find_closest(
                "Jhonatan",
                ["jonny", "lucia", "marie"],
                max_distance=5,
            )
        assert result is not None
        # ASCII-folded distances: jhonatan→jonny=5, →lucia=7, →marie=7.
        # jonny wins at 5, within threshold.
        name, distance = result
        assert name == "jonny"
        assert distance == 5  # noqa: PLR2004

    def test_returns_none_when_above_threshold(self) -> None:
        matcher = self._matcher()
        with patch.object(
            _phonetic_matcher.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr=""),
        ):
            # Threshold 1, but jhonatan→jonny = 4 → no match.
            result = matcher.find_closest(
                "Jhonatan",
                ["jonny", "lucia"],
                max_distance=1,
            )
        assert result is None

    def test_alphabetical_tiebreaker_on_equal_distance(self) -> None:
        """On equal distance, alphabetical tie-breaker → 'aaa' wins
        over 'zzz' even when both are equidistant. Critical for
        deterministic telemetry across runs."""
        matcher = self._matcher()
        with patch.object(
            _phonetic_matcher.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr=""),
        ):
            # query="bb", candidates=["aaa", "zzz"]; Lev("bb","aaa")=3,
            # Lev("bb","zzz")=3 — alphabetical tiebreak picks "aaa".
            result = matcher.find_closest("bb", ["zzz", "aaa"], max_distance=3)
        assert result is not None
        assert result[0] == "aaa"


# ── Distance helper ──────────────────────────────────────────────────


class TestDistance:
    def test_ascii_fold_applied_before_distance(self) -> None:
        matcher = PhoneticMatcher(enabled=False)
        # "Lúcia" and "Lucia" should be distance 0 after ASCII-fold.
        assert matcher.distance("Lúcia", "Lucia") == 0

    def test_distance_known_pair(self) -> None:
        matcher = PhoneticMatcher(enabled=False)
        # Both ASCII-folded: "muller" vs "miller" = distance 1 (u→i).
        assert matcher.distance("Müller", "Miller") == 1
