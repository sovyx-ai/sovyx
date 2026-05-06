"""Tests for the P4 Ed25519 signing + verification path (v0.30.32).

The pre-P4 ``_persistence.py`` ran a "is signature field present?"
theater check that never invoked any cryptographic primitive. v0.30.32
wires real verification against the bundled trust store
(``_trusted_keys/v1.pub``) and adds a sign-at-persistence-boundary
hook driven by the ``--signing-key`` CLI flag.

Test matrix (mission §10):

1. Canonical payload determinism — same profile → same bytes
2. Sign + verify round-trip → ACCEPTED
3. Tamper payload → REJECTED_BAD_SIGNATURE
4. Tamper signature bytes → REJECTED_BAD_SIGNATURE
5. Malformed base64 → REJECTED_MALFORMED_SIGNATURE
6. Wrong-length signature (e.g. 96 bytes) → REJECTED_MALFORMED_SIGNATURE
7. LENIENT load of unsigned → WARN, profile returned
8. STRICT load of unsigned → CalibrationProfileLoadError
9. STRICT load of invalid → CalibrationProfileLoadError
10. Trust store missing → REJECTED_NO_TRUSTED_KEY (LENIENT logs, STRICT raises)
11. Full sign + save + load round-trip in STRICT mode
"""

from __future__ import annotations

import base64
from dataclasses import replace
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from sovyx.voice.calibration import _persistence as persistence
from sovyx.voice.calibration._persistence import (
    CalibrationProfileLoadError,
    _LoadMode,
    _verify_calibration_signature,
    load_calibration_profile,
    save_calibration_profile,
)
from sovyx.voice.calibration._signing import (
    VerifyResult,
    canonical_calibration_payload,
)
from sovyx.voice.calibration.schema import (
    CalibrationConfidence,
    CalibrationDecision,
    CalibrationProfile,
    HardwareFingerprint,
    MeasurementSnapshot,
)

# ════════════════════════════════════════════════════════════════════
# Fixtures
# ════════════════════════════════════════════════════════════════════


def _fingerprint() -> HardwareFingerprint:
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
        codec_id="10ec:0257",
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
        duration_s=10.0,
        rms_dbfs_per_capture=(),
        vad_speech_probability_max=0.0,
        vad_speech_probability_p99=0.0,
        noise_floor_dbfs_estimate=-60.0,
        capture_callback_p99_ms=0.0,
        capture_jitter_ms=0.0,
        portaudio_latency_advertised_ms=0.0,
        mixer_card_index=0,
        mixer_capture_pct=50,
        mixer_boost_pct=0,
        mixer_internal_mic_boost_pct=0,
        mixer_attenuation_regime="healthy",
        echo_correlation_db=None,
        triage_winner_hid=None,
        triage_winner_confidence=None,
    )


def _decision() -> CalibrationDecision:
    return CalibrationDecision(
        target="advice.action",
        target_class="TuningAdvice",
        operation="advise",
        value="sovyx doctor voice --fix --yes",
        rationale="r10 mixer attenuated",
        rule_id="R10_mic_attenuated",
        rule_version=2,
        confidence=CalibrationConfidence.HIGH,
    )


def _profile(*, signature: str | None = None) -> CalibrationProfile:
    return CalibrationProfile(
        schema_version=1,
        profile_id="11111111-2222-3333-4444-555555555555",
        mind_id="default",
        fingerprint=_fingerprint(),
        measurements=_measurements(),
        decisions=(_decision(),),
        provenance=(),
        generated_by_engine_version="0.30.32",
        generated_by_rule_set_version=11,
        generated_at_utc="2026-05-06T18:02:00Z",
        signature=signature,
    )


def _generate_keypair(tmp_path: Path) -> tuple[Path, Ed25519PrivateKey]:
    """Generate an ephemeral keypair + write the PEM private key to disk.

    The trust-store public key is patched in via ``_swap_trust_store``
    in each test so the verifier checks against the same key the test
    just signed with.
    """
    private_key = Ed25519PrivateKey.generate()
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    key_path = tmp_path / "test_signing.priv"
    key_path.write_bytes(pem)
    return key_path, private_key


