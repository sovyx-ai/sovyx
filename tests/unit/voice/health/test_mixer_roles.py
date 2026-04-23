"""Tests for the MixerControlRoleResolver (L2.5 Phase F1.B)."""

from __future__ import annotations

from sovyx.voice.health import (
    HardwareContext,
    MixerCardSnapshot,
    MixerControlRole,
    MixerControlRoleResolver,
    MixerControlSnapshot,
)
from sovyx.voice.health._linux_mixer_probe import (
    _BOOST_CONTROL_PATTERNS,  # noqa: PLC2701 — regression guard across modules
)
from sovyx.voice.health._mixer_roles import (
    _CODEC_OVERRIDE_TABLE,  # noqa: PLC2701 — assert shipped seed matches pilot
    _HDA_ROLE_TABLE,  # noqa: PLC2701
    _SUBSTRING_FALLBACK,  # noqa: PLC2701
)


def _gain_control(name: str) -> MixerControlSnapshot:
    """Build a plausible gain-bearing control snapshot for a given name."""
    return MixerControlSnapshot(
        name=name,
        min_raw=0,
        max_raw=80,
        current_raw=40,
        current_db=-18.0,
        max_db=0.0,
        is_boost_control=False,
        saturation_risk=False,
        asymmetric=False,
    )


def _card(controls: tuple[MixerControlSnapshot, ...]) -> MixerCardSnapshot:
    return MixerCardSnapshot(
        card_index=0,
        card_id="Generic",
        card_longname="HDA Intel PCH",
        controls=controls,
        aggregated_boost_db=0.0,
        saturation_warning=False,
    )


class TestResolveHDATable:
    """Layer 2: driver-family exact-match resolves HDA control names."""

    def test_capture_maps_to_master(self) -> None:
        r = MixerControlRoleResolver()
        assert r.resolve("hda", None, "Capture") == MixerControlRole.CAPTURE_MASTER

    def test_internal_mic_boost_maps_exact(self) -> None:
        r = MixerControlRoleResolver()
        assert r.resolve("hda", None, "Internal Mic Boost") == MixerControlRole.INTERNAL_MIC_BOOST

    def test_mic_boost_maps_to_preamp(self) -> None:
        r = MixerControlRoleResolver()
        assert r.resolve("hda", None, "Mic Boost") == MixerControlRole.PREAMP_BOOST

    def test_front_and_rear_mic_boost_both_preamp(self) -> None:
        r = MixerControlRoleResolver()
        assert r.resolve("hda", None, "Front Mic Boost") == MixerControlRole.PREAMP_BOOST
        assert r.resolve("hda", None, "Rear Mic Boost") == MixerControlRole.PREAMP_BOOST

    def test_auto_mute_mode(self) -> None:
        r = MixerControlRoleResolver()
        assert r.resolve("hda", None, "Auto-Mute Mode") == MixerControlRole.AUTO_MUTE

    def test_capture_switch(self) -> None:
        r = MixerControlRoleResolver()
        assert r.resolve("hda", None, "Capture Switch") == MixerControlRole.CAPTURE_SWITCH

    def test_input_source(self) -> None:
        r = MixerControlRoleResolver()
        assert r.resolve("hda", None, "Input Source") == MixerControlRole.INPUT_SOURCE_SELECTOR

    def test_digital_capture_volume(self) -> None:
        r = MixerControlRoleResolver()
        assert r.resolve("hda", None, "Digital Capture Volume") == MixerControlRole.DIGITAL_CAPTURE

    def test_case_insensitive(self) -> None:
        r = MixerControlRoleResolver()
        assert r.resolve("hda", None, "CAPTURE") == MixerControlRole.CAPTURE_MASTER
        assert r.resolve("hda", None, "internal mic boost") == MixerControlRole.INTERNAL_MIC_BOOST

    def test_empty_name_is_unknown(self) -> None:
        r = MixerControlRoleResolver()
        assert r.resolve("hda", None, "") == MixerControlRole.UNKNOWN

    def test_unknown_hda_control_falls_through_to_unknown(self) -> None:
        r = MixerControlRoleResolver()
        # No substring of _SUBSTRING_FALLBACK matches.
        assert r.resolve("hda", None, "SomeExoticVendorControl") == MixerControlRole.UNKNOWN


