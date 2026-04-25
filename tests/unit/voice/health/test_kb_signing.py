"""Tests for :mod:`sovyx.voice.health._mixer_kb._signing` (F2 foundation).

Covers the Ed25519 verifier + canonicalisation + trusted-key
loader. Per CLAUDE.md anti-pattern #20 the tests use an in-memory
keypair generated per-test (``Ed25519PrivateKey.generate()``) so
no on-disk keys are required during CI.

Reference: MISSION-voice-mixer-enterprise-refactor-2026-04-25 §4 F2.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PublicFormat,
)

from sovyx.voice.health._mixer_kb._signing import (
    _ED25519_SIGNATURE_LEN,
    _SIGNATURE_FIELD,
    KBSignatureError,
    KBSignatureVerifier,
    Mode,
    VerifyResult,
    canonical_payload,
    load_trusted_public_key,
    trusted_key_path,
)

# ── Helpers ────────────────────────────────────────────────────────


def _make_keypair() -> tuple[Ed25519PrivateKey, Ed25519PublicKey]:
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()
    return priv, pub


def _sign_profile(priv: Ed25519PrivateKey, profile: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``profile`` with a freshly-computed ``signature`` field."""
    payload = canonical_payload(profile)
    sig = priv.sign(payload)
    out = dict(profile)
    out[_SIGNATURE_FIELD] = base64.b64encode(sig).decode("ascii")
    return out


def _sample_profile() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "profile_id": "test_profile",
        "profile_version": 1,
        "codec_id_glob": "ALC*",
    }


# ── canonical_payload ──────────────────────────────────────────────


class TestCanonicalPayload:
    def test_strips_signature_field(self) -> None:
        profile = _sample_profile()
        with_sig = dict(profile)
        with_sig[_SIGNATURE_FIELD] = "abcd"
        assert canonical_payload(profile) == canonical_payload(with_sig)

    def test_deterministic_under_key_reorder(self) -> None:
        a = {"b": 2, "a": 1, "c": 3}
        b = {"a": 1, "b": 2, "c": 3}
        assert canonical_payload(a) == canonical_payload(b)

    def test_returns_utf8_bytes(self) -> None:
        out = canonical_payload(_sample_profile())
        assert isinstance(out, bytes)
        # Must round-trip via UTF-8.
        assert out.decode("utf-8")

    def test_handles_unicode(self) -> None:
        profile = {"description": "Conexant café 🎙️"}
        out = canonical_payload(profile)
        # Decoded form preserves the non-ASCII chars (allow_unicode=True).
        assert "café" in out.decode("utf-8")

    def test_distinct_content_distinct_payload(self) -> None:
        a = canonical_payload({"profile_id": "a"})
        b = canonical_payload({"profile_id": "b"})
        assert a != b


# ── VerifyResult / Mode enums ─────────────────────────────────────


class TestEnums:
    def test_verify_result_values(self) -> None:
        values = {r.value for r in VerifyResult}
        assert values == {
            "accepted",
            "rejected_no_signature",
            "rejected_bad_signature",
            "rejected_no_trusted_key",
            "rejected_malformed_signature",
        }

    def test_mode_values(self) -> None:
        assert {m.value for m in Mode} == {"lenient", "strict"}

    def test_str_enum_value_comparison(self) -> None:
        """Anti-pattern #9 — string equality must work (xdist-safe)."""
        assert VerifyResult.ACCEPTED == "accepted"
        assert Mode.LENIENT == "lenient"


# ── KBSignatureVerifier ────────────────────────────────────────────


