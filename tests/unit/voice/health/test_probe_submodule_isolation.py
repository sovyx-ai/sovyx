"""Isolated tests for the probe submodule split (Phase 6 / T6.23).

Pre-T6.23 the probe submodule split (T05 / godfile-splits mission)
shipped with most coverage going through the top-level ``probe()``
entry point in ``test_probe.py``. The split helpers had spotty
direct coverage:

* ``_classifier.py`` helpers (``_linear_to_db``, ``_format_scale``,
  ``_warmup_samples``) — exercised transitively via ``_compute_rms_db``
  / ``_diagnose_cold`` paths but never pinned in isolation.
* ``_warm.py`` (``_diagnose_warm``, ``_analyse_rms``, ``_analyse_vad``)
  — ZERO direct tests. The full warm-path classification table
  (NO_SIGNAL / LOW_SIGNAL / HEALTHY / APO_DEGRADED / VAD_INSENSITIVE)
  was only exercised end-to-end.
* ``_dispatch.py`` helpers (``_combo_tag``,
  ``_build_probe_wasapi_settings``, ``_load_sounddevice``) — also
  zero direct coverage.

T6.26 already pinned ``_compute_rms_db`` + ``_classify_open_error``
property invariants. T6.23 closes the example-based gaps for
helpers + ``_diagnose_warm`` so refactors of any single split-out
file fail loudly instead of silently shifting behaviour.

NOT covered here per ``feedback_no_speculation`` (already covered):

* ``_diagnose_cold`` — extensive Furo W-1 coverage in
  ``test_probe.py`` (~10 cases).
* ``_compute_rms_db`` — property tests in
  ``test_voice_probe_invariants.py``.
* ``_classify_open_error`` — property tests in
  ``test_voice_probe_invariants.py``.
* ``_open_input_stream`` / ``_run_probe`` / ``probe`` — end-to-end
  tests in ``test_probe.py``.
"""

from __future__ import annotations

import math
from typing import Any
from unittest.mock import MagicMock

import numpy as np
import numpy.typing as npt
import pytest

from sovyx.voice.health.contract import Combo, Diagnosis
from sovyx.voice.health.probe._classifier import (
    _RMS_DB_LOW_SIGNAL_CEILING,
    _RMS_DB_NO_SIGNAL_CEILING,
    _TARGET_PIPELINE_WINDOW,
    _VAD_APO_DEGRADED_CEILING,
    _VAD_HEALTHY_FLOOR,
    _format_scale,
    _linear_to_db,
    _warmup_samples,
)
from sovyx.voice.health.probe._dispatch import (
    _build_probe_wasapi_settings,
    _combo_tag,
    _load_sounddevice,
)
from sovyx.voice.health.probe._warm import (
    _analyse_rms,
    _analyse_vad,
    _default_frame_normalizer_factory,
    _diagnose_warm,
)


def _combo(
    *,
    host_api: str = "WASAPI",
    sample_rate: int = 48_000,
    channels: int = 1,
    sample_format: str = "int16",
    exclusive: bool = False,
    auto_convert: bool = False,
    frames_per_buffer: int = 480,
) -> Combo:
    return Combo(
        host_api=host_api,
        sample_rate=sample_rate,
        channels=channels,
        sample_format=sample_format,
        exclusive=exclusive,
        auto_convert=auto_convert,
        frames_per_buffer=frames_per_buffer,
    )


# ── _classifier helpers ──────────────────────────────────────────────


class TestLinearToDb:
    def test_zero_returns_minus_infinity(self) -> None:
        assert _linear_to_db(0.0) == float("-inf")

    def test_negative_returns_minus_infinity(self) -> None:
        # Defensive — RMS is non-negative by construction, but the
        # helper guards against arithmetic edge cases.
        assert _linear_to_db(-0.5) == float("-inf")

    def test_unity_amplitude_is_zero_db(self) -> None:
        assert _linear_to_db(1.0) == 0.0

    def test_half_amplitude_is_minus_six_db(self) -> None:
        assert math.isclose(_linear_to_db(0.5), -6.0206, abs_tol=0.001)

    def test_full_scale_int16_normalised(self) -> None:
        # 32767 / 32768 ≈ 0.99997 → very close to 0 dB.
        result = _linear_to_db(32767.0 / 32768.0)
        assert -0.001 < result < 0.0