@pytest.fixture()
def _swap_trust_store():
    """Save + restore the module-level trust store between tests.

    Tests that need to verify against an ephemeral public key use
    ``_install_trust_store(pubkey)`` to swap it in, then the fixture
    restores the production v1.pub at teardown.
    """
    original_key = persistence._TRUSTED_PUBKEY
    original_loaded = persistence._TRUSTED_PUBKEY_LOADED
    yield
    persistence._TRUSTED_PUBKEY = original_key
    persistence._TRUSTED_PUBKEY_LOADED = original_loaded


def _install_trust_store(pubkey) -> None:  # noqa: ANN001 -- Ed25519PublicKey
    persistence._TRUSTED_PUBKEY = pubkey
    persistence._TRUSTED_PUBKEY_LOADED = True


def _clear_trust_store() -> None:
    persistence._TRUSTED_PUBKEY = None
    persistence._TRUSTED_PUBKEY_LOADED = True  # mark "loaded as None"


# ════════════════════════════════════════════════════════════════════
# 1. Canonical payload determinism
# ════════════════════════════════════════════════════════════════════


class TestCanonicalPayload:
    def test_same_profile_same_bytes(self) -> None:
        p1 = _profile(signature="ignored1")
        p2 = _profile(signature="ignored2")
        # canonical_signing_payload strips signature; the rest is
        # identical → byte-identical.
        b1 = canonical_calibration_payload(p1.canonical_signing_payload())
        b2 = canonical_calibration_payload(p2.canonical_signing_payload())
        assert b1 == b2


# ════════════════════════════════════════════════════════════════════
# 2-6. Verifier behaviour matrix
# ════════════════════════════════════════════════════════════════════


@pytest.mark.usefixtures("_swap_trust_store")
class TestVerifierMatrix:
    def test_round_trip_accepted(self, tmp_path: Path) -> None:
        _key_path, private_key = _generate_keypair(tmp_path)
        _install_trust_store(private_key.public_key())

        profile_unsigned = _profile()
        payload = canonical_calibration_payload(profile_unsigned.canonical_signing_payload())
        sig_b64 = base64.b64encode(private_key.sign(payload)).decode("ascii")
        profile_signed = replace(profile_unsigned, signature=sig_b64)

        assert _verify_calibration_signature(profile_signed) == VerifyResult.ACCEPTED

    def test_tampered_payload_rejected_bad_signature(self, tmp_path: Path) -> None:
        _key_path, private_key = _generate_keypair(tmp_path)
        _install_trust_store(private_key.public_key())

        profile_unsigned = _profile()
        payload = canonical_calibration_payload(profile_unsigned.canonical_signing_payload())
        sig_b64 = base64.b64encode(private_key.sign(payload)).decode("ascii")
        # Tamper with the profile so the canonical payload diverges.
        tampered = replace(
            profile_unsigned,
            signature=sig_b64,
            mind_id="different_mind",
        )
        assert _verify_calibration_signature(tampered) == VerifyResult.REJECTED_BAD_SIGNATURE

    def test_tampered_signature_rejected_bad_signature(self, tmp_path: Path) -> None:
        _key_path, private_key = _generate_keypair(tmp_path)
        _install_trust_store(private_key.public_key())

        profile_unsigned = _profile()
        payload = canonical_calibration_payload(profile_unsigned.canonical_signing_payload())
        sig_bytes = bytearray(private_key.sign(payload))
        sig_bytes[0] ^= 0xFF  # Flip the first byte
        bad_sig_b64 = base64.b64encode(bytes(sig_bytes)).decode("ascii")
        tampered = replace(profile_unsigned, signature=bad_sig_b64)
        assert _verify_calibration_signature(tampered) == VerifyResult.REJECTED_BAD_SIGNATURE

    def test_malformed_base64_rejected(self, tmp_path: Path) -> None:
        _key_path, private_key = _generate_keypair(tmp_path)
        _install_trust_store(private_key.public_key())
        # "@@@" is not valid base64.
        profile = _profile(signature="@@@invalid base64@@@")
        assert _verify_calibration_signature(profile) == VerifyResult.REJECTED_MALFORMED_SIGNATURE

    def test_wrong_length_rejected_malformed(self, tmp_path: Path) -> None:
        _key_path, private_key = _generate_keypair(tmp_path)
        _install_trust_store(private_key.public_key())
        # 32 bytes of zeros — valid base64 but wrong length for Ed25519.
        wrong_len = base64.b64encode(b"\x00" * 32).decode("ascii")
        profile = _profile(signature=wrong_len)
        assert _verify_calibration_signature(profile) == VerifyResult.REJECTED_MALFORMED_SIGNATURE

    def test_no_signature_rejected_no_signature(self, tmp_path: Path) -> None:
        _key_path, private_key = _generate_keypair(tmp_path)
        _install_trust_store(private_key.public_key())
        profile = _profile(signature=None)
        assert _verify_calibration_signature(profile) == VerifyResult.REJECTED_NO_SIGNATURE

    def test_no_trusted_key_rejected(self) -> None:
        _clear_trust_store()
        profile = _profile(signature="anything")
        assert _verify_calibration_signature(profile) == VerifyResult.REJECTED_NO_TRUSTED_KEY