class TestResolveSubstringFallback:
    """Layer 3: substring fallback catches non-HDA families + unmapped names."""

    def test_unknown_family_uses_fallback(self) -> None:
        r = MixerControlRoleResolver()
        assert r.resolve("unknown", None, "Capture") == MixerControlRole.CAPTURE_MASTER

    def test_usb_family_falls_back(self) -> None:
        # F1 ships no USB table → substring fallback still catches common names.
        r = MixerControlRoleResolver()
        assert r.resolve("usb-audio", None, "Capture") == MixerControlRole.CAPTURE_MASTER

    def test_specific_beats_general_in_fallback(self) -> None:
        # "Internal Mic Boost" contains "Mic Boost" as substring — specific
        # match must win (INTERNAL_MIC_BOOST), not general (PREAMP_BOOST).
        r = MixerControlRoleResolver(driver_family_tables={})  # force Layer 3
        assert r.resolve("hda", None, "Internal Mic Boost") == MixerControlRole.INTERNAL_MIC_BOOST

    def test_front_mic_boost_specific_before_mic_boost(self) -> None:
        r = MixerControlRoleResolver(driver_family_tables={})
        # "Front Mic Boost" could match "mic boost" first alphabetically —
        # order in _SUBSTRING_FALLBACK must put "front mic boost" first.
        assert r.resolve("hda", None, "Front Mic Boost Volume") == MixerControlRole.PREAMP_BOOST

    def test_digital_capture_specific_before_capture(self) -> None:
        r = MixerControlRoleResolver(driver_family_tables={})
        assert (
            r.resolve("hda", None, "Digital Capture Volume X") == MixerControlRole.DIGITAL_CAPTURE
        )

    def test_capture_switch_specific_before_capture(self) -> None:
        r = MixerControlRoleResolver(driver_family_tables={})
        assert r.resolve("hda", None, "Capture Switch") == MixerControlRole.CAPTURE_SWITCH

    def test_line_boost_maps_to_preamp(self) -> None:
        r = MixerControlRoleResolver(driver_family_tables={})
        assert r.resolve("hda", None, "Line Boost") == MixerControlRole.PREAMP_BOOST


class TestResolveCodecOverride:
    """Layer 1: per-codec override wins over both table and fallback."""

    def test_sn6180_shipped_in_override_table(self) -> None:
        assert "14F1:5045" in _CODEC_OVERRIDE_TABLE

    def test_override_wins_over_family_table(self) -> None:
        # Custom override that deliberately disagrees with HDA table.
        custom = {"14F1:5045": {"capture": MixerControlRole.PGA_MASTER}}
        r = MixerControlRoleResolver(codec_override_table=custom)
        assert r.resolve("hda", "14F1:5045", "Capture") == MixerControlRole.PGA_MASTER

    def test_override_misses_fall_through_to_family(self) -> None:
        custom = {"14F1:5045": {"capture": MixerControlRole.PGA_MASTER}}
        r = MixerControlRoleResolver(codec_override_table=custom)
        # Override table has no "internal mic boost" → fall through to HDA table.
        assert (
            r.resolve("hda", "14F1:5045", "Internal Mic Boost")
            == MixerControlRole.INTERNAL_MIC_BOOST
        )

    def test_no_override_for_unknown_codec(self) -> None:
        r = MixerControlRoleResolver()
        # Unknown codec_id → skip Layer 1, Layer 2 still resolves.
        assert r.resolve("hda", "ABCD:EF01", "Capture") == MixerControlRole.CAPTURE_MASTER

    def test_codec_id_none_skips_layer_1(self) -> None:
        r = MixerControlRoleResolver()
        assert r.resolve("hda", None, "Capture") == MixerControlRole.CAPTURE_MASTER


class TestResolveCard:
    """resolve_card groups snapshot controls by role (tuple-valued)."""

    def test_empty_snapshot_returns_empty_mapping(self) -> None:
        r = MixerControlRoleResolver()
        hw = HardwareContext(driver_family="hda")
        out = r.resolve_card(_card(()), hw)
        assert out == {}

    def test_single_control_single_role(self) -> None:
        r = MixerControlRoleResolver()
        hw = HardwareContext(driver_family="hda")
        capture = _gain_control("Capture")
        out = r.resolve_card(_card((capture,)), hw)
        assert out == {MixerControlRole.CAPTURE_MASTER: (capture,)}

    def test_multiple_controls_same_role_grouped(self) -> None:
        # Desktop HDA has Front Mic Boost + Rear Mic Boost both PREAMP_BOOST.
        r = MixerControlRoleResolver()
        hw = HardwareContext(driver_family="hda")
        front = _gain_control("Front Mic Boost")
        rear = _gain_control("Rear Mic Boost")
        out = r.resolve_card(_card((front, rear)), hw)
        # Order preserved — front first, rear second.
        assert out == {MixerControlRole.PREAMP_BOOST: (front, rear)}

    def test_order_preserved_across_roles(self) -> None:
        r = MixerControlRoleResolver()
        hw = HardwareContext(driver_family="hda")
        capture = _gain_control("Capture")
        boost = _gain_control("Internal Mic Boost")
        out = r.resolve_card(_card((capture, boost)), hw)
        assert out[MixerControlRole.CAPTURE_MASTER] == (capture,)
        assert out[MixerControlRole.INTERNAL_MIC_BOOST] == (boost,)

    def test_unknown_role_surfaced_under_unknown_key(self) -> None:
        # Telemetry needs visibility into controls that couldn't be mapped.
        r = MixerControlRoleResolver()
        hw = HardwareContext(driver_family="hda")
        mystery = _gain_control("SomeExoticVendorControl")
        out = r.resolve_card(_card((mystery,)), hw)
        assert out == {MixerControlRole.UNKNOWN: (mystery,)}

    def test_codec_override_used_during_card_resolution(self) -> None:
        custom = {"14F1:5045": {"capture": MixerControlRole.PGA_MASTER}}
        r = MixerControlRoleResolver(codec_override_table=custom)
        hw = HardwareContext(driver_family="hda", codec_id="14F1:5045")
        capture = _gain_control("Capture")
        out = r.resolve_card(_card((capture,)), hw)
        assert out == {MixerControlRole.PGA_MASTER: (capture,)}


