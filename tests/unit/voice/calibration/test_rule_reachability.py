"""W1.3 — calibration rule reachability honesty (anti-pattern #48).

Some calibration rules exist + are unit-tested with synthetic inputs but
cannot fire from real captured data because their ``applies()`` gate reads
a field that has no producer yet (hardcoded sentinel). Per anti-pattern
#48 those rules are NOT deleted (they are scaffolding for a future
producer, exactly like the Windows bypass Tier 1/2 stubs); instead each
self-declares an ``unreachable_reason`` so the ``--evaluate-rules`` preview
discloses it and a future maintainer who wires the producer must remove
the marker. This test locks both sides of that contract.
"""

from __future__ import annotations

from sovyx.voice.calibration.rules import iter_rules, iter_unreachable_rules

# Locked set — wiring a producer (W3/W4 of the deep-investigation mission)
# MUST remove the rule's ``unreachable_reason`` AND update this set, so the
# declaration can never silently rot back into "looks live".
_KNOWN_UNREACHABLE = {
    "R20_windows_apo_active",  # fingerprint.apo_active hardcoded False (no producer)
    "R40_macos_tcc_denied",  # distro_id never "macos" + no Darwin triage producer (MACOS-3)
    "R70_capture_mode_exclusive",  # latency/jitter measurements hardcoded 0.0
    "R80_aec_engine",  # echo_correlation_db hardcoded None
}


def test_unreachable_set_matches_declared_markers() -> None:
    declared = {rule_id for rule_id, _ in iter_unreachable_rules()}
    assert declared == _KNOWN_UNREACHABLE


def test_every_unreachable_rule_has_a_nonempty_reason() -> None:
    for rule_id, reason in iter_unreachable_rules():
        assert reason.strip(), f"{rule_id} declares an empty unreachable_reason"


def test_reachable_rules_do_not_declare_unreachable() -> None:
    # R10 (mic-attenuated) is the only auto-applying rule and is genuinely
    # live on Linux — it must NOT be marked unreachable.
    declared = {rule_id for rule_id, _ in iter_unreachable_rules()}
    assert "R10_mic_attenuated" not in declared


def test_unreachable_rules_are_still_discovered() -> None:
    # The honest fix documents the gap; it does NOT delete the scaffolding,
    # so the rules stay discoverable (RULE_SET_VERSION drift accounting +
    # the future producer wire-up both depend on them existing).
    all_ids = {rule.rule_id for rule in iter_rules()}
    assert all_ids >= _KNOWN_UNREACHABLE
