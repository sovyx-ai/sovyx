"""Tests for :mod:`sovyx.voice._quality_metrics` [Phase 4 T4.21].

Coverage:

* :class:`QualityScore` dataclass shape + ``is_available`` predicate.
* :class:`QualityEstimator` Protocol compliance for all
  implementations.
* :class:`NoOpQualityEstimator` returns NaN for every sub-score.
* :class:`DnsmosQualityEstimator`:
  - construction with the speechmos extras present (skipped here
    because we don't ship librosa as a default — the lazy-import
    + monkeypatch test below proves the contract without paying
    the dep cost),
  - construction WITHOUT speechmos extras → :class:`QualityEstimatorLoadError`,
  - score result shape with a stub speechmos module.
* :func:`build_quality_estimator` factory matrix.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from sovyx.voice._quality_metrics import (
    DnsmosQualityEstimator,
    NoOpQualityEstimator,
    QualityEstimator,
    QualityEstimatorLoadError,
    QualityScore,
    build_quality_estimator,
)

# ── QualityScore ─────────────────────────────────────────────────────────


class TestQualityScore:
    def test_default_all_nan(self) -> None:
        score = QualityScore()
        assert math.isnan(score.ovrl)
        assert math.isnan(score.sig)
        assert math.isnan(score.bak)
        assert math.isnan(score.p808)

    def test_default_is_unavailable(self) -> None:
        assert QualityScore().is_available is False

    def test_partial_score_is_available(self) -> None:
        score = QualityScore(ovrl=3.5)
        assert score.is_available is True

    def test_fully_populated_score_is_available(self) -> None:
        score = QualityScore(ovrl=3.5, sig=4.0, bak=3.8, p808=3.6)
        assert score.is_available is True
        assert score.ovrl == 3.5

    def test_immutable(self) -> None:
        score = QualityScore(ovrl=3.5)
        # frozen+slots → assignment raises (FrozenInstanceError or
        # AttributeError depending on Python version).
        with pytest.raises((Exception,)):
            score.ovrl = 4.0  # type: ignore[misc]


# ── NoOpQualityEstimator ────────────────────────────────────────────────


class TestNoOpQualityEstimator:
    def test_implements_protocol(self) -> None:
        est = NoOpQualityEstimator()
        assert isinstance(est, QualityEstimator)

    def test_returns_unavailable_score(self) -> None:
        est = NoOpQualityEstimator()
        result = est.score(
            np.zeros(8000, dtype=np.float32),
            sample_rate=16_000,
        )
        assert isinstance(result, QualityScore)
        assert result.is_available is False

    def test_returns_score_for_any_input(self) -> None:
        # Even garbage input never raises — NoOp's job is to
        # always say "no measurement".
        est = NoOpQualityEstimator()
        for size in (0, 100, 1024, 16_000, 80_000):
            result = est.score(
                np.zeros(size, dtype=np.float32),
                sample_rate=16_000,
            )
            assert math.isnan(result.ovrl)


# ── DnsmosQualityEstimator (extras-gated) ───────────────────────────────


class TestDnsmosLazyImport:
    """Construction MUST raise QualityEstimatorLoadError when
    speechmos isn't installed.

    These tests don't require the actual speechmos package — they
    monkeypatch the loader so the error path is exercised
    deterministically.
    """

    def test_load_error_when_speechmos_missing(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def _raise() -> object:
            raise ImportError("speechmos not installed (simulated)")

        monkeypatch.setattr(
            DnsmosQualityEstimator,
            "_load_dnsmos_module",
            staticmethod(_raise),
        )
        with pytest.raises(QualityEstimatorLoadError, match="speechmos"):
            DnsmosQualityEstimator()

    def test_load_error_message_includes_install_hint(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def _raise() -> object:
            raise ImportError("simulated")

        monkeypatch.setattr(
            DnsmosQualityEstimator,
            "_load_dnsmos_module",
            staticmethod(_raise),
        )
        try:
            DnsmosQualityEstimator()
        except QualityEstimatorLoadError as exc:
            assert "voice-quality" in str(exc)
        else:
            pytest.fail("expected QualityEstimatorLoadError")


class _StubDnsmos:
    """In-memory speechmos.dnsmos lookalike for score-shape tests."""

    @staticmethod
    def run(audio: np.ndarray, *, sr: int) -> dict[str, float]:
        # Return all four sub-scores for shape validation.
        # Production speechmos returns sub-scores in [1, 5].
        _ = audio
        _ = sr
        return {
            "ovrl_mos": 3.5,
            "sig_mos": 4.0,
            "bak_mos": 3.8,
            "p808_mos": 3.6,
        }


class TestDnsmosScoreShape:
    """With a stub dnsmos module wired in, the estimator returns
    a properly-shaped QualityScore."""

    def _build_with_stub(
        self, monkeypatch: pytest.MonkeyPatch, stub: object
    ) -> DnsmosQualityEstimator:
        monkeypatch.setattr(
            DnsmosQualityEstimator,
            "_load_dnsmos_module",
            staticmethod(lambda: stub),
        )
        return DnsmosQualityEstimator()

    def test_score_returns_quality_score(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        est = self._build_with_stub(monkeypatch, _StubDnsmos())
        result = est.score(
            np.zeros(16_000, dtype=np.float32),
            sample_rate=16_000,
        )
        assert isinstance(result, QualityScore)
        assert result.ovrl == 3.5
        assert result.sig == 4.0
        assert result.bak == 3.8
        assert result.p808 == 3.6
        assert result.is_available is True

    def test_score_rejects_non_float32(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        est = self._build_with_stub(monkeypatch, _StubDnsmos())
        with pytest.raises(ValueError, match="float32"):
            est.score(
                np.zeros(16_000, dtype=np.int16),
                sample_rate=16_000,
            )

    def test_score_rejects_zero_sample_rate(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        est = self._build_with_stub(monkeypatch, _StubDnsmos())
        with pytest.raises(ValueError, match="sample_rate"):
            est.score(
                np.zeros(16_000, dtype=np.float32),
                sample_rate=0,
            )

    def test_score_rejects_non_dict_dnsmos_return(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Defence-in-depth: a future speechmos version that
        # changes the return type from dict to NamedTuple should
        # surface loud, not produce silent garbage scores.
        class _BadStub:
            @staticmethod
            def run(audio: np.ndarray, *, sr: int) -> tuple[float, float]:
                _ = audio
                _ = sr
                return (3.5, 4.0)

        est = self._build_with_stub(monkeypatch, _BadStub())
        with pytest.raises(RuntimeError, match="speechmos"):
            est.score(
                np.zeros(16_000, dtype=np.float32),
                sample_rate=16_000,
            )

    def test_score_partial_dict_yields_nan_for_missing(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Defence: future speechmos drops a sub-score → estimator
        # returns NaN for that field instead of crashing.
        class _PartialStub:
            @staticmethod
            def run(audio: np.ndarray, *, sr: int) -> dict[str, float]:
                _ = audio
                _ = sr
                return {"ovrl_mos": 3.5, "sig_mos": 4.0}  # bak + p808 missing

        est = self._build_with_stub(monkeypatch, _PartialStub())
        result = est.score(
            np.zeros(16_000, dtype=np.float32),
            sample_rate=16_000,
        )
        assert result.ovrl == 3.5
        assert result.sig == 4.0
        assert math.isnan(result.bak)
        assert math.isnan(result.p808)


# ── build_quality_estimator factory ─────────────────────────────────────


class TestBuildQualityEstimator:
    def test_disabled_returns_noop(self) -> None:
        est = build_quality_estimator(enabled=False)
        assert isinstance(est, NoOpQualityEstimator)

    def test_engine_off_returns_noop(self) -> None:
        est = build_quality_estimator(enabled=True, engine="off")
        assert isinstance(est, NoOpQualityEstimator)

    def test_disabled_off_returns_noop(self) -> None:
        est = build_quality_estimator(enabled=False, engine="off")
        assert isinstance(est, NoOpQualityEstimator)

    def test_unknown_engine_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="Unknown quality engine"):
            build_quality_estimator(enabled=True, engine="bogus")  # type: ignore[arg-type]

    def test_dnsmos_engine_propagates_load_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # When the operator selects dnsmos but speechmos isn't
        # installed, the factory raises through (not silently
        # returns NoOp). Loud failure is the contract.
        def _raise() -> object:
            raise ImportError("simulated")

        monkeypatch.setattr(
            DnsmosQualityEstimator,
            "_load_dnsmos_module",
            staticmethod(_raise),
        )
        with pytest.raises(QualityEstimatorLoadError):
            build_quality_estimator(enabled=True, engine="dnsmos")
