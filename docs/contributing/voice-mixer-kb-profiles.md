# Contributing a Voice Mixer KB Profile

The Voice Mixer KB (knowledge base) cascade is the L2.5 layer of Sovyx's
defense-in-depth audio architecture. Each KB profile encodes the
ALSA mixer presets that make a specific codec / hardware combination
work reliably for voice capture.

This guide explains how to contribute a profile for hardware Sovyx
doesn't yet have a first-party profile for.

> **TL;DR:** capture HIL evidence (`amixer dump` before + after,
> validation WAV), draft the YAML, sign it with the community key,
> open a PR with the four required artefacts, get review.

---

## Why Sovyx needs your profile

The mission's §3.12 codec coverage matrix problem: every codec ships
with a different "factory default" mixer state, and most of them are
wrong for voice capture. Some clip the signal (Mic Boost = max);
others attenuate it below the VAD floor (Capture = 0). The L2.5
cascade applies a per-codec preset that puts the chain inside the
"healthy" envelope.

Sovyx ships **one** first-party profile in v0.23.0 (the Conexant
SN6180 in the operator's VAIO that motivated the entire refactor).
Every other codec falls through to the **AGC2 closed-loop digital
gain** at Layer 4, which works but is less precise than a properly-
tuned per-codec preset.

Community-contributed profiles close that gap. Each one you
contribute helps every other Sovyx user with the same hardware.

---

## What "HIL attestation" means

HIL = Hardware-In-the-Loop. We require that profiles be verified
on physical hardware, not extrapolated from datasheets. This is the
same standard Apple, Google (ChromeOS CRAS overlays), and Spotify
(per-platform encoder profiles) hold themselves to.

You attest by capturing four artefacts during the pilot run on your
hardware:

1. **Pre-apply `amixer dump`** — the saturated/attenuated factory
   state that motivated the profile. Lives at
   `tests/fixtures/voice/mixer/<profile_id>_before.txt`.
2. **Post-apply `amixer dump`** — the state after Sovyx applied
   the recommended preset. Lives at
   `tests/fixtures/voice/mixer/<profile_id>_after.txt`.
3. **Capture WAV** — 3 s of conversational audio captured AFTER
   apply, used to verify the validation gates pass. Lives at
   `tests/fixtures/voice/mixer/<profile_id>_capture.wav`.
4. **Pytest fixture-driven test** — pins the recommended preset
   values + validation gate envelope. Lives at
   `tests/unit/voice/health/mixer_kb/test_<profile_id>.py`.

The `tests/unit/voice/health/mixer_kb/test_conexant_sn6180.py` test
shipped with v0.23.0 is the reference template.

---

## Step-by-step: contributing a profile

### 1. Identify your codec

```bash
cat /proc/asound/card0/codec#0 | head -10
```

Look for the `Vendor Id` + `Codec` lines. The codec id pattern
typically appears as `xxxx:yyyy` hex (e.g., `14F1:5045` for the SN6180).

### 2. Capture pre-apply state

Reproduce the broken voice-capture behaviour first (Sovyx logs
`voice.deaf.detected` at boot), then capture:

```bash
amixer -c 0 dump > tests/fixtures/voice/mixer/<your_profile_id>_before.txt
```

### 3. Determine the recommended preset

Manually adjust `amixer` controls until voice capture works:

```bash
amixer -c 0 sset 'Capture' 50%
amixer -c 0 sset 'Mic Boost' 33%
# ... iterate until Sovyx's RMS + Silero gates pass
```

Capture the post-apply state:

```bash
amixer -c 0 dump > tests/fixtures/voice/mixer/<your_profile_id>_after.txt
```

### 4. Capture validation audio

Speak into the mic for 3 seconds while running the Sovyx capture
diagnostic:

```bash
sovyx doctor voice --capture-fixture tests/fixtures/voice/mixer/<your_profile_id>_capture.wav
```

The captured WAV must:
- RMS in [-38, -12] dBFS (or whatever range your tests pin)
- Silero VAD probability ≥ 0.5
- Peak ≤ -3 dBFS

### 5. Draft the YAML profile

Copy `src/sovyx/voice/health/_mixer_kb/profiles/conexant_sn6180_vaio_vjfe69.yaml`
as a template. Update:

- `profile_id`: lowercase + underscores (e.g., `realtek_alc256_thinkpad_t14`)
- `codec_id_glob`: matches your codec from step 1
- `system_vendor_glob` / `system_product_glob`: from `dmidecode -s system-product-name`
- `factory_signature`: encodes the broken pre-apply state from step 2
- `recommended_preset.controls`: the values from step 3
- `validation`: gates calibrated to your hardware (see `validation` block in the SN6180 reference)
- `verified_on`: your host's identity + date + your handle
- `contributed_by`: your handle

### 6. Sign the profile

Production-grade signing requires the Sovyx maintainer key (HSM-
backed). For community contributions, the dev key v1 in
`scripts/dev/generate_kb_signing_key.py` works for local testing
but profiles signed with it CANNOT be merged.

Open the PR **without** a signature; a maintainer signs the profile
during review using the production key.

```bash
# Local testing only:
uv run python scripts/dev/sign_kb_profile.py \
    --profile src/sovyx/voice/health/_mixer_kb/profiles/<your_profile_id>.yaml
```

### 7. Write the test

Mirror `tests/unit/voice/health/mixer_kb/test_conexant_sn6180.py`.
Pin: file presence, schema validity, recommended preset values,
validation gate envelope, provenance fields.

### 8. Open a PR

PR must include:
- `<profile_id>.yaml` under `src/sovyx/voice/health/_mixer_kb/profiles/`
- The three fixture files under `tests/fixtures/voice/mixer/`
- The fixture-driven pytest under `tests/unit/voice/health/mixer_kb/`

PR description must include:
- The `amixer dump` snippets (operator-readable diff between before + after)
- Output of the `sovyx doctor voice` capture diagnostic showing
  RMS / Silero / peak values pre- and post-apply
- A short narrative explaining what was broken on your hardware
  and how the preset fixes it

### 9. Reviewer checklist

A reviewer pair must validate:

- [ ] HIL attestation is real (not extrapolated). One reviewer
  must confirm matching hardware OR explicitly document an
  exception.
- [ ] Validation gates align with the captured WAV's measured
  envelope (no silent fudging).
- [ ] No `match_keys` collision with shipped first-party profiles.
- [ ] Maintainer signs the profile + commits the signature.
- [ ] Schema test (`test_<profile_id>.py`) passes.
- [ ] `mkdocs build --strict` and full quality gates pass.

---

## What if my hardware also has issues at the OS level (PipeWire / WirePlumber)?

The KB cascade is Layer 2.5. If your hardware needs a PipeWire
echo-cancel module (Layer 1) or a UCM verb (Layer 2), those land
upstream — open separate issues against PipeWire / alsa-ucm-conf
respectively. Document the dependency in your profile's
`known_caveats`.

---

## Reference

- [SN6180 first-party profile](../../src/sovyx/voice/health/_mixer_kb/profiles/conexant_sn6180_vaio_vjfe69.yaml)
- [KB profile schema (pydantic v2)](../../src/sovyx/voice/health/_mixer_kb/schema.py)
- [Loader + signature verifier](../../src/sovyx/voice/health/_mixer_kb/loader.py)
- [Mission MISSION-voice-mixer-enterprise-refactor-2026-04-25 §3.12](../../docs-internal/diagnostics/MISSION-voice-mixer-enterprise-refactor-2026-04-25.md)
