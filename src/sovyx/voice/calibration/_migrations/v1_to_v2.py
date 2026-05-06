"""Identity placeholder for the v1→v2 migration step.

When the calibration profile schema actually bumps to v2 (operator-
visible reshape: e.g. adding ``fingerprint.audio_config_hash`` for L4
KB lookups, or a new measurement field), replace this identity body
with the real reshape logic. The registry contract is unchanged; the
loader walks the same chain.

Until v2 ships, this function exists so:

* The registry has a registered edge for `(1, 2)` — operators
  re-running with a future Sovyx that DOES bump
  :data:`CALIBRATION_PROFILE_SCHEMA_VERSION` to 2 will see their
  v1 profiles migrate cleanly (the migration runs, the schema
  version bumps, and the loader proceeds).
* Tests can exercise the chain walker against a registered edge
  even though the runtime still ships v1.

History: introduced in v0.30.33 as P5.T2 of mission
``MISSION-voice-calibration-extreme-audit-2026-05-06.md`` §9.
"""

from __future__ import annotations

from typing import Any

from sovyx.voice.calibration._migrations import register_migration


@register_migration(from_v=1, to_v=2)
def migrate_v1_to_v2(raw: dict[str, Any]) -> dict[str, Any]:
    """Identity migration v1→v2: bump ``schema_version`` only.

    Replace this body with the real reshape when v2 ships. Pure-
    function contract: no IO, no time, no randomness — the property
    test verifies idempotency by running this function twice on the
    same input.
    """
    raw["schema_version"] = 2
    return raw