# ════════════════════════════════════════════════════════════════════
# 7-9. Load path (LENIENT vs STRICT) + new event
# ════════════════════════════════════════════════════════════════════


@pytest.mark.usefixtures("_swap_trust_store")
class TestLoadPath:
    def test_lenient_accepts_unsigned(self, tmp_path: Path) -> None:
        _key_path, private_key = _generate_keypair(tmp_path)
        _install_trust_store(private_key.public_key())
        save_calibration_profile(_profile(signature=None), data_dir=tmp_path)
        loaded = load_calibration_profile(
            data_dir=tmp_path, mind_id="default", mode=_LoadMode.LENIENT
        )
        assert loaded.signature is None

    def test_strict_rejects_unsigned(self, tmp_path: Path) -> None:
        _key_path, private_key = _generate_keypair(tmp_path)
        _install_trust_store(private_key.public_key())
        save_calibration_profile(_profile(signature=None), data_dir=tmp_path)
        with pytest.raises(CalibrationProfileLoadError, match="unsigned"):
            load_calibration_profile(data_dir=tmp_path, mind_id="default", mode=_LoadMode.STRICT)

    def test_strict_rejects_invalid_signature(self, tmp_path: Path) -> None:
        _key_path, private_key = _generate_keypair(tmp_path)
        _install_trust_store(private_key.public_key())
        # Wrong-length sig → REJECTED_MALFORMED_SIGNATURE
        bad_sig = base64.b64encode(b"\x00" * 32).decode("ascii")
        save_calibration_profile(_profile(signature=bad_sig), data_dir=tmp_path)
        with pytest.raises(CalibrationProfileLoadError, match="signature verification"):
            load_calibration_profile(data_dir=tmp_path, mind_id="default", mode=_LoadMode.STRICT)

    def test_signature_invalid_event_carries_verdict(self, tmp_path: Path) -> None:
        _key_path, private_key = _generate_keypair(tmp_path)
        _install_trust_store(private_key.public_key())
        bad_sig = base64.b64encode(b"\x00" * 32).decode("ascii")
        save_calibration_profile(_profile(signature=bad_sig), data_dir=tmp_path)

        events: list[tuple[str, dict]] = []

        class _Cap:
            def info(self, event: str, **kwargs: object) -> None:
                events.append((event, dict(kwargs)))

            def warning(self, event: str, **kwargs: object) -> None:
                events.append((event, dict(kwargs)))

            def debug(self, event: str, **kwargs: object) -> None:
                events.append((event, dict(kwargs)))

        original = persistence.logger
        persistence.logger = _Cap()  # type: ignore[assignment]
        try:
            load_calibration_profile(data_dir=tmp_path, mind_id="default", mode=_LoadMode.LENIENT)
        finally:
            persistence.logger = original  # type: ignore[assignment]

        invalid = next(e for e in events if e[0] == "voice.calibration.profile.signature.invalid")
        assert invalid[1]["verdict"] == "rejected_malformed_signature"
        assert invalid[1]["mode"] == "lenient"


