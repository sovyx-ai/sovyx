"""Ed25519 signing helpers for calibration profiles.

Calibration uses the same Ed25519 cryptographic primitive as the mixer
KB (:mod:`sovyx.voice.health._mixer_kb._signing`), but with a
**JSON-based** canonical payload (calibration profiles persist as
``calibration.json`` under ``<data_dir>/<mind_id>/``, not YAML), and
a per-host operator-local signing key (generated on first use) rather
than the community-trust root key the mixer KB uses.

This module:

1. Re-exports ``Mode``, ``VerifyResult``, and ``KBSignatureError``
   from the mixer KB signing module so the calibration verifier
   surfaces the same operator-facing error types.
2. Provides :func:`canonical_calibration_payload` -- the JSON
   counterpart of mixer KB's YAML ``canonical_payload``. Identical
   contract: the verifier and the signer agree on the bytes-to-sign.

Signing capability (key generation + ``sign_calibration_payload``)
ships in T2.13's pre-tag-v0.30.15 hardening commit, NOT here. v0.30.15
ships unsigned profiles by default in ``Mode.LENIENT`` so the
persistence layer is exercised in production for one minor cycle
before the STRICT default flip in v0.30.17 (per
``feedback_staged_adoption``).

History: introduced in v0.30.15 as T2.7 of mission
``MISSION-voice-self-calibrating-system-2026-05-05.md`` Layer 2.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

# Re-exports for callers who treat calibration verification + the
# mixer KB verification as one operator-facing surface (same exit
# codes, same operator UX).
from sovyx.voice.health._mixer_kb._signing import (
    KBSignatureError,
    Mode,
    VerifyResult,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "KBSignatureError",
    "Mode",
    "VerifyResult",
    "canonical_calibration_payload",
]


def canonical_calibration_payload(payload: Mapping[str, object]) -> bytes:
    """Return the deterministic JSON byte form of a calibration signing payload.

    ``payload`` is the dict returned by
    :meth:`sovyx.voice.calibration.schema.CalibrationProfile.canonical_signing_payload`.
    Strips the ``signature`` field (defensive -- the canonical payload
    method already excludes it, but we strip again here for symmetry
    with the mixer KB signer + so callers can pass a full
    profile-dict-with-signature without footgunning the bytes).

    Both the signer and the verifier feed this exact byte string to
    Ed25519. Agreement on the canonical form is the contract; any
    deviation breaks signature verification even when the semantics
    are unchanged.

    Args:
        payload: The signing-payload mapping. Caller's responsibility
            to ensure keys + values are JSON-serializable; this
            function does not validate the schema.

    Returns:
        UTF-8-encoded bytes ready for ``Ed25519PublicKey.verify(sig, bytes)``.
    """
    stripped = {k: v for k, v in payload.items() if k != "signature"}
    canonical_str = json.dumps(
        stripped,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return canonical_str.encode("utf-8")
