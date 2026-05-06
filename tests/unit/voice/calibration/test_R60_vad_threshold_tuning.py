"""Unit tests for R60_vad_threshold_tuning."""

from __future__ import annotations

from sovyx.voice.calibration import (
    CalibrationConfidence,
    HardwareFingerprint,
    MeasurementSnapshot,
    RuleContext,
)
from sovyx.voice.calibration.rules.R60_vad_threshold_tuning import rule


def _fingerprint() -> HardwareFingerprint:
    return HardwareFingerprint(
        schema_version=1,
        captured_at_utc="2026-05-05T18:00:00Z",
        distro_id="linuxmint",
        distro_id_like="debian",
        kernel_release="6.8",
        kernel_major_minor="6.8",
        cpu_model="Intel",
        cpu_cores=8,
        ram_mb=16384,
        has_gpu=False,
        gpu_vram_mb=0,
        audio_stack="pipewire",
        pipewire_version="1.0.5",
        pulseaudio_version=None,
        alsa_lib_version="1.2.10",
        codec_id="10ec:0257",
        driver_family="hda",
        system_vendor="Generic",
        system_product="x",
        capture_card_count=1,
        capture_devices=("Mic",),
        apo_active=False,
        apo_name=None,
        hal_interceptors=(),
        pulse_modules_destructive=(),
    )


def _measurements(*, vad_max: float = 0.40) -> MeasurementSnapshot:
    return MeasurementSnapshot(
        schema_version=1,
        captured_at_utc="2026-05-05T18:01:00Z",
        duration_s=30.0,
        rms_dbfs_per_capture=(-25.0,),
        vad_speech_probability_max=vad_max,
        vad_speech_probability_p99=vad_max - 0.05,
        noise_floor_dbfs_estimate=-55.0,
        capture_callback_p99_ms=12.0,
        capture_jitter_ms=0.5,
        portaudio_latency_advertised_ms=10.0,
        mixer_card_index=0,
        mixer_capture_pct=70,
        mixer_boost_pct=30,
        mixer_internal_mic_boost_pct=0,
        mixer_attenuation_regime="healthy",
        echo_correlation_db=None,
        triage_winner_hid=None,
        triage_winner_confidence=None,
    )


def _ctx(*, vad_max: float = 0.40) -> RuleContext:
    return RuleContext(
        fingerprint=_fingerprint(),
        measurements=_measurements(vad_max=vad_max),
        triage_result=None,
        prior_decisions=(),
    )


class TestApplies:
    def test_fires_in_window(self) -> None:
        assert rule.applies(_ctx(vad_max=0.40)) is True

    def test_fires_at_lower_bound(self) -> None:
        assert rule.applies(_ctx(vad_max=0.30)) is True

    def test_fires_at_upper_bound(self) -> None:
        assert rule.applies(_ctx(vad_max=0.55)) is True

    def test_does_not_fire_below_window(self) -> None:
        assert rule.applies(_ctx(vad_max=0.20)) is False

    def test_does_not_fire_above_window(self) -> None:
        assert rule.applies(_ctx(vad_max=0.95)) is False

    def test_does_not_fire_on_zero_signal(self) -> None:
        # Zero VAD signal means no captures happened; do not speculate.
        assert rule.applies(_ctx(vad_max=0.0)) is False


class TestEvaluate:
    def test_emits_advise_medium(self) -> None:
        evaluation = rule.evaluate(_ctx())
        decision = evaluation.decisions[0]
        assert decision.operation == "advise"
        assert decision.confidence == CalibrationConfidence.MEDIUM

    def test_advice_mentions_threshold_floor(self) -> None:
        evaluation = rule.evaluate(_ctx())
        # The recommended floor (0.30) appears in the advice value.
        assert "0.30" in evaluation.decisions[0].value
        assert "vad_threshold" in evaluation.decisions[0].value


class TestPriority:
    def test_priority_50(self) -> None:
        assert rule.priority == 50
