"""Sign a Mixer KB YAML profile with the Ed25519 dev key.

Reads the YAML, strips any existing ``signature`` field,
canonicalises via :func:`sovyx.voice.health._mixer_kb._signing.canonical_payload`,
signs the canonical bytes with the v1 private key, and writes the
profile back with the new ``signature`` field appended at the top.

Usage::

    uv run python scripts/dev/sign_kb_profile.py \\
        --profile src/sovyx/voice/health/_mixer_kb/profiles/foo.yaml \\
        --key .signing-keys/sovyx_kb_v1.priv

Reference: MISSION-voice-100pct-autonomous-2026-04-25.md step 7.
"""

from __future__ import annotations

import argparse
import base64
import sys
from pathlib import Path

import yaml
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from sovyx.voice.health._mixer_kb._signing import canonical_payload

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PRIVATE_KEY_PATH = REPO_ROOT / ".signing-keys" / "sovyx_kb_v1.priv"


def _load_private_key(path: Path) -> Ed25519PrivateKey:
    """Load the Ed25519 private key from a PEM file."""
    pem_bytes = path.read_bytes()
    key = load_pem_private_key(pem_bytes, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        msg = (
            f"key at {path} is a {type(key).__name__}, not Ed25519PrivateKey — "
            f"KB signing requires Ed25519"
        )
        raise SystemExit(msg)
    return key


def sign_profile(profile_path: Path, private_key_path: Path) -> None:
    """Sign the profile in place. Mutates the YAML on disk."""
    raw = profile_path.read_bytes()
    profile = yaml.safe_load(raw)
    if not isinstance(profile, dict):
        msg = f"{profile_path} did not parse to a dict — got {type(profile).__name__}"
        raise SystemExit(msg)

    private_key = _load_private_key(private_key_path)
    payload = canonical_payload(profile)
    signature_bytes = private_key.sign(payload)
    signature_b64 = base64.b64encode(signature_bytes).decode("ascii")

    # Replace any existing signature; preserve all other fields.
    profile = {
        **{k: v for k, v in profile.items() if k != "signature"},
        "signature": signature_b64,
    }

    output = yaml.safe_dump(
        profile,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
        default_style=None,
    )
    profile_path.write_text(output, encoding="utf-8")
    print(f"signed: {profile_path}")  # noqa: T201
    print(f"signature: {signature_b64}")  # noqa: T201


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile",
        type=Path,
        required=True,
        help="Path to the YAML profile to sign",
    )
    parser.add_argument(
        "--key",
        type=Path,
        default=DEFAULT_PRIVATE_KEY_PATH,
        help="Path to the Ed25519 private key (PEM). Default: .signing-keys/sovyx_kb_v1.priv",
    )
    args = parser.parse_args()

    if not args.profile.is_file():
        print(f"profile not found: {args.profile}", file=sys.stderr)  # noqa: T201
        return 1
    if not args.key.is_file():
        print(  # noqa: T201
            f"private key not found: {args.key}\n"
            f"generate it first: uv run python scripts/dev/generate_kb_signing_key.py",
            file=sys.stderr,
        )
        return 1

    sign_profile(args.profile, args.key)
    return 0


if __name__ == "__main__":
    sys.exit(main())
