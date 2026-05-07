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

    def test_future_schema_version_raises(self, tmp_path: Path) -> None:
        # P5 v0.30.33: v999 > runtime's v1 → migration walker rejects
        # downgrade attempt with a typed CalibrationProfileMigrationError
        # (subclass of CalibrationProfileLoadError; existing surfaces
        # treat both uniformly).
        path = tmp_path / "default" / "calibration.json"
        path.parent.mkdir(parents=True)
        path.write_text('{"schema_version": 999}')
        with pytest.raises(CalibrationProfileLoadError, match="downgrade not supported"):
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

    def test_strict_rejects_invalid_signature(self, tmp_path: Path) -> None:
        # P4 v0.30.32: real Ed25519 verification. ``"abcdef" * 16`` decodes
        # to 72 bytes (not 64) → REJECTED_MALFORMED_SIGNATURE; STRICT
        # raises with verdict in the message. The pre-P4 theater check
        # accepted any non-None signature; that branch no longer exists.
        save_calibration_profile(_profile(signature="abcdef" * 16), data_dir=tmp_path)
        with pytest.raises(CalibrationProfileLoadError, match="signature verification"):
            load_calibration_profile(data_dir=tmp_path, mind_id="default", mode=_LoadMode.STRICT)


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

    def test_loaded_event_signature_status_invalid(self, tmp_path: Path) -> None:
        # P4 v0.30.32: ``"abcdef" * 16`` is malformed (72 bytes after
        # base64 decode, not 64) → ``signature_status="invalid"`` and
        # the new ``signature.invalid`` event fires with the verdict.
        save_calibration_profile(_profile(signature="abcdef" * 16), data_dir=tmp_path)
        events, original = self._capture_logger()
        try:
            load_calibration_profile(data_dir=tmp_path, mind_id="default")
        finally:
            self._restore(original)

        loaded = next(e for e in events if e[0] == "voice.calibration.profile.loaded")
        assert loaded[1]["signature_status"] == "invalid"
        invalid = next(e for e in events if e[0] == "voice.calibration.profile.signature.invalid")
        assert invalid[1]["verdict"] == "rejected_malformed_signature"
        assert invalid[1]["mode"] == "lenient"

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


# ====================================================================
# rc.12 — multi-generation backup chain
# ====================================================================