class TestKBSignatureVerifier:
    def test_accepts_valid_signature(self) -> None:
        priv, pub = _make_keypair()
        verifier = KBSignatureVerifier(pub)
        signed = _sign_profile(priv, _sample_profile())
        assert verifier.verify(signed) == VerifyResult.ACCEPTED

    def test_rejects_no_signature_field(self) -> None:
        _, pub = _make_keypair()
        verifier = KBSignatureVerifier(pub)
        result = verifier.verify(_sample_profile())
        assert result == VerifyResult.REJECTED_NO_SIGNATURE

    def test_rejects_empty_signature(self) -> None:
        _, pub = _make_keypair()
        verifier = KBSignatureVerifier(pub)
        profile = _sample_profile()
        profile[_SIGNATURE_FIELD] = ""
        assert verifier.verify(profile) == VerifyResult.REJECTED_NO_SIGNATURE

    def test_rejects_signature_from_other_key(self) -> None:
        priv_a, _ = _make_keypair()
        _, pub_b = _make_keypair()
        verifier = KBSignatureVerifier(pub_b)
        signed = _sign_profile(priv_a, _sample_profile())
        assert verifier.verify(signed) == VerifyResult.REJECTED_BAD_SIGNATURE

    def test_rejects_tampered_content(self) -> None:
        priv, pub = _make_keypair()
        verifier = KBSignatureVerifier(pub)
        signed = _sign_profile(priv, _sample_profile())
        # Mutate content AFTER signing — signature must no longer verify.
        signed["profile_version"] = 999
        assert verifier.verify(signed) == VerifyResult.REJECTED_BAD_SIGNATURE

    def test_rejects_malformed_base64(self) -> None:
        _, pub = _make_keypair()
        verifier = KBSignatureVerifier(pub)
        profile = _sample_profile()
        profile[_SIGNATURE_FIELD] = "not-valid-base64!!!"
        assert verifier.verify(profile) == VerifyResult.REJECTED_MALFORMED_SIGNATURE

    def test_rejects_wrong_signature_length(self) -> None:
        _, pub = _make_keypair()
        verifier = KBSignatureVerifier(pub)
        profile = _sample_profile()
        # 32 bytes — half of an Ed25519 signature.
        short_sig = base64.b64encode(b"\x00" * 32).decode("ascii")
        profile[_SIGNATURE_FIELD] = short_sig
        assert verifier.verify(profile) == VerifyResult.REJECTED_MALFORMED_SIGNATURE

    def test_rejects_non_string_signature(self) -> None:
        _, pub = _make_keypair()
        verifier = KBSignatureVerifier(pub)
        profile = _sample_profile()
        profile[_SIGNATURE_FIELD] = 12345  # not a string
        assert verifier.verify(profile) == VerifyResult.REJECTED_MALFORMED_SIGNATURE

    def test_no_trusted_key_short_circuits(self) -> None:
        """Verifier with no trusted key returns
        REJECTED_NO_TRUSTED_KEY without inspecting profile content."""
        verifier = KBSignatureVerifier(public_key=None)
        # has_trusted_key reflects state.
        assert verifier.has_trusted_key is False
        # Even with a present signature field, returns no-trusted-key.
        result = verifier.verify({"signature": "anything", "profile_id": "x"})
        assert result == VerifyResult.REJECTED_NO_TRUSTED_KEY

    def test_default_mode_is_lenient(self) -> None:
        _, pub = _make_keypair()
        verifier = KBSignatureVerifier(pub)
        assert verifier.mode is Mode.LENIENT

    def test_lenient_mode_returns_verdict_no_raise(self) -> None:
        _, pub = _make_keypair()
        verifier = KBSignatureVerifier(pub, mode=Mode.LENIENT)
        # Should not raise even on rejected verdict.
        result = verifier.verify(_sample_profile())  # no signature
        assert result == VerifyResult.REJECTED_NO_SIGNATURE

    def test_strict_mode_raises_on_rejection(self) -> None:
        _, pub = _make_keypair()
        verifier = KBSignatureVerifier(pub, mode=Mode.STRICT)
        with pytest.raises(KBSignatureError) as exc_info:
            verifier.verify({"profile_id": "tx"})  # no signature
        err = exc_info.value
        assert err.result == VerifyResult.REJECTED_NO_SIGNATURE
        assert err.profile_id == "tx"

    def test_strict_mode_does_not_raise_on_accepted(self) -> None:
        priv, pub = _make_keypair()
        verifier = KBSignatureVerifier(pub, mode=Mode.STRICT)
        signed = _sign_profile(priv, _sample_profile())
        # Should not raise on success.
        assert verifier.verify(signed) == VerifyResult.ACCEPTED


