# Mixer KB — shipped profiles

This directory holds YAML profiles that describe hardware-specific mixer
presets for the Voice Mixer Sanity L2.5 layer.

## What lives here

Each `*.yaml` file in this directory is a
[MixerKBProfile](../../contract.py) — one per hardware class.

Files are loaded at startup by
[`MixerKBLookup.load_shipped`](../__init__.py) and validated via the
pydantic schema in [schema.py](../schema.py).

**Files prefixed with `_` (e.g. `_index.yaml`) are reserved for loader
metadata and skipped by the profile enumeration.**

## Phase F1 ships empty

Phase F1 (v0.22.0) ships the loader infrastructure only. Production
profile content lands in Phase F1.H per the
[V2 Master Plan](../../../../../docs-internal/missions/VOICE-MIXER-SANITY-L2.5-MASTER-PLAN-v2.md)
— each profile requires HIL attestation captured on the target hardware
(pilot + reference laptops) before merge, per `F.3 Contribution workflow`.

## Adding a profile

See `docs/contributing.md` (Phase F1.K doc section). Each PR must include:

1. `<profile_id>.yaml` here
2. `tests/fixtures/voice/mixer/<profile_id>_before.txt` (pre-apply amixer dump)
3. `tests/fixtures/voice/mixer/<profile_id>_after.txt` (post-apply dump)
4. `tests/fixtures/voice/mixer/<profile_id>_capture.wav` (3 s validation audio)
5. `tests/unit/voice/health/mixer_kb/test_<profile_id>.py`

A reviewer pair validates the PR; one reviewer must confirm HIL on
matching hardware or document an explicit exception.

## Schema reference

See [Appendix 2 of the V2 Master Plan](../../../../../docs-internal/missions/VOICE-MIXER-SANITY-L2.5-MASTER-PLAN-v2.md#appendix-2--kb-profile-yaml-schema-v1)
for the complete YAML schema.
