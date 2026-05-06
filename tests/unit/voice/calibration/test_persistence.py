"""Unit tests for sovyx.voice.calibration._persistence.

Coverage:
* save_calibration_profile + load_calibration_profile JSON round-trip
* Atomicity: temp file used; final path replaced once write succeeds
* Schema-version gates: missing/non-int/unknown -> CalibrationProfileLoadError
* LENIENT mode accepts unsigned profiles with WARN
* STRICT mode rejects unsigned profiles
* Malformed JSON / not-an-object -> CalibrationProfileLoadError
* Profile path computation: <data_dir>/<mind_id>/calibration.json
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sovyx.voice.calibration import (
    CalibrationConfidence,
    CalibrationDecision,
    CalibrationProfile,
    HardwareFingerprint,
    MeasurementSnapshot,
    ProvenanceTrace,
)
from sovyx.voice.calibration._persistence import (
    CalibrationProfileLoadError,
    _LoadMode,
    load_calibration_profile,
    profile_path,
    save_calibration_profile,
)

# ====================================================================
# Fixtures
# ====================================================================


def _fingerprint() -> HardwareFingerprint:
    return HardwareFingerprint(
        schema_version=1,
        captured_at_utc="2026-05-05T18:00:00Z",
        distro_id="linuxmint",
        distro_id_like="debian",
        kernel_release="6.8.0-50-generic",
        kernel_major_minor="6.8",
        cpu_model="Intel",
        cpu_cores=12,
        ram_mb=16384,
        has_gpu=False,
        gpu_vram_mb=0,
        audio_stack="pipewire",
        pipewire_version="1.0.5",
        pulseaudio_version=None,
        alsa_lib_version="1.2.10",
        codec_id="10ec:0257",
        driver_family="hda",
        system_vendor="Sony",
        system_product="VAIO",
        capture_card_count=1,
        capture_devices=("Mic A", "Mic B"),
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
        rms_dbfs_per_capture=(-25.0, -26.0),
        vad_speech_probability_max=0.95,
        vad_speech_probability_p99=0.92,
        noise_floor_dbfs_estimate=-55.0,
        capture_callback_p99_ms=12.0,
        capture_jitter_ms=0.5,
        portaudio_latency_advertised_ms=10.0,
        mixer_card_index=0,
        mixer_capture_pct=75,
        mixer_boost_pct=50,
        mixer_internal_mic_boost_pct=25,
        mixer_attenuation_regime="healthy",
        echo_correlation_db=-45.0,
        triage_winner_hid="H10",
        triage_winner_confidence=0.95,
    )


def _profile(*, signature: str | None = None) -> CalibrationProfile:
    return CalibrationProfile(
        schema_version=1,
        profile_id="11111111-2222-3333-4444-555555555555",
        mind_id="default",
        fingerprint=_fingerprint(),
        measurements=_measurements(),
        decisions=(
            CalibrationDecision(
                target="advice.action",
                target_class="TuningAdvice",
                operation="advise",
                value="sovyx doctor voice --fix --yes",
                rationale="mixer attenuated",
                rule_id="R10_mic_attenuated",
                rule_version=1,
                confidence=CalibrationConfidence.HIGH,
            ),
        ),
        provenance=(
            ProvenanceTrace(
                rule_id="R10_mic_attenuated",
                rule_version=1,
                fired_at_utc="2026-05-05T18:02:00Z",
                matched_conditions=("audio_stack=pipewire", "regime=attenuated"),
                produced_decisions=("advise: advice.action = ...",),
                confidence=CalibrationConfidence.HIGH,
            ),
        ),
        generated_by_engine_version="0.30.15",
        generated_by_rule_set_version=1,
        generated_at_utc="2026-05-05T18:02:00Z",
        signature=signature,
    )


# ====================================================================
# profile_path
# ====================================================================


class TestProfilePath:
    """Canonical path is <data_dir>/<mind_id>/calibration.json."""

    def test_default(self, tmp_path: Path) -> None:
        path = profile_path(data_dir=tmp_path, mind_id="default")
        assert path == tmp_path / "default" / "calibration.json"

    def test_per_mind_isolation(self, tmp_path: Path) -> None:
        path_a = profile_path(data_dir=tmp_path, mind_id="alice")
        path_b = profile_path(data_dir=tmp_path, mind_id="bob")
        assert path_a != path_b
        assert path_a.parent.name == "alice"
        assert path_b.parent.name == "bob"


# ====================================================================
# Round-trip
# ====================================================================


class TestRoundTrip:
    """save -> load returns equivalent profile."""

    def test_basic_round_trip(self, tmp_path: Path) -> None:
        original = _profile()
        save_calibration_profile(original, data_dir=tmp_path)
        loaded = load_calibration_profile(data_dir=tmp_path, mind_id="default")
        assert loaded == original

    def test_round_trip_preserves_tuples(self, tmp_path: Path) -> None:
        original = _profile()
        save_calibration_profile(original, data_dir=tmp_path)
        loaded = load_calibration_profile(data_dir=tmp_path, mind_id="default")
        # JSON has lists, but load_*_from_dict converts back to tuples.
        assert isinstance(loaded.fingerprint.capture_devices, tuple)
        assert isinstance(loaded.measurements.rms_dbfs_per_capture, tuple)
        assert isinstance(loaded.decisions, tuple)
        assert isinstance(loaded.provenance, tuple)
        assert isinstance(loaded.provenance[0].matched_conditions, tuple)

    def test_round_trip_preserves_enums(self, tmp_path: Path) -> None:
        original = _profile()
        save_calibration_profile(original, data_dir=tmp_path)
        loaded = load_calibration_profile(data_dir=tmp_path, mind_id="default")
        assert loaded.decisions[0].confidence == CalibrationConfidence.HIGH
        assert loaded.provenance[0].confidence == CalibrationConfidence.HIGH

    def test_round_trip_preserves_none_signature(self, tmp_path: Path) -> None:
        original = _profile(signature=None)
        save_calibration_profile(original, data_dir=tmp_path)
        loaded = load_calibration_profile(data_dir=tmp_path, mind_id="default")
        assert loaded.signature is None

    def test_round_trip_preserves_set_signature(self, tmp_path: Path) -> None:
        original = _profile(signature="abcdef" * 16)
        save_calibration_profile(original, data_dir=tmp_path)
        loaded = load_calibration_profile(data_dir=tmp_path, mind_id="default")
        assert loaded.signature == "abcdef" * 16

    def test_save_creates_per_mind_subdirectory(self, tmp_path: Path) -> None:
        # data_dir must NOT have the per-mind subdir pre-created.
        original = _profile()
        save_calibration_profile(original, data_dir=tmp_path)
        assert (tmp_path / "default").is_dir()
        assert (tmp_path / "default" / "calibration.json").is_file()

    def test_save_overwrites_atomically(self, tmp_path: Path) -> None:
        # Two saves; the second overwrites the first.
        v1 = _profile(signature="v1")
        save_calibration_profile(v1, data_dir=tmp_path)
        v2 = _profile(signature="v2")
        save_calibration_profile(v2, data_dir=tmp_path)
        loaded = load_calibration_profile(data_dir=tmp_path, mind_id="default")
        assert loaded.signature == "v2"
        # Tmp file is cleaned up by os.replace.
        assert not (tmp_path / "default" / "calibration.json.tmp").exists()


# ====================================================================
# Schema-version gates
# ====================================================================


class TestSchemaVersionGate:
    """Loader raises on missing/wrong schema_version."""

    def test_missing_schema_version_raises(self, tmp_path: Path) -> None:
        # Hand-craft a profile JSON with no schema_version.
        path = tmp_path / "default" / "calibration.json"
        path.parent.mkdir(parents=True)
        path.write_text('{"profile_id": "x", "mind_id": "default"}')
        with pytest.raises(CalibrationProfileLoadError, match="missing or non-int schema_version"):
            load_calibration_profile(data_dir=tmp_path, mind_id="default")

    def test_non_int_schema_version_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "default" / "calibration.json"
        path.parent.mkdir(parents=True)
        path.write_text('{"schema_version": "not-an-int"}')
        with pytest.raises(CalibrationProfileLoadError, match="missing or non-int schema_version"):
            load_calibration_profile(data_dir=tmp_path, mind_id="default")

    def test_unknown_schema_version_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "default" / "calibration.json"
        path.parent.mkdir(parents=True)
        path.write_text('{"schema_version": 999}')
        with pytest.raises(CalibrationProfileLoadError, match="schema_version=999"):
            load_calibration_profile(data_dir=tmp_path, mind_id="default")


# ====================================================================
# Error paths
# ====================================================================


class TestErrorPaths:
    """File-not-found, malformed JSON, non-object body."""

    def test_file_not_found(self, tmp_path: Path) -> None:
        with pytest.raises(CalibrationProfileLoadError, match="not found"):
            load_calibration_profile(data_dir=tmp_path, mind_id="ghost")

    def test_malformed_json_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "default" / "calibration.json"
        path.parent.mkdir(parents=True)
        path.write_text("{not valid json}")
        with pytest.raises(CalibrationProfileLoadError, match="not valid JSON"):
            load_calibration_profile(data_dir=tmp_path, mind_id="default")

    def test_non_object_root_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "default" / "calibration.json"
        path.parent.mkdir(parents=True)
        path.write_text("[1, 2, 3]")
        with pytest.raises(CalibrationProfileLoadError, match="must be a JSON object"):
            load_calibration_profile(data_dir=tmp_path, mind_id="default")

    def test_missing_required_field_raises(self, tmp_path: Path) -> None:
        # schema_version=1 passes the gate, but the rest is missing.
        path = tmp_path / "default" / "calibration.json"
        path.parent.mkdir(parents=True)
        path.write_text('{"schema_version": 1}')
        with pytest.raises(CalibrationProfileLoadError, match="malformed"):
            load_calibration_profile(data_dir=tmp_path, mind_id="default")


# ====================================================================
# LENIENT vs STRICT signature mode
# ====================================================================


class TestSignatureMode:
    """LENIENT default accepts unsigned; STRICT rejects unsigned."""

    def test_lenient_accepts_unsigned(self, tmp_path: Path) -> None:
        save_calibration_profile(_profile(signature=None), data_dir=tmp_path)
        loaded = load_calibration_profile(
            data_dir=tmp_path, mind_id="default", mode=_LoadMode.LENIENT
        )
        assert loaded.signature is None

    def test_strict_rejects_unsigned(self, tmp_path: Path) -> None:
        save_calibration_profile(_profile(signature=None), data_dir=tmp_path)
        with pytest.raises(CalibrationProfileLoadError, match="unsigned"):
            load_calibration_profile(data_dir=tmp_path, mind_id="default", mode=_LoadMode.STRICT)

    def test_strict_accepts_signed(self, tmp_path: Path) -> None:
        save_calibration_profile(_profile(signature="abcdef" * 16), data_dir=tmp_path)
        # Even STRICT accepts when a signature is present (verification
        # against a public key lands in v0.30.17).
        loaded = load_calibration_profile(
            data_dir=tmp_path, mind_id="default", mode=_LoadMode.STRICT
        )
        assert loaded.signature == "abcdef" * 16


# ====================================================================
# Telemetry events (T2.10) -- profile.loaded / profile.persisted
# ====================================================================


class TestPersistenceTelemetry:
    """voice.calibration.profile.* events fire with hashed identifiers."""

    def _capture_logger(self) -> tuple[list[tuple[str, dict[str, object]]], object]:
        from sovyx.voice.calibration import _persistence as persistence_module

        events: list[tuple[str, dict[str, object]]] = []

        class _Capturing:
            def info(self, event: str, **kwargs: object) -> None:
                events.append((event, kwargs))

            def warning(self, event: str, **kwargs: object) -> None:
                events.append((event, kwargs))

        original = persistence_module.logger
        persistence_module.logger = _Capturing()  # type: ignore[assignment]
        return events, original

    def _restore(self, original: object) -> None:
        from sovyx.voice.calibration import _persistence as persistence_module

        persistence_module.logger = original  # type: ignore[assignment]

    def test_persisted_event_uses_hashes(self, tmp_path: Path) -> None:
        events, original = self._capture_logger()
        try:
            save_calibration_profile(_profile(signature=None), data_dir=tmp_path)
        finally:
            self._restore(original)

        persisted = next(e for e in events if e[0] == "voice.calibration.profile.persisted")
        # Hashed identifiers, not raw mind_id / profile_id
        assert "mind_id_hash" in persisted[1]
        assert "profile_id_hash" in persisted[1]
        assert persisted[1]["mind_id_hash"] != "default"

    def test_loaded_event_signature_status_accepted(self, tmp_path: Path) -> None:
        save_calibration_profile(_profile(signature="abcdef" * 16), data_dir=tmp_path)
        events, original = self._capture_logger()
        try:
            load_calibration_profile(data_dir=tmp_path, mind_id="default")
        finally:
            self._restore(original)

        loaded = next(e for e in events if e[0] == "voice.calibration.profile.loaded")
        assert loaded[1]["signature_status"] == "accepted"
        assert loaded[1]["mode"] == "lenient"

    def test_loaded_event_signature_status_missing(self, tmp_path: Path) -> None:
        save_calibration_profile(_profile(signature=None), data_dir=tmp_path)
        events, original = self._capture_logger()
        try:
            load_calibration_profile(data_dir=tmp_path, mind_id="default")
        finally:
            self._restore(original)

        loaded = next(e for e in events if e[0] == "voice.calibration.profile.loaded")
        assert loaded[1]["signature_status"] == "missing"

    def test_rolled_back_event_includes_profile_id_hash(self, tmp_path: Path) -> None:
        """v0.30.26 spec §8.3: rolled_back event carries profile_id_hash."""
        from dataclasses import replace

        from sovyx.voice.calibration import rollback_calibration_profile

        # Save twice -- first becomes the .bak, second the canonical.
        first = _profile(signature=None)
        second = replace(first, profile_id="22222222-2222-3333-4444-555555555555")
        save_calibration_profile(first, data_dir=tmp_path)
        save_calibration_profile(second, data_dir=tmp_path)

        events, original = self._capture_logger()
        try:
            rollback_calibration_profile(data_dir=tmp_path, mind_id="default")
        finally:
            self._restore(original)

        rolled = next(e for e in events if e[0] == "voice.calibration.applier.rolled_back")
        assert "profile_id_hash" in rolled[1]
        assert isinstance(rolled[1]["profile_id_hash"], str)
        assert len(rolled[1]["profile_id_hash"]) == 16
        assert rolled[1]["rollback_reason"] == "operator_initiated"
