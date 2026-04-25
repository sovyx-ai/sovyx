"""Ed25519 signing + verification for Mixer KB profiles (F2 foundation).

Foundation for the F2 acceptance gate: every shipped + community-
contributed KB profile MUST carry a valid Ed25519 signature
verifiable against a trusted public key bundled with Sovyx.

Why signing matters
===================

KB profiles drive ALSA mixer write operations — code that mutates
hardware state. A maliciously-crafted profile could:

* Set every mixer control to 0 dB → permanent device degradation.
* Issue device-specific evil sequences that reach kernel ioctls.
* Mask other profiles by squatting on a popular ``profile_id`` +
  matching a wide ``codec_id_glob``, redirecting users to its
  preset.

Pre-F2, the schema accepted a ``signature`` field but did not
verify it (loader.py emits a DEBUG when present, that's all).
F2 closes the gap: any profile without a verifiable signature is
rejected (or warned, in lenient mode).

The signing model
=================

* **Single trusted root key (initial)** — Sovyx ships one trusted
  Ed25519 public key per release. Profiles signed with the
  matching private key (held by Sovyx maintainers) verify; all
  others are rejected. Multi-key + key rotation is a deliberate
  follow-up — getting the single-key path right first is the
  enterprise pattern.
* **Canonical content** — the signed payload is the YAML profile
  with the ``signature`` field STRIPPED, then re-serialised in a
  deterministic byte form (sorted keys, no aliases, no flow
  style). Without canonicalisation, a profile's signature would
  break under any whitespace / key-order edit even when the
  semantics are unchanged.
* **Lenient default during adoption** — ``Mode.LENIENT`` (the
  default) emits a structured WARN
  (``voice.kb.signature.invalid``) and returns a non-fatal
  verdict; ``Mode.STRICT`` raises :class:`KBSignatureError`. Per
  the staged-adoption discipline (CLAUDE.md), the loader can
  start in lenient mode while shipped profiles are signed and
  the canonical signing pipeline is verified end-to-end; flip to
  strict in a follow-up commit.

Public API
==========

Three call-site primitives:

* :func:`canonical_payload` — deterministic byte form of a YAML
  profile dict (signature field stripped). Pure function, no
  crypto. The same primitive is used by both the signer (cloud
  side) and the verifier (this side) so they agree on the bytes.
* :class:`KBSignatureVerifier` — wraps an
  :class:`Ed25519PublicKey` plus a :class:`Mode`. Provides
  :meth:`verify` returning a :class:`VerifyResult` enum. Pure
  per-call (no I/O, no global state).
* :func:`load_trusted_public_key` — loads the package-shipped
  trusted public key from disk. Returns ``None`` if the file is
  absent (enables tests + first-boot scenarios where the key
  hasn't been embedded yet).

Reference: MISSION-voice-mixer-enterprise-refactor-2026-04-25 §4
(KB profile schema v2 + signing), §3.12 (mixer band-aid catalog),
F2 task; cryptography library Ed25519 docs
(``cryptography.hazmat.primitives.asymmetric.ed25519``);
sovyx.license precedent (existing JWT/Ed25519 client validator).
"""

from __future__ import annotations

import base64
import binascii
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_pem_public_key

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = get_logger(__name__)


_SIGNATURE_FIELD = "signature"
"""The YAML field that carries the base64-encoded Ed25519 signature.
Stripped from the payload before canonicalisation so the signature
is over the *content* of the profile, not over a self-referential
field."""


_TRUSTED_KEY_FILENAME = "v1.pub"
"""Name of the package-shipped trusted public key file. The file
lives at ``src/sovyx/voice/health/_mixer_kb/_trusted_keys/v1.pub``
in PEM format. Versioned filename anticipates key rotation: when a
new root key replaces v1, ``v2.pub`` ships alongside it for an
overlapping verification window."""


# ── Result + mode enums ────────────────────────────────────────────


class VerifyResult(StrEnum):
    """Outcome of a verification attempt.

    StrEnum (anti-pattern #9) so value-based comparison is stable
    across pytest-xdist namespace duplication and JSON serialisation
    matches the structured-log "verdict" field verbatim.
    """

    ACCEPTED = "accepted"
    """Profile carries a valid signature from a trusted key."""

    REJECTED_NO_SIGNATURE = "rejected_no_signature"
    """Profile does not carry a ``signature`` field at all."""

    REJECTED_BAD_SIGNATURE = "rejected_bad_signature"
    """Signature field present but does not verify against the
    trusted public key (tampered profile, wrong key, or signature
    over different content)."""

    REJECTED_NO_TRUSTED_KEY = "rejected_no_trusted_key"
    """No trusted public key is configured on the verifier — every
    verify call short-circuits to this verdict. Caller (loader)
    decides whether to warn-and-skip or hard-fail."""

    REJECTED_MALFORMED_SIGNATURE = "rejected_malformed_signature"
    """Signature field present but isn't valid base64 / has wrong
    decoded length (Ed25519 sigs are exactly 64 bytes)."""


