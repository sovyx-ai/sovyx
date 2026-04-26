# Voice KB Signing Key — Rotation Procedure

Internal-facing operator guide for rotating the Ed25519 signing key
used to sign Mixer KB profiles.

## Current key (v1) — dev lifecycle

The v1 key was generated in Step 7 of
`MISSION-voice-100pct-autonomous-2026-04-25.md`. It is a **dev key**:

- Public key: `src/sovyx/voice/health/_mixer_kb/_trusted_keys/v1.pub`
  (committed to the repo, ships with every wheel).
- Private key: `.signing-keys/sovyx_kb_v1.priv` (gitignored — STAYS LOCAL).
- Cycle: v0.23.x.
- Mode: `Mode.LENIENT` (verifier emits WARN on bad/missing signatures
  but doesn't reject the profile).

The dev key is **not suitable for production** — anyone with write
access to the repo can re-generate it. The rotation procedure below
describes the production-grade replacement for v0.24.0+.

---

## When to rotate

- **Scheduled:** once per major release (v0.X → v1.0 stays compatible;
  v1.0+ rotates).
- **Compromise:** immediately, regardless of release cycle. Compromise
  signals: private key was committed to the repo accidentally + we
  don't know who else has it; CI/CD pipeline that holds the key was
  breached; a maintainer with key access left the project.

---

## Production rotation procedure (v0.24.0+)

### Step 1 — Generate the new keypair on an HSM

The production key MUST be generated and stored on a hardware
security module (HSM) — YubiKey, AWS KMS, GCP Cloud KMS, or
equivalent. Never on a developer laptop.

```bash
# Example using YubiKey (yubico-piv-tool):
yubico-piv-tool -a generate -s 9c -A ED25519 -o /tmp/sovyx_kb_v2.pub
yubico-piv-tool -a verify-pin -a selfsign-certificate -s 9c \
    -S "/CN=Sovyx KB Signing v2/" -i /tmp/sovyx_kb_v2.pub -o /tmp/sovyx_kb_v2.crt
```

The HSM holds the private key. Signing operations call into the
HSM via `pkcs11` or vendor-specific tooling. The PEM-extracted
public key is what ships in the repo.

### Step 2 — Ship the new public key alongside the old one

```bash
# Copy the new public key to the trusted_keys dir.
cp /tmp/sovyx_kb_v2.pub src/sovyx/voice/health/_mixer_kb/_trusted_keys/v2.pub
```

The loader (`load_trusted_public_key`) currently loads only `v1.pub`.
For an overlapping rotation window (one minor-version cycle):

1. Update `_signing.py` to load both `v1.pub` AND `v2.pub` and
   construct a multi-key verifier that accepts ANY of the trusted
   keys.
2. Bump `pyproject.toml` version to v0.24.0.
3. Document the rotation in `docs/migration/`.

### Step 3 — Re-sign every shipped first-party profile with v2

```bash
# Sign each profile with the new key.
for profile in src/sovyx/voice/health/_mixer_kb/profiles/*.yaml; do
    uv run python scripts/dev/sign_kb_profile.py \
        --profile "$profile" \
        --key /path/to/v2.priv  # OR HSM-backed signing
done
```

Verify each profile re-signs cleanly + the v1.pub verifier rejects
the v2-signed payload (cryptographic confidence: same content + new
key = different signature).

### Step 4 — Soak v0.24.0 for one minor cycle

Ship v0.24.0 with multi-key verification (v1 + v2) active. Operators
on existing profiles signed under v1 continue to verify cleanly.
Operators receiving v2-signed profiles also verify.

### Step 5 — Drop v1 in v0.25.0

Remove `v1.pub` from `_trusted_keys/`. The loader falls back to
`v2.pub` only. Profiles still signed under v1 are now unverifiable
and skip in `Mode.STRICT` (warn in `Mode.LENIENT` until Phase F2
flips strict-mode default).

---

## Compromise response

If the v1 private key is suspected compromised:

1. **Within 24 hours:** publish a security advisory at
   https://github.com/sovyx-ai/sovyx/security/advisories. Mark
   v0.23.x as "compromised signing key — upgrade to v0.23.X+1
   immediately".
2. **Patch release v0.23.X+1:**
   - Generate v2 keypair on HSM (Step 1 above).
   - Ship v2.pub. **Do NOT keep v1.pub** — every v1-signed profile
     is now untrusted regardless of content.
   - Re-sign every shipped first-party profile with v2.
   - Update `Mode.LENIENT` → `Mode.STRICT` in the loader so any
     v1-signed profile that survives a re-deploy gets rejected.
3. **Operator action required:** community-contributed profiles
   signed under v1 must be re-signed by the contributor against
   v2 before they can be re-merged. The community contribution
   guide gets a notice + the existing PR queue is purged.

---

## Why this is structured this way

The procedure mirrors industry-standard practice from:

- **Apple Developer ID** — single trusted root, multi-year cycle,
  HSM-backed, immediate-revoke-on-compromise.
- **Mozilla add-on signing** — multi-key trust store with overlapping
  windows for rotation.
- **Sigstore** — short-lived signing keys with transparency log;
  Sovyx's profile catalog is small enough that we don't need
  Sigstore's complexity yet.
- **GitHub commit signing** — SSH key + GPG key trust models proven
  at scale.

The Sovyx audio team's commitment: never ship a profile signed with a
key the team doesn't control end-to-end. The dev v1 key is acceptable
for v0.23.x because the cycle is short and the threat model is
"developer experimenting locally". Production deployments require the
v2 HSM rotation.

---

## Reference

- [Step 7 mission entry](../../docs-internal/diagnostics/MISSION-voice-100pct-autonomous-2026-04-25.md)
- [Signing infrastructure source](../../src/sovyx/voice/health/_mixer_kb/_signing.py)
- [Generation utility](../../scripts/dev/generate_kb_signing_key.py)
- [Profile signing utility](../../scripts/dev/sign_kb_profile.py)
- [Apple Developer ID rotation](https://developer.apple.com/documentation/security)
- [Mozilla signing service](https://github.com/mozilla-services/autograph)
- [Sigstore Cosign](https://github.com/sigstore/cosign)
