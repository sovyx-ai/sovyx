"""Voice diagnostic tarball synthesis (§8.7 of the calibration mission).

Builds deterministic synthetic tarballs from compact Python specs
instead of committing binary blobs. Each scenario maps to a
``Scenario`` dataclass + the ``build_tarball`` helper materializes
it as a ``.tar.gz`` under a tmp_path that the triage analyzer
(:mod:`sovyx.voice.diagnostics.triage`) consumes verbatim.

Note on directory naming: the mission spec §8.7 lists the path as
``tests/fixtures/voice-diag/`` (with hyphen). We use the underscore
form ``voice_diag/`` because Python's import system rejects hyphens
in package names; the contents + behaviour are identical.

Mission: docs-internal/missions/MISSION-voice-self-calibrating-system-2026-05-05.md §8.7 (v0.30.23).
"""

from tests.fixtures.voice_diag.synth import (  # noqa: F401
    Scenario,
    build_tarball,
    scenario_golden_path,
    scenario_h1_mic_destroyed_apo,
    scenario_h4_pulse_destructive_filter,
    scenario_h5_macos_tcc_denied,
    scenario_h6_selftest_failed,
    scenario_h9_hardware_gap,
    scenario_h10_mixer_attenuated,
    scenario_multi_hypothesis,
)