class Mode(StrEnum):
    """Verification strictness."""

    LENIENT = "lenient"
    """Bad / missing signature emits a structured WARN but
    :meth:`KBSignatureVerifier.verify` returns the verdict — caller
    decides whether to use the profile. Default during the F2
    adoption window."""

    STRICT = "strict"
    """Bad / missing signature raises :class:`KBSignatureError`.
    Use after the F2 adoption window has confirmed all shipped
    profiles are properly signed."""


_ED25519_SIGNATURE_LEN = 64
"""Ed25519 signatures are exactly 64 bytes (RFC 8032 §5.1.6).
Anything else is malformed — reject loudly."""


# ── Exceptions ─────────────────────────────────────────────────────


class KBSignatureError(Exception):
    """Raised by :class:`KBSignatureVerifier` in :class:`Mode.STRICT`
    when verification fails.

    Carries the :class:`VerifyResult` so the caller can branch on
    the specific failure class without parsing the message.
    """

    def __init__(self, result: VerifyResult, *, profile_id: str = "") -> None:
        self.result = result
        self.profile_id = profile_id
        msg = f"KB signature verification failed: {result.value}"
        if profile_id:
            msg = f"{msg} (profile_id={profile_id!r})"
        super().__init__(msg)


# ── Canonical payload ──────────────────────────────────────────────


def canonical_payload(profile_dict: Mapping[str, Any]) -> bytes:
    """Return the deterministic byte form of ``profile_dict``.

    Strips the ``signature`` field, then serialises with sorted
    keys + no aliases + block style. The result is what BOTH the
    signer (cloud side) and the verifier (this module) feed into
    Ed25519 — agreement on canonical form is the contract.

    Args:
        profile_dict: Parsed-from-YAML profile as a dict-like
            mapping. Caller's responsibility to ensure it parses
            cleanly; this function does not validate the schema.

    Returns:
        UTF-8-encoded bytes ready to feed to
        :meth:`Ed25519PublicKey.verify`.
    """
    stripped = {k: v for k, v in profile_dict.items() if k != _SIGNATURE_FIELD}
    # default_flow_style=False → block style (multiline mappings).
    # sort_keys=True → deterministic key order.
    # allow_unicode=True → preserve the input's char repertoire.
    # default_style=None → quote only when necessary (smaller bytes).
    canonical_str = yaml.safe_dump(
        stripped,
        default_flow_style=False,
        sort_keys=True,
        allow_unicode=True,
        default_style=None,
    )
    return canonical_str.encode("utf-8")


# ── Verifier ───────────────────────────────────────────────────────


