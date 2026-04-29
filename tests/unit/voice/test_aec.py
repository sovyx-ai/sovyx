"""Tests for :mod:`sovyx.voice._aec` — Phase 4 / T4.1-T4.3 foundation.

Covers:

* :class:`AecConfig` filter-length derivation from sample rate +
  filter-length-ms.
* :func:`build_aec_processor` factory matrix
  (enabled × engine × pyaec-availability).
* :class:`NoOpAec` pass-through contract.
* :class:`SpeexAecProcessor` smoke + input-validation.
* :func:`compute_erle` math under known-ERLE synthetic signals.

Speex AEC is exercised via the real ``pyaec`` package (foundation
ships ``pyaec>=1.0`` in ``[voice]`` extras). Where the test logic
needs to assert behaviour without depending on the bundled native
library, ``_load_pyaec_module`` is monkeypatched at the class level
so the test substitutes a deterministic stub.
"""

from __future__ import annotations

import dataclasses

import numpy as np
import pytest

from sovyx.voice._aec import (
    AecConfig,
    AecLoadError,
    AecProcessor,
    NoOpAec,
    NullRenderProvider,
    RenderPcmProvider,
    SpeexAecProcessor,
    build_aec_processor,
    build_frame_normalizer_aec,
    compute_erle,
)

# ── AecConfig ────────────────────────────────────────────────────────────


class TestAecConfig:
    """Filter-length derivation rules."""

    def test_filter_length_samples_at_16khz_128ms(self) -> None:
        cfg = AecConfig(
            enabled=True,
            engine="speex",
            sample_rate=16000,
            frame_size_samples=160,
            filter_length_ms=128,
        )
        # 16000 * 128 / 1000 = 2048
        assert cfg.filter_length_samples == 2048

    def test_filter_length_samples_at_48khz_64ms(self) -> None:
        cfg = AecConfig(
            enabled=True,
            engine="speex",
            sample_rate=48000,
            frame_size_samples=480,
            filter_length_ms=64,
        )
        # 48000 * 64 / 1000 = 3072
        assert cfg.filter_length_samples == 3072

    def test_filter_length_floor_is_one_sample(self) -> None:
        # Nonsensical (1 ms at 16 kHz = 16 samples) — still positive.
        cfg = AecConfig(
            enabled=True,
            engine="speex",
            sample_rate=16000,
            frame_size_samples=160,
            filter_length_ms=1,
        )
        assert cfg.filter_length_samples == 16


# ── NoOpAec ──────────────────────────────────────────────────────────────


class TestNoOpAec:
    """Pass-through guarantees."""

    def test_implements_aec_processor_protocol(self) -> None:
        aec = NoOpAec()
        assert isinstance(aec, AecProcessor)

    def test_process_returns_input_unchanged(self) -> None:
        aec = NoOpAec()
        capture = np.array([1, -2, 3, -4, 5], dtype=np.int16)
        render = np.array([10, 20, 30, 40, 50], dtype=np.int16)
        out = aec.process(capture, render)
        np.testing.assert_array_equal(out, capture)

    def test_process_returns_same_object_for_zero_copy(self) -> None:
        aec = NoOpAec()
        capture = np.zeros(160, dtype=np.int16)
        render = np.zeros(160, dtype=np.int16)
        # Spec contract: identity preserved so callers can avoid a
        # copy on the hot path when AEC is disabled.
        assert aec.process(capture, render) is capture

    def test_reset_does_not_raise(self) -> None:
        NoOpAec().reset()


# ── SpeexAecProcessor ────────────────────────────────────────────────────


@pytest.fixture()
def speex_config() -> AecConfig:
    return AecConfig(
        enabled=True,
        engine="speex",
        sample_rate=16000,
        frame_size_samples=160,  # 10 ms @ 16 kHz
        filter_length_ms=64,
    )


