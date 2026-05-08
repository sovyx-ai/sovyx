"""Operator-driven Ed25519 signing-key generation for calibration profiles.

Mission: BT.B.3 of ``MISSION-voice-v0_32_0-structural-closure-2026-05-08.md``.

Pre-v0.32.0 only ``scripts/dev/generate_calibration_signing_key.py`` (a
dev-only script in the repo root) produced a usable signing key. Operators
running shipped Sovyx had no surface to generate the key, which gated the
:data:`Mode.STRICT` default flip planned for v0.33.0+ on a wizard-driven
generator landing first (see ``_signing.py:26-31``).

This module is the foundation: a single function, used by both the CLI
(``sovyx voice generate-signing-key``) and the dashboard
(``POST /api/voice/calibration/generate-signing-key``), that:

1. Generates an Ed25519 keypair via :mod:`cryptography`.
2. Persists the private key to ``<data_dir>/<mind_id>/calibration.signing-key.priv``
   in unencrypted PKCS8 PEM (matching the dev script).
3. Persists the public key to ``<data_dir>/<mind_id>/calibration.signing-key.pub``
   in SubjectPublicKeyInfo PEM.
4. Sets POSIX permissions ``0o600`` on the private key + ``0o644`` on the
   public key. Windows ignores these chmod calls (POSIX semantics absent);
   the file ACLs default to "owner only" via ``Path.write_bytes`` opening
   with the default umask, which on Windows means the current user has
   read/write through the inherited NTFS ACL.
5. Refuses to overwrite an existing key unless ``force=True``.
6. Returns the SHA-256 fingerprint (first 8 hex chars) of the public key
   PEM bytes — both the CLI and the dashboard surface this as a stable
   short identifier for operators.

The canonical paths (``calibration.signing-key.priv`` /
``calibration.signing-key.pub``) under ``<data_dir>/<mind_id>/`` are NEW
in v0.32.0 — distinct from the dev script's repo-root paths. Per-mind
keys mean a multi-mind operator can have independent signing keys per
mind (informational at v0.32.0 since calibration is single-mind via the
sentinel resolver; the path layout is forward-compatible with the
multi-mind future).

The :data:`Mode.STRICT` default flip is NOT performed here — that is
v0.33.0+ work after telemetry confirms wide adoption (see
``feedback_staged_adoption``). v0.32.0 ships only the foundation.
"""

from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from sovyx.observability.logging import get_logger
from sovyx.observability.privacy import short_hash

if TYPE_CHECKING:
    from pathlib import Path

logger = get_logger(__name__)

PRIVATE_KEY_FILENAME: Final[str] = "calibration.signing-key.priv"
"""Filename for the per-mind unencrypted Ed25519 private signing key (PKCS8 PEM)."""

PUBLIC_KEY_FILENAME: Final[str] = "calibration.signing-key.pub"
"""Filename for the per-mind Ed25519 public signing key (SubjectPublicKeyInfo PEM)."""


class SigningKeyExistsError(Exception):
    """Raised when a signing key already exists and ``force`` is not set."""

    def __init__(self, *, private_path: Path, public_path: Path) -> None:
        self.private_path = private_path
        self.public_path = public_path
        super().__init__(
            f"calibration signing key already exists at {private_path} / "
            f"{public_path}. Pass force=True to overwrite (existing signed "
            "profiles will need to be re-signed against the new key).",
        )


@dataclass(frozen=True, slots=True)
class GeneratedSigningKey:
    """Result of a successful key-generation call."""

    private_key_path: Path
    """Absolute path to the persisted private key (PKCS8 PEM, ``0o600`` POSIX)."""

    public_key_path: Path
    """Absolute path to the persisted public key (SubjectPublicKeyInfo PEM, ``0o644`` POSIX)."""

    public_key_pem: str
    """The public key PEM as a UTF-8 string (callers may surface this directly)."""

    fingerprint_short: str
    """First 8 hex chars of SHA-256(public_key_pem_bytes); stable operator-facing id."""


def signing_key_paths(*, data_dir: Path, mind_id: str) -> tuple[Path, Path]:
    """Return ``(private_key_path, public_key_path)`` for the given mind.

    These are the canonical v0.32.0+ on-disk locations:

    * ``<data_dir>/<mind_id>/calibration.signing-key.priv``
    * ``<data_dir>/<mind_id>/calibration.signing-key.pub``

    Distinct from the dev script's repo-root paths
    (``.signing-keys/sovyx_calibration_v1.priv`` /
    ``src/sovyx/voice/calibration/_trusted_keys/v1.pub``).
    """
    base = data_dir / mind_id
    return (base / PRIVATE_KEY_FILENAME, base / PUBLIC_KEY_FILENAME)


