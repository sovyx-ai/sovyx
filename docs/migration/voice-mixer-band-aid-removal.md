# Voice Mixer Band-Aid Removal — Migration Guide (v0.23.0 → v0.24.0)

This guide documents the deprecation + removal path for the legacy
ALSA mixer band-aid functions and their associated config knobs.

> **TL;DR:** The four `linux_mixer_*_fraction` config knobs and the two
> functions `apply_mixer_reset` / `apply_mixer_boost_up` are
> **deprecated in v0.23.0** and **scheduled for removal in v0.24.0**.
> The L2.5 KB cascade (`apply_mixer_preset`) + the AGC2 closed-loop
> (Layer 4) replace them. Operators using the legacy path see one
> structured WARN per invocation; CI / dashboards can attribute the
> WARN count to drive cleanup before v0.24.0 lands.

## Why deprecation, why now

The pre-v0.22.5 voice stack tuned ALSA mixer controls via two
hardcoded "fraction of max" functions:

- `apply_mixer_reset(card, controls, tuning)` — REDUCES boost +
  capture controls when the codec is saturated. Driven by
  `tuning.linux_mixer_capture_reset_fraction` (default 0.5) +
  `tuning.linux_mixer_boost_reset_fraction` (default 0.0).
- `apply_mixer_boost_up(card, controls, tuning)` — INCREASES boost +
  capture controls when the codec is attenuated. Driven by
  `tuning.linux_mixer_capture_attenuation_fix_fraction` (default 0.5)
  + `tuning.linux_mixer_boost_attenuation_fix_fraction` (default 0.33).

The fractions were tuned on ONE pilot host (the VAIO VJFE69F11X-B0221H
with the Conexant SN6180 codec). Mission
`MISSION-voice-mixer-enterprise-refactor-2026-04-25.md` §1.1 documents
the regime-flip that motivated the L2.5 KB cascade refactor: the v0.22.2
defaults of 0.75 / 0.66 lifted attenuated controls past the saturation
ceiling, flipping the regime — fix detected attenuation, applied boost,
re-probe reported saturation, deaf-detection still triggered with a
CLIPPED signal instead of a SILENT one.

The L2.5 KB cascade replaces the two hardcoded fractions with a
**per-codec preset catalogue** (`MixerKBProfile` YAML profiles signed
with Ed25519, validated against post-apply RMS + Silero gates) + a
**universal Layer-4 fallback** (in-process AGC2 closed-loop digital
gain that recovers below-VAD-floor signals without mutating the OS-
level mixer). Both are precise where the legacy path was approximate.

## What deprecates in v0.23.0

### Functions

The two functions emit a structured WARN at every invocation:

```
{
  "event": "voice.deprecation.legacy_mixer_band_aid_call",
  "voice.function": "apply_mixer_reset",  # or apply_mixer_boost_up
  "voice.removal_target": "v0.24.0",
  "voice.replacement": "L2.5 KB cascade (apply_mixer_preset) + AGC2 closed-loop (Layer 4)",
  "voice.action_required": "Migrate the call site to apply_mixer_preset"
}
```

### Config knobs

The four `linux_mixer_*_fraction` fields on
`VoiceTuningConfig` are deprecated. Setting them to non-default values
emits a structured WARN at boot via
`engine/config.warn_on_deprecated_mixer_overrides`:

- `linux_mixer_boost_reset_fraction` (default 0.0)
- `linux_mixer_capture_reset_fraction` (default 0.5)
- `linux_mixer_capture_attenuation_fix_fraction` (default 0.5)
- `linux_mixer_boost_attenuation_fix_fraction` (default 0.33)

The env-var equivalents
(`SOVYX_TUNING__VOICE__LINUX_MIXER_*`) are also deprecated.

## How to migrate

### Use the L2.5 KB cascade

Replace the legacy reset / boost-up call:

```python
# Before (v0.22.x)
from sovyx.voice.health._linux_mixer_apply import apply_mixer_reset

snapshot = await apply_mixer_reset(
    card_index=0,
    controls_to_reset=saturated_controls,
    tuning=tuning,
)
```

```python
# After (v0.23.0+)
from sovyx.voice.health._linux_mixer_apply import apply_mixer_preset
from sovyx.voice.health._mixer_kb import MixerKBLookup

kb = MixerKBLookup.load_shipped()
profile = kb.match(hardware_context)  # pick the profile for this codec
if profile is not None:
    snapshot = await apply_mixer_preset(
        card_index=0,
        preset=profile.recommended_preset,
        snapshots=current_mixer_snapshots,
        tuning=tuning,
    )
else:
    # No KB profile match → AGC2 Layer-4 fallback handles it.
    pass
```

The KB cascade is **declarative**: profiles are YAML files signed by
the Sovyx team. Ship a new codec profile via the contribution path
documented in `docs/contributing/voice-mixer-kb-profiles.md`.

### Disable the legacy path entirely

If you don't need the legacy band-aid (e.g. your codec is already
covered by a shipped KB profile OR AGC2 Layer-4 alone is sufficient):

1. Stop calling `apply_mixer_reset` + `apply_mixer_boost_up` from
   your code.
2. Set the four fraction config fields back to their defaults
   (or remove them entirely from your YAML / env).
3. Confirm via the WARN count: `grep voice.deprecation.legacy_mixer_band_aid_call`
   in your structured log should return zero results across the soak
   window.

## Rollback path if KB cascade misclassifies

The L2.5 KB cascade applies a preset only when:

1. A profile matches the active codec via `match_keys`
2. The `factory_signature` matches the pre-apply state within
   tolerance
3. The post-apply state passes the validation gates
   (RMS in range + Silero ≥ threshold + peak below clipping)

If post-apply validation fails, the cascade rolls back to the pre-apply
state automatically (`apply_mixer_preset` carries the same atomicity
contract as `apply_mixer_reset` / `apply_mixer_boost_up` did).

If a misclassification is suspected (operator reports voice degrading
post-v0.23.0):

1. Check `voice.cascade.boot_decision` log for the matched profile_id
   and the validation outcome
2. Set `voice_kb_disabled=true` env to skip KB cascade entirely; the
   legacy fractions take over (but emit deprecation WARN)
3. Open a GitHub issue with the profile_id + the operator's amixer
   dump triplet (before / after / capture wav)

## Removal in v0.24.0

When v0.24.0 ships:

1. The two deprecated functions are deleted from `_linux_mixer_apply.py`
2. The four config knobs are removed from `VoiceTuningConfig`
3. The deprecation WARN surface is removed
4. Any test or config still referencing the deleted names fails loud
   (no silent silent-shim)

This corresponds to mission §9.1.1 acceptance criterion: "zero hits"
for `apply_mixer_reset` / `apply_mixer_boost_up` /
`linux_mixer_*_fraction` in `src/sovyx/`.

The release notes for v0.24.0 will state the deletion explicitly + link
back to this migration guide. Pilot data from B7 (Windows), C5 (macOS),
and E3 (3-OS) gates the v0.24.0 release; the deprecated path stays
callable until pilots are green.

## Reference

- Mission: `MISSION-voice-100pct-autonomous-2026-04-25.md` §1.8 + step 17
- Original refactor: `MISSION-voice-mixer-enterprise-refactor-2026-04-25.md` §3.12
- L2.5 KB cascade: `docs/contributing/voice-mixer-kb-profiles.md`
- Signing key rotation: `docs/contributing/voice-kb-rotation.md`
- Deprecated functions: `src/sovyx/voice/health/_linux_mixer_apply.py:94, 200`
- Replacement function: `src/sovyx/voice/health/_linux_mixer_apply.py:apply_mixer_preset`
