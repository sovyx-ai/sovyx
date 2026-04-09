"""Tests for sovyx.cli.commands.brain_analyze."""

from __future__ import annotations

import pytest

from sovyx.cli.commands.brain_analyze import (
    _analyze_scores,
    _quartiles,
    _shannon_entropy,
)


class TestShannonEntropy:
    """Shannon entropy computation."""

    def test_uniform_high(self) -> None:
        values = [i / 100 for i in range(100)]
        assert _shannon_entropy(values) > 3.5  # noqa: PLR2004

    def test_concentrated_low(self) -> None:
        values = [0.5] * 100
        assert _shannon_entropy(values) < 0.01  # noqa: PLR2004

    def test_empty(self) -> None:
        assert _shannon_entropy([]) == 0.0

    def test_single(self) -> None:
        assert _shannon_entropy([0.5]) == 0.0


class TestQuartiles:
    """Quartile computation."""

    def test_sorted_values(self) -> None:
        values = list(range(100))
        q1, median, q3 = _quartiles([v / 100 for v in values])
        assert q1 == pytest.approx(0.25, abs=0.02)
        assert median == pytest.approx(0.50, abs=0.02)
        assert q3 == pytest.approx(0.75, abs=0.02)

    def test_empty(self) -> None:
        assert _quartiles([]) == (0.0, 0.0, 0.0)


class TestAnalyzeScores:
    """Score distribution analysis."""

    def test_healthy_distribution(self) -> None:
        values = [i / 100 for i in range(100)]
        result = _analyze_scores(values, "importance")
        assert result["health"] == "🟢 healthy"
        assert result["count"] == 100  # noqa: PLR2004

    def test_collapsed_distribution(self) -> None:
        values = [0.5] * 100
        result = _analyze_scores(values, "importance")
        assert "CRITICAL" in str(result["health"])

    def test_empty(self) -> None:
        result = _analyze_scores([], "importance")
        assert result["count"] == 0

    def test_json_keys_present(self) -> None:
        values = [0.3, 0.5, 0.7, 0.9]
        result = _analyze_scores(values, "confidence")
        for key in ("mean", "min", "max", "q1", "median", "q3", "entropy", "health"):
            assert key in result
