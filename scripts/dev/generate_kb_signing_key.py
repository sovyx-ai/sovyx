"""Generate the Ed25519 dev signing keypair for KB profiles.

One-shot utility. Generates an Ed25519 keypair, writes the PRIVATE key
to ``.signing-keys/sovyx_kb_v1.priv`` (mode 0600, gitignored), and the
PUBLIC key to ``src/sovyx/voice/health/_mixer_kb/_trusted_keys/v1.pub``
(committed to the repo).

Idempotent: refuses to overwrite existing keys. To rotate, follow the
procedure in ``docs/contributing/voice-kb-rotation.md``.

Usage::

    uv run python scripts/dev/generate_kb_signing_key.py

Reference: MISSION-voice-100pct-autonomous-2026-04-25.md step 7.
"""

from __future__ import annotations

import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

REPO_ROOT = Path(__file__).resolve().parents[2]
PRIVATE_KEY_PATH = REPO_ROOT / ".signing-keys" / "sovyx_kb_v1.priv"
PUBLIC_KEY_PATH = (
    REPO_ROOT / "src" / "sovyx" / "voice" / "health" / "_mixer_kb" / "_trusted_keys" / "v1.pub"
)


def main() -> int:
    """Generate keypair, write to disk, return exit code."""
    if PRIVATE_KEY_PATH.exists():
        print(  # noqa: T201
            f"refusing to overwrite existing private key at {PRIVATE_KEY_PATH}.\n"
            "delete it first if you intend to rotate keys.",
            file=sys.stderr,
        )
        return 1
    if PUBLIC_KEY_PATH.exists():
        print(  # noqa: T201
            f"refusing to overwrite existing public key at {PUBLIC_KEY_PATH}.\n"
            "delete it first if you intend to rotate keys.",
            file=sys.stderr,
        )
        return 1

    PRIVATE_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    PUBLIC_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)

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

    PRIVATE_KEY_PATH.write_bytes(private_pem)
    PUBLIC_KEY_PATH.write_bytes(public_pem)

    # On POSIX hosts, restrict private key permissions to 0o600 so
    # other users on the dev machine can't read it. Windows ignores
    # POSIX modes; the file lives under .signing-keys/ which is
    # gitignored.
    if sys.platform != "win32":
        PRIVATE_KEY_PATH.chmod(0o600)

    print(f"wrote private key: {PRIVATE_KEY_PATH}")  # noqa: T201
    print(f"wrote public key:  {PUBLIC_KEY_PATH}")  # noqa: T201
    print(  # noqa: T201
        "\nThe public key is committed to the repo. The private key "
        "STAYS LOCAL — it is gitignored under .signing-keys/.",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
