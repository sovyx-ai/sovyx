"""Tests for :mod:`sovyx.voice._double_talk_detector` [Phase 4 T4.9].

Coverage:

* :func:`compute_ncc` algebraic identities (perfect echo,
  uncorrelated, anti-correlated, silence floors).
* :class:`DoubleTalkDetector` threshold semantics + bounds
  validation.
* :class:`DoubleTalkDecision` carries both the raw NCC and the
  threshold verdict.
"""

from __future__ import annotations

import numpy as np
import pytest

from sovyx.voice._double_talk_detector import (
    DoubleTalkDecision,
    DoubleTalkDetector,
    compute_ncc,
)

# ── compute_ncc — algebraic identities ──────────────────────────────────


class TestComputeNccPureEcho:
    """When capture is a linear function of render, NCC ≈ ±1."""

    def test_capture_equals_render_yields_ncc_one(self) -> None:
        rng = np.random.default_rng(0)
        render = (rng.standard_normal(512) * 1000).astype(np.int16)
        capture = render.copy()
        ncc = compute_ncc(render, capture)
        assert ncc is not None
        assert ncc == pytest.approx(1.0, abs=1e-6)

    def test_capture_scaled_render_yields_ncc_one(self) -> None:
        # capture = 0.5 * render → NCC still 1.0 (correlation ignores
        # amplitude scaling because we normalise by both powers).
        rng = np.random.default_rng(1)
        render = (rng.standard_normal(512) * 2000).astype(np.int16)
        capture = (render.astype(np.float64) * 0.5).astype(np.int16)
        ncc = compute_ncc(render, capture)
        assert ncc is not None
        assert ncc == pytest.approx(1.0, abs=0.01)

    def test_anti_correlated_signals_yield_ncc_minus_one(self) -> None:
        rng = np.random.default_rng(2)
        render = (rng.standard_normal(512) * 1000).astype(np.int16)
        capture = (-render.astype(np.float64)).astype(np.int16)
        ncc = compute_ncc(render, capture)
        assert ncc is not None
        assert ncc == pytest.approx(-1.0, abs=1e-6)


class TestComputeNccUncorrelated:
    """Independent signals → NCC near zero."""

    def test_independent_random_signals(self) -> None:
        rng = np.random.default_rng(3)
        render = (rng.standard_normal(2048) * 1000).astype(np.int16)
        capture = (rng.standard_normal(2048) * 1000).astype(np.int16)
        ncc = compute_ncc(render, capture)
        assert ncc is not None
        # 2048 samples → standard error ~ 1/sqrt(2048) ≈ 0.022.
        # Allow ±0.1 to keep the test stable across rng implementations.
        assert abs(ncc) < 0.1


class TestComputeNccDoubleTalkScenario:
    """capture = render + user_voice → NCC drops below 1.0."""

    def test_user_voice_added_drops_ncc(self) -> None:
        rng = np.random.default_rng(4)
        render = (rng.standard_normal(2048) * 1000).astype(np.int16)
        user_voice = (rng.standard_normal(2048) * 1500).astype(np.int16)
        capture = render.astype(np.int32) + user_voice.astype(np.int32)
        capture_int16 = np.clip(capture, -32_768, 32_767).astype(np.int16)
        ncc = compute_ncc(render, capture_int16)
        assert ncc is not None
        # Mixed signal → NCC between 0 (uncorrelated dominant) and 1
        # (pure echo dominant). User voice ~50% louder than render
        # → expect ~0.5 to 0.7.
        assert 0.3 < ncc < 0.85


class TestComputeNccSilenceFloor:
    """Either side silent → NCC undefined → return None."""

    def test_silent_render_returns_none(self) -> None:
        capture = np.full(512, 1000, dtype=np.int16)
        render = np.zeros(512, dtype=np.int16)
        assert compute_ncc(render, capture) is None

    def test_silent_capture_returns_none(self) -> None:
        render = np.full(512, 1000, dtype=np.int16)
        capture = np.zeros(512, dtype=np.int16)
        assert compute_ncc(render, capture) is None

    def test_both_silent_returns_none(self) -> None:
        zeros = np.zeros(512, dtype=np.int16)
        assert compute_ncc(zeros, zeros) is None


