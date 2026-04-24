"""Tests for the Mixer KB package (L2.5 Phase F1.C).

Covers:

* Schema validation (pydantic KBProfileModel)
* Loader directory enumeration + skip-on-error behaviour
* Matcher weighted scoring + codec gate + ambiguity detection
* MixerKBLookup integration
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest
import yaml
from pydantic import ValidationError

from sovyx.voice.health import (
    FactorySignature,
    HardwareContext,
    MixerCardSnapshot,
    MixerControlRole,
    MixerControlRoleResolver,
    MixerControlSnapshot,
    MixerKBLookup,
    MixerKBMatch,
    MixerKBProfile,
    MixerPresetControl,
    MixerPresetSpec,
    MixerPresetValueFraction,
    MixerPresetValueRaw,
    ValidationGates,
    VerificationRecord,
)
from sovyx.voice.health._mixer_kb.loader import (
    load_profile_file,
    load_profiles_from_directory,
)
from sovyx.voice.health._mixer_kb.matcher import score_profile
from sovyx.voice.health._mixer_kb.schema import KBProfileModel

_GOOD_YAML = dedent("""
    schema_version: 1
    profile_id: vaio_vjfe69_sn6180
    profile_version: 1
    description: Sony VAIO FE-series with Conexant SN6180.

    codec_id_glob: "14F1:5045"
    driver_family: hda
    system_vendor_glob: "Sony*"
    system_product_glob: "VJFE69*"
    kernel_major_minor_glob: "6.*"
    audio_stack: pipewire
    match_threshold: 0.6

    factory_regime: attenuation
    factory_signature:
      capture_master:
        expected_fraction_range: [0.3, 0.6]
      internal_mic_boost:
        expected_raw_range: [0, 0]

    recommended_preset:
      controls:
        - role: capture_master
          value: {fraction: 1.0}
        - role: internal_mic_boost
          value: {raw: 0}
      auto_mute_mode: disabled
      runtime_pm_target: "on"

    validation:
      rms_dbfs_range: [-30, -15]
      peak_dbfs_max: -2
      snr_db_vocal_band_min: 15
      silero_prob_min: 0.5
      wake_word_stage2_prob_min: 0.4

    verified_on:
      - system_product: "VJFE69F11X-B0221H"
        codec_id: "14F1:5045"
        kernel: "6.14.0-37"
        distro: "linuxmint-22.2"
        verified_at: "2026-04-23"
        verified_by: "sovyx-core-pilot"

    contributed_by: sovyx-core