class TestFormatScale:
    @pytest.mark.parametrize(
        ("sample_format", "expected"),
        [
            ("int16", float(1 << 15)),
            ("int24", float(1 << 23)),
            ("float32", 1.0),
        ],
    )
    def test_known_formats(self, sample_format: str, expected: float) -> None:
        assert _format_scale(sample_format) == expected

    def test_unknown_format_raises(self) -> None:
        # The pragma:nocover branch is unreachable in production
        # because the cascade table only emits known formats — but the
        # raise IS the contract for hand-built test combos / future
        # additions. Pin it.
        with pytest.raises(ValueError, match="unexpected sample_format"):
            _format_scale("int8")


class TestWarmupSamples:
    def test_returns_int_proportional_to_sample_rate(self) -> None:
        # _WARMUP_DISCARD_MS is sourced from VoiceTuningConfig at
        # import time; the formula is sample_rate * ms / 1000.
        # Two identical-format combos at different rates must
        # report proportional warmup counts.
        combo_48k = _combo(sample_rate=48_000)
        combo_16k = _combo(sample_rate=16_000)
        warmup_48 = _warmup_samples(combo_48k)
        warmup_16 = _warmup_samples(combo_16k)
        # 48 kHz → 3× more samples in same wall-clock window.
        assert warmup_48 == 3 * warmup_16

    def test_returns_non_negative_int(self) -> None:
        warmup = _warmup_samples(_combo())
        assert isinstance(warmup, int)
        assert warmup >= 0


# ── _warm path: _analyse_rms ──────────────────────────────────────────