class TestSpeexAecProcessorConstructor:
    """Construction-time guarantees."""

    def test_rejects_non_speex_engine(self, speex_config: AecConfig) -> None:
        bad = dataclasses.replace(speex_config, engine="off")
        with pytest.raises(ValueError, match="engine='speex'"):
            SpeexAecProcessor(bad)

    def test_constructs_with_real_pyaec(self, speex_config: AecConfig) -> None:
        # Foundation ships pyaec in [voice] extras; exercise the real
        # binding to confirm DLL loads.
        aec = SpeexAecProcessor(speex_config)
        assert isinstance(aec, AecProcessor)

    def test_raises_aec_load_error_when_pyaec_missing(
        self,
        speex_config: AecConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def _raise(*_a: object, **_kw: object) -> object:
            raise ImportError("pyaec not installed (simulated)")

        monkeypatch.setattr(SpeexAecProcessor, "_load_pyaec_module", staticmethod(_raise))
        with pytest.raises(AecLoadError, match="pyaec"):
            SpeexAecProcessor(speex_config)


class TestSpeexAecProcessorProcess:
    """Process-time input validation + smoke behaviour."""

    def test_rejects_non_int16_capture(self, speex_config: AecConfig) -> None:
        aec = SpeexAecProcessor(speex_config)
        capture = np.zeros(160, dtype=np.float32)
        render = np.zeros(160, dtype=np.int16)
        with pytest.raises(ValueError, match="capture dtype"):
            aec.process(capture, render)

    def test_rejects_non_int16_render(self, speex_config: AecConfig) -> None:
        aec = SpeexAecProcessor(speex_config)
        capture = np.zeros(160, dtype=np.int16)
        render = np.zeros(160, dtype=np.float32)
        with pytest.raises(ValueError, match="render dtype"):
            aec.process(capture, render)

    def test_rejects_shape_mismatch(self, speex_config: AecConfig) -> None:
        aec = SpeexAecProcessor(speex_config)
        capture = np.zeros(160, dtype=np.int16)
        render = np.zeros(80, dtype=np.int16)
        with pytest.raises(ValueError, match="shape mismatch"):
            aec.process(capture, render)

    def test_rejects_wrong_frame_size(self, speex_config: AecConfig) -> None:
        aec = SpeexAecProcessor(speex_config)
        # 80 samples instead of configured 160.
        capture = np.zeros(80, dtype=np.int16)
        render = np.zeros(80, dtype=np.int16)
        with pytest.raises(ValueError, match="frame size mismatch"):
            aec.process(capture, render)

    def test_short_circuits_when_render_silent(
        self,
        speex_config: AecConfig,
    ) -> None:
        """Render exact-zero → return capture unchanged (no filter update)."""
        aec = SpeexAecProcessor(speex_config)
        capture = (np.random.default_rng(42).integers(-1000, 1000, 160)).astype(
            np.int16,
        )
        render = np.zeros(160, dtype=np.int16)
        out = aec.process(capture, render)
        np.testing.assert_array_equal(out, capture)

    def test_process_returns_int16_same_length(
        self,
        speex_config: AecConfig,
    ) -> None:
        aec = SpeexAecProcessor(speex_config)
        rng = np.random.default_rng(7)
        capture = rng.integers(-1000, 1000, 160).astype(np.int16)
        render = rng.integers(-1000, 1000, 160).astype(np.int16)
        out = aec.process(capture, render)
        assert out.dtype == np.int16
        assert out.shape == capture.shape

    def test_reset_keeps_processor_callable(
        self,
        speex_config: AecConfig,
    ) -> None:
        aec = SpeexAecProcessor(speex_config)
        rng = np.random.default_rng(1)
        capture = rng.integers(-1000, 1000, 160).astype(np.int16)
        render = rng.integers(-1000, 1000, 160).astype(np.int16)
        aec.process(capture, render)
        aec.reset()
        # Post-reset must still process without raising.
        out = aec.process(capture, render)
        assert out.shape == capture.shape


# ── build_aec_processor factory ──────────────────────────────────────────


class TestBuildAecProcessor:
    """Factory matrix."""

    def _cfg(self, **overrides: object) -> AecConfig:
        base: dict[str, object] = {
            "enabled": True,
            "engine": "speex",
            "sample_rate": 16000,
            "frame_size_samples": 160,
            "filter_length_ms": 64,
        }
        base.update(overrides)
        return AecConfig(**base)  # type: ignore[arg-type]

    def test_disabled_returns_noop(self) -> None:
        aec = build_aec_processor(self._cfg(enabled=False))
        assert isinstance(aec, NoOpAec)

    def test_engine_off_returns_noop(self) -> None:
        aec = build_aec_processor(self._cfg(engine="off"))
        assert isinstance(aec, NoOpAec)

    def test_disabled_off_returns_noop(self) -> None:
        aec = build_aec_processor(self._cfg(enabled=False, engine="off"))
        assert isinstance(aec, NoOpAec)

    def test_enabled_speex_returns_speex(self) -> None:
        aec = build_aec_processor(self._cfg())
        assert isinstance(aec, SpeexAecProcessor)

    def test_enabled_unknown_engine_raises_value_error(self) -> None:
        # Use object.__setattr__ to bypass the frozen dataclass
        # constraint and inject an invalid engine token (simulating a
        # future regression where a config validator missed a value).
        cfg = self._cfg()
        object.__setattr__(cfg, "engine", "bogus")  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="Unknown AEC engine"):
            build_aec_processor(cfg)


# ── compute_erle math ────────────────────────────────────────────────────