class TestComputeNccValidation:
    """Shape / dtype contract."""

    def test_shape_mismatch_raises(self) -> None:
        with pytest.raises(ValueError, match="shape mismatch"):
            compute_ncc(
                np.zeros(512, dtype=np.int16),
                np.zeros(256, dtype=np.int16),
            )

    def test_render_non_int16_raises(self) -> None:
        with pytest.raises(ValueError, match="int16"):
            compute_ncc(
                np.zeros(512, dtype=np.float32),
                np.zeros(512, dtype=np.int16),
            )

    def test_capture_non_int16_raises(self) -> None:
        with pytest.raises(ValueError, match="int16"):
            compute_ncc(
                np.zeros(512, dtype=np.int16),
                np.zeros(512, dtype=np.float32),
            )


# ── DoubleTalkDetector ──────────────────────────────────────────────────


class TestDoubleTalkDetectorConstruction:
    def test_default_threshold_is_half(self) -> None:
        det = DoubleTalkDetector()
        assert det.threshold == 0.5

    def test_threshold_can_be_overridden(self) -> None:
        det = DoubleTalkDetector(threshold=0.3)
        assert det.threshold == 0.3

    def test_threshold_below_minus_one_rejected(self) -> None:
        with pytest.raises(ValueError, match=r"\[-1.0, 1.0\]"):
            DoubleTalkDetector(threshold=-1.5)

    def test_threshold_above_one_rejected(self) -> None:
        with pytest.raises(ValueError, match=r"\[-1.0, 1.0\]"):
            DoubleTalkDetector(threshold=1.5)


class TestDoubleTalkDetectorAnalyze:
    def test_pure_echo_below_threshold_not_detected(self) -> None:
        rng = np.random.default_rng(5)
        render = (rng.standard_normal(512) * 1000).astype(np.int16)
        capture = render.copy()  # pure echo → NCC=1.0
        det = DoubleTalkDetector(threshold=0.5)
        decision = det.analyze(render, capture)
        assert decision.ncc == pytest.approx(1.0, abs=0.01)
        assert decision.detected is False

    def test_uncorrelated_below_threshold_detected(self) -> None:
        rng = np.random.default_rng(6)
        render = (rng.standard_normal(2048) * 1000).astype(np.int16)
        capture = (rng.standard_normal(2048) * 1000).astype(np.int16)
        det = DoubleTalkDetector(threshold=0.5)
        decision = det.analyze(render, capture)
        assert decision.ncc is not None
        assert decision.detected is True  # NCC ~ 0 < 0.5

    def test_silence_returns_undecided_decision(self) -> None:
        det = DoubleTalkDetector()
        decision = det.analyze(
            np.zeros(512, dtype=np.int16),
            np.zeros(512, dtype=np.int16),
        )
        assert decision.ncc is None
        assert decision.detected is False

    def test_threshold_boundary_exclusive_below(self) -> None:
        # NCC algebra for capture = α·render + β·unrelated (with
        # render ⊥ unrelated, equal variance):
        #   NCC = α / sqrt(α² + β²)
        # So α=0.2, β=0.8 → NCC = 0.2 / sqrt(0.04 + 0.64) ≈ 0.243.
        # Below the threshold=0.45 → detected=True.
        rng = np.random.default_rng(7)
        render = (rng.standard_normal(2048) * 1000).astype(np.int16)
        unrelated = (rng.standard_normal(2048) * 1000).astype(np.int16)
        capture = (0.2 * render + 0.8 * unrelated).astype(np.int16)
        det = DoubleTalkDetector(threshold=0.45)
        decision = det.analyze(render, capture)
        assert decision.ncc is not None
        assert decision.ncc < 0.45
        assert decision.detected is True


class TestDoubleTalkDecisionShape:
    """The frozen-slots dataclass surface."""

    def test_undecided_decision(self) -> None:
        d = DoubleTalkDecision(ncc=None, detected=False)
        assert d.ncc is None
        assert d.detected is False

    def test_detected_decision(self) -> None:
        d = DoubleTalkDecision(ncc=0.3, detected=True)
        assert d.ncc == 0.3
        assert d.detected is True

    def test_decision_is_immutable(self) -> None:
        d = DoubleTalkDecision(ncc=0.7, detected=False)
        with pytest.raises((AttributeError, Exception)):
            d.ncc = 0.0  # type: ignore[misc]