def signing_key_exists(*, data_dir: Path, mind_id: str) -> bool:
    """Return True when EITHER half of the keypair already exists on disk.

    The dashboard's ``status`` field calls this to render
    ``"Generated"`` vs ``"Not yet generated"``. We treat partial
    presence (only ``.priv`` OR only ``.pub``) as "exists" for safety:
    overwriting half a keypair via ``force=False`` would leave a
    mismatched pair on disk, which would silently break verification
    (the loader reads ``v1.pub`` from the repo-shipped trust store, not
    these per-mind paths — but a mismatched pair under the per-mind
    dir would fail any future per-mind verifier the same way).
    """
    priv, pub = signing_key_paths(data_dir=data_dir, mind_id=mind_id)
    return priv.exists() or pub.exists()


def public_key_fingerprint_short(public_key_pem: bytes) -> str:
    """Return the first 8 hex chars of SHA-256 over the PEM bytes.

    Used by the CLI + dashboard as a stable short identifier the
    operator can copy/paste to confirm the key in use. Eight hex chars
    = 32 bits of entropy — enough to disambiguate a handful of keys
    per host without being so long it discourages copy-paste.
    """
    return hashlib.sha256(public_key_pem).hexdigest()[:8]


def generate_signing_key(
    *,
    data_dir: Path,
    mind_id: str,
    force: bool = False,
    output_path: Path | None = None,
    source: str = "cli",
) -> GeneratedSigningKey:
    """Generate + persist an Ed25519 signing keypair for ``mind_id``.

    Args:
        data_dir: Sovyx data directory (canonical: ``EngineConfig.data_dir``).
        mind_id: Owning mind. Per-mind layout is forward-compatible
            with the multi-mind future even though calibration is
            single-mind today (sentinel resolver flow).
        force: When True, overwrite any existing keypair. When False
            (default), refuse with :class:`SigningKeyExistsError`.
        output_path: Optional override for the private-key location.
            When provided, the public key is written next to it with
            the ``.pub`` filename instead of ``.priv``. ``data_dir`` /
            ``mind_id`` are still used for the structured event.
        source: ``"cli"`` or ``"dashboard"`` — surfaced via the
            structured event for telemetry partitioning.

    Returns:
        A :class:`GeneratedSigningKey` carrying the absolute paths +
        the public key PEM string + the short SHA-256 fingerprint.

    Raises:
        SigningKeyExistsError: when ``force=False`` and either half of
            the keypair already exists at the resolved location.
        OSError: when the filesystem refuses the write (permission /
            read-only mount / disk full). The caller decides whether
            to surface this to the operator or treat it as a 500.

    Side effects:
        * Creates parent directories as needed (``parents=True``).
        * Writes two files (``private_path`` + ``public_path``).
        * On POSIX, ``chmod`` the private key to ``0o600`` and the
          public key to ``0o644``. On Windows, the chmod is a no-op
          (POSIX semantics absent); the inherited NTFS ACL handles
          access control.
        * Emits one structured ``voice.calibration.signing_key.generated``
          event at INFO level with ``source``, ``mode``, ``mind_id_hash``,
          and ``fingerprint_short``.
    """
    if output_path is not None:
        private_path = output_path
        public_path = output_path.with_name(
            output_path.stem.removesuffix(".priv") + ".pub",
        )
    else:
        private_path, public_path = signing_key_paths(
            data_dir=data_dir,
            mind_id=mind_id,
        )

    if not force and (private_path.exists() or public_path.exists()):
        raise SigningKeyExistsError(
            private_path=private_path,
            public_path=public_path,
        )

    # Ensure parent directory exists (per-mind dir may not yet exist
    # if the operator hasn't run any mind-specific command yet).
    private_path.parent.mkdir(parents=True, exist_ok=True)
    public_path.parent.mkdir(parents=True, exist_ok=True)

    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()

    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    private_path.write_bytes(private_pem)
    public_path.write_bytes(public_pem)

    # POSIX permissions — owner read/write only on the private key,
    # world-readable on the public key (matches SSH-style conventions).
    # Windows ignores these chmod calls (POSIX semantics absent); the
    # NTFS ACL inherited from the parent directory governs access.
    if sys.platform != "win32":
        private_path.chmod(0o600)
        public_path.chmod(0o644)

    fingerprint_short = public_key_fingerprint_short(public_pem)

    logger.info(
        "voice.calibration.signing_key.generated",
        source=source,
        mode="forced" if force else "created",
        mind_id_hash=short_hash(mind_id),
        fingerprint_short=fingerprint_short,
    )

    return GeneratedSigningKey(
        private_key_path=private_path,
        public_key_path=public_path,
        public_key_pem=public_pem.decode("utf-8"),
        fingerprint_short=fingerprint_short,
    )


__all__ = [
    "PRIVATE_KEY_FILENAME",
    "PUBLIC_KEY_FILENAME",
    "GeneratedSigningKey",
    "SigningKeyExistsError",
    "generate_signing_key",
    "public_key_fingerprint_short",
    "signing_key_exists",
    "signing_key_paths",
]