class TestAnalyseRms:
    def test_empty_blocks_returns_minus_infinity(self) -> None:
        assert _analyse_rms([], _combo()) == float("-inf")

    def test_all_warmup_returns_minus_infinity(self) -> None:
        # When every captured sample falls within the warmup window,
        # there's no post-warmup audio to analyse → -inf.
        combo = _combo(sample_rate=48_000)
        warmup = _warmup_samples(combo)
        # Block size strictly less than warmup count.
        block = np.zeros(max(1, warmup // 2), dtype=np.int16)
        assert _analyse_rms([block], combo) == float("-inf")

    def test_post_warmup_signal_returns_finite_db(self) -> None:
        combo = _combo(sample_rate=48_000)
        warmup = _warmup_samples(combo)
        # 2 × warmup samples at half-scale int16 amplitude. After the
        # warmup discard, half the block remains.
        block = np.full(2 * warmup, 16384, dtype=np.int16)
        result = _analyse_rms([block], combo)
        assert math.isfinite(result)
        # Half-scale int16 ≈ -6 dB ± LSB noise.
        assert -7.0 < result < -5.0

    def test_multichannel_block_downmixed_to_mono(self) -> None:
        # 2-channel block — mean across channels must collapse before
        # RMS so multichannel hardware reports the same dBFS as mono.
        combo = _combo(sample_rate=48_000, channels=2)
        warmup = _warmup_samples(combo)
        # Block shape (N, 2) where each channel carries the same value.
        n_samples = 2 * warmup
        block = np.full((n_samples, 2), 16384, dtype=np.int16)
        result = _analyse_rms([block], combo)
        assert math.isfinite(result)
        # Identical channels → mean = same value → dBFS unchanged.
        assert -7.0 < result < -5.0


# ── _warm path: _analyse_vad ──────────────────────────────────────────


class _FakeSileroVAD:
    """Minimal stand-in returning a configurable per-frame probability."""

    def __init__(self, *, probability: float = 0.5) -> None:
        self.probability = probability
        self.calls = 0

    def process_frame(self, _frame: npt.NDArray[Any]) -> Any:  # noqa: ANN401
        self.calls += 1
        result = MagicMock()
        result.probability = self.probability
        return result


class _FakeFrameNormalizer:
    """Stand-in that yields one canonical 16 kHz / 512-sample window per push."""

    def __init__(
        self,
        source_rate: int,  # noqa: ARG002
        source_channels: int,  # noqa: ARG002
        source_format: str,  # noqa: ARG002
    ) -> None:
        self.pushes = 0

    def push(self, _block: npt.NDArray[Any]) -> list[npt.NDArray[Any]]:
        self.pushes += 1
        return [np.zeros(_TARGET_PIPELINE_WINDOW, dtype=np.float32)]


class TestAnalyseVad:
    def test_empty_blocks_returns_zero_zero(self) -> None:
        result = _analyse_vad(
            [],
            combo=_combo(),
            vad=_FakeSileroVAD(),  # type: ignore[arg-type]
            frame_normalizer_factory=_FakeFrameNormalizer,  # type: ignore[arg-type]
        )
        assert result == (0.0, 0.0)

    def test_warmup_only_returns_zero_zero(self) -> None:
        # Block fits entirely in the warmup window → no VAD frames
        # produced → (0.0, 0.0).
        combo = _combo(sample_rate=48_000)
        warmup = _warmup_samples(combo)
        block = np.zeros(max(1, warmup // 2), dtype=np.int16)
        vad = _FakeSileroVAD(probability=0.9)
        result = _analyse_vad(
            [block],
            combo=combo,
            vad=vad,  # type: ignore[arg-type]
            frame_normalizer_factory=_FakeFrameNormalizer,  # type: ignore[arg-type]
        )
        assert result == (0.0, 0.0)
        assert vad.calls == 0  # warmup peeled off before VAD ran.

    def test_post_warmup_returns_max_and_mean(self) -> None:
        combo = _combo(sample_rate=48_000)
        warmup = _warmup_samples(combo)
        # 2 × warmup samples → post-warmup half pushes through normalizer.
        block = np.zeros(2 * warmup, dtype=np.int16)
        vad = _FakeSileroVAD(probability=0.75)
        max_p, mean_p = _analyse_vad(
            [block],
            combo=combo,
            vad=vad,  # type: ignore[arg-type]
            frame_normalizer_factory=_FakeFrameNormalizer,  # type: ignore[arg-type]
        )
        assert math.isclose(max_p, 0.75, abs_tol=1e-6)
        assert math.isclose(mean_p, 0.75, abs_tol=1e-6)
        assert vad.calls >= 1

    def test_default_factory_used_when_none(self) -> None:
        # The contract is that ``frame_normalizer_factory=None`` falls
        # back to the canonical default. Pin via direct call — the
        # default factory builds a real FrameNormalizer that needs the
        # full module dependency, so we just verify the function
        # returns something call-shaped.
        factory = _default_frame_normalizer_factory
        normalizer = factory(48_000, 1, "int16")
        # Just check the contract — has push() callable.
        assert callable(normalizer.push)

    def test_2d_block_warmup_strip_preserves_channel_axis(self) -> None:
        # Multichannel block during warmup peel — second-axis (channel)
        # must NOT be touched by the warmup stripper. Defensive
        # regression guard against the warmup loop accidentally
        # collapsing channels.
        combo = _combo(sample_rate=48_000, channels=2)
        warmup = _warmup_samples(combo)
        # Block carries 4 × warmup samples across 2 channels.
        block = np.zeros((4 * warmup, 2), dtype=np.int16)
        vad = _FakeSileroVAD(probability=0.4)
        max_p, _ = _analyse_vad(
            [block],
            combo=combo,
            vad=vad,  # type: ignore[arg-type]
            frame_normalizer_factory=_FakeFrameNormalizer,  # type: ignore[arg-type]
        )
        # VAD ran at least once (post-warmup chunk got through).
        assert vad.calls >= 1
        assert math.isclose(max_p, 0.4, abs_tol=1e-6)


# ── _warm path: _diagnose_warm classification table ──────────────────


class TestDiagnoseWarm:
    """Pin the warm-mode diagnosis table (ADR §4.3)."""

    def test_zero_callbacks_is_no_signal(self) -> None:
        result = _diagnose_warm(rms_db=-30.0, vad_max_prob=0.9, callbacks_fired=0)
        assert result is Diagnosis.NO_SIGNAL

    def test_rms_below_no_signal_ceiling_is_no_signal(self) -> None:
        # Far below the no-signal floor — definite NO_SIGNAL.
        result = _diagnose_warm(
            rms_db=_RMS_DB_NO_SIGNAL_CEILING - 10.0,
            vad_max_prob=0.9,
            callbacks_fired=100,
        )
        assert result is Diagnosis.NO_SIGNAL

    def test_rms_below_low_signal_ceiling_is_low_signal(self) -> None:
        # Between no-signal and low-signal thresholds → LOW_SIGNAL.
        # Use midpoint to avoid boundary-condition flakiness.
        rms = (_RMS_DB_NO_SIGNAL_CEILING + _RMS_DB_LOW_SIGNAL_CEILING) / 2
        result = _diagnose_warm(rms_db=rms, vad_max_prob=0.9, callbacks_fired=100)
        assert result is Diagnosis.LOW_SIGNAL

    def test_healthy_rms_high_vad_is_healthy(self) -> None:
        # rms ≥ low-signal ceiling AND vad ≥ healthy floor → HEALTHY.
        result = _diagnose_warm(
            rms_db=_RMS_DB_LOW_SIGNAL_CEILING + 5.0,
            vad_max_prob=_VAD_HEALTHY_FLOOR + 0.05,
            callbacks_fired=100,
        )
        assert result is Diagnosis.HEALTHY

    def test_healthy_rms_dead_vad_is_apo_degraded(self) -> None:
        # Voice Clarity signature: healthy RMS + flat VAD.
        result = _diagnose_warm(
            rms_db=_RMS_DB_LOW_SIGNAL_CEILING + 5.0,
            vad_max_prob=_VAD_APO_DEGRADED_CEILING / 2,  # well below ceiling
            callbacks_fired=100,
        )
        assert result is Diagnosis.APO_DEGRADED

    def test_healthy_rms_middling_vad_is_vad_insensitive(self) -> None:
        # rms healthy, vad between APO_DEGRADED ceiling and HEALTHY
        # floor → VAD_INSENSITIVE (mic with very low sensitivity or
        # heavy background noise).
        rms = _RMS_DB_LOW_SIGNAL_CEILING + 5.0
        # Pick a VAD value strictly between the two thresholds.
        vad_mid = (_VAD_APO_DEGRADED_CEILING + _VAD_HEALTHY_FLOOR) / 2
        result = _diagnose_warm(
            rms_db=rms,
            vad_max_prob=vad_mid,
            callbacks_fired=100,
        )
        assert result is Diagnosis.VAD_INSENSITIVE

    def test_no_signal_priority_over_callbacks(self) -> None:
        # callbacks_fired check happens FIRST. Even with healthy RMS
        # and VAD, zero callbacks → NO_SIGNAL.
        result = _diagnose_warm(
            rms_db=-15.0,
            vad_max_prob=0.99,
            callbacks_fired=0,
        )
        assert result is Diagnosis.NO_SIGNAL

    # T6.2 — STREAM_OPEN_TIMEOUT distinction

    def test_zero_callbacks_short_elapsed_is_no_signal(self) -> None:
        # Below the 5 s threshold → still NO_SIGNAL (probe didn't wait
        # long enough to claim STREAM_OPEN_TIMEOUT).
        result = _diagnose_warm(
            rms_db=-30.0,
            vad_max_prob=0.0,
            callbacks_fired=0,
            elapsed_ms=1_500,  # default cold probe duration
        )
        assert result is Diagnosis.NO_SIGNAL

    def test_zero_callbacks_long_elapsed_is_stream_open_timeout(self) -> None:
        # ≥ 5 s elapsed without a callback → driver wedged → STREAM_OPEN_TIMEOUT.
        result = _diagnose_warm(
            rms_db=-30.0,
            vad_max_prob=0.0,
            callbacks_fired=0,
            elapsed_ms=5_000,
        )
        assert result is Diagnosis.STREAM_OPEN_TIMEOUT

    def test_zero_callbacks_no_elapsed_falls_back_to_no_signal(self) -> None:
        # Backwards compat: pre-T6.2 callers don't pass elapsed_ms.
        # Default behaviour stays NO_SIGNAL.
        result = _diagnose_warm(
            rms_db=-30.0,
            vad_max_prob=0.0,
            callbacks_fired=0,
        )
        assert result is Diagnosis.NO_SIGNAL

    def test_callbacks_fired_skips_timeout_check(self) -> None:
        # Even with elapsed >> threshold, ANY callback firing means
        # the driver is alive — STREAM_OPEN_TIMEOUT does NOT fire.
        # The downstream RMS / VAD path takes over. Drive RMS below
        # the no-signal ceiling so the verdict is NO_SIGNAL via the
        # RMS branch (NOT via the callbacks_fired==0 fall-through).
        result = _diagnose_warm(
            rms_db=-100.0,  # well below _RMS_DB_NO_SIGNAL_CEILING (-70)
            vad_max_prob=0.0,
            callbacks_fired=10,
            elapsed_ms=10_000,
        )
        # callbacks ≥ 1 → STREAM_OPEN_TIMEOUT skipped; RMS branch takes
        # over; rms < no_signal ceiling → NO_SIGNAL.
        assert result is Diagnosis.NO_SIGNAL

    def test_callbacks_fired_with_healthy_signal_returns_healthy(self) -> None:
        # Symmetric companion: callbacks ≥ 1 + healthy RMS + healthy
        # VAD → HEALTHY regardless of elapsed_ms. STREAM_OPEN_TIMEOUT
        # is gated on callbacks==0 and never preempts the success path.
        result = _diagnose_warm(
            rms_db=_RMS_DB_LOW_SIGNAL_CEILING + 5.0,
            vad_max_prob=_VAD_HEALTHY_FLOOR + 0.05,
            callbacks_fired=49,
            elapsed_ms=15_000,
        )
        assert result is Diagnosis.HEALTHY


# ── T6.2 — STREAM_OPEN_TIMEOUT in cold path ────────────────────────


class TestDiagnoseColdStreamOpenTimeout:
    """T6.2 — _diagnose_cold honors the same elapsed_ms threshold as warm."""

    def _cold_combo(self) -> Combo:
        return _combo(host_api="WASAPI", sample_rate=16_000)

    def test_zero_callbacks_short_elapsed_is_no_signal(self) -> None:
        from sovyx.voice.health.probe._cold import _diagnose_cold

        result = _diagnose_cold(
            callbacks_fired=0,
            rms_db=float("-inf"),
            combo=self._cold_combo(),
            elapsed_ms=1_500,
        )
        assert result is Diagnosis.NO_SIGNAL

    def test_zero_callbacks_long_elapsed_is_stream_open_timeout(self) -> None:
        from sovyx.voice.health.probe._cold import _diagnose_cold

        result = _diagnose_cold(
            callbacks_fired=0,
            rms_db=float("-inf"),
            combo=self._cold_combo(),
            elapsed_ms=5_000,
        )
        assert result is Diagnosis.STREAM_OPEN_TIMEOUT

    def test_zero_callbacks_no_elapsed_falls_back_to_no_signal(self) -> None:
        # Backwards compat: pre-T6.2 callers (notably the existing
        # property test in test_probe.py) don't pass elapsed_ms. The
        # legacy NO_SIGNAL classification is preserved.
        from sovyx.voice.health.probe._cold import _diagnose_cold

        result = _diagnose_cold(
            callbacks_fired=0,
            rms_db=float("-inf"),
            combo=self._cold_combo(),
        )
        assert result is Diagnosis.NO_SIGNAL

    def test_callbacks_fired_skips_timeout_check(self) -> None:
        # Healthy callback rate → STREAM_OPEN_TIMEOUT does NOT fire
        # regardless of elapsed_ms. RMS / strict-validation path
        # takes over per existing Furo W-1 logic.
        from sovyx.voice.health.probe._cold import _diagnose_cold

        result = _diagnose_cold(
            callbacks_fired=49,
            rms_db=-30.0,  # healthy RMS
            combo=self._cold_combo(),
            elapsed_ms=10_000,
        )
        # callbacks ≥1 + healthy RMS → HEALTHY (Furo W-1 lenient/strict
        # acceptance — verified in test_probe.py); STREAM_OPEN_TIMEOUT
        # is unreachable.
        assert result is Diagnosis.HEALTHY


# ── _dispatch helpers ─────────────────────────────────────────────────


class TestComboTag:
    def test_carries_all_diagnostic_fields(self) -> None:
        tag = _combo_tag(
            _combo(
                host_api="WASAPI",
                sample_rate=48_000,
                channels=2,
                sample_format="int16",
                exclusive=True,
            ),
        )
        # Compact pipe-separated format. Operators read tight log
        # lines; the format is the wire contract for cascade-attempt
        # observability.
        assert "WASAPI" in tag
        assert "48000" in tag
        assert "2" in tag
        assert "int16" in tag
        assert "excl=True" in tag

    def test_shared_mode_marked_excl_false(self) -> None:
        tag = _combo_tag(_combo(exclusive=False))
        assert "excl=False" in tag


class TestBuildProbeWasapiSettings:
    """Defensive helper — every fallback branch must return None gracefully."""

    def test_non_wasapi_host_api_returns_none(self) -> None:
        # Combo() validates host_api against the runtime platform, so
        # construct a Linux combo explicitly via ``platform_key`` to
        # let "ALSA" pass __post_init__ on a Windows test host.
        sd = MagicMock()
        non_wasapi_combo = Combo(
            host_api="ALSA",
            sample_rate=48_000,
            channels=1,
            sample_format="int16",
            exclusive=False,
            auto_convert=True,
            frames_per_buffer=480,
            platform_key="linux",
        )
        result = _build_probe_wasapi_settings(sd, non_wasapi_combo)
        assert result is None

    def test_no_auto_convert_or_exclusive_returns_none(self) -> None:
        sd = MagicMock()
        sd.WasapiSettings = MagicMock()
        result = _build_probe_wasapi_settings(
            sd,
            _combo(host_api="WASAPI", auto_convert=False, exclusive=False),
        )
        assert result is None
        # Constructor was NOT invoked when nothing to apply.
        sd.WasapiSettings.assert_not_called()

    def test_missing_wasapi_settings_attr_returns_none(self) -> None:
        # Older PortAudio wheels don't expose WasapiSettings.
        sd = MagicMock(spec=[])  # spec=[] strips all attrs
        result = _build_probe_wasapi_settings(
            sd,
            _combo(host_api="WASAPI", auto_convert=True),
        )
        assert result is None

    def test_typeerror_in_constructor_returns_none(self) -> None:
        # Older WasapiSettings rejects the kwarg set → TypeError.
        sd = MagicMock()
        sd.WasapiSettings = MagicMock(side_effect=TypeError("unknown kwarg"))
        result = _build_probe_wasapi_settings(
            sd,
            _combo(host_api="WASAPI", exclusive=True),
        )
        assert result is None

    def test_wasapi_with_exclusive_calls_constructor(self) -> None:
        sd = MagicMock()
        instance = MagicMock(name="WasapiSettings_instance")
        sd.WasapiSettings = MagicMock(return_value=instance)
        result = _build_probe_wasapi_settings(
            sd,
            _combo(host_api="WASAPI", exclusive=True),
        )
        assert result is instance
        sd.WasapiSettings.assert_called_once_with(exclusive=True)

    def test_wasapi_with_auto_convert_calls_constructor(self) -> None:
        sd = MagicMock()
        instance = MagicMock(name="WasapiSettings_instance")
        sd.WasapiSettings = MagicMock(return_value=instance)
        result = _build_probe_wasapi_settings(
            sd,
            _combo(host_api="WASAPI", auto_convert=True),
        )
        assert result is instance
        sd.WasapiSettings.assert_called_once_with(auto_convert=True)

    def test_wasapi_with_both_flags(self) -> None:
        sd = MagicMock()
        instance = MagicMock()
        sd.WasapiSettings = MagicMock(return_value=instance)
        result = _build_probe_wasapi_settings(
            sd,
            _combo(host_api="WASAPI", auto_convert=True, exclusive=True),
        )
        assert result is instance
        sd.WasapiSettings.assert_called_once_with(auto_convert=True, exclusive=True)

    def test_windows_wasapi_label_treated_same(self) -> None:
        # The cascade table emits "WASAPI" or "Windows WASAPI" depending
        # on the surface; both must route through this helper.
        sd = MagicMock()
        sd.WasapiSettings = MagicMock(return_value=MagicMock())
        result = _build_probe_wasapi_settings(
            sd,
            _combo(host_api="Windows WASAPI", exclusive=True),
        )
        assert result is not None


class TestLoadSounddevice:
    def test_returns_sounddevice_module(self) -> None:
        # On test hosts sounddevice IS importable (it's a dev dep).
        # We just verify the function returns something module-shaped
        # — guards against accidental refactor that returns a class.
        sd = _load_sounddevice()
        # The real sounddevice module exposes ``InputStream``.
        assert hasattr(sd, "InputStream")