class TestConstructorInjection:
    """Tables can be overridden at construction for targeted tests."""

    def test_empty_tables_collapse_to_fallback_only(self) -> None:
        r = MixerControlRoleResolver(
            codec_override_table={},
            driver_family_tables={},
        )
        # Substring fallback still in place → "Capture" still resolves.
        assert r.resolve("hda", None, "Capture") == MixerControlRole.CAPTURE_MASTER

    def test_empty_fallback_collapses_to_unknown(self) -> None:
        r = MixerControlRoleResolver(
            codec_override_table={},
            driver_family_tables={},
            substring_fallback=(),
        )
        assert r.resolve("hda", None, "Capture") == MixerControlRole.UNKNOWN

    def test_custom_driver_family_table(self) -> None:
        # Future SOF table — tests can inject today via constructor.
        sof = {"pga1.0 1 master capture volume": MixerControlRole.PGA_MASTER}
        r = MixerControlRoleResolver(
            driver_family_tables={"sof": sof},
        )
        assert (
            r.resolve("sof", None, "PGA1.0 1 Master Capture Volume") == MixerControlRole.PGA_MASTER
        )


class TestBoostPatternConsistency:
    """Regression guard: every _BOOST_CONTROL_PATTERNS entry resolves to a role.

    The probe (``_linux_mixer_probe.py``) flags controls as
    ``is_boost_control`` when their name matches any of these patterns.
    If a pattern here doesn't resolve via the resolver, the apply layer
    would receive a "boost" control with no target role — undefined
    behaviour. This test fails fast when _BOOST_CONTROL_PATTERNS gains
    a pattern that doesn't exist in _SUBSTRING_FALLBACK or driver tables.
    """

    def test_every_boost_pattern_has_role(self) -> None:
        r = MixerControlRoleResolver(
            codec_override_table={},
            driver_family_tables={},
        )
        unresolved: list[str] = []
        for pattern in _BOOST_CONTROL_PATTERNS:
            role = r.resolve("unknown", None, pattern)
            if role is MixerControlRole.UNKNOWN:
                unresolved.append(pattern)
        assert not unresolved, (
            f"_BOOST_CONTROL_PATTERNS entries without a resolver match: "
            f"{unresolved}. Add them to _SUBSTRING_FALLBACK in _mixer_roles.py."
        )

    def test_every_hda_table_entry_has_expected_role(self) -> None:
        # _HDA_ROLE_TABLE is authoritative for its keys; consistency invariant
        # — every HDA table value must be a real MixerControlRole (not a typo).
        for name, role in _HDA_ROLE_TABLE.items():
            assert isinstance(role, MixerControlRole), (
                f"_HDA_ROLE_TABLE[{name!r}] is not a MixerControlRole"
            )
            assert role is not MixerControlRole.UNKNOWN, (
                f"_HDA_ROLE_TABLE[{name!r}] maps to UNKNOWN — misconfig"
            )

    def test_substring_fallback_is_ordered_specific_first(self) -> None:
        # Invariant: "internal mic boost" appears before "mic boost";
        # "capture switch" + "digital capture" appear before "capture".
        patterns = [p for p, _ in _SUBSTRING_FALLBACK]

        def idx(pattern: str) -> int:
            return patterns.index(pattern)

        assert idx("internal mic boost") < idx("mic boost")
        assert idx("front mic boost") < idx("mic boost")
        assert idx("rear mic boost") < idx("mic boost")
        assert idx("digital capture") < idx("capture")
        assert idx("capture switch") < idx("capture")
