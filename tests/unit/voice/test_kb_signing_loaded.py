"""Tests for Ed25519 KB signing infrastructure live (Step 7).

The dev key v1 is shipped in this commit at
``src/sovyx/voice/health/_mixer_kb/_trusted_keys/v1.pub``. These tests
pin the contract:

* :func:`load_trusted_public_key` returns a non-None Ed25519PublicKey
  when the key file is present.
* :class:`KBSignatureVerifier` constructed with the loaded key returns
  REJECTED_NO_SIGNATURE for a dict missing the ``signature`` field.
* End-to-end round-trip: sign-then-verify with the local private key
  yields ACCEPTED.

Reference: MISSION-voice-100pct-autonomous-2026-04-25.md step 7.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from sovyx.voice.health._mixer_kb._signing import (
    KBSignatureVerifier,
    Mode,
    VerifyResult,
    canonical_payload,
    load_trusted_public_key,
    trusted_key_path,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
PRIVATE_KEY_PATH = REPO_ROOT / ".signing-keys" / "sovyx_kb_v1.priv"


class TestTrustedKeyShipped:
    """The dev v1 public key must ship with the wheel."""

    def test_trusted_key_path_points_to_committed_file(self) -> None:
        path = trusted_key_path()
        assert path.is_file(), f"Step 7 must ship v1.pub at {path}"

    def test_load_trusted_public_key_returns_ed25519_key(self) -> None:
        key = load_trusted_public_key()
        assert key is not None
        assert isinstance(key, Ed25519PublicKey)


class TestVerifierContractWithSippedKey:
    """The verifier constructed with the shipped key behaves correctly."""

    def test_unsigned_profile_rejected_no_signature(self) -> None:
        verifier = KBSignatureVerifier(load_trusted_public_key(), mode=Mode.LENIENT)
        verdict = verifier.verify({"profile_id": "test", "schema_version": 2})
        assert verdict is VerifyResult.REJECTED_NO_SIGNATURE

    def test_malformed_signature_rejected(self) -> None:
        verifier = KBSignatureVerifier(load_trusted_public_key(), mode=Mode.LENIENT)
        verdict = verifier.verify(
            {"profile_id": "t", "schema_version": 2, "signature": "not-base64"},
        )
        assert verdict is VerifyResult.REJECTED_MALFORMED_SIGNATURE

    def test_wrong_signature_rejected_bad_signature(self) -> None:
        verifier = KBSignatureVerifier(load_trusted_public_key(), mode=Mode.LENIENT)
        # 64-byte zero signature — well-formed length, wrong bytes.
        bogus_sig = base64.b64encode(b"\x00" * 64).decode("ascii")
        verdict = verifier.verify(
            {"profile_id": "t", "schema_version": 2, "signature": bogus_sig},
        )
        assert verdict is VerifyResult.REJECTED_BAD_SIGNATURE


@pytest.mark.skipif(
    not PRIVATE_KEY_PATH.is_file(),
    reason="dev private key absent (only present on dev hosts that ran scripts/dev/generate_kb_signing_key.py)",
)
class TestRoundTrip:
    """Sign-then-verify must produce ACCEPTED.

    Skipped on hosts that don't have the local dev private key (CI
    + clean clones). Active on the dev host where Step 7 was run,
    plus any other dev that ran ``generate_kb_signing_key.py``.
    """

    def test_signed_profile_accepted(self) -> None:
        profile = {
            "profile_id": "round_trip_test",
            "schema_version": 2,
            "match_keys": {"codec_id_glob": "test:*"},
        }

        # Sign with the dev key.
        priv_pem = PRIVATE_KEY_PATH.read_bytes()
        priv_key = load_pem_private_key(priv_pem, password=None)
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        assert isinstance(priv_key, Ed25519PrivateKey)
        signature = priv_key.sign(canonical_payload(profile))
        signed_profile = {**profile, "signature": base64.b64encode(signature).decode("ascii")}

        # Verify with the trusted public key.
        verifier = KBSignatureVerifier(load_trusted_public_key(), mode=Mode.STRICT)
        verdict = verifier.verify(signed_profile)
        assert verdict is VerifyResult.ACCEPTED
