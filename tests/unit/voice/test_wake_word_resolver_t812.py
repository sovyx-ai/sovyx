"""Tests for :class:`sovyx.voice._wake_word_resolver.WakeWordModelResolver` — Phase 8 / T8.12.

Covers the three resolution strategies (EXACT / PHONETIC / NONE) +
the :class:`PretrainedModelRegistry` filesystem listing.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from sovyx.voice import _phonetic_matcher
from sovyx.voice._phonetic_matcher import PhoneticMatcher
from sovyx.voice._wake_word_resolver import (
    PretrainedModelRegistry,
    WakeWordModelResolver,
    WakeWordResolution,
    WakeWordResolutionStrategy,
)

# ── PretrainedModelRegistry ──────────────────────────────────────────


class TestPretrainedModelRegistry:
    def test_missing_directory_returns_empty_list(self, tmp_path: Path) -> None:
        registry = PretrainedModelRegistry(tmp_path / "nonexistent")
        assert registry.list_available() == []
        assert registry.model_path("anything") is None

    def test_lists_only_onnx_files(self, tmp_path: Path) -> None:
        models_dir = tmp_path / "pretrained"
        models_dir.mkdir()
        (models_dir / "jonny.onnx").write_bytes(b"fake")
        (models_dir / "lucia.onnx").write_bytes(b"fake")
        (models_dir / "readme.txt").write_text("ignore me")
        (models_dir / "marie.bin").write_bytes(b"ignore me too")

        registry = PretrainedModelRegistry(models_dir)
        # Sorted alphabetical for deterministic iteration.
        assert registry.list_available() == ["jonny", "lucia"]

    def test_normalises_filenames_with_diacritics(self, tmp_path: Path) -> None:
        models_dir = tmp_path / "pretrained"
        models_dir.mkdir()
        (models_dir / "Lúcia.onnx").write_bytes(b"fake")
        (models_dir / "JONNY.onnx").write_bytes(b"fake")

        registry = PretrainedModelRegistry(models_dir)
        # Both ASCII-folded + lowercased.
        assert registry.list_available() == ["jonny", "lucia"]

    def test_model_path_normalises_lookup(self, tmp_path: Path) -> None:
        models_dir = tmp_path / "pretrained"
        models_dir.mkdir()
        target = models_dir / "Lúcia.onnx"
        target.write_bytes(b"fake")

        registry = PretrainedModelRegistry(models_dir)
        # Multiple lookups with different normalisations resolve.
        assert registry.model_path("Lúcia") == target.resolve()
        assert registry.model_path("lucia") == target.resolve()
        assert registry.model_path("LUCIA") == target.resolve()
        assert registry.model_path("nonexistent") is None

    def test_empty_query_returns_none(self, tmp_path: Path) -> None:
        models_dir = tmp_path / "pretrained"
        models_dir.mkdir()
        (models_dir / "jonny.onnx").write_bytes(b"fake")
        registry = PretrainedModelRegistry(models_dir)
        assert registry.model_path("") is None
        assert registry.model_path("   ") is None

    def test_models_dir_property(self, tmp_path: Path) -> None:
        models_dir = tmp_path / "pretrained"
        registry = PretrainedModelRegistry(models_dir)
        assert registry.models_dir == models_dir


# ── WakeWordModelResolver ────────────────────────────────────────────


@pytest.fixture
def populated_registry(tmp_path: Path) -> PretrainedModelRegistry:
    """A registry with three pretrained models."""
    models_dir = tmp_path / "pretrained"
    models_dir.mkdir()
    for name in ("jonny", "lucia", "marie"):
        (models_dir / f"{name}.onnx").write_bytes(b"fake")
    return PretrainedModelRegistry(models_dir)


@pytest.fixture
def empty_registry(tmp_path: Path) -> PretrainedModelRegistry:
    return PretrainedModelRegistry(tmp_path / "empty_pool")


class TestExactStrategy:
    def test_exact_match_returns_exact(
        self,
        populated_registry: PretrainedModelRegistry,
    ) -> None:
        resolver = WakeWordModelResolver(registry=populated_registry)
        result = resolver.resolve("Jonny")
        assert result.strategy is WakeWordResolutionStrategy.EXACT
        assert result.matched_name == "jonny"
        assert result.phoneme_distance == 0
        assert result.model_path is not None
        assert result.model_path.name == "jonny.onnx"

    def test_exact_match_normalises_diacritics(
        self,
        populated_registry: PretrainedModelRegistry,
    ) -> None:
        resolver = WakeWordModelResolver(registry=populated_registry)
        result = resolver.resolve("LÚCIA")
        assert result.strategy is WakeWordResolutionStrategy.EXACT
        assert result.matched_name == "lucia"


class TestPhoneticStrategy:
    def test_phonetic_match_when_no_exact(
        self,
        populated_registry: PretrainedModelRegistry,
    ) -> None:
        # Force matcher available + subprocess returns "" so it
        # falls back to ASCII-fold Levenshtein.
        with patch.object(
            _phonetic_matcher.shutil,
            "which",
            return_value="/fake/bin/espeak-ng",
        ):
            matcher = PhoneticMatcher()

        # Mock the subprocess call inside find_closest to return "" so
        # ASCII-fold path engages deterministically.
        from subprocess import CompletedProcess  # noqa: PLC0415

        with patch.object(
            _phonetic_matcher.subprocess,
            "run",
            return_value=CompletedProcess(args=[], returncode=1, stdout="", stderr=""),
        ):
            resolver = WakeWordModelResolver(
                registry=populated_registry,
                phonetic_matcher=matcher,
                max_phoneme_distance=5,
            )
            result = resolver.resolve("Jhonatan")
            assert result.strategy is WakeWordResolutionStrategy.PHONETIC
            assert result.matched_name == "jonny"
            assert result.phoneme_distance == 5  # noqa: PLR2004
            assert result.model_path is not None
            assert result.model_path.name == "jonny.onnx"

    def test_no_match_when_above_threshold(
        self,
        populated_registry: PretrainedModelRegistry,
    ) -> None:
        with patch.object(
            _phonetic_matcher.shutil,
            "which",
            return_value="/fake/bin/espeak-ng",
        ):
            matcher = PhoneticMatcher()
        from subprocess import CompletedProcess  # noqa: PLC0415

        with patch.object(
            _phonetic_matcher.subprocess,
            "run",
            return_value=CompletedProcess(args=[], returncode=1, stdout="", stderr=""),
        ):
            resolver = WakeWordModelResolver(
                registry=populated_registry,
                phonetic_matcher=matcher,
                max_phoneme_distance=1,
            )
            # Threshold 1 — no candidate within 1 of "Jhonatan".
            result = resolver.resolve("Jhonatan")
            assert result.strategy is WakeWordResolutionStrategy.NONE


class TestNoneStrategy:
    def test_empty_wake_word_returns_none(
        self,
        populated_registry: PretrainedModelRegistry,
    ) -> None:
        resolver = WakeWordModelResolver(registry=populated_registry)
        result = resolver.resolve("")
        assert result.strategy is WakeWordResolutionStrategy.NONE
        assert result.model_path is None
        assert result.phoneme_distance == -1

    def test_whitespace_wake_word_returns_none(
        self,
        populated_registry: PretrainedModelRegistry,
    ) -> None:
        resolver = WakeWordModelResolver(registry=populated_registry)
        result = resolver.resolve("   \t\n")
        assert result.strategy is WakeWordResolutionStrategy.NONE

    def test_no_matcher_no_exact_returns_none(
        self,
        populated_registry: PretrainedModelRegistry,
    ) -> None:
        resolver = WakeWordModelResolver(
            registry=populated_registry,
            phonetic_matcher=None,
        )
        result = resolver.resolve("Jhonatan")  # not in pool, no matcher
        assert result.strategy is WakeWordResolutionStrategy.NONE

    def test_unavailable_matcher_no_exact_returns_none(
        self,
        populated_registry: PretrainedModelRegistry,
    ) -> None:
        matcher = PhoneticMatcher(enabled=False)
        resolver = WakeWordModelResolver(
            registry=populated_registry,
            phonetic_matcher=matcher,
        )
        result = resolver.resolve("Jhonatan")
        assert result.strategy is WakeWordResolutionStrategy.NONE

    def test_empty_pool_with_matcher_returns_none(
        self,
        empty_registry: PretrainedModelRegistry,
    ) -> None:
        with patch.object(
            _phonetic_matcher.shutil,
            "which",
            return_value="/fake/bin/espeak-ng",
        ):
            matcher = PhoneticMatcher()
        resolver = WakeWordModelResolver(
            registry=empty_registry,
            phonetic_matcher=matcher,
        )
        # Empty pool — even matcher can't find anything.
        result = resolver.resolve("Jonny")
        assert result.strategy is WakeWordResolutionStrategy.NONE


# ── Resolution dataclass shape ───────────────────────────────────────


class TestResolutionImmutable:
    def test_dataclass_is_frozen(self, tmp_path: Path) -> None:
        models_dir = tmp_path / "pretrained"
        models_dir.mkdir()
        (models_dir / "jonny.onnx").write_bytes(b"fake")
        registry = PretrainedModelRegistry(models_dir)
        resolver = WakeWordModelResolver(registry=registry)
        result = resolver.resolve("Jonny")
        with pytest.raises((AttributeError, TypeError)):
            result.matched_name = "tampered"  # type: ignore[misc]


class TestStrategyEnum:
    def test_strategy_string_values(self) -> None:
        # Stable wire format for telemetry labels.
        assert WakeWordResolutionStrategy.EXACT.value == "exact"
        assert WakeWordResolutionStrategy.PHONETIC.value == "phonetic"
        assert WakeWordResolutionStrategy.NONE.value == "none"

    def test_resolution_uses_correct_enum_values(
        self,
        populated_registry: PretrainedModelRegistry,
    ) -> None:
        # Pin against accidental enum renaming — telemetry labels
        # depend on these exact strings.
        resolver = WakeWordModelResolver(registry=populated_registry)
        result = resolver.resolve("Jonny")
        assert isinstance(result, WakeWordResolution)
        assert str(result.strategy) == "exact"
