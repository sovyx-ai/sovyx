"""F2 wire-up tests — KBSignatureVerifier + loader integration.

Verifies that the loader correctly threads the verifier through
the load path. Pure foundation tests live in test_kb_signing.py;
this module tests the seam where signing meets the loader.

Reference: MISSION-voice-mixer-enterprise-refactor-2026-04-25 §4 F2.
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import Any

import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from sovyx.voice.health._mixer_kb._signing import (
    KBSignatureError,
    KBSignatureVerifier,
    Mode,
    VerifyResult,
    canonical_payload,
)
from sovyx.voice.health._mixer_kb.loader import (
    load_profile_file,
    load_profiles_from_directory,
)

# Re-use the canonical test YAML from test_mixer_kb.py to ensure
# the wire-up tests match the schema-side fixtures faithfully.
from tests.unit.voice.health.test_mixer_kb import _GOOD_YAML, _write_profile

# ── Helpers ────────────────────────────────────────────────────────


def _make_keypair() -> tuple[Ed25519PrivateKey, Ed25519PublicKey]:
    priv = Ed25519PrivateKey.generate()
    return priv, priv.public_key()


def _signed_yaml_text(priv: Ed25519PrivateKey, profile_id: str) -> str:
    """Return YAML text with a freshly-computed signature field."""
    yaml_text = _GOOD_YAML.replace(
        "profile_id: vaio_vjfe69_sn6180",
        f"profile_id: {profile_id}",
    )
    parsed: dict[str, Any] = yaml.safe_load(yaml_text)
    payload = canonical_payload(parsed)
    sig = priv.sign(payload)
    parsed["signature"] = base64.b64encode(sig).decode("ascii")
    return yaml.safe_dump(parsed, sort_keys=False)


def _write_signed_profile(
    dirpath: Path,
    priv: Ed25519PrivateKey,
    profile_id: str,
) -> Path:
    text = _signed_yaml_text(priv, profile_id)
    path = dirpath / f"{profile_id}.yaml"
    path.write_text(text, encoding="utf-8")
    return path


# ── load_profile_file with verifier ────────────────────────────────


class TestLoadProfileFileWithVerifier:
    def test_no_verifier_skips_verification_legacy_behaviour(
        self,
        tmp_path: Path,
    ) -> None:
        """verifier=None preserves the F1 behaviour: signature field
        ignored entirely."""
        path = _write_profile(tmp_path, "test_profile_a")
        profile = load_profile_file(path, verifier=None)
        assert profile.profile_id == "test_profile_a"

    def test_lenient_verifier_unsigned_profile_loads_anyway(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """LENIENT mode + unsigned profile → emits structured WARN
        but profile is still returned."""
        _, pub = _make_keypair()
        verifier = KBSignatureVerifier(pub, mode=Mode.LENIENT)
        path = _write_profile(tmp_path, "test_profile_b")
        with caplog.at_level(logging.WARNING):
            profile = load_profile_file(path, verifier=verifier)
        assert profile.profile_id == "test_profile_b"
        assert any(
            "voice.kb.signature.invalid" in str(r.msg) for r in caplog.records
        )

    def test_strict_verifier_unsigned_profile_raises(
        self,
        tmp_path: Path,
    ) -> None:
        """STRICT mode + unsigned profile → KBSignatureError."""
        _, pub = _make_keypair()
        verifier = KBSignatureVerifier(pub, mode=Mode.STRICT)
        path = _write_profile(tmp_path, "test_profile_c")
        with pytest.raises(KBSignatureError) as exc_info:
            load_profile_file(path, verifier=verifier)
        assert exc_info.value.result == VerifyResult.REJECTED_NO_SIGNATURE

    def test_signed_profile_verifies_under_strict_mode(
        self,
        tmp_path: Path,
    ) -> None:
        priv, pub = _make_keypair()
        verifier = KBSignatureVerifier(pub, mode=Mode.STRICT)
        path = _write_signed_profile(tmp_path, priv, "test_profile_d")
        # Should not raise — signature verifies.
        profile = load_profile_file(path, verifier=verifier)
        assert profile.profile_id == "test_profile_d"

    def test_signed_profile_with_wrong_key_raises_under_strict(
        self,
        tmp_path: Path,
    ) -> None:
        priv_signer, _ = _make_keypair()
        _, pub_other = _make_keypair()
        verifier = KBSignatureVerifier(pub_other, mode=Mode.STRICT)
        path = _write_signed_profile(tmp_path, priv_signer, "test_profile_e")
        with pytest.raises(KBSignatureError) as exc_info:
            load_profile_file(path, verifier=verifier)
        assert exc_info.value.result == VerifyResult.REJECTED_BAD_SIGNATURE


# ── load_profiles_from_directory with verifier ────────────────────


class TestLoadProfilesFromDirectoryWithVerifier:
    def test_no_verifier_loads_all_profiles_legacy(
        self,
        tmp_path: Path,
    ) -> None:
        _write_profile(tmp_path, "p_a")
        _write_profile(tmp_path, "p_b")
        profiles = load_profiles_from_directory(tmp_path, verifier=None)
        assert {p.profile_id for p in profiles} == {"p_a", "p_b"}

    def test_strict_verifier_skips_unsigned_profiles(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """STRICT mode — unsigned profiles get skipped (per cohort
        failure policy: one bad profile shouldn't sink the pool).
        The verifier raises KBSignatureError, the loader catches it
        and logs a structured WARN."""
        _, pub = _make_keypair()
        verifier = KBSignatureVerifier(pub, mode=Mode.STRICT)
        _write_profile(tmp_path, "p_unsigned_x")
        _write_profile(tmp_path, "p_unsigned_y")
        with caplog.at_level(logging.WARNING):
            profiles = load_profiles_from_directory(tmp_path, verifier=verifier)
        assert profiles == []
        # Each skip should produce a structured WARN.
        rejected = [
            r for r in caplog.records
            if "mixer_kb_profile_signature_rejected" in str(r.msg)
        ]
        assert len(rejected) == 2

    def test_strict_verifier_mixed_pool_keeps_valid_only(
        self,
        tmp_path: Path,
    ) -> None:
        """Cohort failure policy: signed profiles load, unsigned
        ones get skipped."""
        priv, pub = _make_keypair()
        verifier = KBSignatureVerifier(pub, mode=Mode.STRICT)
        _write_signed_profile(tmp_path, priv, "p_signed_ok")
        _write_profile(tmp_path, "p_unsigned_skip")
        profiles = load_profiles_from_directory(tmp_path, verifier=verifier)
        ids = {p.profile_id for p in profiles}
        assert ids == {"p_signed_ok"}

    def test_lenient_verifier_loads_all_with_warnings(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """LENIENT mode loads everything but warns on unsigned."""
        _, pub = _make_keypair()
        verifier = KBSignatureVerifier(pub, mode=Mode.LENIENT)
        _write_profile(tmp_path, "p_lenient_a")
        _write_profile(tmp_path, "p_lenient_b")
        with caplog.at_level(logging.WARNING):
            profiles = load_profiles_from_directory(tmp_path, verifier=verifier)
        assert {p.profile_id for p in profiles} == {"p_lenient_a", "p_lenient_b"}
        # Two warnings — one per unsigned profile.
        warns = [
            r for r in caplog.records
            if "voice.kb.signature.invalid" in str(r.msg)
        ]
        assert len(warns) == 2