class TestMultiGenerationBackup:
    """rc.12 (operator-debt P1 from rc.11 final-audit): the pre-rc.12
    single-slot ``.bak`` model lost the original good state when the
    operator calibrated twice with bad results in a row. rc.12 keeps
    a 3-generation rotating chain so up to 3 prior states are
    recoverable."""

    def test_save_rotates_into_bak_1(self, tmp_path: Path) -> None:
        """First non-trivial save: current → .bak.1, no prior chain."""
        from dataclasses import replace

        from sovyx.voice.calibration._persistence import (
            _MAX_BACKUP_GENERATIONS,
            list_calibration_backups,
            profile_backup_path,
        )

        first = _profile(signature=None)
        second = replace(first, profile_id="22222222-2222-3333-4444-555555555555")
        save_calibration_profile(first, data_dir=tmp_path)
        save_calibration_profile(second, data_dir=tmp_path)

        bak1 = profile_backup_path(data_dir=tmp_path, mind_id="default", generation=1)
        bak2 = profile_backup_path(data_dir=tmp_path, mind_id="default", generation=2)
        assert bak1.is_file()
        assert not bak2.is_file()
        backups = list_calibration_backups(data_dir=tmp_path, mind_id="default")
        assert len(backups) == 1
        assert backups[0][0] == 1
        # Sanity: generation N path layout is correct.
        assert _MAX_BACKUP_GENERATIONS == 3

    def test_three_saves_fill_chain(self, tmp_path: Path) -> None:
        """3 saves after the initial one populate .bak.1 / .bak.2 / .bak.3."""
        from dataclasses import replace

        from sovyx.voice.calibration._persistence import (
            list_calibration_backups,
            profile_backup_path,
        )

        first = _profile(signature=None)
        save_calibration_profile(first, data_dir=tmp_path)
        for i in range(3):
            next_p = replace(first, profile_id=f"3333333{i}-2222-3333-4444-555555555555")
            save_calibration_profile(next_p, data_dir=tmp_path)

        for gen in (1, 2, 3):
            assert profile_backup_path(
                data_dir=tmp_path, mind_id="default", generation=gen
            ).is_file()
        backups = list_calibration_backups(data_dir=tmp_path, mind_id="default")
        assert len(backups) == 3

    def test_fourth_save_drops_oldest_generation(self, tmp_path: Path) -> None:
        """Chain is bounded at MAX_BACKUP_GENERATIONS — 4th save drops
        the oldest (.bak.3 deleted, .bak.2 → .bak.3, .bak.1 → .bak.2,
        current → .bak.1). No unbounded disk growth."""
        from dataclasses import replace

        from sovyx.voice.calibration._persistence import list_calibration_backups

        first = _profile(signature=None)
        save_calibration_profile(first, data_dir=tmp_path)
        for i in range(4):
            next_p = replace(first, profile_id=f"4444444{i}-2222-3333-4444-555555555555")
            save_calibration_profile(next_p, data_dir=tmp_path)

        # Still bounded at 3.
        backups = list_calibration_backups(data_dir=tmp_path, mind_id="default")
        assert len(backups) == 3

    def test_rollback_shifts_chain_down(self, tmp_path: Path) -> None:
        """After rollback, .bak.2 becomes .bak.1, .bak.3 becomes .bak.2 —
        operator can roll back AGAIN through the chain."""
        from dataclasses import replace

        from sovyx.voice.calibration import rollback_calibration_profile
        from sovyx.voice.calibration._persistence import list_calibration_backups

        first = _profile(signature=None)
        save_calibration_profile(first, data_dir=tmp_path)
        for i in range(3):
            next_p = replace(first, profile_id=f"5555555{i}-2222-3333-4444-555555555555")
            save_calibration_profile(next_p, data_dir=tmp_path)

        # Pre-rollback: 3 backups available.
        before = list_calibration_backups(data_dir=tmp_path, mind_id="default")
        assert len(before) == 3

        rollback_calibration_profile(data_dir=tmp_path, mind_id="default")

        # Post-rollback: 2 backups remain (chain shifted down).
        after = list_calibration_backups(data_dir=tmp_path, mind_id="default")
        assert len(after) == 2
        # Generations are still numbered 1, 2 (not 2, 3).
        assert {gen for gen, _ in after} == {1, 2}

    def test_rollback_chain_exhaustion_raises(self, tmp_path: Path) -> None:
        """After consuming all 3 backups, the 4th rollback raises with
        a friendly message pointing to --calibrate."""
        from dataclasses import replace

        from sovyx.voice.calibration import rollback_calibration_profile
        from sovyx.voice.calibration._persistence import (
            CalibrationProfileRollbackError,
        )

        first = _profile(signature=None)
        save_calibration_profile(first, data_dir=tmp_path)
        for i in range(3):
            next_p = replace(first, profile_id=f"6666666{i}-2222-3333-4444-555555555555")
            save_calibration_profile(next_p, data_dir=tmp_path)

        # Exhaust the chain.
        for _ in range(3):
            rollback_calibration_profile(data_dir=tmp_path, mind_id="default")

        # 4th rollback: chain empty.
        with pytest.raises(CalibrationProfileRollbackError) as exc_info:
            rollback_calibration_profile(data_dir=tmp_path, mind_id="default")
        assert "exhausted" in str(exc_info.value).lower()

    def test_legacy_bak_migrated_into_chain_on_save(self, tmp_path: Path) -> None:
        """Pre-rc.12 single-slot ``.bak`` is auto-migrated to ``.bak.1``
        when the next save happens. Operators upgrading from rc.11 keep
        their last backup."""
        import shutil
        from dataclasses import replace

        from sovyx.voice.calibration._persistence import (
            _LEGACY_BAK_SUFFIX,
            _PROFILE_FILENAME,
            list_calibration_backups,
        )

        first = _profile(signature=None)
        save_calibration_profile(first, data_dir=tmp_path)
        # Manually fabricate the legacy .bak by copying the current
        # canonical aside (simulating an rc.11-format backup left
        # over after upgrade).
        canonical = tmp_path / "default" / _PROFILE_FILENAME
        legacy_bak = tmp_path / "default" / (_PROFILE_FILENAME + _LEGACY_BAK_SUFFIX)
        shutil.copy(canonical, legacy_bak)

        # Trigger a save — should migrate legacy .bak → .bak.1 BEFORE
        # rotating the current into .bak.1 (so .bak.1 ends up holding
        # the rotated CURRENT, .bak.2 holds the migrated legacy).
        # Actually: the migration runs BEFORE rotation, so legacy → .bak.1
        # then rotation makes .bak.1 → .bak.2 + current → .bak.1.
        second = replace(first, profile_id="77777777-2222-3333-4444-555555555555")
        save_calibration_profile(second, data_dir=tmp_path)

        backups = list_calibration_backups(data_dir=tmp_path, mind_id="default")
        # 2 generations: rotated current at .bak.1, migrated legacy at .bak.2.
        assert len(backups) == 2
        # Legacy file no longer exists (consumed by the migration).
        assert not legacy_bak.is_file()

    def test_legacy_bak_migrated_on_rollback_when_no_chain(self, tmp_path: Path) -> None:
        """Operator upgrades from rc.11 with a legacy .bak in place, has
        not yet calibrated under rc.12, and clicks Rollback. The legacy
        file is migrated transparently and the rollback proceeds."""
        import shutil

        from sovyx.voice.calibration import rollback_calibration_profile
        from sovyx.voice.calibration._persistence import (
            _LEGACY_BAK_SUFFIX,
            _PROFILE_FILENAME,
        )

        first = _profile(signature=None)
        save_calibration_profile(first, data_dir=tmp_path)
        canonical = tmp_path / "default" / _PROFILE_FILENAME
        legacy_bak = tmp_path / "default" / (_PROFILE_FILENAME + _LEGACY_BAK_SUFFIX)
        shutil.copy(canonical, legacy_bak)

        # Replace the canonical with something different so the
        # rollback has to actually restore the legacy content.
        canonical.write_text('{"changed": true}', encoding="utf-8")

        restored = rollback_calibration_profile(data_dir=tmp_path, mind_id="default")
        assert restored == canonical
        # The restored content matches the legacy backup (which was
        # the original valid profile).
        import json

        loaded = json.loads(canonical.read_text(encoding="utf-8"))
        assert loaded.get("profile_id") == first.profile_id

    def test_invalid_generation_raises_value_error(self, tmp_path: Path) -> None:
        """profile_backup_path defends the generation argument."""
        from sovyx.voice.calibration._persistence import profile_backup_path

        with pytest.raises(ValueError, match="generation must be in"):
            profile_backup_path(data_dir=tmp_path, mind_id="default", generation=0)
        with pytest.raises(ValueError, match="generation must be in"):
            profile_backup_path(data_dir=tmp_path, mind_id="default", generation=4)
