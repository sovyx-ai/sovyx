# Voice Mixer Band-Aid Removal — Migration Guide

This guide documents the deprecation + removal path for the legacy
ALSA mixer band-aid functions and their associated config knobs.

> **TL;DR:** The four `linux_mixer_*_fraction` operator-tunable config
> knobs were **removed in v0.32.6**; their values now live as module-
> level constants in `voice/health/_linux_mixer_apply.py`
> (`_BOOST_RESET_FRACTION = 0.0`, `_CAPTURE_RESET_FRACTION = 0.5`,
> `_BOOST_ATTENUATION_FIX_FRACTION = 0.33`, `_CAPTURE_ATTENUATION_FIX_FRACTION = 0.5`).
> Stale `SOVYX_TUNING__VOICE__LINUX_MIXER_*_FRACTION` env overrides
> are silently ignored by pydantic-settings (`extra="ignore"`).
> The two underlying functions `apply_mixer_reset` /
> `apply_mixer_boost_up` remain active (they are the production fix
> path for `sovyx doctor voice --fix --yes` + the dashboard wizard +
> calibration R10), and continue to emit a per-invocation WARN that
> tracks adoption of the L2.5 KB cascade (`apply_mixer_preset`) +
> AGC2 closed-loop (Layer 4) replacement path.

## Why deprecation, why now

The pre-v0.22.5 voice stack tuned ALSA mixer controls via two
hardcoded "fraction of max" functions:

- `apply_mixer_reset(card, controls, tuning)` — REDUCES boost +
  capture controls when the codec is saturated. Driven by
  `_CAPTURE_RESET_FRACTION = 0.5` + `_BOOST_RESET_FRACTION = 0.0`
  (module-level constants since v0.32.6; previously
  `tuning.linux_mixer_capture_reset_fraction` /
  `tuning.linux_mixer_boost_reset_fraction`).
- `apply_mixer_boost_up(card, controls, tuning)` — INCREASES boost +
  capture controls when the codec is attenuated. Driven by
  `_CAPTURE_ATTENUATION_FIX_FRACTION = 0.5` +
  `_BOOST_ATTENUATION_FIX_FRACTION = 0.33` (module-level constants since
  v0.32.6; previously `tuning.linux_mixer_capture_attenuation_fix_fraction` /
  `tuning.linux_mixer_boost_attenuation_fix_fraction`).

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

### Config knobs (REMOVED v0.32.6)

The four `linux_mixer_*_fraction` operator-tunable fields on
`VoiceTuningConfig` were **removed in v0.32.6** after 9 minor cycles
of WARN-only soak (deprecated v0.23.0, original removal target v0.27.0):

- `linux_mixer_boost_reset_fraction` → `_BOOST_RESET_FRACTION = 0.0`
- `linux_mixer_capture_reset_fraction` → `_CAPTURE_RESET_FRACTION = 0.5`
- `linux_mixer_capture_attenuation_fix_fraction` → `_CAPTURE_ATTENUATION_FIX_FRACTION = 0.5`
- `linux_mixer_boost_attenuation_fix_fraction` → `_BOOST_ATTENUATION_FIX_FRACTION = 0.33`

Stale env overrides
(`SOVYX_TUNING__VOICE__LINUX_MIXER_*_FRACTION`) are silently ignored
by pydantic-settings (`extra="ignore"`) — they do not raise, but they
also do not affect runtime. Operators who need codec-specific tuning
should ship a KB profile (Layer 3) rather than override fractions
globally; see `docs/contributing/voice-mixer-kb-profiles.md`.

The boot-time WARN function `warn_on_deprecated_mixer_overrides` and
its companion roster `_DEPRECATED_MIXER_FRACTIONS` were removed in
the same patch.

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
2. (v0.32.6+: no operator action needed for the config knobs — they
   no longer exist; any stale env vars are silently dropped.)
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
2. Open a GitHub issue with the profile_id + the operator's amixer
   dump triplet (before / after / capture wav)

There is no global KB kill-switch config knob: L2.5 runs only when the
cascade caller opts in (`run_cascade(mixer_sanity=MixerSanitySetup(...))`,
default `None` = off) and it rolls back automatically whenever post-apply
validation fails, so a misclassified preset never persists.

## Removal status

**v0.32.6 (Phase 5.C foundation+cleanup):**

1. ✅ The four config knobs are removed from `VoiceTuningConfig`
   (their values are now module-level constants in
   `voice/health/_linux_mixer_apply.py`).
2. ✅ The deprecation WARN surface
   (`warn_on_deprecated_mixer_overrides` + `_DEPRECATED_MIXER_FRACTIONS`
   roster + `voice.config.deprecated_mixer_fraction_in_use` event) is removed.
3. ✅ The factory boot-time WARN call site is removed from
   `voice/factory/__init__.py`.
4. ❌ The two deprecated functions `apply_mixer_reset` /
   `apply_mixer_boost_up` are **NOT yet removed** — they are the
   production fix path for `sovyx doctor voice --fix --yes`, the
   dashboard wizard, and calibration rule R10. The per-invocation
   WARN (`voice.deprecation.legacy_mixer_band_aid_call`) continues to
   fire so dashboards can attribute the call-site graph and drive
   adoption of the L2.5 KB cascade replacement.

The release notes for v0.32.6 cite this migration guide. Functions
will be removed only after KB cascade adoption reaches zero
`voice.deprecation.legacy_mixer_band_aid_call` events across one full
minor cycle of pilot soak.

## Reference

- Mission: `MISSION-voice-100pct-autonomous-2026-04-25.md` §1.8 + step 17
- Original refactor: `MISSION-voice-mixer-enterprise-refactor-2026-04-25.md` §3.12
- L2.5 KB cascade: `docs/contributing/voice-mixer-kb-profiles.md`
- Signing key rotation: `docs/contributing/voice-kb-rotation.md`
- Deprecated functions: `apply_mixer_reset` / `apply_mixer_boost_up` in `src/sovyx/voice/health/_linux_mixer_apply.py`
- Replacement function: `src/sovyx/voice/health/_linux_mixer_apply.py:apply_mixer_preset`
