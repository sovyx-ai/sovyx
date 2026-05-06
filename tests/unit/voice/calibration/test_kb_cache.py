"""Tests for sovyx.voice.calibration._kb_cache.

Validates: cache_path resolution, store_profile (atomic write +
overwrite), lookup_profile (hit / miss / malformed JSON / wrong root
type / schema mismatch), has_match (cheap presence check).
"""

from __future__ import annotations

from pathlib import Path

from sovyx.voice.calibration import (
    CalibrationConfidence,
    CalibrationDecision,
    CalibrationProfile,
    HardwareFingerprint,
    MeasurementSnapshot,
)
from sovyx.voice.calibration._kb_cache import (
    cache_path,
    has_match,
    kb_dir,
    lookup_profile,
    store_profile,
)


def _fingerprint(*, codec_id: str = "10ec:0257") -> HardwareFingerprint:
    return HardwareFingerprint(
        schema_version=1,
        captured_at_utc="2026-05-06T18:00:00Z",
        distro_id="linuxmint",
        distro_id_like="debian",
        kernel_release="6.8",
        kernel_major_minor="6.8",
        cpu_model="Intel",
        cpu_cores=12,
        ram_mb=16384,
        has_gpu=False,
        gpu_vram_mb=0,
        audio_stack="pipewire",
        pipewire_version="1.0.5",
        pulseaudio_version=None,
        alsa_lib_version="ALSA",
        codec_id=codec_id,
        driver_family="hda",
        system_vendor="Sony",
        system_product="VAIO",
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
        captured_at_utc="2026-05-06T18:01:00Z",
        duration_s=0.0,
        rms_dbfs_per_capture=(),
        vad_speech_probability_max=0.0,
        vad_speech_probability_p99=0.0,
        noise_floor_dbfs_estimate=0.0,
        capture_callback_p99_ms=0.0,
        capture_jitter_ms=0.0,
        portaudio_latency_advertised_ms=0.0,
        mixer_card_index=None,
        mixer_capture_pct=None,
        mixer_boost_pct=None,
        mixer_internal_mic_boost_pct=None,
        mixer_attenuation_regime=None,
        echo_correlation_db=None,
        triage_winner_hid=None,
        triage_winner_confidence=None,
    )


def _profile(
    *, mind_id: str = "default", fingerprint: HardwareFingerprint | None = None
) -> CalibrationProfile:
    fp = fingerprint if fingerprint is not None else _fingerprint()
    return CalibrationProfile(
        schema_version=1,
        profile_id="11111111-2222-3333-4444-555555555555",
        mind_id=mind_id,
        fingerprint=fp,
        measurements=_measurements(),
        decisions=(
            CalibrationDecision(
                target="advice.action",
                target_class="TuningAdvice",
                operation="advise",
                value="run X",
                rationale="r",
                rule_id="R10",
                rule_version=1,
                confidence=CalibrationConfidence.HIGH,
            ),
        ),
        provenance=(),
        generated_by_engine_version="0.30.18",
        generated_by_rule_set_version=1,
        generated_at_utc="2026-05-06T18:02:00Z",
        signature=None,
    )


class TestPaths:
    def test_kb_dir(self, tmp_path: Path) -> None:
        assert kb_dir(tmp_path) == tmp_path / "voice_calibration" / "_kb"

    def test_cache_path_uses_fingerprint_hash(self, tmp_path: Path) -> None:
        h = "a" * 64
        path = cache_path(tmp_path, h)
        assert path.name == f"{h}.json"
        assert path.parent == kb_dir(tmp_path)


class TestStoreAndLookup:
    def test_round_trip(self, tmp_path: Path) -> None:
        original = _profile()
        store_profile(original, data_dir=tmp_path)
        loaded = lookup_profile(
            data_dir=tmp_path,
            fingerprint_hash=original.fingerprint.fingerprint_hash,
        )
        assert loaded is not None
        assert loaded.fingerprint.fingerprint_hash == original.fingerprint.fingerprint_hash
        assert loaded.decisions == original.decisions

    def test_overwrite_atomic(self, tmp_path: Path) -> None:
        v1 = _profile(mind_id="alice")
        store_profile(v1, data_dir=tmp_path)
        v2 = _profile(mind_id="bob")  # same fingerprint, different mind_id
        store_profile(v2, data_dir=tmp_path)
        loaded = lookup_profile(
            data_dir=tmp_path,
            fingerprint_hash=v2.fingerprint.fingerprint_hash,
        )
        assert loaded is not None
        # v2 wins -- atomic os.replace overwrote.
        assert loaded.mind_id == "bob"
        # tmp file cleaned up.
        assert not (
            cache_path(tmp_path, v2.fingerprint.fingerprint_hash).with_suffix(".json.tmp").exists()
        )

    def test_distinct_fingerprints_distinct_cache_entries(self, tmp_path: Path) -> None:
        a = _profile(fingerprint=_fingerprint(codec_id="10ec:0257"))
        b = _profile(fingerprint=_fingerprint(codec_id="8086:9d70"))
        store_profile(a, data_dir=tmp_path)
        store_profile(b, data_dir=tmp_path)
        # Both cached; lookup by hash returns the right one.
        loaded_a = lookup_profile(
            data_dir=tmp_path, fingerprint_hash=a.fingerprint.fingerprint_hash
        )
        loaded_b = lookup_profile(
            data_dir=tmp_path, fingerprint_hash=b.fingerprint.fingerprint_hash
        )
        assert loaded_a is not None and loaded_b is not None
        assert loaded_a.fingerprint.codec_id == "10ec:0257"
        assert loaded_b.fingerprint.codec_id == "8086:9d70"


class TestLookupMissPaths:
    def test_lookup_missing_returns_none(self, tmp_path: Path) -> None:
        assert lookup_profile(data_dir=tmp_path, fingerprint_hash="x" * 64) is None

    def test_lookup_malformed_json_returns_none(self, tmp_path: Path) -> None:
        h = "z" * 64
        path = cache_path(tmp_path, h)
        path.parent.mkdir(parents=True)
        path.write_text("{not valid json}", encoding="utf-8")
        assert lookup_profile(data_dir=tmp_path, fingerprint_hash=h) is None

    def test_lookup_non_object_root_returns_none(self, tmp_path: Path) -> None:
        h = "y" * 64
        path = cache_path(tmp_path, h)
        path.parent.mkdir(parents=True)
        path.write_text("[1, 2]", encoding="utf-8")
        assert lookup_profile(data_dir=tmp_path, fingerprint_hash=h) is None

    def test_lookup_schema_mismatch_returns_none(self, tmp_path: Path) -> None:
        h = "w" * 64
        path = cache_path(tmp_path, h)
        path.parent.mkdir(parents=True)
        path.write_text('{"schema_version": 1, "missing": "fields"}', encoding="utf-8")
        assert lookup_profile(data_dir=tmp_path, fingerprint_hash=h) is None


class TestHasMatch:
    def test_has_match_false_when_missing(self, tmp_path: Path) -> None:
        assert has_match(data_dir=tmp_path, fingerprint_hash="x" * 64) is False

    def test_has_match_true_after_store(self, tmp_path: Path) -> None:
        p = _profile()
        store_profile(p, data_dir=tmp_path)
        assert (
            has_match(data_dir=tmp_path, fingerprint_hash=p.fingerprint.fingerprint_hash) is True
        )
