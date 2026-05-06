"""Unit tests for R70_capture_mode_exclusive."""

from __future__ import annotations

from sovyx.voice.calibration import (
    CalibrationConfidence,
    HardwareFingerprint,
    MeasurementSnapshot,
    RuleContext,
)
from sovyx.voice.calibration.rules.R70_capture_mode_exclusive import rule


def _fingerprint(*, distro_id: str = "windows") -> HardwareFingerprint:
    return HardwareFingerprint(
        schema_version=1,
        captured_at_utc="2026-05-05T18:00:00Z",
        distro_id=distro_id,
        distro_id_like="windows" if distro_id == "windows" else "debian",
        kernel_release="10.0",
        kernel_major_minor="10.0",
        cpu_model="Intel",
        cpu_cores=8,
        ram_mb=16384,
        has_gpu=False,
        gpu_vram_mb=0,
        audio_stack="wasapi",
        pipewire_version=None,
        pulseaudio_version=None,
        alsa_lib_version=None,
        codec_id=None,
        driver_family=None,
        system_vendor="x",
        system_product="x",
        capture_card_count=1,
        capture_devices=("Mic",),
        apo_active=False,
        apo_name=None,
        hal_interceptors=(),
        pulse_modules_destructive=(),
    )


def _measurements(
    *,
    latency_ms: float = 50.0,
    jitter_ms: float = 0.5,
) -> MeasurementSnapshot:
    return MeasurementSnapshot(
        schema_version=1,
        captured_at_utc="2026-05-05T18:01:00Z",
        duration_s=30.0,
        rms_dbfs_per_capture=(-25.0,),
        vad_speech_probability_max=0.9,
        vad_speech_probability_p99=0.85,
        noise_floor_dbfs_estimate=-55.0,
        capture_callback_p99_ms=12.0,
        capture_jitter_ms=jitter_ms,
        portaudio_latency_advertised_ms=latency_ms,
        mixer_card_index=None,
        mixer_capture_pct=None,
        mixer_boost_pct=None,
        mixer_internal_mic_boost_pct=None,
        mixer_attenuation_regime=None,
        echo_correlation_db=None,
        triage_winner_hid=None,
        triage_winner_confidence=None,
    )


def _ctx(
    *,
    distro_id: str = "windows",
    latency_ms: float = 50.0,
    jitter_ms: float = 0.5,
) -> RuleContext:
    return RuleContext(
        fingerprint=_fingerprint(distro_id=distro_id),
        measurements=_measurements(latency_ms=latency_ms, jitter_ms=jitter_ms),
        triage_result=None,
        prior_decisions=(),
    )


class TestApplies:
    def test_fires_on_high_latency(self) -> None:
        assert rule.applies(_ctx(latency_ms=50.0, jitter_ms=0.5)) is True

    def test_fires_on_high_jitter(self) -> None:
        assert rule.applies(_ctx(latency_ms=10.0, jitter_ms=8.0)) is True

    def test_fires_when_both_exceed(self) -> None:
        assert rule.applies(_ctx(latency_ms=50.0, jitter_ms=8.0)) is True

    def test_does_not_fire_under_thresholds(self) -> None:
        assert rule.applies(_ctx(latency_ms=15.0, jitter_ms=2.0)) is False

    def test_does_not_fire_on_linux(self) -> None:
        assert rule.applies(_ctx(distro_id="linuxmint", latency_ms=80.0)) is False

    def test_does_not_fire_on_macos(self) -> None:
        assert rule.applies(_ctx(distro_id="macos", latency_ms=80.0)) is False


class TestEvaluate:
    def test_emits_advise_medium(self) -> None:
        evaluation = rule.evaluate(_ctx())
        decision = evaluation.decisions[0]
        assert decision.operation == "advise"
        assert decision.confidence == CalibrationConfidence.MEDIUM

    def test_advice_recommends_exclusive(self) -> None:
        evaluation = rule.evaluate(_ctx())
        assert "exclusive" in evaluation.decisions[0].value.lower()
        assert "trade-off" in evaluation.decisions[0].value.lower()


class TestPriority:
    def test_priority_45(self) -> None:
        assert rule.priority == 45
