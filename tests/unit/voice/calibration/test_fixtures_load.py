"""Sanity-load every JSON fixture under tests/fixtures/voice-calibration/.

Validates that the committed fixtures stay byte-compatible with the
current schema dataclasses (anti-pattern guard: a schema_version bump
+ stale fixture would silently regress regression coverage).

Coverage:

* fingerprint_vaio_pipewire.json -> HardwareFingerprint
* fingerprint_thinkpad_pulse.json -> HardwareFingerprint
* measurement_attenuated.json -> MeasurementSnapshot
* measurement_clean.json -> MeasurementSnapshot
* profile_v1_signed.json -> CalibrationProfile (full round-trip via
  load_calibration_profile after copying into tmp_path/<mind_id>/)
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from sovyx.voice.calibration import (
    CalibrationProfile,
    HardwareFingerprint,
    MeasurementSnapshot,
    load_calibration_profile,
)
from sovyx.voice.calibration._persistence import _profile_from_dict

_FIXTURES = Path(__file__).resolve().parents[4] / "tests" / "fixtures" / "voice-calibration"


class TestFingerprintFixtures:
    def test_vaio_pipewire(self) -> None:
        raw = json.loads((_FIXTURES / "fingerprint_vaio_pipewire.json").read_text())
        fp = HardwareFingerprint(**raw)
        assert fp.audio_stack == "pipewire"
        assert fp.system_vendor == "Sony"
        assert fp.codec_id == "10ec:0257"

    def test_thinkpad_pulse(self) -> None:
        raw = json.loads((_FIXTURES / "fingerprint_thinkpad_pulse.json").read_text())
        # JSON arrays must convert to tuples to match the frozen dataclass.
        raw["capture_devices"] = tuple(raw["capture_devices"])
        raw["hal_interceptors"] = tuple(raw["hal_interceptors"])
        raw["pulse_modules_destructive"] = tuple(raw["pulse_modules_destructive"])
        fp = HardwareFingerprint(**raw)
        assert fp.audio_stack == "pulseaudio"
        assert "module-echo-cancel" in fp.pulse_modules_destructive


class TestMeasurementFixtures:
    def test_attenuated(self) -> None:
        raw = json.loads((_FIXTURES / "measurement_attenuated.json").read_text())
        raw["rms_dbfs_per_capture"] = tuple(raw["rms_dbfs_per_capture"])
        m = MeasurementSnapshot(**raw)
        assert m.mixer_attenuation_regime == "attenuated"
        assert m.triage_winner_hid == "H10"

    def test_clean(self) -> None:
        raw = json.loads((_FIXTURES / "measurement_clean.json").read_text())
        raw["rms_dbfs_per_capture"] = tuple(raw["rms_dbfs_per_capture"])
        m = MeasurementSnapshot(**raw)
        assert m.mixer_attenuation_regime == "healthy"
        assert m.triage_winner_hid is None


class TestSignedProfileRoundTrip:
    def test_profile_loads_via_persistence_path(self, tmp_path: Path) -> None:
        # Copy the fixture into <tmp_path>/<mind_id>/calibration.json
        # so load_calibration_profile finds it via its conventional path.
        raw = json.loads((_FIXTURES / "profile_v1_signed.json").read_text())
        mind_id = raw["mind_id"]
        target_dir = tmp_path / mind_id
        target_dir.mkdir()
        shutil.copy(_FIXTURES / "profile_v1_signed.json", target_dir / "calibration.json")

        loaded = load_calibration_profile(data_dir=tmp_path, mind_id=mind_id)
        assert isinstance(loaded, CalibrationProfile)
        assert loaded.profile_id == "11111111-2222-3333-4444-555555555555"
        assert loaded.signature is not None
        assert len(loaded.signature) == 64
        # Decision tuple round-trips with rule_id intact.
        assert loaded.decisions[0].rule_id == "R10_mic_attenuated"

    def test_profile_dict_to_dataclass(self) -> None:
        raw = json.loads((_FIXTURES / "profile_v1_signed.json").read_text())
        profile = _profile_from_dict(raw)
        assert profile.generated_by_rule_set_version == 10