""").strip()


def _write_profile(dirpath: Path, profile_id: str, body: str | None = None) -> Path:
    """Write a YAML profile named ``<profile_id>.yaml`` under ``dirpath``."""
    yaml_body = body if body is not None else _GOOD_YAML
    if body is None:
        # Rewrite profile_id to match filename for the default fixture.
        yaml_body = yaml_body.replace(
            "profile_id: vaio_vjfe69_sn6180",
            f"profile_id: {profile_id}",
        )
    path = dirpath / f"{profile_id}.yaml"
    path.write_text(yaml_body, encoding="utf-8")
    return path


# ── Schema tests ──────────────────────────────────────────────────────


class TestKBProfileModel:
    """Pydantic schema validates YAML-boundary input."""

    def test_good_yaml_parses(self) -> None:
        data = yaml.safe_load(_GOOD_YAML)
        model = KBProfileModel.model_validate(data)
        assert model.profile_id == "vaio_vjfe69_sn6180"
        assert model.driver_family == "hda"
        assert model.match_threshold == 0.6

    def test_extra_field_rejected(self) -> None:
        data = yaml.safe_load(_GOOD_YAML)
        data["mystery_field"] = "value"
        with pytest.raises(ValidationError, match="mystery_field"):
            KBProfileModel.model_validate(data)

    def test_schema_version_2_rejected(self) -> None:
        data = yaml.safe_load(_GOOD_YAML)
        data["schema_version"] = 2
        with pytest.raises(ValidationError):
            KBProfileModel.model_validate(data)

    def test_missing_required_field(self) -> None:
        data = yaml.safe_load(_GOOD_YAML)
        del data["codec_id_glob"]
        with pytest.raises(ValidationError, match="codec_id_glob"):
            KBProfileModel.model_validate(data)

    def test_profile_id_must_be_snake_case(self) -> None:
        data = yaml.safe_load(_GOOD_YAML)
        data["profile_id"] = "VAIO-vjfe69"  # capitals + dash not allowed
        with pytest.raises(ValidationError):
            KBProfileModel.model_validate(data)

    def test_invalid_driver_family(self) -> None:
        data = yaml.safe_load(_GOOD_YAML)
        data["driver_family"] = "firewire"
        with pytest.raises(ValidationError):
            KBProfileModel.model_validate(data)

    def test_role_unknown_in_factory_signature_rejected(self) -> None:
        data = yaml.safe_load(_GOOD_YAML)
        data["factory_signature"]["unknown"] = {"expected_raw_range": [0, 0]}
        with pytest.raises(ValidationError, match="UNKNOWN"):
            KBProfileModel.model_validate(data)

    def test_invalid_role_name_in_factory_signature(self) -> None:
        data = yaml.safe_load(_GOOD_YAML)
        data["factory_signature"]["bogus_role"] = {"expected_raw_range": [0, 0]}
        with pytest.raises(ValidationError, match="bogus_role"):
            KBProfileModel.model_validate(data)

    def test_factory_signature_empty_rejected(self) -> None:
        data = yaml.safe_load(_GOOD_YAML)
        data["factory_signature"] = {}
        with pytest.raises(ValidationError):
            KBProfileModel.model_validate(data)

    def test_factory_signature_all_none_ranges_rejected(self) -> None:
        data = yaml.safe_load(_GOOD_YAML)
        data["factory_signature"]["capture_master"] = {}
        with pytest.raises(ValidationError, match="at least one"):
            KBProfileModel.model_validate(data)

    def test_preset_value_both_raw_and_fraction_rejected(self) -> None:
        data = yaml.safe_load(_GOOD_YAML)
        data["recommended_preset"]["controls"][0]["value"] = {
            "raw": 80,
            "fraction": 1.0,
        }
        with pytest.raises(ValidationError, match="exactly one"):
            KBProfileModel.model_validate(data)

    def test_preset_role_unknown_rejected(self) -> None:
        data = yaml.safe_load(_GOOD_YAML)
        data["recommended_preset"]["controls"][0]["role"] = "unknown"
        with pytest.raises(ValidationError, match="UNKNOWN"):
            KBProfileModel.model_validate(data)

    def test_preset_role_invalid_rejected(self) -> None:
        data = yaml.safe_load(_GOOD_YAML)
        data["recommended_preset"]["controls"][0]["role"] = "mystery_role"
        with pytest.raises(ValidationError, match="mystery_role"):
            KBProfileModel.model_validate(data)

    def test_validation_gates_silero_out_of_range(self) -> None:
        data = yaml.safe_load(_GOOD_YAML)
        data["validation"]["silero_prob_min"] = 1.5
        with pytest.raises(ValidationError):
            KBProfileModel.model_validate(data)

    def test_verified_on_empty_rejected(self) -> None:
        data = yaml.safe_load(_GOOD_YAML)
        data["verified_on"] = []
        with pytest.raises(ValidationError):
            KBProfileModel.model_validate(data)

    def test_to_profile_produces_dataclass(self) -> None:
        data = yaml.safe_load(_GOOD_YAML)
        model = KBProfileModel.model_validate(data)
        profile = model.to_profile()
        assert isinstance(profile, MixerKBProfile)
        assert profile.profile_id == "vaio_vjfe69_sn6180"
        assert profile.match_threshold == 0.6
        assert MixerControlRole.CAPTURE_MASTER in profile.factory_signature
        assert profile.recommended_preset.auto_mute_mode == "disabled"
        assert profile.recommended_preset.runtime_pm_target == "on"
        assert profile.validation_gates.silero_prob_min == 0.5


# ── Loader tests ──────────────────────────────────────────────────────


class TestLoadProfileFile:
    """load_profile_file: one YAML → one profile or raises."""

    def test_happy_path(self, tmp_path: Path) -> None:
        path = _write_profile(tmp_path, "vaio_vjfe69_sn6180")
        profile = load_profile_file(path)
        assert profile.profile_id == "vaio_vjfe69_sn6180"

    def test_non_mapping_yaml_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.yaml"
        path.write_text("- a\n- b\n", encoding="utf-8")
        with pytest.raises(ValueError, match="mapping at the top"):
            load_profile_file(path)

    def test_malformed_yaml_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "malformed.yaml"
        path.write_text("key: [unclosed", encoding="utf-8")
        with pytest.raises(yaml.YAMLError):
            load_profile_file(path)

    def test_filename_stem_mismatch_raises(self, tmp_path: Path) -> None:
        # profile_id in YAML must match filename stem.
        path = _write_profile(tmp_path, "differently_named")
        # _write_profile rewrites profile_id → "differently_named" by default;
        # now manually override back to disagree with the filename.
        body = path.read_text(encoding="utf-8").replace(
            "profile_id: differently_named",
            "profile_id: vaio_vjfe69_sn6180",
        )
        path.write_text(body, encoding="utf-8")
        with pytest.raises(ValueError, match="disagrees with filename"):
            load_profile_file(path)


class TestLoadProfilesFromDirectory:
    """load_profiles_from_directory: directory enumeration + graceful skip."""

    def test_empty_directory(self, tmp_path: Path) -> None:
        profiles = load_profiles_from_directory(tmp_path)
        assert profiles == []

    def test_missing_directory(self, tmp_path: Path) -> None:
        profiles = load_profiles_from_directory(tmp_path / "does_not_exist")
        assert profiles == []

    def test_directory_is_a_file(self, tmp_path: Path) -> None:
        file_path = tmp_path / "not_a_dir.yaml"
        file_path.write_text(_GOOD_YAML, encoding="utf-8")
        profiles = load_profiles_from_directory(file_path)
        assert profiles == []

    def test_loads_multiple_and_sorts(self, tmp_path: Path) -> None:
        _write_profile(tmp_path, "zzz_last")
        _write_profile(tmp_path, "aaa_first")
        _write_profile(tmp_path, "mmm_middle")
        profiles = load_profiles_from_directory(tmp_path)
        ids = [p.profile_id for p in profiles]
        assert ids == ["aaa_first", "mmm_middle", "zzz_last"]

    def test_skips_underscore_prefixed_files(self, tmp_path: Path) -> None:
        _write_profile(tmp_path, "valid_profile")
        # Index file — must NOT be loaded as a profile.
        (tmp_path / "_index.yaml").write_text("# reserved\n", encoding="utf-8")
        profiles = load_profiles_from_directory(tmp_path)
        assert [p.profile_id for p in profiles] == ["valid_profile"]

    def test_skips_malformed_file_keeps_others(self, tmp_path: Path) -> None:
        _write_profile(tmp_path, "good_profile")
        bad = tmp_path / "bad_profile.yaml"
        bad.write_text("key: [unclosed", encoding="utf-8")
        profiles = load_profiles_from_directory(tmp_path)
        assert [p.profile_id for p in profiles] == ["good_profile"]

    def test_skips_schema_invalid_keeps_others(self, tmp_path: Path) -> None:
        _write_profile(tmp_path, "good_profile")
        invalid_body = _GOOD_YAML.replace(
            "profile_id: vaio_vjfe69_sn6180",
            "profile_id: invalid_profile\nmystery_field: x",
        )
        _write_profile(tmp_path, "invalid_profile", body=invalid_body)
        profiles = load_profiles_from_directory(tmp_path)
        assert [p.profile_id for p in profiles] == ["good_profile"]


# ── Matcher tests ─────────────────────────────────────────────────────


def _pilot_profile() -> MixerKBProfile:
    """Build the pilot VAIO VJFE69 SN6180 profile programmatically."""
    return MixerKBProfile(
        profile_id="vaio_vjfe69_sn6180",
        profile_version=1,
        schema_version=1,
        codec_id_glob="14F1:5045",
        driver_family="hda",
        system_vendor_glob="Sony*",
        system_product_glob="VJFE69*",
        distro_family=None,
        audio_stack="pipewire",
        kernel_major_minor_glob="6.*",
        match_threshold=0.6,
        factory_regime="attenuation",
        factory_signature={
            MixerControlRole.CAPTURE_MASTER: FactorySignature(
                expected_raw_range=None,
                expected_fraction_range=(0.3, 0.6),
                expected_db_range=None,
            ),
            MixerControlRole.INTERNAL_MIC_BOOST: FactorySignature(
                expected_raw_range=(0, 0),
                expected_fraction_range=None,
                expected_db_range=None,
            ),
        },
        recommended_preset=MixerPresetSpec(
            controls=(
                MixerPresetControl(
                    role=MixerControlRole.CAPTURE_MASTER,
                    value=MixerPresetValueFraction(fraction=1.0),
                ),
                MixerPresetControl(
                    role=MixerControlRole.INTERNAL_MIC_BOOST,
                    value=MixerPresetValueRaw(raw=0),
                ),
            ),
        ),
        validation_gates=ValidationGates(
            rms_dbfs_range=(-30.0, -15.0),
            peak_dbfs_max=-2.0,
            snr_db_vocal_band_min=15.0,
            silero_prob_min=0.5,
            wake_word_stage2_prob_min=0.4,
        ),
        verified_on=(
            VerificationRecord(
                system_product="VJFE69F11X-B0221H",
                codec_id="14F1:5045",
                kernel="6.14.0-37",
                distro="linuxmint-22.2",
                verified_at="2026-04-23",
                verified_by="sovyx-core-pilot",
            ),
        ),
        contributed_by="sovyx-core",
    )


def _control(name: str, *, current_raw: int, max_raw: int) -> MixerControlSnapshot:
    return MixerControlSnapshot(
        name=name,
        min_raw=0,
        max_raw=max_raw,
        current_raw=current_raw,
        current_db=None,
        max_db=None,
        is_boost_control=False,
        saturation_risk=False,
        asymmetric=False,
    )


def _card_with_controls(controls: tuple[MixerControlSnapshot, ...]) -> MixerCardSnapshot:
    return MixerCardSnapshot(
        card_index=0,
        card_id="Generic",
        card_longname="HDA Intel PCH",
        controls=controls,
        aggregated_boost_db=0.0,
        saturation_warning=False,
    )


def _pilot_factory_snapshot() -> MixerCardSnapshot:
    """Mixer state matching the pilot's factory-attenuation signature.

    Capture: 40/80 = 0.5 fraction (inside 0.3-0.6 range).
    Internal Mic Boost: raw=0 (inside 0-0 range).
    """
    return _card_with_controls(
        (
            _control("Capture", current_raw=40, max_raw=80),
            _control("Internal Mic Boost", current_raw=0, max_raw=3),
        ),
    )


def _healthy_snapshot() -> MixerCardSnapshot:
    """Mixer state that does NOT match pilot's factory-bad signature."""
    return _card_with_controls(
        (
            _control("Capture", current_raw=64, max_raw=80),  # 0.8 — outside 0.3-0.6
            _control("Internal Mic Boost", current_raw=2, max_raw=3),  # raw=2 outside 0-0
        ),
    )


