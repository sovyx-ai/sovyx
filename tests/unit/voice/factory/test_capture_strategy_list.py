"""Pin the platform-specific bypass-strategy lists in voice.factory._capture.

Mission anchor:
``docs-internal/missions/MISSION-voice-linux-silent-mic-remediation-2026-05-04.md``
§Phase 2 T2.3 — when the new strategies T2.1 (WirePlumber default-
source) + T2.2 (ALSA Capture switch) registered into the Linux list,
the order matters: cheapest-first / most-specific-first / mutator-
last is the established convention. A silent re-ordering by a future
refactor would change runtime semantics without flagging in code
review.

These tests pin:

* The exact order of the Linux strategy list (5 entries post-T2.3).
* Windows + macOS lists unaffected by the T2.3 work (still 1 + 0
  respectively).
* Each strategy's stable ``name`` attribute (treated as external API
  per the Strategy Protocol contract).
"""

from __future__ import annotations

from sovyx.voice.factory._capture import _build_bypass_strategies


class TestLinuxStrategyOrder:
    """Mission §Phase 2 T2.3 — Linux strategy list ordering contract."""

    def test_linux_returns_five_strategies(self) -> None:
        strategies = _build_bypass_strategies("linux")
        assert len(strategies) == 5

    def test_linux_strategy_order_is_pinned(self) -> None:
        # Cheapest + most-specific first → riskier mutators last.
        # Pre-T2.3: 3 strategies. Post-T2.3: 5. Order matters because
        # the coordinator iterates this list deterministically — a
        # silent re-shuffle changes which strategy fires first against
        # any given degradation pattern.
        strategies = _build_bypass_strategies("linux")
        names = [s.name for s in strategies]
        assert names == [
            "linux.alsa_mixer_reset",  # 1st: per-card mixer reset (no stream teardown)
            "linux.session_manager_escape",  # 2nd: virtual ↔ hw swap
            "linux.pipewire_direct",  # 3rd: bypass session manager (opt-in)
            "linux.wireplumber_default_source",  # 4th: T2.1, mutates host-wide routing
            "linux.alsa_capture_switch",  # 5th: T2.2, iterates ALL input cards
        ]

    def test_linux_strategy_classes_match_names(self) -> None:
        # Defensive: each entry's runtime type matches its declared
        # name. Catches the case where the lazy-export __getattr__
        # silently resolves the wrong class because of a typo in
        # _LAZY_EXPORTS.
        strategies = _build_bypass_strategies("linux")
        expected = {
            "linux.alsa_mixer_reset": "LinuxALSAMixerResetBypass",
            "linux.session_manager_escape": "LinuxSessionManagerEscapeBypass",
            "linux.pipewire_direct": "LinuxPipeWireDirectBypass",
            "linux.wireplumber_default_source": "LinuxWirePlumberDefaultSourceBypass",
            "linux.alsa_capture_switch": "LinuxALSACaptureSwitchBypass",
        }
        for strategy in strategies:
            assert type(strategy).__name__ == expected[strategy.name], (
                f"strategy name {strategy.name!r} mapped to wrong class "
                f"{type(strategy).__name__!r}; expected {expected[strategy.name]!r}"
            )


class TestNonLinuxStrategyLists:
    """T2.3 must not touch Windows or macOS lists."""

    def test_win32_returns_one_strategy(self) -> None:
        # WindowsWASAPIExclusiveBypass is the only Windows strategy in
        # the current factory wire-up. T28 introduced
        # WindowsHostApiRotateThenExclusiveBypass + WindowsRawCommunicationsBypass
        # but they are not yet registered in _build_bypass_strategies
        # — they are exposed for future explicit registration. Pinning
        # the count here catches an accidental T28 wire-in.
        strategies = _build_bypass_strategies("win32")
        assert len(strategies) == 1
        assert strategies[0].name.startswith("win.")

    def test_darwin_returns_empty_list(self) -> None:
        # macOS bypass strategies (Phase 4 — coreaudio_vpio_off) are
        # not yet shipped. Pinning empty here catches an accidental
        # registration before the strategies are actually implemented.
        strategies = _build_bypass_strategies("darwin")
        assert strategies == []

    def test_unknown_platform_returns_empty_list(self) -> None:
        # Defensive fallback for any future platform_key value
        # (freebsd, etc.).
        strategies = _build_bypass_strategies("freebsd")
        assert strategies == []
