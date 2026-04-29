"""Acoustic Echo Cancellation foundation (Phase 4 / T4.1-T4.3).

Foundation module for in-process AEC. Provides an abstract
:class:`AecProcessor` Protocol with two concrete implementations:

* :class:`NoOpAec` — pass-through, no-op. Used when
  ``EngineConfig.tuning.voice.voice_aec_enabled=False`` (the
  foundation default per ``feedback_staged_adoption``).
* :class:`SpeexAecProcessor` — Speex Echo Canceller via the
  ``pyaec`` package. NLMS-based MDF adaptive filter; typical
  production ERLE 20-25 dB. Below the Phase 4 promotion gate of
  30 dB but cross-platform shippable today.

The mission spec's preferred ``webrtc-audio-processing`` PyPI
binding does not ship Windows wheels and fails to build from source
under MSVC. Until a custom ctypes shim around ``webrtc-aec3`` is
built (planned for v0.28.0+), Speex is the foundation engine.

This module is **synchronous** — :meth:`AecProcessor.process` is
called from the audio thread inside :class:`FrameNormalizer`, which
is itself sync. Per CLAUDE.md anti-pattern #14, async callers MUST
wrap the call site in :func:`asyncio.to_thread` if invoking from a
coroutine. Foundation phase ships the abstraction only — the
:mod:`sovyx.voice._frame_normalizer` wire-up lands in T4.4 (a
separate commit).

Foundation phase scope (this commit, T4.1-T4.3 only):

* :class:`AecProcessor` Protocol — interface contract.
* :class:`AecConfig` — tuning knobs.
* :class:`NoOpAec` — engine="off" / disabled-default fallback.
* :class:`SpeexAecProcessor` — engine="speex" concrete.
* :func:`compute_erle` — pure-DSP ERLE measurement (T4.3).
* :func:`build_aec_processor` — factory keyed by config.

Out of scope (later commits per ``feedback_staged_adoption``):

* T4.4 wire-up into :mod:`sovyx.voice._frame_normalizer`.
* T4.5 default-flag flip planning (foundation default stays False).
* T4.6 bypass-detection auto-engage when WASAPI exclusive bypasses
  the OS AEC.
* T4.7 ERLE histogram metric.
* T4.8 ``voice.aec.engaged`` counter.
* T4.9 double-talk detector.
* T4.10 cross-platform integration tests with real render+capture.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

import numpy as np

from sovyx.observability.logging import get_logger

logger = get_logger(__name__)


# ── Configuration ────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class AecConfig:
    """Immutable AEC tuning snapshot derived from
    :class:`sovyx.engine.config.VoiceTuningConfig`.

    Constructed once per pipeline lifetime; not mutated at runtime.
    Operators rebuild the processor (via :func:`build_aec_processor`)
    after a config reload.
    """

    enabled: bool
    engine: Literal["off", "speex"]
    sample_rate: int
    frame_size_samples: int
    filter_length_ms: int

    @property
    def filter_length_samples(self) -> int:
        """Filter tap count derived from ``filter_length_ms``.

        Speex AEC documentation recommends powers of two. We compute
        ``round(rate * length_ms / 1000)`` and let the C side handle
        rounding to the next power of two internally.
        """
        return max(1, round(self.sample_rate * self.filter_length_ms / 1000))


# ── Protocol ─────────────────────────────────────────────────────────────


@runtime_checkable
class RenderPcmProvider(Protocol):
    """Source of render-side reference PCM for AEC.

    The capture-side pipeline is the consumer; the playback path is
    the producer. Concrete implementations buffer recent TTS render
    PCM and return the slice that aligns in time with the current
    capture window.

    Foundation phase (T4.4) ships only the interface + the no-op
    fallback :class:`NullRenderProvider`. The concrete buffer that
    captures TTS playback PCM and timestamps it for capture
    alignment is staged for T4.4.b (render-PCM capture
    infrastructure in :mod:`sovyx.voice.pipeline._output_queue`).
    Until then the wire-up always sees zeros render → AEC
    short-circuits to passthrough. Wiring the abstraction NOW pins
    the contract so the future provider doesn't reshape the
    FrameNormalizer surface.
    """

    def get_aligned_window(self, n_samples: int) -> np.ndarray:
        """Return ``n_samples`` int16 PCM aligned with the next capture window.

        Args:
            n_samples: Number of samples requested. Matches the
                capture window size (typically 512 = 32 ms @ 16 kHz).

        Returns:
            int16 ndarray of length ``n_samples``. Implementations
            MUST return zeros when no render data is currently being
            played back (TTS idle); short-circuit logic in the AEC
            relies on the all-zero signature to skip filter updates.
        """
        ...


class NullRenderProvider:
    """No-op render provider — always returns zeros.

    Used as the foundation default until the T4.4.b render-PCM
    capture infrastructure lands. Returning zeros means AEC sees no
    echo to cancel and short-circuits to passthrough; safe to wire
    everywhere even before the playback path produces real reference
    PCM.
    """

    def get_aligned_window(self, n_samples: int) -> np.ndarray:
        return np.zeros(n_samples, dtype=np.int16)


@runtime_checkable
class RenderPcmSink(Protocol):
    """Write-side counterpart to :class:`RenderPcmProvider`.

    The TTS playback path (:mod:`sovyx.voice.pipeline._output_queue`)
    is the producer; it calls :meth:`feed` immediately before the
    playback dispatch so the buffered render PCM aligns with what
    the speaker is about to emit. The capture path then reads via
    :meth:`RenderPcmProvider.get_aligned_window`.

    Concrete implementation: :class:`sovyx.voice._render_pcm_buffer.RenderPcmBuffer`
    implements both :class:`RenderPcmProvider` and
    :class:`RenderPcmSink` so a single buffer instance bridges the
    producer/consumer pair. Tests can mock either side
    independently via the Protocol contracts.
    """

    def feed(self, pcm: np.ndarray, sample_rate: int) -> None:
        """Append the next chunk of render PCM to the buffer.

        Args:
            pcm: Source PCM array. Mono ``(N,)`` or stereo
                ``(N, C)``. Implementation downmixes + resamples to
                the FrameNormalizer's 16 kHz mono invariant.
            sample_rate: Source rate in Hz.
        """
        ...


@runtime_checkable
class AecProcessor(Protocol):
    """Minimal AEC interface.

    Implementations process one PCM frame at a time. Both inputs are
    int16 PCM at the configured ``sample_rate``; the lengths must
    match the configured ``frame_size_samples``. The implementation
    is responsible for any internal buffering / state.

    Stateful contract: each :meth:`process` call updates the internal
    adaptive filter using both the capture and render signals.
    Callers MUST feed the render signal aligned in time with the
    capture signal — render-to-capture skew larger than half the
    filter length destroys convergence. The wire-up in T4.4 handles
    alignment.
    """

    def process(
        self,
        capture: np.ndarray,
        render: np.ndarray,
    ) -> np.ndarray:
        """Cancel the echo of ``render`` from ``capture``.

        Args:
            capture: Mic-side int16 PCM, length == frame_size_samples.
            render: Far-end (TTS playback) int16 PCM, same length.
                When the render side is silent, callers may pass a
                zeros array — the implementation should detect that
                case and short-circuit (no useful filter update when
                the reference is silent).

        Returns:
            Cleaned capture int16 PCM. Same length as ``capture``.
            For :class:`NoOpAec` this is ``capture`` verbatim.
        """
        ...

    def reset(self) -> None:
        """Reset the internal adaptive filter state.

        Called when the audio path is invalidated (device change,
        explicit pipeline restart) so a stale filter doesn't poison
        the next session.
        """
        ...


# ── Concrete: no-op (engine="off") ───────────────────────────────────────


class NoOpAec:
    """Pass-through AEC for ``engine="off"`` / disabled foundation.

    Kept as an explicit class (rather than ``None``) so wire-up
    sites can call :meth:`process` unconditionally without a None
    check on every audio frame. The cost is one Python attribute
    lookup + return; the GIL-released NumPy view return makes it
    free in practice.
    """

    def process(
        self,
        capture: np.ndarray,
        render: np.ndarray,  # noqa: ARG002 — interface contract
    ) -> np.ndarray:
        """Return ``capture`` unchanged."""
        return capture

    def reset(self) -> None:
        """No state — no-op."""


# ── Concrete: Speex Echo Canceller (engine="speex") ──────────────────────


class SpeexAecProcessor:
    """AEC via the ``pyaec`` Speex Echo Canceller binding.

    ``pyaec`` ships a bundled native library (``aec.dll`` on Windows,
    ``libaec.so`` on Linux, ``libaec.dylib`` on macOS) so install is
    pure-pip without a build environment. The wrapper is a ctypes
    shim; the C side runs the Speex MDF (Multi-Delay block Frequency)
    adaptive filter. Production ERLE typically 20-25 dB.

    Lazy-imports ``pyaec`` so non-AEC daemons don't pay the load
    cost. :class:`AecLoadError` is raised when ``pyaec`` is missing
    AND :class:`SpeexAecProcessor` was explicitly selected — silent
    fallback to :class:`NoOpAec` would mask a configuration mistake.
    """

    def __init__(self, config: AecConfig) -> None:
        if config.engine != "speex":
            raise ValueError(
                f"SpeexAecProcessor requires engine='speex', got {config.engine!r}",
            )
        self._config = config
        self._aec = self._load_pyaec()

    @staticmethod
    def _load_pyaec_module() -> object:
        """Lazy-import ``pyaec``.

        Separate method so tests can monkeypatch the import without
        having to manipulate ``sys.modules``. Raising ``ImportError``
        here is the contract — the caller (``_load_pyaec``) translates
        it into :class:`AecLoadError` so the public surface speaks the
        AEC-domain error type.
        """
        import pyaec  # type: ignore[import-untyped]  # noqa: PLC0415 — lazy by design; pyaec ships no py.typed marker

        return pyaec

    def _load_pyaec(self) -> object:
        """Construct the underlying ``pyaec.Aec`` instance."""
        try:
            pyaec_mod = self._load_pyaec_module()
        except ImportError as exc:
            raise AecLoadError(
                "Speex AEC requires the 'pyaec' package — install via "
                "'pip install sovyx[voice]' or set "
                "voice_aec_engine='off' to disable.",
            ) from exc
        return pyaec_mod.Aec(  # type: ignore[attr-defined]
            frame_size=self._config.frame_size_samples,
            filter_length=self._config.filter_length_samples,
            sample_rate=self._config.sample_rate,
            enable_preprocess=True,
        )

    def process(
        self,
        capture: np.ndarray,
        render: np.ndarray,
    ) -> np.ndarray:
        """Run one frame through the Speex echo canceller.

        Validates input shape + dtype; returns int16 ndarray matching
        ``capture`` length. When the render reference is exact-zero
        (no playback active), short-circuits to capture passthrough —
        Speex's filter still updates on a zeros reference but the
        update has no useful learning content and slightly drifts
        the converged filter.
        """
        if capture.dtype != np.int16:
            raise ValueError(f"capture dtype must be int16, got {capture.dtype}")
        if render.dtype != np.int16:
            raise ValueError(f"render dtype must be int16, got {render.dtype}")
        if capture.shape != render.shape:
            raise ValueError(
                f"capture/render shape mismatch: {capture.shape} vs {render.shape}",
            )
        if capture.size != self._config.frame_size_samples:
            raise ValueError(
                f"frame size mismatch: got {capture.size}, expected "
                f"{self._config.frame_size_samples}",
            )

        if not np.any(render):
            return capture

        cleaned_bytes = self._aec.cancel_echo(  # type: ignore[attr-defined]
            capture.tobytes(),
            render.tobytes(),
        )
        # ``pyaec.cancel_echo`` returns a Python list whose entries
        # are *signed* int8 bytes (one list element per byte of the
        # int16 little-endian PCM frame, in range -128..127). Direct
        # ``bytes(cleaned_bytes)`` would raise on negative entries.
        # Reinterpret via NumPy: treat the list as ``int8`` then view
        # as ``int16`` to recover the cancelled PCM samples.
        cleaned_int8 = np.asarray(cleaned_bytes, dtype=np.int8).tobytes()
        return np.frombuffer(cleaned_int8, dtype=np.int16).copy()

    def reset(self) -> None:
        """Reconstruct the underlying canceller to clear filter state.

        ``pyaec`` does not expose an explicit ``reset()`` on the
        Speex C handle, so we rebuild the wrapper. The cost is one
        DLL allocation + filter-tap zero-fill; sub-millisecond.
        """
        self._aec = self._load_pyaec()


class AecLoadError(RuntimeError):
    """Raised when an AEC engine cannot be loaded.

    Concrete trigger today: ``engine="speex"`` selected but the
    ``pyaec`` package is not installed. Caller should either install
    the ``[voice]`` extras or downgrade to ``engine="off"``.
    """


# ── Factory ──────────────────────────────────────────────────────────────


def build_aec_processor(config: AecConfig) -> AecProcessor:
    """Construct the concrete AEC processor for the given config.

    The matrix:

    * ``enabled=False`` OR ``engine="off"`` → :class:`NoOpAec`
    * ``enabled=True`` AND ``engine="speex"`` → :class:`SpeexAecProcessor`

    Raises :class:`AecLoadError` when an enabled engine cannot load
    (e.g. ``engine="speex"`` but ``pyaec`` not installed). The
    foundation pipeline contract is "AEC must always have a
    processor"; refusing to start is louder than silently
    pass-through-ing on a misconfigured production daemon.
    """
    if not config.enabled or config.engine == "off":
        return NoOpAec()
    if config.engine == "speex":
        return SpeexAecProcessor(config)
    raise ValueError(f"Unknown AEC engine: {config.engine!r}")


# ── ERLE measurement (T4.3) ─────────────────────────────────────────────


def compute_erle(
    render: np.ndarray,
    original_capture: np.ndarray,
    cleaned_capture: np.ndarray,
) -> float:
    """Compute Echo Return Loss Enhancement in dB.

    ERLE measures how much echo energy the canceller removed from the
    capture path. The reference definition (ITU-T G.168 §6.2):

        ERLE = 10 · log10(P_y / P_e)

    where ``P_y`` is the power of the original capture (mic) signal
    and ``P_e`` is the power of the cleaned capture (residual after
    AEC). Both signals are observed during the same frame window.
    The render signal is a sanity gate — when render energy is below
    the silence floor we cannot meaningfully measure ERLE because
    there was no echo to cancel; the function returns 0.0 dB in that
    case.

    Production targets (master mission §Phase 4 promotion gate):

    * AEC ERLE ≥ 30 dB sustained (Q-SYS pro-grade).
    * Acceptable threshold ≥ 25 dB warn.
    * Below 25 dB → AEC needs investigation (filter divergence,
      misalignment, double-talk over-suppression).

    Args:
        render: Far-end int16 PCM (TTS playback) for the same frame
            window. Used as the silence-gate reference; the function
            returns ``0.0`` when render is below the silence floor
            (no echo was present, ERLE is undefined).
        original_capture: Mic-side int16 PCM BEFORE AEC.
        cleaned_capture: Mic-side int16 PCM AFTER AEC. Must be the
            same length as ``original_capture``.

    Returns:
        ERLE in dB (positive = echo reduced; 0.0 dB = no improvement
        OR no echo present). Capped at +120 dB to avoid log(0)
        explosions when the canceller produces an exact-zero residual
        (rare with real hardware but possible in synthetic tests).

    Raises:
        ValueError: shape / dtype mismatch.
    """
    if original_capture.shape != cleaned_capture.shape:
        raise ValueError(
            f"original/cleaned shape mismatch: {original_capture.shape} "
            f"vs {cleaned_capture.shape}",
        )
    if render.shape != original_capture.shape:
        raise ValueError(
            f"render/capture shape mismatch: {render.shape} vs {original_capture.shape}",
        )

    render_power = float(np.mean(np.square(render.astype(np.float64))))
    silence_floor = 1.0  # corresponds to ~0 dBFS reference noise on int16
    if render_power < silence_floor:
        return 0.0

    original_power = float(np.mean(np.square(original_capture.astype(np.float64))))
    cleaned_power = float(np.mean(np.square(cleaned_capture.astype(np.float64))))

    # Order matters: silent original means there was no echo to
    # measure, so ERLE is undefined → 0.0 dB. We must check this
    # BEFORE the silent-cleaned branch, otherwise a dual-silent
    # frame would falsely report perfect cancellation.
    if original_power < silence_floor:
        return 0.0
    if cleaned_power < silence_floor:
        return 120.0

    return 10.0 * float(np.log10(original_power / cleaned_power))


def build_frame_normalizer_aec(
    *,
    enabled: bool,
    engine: Literal["off", "speex"],
    filter_length_ms: int,
) -> AecProcessor:
    """Build an :class:`AecProcessor` configured for the FrameNormalizer.

    Convenience over :func:`build_aec_processor` — pins the
    sample-rate + frame-size to the FrameNormalizer invariants
    (16 kHz, 512-sample windows) so call sites only forward the
    operator-tunable knobs from :class:`VoiceTuningConfig`.

    Args:
        enabled: Master switch (mirrors ``voice_aec_enabled``).
        engine: Concrete engine selector
            (mirrors ``voice_aec_engine``).
        filter_length_ms: Adaptive filter length in ms
            (mirrors ``voice_aec_filter_length_ms``).
    """
    config = AecConfig(
        enabled=enabled,
        engine=engine,
        sample_rate=16_000,
        frame_size_samples=512,
        filter_length_ms=filter_length_ms,
    )
    return build_aec_processor(config)


__all__ = [
    "AecConfig",
    "AecLoadError",
    "AecProcessor",
    "NoOpAec",
    "NullRenderProvider",
    "RenderPcmProvider",
    "RenderPcmSink",
    "SpeexAecProcessor",
    "build_aec_processor",
    "build_frame_normalizer_aec",
    "compute_erle",
]