# ── Trusted-key loader ─────────────────────────────────────────────


class TestTrustedKeyLoader:
    def test_path_points_into_package(self) -> None:
        path = trusted_key_path()
        assert path.parent.name == "_trusted_keys"
        assert path.name == "v1.pub"

    def test_returns_none_when_file_absent(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # Redirect to an empty directory — no key file present.
        from sovyx.voice.health._mixer_kb import _signing as sig_mod

        empty = tmp_path / "_trusted_keys" / "v1.pub"
        monkeypatch.setattr(sig_mod, "trusted_key_path", lambda: empty)
        assert load_trusted_public_key() is None

    def test_loads_pem_ed25519_key(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        priv, pub = _make_keypair()
        pem = pub.public_bytes(
            encoding=Encoding.PEM,
            format=PublicFormat.SubjectPublicKeyInfo,
        )
        key_path = tmp_path / "v1.pub"
        key_path.write_bytes(pem)

        from sovyx.voice.health._mixer_kb import _signing as sig_mod

        monkeypatch.setattr(sig_mod, "trusted_key_path", lambda: key_path)
        loaded = load_trusted_public_key()
        assert isinstance(loaded, Ed25519PublicKey)

    def test_rejects_non_ed25519_key(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """A PEM file containing an RSA key must raise RuntimeError —
        the verifier requires Ed25519."""
        from cryptography.hazmat.primitives.asymmetric import rsa

        rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = rsa_key.public_key().public_bytes(
            encoding=Encoding.PEM,
            format=PublicFormat.SubjectPublicKeyInfo,
        )
        key_path = tmp_path / "v1.pub"
        key_path.write_bytes(pem)

        from sovyx.voice.health._mixer_kb import _signing as sig_mod

        monkeypatch.setattr(sig_mod, "trusted_key_path", lambda: key_path)
        with pytest.raises(RuntimeError, match="Ed25519"):
            load_trusted_public_key()


# ── End-to-end roundtrip ──────────────────────────────────────────


class TestEndToEnd:
    def test_sign_then_verify_roundtrip(self) -> None:
        priv, pub = _make_keypair()
        verifier = KBSignatureVerifier(pub, mode=Mode.STRICT)
        for profile_id in ("conexant_sn6180", "realtek_alc256", "cirrus_cs42l84"):
            profile = {
                "schema_version": 1,
                "profile_id": profile_id,
                "profile_version": 1,
                "codec_id_glob": f"{profile_id}*",
                "driver_family": "hda",
            }
            signed = _sign_profile(priv, profile)
            # Round-trip the signed profile through canonical payload
            # to ensure the byte form matches what was actually signed.
            assert len(base64.b64decode(signed[_SIGNATURE_FIELD])) == _ED25519_SIGNATURE_LEN
            assert verifier.verify(signed) == VerifyResult.ACCEPTED

    def test_signature_invariant_under_field_reorder(self) -> None:
        """A profile signed in one key order must still verify when
        the YAML is re-parsed in a different key order. This is the
        whole point of canonicalisation."""
        priv, pub = _make_keypair()
        verifier = KBSignatureVerifier(pub)
        profile = {
            "z_field": "last",
            "a_field": "first",
            "schema_version": 1,
            "profile_id": "test",
        }
        signed = _sign_profile(priv, profile)
        # Re-create with reversed key order.
        reordered = dict(reversed(list(signed.items())))
        assert verifier.verify(reordered) == VerifyResult.ACCEPTED
