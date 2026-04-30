"""Voice quality metrics — DNSMOS / PESQ foundation (Phase 4 / T4.21).

Foundation module for perceptual voice-quality measurement.
Mirrors the AEC / NS / SNR foundation pattern: abstract Protocol +
NoOp default + a real implementation gated behind an opt-in
extras_require. The default ships with zero new dependencies.

Engine choice rationale (verified 2026-04-29 per
feedback_no_speculation):

The mission spec's preferred ``dnsmos`` Python package + ``pesq``
binding were investigated and rejected for v0.27.0 default
shipment:

* ``dnsmos`` — does not exist on PyPI under that name.
* ``pesq==0.0.4`` — fails to build on Windows MSVC ("Unable to
  find a compatible Visual Studio installation").
* ``pypesq`` — fails build with ``NUMPY_SETUP`` error.
* ``pesq-binding`` — does not exist on PyPI.
* ``speechmos==0.0.1.1`` — INSTALLS cleanly on Windows
  (ships pre-built ONNX models for DNSMOS / AEC-MOS / PLC-MOS),
  but transitively requires ``librosa`` which pulls in ``numba``
  + ``llvmlite`` + ``scikit-learn`` (~ 100 MB total) AND the
  ``numba`` _internal.dll is blocked by Windows Application
  Control on locked-down systems.

Conclusion: shipping these as a default dependency would
disproportionately bloat every voice install AND risk DLL-load
failures on enterprise Windows endpoints. The same enterprise
calculus that rejected ``pyrnnoise`` (T4.11) applies here.

Foundation engine: opt-in heavy DSP via ``[voice-quality]``
extras_require. The default ``QualityEstimator`` is the
:class:`NoOpQualityEstimator` which returns NaN for every
measurement — a structured "not available" signal that the
dashboard can render as "—" without crashing on missing data.

Operators who want real DNSMOS / PESQ install the extras::

    pip install sovyx[voice-quality]

The :class:`DnsmosQualityEstimator` lazy-imports ``speechmos``
and raises a clear :class:`QualityEstimatorLoadError` if the
operator selected the engine but didn't install the extras.

Foundation phase scope (T4.21, this commit):

* :class:`QualityEstimator` Protocol — minimal interface.
* :class:`QualityScore` immutable result dataclass.
* :class:`NoOpQualityEstimator` — returns NaN, no deps.
* :class:`DnsmosQualityEstimator` — lazy speechmos.
* :func:`build_quality_estimator` — factory keyed by engine name.
* :class:`QualityEstimatorLoadError` — structured "extras
  missing" error.

Out of scope (later commits):

* T4.22 — :func:`compute_pesq(reference, degraded)` for
  reference-based PESQ scoring (test-corpus only).
* T4.23 — wire DNSMOS into capture path; per-5-s window
  ``voice.audio.dnsmos_score`` histogram emission.
* T4.24 — synthetic test-corpus PESQ for CI gates.
* T4.25 — per-session DNSMOS p50/p95 in heartbeat event.
* T4.26 — dashboard quality MOS panel.
* T4.27 — alert when p95 < 3.5 (Skype/Zoom acceptable).
* T4.28 — alert when p95 < 3.0 (Skype/Zoom poor).
* T4.29 — DNSMOS bucketing (poor / acceptable / good /
  excellent).
* T4.30 — per-endpoint DNSMOS trending.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

import numpy as np

from sovyx.observability.logging import get_logger

logger = get_logger(__name__)


_NOT_AVAILABLE = float("nan")
"""NaN signals "no measurement available" downstream."""


@dataclass(frozen=True, slots=True)
class QualityScore:
    """Immutable per-call quality result.

    DNSMOS returns 4 sub-scores; PESQ returns a single MOS-LQO
    value. The unified shape pins all four fields so callers
    don't have to switch on the engine. NaN-valued fields
    (default) signal "engine didn't measure this dimension".

    Attributes:
        ovrl: Overall MOS in ``[1.0, 5.0]`` (DNSMOS OVRL +
            PESQ MOS-LQO map here).
        sig: Speech-quality MOS in ``[1.0, 5.0]`` (DNSMOS SIG;
            PESQ doesn't break this out — leave NaN).
        bak: Background-noise quality MOS in ``[1.0, 5.0]``
            (DNSMOS BAK; PESQ doesn't break this out).
        p808: P.808-trained MOS in ``[1.0, 5.0]`` (DNSMOS P808;
            PESQ doesn't compute this).
    """

    ovrl: float = _NOT_AVAILABLE
    sig: float = _NOT_AVAILABLE
    bak: float = _NOT_AVAILABLE
    p808: float = _NOT_AVAILABLE

    @property
    def is_available(self) -> bool:
        """``True`` when at least one sub-score is a real number."""
        return any(not math.isnan(v) for v in (self.ovrl, self.sig, self.bak, self.p808))


@runtime_checkable
class QualityEstimator(Protocol):
    """Minimal voice-quality estimator interface.

    Implementations score one PCM frame at a time. Input is
    ``float32`` (NOT int16) in ``[-1, 1]`` at the configured
    ``sample_rate``. The frame should typically be 5-10 s long
    — DNSMOS is calibrated for those window sizes; shorter
    windows produce noisier estimates.
    """

    def score(self, frame: np.ndarray, *, sample_rate: int) -> QualityScore:
        """Return the quality score for ``frame``.

        Args:
            frame: ``float32`` PCM in ``[-1, 1]``. Length should
                cover at least a few seconds of audio for
                statistically meaningful scores.
            sample_rate: Audio sample rate in Hz.

        Returns:
            :class:`QualityScore`. The :class:`NoOpQualityEstimator`
            returns ``QualityScore()`` (all NaN); concrete engines
            populate the fields they support.
        """
        ...


# ── Concrete: no-op (default — zero deps) ────────────────────────────────


class NoOpQualityEstimator:
    """Pass-through quality estimator — always returns NaN scores.

    Foundation default. Operators who want real DNSMOS / PESQ
    install ``[voice-quality]`` extras and wire
    :class:`DnsmosQualityEstimator` via the
    :func:`build_quality_estimator` factory.
    """

    def score(
        self,
        frame: np.ndarray,  # noqa: ARG002 — interface contract
        *,
        sample_rate: int,  # noqa: ARG002 — interface contract
    ) -> QualityScore:
        return QualityScore()


# ── Concrete: DNSMOS via speechmos (opt-in extras) ───────────────────────


class DnsmosQualityEstimator:
    """DNSMOS scorer via the ``speechmos`` package (opt-in extras).

    Lazy-imports ``speechmos`` so non-quality daemons don't pay
    the ~100 MB librosa transitive dep. When the package is
    missing, raises :class:`QualityEstimatorLoadError` with a
    clear install hint.

    The DNSMOS model expects 16 kHz audio in ``[-1, 1]`` float;
    inputs at other sample rates are passed through unchanged
    (the speechmos wrapper handles internal resampling).
    """

    def __init__(self) -> None:
        self._dnsmos = self._load_dnsmos()

    @staticmethod
    def _load_dnsmos_module() -> object:
        """Lazy-import ``speechmos.dnsmos``.

        Separate method so tests can monkeypatch the import without
        manipulating ``sys.modules``. Raising ``ImportError`` here
        is the contract — the caller (``_load_dnsmos``) translates
        it into :class:`QualityEstimatorLoadError`.
        """
        # ``speechmos`` lives in the opt-in [voice-quality] extras
        # so mypy's strict-mode default sync doesn't see it. The
        # ``import-not-found`` ignore is canonical for runtime-only
        # deps; the lazy import keeps the librosa + numba
        # transitive load off the default voice path.
        from speechmos import dnsmos  # type: ignore[import-not-found]  # noqa: PLC0415

        return dnsmos

    def _load_dnsmos(self) -> object:
        """Resolve the ``speechmos.dnsmos`` module or raise."""
        try:
            return self._load_dnsmos_module()
        except ImportError as exc:
            raise QualityEstimatorLoadError(
                "DNSMOS quality estimator requires the 'speechmos' "
                "package — install via 'pip install sovyx[voice-quality]' "
                "or set voice_quality_engine='off' to disable.",
            ) from exc

    def score(self, frame: np.ndarray, *, sample_rate: int) -> QualityScore:
        """Run DNSMOS on ``frame`` and return the 4 sub-scores."""
        if frame.dtype != np.float32:
            msg = f"frame dtype must be float32, got {frame.dtype}"
            raise ValueError(msg)
        if sample_rate <= 0:
            msg = f"sample_rate must be positive, got {sample_rate}"
            raise ValueError(msg)
        # The speechmos wrapper exposes ``run(audio, sr=...)`` per
        # the v0.0.1.1 surface verified 2026-04-29.
        result = self._dnsmos.run(frame, sr=sample_rate)  # type: ignore[attr-defined]
        # speechmos returns a dict with OVRL / SIG / BAK / P808
        # keys; coerce to QualityScore (NaN-safe for missing keys
        # in case future versions rename).
        if not isinstance(result, dict):
            msg = f"speechmos.dnsmos.run returned {type(result).__name__}, expected dict"
            raise RuntimeError(msg)
        return QualityScore(
            ovrl=float(result.get("ovrl_mos", _NOT_AVAILABLE)),
            sig=float(result.get("sig_mos", _NOT_AVAILABLE)),
            bak=float(result.get("bak_mos", _NOT_AVAILABLE)),
            p808=float(result.get("p808_mos", _NOT_AVAILABLE)),
        )


class QualityEstimatorLoadError(RuntimeError):
    """Raised when a quality engine cannot be loaded.

    Concrete trigger today: ``engine="dnsmos"`` selected but the
    ``speechmos`` extras are not installed. Caller should either
    install ``[voice-quality]`` or downgrade to ``engine="off"``.
    """


# ── Factory ──────────────────────────────────────────────────────────────


def build_quality_estimator(
    *,
    enabled: bool,
    engine: Literal["off", "dnsmos"] = "off",
) -> QualityEstimator:
    """Construct the concrete quality estimator for the given config.

    Matrix:

    * ``enabled=False`` OR ``engine="off"`` →
      :class:`NoOpQualityEstimator`
    * ``enabled=True`` AND ``engine="dnsmos"`` →
      :class:`DnsmosQualityEstimator` (raises
      :class:`QualityEstimatorLoadError` if speechmos missing)

    Raises :class:`ValueError` for unknown engines so a future
    refactor that adds an engine without updating the factory
    fails loudly.
    """
    if not enabled or engine == "off":
        return NoOpQualityEstimator()
    if engine == "dnsmos":
        return DnsmosQualityEstimator()
    raise ValueError(f"Unknown quality engine: {engine!r}")


__all__ = [
    "DnsmosQualityEstimator",
    "NoOpQualityEstimator",
    "QualityEstimator",
    "QualityEstimatorLoadError",
    "QualityScore",
    "build_quality_estimator",
]