class TestComputeErle:
    """ERLE under synthetic signals with known-correct dB values."""

    def _frame(self, rng: np.random.Generator, scale: float, n: int = 160) -> np.ndarray:
        return (rng.standard_normal(n) * scale).astype(np.int16)

    def test_perfect_cancellation_caps_at_120_db(self) -> None:
        rng = np.random.default_rng(0)
        render = self._frame(rng, 1000.0)
        original = self._frame(rng, 1000.0)
        cleaned = np.zeros_like(original)
        erle = compute_erle(render, original, cleaned)
        assert erle == 120.0

    def test_no_cancellation_returns_zero_db(self) -> None:
        rng = np.random.default_rng(1)
        render = self._frame(rng, 1000.0)
        original = self._frame(rng, 1000.0)
        cleaned = original.copy()
        erle = compute_erle(render, original, cleaned)
        # Same signal in/out → ratio = 1 → 10*log10(1) = 0 dB.
        assert abs(erle) < 1e-9

    def test_half_amplitude_residual_yields_about_6_db(self) -> None:
        # cleaned = 0.5 * original → power ratio 4 → ERLE ≈ 6.02 dB.
        rng = np.random.default_rng(2)
        render = self._frame(rng, 1000.0)
        original = self._frame(rng, 1000.0)
        cleaned = (original.astype(np.float64) * 0.5).astype(np.int16)
        erle = compute_erle(render, original, cleaned)
        assert 5.5 < erle < 6.5

    def test_quarter_amplitude_residual_yields_about_12_db(self) -> None:
        # cleaned = 0.25 * original → power ratio 16 → ERLE ≈ 12.04 dB.
        rng = np.random.default_rng(3)
        render = self._frame(rng, 1000.0)
        original = self._frame(rng, 1000.0)
        cleaned = (original.astype(np.float64) * 0.25).astype(np.int16)
        erle = compute_erle(render, original, cleaned)
        assert 11.5 < erle < 12.5

    def test_silent_render_returns_zero_db(self) -> None:
        # No echo to cancel → ERLE undefined → 0.0 by contract.
        rng = np.random.default_rng(4)
        render = np.zeros(160, dtype=np.int16)
        original = self._frame(rng, 1000.0)
        cleaned = self._frame(rng, 100.0)
        assert compute_erle(render, original, cleaned) == 0.0

    def test_silent_original_returns_zero_db(self) -> None:
        # Capture was already silent → no echo to remove → 0.0.
        rng = np.random.default_rng(5)
        render = self._frame(rng, 1000.0)
        original = np.zeros(160, dtype=np.int16)
        cleaned = np.zeros(160, dtype=np.int16)
        assert compute_erle(render, original, cleaned) == 0.0

    def test_shape_mismatch_capture_cleaned(self) -> None:
        render = np.zeros(160, dtype=np.int16)
        original = np.zeros(160, dtype=np.int16)
        cleaned = np.zeros(80, dtype=np.int16)
        with pytest.raises(ValueError, match="original/cleaned"):
            compute_erle(render, original, cleaned)

    def test_shape_mismatch_render_capture(self) -> None:
        render = np.zeros(80, dtype=np.int16)
        original = np.zeros(160, dtype=np.int16)
        cleaned = np.zeros(160, dtype=np.int16)
        with pytest.raises(ValueError, match="render/capture"):
            compute_erle(render, original, cleaned)


# ── T4.4 — RenderPcmProvider + NullRenderProvider ────────────────────────


class TestNullRenderProvider:
    """No-op provider returns zeros at the requested length."""

    def test_implements_protocol(self) -> None:
        provider = NullRenderProvider()
        assert isinstance(provider, RenderPcmProvider)

    def test_returns_zeros_at_requested_length(self) -> None:
        provider = NullRenderProvider()
        out = provider.get_aligned_window(512)
        assert out.shape == (512,)
        assert out.dtype == np.int16
        assert np.all(out == 0)

    def test_returns_independent_buffer_per_call(self) -> None:
        provider = NullRenderProvider()
        a = provider.get_aligned_window(160)
        b = provider.get_aligned_window(160)
        # No aliasing — caller can mutate one without affecting future.
        a[0] = 12345
        assert b[0] == 0


# ── T4.4 — build_frame_normalizer_aec helper ─────────────────────────────


class TestBuildFrameNormalizerAec:
    """The 16 kHz / 512-sample factory pins the FrameNormalizer invariants."""

    def test_disabled_returns_noop(self) -> None:
        aec = build_frame_normalizer_aec(
            enabled=False,
            engine="speex",
            filter_length_ms=128,
        )
        assert isinstance(aec, NoOpAec)

    def test_engine_off_returns_noop(self) -> None:
        aec = build_frame_normalizer_aec(
            enabled=True,
            engine="off",
            filter_length_ms=128,
        )
        assert isinstance(aec, NoOpAec)

    def test_enabled_speex_returns_speex(self) -> None:
        aec = build_frame_normalizer_aec(
            enabled=True,
            engine="speex",
            filter_length_ms=64,
        )
        assert isinstance(aec, SpeexAecProcessor)
        # Verify the FrameNormalizer-mandated frame_size = 512.
        cap = np.zeros(512, dtype=np.int16)
        ref = np.zeros(512, dtype=np.int16)
        out = aec.process(cap, ref)
        assert out.shape == (512,)