class TestScoreProfile:
    """score_profile: weighted scoring with codec gate + factory signature."""

    def test_codec_mismatch_returns_zero(self) -> None:
        profile = _pilot_profile()
        hw = HardwareContext(driver_family="hda", codec_id="10EC:0257")
        resolver = MixerControlRoleResolver()
        score, breakdown = score_profile(
            profile,
            hw,
            [_pilot_factory_snapshot()],
            resolver,
        )
        assert score == 0.0
        assert breakdown == (("codec_id", 0.0, 0.4),)

    def test_codec_none_returns_zero(self) -> None:
        profile = _pilot_profile()
        hw = HardwareContext(driver_family="hda", codec_id=None)
        resolver = MixerControlRoleResolver()
        score, _ = score_profile(profile, hw, [_pilot_factory_snapshot()], resolver)
        assert score == 0.0

    def test_full_match_pilot(self) -> None:
        # Every declared field matches — score should be 1.0.
        profile = _pilot_profile()
        hw = HardwareContext(
            driver_family="hda",
            codec_id="14F1:5045",
            system_vendor="Sony Group Corporation",
            system_product="VJFE69F11X-B0221H",
            distro="linuxmint-22.2",
            audio_stack="pipewire",
            kernel="6.14.0-37-generic",
        )
        resolver = MixerControlRoleResolver()
        score, breakdown = score_profile(
            profile,
            hw,
            [_pilot_factory_snapshot()],
            resolver,
        )
        assert score == pytest.approx(1.0)
        fields_hit = {name for name, _, _ in breakdown}
        assert fields_hit == {
            "codec_id",
            "driver_family",
            "system_vendor",
            "system_product",
            "audio_stack",
            "kernel_mm",
            "factory_sig",
        }

    def test_codec_match_without_factory_sig_is_hard_gated(self) -> None:
        """Paranoid-QA CRITICAL #10 — factory_signature is a HARD gate.

        Codec matches, driver mismatches, and factory_sig scores 0
        (no signature role matches the healthy snapshot). Previously
        the weighted algorithm gave score≈0.8 — high enough to apply
        the preset on healthy hardware. Now the hard gate returns
        exactly 0.0 so the lookup filters the profile out.
        """
        profile = _pilot_profile()
        hw = HardwareContext(
            driver_family="usb-audio",  # != profile.driver_family=hda
            codec_id="14F1:5045",
        )
        resolver = MixerControlRoleResolver()
        score, breakdown = score_profile(
            profile,
            hw,
            [_healthy_snapshot()],  # factory_sig won't match
            resolver,
        )
        assert score == 0.0
        fields_hit = {name for name, _, _ in breakdown}
        # codec_id entry is present (with value 1.0) but factory_sig
        # forced the hard gate — both appear in the breakdown.
        assert "codec_id" in fields_hit
        assert "factory_sig" in fields_hit
        # factory_sig entry has score 0 — the hard-gate signal.
        sig_entry = next(b for b in breakdown if b[0] == "factory_sig")
        assert sig_entry[1] == 0.0

    def test_factory_sig_fraction_match(self) -> None:
        profile = _pilot_profile()
        hw = HardwareContext(driver_family="hda", codec_id="14F1:5045")
        resolver = MixerControlRoleResolver()
        _, breakdown = score_profile(
            profile,
            hw,
            [_pilot_factory_snapshot()],
            resolver,
        )
        sig_entry = next(b for b in breakdown if b[0] == "factory_sig")
        # Both roles match → sig_score = 2/2 = 1.0
        assert sig_entry[1] == pytest.approx(1.0)

    def test_factory_sig_partial_match(self) -> None:
        profile = _pilot_profile()
        hw = HardwareContext(driver_family="hda", codec_id="14F1:5045")
        resolver = MixerControlRoleResolver()
        # Capture matches (0.5 frac), Internal Mic Boost does NOT (raw=2).
        mixed = _card_with_controls(
            (
                _control("Capture", current_raw=40, max_raw=80),
                _control("Internal Mic Boost", current_raw=2, max_raw=3),
            ),
        )
        _, breakdown = score_profile(profile, hw, [mixed], resolver)
        sig_entry = next(b for b in breakdown if b[0] == "factory_sig")
        assert sig_entry[1] == pytest.approx(0.5)  # 1/2

    def test_codec_glob_wildcard(self) -> None:
        # fnmatch handles *, ?, [seq] — verify glob with wildcard.
        profile = MixerKBProfile(
            profile_id="glob_test",
            profile_version=1,
            schema_version=1,
            codec_id_glob="14F1:*",  # matches any Conexant
            driver_family="hda",
            system_vendor_glob=None,
            system_product_glob=None,
            distro_family=None,
            audio_stack=None,
            kernel_major_minor_glob=None,
            match_threshold=0.5,
            factory_regime="attenuation",
            factory_signature={
                MixerControlRole.CAPTURE_MASTER: FactorySignature(
                    expected_raw_range=None,
                    expected_fraction_range=(0.0, 1.0),
                    expected_db_range=None,
                ),
            },
            recommended_preset=MixerPresetSpec(
                controls=(
                    MixerPresetControl(
                        role=MixerControlRole.CAPTURE_MASTER,
                        value=MixerPresetValueFraction(fraction=1.0),
                    ),
                ),
            ),
            validation_gates=ValidationGates(
                rms_dbfs_range=(-30.0, -15.0),
                peak_dbfs_max=-2.0,
                snr_db_vocal_band_min=15.0,
                silero_prob_min=0.5,
                wake_word_stage2_prob_min=0.4,
            ),
            verified_on=(
                VerificationRecord(
                    system_product="X",
                    codec_id="14F1:5045",
                    kernel="6.0",
                    distro="x",
                    verified_at="2026-04-23",
                    verified_by="x",
                ),
            ),
            contributed_by="x",
        )
        hw = HardwareContext(driver_family="hda", codec_id="14F1:5046")
        resolver = MixerControlRoleResolver()
        score, _ = score_profile(profile, hw, [_pilot_factory_snapshot()], resolver)
        assert score > 0.0