class KBSignatureVerifier:
    """Per-trusted-key Ed25519 verifier for KB profiles.

    Construct once at loader init with the trusted public key + mode;
    call :meth:`verify` per profile. No I/O, no global state — safe
    to share across threads.

    Args:
        public_key: The trusted Ed25519 public key. ``None`` is
            permitted (e.g. tests, first-boot scenarios where no
            key is bundled) — every verify call short-circuits to
            :attr:`VerifyResult.REJECTED_NO_TRUSTED_KEY`.
        mode: :class:`Mode.LENIENT` (default) returns the verdict;
            :class:`Mode.STRICT` raises :class:`KBSignatureError`
            on rejection.
    """

    def __init__(
        self,
        public_key: Ed25519PublicKey | None,
        *,
        mode: Mode = Mode.LENIENT,
    ) -> None:
        self._public_key = public_key
        self._mode = mode

    @property
    def has_trusted_key(self) -> bool:
        """Whether a trusted public key is configured."""
        return self._public_key is not None

    @property
    def mode(self) -> Mode:
        return self._mode

    def verify(self, profile_dict: Mapping[str, Any]) -> VerifyResult:
        """Verify ``profile_dict``'s ``signature`` field.

        Args:
            profile_dict: Parsed YAML profile. Must include a
                ``signature`` field (base64 Ed25519 signature) for
                non-rejection paths.

        Returns:
            The verdict. In :class:`Mode.LENIENT` every verdict is
            returned and a structured WARN fires for non-ACCEPTED
            results; in :class:`Mode.STRICT` non-ACCEPTED verdicts
            raise :class:`KBSignatureError`.

        Raises:
            KBSignatureError: Only in :class:`Mode.STRICT` when the
                verdict is not :attr:`VerifyResult.ACCEPTED`.
        """
        result = self._verify_inner(profile_dict)
        profile_id = str(profile_dict.get("profile_id", ""))
        if result is VerifyResult.ACCEPTED:
            logger.debug(
                "voice.kb.signature.accepted",
                profile_id=profile_id,
            )
            return result

        # Non-accepted — emit structured event regardless of mode.
        logger.warning(
            "voice.kb.signature.invalid",
            profile_id=profile_id,
            verdict=result.value,
            mode=self._mode.value,
            action_required=(
                "regenerate signature with the canonical Sovyx KB "
                "signing key, OR confirm the profile_id is shipped "
                "by Sovyx (community profiles need community-key "
                "signature, not implemented yet)"
            ),
        )
        if self._mode is Mode.STRICT:
            raise KBSignatureError(result, profile_id=profile_id)
        return result

    def _verify_inner(self, profile_dict: Mapping[str, Any]) -> VerifyResult:
        """Pure verification — no logging, no raising."""
        if self._public_key is None:
            return VerifyResult.REJECTED_NO_TRUSTED_KEY
        sig_b64 = profile_dict.get(_SIGNATURE_FIELD)
        if not sig_b64:
            return VerifyResult.REJECTED_NO_SIGNATURE
        if not isinstance(sig_b64, str):
            # YAML may parse as bytes / int / etc. Reject anything
            # that isn't a string.
            return VerifyResult.REJECTED_MALFORMED_SIGNATURE
        try:
            signature = base64.b64decode(sig_b64, validate=True)
        except (binascii.Error, ValueError):
            return VerifyResult.REJECTED_MALFORMED_SIGNATURE
        if len(signature) != _ED25519_SIGNATURE_LEN:
            return VerifyResult.REJECTED_MALFORMED_SIGNATURE
        payload = canonical_payload(profile_dict)
        try:
            self._public_key.verify(signature, payload)
        except InvalidSignature:
            return VerifyResult.REJECTED_BAD_SIGNATURE
        return VerifyResult.ACCEPTED


# ── Trusted-key loader ─────────────────────────────────────────────


def trusted_key_path() -> Path:
    """Path to the package-shipped trusted public key.

    The file lives at
    ``src/sovyx/voice/health/_mixer_kb/_trusted_keys/<filename>``
    and is bundled as package data. Returning the path (rather
    than the loaded key) gives callers the option to log
    presence/absence at boot without forcing a key parse.
    """
    return Path(__file__).parent / "_trusted_keys" / _TRUSTED_KEY_FILENAME


def load_trusted_public_key() -> Ed25519PublicKey | None:
    """Load the package-shipped trusted Ed25519 public key.

    Returns ``None`` if the key file is absent (this is legitimate
    during F2 adoption — the key file is added in a separate commit
    once the cloud-side signer is in place). Callers must tolerate
    None and fall back to LENIENT-mode-with-no-key behaviour.

    The file is read as PEM (matches the
    :mod:`sovyx.license` precedent which also ships a PEM public
    key). DER would be more compact but PEM is operator-friendly:
    the file opens in any text editor, the format is self-
    documenting (``BEGIN PUBLIC KEY`` header), and it survives
    line-ending normalisation by version control.
    """
    path = trusted_key_path()
    if not path.is_file():
        logger.info(
            "voice.kb.trusted_key.absent",
            path=str(path),
            action_required=(
                "ship the trusted public key alongside this release "
                "OR run in LENIENT mode (KB profile signatures will "
                "be unverifiable until the key is shipped)"
            ),
        )
        return None
    pem_bytes = path.read_bytes()
    key = load_pem_public_key(pem_bytes)
    if not isinstance(key, Ed25519PublicKey):
        type_name = type(key).__name__
        msg = (
            f"trusted_key file at {path} is a {type_name}, not "
            f"Ed25519PublicKey — KB signing requires Ed25519"
        )
        raise RuntimeError(msg)
    return key


__all__ = [
    "KBSignatureError",
    "KBSignatureVerifier",
    "Mode",
    "VerifyResult",
    "canonical_payload",
    "load_trusted_public_key",
    "trusted_key_path",
]