# ════════════════════════════════════════════════════════════════════
# 10. No-trust-store rejection on load
# ════════════════════════════════════════════════════════════════════


@pytest.mark.usefixtures("_swap_trust_store")
class TestNoTrustStore:
    def test_lenient_load_signed_without_trust_key_invalid(self, tmp_path: Path) -> None:
        _clear_trust_store()
        # Signed profile but no trust key → REJECTED_NO_TRUSTED_KEY,
        # LENIENT logs WARN + accepts the load.
        any_sig = base64.b64encode(b"\x00" * 64).decode("ascii")
        save_calibration_profile(_profile(signature=any_sig), data_dir=tmp_path)
        loaded = load_calibration_profile(
            data_dir=tmp_path, mind_id="default", mode=_LoadMode.LENIENT
        )
        assert loaded.signature == any_sig

    def test_strict_load_signed_without_trust_key_raises(self, tmp_path: Path) -> None:
        _clear_trust_store()
        any_sig = base64.b64encode(b"\x00" * 64).decode("ascii")
        save_calibration_profile(_profile(signature=any_sig), data_dir=tmp_path)
        with pytest.raises(CalibrationProfileLoadError, match="signature verification"):
            load_calibration_profile(data_dir=tmp_path, mind_id="default", mode=_LoadMode.STRICT)


# ════════════════════════════════════════════════════════════════════
# 11. Full sign+save+load round-trip
# ════════════════════════════════════════════════════════════════════


@pytest.mark.usefixtures("_swap_trust_store")
class TestSignSaveLoadRoundTrip:
    def test_signed_profile_loads_under_strict(self, tmp_path: Path) -> None:
        key_path, private_key = _generate_keypair(tmp_path)
        _install_trust_store(private_key.public_key())

        save_calibration_profile(
            _profile(signature=None),
            data_dir=tmp_path,
            signing_key_path=key_path,
        )
        # Load under STRICT — should succeed because the persistence
        # layer signed the profile against the same key the trust
        # store carries.
        loaded = load_calibration_profile(
            data_dir=tmp_path, mind_id="default", mode=_LoadMode.STRICT
        )
        assert loaded.signature is not None
        # Signature is base64 of 64 bytes → 88 chars (with padding).
        assert len(base64.b64decode(loaded.signature, validate=True)) == 64

    def test_signing_failure_falls_through_to_unsigned_persist(self, tmp_path: Path) -> None:
        # Pass a bogus signing key path -> signing fails -> profile is
        # persisted UNSIGNED + a structured WARN fires. The save itself
        # MUST NOT raise.
        bogus_key = tmp_path / "nonexistent.priv"
        # File doesn't exist -> the if signing_key_path.is_file() guard
        # skips signing silently. Pass a file that EXISTS but isn't
        # PEM to exercise the warn path.
        bad_key = tmp_path / "bad.priv"
        bad_key.write_bytes(b"this is not PEM")

        events: list[tuple[str, dict]] = []

        class _Cap:
            def info(self, event: str, **kwargs: object) -> None:
                events.append((event, dict(kwargs)))

            def warning(self, event: str, **kwargs: object) -> None:
                events.append((event, dict(kwargs)))

        original = persistence.logger
        persistence.logger = _Cap()  # type: ignore[assignment]
        try:
            # rc.7 (Agent 2 NEW.2/NEW.3): save_calibration_profile now
            # returns SaveProfileResult(path, signed); unpack to keep
            # the existing target.is_file() assertion semantics.
            save_result = save_calibration_profile(
                _profile(signature=None),
                data_dir=tmp_path,
                signing_key_path=bad_key,
            )
            target = save_result.path
        finally:
            persistence.logger = original  # type: ignore[assignment]

        assert target.is_file()
        signing_failed = next(
            (e for e in events if e[0] == "voice.calibration.profile.signing_failed"),
            None,
        )
        assert signing_failed is not None
        assert "mind_id_hash" in signing_failed[1]

        persisted = next(e for e in events if e[0] == "voice.calibration.profile.persisted")
        assert persisted[1]["signed"] is False
        # Reference to the ignored bogus path so static checks accept it.
        _ = bogus_key


