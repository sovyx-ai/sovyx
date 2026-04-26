"""Tests for the Conexant SN6180 first-party KB profile (Step 8).

The first signed first-party profile shipped by Sovyx. Built from HIL
evidence captured during the v0.22.2 pilot run that motivated the
entire L2.5 KB cascade refactor (see
``MISSION-voice-mixer-enterprise-refactor-2026-04-25.md`` §1.1).

These tests pin the contract:

* The profile YAML loads cleanly via the schema validator.
* Signature verifies against the shipped trusted public key
  (``_trusted_keys/v1.pub``).
* The recommended preset matches the values established by the pilot
  (Capture = 50 % fraction, Mic Boost = 33 %, Internal Mic Boost = 33 %).
* Validation gates align with the pilot's measured RMS / Silero
  envelope.
* The loader's `KBSignatureVerifier` integration accepts the
  profile in both LENIENT and STRICT modes.

Reference: MISSION-voice-100pct-autonomous-2026-04-25.md step 8.
"""

from __future__ import annotations

from pathlib import Path

from sovyx.voice.health._mixer_kb._signing import (
    KBSignatureVerifier,
    Mode,
    VerifyResult,
    load_trusted_public_key,
)
from sovyx.voice.health._mixer_kb.loader import load_profile_file

PROFILE_PATH = (
    Path(__file__).resolve().parents[5]
    / "src"
    / "sovyx"
    / "voice"
    / "health"
    / "_mixer_kb"
    / "profiles"
    / "conexant_sn6180_vaio_vjfe69.yaml"
)


class TestProfileFileShipped:
    def test_yaml_present(self) -> None:
        assert PROFILE_PATH.is_file(), f"Step 8 must ship profile at {PROFILE_PATH}"

    def test_yaml_carries_signature_field(self) -> None:
        text = PROFILE_PATH.read_text(encoding="utf-8")
        # Signature line is added by sign_kb_profile.py at the bottom of the YAML.
        assert "signature:" in text, "Step 7 sign tool must have populated the signature field"


class TestProfileLoadsViaSchema:
    def test_loads_without_signature_verification(self) -> None:
        """The legacy F1 path (verifier=None) must still work."""
        profile = load_profile_file(PROFILE_PATH)
        assert profile.profile_id == "conexant_sn6180_vaio_vjfe69"
        assert profile.profile_version == 1
        assert profile.driver_family == "hda"
        assert profile.codec_id_glob == "*14F1:5045*"

    def test_loads_with_lenient_verification_accepted(self) -> None:
        verifier = KBSignatureVerifier(load_trusted_public_key(), mode=Mode.LENIENT)
        profile = load_profile_file(PROFILE_PATH, verifier=verifier)
        assert profile.profile_id == "conexant_sn6180_vaio_vjfe69"

    def test_loads_with_strict_verification_accepted(self) -> None:
        """STRICT mode must succeed — the profile is signed with the dev key."""
        verifier = KBSignatureVerifier(load_trusted_public_key(), mode=Mode.STRICT)
        profile = load_profile_file(PROFILE_PATH, verifier=verifier)
        assert profile.profile_id == "conexant_sn6180_vaio_vjfe69"


class TestSignatureRoundTrip:
    def test_signature_verifies_against_trusted_key(self) -> None:
        import yaml

        verifier = KBSignatureVerifier(load_trusted_public_key(), mode=Mode.LENIENT)
        profile_dict = yaml.safe_load(PROFILE_PATH.read_text(encoding="utf-8"))
        verdict = verifier.verify(profile_dict)
        assert verdict is VerifyResult.ACCEPTED


class TestRecommendedPreset:
    """Pin the load-bearing preset values from the pilot run."""

    def test_capture_master_at_half(self) -> None:
        from sovyx.voice.health.contract import (
            MixerControlRole,
            MixerPresetValueFraction,
        )

        profile = load_profile_file(PROFILE_PATH)
        for control in profile.recommended_preset.controls:
            if control.role is MixerControlRole.CAPTURE_MASTER:
                assert isinstance(control.value, MixerPresetValueFraction)
                assert control.value.fraction == 0.5
                return
        msg = "capture_master role missing from recommended_preset"
        raise AssertionError(msg)

    def test_internal_mic_boost_at_one_third(self) -> None:
        from sovyx.voice.health.contract import (
            MixerControlRole,
            MixerPresetValueFraction,
        )

        profile = load_profile_file(PROFILE_PATH)
        for control in profile.recommended_preset.controls:
            if control.role is MixerControlRole.INTERNAL_MIC_BOOST:
                assert isinstance(control.value, MixerPresetValueFraction)
                assert abs(control.value.fraction - 0.33) < 0.01
                return
        msg = "internal_mic_boost role missing from recommended_preset"
        raise AssertionError(msg)

    def test_preamp_boost_at_one_third(self) -> None:
        from sovyx.voice.health.contract import (
            MixerControlRole,
            MixerPresetValueFraction,
        )

        profile = load_profile_file(PROFILE_PATH)
        for control in profile.recommended_preset.controls:
            if control.role is MixerControlRole.PREAMP_BOOST:
                assert isinstance(control.value, MixerPresetValueFraction)
                assert abs(control.value.fraction - 0.33) < 0.01
                return
        msg = "preamp_boost role missing from recommended_preset"
        raise AssertionError(msg)


class TestValidationGates:
    """The post-apply gates must reflect the pilot's measured envelope."""

    def test_rms_dbfs_range_brackets_minus_24_dbfs(self) -> None:
        profile = load_profile_file(PROFILE_PATH)
        lo, hi = profile.validation_gates.rms_dbfs_range
        # The pilot's measured baseline at the recommended preset
        # was -24 dBFS ± 6. The gate must include that target.
        assert lo <= -24.0 <= hi

    def test_silero_min_at_least_50_pct(self) -> None:
        profile = load_profile_file(PROFILE_PATH)
        assert profile.validation_gates.silero_prob_min >= 0.5

    def test_peak_below_clipping(self) -> None:
        profile = load_profile_file(PROFILE_PATH)
        assert profile.validation_gates.peak_dbfs_max <= -3.0


class TestProvenance:
    def test_verified_on_includes_pilot_host(self) -> None:
        profile = load_profile_file(PROFILE_PATH)
        assert len(profile.verified_on) >= 1
        first = profile.verified_on[0]
        # The pilot host's product is the load-bearing identity for
        # downstream debugging — pin it.
        assert "VJFE69" in first.system_product

    def test_contributed_by_set(self) -> None:
        profile = load_profile_file(PROFILE_PATH)
        assert profile.contributed_by != ""
