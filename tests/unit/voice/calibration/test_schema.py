"""Unit tests for sovyx.voice.calibration.schema.

Invariants exercised:

* All dataclasses are ``frozen=True`` (assignment raises FrozenInstanceError)
* All dataclasses use ``slots=True`` (no per-instance __dict__)
* :attr:`HardwareFingerprint.fingerprint_hash` is deterministic (same
  inputs -> same hex digest) and stable across tuple-order shuffles
  in capture_devices / hal_interceptors / pulse_modules_destructive
* :attr:`HardwareFingerprint.fingerprint_hash` excludes ``schema_version``
  and ``captured_at_utc`` (those are metadata, not identity)
* :attr:`CalibrationProfile.applicable_decisions` filters correctly:
  only ``operation == "set"`` AND ``confidence != EXPERIMENTAL``
* :meth:`CalibrationProfile.canonical_signing_payload` excludes the
  ``signature`` field (so signing covers payload not the signature itself)
* :class:`CalibrationConfidence` is a closed StrEnum with exactly
  4 members so OTel cardinality stays bounded
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from sovyx.voice.calibration import (
    CALIBRATION_PROFILE_SCHEMA_VERSION,
    HARDWARE_FINGERPRINT_SCHEMA_VERSION,
    MEASUREMENT_SNAPSHOT_SCHEMA_VERSION,
    CalibrationConfidence,
    CalibrationDecision,
    CalibrationProfile,
    HardwareFingerprint,
    MeasurementSnapshot,
    ProvenanceTrace,
)

# ====================================================================
# Fixtures
# ====================================================================


def _make_fingerprint(**overrides: object) -> HardwareFingerprint:
    base: dict[str, object] = {
        "schema_version": HARDWARE_FINGERPRINT_SCHEMA_VERSION,
        "captured_at_utc": "2026-05-05T18:00:00Z",
        "distro_id": "linuxmint",
        "distro_id_like": "debian",
        "kernel_release": "6.8.0-50-generic",
        "kernel_major_minor": "6.8",
        "cpu_model": "Intel Core i5-1240P",
        "cpu_cores": 12,
        "ram_mb": 16384,
        "has_gpu": False,
        "gpu_vram_mb": 0,
        "audio_stack": "pipewire",
        "pipewire_version": "1.0.5",
        "pulseaudio_version": None,
        "alsa_lib_version": "1.2.10",
        "codec_id": "10ec:0257",
        "driver_family": "hda",
        "system_vendor": "Sony",
        "system_product": "VJFE69F11X-B0221H",
        "capture_card_count": 1,
        "capture_devices": ("Internal Mic",),
        "apo_active": False,
        "apo_name": None,
        "hal_interceptors": (),
        "pulse_modules_destructive": (),
    }
    base.update(overrides)
    return HardwareFingerprint(**base)  # type: ignore[arg-type]


def _make_measurements(**overrides: object) -> MeasurementSnapshot:
    base: dict[str, object] = {
        "schema_version": MEASUREMENT_SNAPSHOT_SCHEMA_VERSION,
        "captured_at_utc": "2026-05-05T18:01:00Z",
        "duration_s": 30.0,
        "rms_dbfs_per_capture": (-25.0, -26.0, -25.5),
        "vad_speech_probability_max": 0.95,
        "vad_speech_probability_p99": 0.92,
        "noise_floor_dbfs_estimate": -55.0,
        "capture_callback_p99_ms": 12.0,
        "capture_jitter_ms": 0.5,
        "portaudio_latency_advertised_ms": 10.0,
        "mixer_card_index": 0,
        "mixer_capture_pct": 75,
        "mixer_boost_pct": 50,
        "mixer_internal_mic_boost_pct": 25,
        "mixer_attenuation_regime": "healthy",
        "echo_correlation_db": -45.0,
        "triage_winner_hid": None,
        "triage_winner_confidence": None,
    }
    base.update(overrides)
    return MeasurementSnapshot(**base)  # type: ignore[arg-type]


def _make_decision(**overrides: object) -> CalibrationDecision:
    base: dict[str, object] = {
        "target": "mind.voice.voice_input_device_name",
        "target_class": "MindConfig.voice",
        "operation": "set",
        "value": "Internal Mic",
        "rationale": "Detected the only capture device with channels >= 1.",
        "rule_id": "R_test",
        "rule_version": 1,
        "confidence": CalibrationConfidence.HIGH,
    }
    base.update(overrides)
    return CalibrationDecision(**base)  # type: ignore[arg-type]


def _make_profile(
    *,
    decisions: tuple[CalibrationDecision, ...] = (),
    provenance: tuple[ProvenanceTrace, ...] = (),
    signature: str | None = None,
) -> CalibrationProfile:
    return CalibrationProfile(
        schema_version=CALIBRATION_PROFILE_SCHEMA_VERSION,
        profile_id="11111111-2222-3333-4444-555555555555",
        mind_id="default",
        fingerprint=_make_fingerprint(),
        measurements=_make_measurements(),
        decisions=decisions,
        provenance=provenance,
        generated_by_engine_version="0.30.15",
        generated_by_rule_set_version=1,
        generated_at_utc="2026-05-05T18:02:00Z",
        signature=signature,
    )


# ====================================================================
# Frozen + slots invariants
# ====================================================================


class TestFrozenInvariants:
    """All schema dataclasses reject mutation."""

    def test_fingerprint_is_frozen(self) -> None:
        fp = _make_fingerprint()
        with pytest.raises(FrozenInstanceError):
            fp.cpu_cores = 999  # type: ignore[misc]

    def test_measurements_is_frozen(self) -> None:
        m = _make_measurements()
        with pytest.raises(FrozenInstanceError):
            m.duration_s = 999.0  # type: ignore[misc]

    def test_decision_is_frozen(self) -> None:
        d = _make_decision()
        with pytest.raises(FrozenInstanceError):
            d.value = "other"  # type: ignore[misc]

    def test_provenance_trace_is_frozen(self) -> None:
        t = ProvenanceTrace(
            rule_id="R_test",
            rule_version=1,
            fired_at_utc="2026-05-05T18:00:00Z",
            matched_conditions=(),
            produced_decisions=(),
            confidence=CalibrationConfidence.HIGH,
        )
        with pytest.raises(FrozenInstanceError):
            t.rule_version = 2  # type: ignore[misc]

    def test_profile_is_frozen(self) -> None:
        p = _make_profile()
        with pytest.raises(FrozenInstanceError):
            p.mind_id = "other"  # type: ignore[misc]


class TestSlotsInvariants:
    """All schema dataclasses use slots (no per-instance __dict__)."""

    def test_fingerprint_has_no_dict(self) -> None:
        fp = _make_fingerprint()
        assert not hasattr(fp, "__dict__")

    def test_measurements_has_no_dict(self) -> None:
        m = _make_measurements()
        assert not hasattr(m, "__dict__")

    def test_decision_has_no_dict(self) -> None:
        d = _make_decision()
        assert not hasattr(d, "__dict__")

    def test_profile_has_no_dict(self) -> None:
        p = _make_profile()
        assert not hasattr(p, "__dict__")


# ====================================================================
# Fingerprint hash determinism + identity
# ====================================================================


class TestFingerprintHash:
    """fingerprint_hash is the L4 community-KB lookup key."""

    def test_hash_is_64_hex_chars_sha256(self) -> None:
        fp = _make_fingerprint()
        h = fp.fingerprint_hash
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_same_inputs_produce_same_hash(self) -> None:
        fp1 = _make_fingerprint()
        fp2 = _make_fingerprint()
        assert fp1.fingerprint_hash == fp2.fingerprint_hash

    def test_capture_at_excluded_from_hash(self) -> None:
        # captured_at_utc is metadata, not identity.
        fp1 = _make_fingerprint(captured_at_utc="2026-05-05T18:00:00Z")
        fp2 = _make_fingerprint(captured_at_utc="2026-12-25T00:00:00Z")
        assert fp1.fingerprint_hash == fp2.fingerprint_hash

    def test_schema_version_excluded_from_hash(self) -> None:
        fp1 = _make_fingerprint(schema_version=1)
        # Schema_version is metadata; bumping it shouldn't shift the
        # identity hash for the same hardware.
        fp2 = _make_fingerprint(schema_version=999)
        assert fp1.fingerprint_hash == fp2.fingerprint_hash

    def test_capture_devices_order_does_not_affect_hash(self) -> None:
        # Tuples are sorted before hashing so capture-order changes
        # don't shift the L4 lookup key.
        fp1 = _make_fingerprint(capture_devices=("Mic A", "Mic B", "Mic C"))
        fp2 = _make_fingerprint(capture_devices=("Mic C", "Mic A", "Mic B"))
        assert fp1.fingerprint_hash == fp2.fingerprint_hash

    def test_hal_interceptors_order_does_not_affect_hash(self) -> None:
        fp1 = _make_fingerprint(hal_interceptors=("Krisp", "BlackHole"))
        fp2 = _make_fingerprint(hal_interceptors=("BlackHole", "Krisp"))
        assert fp1.fingerprint_hash == fp2.fingerprint_hash

    def test_different_codec_id_produces_different_hash(self) -> None:
        fp1 = _make_fingerprint(codec_id="10ec:0257")
        fp2 = _make_fingerprint(codec_id="8086:9d70")
        assert fp1.fingerprint_hash != fp2.fingerprint_hash

    def test_different_kernel_major_minor_produces_different_hash(self) -> None:
        fp1 = _make_fingerprint(kernel_major_minor="6.8")
        fp2 = _make_fingerprint(kernel_major_minor="6.9")
        assert fp1.fingerprint_hash != fp2.fingerprint_hash

    def test_different_audio_stack_produces_different_hash(self) -> None:
        fp1 = _make_fingerprint(audio_stack="pipewire")
        fp2 = _make_fingerprint(audio_stack="pulseaudio")
        assert fp1.fingerprint_hash != fp2.fingerprint_hash

    def test_apo_active_toggle_affects_hash(self) -> None:
        # APO active state is part of the fingerprint because it
        # demands different config decisions.
        fp1 = _make_fingerprint(apo_active=False, apo_name=None)
        fp2 = _make_fingerprint(apo_active=True, apo_name="Voice Clarity")
        assert fp1.fingerprint_hash != fp2.fingerprint_hash


# ====================================================================
# CalibrationProfile.applicable_decisions filter
# ====================================================================


class TestApplicableDecisions:
    """Filter: operation == 'set' AND confidence != EXPERIMENTAL."""

    def test_set_high_confidence_is_applicable(self) -> None:
        d = _make_decision(operation="set", confidence=CalibrationConfidence.HIGH)
        p = _make_profile(decisions=(d,))
        assert p.applicable_decisions == (d,)

    def test_set_medium_confidence_is_applicable(self) -> None:
        d = _make_decision(operation="set", confidence=CalibrationConfidence.MEDIUM)
        p = _make_profile(decisions=(d,))
        assert p.applicable_decisions == (d,)

    def test_set_low_confidence_is_applicable(self) -> None:
        d = _make_decision(operation="set", confidence=CalibrationConfidence.LOW)
        p = _make_profile(decisions=(d,))
        assert p.applicable_decisions == (d,)

    def test_set_experimental_is_not_applicable(self) -> None:
        d = _make_decision(
            operation="set",
            confidence=CalibrationConfidence.EXPERIMENTAL,
        )
        p = _make_profile(decisions=(d,))
        assert p.applicable_decisions == ()

    def test_advise_operation_is_not_applicable(self) -> None:
        d = _make_decision(operation="advise", confidence=CalibrationConfidence.HIGH)
        p = _make_profile(decisions=(d,))
        assert p.applicable_decisions == ()

    def test_preserve_operation_is_not_applicable(self) -> None:
        d = _make_decision(operation="preserve", confidence=CalibrationConfidence.HIGH)
        p = _make_profile(decisions=(d,))
        assert p.applicable_decisions == ()

    def test_mixed_decisions_filtered_correctly(self) -> None:
        applied = _make_decision(
            target="mind.voice.voice_input_device_name",
            operation="set",
            confidence=CalibrationConfidence.HIGH,
        )
        advised = _make_decision(
            target="advice.action",
            operation="advise",
            confidence=CalibrationConfidence.HIGH,
        )
        experimental = _make_decision(
            target="tuning.voice.vad_threshold",
            operation="set",
            confidence=CalibrationConfidence.EXPERIMENTAL,
        )
        p = _make_profile(decisions=(applied, advised, experimental))
        assert p.applicable_decisions == (applied,)


# ====================================================================
# canonical_signing_payload excludes the signature field
# ====================================================================


class TestCanonicalSigningPayload:
    """Signing payload covers profile identity but NOT the signature itself."""

    def test_payload_does_not_contain_signature_key(self) -> None:
        p = _make_profile(signature=None)
        payload = p.canonical_signing_payload()
        assert "signature" not in payload

    def test_payload_contains_identity_fields(self) -> None:
        p = _make_profile()
        payload = p.canonical_signing_payload()
        assert payload["profile_id"] == p.profile_id
        assert payload["mind_id"] == p.mind_id
        assert payload["fingerprint_hash"] == p.fingerprint.fingerprint_hash

    def test_payload_is_signature_independent(self) -> None:
        # Same profile content with different signature values
        # produces the same canonical payload (so signing covers the
        # payload, not the signature itself).
        p_unsigned = _make_profile(signature=None)
        p_signed = _make_profile(signature="abcdef" * 16)
        assert p_unsigned.canonical_signing_payload() == p_signed.canonical_signing_payload()


# ====================================================================
# CalibrationConfidence enum invariants
# ====================================================================


class TestCalibrationConfidenceEnum:
    """Closed enum keeps OTel cardinality bounded."""

    def test_exactly_four_members(self) -> None:
        members = list(CalibrationConfidence)
        assert len(members) == 4
        assert {m.value for m in members} == {"high", "medium", "low", "experimental"}

    def test_string_values_are_lowercase(self) -> None:
        for m in CalibrationConfidence:
            assert m.value == m.value.lower()

    def test_unknown_string_rejected(self) -> None:
        with pytest.raises(ValueError):
            CalibrationConfidence("super-confident")