# ════════════════════════════════════════════════════════════════════
# QA-FIX-2 (v0.31.0-rc.2) — observability for path-not-found case
# ════════════════════════════════════════════════════════════════════


@pytest.mark.usefixtures("_swap_trust_store")
class TestSigningKeyPathMissingObservability:
    """Operator passes ``--signing-key`` with a non-existent path: the
    pre-rc.2 ``is_file()`` short-circuit silently fell through to an
    unsigned write, leaving operators wondering why the profile they
    expected to be signed wasn't. Post-rc.2 the persistence layer
    emits ``voice.calibration.profile.signing_skipped`` with closed-
    enum ``reason="key_path_missing"`` so the gap is observable.
    """

    def test_signing_skipped_event_fires_when_path_missing(self, tmp_path: Path) -> None:
        # Path supplied but file does not exist (operator typo / CI
        # misconfiguration / file deleted between resolve + apply).
        nonexistent = tmp_path / "absent.priv"

        events: list[tuple[str, dict]] = []

        class _Cap:
            def info(self, event: str, **kwargs: object) -> None:
                events.append((event, dict(kwargs)))

            def warning(self, event: str, **kwargs: object) -> None:
                events.append((event, dict(kwargs)))

        original = persistence.logger
        persistence.logger = _Cap()  # type: ignore[assignment]
        try:
            # rc.7 (Agent 2 NEW.2/NEW.3): SaveProfileResult unpack.
            save_result = save_calibration_profile(
                _profile(signature=None),
                data_dir=tmp_path,
                signing_key_path=nonexistent,
            )
            target = save_result.path
        finally:
            persistence.logger = original  # type: ignore[assignment]

        assert target.is_file(), "profile must still persist (best-effort signing)"
        assert save_result.signed is False, (
            "missing key path → signed=False (signing skipped, persisted unsigned)"
        )
        skipped = next(
            (e for e in events if e[0] == "voice.calibration.profile.signing_skipped"),
            None,
        )
        assert skipped is not None, "signing_skipped telemetry must fire on path-missing"
        assert skipped[1]["reason"] == "key_path_missing"
        assert "mind_id_hash" in skipped[1]
        assert "profile_id_hash" in skipped[1]
        # And the persisted event reports unsigned.
        persisted = next(e for e in events if e[0] == "voice.calibration.profile.persisted")
        assert persisted[1]["signed"] is False

    def test_no_signing_key_does_not_emit_skipped(self, tmp_path: Path) -> None:
        """When ``signing_key_path`` is None (operator opted out
        entirely), no skipped event should fire — that would noise-
        spam the unsigned-by-default operator path. Skipped fires
        ONLY when the operator passed a path that doesn't resolve.
        """
        events: list[tuple[str, dict]] = []

        class _Cap:
            def info(self, event: str, **kwargs: object) -> None:
                events.append((event, dict(kwargs)))

            def warning(self, event: str, **kwargs: object) -> None:
                events.append((event, dict(kwargs)))

        original = persistence.logger
        persistence.logger = _Cap()  # type: ignore[assignment]
        try:
            save_calibration_profile(_profile(signature=None), data_dir=tmp_path)
        finally:
            persistence.logger = original  # type: ignore[assignment]

        skipped = [e for e in events if e[0] == "voice.calibration.profile.signing_skipped"]
        assert skipped == [], "signing_skipped must NOT fire when no key was supplied"