# ── MixerKBLookup integration ─────────────────────────────────────────


class TestMixerKBLookup:
    """Integration: profiles + resolver → match result."""

    def test_empty_lookup_returns_none(self) -> None:
        resolver = MixerControlRoleResolver()
        lookup = MixerKBLookup([], resolver=resolver)
        hw = HardwareContext(driver_family="hda", codec_id="14F1:5045")
        assert lookup.match(hw, [_pilot_factory_snapshot()]) is None

    def test_single_match_above_threshold(self) -> None:
        resolver = MixerControlRoleResolver()
        lookup = MixerKBLookup([_pilot_profile()], resolver=resolver)
        hw = HardwareContext(
            driver_family="hda",
            codec_id="14F1:5045",
            system_vendor="Sony Group Corporation",
            system_product="VJFE69F11X-B0221H",
            audio_stack="pipewire",
            kernel="6.14.0-37",
        )
        result = lookup.match(hw, [_pilot_factory_snapshot()])
        assert result is not None
        assert isinstance(result, MixerKBMatch)
        assert result.profile.profile_id == "vaio_vjfe69_sn6180"
        assert result.score >= 0.6
        assert result.is_user_contributed is False

    def test_below_threshold_returns_none(self) -> None:
        resolver = MixerControlRoleResolver()
        lookup = MixerKBLookup([_pilot_profile()], resolver=resolver)
        # Codec mismatch → score 0 → below threshold.
        hw = HardwareContext(driver_family="hda", codec_id="10EC:0257")
        assert lookup.match(hw, [_pilot_factory_snapshot()]) is None

    def test_min_score_override(self) -> None:
        resolver = MixerControlRoleResolver()
        # Profile with tight match_threshold=0.95 + partially-matching
        # factory_signature → actual score lands ~0.92, so default
        # threshold rejects but min_score=0.5 override admits.
        #
        # Factory signature: CAPTURE_MASTER range won't match (probe
        # reads 40/80=0.5 fraction; range demands 0.95-1.0), while
        # INTERNAL_MIC_BOOST does match (raw=0, range=0-0). sig_score
        # = 1/2 = 0.5. Score = (0.4 + 0.1 + 0.05) / 0.6 = 0.9166.
        tight = MixerKBProfile(
            profile_id="tight_profile",
            profile_version=1,
            schema_version=1,
            codec_id_glob="14F1:5045",
            driver_family="hda",
            system_vendor_glob=None,
            system_product_glob=None,
            distro_family=None,
            audio_stack=None,
            kernel_major_minor_glob=None,
            match_threshold=0.95,
            factory_regime="attenuation",
            factory_signature={
                MixerControlRole.CAPTURE_MASTER: FactorySignature(
                    expected_raw_range=None,
                    expected_fraction_range=(0.95, 1.0),  # won't match 0.5
                    expected_db_range=None,
                ),
                MixerControlRole.INTERNAL_MIC_BOOST: FactorySignature(
                    expected_raw_range=(0, 0),  # will match raw=0
                    expected_fraction_range=None,
                    expected_db_range=None,
                ),
            },
            recommended_preset=MixerPresetSpec(
                controls=(
                    MixerPresetControl(
                        role=MixerControlRole.CAPTURE_MASTER,
                        value=MixerPresetValueFraction(fraction=1.0),
                    ),
                ),
            ),
            validation_gates=ValidationGates(
                rms_dbfs_range=(-30.0, -15.0),
                peak_dbfs_max=-2.0,
                snr_db_vocal_band_min=15.0,
                silero_prob_min=0.5,
                wake_word_stage2_prob_min=0.4,
            ),
            verified_on=(
                VerificationRecord(
                    system_product="x",
                    codec_id="14F1:5045",
                    kernel="6.0",
                    distro="x",
                    verified_at="2026-04-23",
                    verified_by="x",
                ),
            ),
            contributed_by="x",
        )
        lookup = MixerKBLookup([tight], resolver=resolver)
        hw = HardwareContext(driver_family="hda", codec_id="14F1:5045")
        # Sanity: confirm the computed score is in the expected window.
        score, _ = score_profile(
            tight,
            hw,
            [_pilot_factory_snapshot()],
            resolver,
        )
        assert 0.90 < score < 0.95  # noqa: PLR2004 — scoring algorithm invariant
        # At profile.match_threshold=0.95 → score=0.9166 is below → None.
        assert lookup.match(hw, [_pilot_factory_snapshot()]) is None
        # Override to 0.5 → admits.
        match = lookup.match(hw, [_pilot_factory_snapshot()], min_score=0.5)
        assert match is not None
        assert match.profile.profile_id == "tight_profile"

    def test_ambiguous_match_returns_none(self) -> None:
        resolver = MixerControlRoleResolver()
        # Two near-identical profiles — only differ by profile_id.
        p1 = _pilot_profile()
        p2_data = {
            "profile_id": "sibling_profile",
            "profile_version": 1,
            "schema_version": 1,
            "codec_id_glob": "14F1:5045",
            "driver_family": "hda",
            "system_vendor_glob": "Sony*",
            "system_product_glob": "VJFE69*",
            "distro_family": None,
            "audio_stack": "pipewire",
            "kernel_major_minor_glob": "6.*",
            "match_threshold": 0.6,
            "factory_regime": "attenuation",
            "factory_signature": {
                MixerControlRole.CAPTURE_MASTER: FactorySignature(
                    expected_raw_range=None,
                    expected_fraction_range=(0.3, 0.6),
                    expected_db_range=None,
                ),
                MixerControlRole.INTERNAL_MIC_BOOST: FactorySignature(
                    expected_raw_range=(0, 0),
                    expected_fraction_range=None,
                    expected_db_range=None,
                ),
            },
            "recommended_preset": MixerPresetSpec(
                controls=(
                    MixerPresetControl(
                        role=MixerControlRole.CAPTURE_MASTER,
                        value=MixerPresetValueFraction(fraction=1.0),
                    ),
                ),
            ),
            "validation_gates": ValidationGates(
                rms_dbfs_range=(-30.0, -15.0),
                peak_dbfs_max=-2.0,
                snr_db_vocal_band_min=15.0,
                silero_prob_min=0.5,
                wake_word_stage2_prob_min=0.4,
            ),
            "verified_on": (
                VerificationRecord(
                    system_product="x",
                    codec_id="14F1:5045",
                    kernel="6.0",
                    distro="x",
                    verified_at="2026-04-23",
                    verified_by="x",
                ),
            ),
            "contributed_by": "x",
        }
        p2 = MixerKBProfile(**p2_data)  # type: ignore[arg-type]
        lookup = MixerKBLookup([p1, p2], resolver=resolver)
        hw = HardwareContext(
            driver_family="hda",
            codec_id="14F1:5045",
            system_vendor="Sony",
            system_product="VJFE69F11X",
            audio_stack="pipewire",
            kernel="6.14.0",
        )
        # Both score ~identical → ambiguous → None.
        result = lookup.match(hw, [_pilot_factory_snapshot()])
        assert result is None

    def test_clear_winner_no_ambiguity(self) -> None:
        resolver = MixerControlRoleResolver()
        # p1 matches strongly, p2 only via codec.
        p1 = _pilot_profile()
        p2 = MixerKBProfile(
            **{
                "profile_id": "distant_profile",
                "profile_version": 1,
                "schema_version": 1,
                "codec_id_glob": "14F1:*",  # still matches
                "driver_family": "usb-audio",  # mismatches
                "system_vendor_glob": "Dell*",  # mismatches
                "system_product_glob": None,
                "distro_family": None,
                "audio_stack": None,
                "kernel_major_minor_glob": None,
                "match_threshold": 0.5,
                "factory_regime": "saturation",
                "factory_signature": {
                    MixerControlRole.CAPTURE_MASTER: FactorySignature(
                        expected_raw_range=None,
                        expected_fraction_range=(0.95, 1.0),  # doesn't match 0.5
                        expected_db_range=None,
                    ),
                },
                "recommended_preset": MixerPresetSpec(
                    controls=(
                        MixerPresetControl(
                            role=MixerControlRole.CAPTURE_MASTER,
                            value=MixerPresetValueFraction(fraction=0.6),
                        ),
                    ),
                ),
                "validation_gates": ValidationGates(
                    rms_dbfs_range=(-30.0, -15.0),
                    peak_dbfs_max=-2.0,
                    snr_db_vocal_band_min=15.0,
                    silero_prob_min=0.5,
                    wake_word_stage2_prob_min=0.4,
                ),
                "verified_on": (
                    VerificationRecord(
                        system_product="x",
                        codec_id="14F1:5045",
                        kernel="6.0",
                        distro="x",
                        verified_at="2026-04-23",
                        verified_by="x",
                    ),
                ),
                "contributed_by": "x",
            },  # type: ignore[arg-type]
        )
        lookup = MixerKBLookup([p1, p2], resolver=resolver)
        hw = HardwareContext(
            driver_family="hda",
            codec_id="14F1:5045",
            system_vendor="Sony",
            system_product="VJFE69F11X",
            audio_stack="pipewire",
            kernel="6.14.0",
        )
        result = lookup.match(hw, [_pilot_factory_snapshot()])
        assert result is not None
        assert result.profile.profile_id == "vaio_vjfe69_sn6180"

    def test_load_shipped_works_on_empty_profiles_dir(self) -> None:
        # Phase F1 ships empty profiles/ — load_shipped should return
        # an empty lookup without raising.
        resolver = MixerControlRoleResolver()
        lookup = MixerKBLookup.load_shipped(resolver=resolver)
        assert isinstance(lookup, MixerKBLookup)
        # F1 ships empty, but if future phases add profiles, this
        # stays a smoke test not a count assertion.
        hw = HardwareContext(driver_family="hda", codec_id="DEAD:BEEF")
        result = lookup.match(hw, [])
        assert result is None

    def test_user_contributed_tagged(self, tmp_path: Path) -> None:
        resolver = MixerControlRoleResolver()
        _write_profile(tmp_path, "vaio_vjfe69_sn6180")
        lookup = MixerKBLookup.load_shipped_and_user(tmp_path, resolver=resolver)
        hw = HardwareContext(
            driver_family="hda",
            codec_id="14F1:5045",
            system_vendor="Sony Group",
            system_product="VJFE69F11X",
            audio_stack="pipewire",
            kernel="6.14.0",
        )
        result = lookup.match(hw, [_pilot_factory_snapshot()])
        assert result is not None
        assert result.is_user_contributed is True

    def test_load_shipped_and_user_missing_user_dir(self, tmp_path: Path) -> None:
        resolver = MixerControlRoleResolver()
        lookup = MixerKBLookup.load_shipped_and_user(
            tmp_path / "does_not_exist",
            resolver=resolver,
        )
        assert lookup.user_contributed_profiles == ()
