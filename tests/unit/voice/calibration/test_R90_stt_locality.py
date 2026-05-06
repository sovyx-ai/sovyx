"""Unit tests for R90_stt_locality."""

from __future__ import annotations

from sovyx.voice.calibration import (
    CalibrationConfidence,
    HardwareFingerprint,
    MeasurementSnapshot,
    RuleContext,
)
from sovyx.voice.calibration.rules.R90_stt_locality import rule


def _fingerprint(*, cpu_cores: int = 8, ram_mb: int = 16384) -> HardwareFingerprint:
    return HardwareFingerprint(
        schema_version=1,
        captured_at_utc="2026-05-05T18:00:00Z",
        distro_id="linuxmint",
        distro_id_like="debian",
        kernel_release="6.8",
        kernel_major_minor="6.8",
        cpu_model="Intel",
        cpu_cores=cpu_cores,
        ram_mb=ram_mb,
        has_gpu=False,
        gpu_vram_mb=0,
        audio_stack="pipewire",
        pipewire_version="1.0.5",
        pulseaudio_version=None,
        alsa_lib_version="1.2.10",
        codec_id="10ec:0257",
        driver_family="hda",
        system_vendor="x",
        system_product="x",
        capture_card_count=1,
        capture_devices=("Mic",),
        apo_active=False,
        apo_name=None,
        hal_interceptors=(),
        pulse_modules_destructive=(),
    )


def _measurements() -> MeasurementSnapshot:
    return MeasurementSnapshot(
        schema_version=1,
        captured_at_utc="2026-05-05T18:01:00Z",
        duration_s=30.0,
        rms_dbfs_per_capture=(-25.0,),
        vad_speech_probability_max=0.9,
        vad_speech_probability_p99=0.85,
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


def _ctx(*, cpu_cores: int = 8, ram_mb: int = 16384) -> RuleContext:
    return RuleContext(
        fingerprint=_fingerprint(cpu_cores=cpu_cores, ram_mb=ram_mb),
        measurements=_measurements(),
        triage_result=None,
        prior_decisions=(),
    )


class TestApplies:
    def test_always_fires(self) -> None:
        # R90 produces an advisory regardless of host tier.
        assert rule.applies(_ctx()) is True
        assert rule.applies(_ctx(cpu_cores=2, ram_mb=2048)) is True


class TestEvaluate:
    def test_high_tier_recommends_local(self) -> None:
        evaluation = rule.evaluate(_ctx(cpu_cores=8, ram_mb=16384))
        assert "moonshine" in evaluation.decisions[0].value.lower()
        assert "local" in evaluation.decisions[0].value.lower()

    def test_low_tier_recommends_cloud(self) -> None:
        evaluation = rule.evaluate(_ctx(cpu_cores=2, ram_mb=2048))
        assert "cloud" in evaluation.decisions[0].value.lower()

    def test_just_below_threshold_recommends_cloud(self) -> None:
        # 4 cores OK but ram below threshold -> cloud
        evaluation = rule.evaluate(_ctx(cpu_cores=4, ram_mb=3072))
        assert "cloud" in evaluation.decisions[0].value.lower()

    def test_at_threshold_recommends_local(self) -> None:
        # Exactly at thresholds (ram=4096 + cores=4) -> local capable
        evaluation = rule.evaluate(_ctx(cpu_cores=4, ram_mb=4096))
        assert "local" in evaluation.decisions[0].value.lower()

    def test_confidence_is_low(self) -> None:
        # STT locality is operator-preference heavy; LOW confidence
        # surfaces the recommendation without being prescriptive.
        evaluation = rule.evaluate(_ctx())
        assert evaluation.decisions[0].confidence == CalibrationConfidence.LOW


class TestPriority:
    def test_priority_35(self) -> None:
        assert rule.priority == 35
